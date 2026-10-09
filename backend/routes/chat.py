"""
对话路由 — 基于 SSE 流式的对话接口。

支持单模型与双模型（多角色）两种对话模式。
"""
from __future__ import annotations

import json
import logging
import asyncio
import contextvars
import re
from typing import AsyncGenerator

import anyio
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from attachments import (
    append_model_text,
    build_user_content,
    fetch_pending,
    select_image_ids,
)
from auth import current_user
from clients import GenerationCancelled
from config import CONTEXT_WINDOW_SIZE, CONTEXT_MAX_CHARS, UPLOAD_MAX_TEXT_CHARS
from helpers import resolve_slot, error, get_runtime
from models import ChatRequest
from state import get_ai_client, get_slot_mgr

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])
_slot_locks: dict[tuple[int, int], asyncio.Lock] = {}


class _SlotRun:
    """一次流式回复占用的锁、回滚起点和可关闭的上游流。"""

    def __init__(self, user_id: int, slot_index: int):
        self.user_id = user_id
        self.slot_index = slot_index
        self.cancel_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.lock: asyncio.Lock | None = None
        self.lock_acquired = False
        self.lock_conn = None
        self.locks_released = False
        self.rollback_id = None
        self.keep_partial = False
        self.completed = False
        self.rolled_back = False
        self.inflight: asyncio.Future | None = None
        self._stream = None

    def bind_stream(self, stream) -> None:
        self._stream = stream

    async def close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if not close:
            return
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception as e:
            logger.info(f"关闭上游模型流: {e}")


_slot_runs: dict[tuple[int, int], _SlotRun] = {}
_current_run: contextvars.ContextVar[_SlotRun | None] = contextvars.ContextVar(
    "nehchat_slot_run", default=None,
)


def _active_run() -> _SlotRun | None:
    return _current_run.get()


def _slot_lock(user_id: int, slot_index: int) -> asyncio.Lock:
    key = (user_id, slot_index)
    if key not in _slot_locks:
        _slot_locks[key] = asyncio.Lock()
    return _slot_locks[key]


async def _db_call(method, *args):
    return await asyncio.to_thread(method, *args)


async def _await_cleanup(awaitable):
    """清理阶段不受本次请求取消影响，避免锁和回滚被中途打断。"""
    task = asyncio.current_task()
    uncancel = getattr(task, "uncancel", None) if task is not None else None
    if callable(uncancel):
        while task.cancelling():
            uncancel()
    with anyio.CancelScope(shield=True):
        return await awaitable


async def _tracked_db(run: _SlotRun | None, method, *args):
    """记下尚未结束的写入，取消时也能等到它落库再决定回滚。"""
    fut = asyncio.ensure_future(asyncio.to_thread(method, *args))
    if run is not None:
        run.inflight = fut
    try:
        return await fut
    finally:
        if run is not None and run.inflight is fut and fut.done():
            run.inflight = None


def _append_tracked(run: _SlotRun | None, user_id: int, slot_index: int, messages: list):
    ids = get_slot_mgr().append_messages(user_id, slot_index, messages)
    if run is not None and ids and run.rollback_id is None:
        run.rollback_id = ids[0]
    return ids


async def _drain_inflight(run: _SlotRun | None) -> None:
    if run is None:
        return
    fut = run.inflight
    if fut is not None and not fut.done():
        try:
            await fut
        except Exception as e:
            logger.warning(f"等待未完成的存档写入失败: {e}")
    if run.inflight is fut:
        run.inflight = None


async def _rollback_run(run: _SlotRun | None) -> None:
    """删掉这一轮没写完的消息。模型2 普通失败时保留已完成的模型1。"""
    if run is None:
        return
    await _drain_inflight(run)
    if run.completed or run.keep_partial or run.rolled_back:
        return
    rid = run.rollback_id
    run.rolled_back = True
    if not rid:
        return
    try:
        await asyncio.to_thread(
            get_slot_mgr().delete_messages_from,
            run.user_id,
            run.slot_index,
            rid,
        )
        logger.info(f"已回滚存档 #{run.slot_index + 1} 未完成的消息（起点 #{rid}）")
    except Exception as e:
        run.rolled_back = False
        logger.warning(f"回滚失败: {e}")


