"""
AI 客户端层 — 统一通过 OpenAI 兼容接口调用。

支持流式对话、连通性测试、API 临时故障的自动重试（指数退避）。
供应商 / 模型由数据库目录解析后再传入。
默认关闭思考链：RP 场景下思考只会拖慢出字并污染角色回复。
"""
import asyncio
import logging
import re
import time
from typing import AsyncGenerator, Dict, List, Tuple

import httpx
from openai import (
    AsyncOpenAI,
    APIError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

_API_RETRY_MAX = 2
_API_RETRY_DELAY = 1.0

# 常见 OpenAI 兼容口关闭思考的 extra_body；不被接受时自动去掉再试
_THINKING_OFF_EXTRA = {
    "enable_thinking": False,
    "chat_template_kwargs": {"enable_thinking": False},
}

_THINK_START = re.compile(
    r"<think(?:ing)?>|◁think▷|<redacted_thinking>",
    re.IGNORECASE,
)
_THINK_END = re.compile(
    r"</think(?:ing)?>|◁/think▷|</redacted_thinking>",
    re.IGNORECASE,
)
_THINK_HOLD = re.compile(
    r"<(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?)?|◁(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$",
    re.IGNORECASE,
)


class _ThinkStripper:
    """从流式文本中丢掉思考块，避免泄漏进角色回复。"""

    def __init__(self):
        self._buf = ""
        self._hiding = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._hiding:
                m = _THINK_END.search(self._buf)
                if not m:
                    if len(self._buf) > 32:
                        self._buf = self._buf[-32:]
                    break
                self._buf = self._buf[m.end():]
                self._hiding = False
                if self._buf.startswith("\n"):
                    self._buf = self._buf[1:]
                continue
            m = _THINK_START.search(self._buf)
            if not m:
                hold = 0
                hm = _THINK_HOLD.search(self._buf)
                if hm and hm.end() == len(self._buf):
                    hold = len(hm.group(0))
                if hold:
                    out.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                else:
                    out.append(self._buf)
                    self._buf = ""
                break
            out.append(self._buf[:m.start()])
            self._buf = self._buf[m.end():]
            self._hiding = True
        return "".join(out)

    def flush(self) -> str:
        if self._hiding:
            self._buf = ""
            return ""
        s = self._buf
        self._buf = ""
        return s


class AIClientError(Exception):
    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code


class GenerationCancelled(Exception):
    """调用方主动停止生成，不算上游故障。"""


class AIClient:
    def __init__(self):
        self._clients: Dict[Tuple[str, str, float], AsyncOpenAI] = {}
        self._thinking_extra_rejected: set[str] = set()

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self._thinking_extra_rejected.clear()

    def _get_client(self, base_url: str, api_key: str, timeout: float = 60) -> AsyncOpenAI:
        cache_key = (base_url, api_key, timeout)
        if cache_key not in self._clients:
            self._clients[cache_key] = AsyncOpenAI(
                api_key=api_key, base_url=base_url, timeout=timeout,
            )
        return self._clients[cache_key]

    def _build_kwargs(
        self,
        messages: List[Dict],
        model_id: str,
        max_tokens: int | None,
        params: dict | None,
        stream: bool,
    ) -> dict:
        kwargs = dict(model=model_id, messages=messages, stream=stream)
        if params:
            for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                if k in params:
                    kwargs[k] = params[k]
        if params and params.get("num_predict") is not None:
            requested = params["num_predict"]
            cap = max_tokens if max_tokens is not None else requested
            max_tokens = min(requested, cap)
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return kwargs

    def _with_thinking_off(self, kwargs: dict, base_url: str) -> dict:
        if base_url in self._thinking_extra_rejected:
            return kwargs
        out = dict(kwargs)
        extra = dict(out.get("extra_body") or {})
        extra.update(_THINKING_OFF_EXTRA)
        out["extra_body"] = extra
        return out

    def _reject_thinking_extra(self, base_url: str) -> None:
        if base_url not in self._thinking_extra_rejected:
            self._thinking_extra_rejected.add(base_url)
            logger.info(f"供应商不接受关闭思考参数，后续不再附带: {base_url}")

    @staticmethod
    def _looks_like_unknown_param(exc: Exception) -> bool:
        msg = str(exc).lower()
        keys = (
            "enable_thinking",
            "chat_template_kwargs",
            "unknown parameter",
            "unrecognized",
            "unexpected keyword",
            "extra inputs",
            "not a valid parameter",
            "invalid parameter",
            "未知参数",
            "不支持的参数",
        )
        return any(k in msg for k in keys)

    @staticmethod
    def _delta_text(chunk) -> str:
        if not chunk.choices:
            return ""
        delta = chunk.choices[0].delta
        return (getattr(delta, "content", None) or "") if delta is not None else ""

    async def stream_chat(
        self,
        messages: List[Dict],
        model_id: str,
        base_url: str,
        api_key: str,
        max_tokens: int | None = None,
        params: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        on_stream=None,
    ) -> AsyncGenerator[str, None]:
        """异步生成器，逐块产出回复文本（适配 SSE）。

        cancel_event 被设置或 on_stream 登记的流被关闭时，抛出 GenerationCancelled。
        """
        client = self._get_client(base_url, api_key)
        base_kwargs = self._build_kwargs(messages, model_id, max_tokens, params, stream=True)

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        last_exception = None
        started = False
        stripper = _ThinkStripper()
        use_extra = base_url not in self._thinking_extra_rejected
        for attempt in range(_API_RETRY_MAX + 1):
            stream = None
            try:
                if _cancelled():
                    raise GenerationCancelled()
                kwargs = self._with_thinking_off(base_kwargs, base_url) if use_extra else base_kwargs
                stream = await client.chat.completions.create(**kwargs)
                if on_stream:
                    on_stream(stream)
                if _cancelled():
                    raise GenerationCancelled()
                async for chunk in stream:
                    if _cancelled():
                        raise GenerationCancelled()
                    text = self._delta_text(chunk)
                    if not text:
                        continue
                    started = True
                    visible = stripper.feed(text)
                    if visible:
                        yield visible
                if _cancelled():
                    raise GenerationCancelled()
                tail = stripper.flush()
                if tail:
                    yield tail
                return
            except GenerationCancelled:
                raise
            except (RateLimitError, APITimeoutError, APIConnectionError, APIStatusError,
                    APIError, httpx.TimeoutException, httpx.NetworkError) as e:
                if _cancelled():
                    raise GenerationCancelled() from e
                if started:
                    logger.warning(f"流式响应已开始后中断（不重试，避免内容重复）: {e}")
                    raise AIClientError("回复生成中途中断，请重试", "stream_interrupted") from e
                last_exception = e
                status = getattr(e, "status_code", None)

                if (
                    use_extra
                    and isinstance(e, APIStatusError)
                    and status == 400
                    and self._looks_like_unknown_param(e)
                ):
                    self._reject_thinking_extra(base_url)
                    use_extra = False
                    stripper = _ThinkStripper()
                    continue

                if isinstance(e, RateLimitError) or status == 429:
                    logger.warning(f"API 速率限制 (尝试 {attempt + 1}): {e}")
                    if attempt < _API_RETRY_MAX:
                        await asyncio.sleep(_API_RETRY_DELAY * (2 ** attempt))
                        continue
                    raise AIClientError("API 请求频率过高，请稍后重试", "rate_limited") from e

                if isinstance(e, (APITimeoutError, httpx.TimeoutException)):
                    logger.warning(f"API 请求超时 (尝试 {attempt + 1}): {e}")
                    if attempt < _API_RETRY_MAX:
                        await asyncio.sleep(_API_RETRY_DELAY)
                        continue
                    raise AIClientError("API 请求超时，请检查网络连接", "timeout") from e

                if isinstance(e, (APIConnectionError, httpx.NetworkError)):
                    logger.warning(f"网络错误 (尝试 {attempt + 1}): {e}")
                    if attempt < _API_RETRY_MAX:
                        await asyncio.sleep(_API_RETRY_DELAY * (2 ** attempt))
                        continue
                    raise AIClientError(f"网络连接失败，请检查网络: {e}", "network_error") from e

                if isinstance(e, APIStatusError):
                    if status == 401:
                        raise AIClientError("API 认证失败，请到「模型配置」检查该供应商的密钥", "auth_failed") from e
                    if status == 403:
                        raise AIClientError("API 权限不足，请检查账户权限", "permission_denied") from e
                    if status == 404:
                        raise AIClientError("接口或模型不存在，请检查基础 URL 与 model-id", "not_found") from e
                    if status is not None and status >= 500:
                        logger.error(f"API 服务错误 (尝试 {attempt + 1}): {e}")
                        if attempt < _API_RETRY_MAX:
                            await asyncio.sleep(_API_RETRY_DELAY * (2 ** attempt))
                            continue
                        raise AIClientError(f"API 服务暂时不可用 ({status})", "service_unavailable") from e
                    raise AIClientError(f"API 请求被拒绝 ({status})", "request_failed") from e

                logger.warning(f"API 请求失败 (尝试 {attempt + 1}): {e}")
                if attempt < _API_RETRY_MAX:
                    await asyncio.sleep(_API_RETRY_DELAY * (2 ** attempt))
                    continue
                raise AIClientError(f"API 请求失败: {e}", "request_failed") from e
            finally:
                if on_stream:
                    on_stream(None)
                close = getattr(stream, "close", None)
                if close:
                    try:
                        result = close()
                        if hasattr(result, "__await__"):
                            await result
                    except Exception:
                        logger.debug("关闭上游模型流时出错", exc_info=True)

        raise AIClientError(
            f"API 请求失败 (已重试 {_API_RETRY_MAX} 次): {last_exception}",
            "request_failed",
        )

    async def test_hello(self, model_id: str, base_url: str, api_key: str) -> dict:
        """向模型发一条 hello，仅供用户参考连通性。"""
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=20)
        started = time.perf_counter()
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "stream": False,
        }
        use_extra = base_url not in self._thinking_extra_rejected
        try:
            try:
                kwargs = dict(payload)
                if use_extra:
                    kwargs["extra_body"] = dict(_THINKING_OFF_EXTRA)
                resp = await client.chat.completions.create(**kwargs)
            except APIStatusError as e:
                if use_extra and e.status_code == 400 and self._looks_like_unknown_param(e):
                    self._reject_thinking_extra(base_url)
                    resp = await client.chat.completions.create(**payload)
                else:
                    raise
            text = ""
            if resp.choices:
                raw = resp.choices[0].message.content or ""
                stripper = _ThinkStripper()
                text = (stripper.feed(raw) + stripper.flush()).strip()
            latency = int((time.perf_counter() - started) * 1000)
            preview = text[:200]
            return {"ok": True, "latency_ms": latency, "preview": preview}
        except RateLimitError as e:
            return {"ok": False, "error": f"速率限制: {e}"}
        except (APITimeoutError, httpx.TimeoutException):
            return {"ok": False, "error": "请求超时，请检查网络或基础 URL"}
        except APIConnectionError as e:
            return {"ok": False, "error": f"网络连接失败: {e}"}
        except APIStatusError as e:
            status = e.status_code
            if status == 401:
                return {"ok": False, "error": "认证失败，请检查 API 密钥"}
            if status == 403:
                return {"ok": False, "error": "权限不足"}
            if status == 404:
                return {"ok": False, "error": "接口或模型不存在，请检查基础 URL 与 model-id"}
            return {"ok": False, "error": f"API 错误 ({status}): {e}"}
        except httpx.NetworkError as e:
            return {"ok": False, "error": f"网络连接失败: {e}"}
        except Exception as e:
            logger.warning(f"模型测试失败: {e}", exc_info=True)
            return {"ok": False, "error": str(e) or "测试失败"}
        finally:
            try:
                await client.close()
            except Exception:
                pass
