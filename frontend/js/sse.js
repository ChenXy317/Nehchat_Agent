/**
 * SSE 流读取 — 对话发送 / 继续回复共用。
 */

import { state } from "./state.js";
import { showToast } from "./toast.js";

export const STREAM_ERROR_MESSAGES = {
  auth_failed: "🔑 API 认证失败，请到右上角「模型配置」检查该供应商的密钥",
  missing_api_key: "🔑 密钥未配置，请到「模型配置」填写密钥或环境变量",
  unknown_model: "⚙️ 模型已不存在，请更换模型或在「模型配置」中重新添加",
  rate_limited: "⏳ 请求过于频繁，请稍后重试",
  quota_exceeded: "💰 API 额度不足，请检查账户余额",
  ollama_unreachable: "🔌 无法连接到 Ollama 服务，请确认已启动",
  config_error: "⚙️ 模型配置错误",
  database_error: "💾 存档写入失败，请稍后重试",
  network_error: "🔌 网络连接失败，请检查网络连接",
  timeout: "⏱️ 请求超时，请稍后重试",
  service_unavailable: "🛠️ 模型服务暂时不可用",
  permission_denied: "⛔ API 权限不足",
  slot_busy: "⏳ 该存档正在生成回复，请稍后重试",
  nothing_to_continue: "✉️ 暂无可继续的回复",
  too_many_attempts: "⏳ 尝试次数过多，请稍后再试",
  empty_message: "✉️ 消息不能为空",
  not_found: "❓ 接口或模型不存在，请检查基础 URL 与 model-id",
};

export function streamErrorText(code, content) {
  return STREAM_ERROR_MESSAGES[code] || `⚠️ ${content || "未知错误"}`;
}

export async function readSse(response, onEvent, idleMs = 60_000) {
  if (!response.ok) {
    let errMsg = `请求失败: ${response.status}`;
    try {
      const err = await response.json();
      errMsg = err.message || errMsg;
    } catch (_) { /* 忽略 */ }
    const e = new Error(errMsg);
    e.status = response.status;
    throw e;
  }

  const reader = response.body.getReader();
  state.currentReader = reader;
  const decoder = new TextDecoder();
  let buffer = "";
  let lastChunkTime = Date.now();
  const idleCheck = setInterval(() => {
    if (Date.now() - lastChunkTime > idleMs) {
      state.abortController?.abort();
      showToast("响应超时：长时间未收到数据", "error");
      clearInterval(idleCheck);
    }
  }, 10_000);

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      lastChunkTime = Date.now();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        // 停止后继续读完响应，避免服务端清理被未读完的流堵住；事件本身不再分发
        if (state.streamCancelled) continue;
        try {
          onEvent(JSON.parse(line.slice(6)));
        } catch (_) { /* 忽略解析错误 */ }
      }
    }
  } finally {
    clearInterval(idleCheck);
  }
}

export async function postSse(path, body, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal: state.abortController.signal,
  });
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("app-unauthorized"));
  }
  await readSse(response, onEvent);
}