async def _release_run(run: _SlotRun | None) -> None:
    if run is None or run.locks_released:
        return
    run.locks_released = True
    conn = run.lock_conn
    run.lock_conn = None
    if conn is not None:
        try:
            await asyncio.to_thread(
                get_slot_mgr().release_slot_lock,
                conn,
                run.user_id,
                run.slot_index,
            )
        except Exception as e:
            logger.warning(f"释放存档锁时出错: {e}")
            try:
                conn.close()
            except Exception:
                pass
    if run.lock_acquired and run.lock is not None:
        run.lock_acquired = False
        try:
            run.lock.release()
        except RuntimeError:
            pass


async def _finish_locked(run: _SlotRun, source, started: bool) -> None:
    if started:
        aclose = getattr(source, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception as e:
                logger.warning(f"关闭对话流时出错: {e}")
    await _rollback_run(run)
    await _release_run(run)


def _stream_kwargs(run: _SlotRun | None) -> dict:
    if run is None:
        return {}
    return {"cancel_event": run.cancel_event, "on_stream": run.bind_stream}


async def _locked_stream(user_id: int, slot_index: int, source):
    """带并发互斥保护与取消支持的 SSE 流式生成器。"""
    key = (user_id, slot_index)
    run = _SlotRun(user_id, slot_index)
    run.task = asyncio.current_task()
    run.lock = _slot_lock(user_id, slot_index)
    _slot_runs[key] = run
    token = _current_run.set(run)
    started = False
    try:
        try:
            await asyncio.wait_for(run.lock.acquire(), timeout=1.0)
            run.lock_acquired = True
        except asyncio.TimeoutError:
            yield _sse({
                "type": "error",
                "code": "slot_busy",
                "content": "该存档正在生成回复，请稍后重试",
            })
            return

        run.lock_conn = await _db_call(get_slot_mgr().acquire_slot_lock, user_id, slot_index)
        if run.lock_conn is None:
            yield _sse({
                "type": "error",
                "code": "slot_busy",
                "content": "该存档正在生成回复，请稍后重试",
            })
            return

        async for event in source:
            started = True
            yield event
    finally:
        try:
            await _await_cleanup(_finish_locked(run, source, started))
        finally:
            _current_run.reset(token)
            if _slot_runs.get(key) is run:
                _slot_runs.pop(key, None)


@router.post("/api/slots/{slot_index}/chat/cancel")
async def cancel_chat(slot_index: int, user: dict = Depends(current_user)):
    """停止这一轮生成，回滚未完成消息并放开存档锁。"""
    uid = user["id"]
    run = _slot_runs.get((uid, slot_index))
    if run is None:
        return {"ok": True, "cancelled": False}

    run.cancel_event.set()
    await _await_cleanup(run.close_stream())
    task = run.task
    if task is not None and not task.done():
        await _await_cleanup(_wait_task(task, 8.0))
    if task is not None and not task.done():
        task.cancel()
        await _await_cleanup(_wait_task(task, 3.0))
    await _await_cleanup(_rollback_run(run))
    await _await_cleanup(_release_run(run))
    logger.info(f"已取消用户 #{uid} 存档 #{slot_index + 1} 的生成任务")
    return {
        "ok": True,
        "cancelled": True,
        "rolled_back": bool(run.rolled_back and run.rollback_id),
    }


async def _wait_task(task: asyncio.Task, timeout: float) -> None:
    try:
        await asyncio.wait([task], timeout=timeout)
    except Exception:
        pass

# ── 固定图标 ──
MODEL1_ICON = "🎭"
MODEL2_ICON = "🌟"

# 继续回复时附加给模型的引导词（仅本次请求使用，不入库）
CONTINUE_PROMPT = "请继续你刚才的回复，接着上一条的内容往下说，不要重复已说过的内容。"

# 双模型「继续」时模拟用户留空，让两个角色正常继续对话的引导词
DUAL_CONTINUE_PROMPT = "（用户没有发送新消息，请两位角色自然地继续刚才的对话，推进当前话题。）"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_THINK_BLOCK = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>|◁think▷.*?◁/think▷",
    re.IGNORECASE | re.DOTALL,
)


def _sse(data: dict) -> str:
    """构造 SSE 数据帧；ensure_ascii=False 避免中文被转义，减小传输体积。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _strip_think(text: str) -> str:
    if not text:
        return ""
    return _THINK_BLOCK.sub("", text).strip()


def _exception_error_event(e: Exception, ref_key: str, ref_value) -> dict:
    """把异常映射为统一的 SSE error 事件（供 /api/chat 与 continue 复用）。"""
    error_code = getattr(e, "error_code", None)
    if error_code:
        return {
            "type": "error",
            "code": error_code,
            "content": str(e),
            ref_key: ref_value,
        }
    if isinstance(e, ConnectionError):
        err = str(e)
        return {"type": "error", "code": "network_error", "content": err, ref_key: ref_value}
    if isinstance(e, ValueError):
        return {'type': 'error', 'code': 'config_error', 'content': str(e), ref_key: ref_value}
    if isinstance(e, RuntimeError) and str(e).startswith("数据库"):
        return {'type': 'error', 'code': 'database_error', 'content': '存档写入失败，请稍后重试', ref_key: ref_value}
    err_str = str(e)
    low = err_str.lower()
    if "401" in err_str or "unauthorized" in low or "invalid_api_key" in low:
        return {'type': 'error', 'code': 'auth_failed',
                'content': 'API 认证失败，请检查 API Key 是否正确', ref_key: ref_value}
    if "429" in err_str or "rate_limit" in low or "too_many_requests" in low:
        return {'type': 'error', 'code': 'rate_limited',
                'content': '请求过于频繁，请稍后重试', ref_key: ref_value}
    if "insufficient_quota" in low or "exceeded" in low:
        return {'type': 'error', 'code': 'quota_exceeded',
                'content': 'API 额度不足，请检查账户余额', ref_key: ref_value}
    return {'type': 'error', 'code': 'unknown',
            'content': f'请求失败: {err_str}', ref_key: ref_value}


def _model_messages(user_id: int, history: list) -> list:
    """把历史收成模型消息。带图片的用户消息会变成图文内容。"""
    image_ids = select_image_ids(history)
    text_left = [UPLOAD_MAX_TEXT_CHARS]
    prepared = []
    for message in history:
        role = message.get("role")
        if role == "user":
            content = build_user_content(
                user_id,
                _strip_think(message.get("content") or ""),
                message.get("attachments"),
                image_ids,
                text_left,
            )
        else:
            content = _strip_think(message.get("content") or "")
        prepared.append({"role": role, "content": content})
    return prepared


async def _to_model_messages(user_id: int, history: list) -> list:
    return await asyncio.to_thread(_model_messages, user_id, history)


def _current_user_content(user_id: int, text: str, attachments) -> object:
    return build_user_content(
        user_id,
        text or "",
        attachments,
        select_image_ids([{"role": "user", "attachments": attachments}]),
        [UPLOAD_MAX_TEXT_CHARS],
    )


def _truncate_history(history: list) -> list:
    """先按条数、再按总字符从尾部截断历史。"""
    items = history[-CONTEXT_WINDOW_SIZE:] if len(history) > CONTEXT_WINDOW_SIZE else list(history)
    if not items:
        return []
    total = 0
    kept: list = []
    for m in reversed(items):
        n = len(m.get("content") or "")
        if kept and total + n > CONTEXT_MAX_CHARS:
            break
        kept.append(m)
        total += n
    kept.reverse()
    return kept


def _attachment_title(attachments) -> str:
    for att in attachments or []:
        name = (att.get("name") or "").strip()
        if name:
            return name
    return ""


def _auto_title(slot_index: int, text: str) -> str:
    """用用户首条消息生成存档标题，空内容时退化为 存档N。"""
    line = (text or "").strip().replace("\n", " ").strip()
    if line:
        return line[:20]
    return f"存档{slot_index + 1}"


async def _stream_dual_turn(
    user_id: int,
    slot_index: int,
    data: dict,
    history: list,
    user_content: str,
    user_attachments: list | None = None,
    persist_user: bool = True,
    inject_user: bool | None = None,
    existing_user_id: int | None = None,
    error_ref_key: str = "user_message_id",
) -> AsyncGenerator[str, None]:
    """双模型（或按 response_mode 单侧）回复一轮，产出 SSE 事件。

    persist_user=True：先把用户消息落库（普通发消息），回滚起点 = 用户消息 ID；
    persist_user=False：不落库用户消息。inject_user 默认 True（继续回复把引导词
    只注入内存）；编辑/重开时 inject_user=False，历史里已有该条用户消息。
    """
    if inject_user is None:
        inject_user = not persist_user
    dual_config = data.get("dual_config", {}) or {}
    model = data.get("model") or ""
    system_prompt = data.get("system_prompt", "使用中文回答")
    params = data.get("params", {}) or {}
    response_mode = dual_config.get("response_mode", "both")
    first_model = dual_config.get("first_model", "model1")
    model1_name = dual_config.get("model1_name", "")
    model2_name = dual_config.get("model2_name", "")

    run_model1 = response_mode in ("model1", "both")
    run_model2 = response_mode in ("model2", "both")

    user_msg_id = existing_user_id
    msg_ids = []
    completed = False
    failed_role = "model1"
    run = _active_run()

    try:
        # 用户消息：仅普通发消息时落库并作为回滚起点
        if persist_user:
            saved_ids = await _tracked_db(
                run, _append_tracked, run, user_id, slot_index,
                [{"role": "user", "content": user_content, "attachments": user_attachments or []}],
            )
            if not saved_ids:
                raise RuntimeError("数据库写入用户消息失败")
            user_msg_id = saved_ids[0] if saved_ids else None
            history.append({
                "role": "user",
                "content": user_content,
                "attachments": user_attachments or [],
            })
        elif inject_user:
            history.append({"role": "user", "content": user_content})
        else:
            user_msg_id = existing_user_id

        if run is not None and run.cancel_event.is_set():
            return

        # 决定模型顺序
        model_order = ["model1", "model2"]
        if first_model == "model2":
            model_order = ["model2", "model1"]

        first_resp = None    # 第一个模型的回复内容
        first_role = None    # 第一个模型的角色名

        for role in model_order:
            if role == "model1" and not run_model1:
                continue
            if role == "model2" and not run_model2:
                continue
            if run is not None and run.cancel_event.is_set():
                return
            failed_role = role

            is_current_first = (first_resp is None)  # 第一个跑的模型

            # 按角色选择配置
            if role == "model1":
                cfg_key = model
                cfg_system = system_prompt
                cfg_params = params
                role_name = model1_name
                role_icon = MODEL1_ICON
            else:
                m2 = dual_config.get("model2") or {}
                cfg_key = m2.get("model") or model
                cfg_system = m2.get("system_prompt", system_prompt)
                cfg_params = m2.get("params", params)
                role_name = model2_name
                role_icon = MODEL2_ICON

            rt = get_runtime(user_id, cfg_key, http=False)
            max_tokens = rt.get("max_tokens")

            # ── 构建消息上下文 ──
            if is_current_first:
                # 第一个模型：正常历史（含本轮用户消息）
                ctx = _truncate_history(history)
                messages = [
                    {"role": "system", "content": cfg_system},
                    *(await _to_model_messages(user_id, ctx)),
                ]
            else:
                # 第二个模型：
                #   history 目前 = [...历史..., {user: 本轮}, {assistant: 第一模型回复}]
                #   排除最后两条，取之前的历史
                prev_src = history[:-2] if len(history) >= 2 else []
                prev = _truncate_history(prev_src)
                current = history[-2] if len(history) >= 2 else {}
                user_part = await asyncio.to_thread(
                    _current_user_content,
                    user_id,
                    user_content,
                    current.get("attachments"),
                )
                messages = [
                    {"role": "system", "content": cfg_system},
                    *(await _to_model_messages(user_id, prev)),
                ]
                # 第一个模型的回答传入
                first_name = model2_name if first_role == "model2" else model1_name
                pass_mode = dual_config.get("pass_mode", "user")  # "user" | "assistant"
                if pass_mode == "assistant":
                    messages.append({"role": "user", "content": user_part})
                    messages.append({"role": "assistant", "content": f"{first_name}: {first_resp}"})
                else:
                    # 合并进上一条 user 消息，避免连续两条 user 消息
                    messages.append({
                        "role": "user",
                        "content": append_model_text(
                            user_part, f"[{first_name} 的回复]\n{first_resp}",
                        ),
                    })

            # 发出 model_start 事件
            yield _sse({
                'type': 'model_start',
                'role': role,
                'name': role_name,
                'icon': role_icon,
            })

            chunks = []
            try:
                async for chunk in get_ai_client().stream_chat(
                    messages,
                    model_id=rt["model_id"],
                    base_url=rt["base_url"],
                    api_key=rt["api_key"],
                    max_tokens=max_tokens,
                    params=cfg_params,
                    **_stream_kwargs(run),
                ):
                    chunks.append(chunk)
                    yield _sse({'type': 'chunk', 'content': chunk, 'role': role})
            except BaseException as e:
                is_cancelled = isinstance(e, (asyncio.CancelledError, GeneratorExit, GenerationCancelled))
                if run is not None and run.cancel_event.is_set():
                    is_cancelled = True
                # 仅在非主动取消的常规异常时，模型2调用失败才保留模型1已完成的回复
                if (
                    not is_cancelled
                    and not is_current_first
                    and run is not None
                    and run.rollback_id is not None
                ):
                    run.keep_partial = True
                raise

            if run is not None and run.cancel_event.is_set():
                return

            full_response = "".join(chunks)

            # 保存到数据库
            saved = await _tracked_db(
                run, _append_tracked, run, user_id, slot_index,
                [{"role": "assistant", "content": full_response, "source": role}],
            )
            if not saved:
                raise RuntimeError("数据库写入模型回复失败")
            msg_ids.extend(saved)

            history.append({"role": "assistant", "content": full_response})

            # 记住第一个模型的回答，供第二个模型拼入上下文
            if is_current_first:
                first_resp = full_response
                first_role = role

            # 发出 model_done 事件
            yield _sse({
                'type': 'model_done',
                'role': role,
                'name': role_name,
                'icon': role_icon,
                'message_id': saved[0] if saved else None,
            })

        # 自动标题：普通发消息时用用户原文，继续回复不改标题
        if persist_user and not data.get("title", ""):
            title_src = user_content or _attachment_title(user_attachments)
            if not await _db_call(
                get_slot_mgr().update_slot_meta,
                user_id, slot_index, {"title": _auto_title(slot_index, title_src)}
            ):
                logger.warning(f"自动更新存档 #{slot_index + 1} 标题失败")

        # 最终 done 事件
        completed = True
        if run is not None:
            run.completed = True
        yield _sse({
            'type': 'done',
            'user_message_id': user_msg_id,
            'message_ids': msg_ids,
            'dual': True,
            'continue_turn': not persist_user,
        })

    except GenerationCancelled:
        return

    except (asyncio.CancelledError, GeneratorExit):
        raise

    except Exception as e:
        ref_value = (
            user_msg_id
            if error_ref_key == "user_message_id"
            else (msg_ids[-1] if msg_ids else None)
        )
        event = _exception_error_event(e, error_ref_key, ref_value)
        event["key_target"] = failed_role
        yield _sse(event)

    finally:
        await _await_cleanup(_rollback_run(run))


@router.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(current_user)):
    """流式对话 — 接收 JSON，返回 SSE 流式响应。"""
    uid = user["id"]
    user_content = (req.message or "").strip()
    turn_attachments = []
    if req.attachment_ids and not req.from_id:
        turn_attachments = await _db_call(fetch_pending, uid, list(req.attachment_ids))
        if turn_attachments is None:
            error("invalid_attachment", "附件不存在或已使用", 400)
    if not user_content and not turn_attachments and not req.from_id:
        error("empty_message", "消息不能为空", 400)

    data = await _db_call(resolve_slot, uid, req.slot_index)

    history: list = data.get("history", [])
    system_prompt: str = data.get("system_prompt", "使用中文回答")
    model: str = data.get("model") or ""
    params: dict = data.get("params", {}) or {}
    dual_config: dict = data.get("dual_config", {}) or {}

    # ── 是否双模型 ──
    dual_enabled = dual_config.get("enabled", False)
    response_mode = dual_config.get("response_mode", "both")  # model1 | model2 | both
    first_model = dual_config.get("first_model", "model1")    # 谁先回复

    # 校验双模型配置，避免非法值静默退化为单模型
    if dual_enabled and response_mode not in ("model1", "model2", "both"):
        error("invalid_response_mode", "回复模式无效", 400)
    if dual_enabled and first_model not in ("model1", "model2"):
        error("invalid_first_model", "先回复模型无效", 400)

    # 判断实际要跑哪些模型
    run_model1 = dual_enabled and response_mode in ("model1", "both")
    run_model2 = dual_enabled and response_mode in ("model2", "both")

    # 双模型时的名字包装
    model1_name = dual_config.get("model1_name", "")
    model2_name = dual_config.get("model2_name", "")

    async def stream():
        completed = False
        persist_user = True
        inject_user = True
        local_history = list(history)
        local_content = user_content
        user_msg_id = None
        msg_ids = []
        snapshot = None

        try:
            # 锁内重新读取，避免删/写之间用到过期历史
            fresh = await _db_call(resolve_slot, uid, req.slot_index)
            local_history = list(fresh.get("history") or [])

            if req.from_id:
                target = next(
                    (m for m in local_history if int(m.get("id") or 0) == int(req.from_id)),
                    None,
                )
                if not target or target.get("role") != "user":
                    yield _sse({
                        "type": "error",
                        "code": "invalid_message_id",
                        "content": "只能从一条用户消息重新生成",
                    })
                    return
                local_content = local_content or (target.get("content") or "").strip()
                if not local_content and not (target.get("attachments") or []):
                    yield _sse({
                        "type": "error",
                        "code": "empty_message",
                        "content": "消息不能为空",
                    })
                    return
                if local_content != (target.get("content") or ""):
                    updated = await _db_call(
                        get_slot_mgr().update_message_content,
                        uid, req.slot_index, req.from_id, local_content,
                    )
                    if not updated:
                        raise RuntimeError("更新用户消息失败")
                snapshot = await _db_call(
                    get_slot_mgr().list_messages_after, uid, req.slot_index, req.from_id,
                )
                if not await _db_call(
                    get_slot_mgr().delete_messages_after, uid, req.slot_index, req.from_id,
                ):
                    raise RuntimeError("删除后续消息失败")
                local_history = [
                    m for m in local_history if int(m.get("id") or 0) <= int(req.from_id)
                ]
                for m in local_history:
                    if int(m.get("id") or 0) == int(req.from_id):
                        m["content"] = local_content
                persist_user = False
                inject_user = False
                user_msg_id = req.from_id

            # ── 单模型模式 ──
            if not dual_enabled or (not run_model1 and not run_model2):
                # 退化为单模型（未开启双模型，或双模型无有效回复模式）
                if persist_user:
                    local_history.append({
                        "role": "user",
                        "content": local_content,
                        "attachments": turn_attachments,
                    })
                actual_model = model
                actual_prompt = system_prompt
                actual_params = params

                rt = get_runtime(uid, actual_model, http=False)
                max_tokens = rt.get("max_tokens")

                context_history = _truncate_history(local_history)
                messages = [
                    {"role": "system", "content": actual_prompt},
                    *(await _to_model_messages(uid, context_history)),
                ]

                run = _active_run()
                chunks = []
                if persist_user:
                    saved_ids = await _tracked_db(
                        run, _append_tracked, run, uid, req.slot_index,
                        [{"role": "user", "content": local_content, "attachments": turn_attachments}],
                    )
                    if not saved_ids:
                        raise RuntimeError("数据库写入用户消息失败")
                    user_msg_id = saved_ids[0] if saved_ids else None
                if run is not None and run.cancel_event.is_set():
                    return

                async for chunk in get_ai_client().stream_chat(
                    messages,
                    model_id=rt["model_id"],
                    base_url=rt["base_url"],
                    api_key=rt["api_key"],
                    max_tokens=max_tokens,
                    params=actual_params,
                    **_stream_kwargs(run),
                ):
                    chunks.append(chunk)
                    yield _sse({'type': 'chunk', 'content': chunk})

                if run is not None and run.cancel_event.is_set():
                    return

                full_response = "".join(chunks)
                msg_ids = await _tracked_db(
                    run, _append_tracked, run, uid, req.slot_index,
                    [{"role": "assistant", "content": full_response, "source": "single"}],
                )
                if not msg_ids:
                    raise RuntimeError("数据库写入模型回复失败")
                local_history.append({"role": "assistant", "content": full_response})

                if persist_user and not fresh.get("title", ""):
                    title_src = local_content or _attachment_title(turn_attachments)
                    if not await _db_call(
                        get_slot_mgr().update_slot_meta,
                        uid, req.slot_index, {"title": _auto_title(req.slot_index, title_src)}
                    ):
                        logger.warning(f"自动更新存档 #{req.slot_index + 1} 标题失败")

                done_event = {
                    'type': 'done',
                    'user_message_id': user_msg_id,
                    'assistant_message_id': msg_ids[0] if msg_ids else None,
                }
                completed = True
                if run is not None:
                    run.completed = True
                yield _sse(done_event)
                return

            # ── 双模型模式（共享助手：自行落库、发事件、处理错误与回滚） ──
            gen = _stream_dual_turn(
                uid, req.slot_index, fresh, local_history, local_content,
                user_attachments=turn_attachments if persist_user else None,
                persist_user=persist_user,
                inject_user=inject_user,
                existing_user_id=user_msg_id,
                error_ref_key="user_message_id",
            )
            dual_done = False
            try:
                async for event in gen:
                    if event.startswith("data: "):
                        try:
                            payload = json.loads(event[6:])
                            if payload.get("type") == "done":
                                dual_done = True
                        except json.JSONDecodeError:
                            pass
                    yield event
            finally:
                await gen.aclose()
            completed = dual_done
            if dual_done:
                dual_run = _active_run()
                if dual_run is not None:
                    dual_run.completed = True

        except GenerationCancelled:
            return

        except (asyncio.CancelledError, GeneratorExit):
            raise

        except ConnectionError as e:
            logger.error(f"AI 服务连接错误: {e}")
            yield _sse(_exception_error_event(e, "user_message_id", user_msg_id))

        except ValueError as e:
            logger.error(f"Config error: {e}")
            yield _sse(_exception_error_event(e, "user_message_id", user_msg_id))

        except Exception as e:
            logger.error(f"Chat error: {e}")
            yield _sse(_exception_error_event(e, "user_message_id", user_msg_id))

        finally:
            await _await_cleanup(_rollback_run(_active_run()))
            if not completed and snapshot and req.from_id:
                async def _restore_snapshot():
                    remaining = await _db_call(
                        get_slot_mgr().list_messages_after,
                        uid, req.slot_index, req.from_id,
                    )
                    if not remaining:
                        restored = await _db_call(
                            get_slot_mgr().restore_messages,
                            uid, req.slot_index, snapshot,
                        )
                        if restored:
                            logger.info(
                                f"重新生成失败，已写回存档 #{req.slot_index + 1} 的 {len(snapshot)} 条消息"
                            )
                        else:
                            logger.warning(
                                f"重新生成失败，写回存档 #{req.slot_index + 1} 消息未成功"
                            )
                try:
                    await _await_cleanup(_restore_snapshot())
                except Exception as e:
                    logger.warning(f"恢复消息失败: {e}")

    return StreamingResponse(
        _locked_stream(uid, req.slot_index, stream()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/api/slots/{slot_index}/chat/continue")
async def continue_chat(slot_index: int, user: dict = Depends(current_user)):
    """继续回复。

    单模型：不新增用户消息，让最后一条 AI 回复继续生成（合并回原消息）。
    双模型：相当于用户留空，两个模型按 response_mode / first_model / pass_mode
    正常回复一轮（各新增一条回复，不落库用户消息）。
    """
    uid = user["id"]
    await _db_call(resolve_slot, uid, slot_index)

    async def stream():
        data = await _db_call(resolve_slot, uid, slot_index)
        history: list = list(data.get("history") or [])
        dual_config = data.get("dual_config", {}) or {}

        if dual_config.get("enabled", False):
            if dual_config.get("response_mode", "both") not in ("model1", "model2", "both"):
                yield _sse({
                    "type": "error",
                    "code": "invalid_response_mode",
                    "content": "回复模式无效",
                })
                return
            if dual_config.get("first_model", "model1") not in ("model1", "model2"):
                yield _sse({
                    "type": "error",
                    "code": "invalid_first_model",
                    "content": "先回复模型无效",
                })
                return
            gen = _stream_dual_turn(
                uid, slot_index, data, history, DUAL_CONTINUE_PROMPT,
                persist_user=False, error_ref_key="message_id",
            )
            try:
                async for event in gen:
                    yield event
            finally:
                await gen.aclose()
            return

        last_msg = None
        for m in reversed(history):
            if m.get("role") == "assistant":
                last_msg = m
                break
        if last_msg is None:
            yield _sse({
                "type": "error",
                "code": "nothing_to_continue",
                "content": "暂无可继续的回复",
            })
            return

        message_id = last_msg.get("id")
        original_content = last_msg.get("content") or ""

        cfg_key = data.get("model", "") or ""
        cfg_system = data.get("system_prompt") or "使用中文回答"
        cfg_params = data.get("params") or {}

        try:
            rt = get_runtime(uid, cfg_key, http=False)
        except Exception as e:
            yield _sse(_exception_error_event(e, "message_id", message_id))
            return
        max_tokens = rt.get("max_tokens")

        context_history = _truncate_history(history)
        messages = [
            {"role": "system", "content": cfg_system},
            *(await _to_model_messages(uid, context_history)),
            {"role": "user", "content": CONTINUE_PROMPT},
        ]

        try:
            yield _sse({
                "type": "continue_start",
                "role": "single",
                "name": "",
                "icon": "🤖",
                "message_id": message_id,
            })

            run = _active_run()
            chunks = []
            async for chunk in get_ai_client().stream_chat(
                messages,
                model_id=rt["model_id"],
                base_url=rt["base_url"],
                api_key=rt["api_key"],
                max_tokens=max_tokens,
                params=cfg_params,
                **_stream_kwargs(run),
            ):
                chunks.append(chunk)
                yield _sse({"type": "chunk", "content": chunk, "role": "single"})

            if run is not None and run.cancel_event.is_set():
                return

            full_response = "".join(chunks)
            if full_response and message_id:
                updated = await _db_call(
                    get_slot_mgr().update_message_content,
                    uid, slot_index, message_id, original_content + full_response,
                )
                if not updated:
                    raise RuntimeError("数据库更新模型回复失败")
                if not await _db_call(get_slot_mgr().touch_slot, uid, slot_index):
                    logger.warning(f"刷新存档 #{slot_index + 1} 时间失败")

            yield _sse({
                "type": "done",
                "message_id": message_id,
                "role": "single",
                "continue": True,
            })

        except GenerationCancelled:
            return

        except (asyncio.CancelledError, GeneratorExit):
            raise

        except Exception as e:
            logger.error(f"Continue error: {e}")
            yield _sse(_exception_error_event(e, "message_id", message_id))

    return StreamingResponse(
        _locked_stream(uid, slot_index, stream()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
