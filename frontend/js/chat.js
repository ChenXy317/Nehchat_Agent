/**
 * 对话逻辑 — 发送消息、接收 SSE 流、重新生成与取消，支持双模型流式对话。
 */

import { state } from "./state.js";
import { apiGet, apiPost, apiDelete, apiPatch } from "./api.js";
import { $, scrollToBottom, escapeHtml } from "./utils.js";
import { showToast } from "./toast.js";
import { showConfirm } from "./confirm.js";
import { renderMarkdown, enhanceCodeBlocks } from "./markdown.js";
import { postSse, streamErrorText } from "./sse.js";
import {
  showSlotView,
  showChatView,
  showEmptyState,
  renderMessages,
  addMessage,
  finishStreaming,
  updateSidebarInfo,
  setStreaming,
  addErrorMessage,
  loadSlots,
} from "./ui.js";
import { hasPending, restorePending, takePending, uploadAttachment } from "./attachments.js";

let currentCancelPromise = null;
let lastCancelRolledBack = false;

/** 给消息元素挂载 ↻ 重新生成按钮（记录所属用户消息 ID） */
function attachRegenBtn(msgDiv, userMsgId) {
  if (!msgDiv || !userMsgId) return;
  let regenBtn = msgDiv.querySelector(".regenerate-btn");
  if (!regenBtn) {
    regenBtn = document.createElement("button");
    regenBtn.className = "regenerate-btn";
    regenBtn.textContent = "↻";
    regenBtn.title = "重新生成";
    msgDiv.appendChild(regenBtn);
  }
  regenBtn.dataset.userMsgId = userMsgId;
}

/** 从 .bubble 中获取文本内容容器 */
function getContent(bubble) {
  if (!bubble) return null;
  // 如果有 label bar，内容在 .bubble-content 中
  return bubble.querySelector(".bubble-content") || bubble;
}

/** 获取 .bubble 所在的 .message */
function getMsgDiv(bubble) {
  return bubble ? bubble.closest(".message") : null;
}

// ── 打开存档 ──

export async function openSlot(index) {
  state.currentSlotIndex = index;

  try {
    const data = await apiGet(`/api/slots/${index}/chat`);
    state.currentSlotData = data;
    state.dualEnabled = data.dual_enabled || false;
    state.responseMode = data.response_mode || "both";
    state.firstModel = data.first_model || "model1";
    showChatView();
    renderMessages(data.history || []);
    showToast("已进入存档 #" + (index + 1), "success");
  } catch (e) {
    showToast("加载存档失败: " + e.message, "error");
  }
}

// ── 删除存档 ──

export async function deleteSlotAction(index) {
  const confirmed = await showConfirm(
    `确定要删除存档 #${index + 1} 吗？所有对话将永久丢失。`,
    true
  );
  if (!confirmed) return;

  try {
    await apiDelete(`/api/slots/${index}`);
    if (state.currentSlotIndex === index) {
      backToSlots();
    } else {
      loadSlots();
    }
    showToast("存档已删除", "success");
  } catch (e) {
    showToast("删除失败: " + e.message, "error");
  }
}

// ── 返回存档视图 ──

export function backToSlots() {
  if (state.streaming) {
    cancelStream();
  }
  state.currentSlotIndex = null;
  state.currentSlotData = null;
  state.dualEnabled = false;
  document.getElementById("chat-messages").innerHTML = "";
  closeSidebar();
  showSlotView();
}

// ── 清空对话 ──

export async function clearSlotChat() {
  if (state.streaming) {
    showToast("请等待当前回复完成", "warning");
    return;
  }

  const idx = state.currentSlotIndex;
  if (idx === null) return;

  const container = document.getElementById("chat-messages");
  const empty = !container.querySelector(".message");
  if (empty) return;

  const confirmed = await showConfirm("确定要清空当前存档的所有对话吗？");
  if (!confirmed) return;

  try {
    await apiPost(`/api/slots/${idx}/chat/clear`, {});
    const data = await apiGet(`/api/slots/${idx}/chat`);
    state.currentSlotData = data;
    container.innerHTML = "";
    showEmptyState();
    showToast("对话已清空", "success");
  } catch (e) {
    showToast("清空失败: " + e.message, "error");
  }
}

// ── 发送消息（SSE 流式，JSON 请求） ──

export async function sendMessage(opts = {}) {
  if (opts instanceof Event) opts = {};
  const input = document.getElementById("message-input");
  const fromId = Number.isInteger(opts.fromId) ? opts.fromId : null;
  const text = (opts.text ?? input.value).trim();
  const fromHasAttachments = !!opts.userDiv?.querySelector(".bubble-attachment");
  const staged = fromId ? [] : (hasPending() ? takePending() : []);

  if (state.streaming || state.currentSlotIndex === null) {
    if (staged.length) restorePending(staged);
    return;
  }
  if (!text && staged.length === 0 && !(fromId && fromHasAttachments)) return;

  // 清理上一轮遗留的错误提示
  document.querySelectorAll("#chat-messages .message.error").forEach((el) => el.remove());

  let userMsgDiv = opts.userDiv || null;
  if (!fromId) {
    input.value = "";
    input.style.height = "auto";
    const previews = staged.map((item) => ({
      name: item.name,
      kind: item.kind,
      url: item.previewUrl,
    }));
    const userMsgBubble = addMessage("user", text, false, null, null, previews);
    userMsgDiv = userMsgBubble ? userMsgBubble.closest(".message") : null;
  } else if (userMsgDiv) {
    const bubble = userMsgDiv.querySelector(".bubble");
    if (bubble) bubble.dataset.rawContent = text;
    const contentDiv = bubble ? getContent(bubble) : null;
    if (contentDiv) contentDiv.textContent = text;
    userMsgDiv.dataset.messageId = String(fromId);
  }

  setStreaming(true);
  state.abortController = new AbortController();
  state.streamCancelled = false;
  state.currentReader = null;

  let currentBubble = null;
  let bubbles = []; // [{el, role, fullContent, msgId, label}]
  let gotDone = false;      // 是否收到 done 事件
  let errorHandled = false; // 是否已显示错误提示
  let aborted = false;      // 本轮是否被取消/超时中断
  let userMessageId = null;
  let streamError = null;
  let streamRetry = null;

  let uploadedIds = [];
  try {
    if (staged.length) {
      try {
        for (const item of staged) {
          const meta = await uploadAttachment(item.file);
          uploadedIds.push(meta.id);
          if (item.kind === "image" && userMsgDiv && meta.url) {
            const img = [...userMsgDiv.querySelectorAll(".bubble-attachment-image")]
              .find((node) => node.src === item.previewUrl);
            if (img) img.src = meta.url;
          }
        }
      } catch (uploadError) {
        restorePending(staged);
        if (userMsgDiv) userMsgDiv.remove();
        fillMessageInput(text);
        showToast(uploadError.message || "上传失败", "error");
        setStreaming(false);
        return;
      }
    }
    const payload = {
      slot_index: state.currentSlotIndex,
      message: text,
    };
    if (uploadedIds.length) payload.attachment_ids = uploadedIds;
    if (fromId) payload.from_id = fromId;
    await postSse("/api/chat", payload, (event) => {
      const { type, content, code } = event;
      switch (type) {
        case "model_start": {
          const name = event.name || "";
          const icon = event.icon || "🤖";
          currentBubble = addMessage("assistant", "", true, null,
            icon ? `${icon} ${name}`.trim() : name);
          bubbles.push({
            el: currentBubble,
            role: event.role,
            fullContent: "",
            msgId: null,
            label: `${icon} ${name}`.trim(),
          });
          scrollToBottom();
          break;
        }
        case "chunk": {
          let entry = currentBubble ? bubbles[bubbles.length - 1] : null;
          if (!entry) {
            const bubble = addMessage("assistant", "", true);
            entry = { el: bubble, role: null, fullContent: "", msgId: null, label: null };
            bubbles.push(entry);
            currentBubble = bubble;
          }
          entry.fullContent += content || "";
          const contentDiv = getContent(entry.el);
          if (contentDiv) contentDiv.textContent += content || "";
          scrollToBottom();
          break;
        }
        case "model_done": {
          const entry = bubbles.find(b => b.role === event.role || b.el === currentBubble);
          if (entry) {
            entry.msgId = event.message_id;
            if (event.message_id) {
              const msgDiv = getMsgDiv(entry.el);
              if (msgDiv) msgDiv.dataset.messageId = event.message_id;
            }
            const contentDiv = getContent(entry.el);
            if (contentDiv) {
              entry.el.dataset.rawContent = entry.fullContent;
              contentDiv.innerHTML = renderMarkdown(entry.fullContent);
              enhanceCodeBlocks(contentDiv);
            }
            finishStreaming(entry.el);
          }
          currentBubble = null;
          break;
        }
        case "done": {
          gotDone = true;
          if (event.dual) {
            userMessageId = event.user_message_id;
            if (userMsgDiv && userMessageId) userMsgDiv.dataset.messageId = userMessageId;
            if (event.message_ids) {
              bubbles.forEach((b, i) => {
                if (event.message_ids[i]) {
                  const d = getMsgDiv(b.el);
                  if (d) d.dataset.messageId = event.message_ids[i];
                }
              });
            }
            if (userMessageId) {
              bubbles.forEach((b) => {
                attachRegenBtn(getMsgDiv(b.el), userMessageId);
              });
            }
          } else {
            userMessageId = event.user_message_id;
            const assistantMessageId = event.assistant_message_id;
            if (userMsgDiv && userMessageId) userMsgDiv.dataset.messageId = userMessageId;
            const entry = bubbles[bubbles.length - 1];
            if (entry && entry.el) {
              const msgDiv = getMsgDiv(entry.el);
              if (msgDiv && assistantMessageId) msgDiv.dataset.messageId = assistantMessageId;
              const contentDiv = getContent(entry.el);
              if (contentDiv) {
                entry.el.dataset.rawContent = entry.fullContent;
                contentDiv.innerHTML = renderMarkdown(entry.fullContent);
                enhanceCodeBlocks(contentDiv);
              }
              finishStreaming(entry.el);
              if (userMsgDiv && userMessageId) {
                attachRegenBtn(msgDiv, userMessageId);
              }
            }
          }
          loadSlots();
          break;
        }
        case "error": {
          errorHandled = true;
          const completedBubbles = bubbles.filter(b => b.msgId);
          const hasCompleted = completedBubbles.length > 0 &&
            code !== "database_error";

          if (hasCompleted) {
            bubbles.forEach(b => {
              if (!b.msgId) {
                const d = getMsgDiv(b.el);
                if (d) d.remove();
              }
            });
            bubbles = bubbles.filter(b => b.msgId);
            if (currentBubble) {
              const d = getMsgDiv(currentBubble);
              if (d) d.remove();
              currentBubble = null;
            }
            if (userMsgDiv && event.user_message_id) {
              userMsgDiv.dataset.messageId = event.user_message_id;
            }
          } else {
            bubbles.forEach(b => {
              if (b.el) {
                const d = getMsgDiv(b.el);
                if (d) d.remove();
              }
            });
            bubbles = [];
            if (currentBubble) {
              const d = getMsgDiv(currentBubble);
              if (d) d.remove();
              currentBubble = null;
            }
            if (userMsgDiv && !fromId) userMsgDiv.remove();
            if (!fromId && staged.length) restorePending(staged);
          }
          streamError = streamErrorText(code, content);
          streamRetry = (hasCompleted || fromId) ? null : text;
          break;
        }
      }
    });
  } catch (e) {
    if (state.streamCancelled || e.name === "AbortError") {
      // 取消/超时：统一在 finally 中回滚本轮气泡并提示
      aborted = true;
    } else {
      errorHandled = true;
      bubbles.forEach(b => {
        if (b.el) {
          const d = getMsgDiv(b.el);
          if (d) d.remove();
        }
      });
      bubbles = [];
      if (currentBubble) {
        const d = getMsgDiv(currentBubble);
        if (d) d.remove();
        currentBubble = null;
      }
      if (userMsgDiv && !fromId) userMsgDiv.remove();
      if (!fromId && staged.length) restorePending(staged);
      streamError = `请求失败: ${e.message}`;
      streamRetry = fromId ? null : text;
    }
  } finally {
    if (state.streamCancelled) {
      rollbackMessages(text, { keepUser: !!fromId, userDiv: userMsgDiv, bubbles, restoreInput: false });
      showToast("已取消", "info");
    } else if (aborted) {
      rollbackMessages(text, { keepUser: !!fromId, userDiv: userMsgDiv, bubbles, restoreInput: false });
    }
  }

  await refreshSlotAfterStream({
    gotDone, errorHandled, aborted,
    errorText: streamError, retryText: streamRetry,
    reloadHistory: !!fromId,
    restoreText: (!fromId && (state.streamCancelled || aborted)) ? text : null,
    stagedFiles: staged,
  });
  if (gotDone) {
    for (const item of staged) {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    }
  }
  document.getElementById("message-input")?.focus();
}

function fillMessageInput(text) {
  const msgInput = document.getElementById("message-input");
  if (!msgInput) return;
  msgInput.value = text || "";
  msgInput.style.height = "auto";
  if (text) {
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + "px";
  }
  msgInput.focus();
  try {
    const len = (text || "").length;
    msgInput.setSelectionRange(len, len);
  } catch (_) { /* 忽略不支持选择范围的场景 */ }
}

async function refreshSlotAfterStream({
  gotDone, errorHandled, aborted, errorText = null, retryText = null, reloadHistory = false,
  restoreText = null, stagedFiles = null,
}) {
  const isCancelled = state.streamCancelled || aborted;

  if (isCancelled && currentCancelPromise) {
    try {
      await currentCancelPromise;
    } catch (_) { /* 忽略 */ }
    currentCancelPromise = null;
  }

  if (state.currentSlotIndex !== null) {
    try {
      state.currentSlotData = await apiGet(`/api/slots/${state.currentSlotIndex}/chat`);
      state.dualEnabled = state.currentSlotData.dual_enabled || false;
      state.responseMode = state.currentSlotData.response_mode || "both";
      state.firstModel = state.currentSlotData.first_model || "model1";

      if (isCancelled && !gotDone) {
        const hist = state.currentSlotData.history || [];
        const last = hist[hist.length - 1];
        const pendingUser = typeof restoreText === "string"
          && restoreText.trim()
          && last
          && last.role === "user"
          && (last.content || "").trim() === restoreText.trim();
        // 这一轮已完整落库时保留历史；未完成则把原文放回输入框
        const putBack = typeof restoreText === "string" && (lastCancelRolledBack || !pendingUser);
        if (putBack && pendingUser) hist.pop();
        state.currentSlotData.history = hist;
        renderMessages(hist);
        if (putBack) {
          fillMessageInput(restoreText);
          if (stagedFiles?.length) restorePending(stagedFiles);
        } else if (typeof restoreText === "string") {
          fillMessageInput("");
        }
      } else if (!gotDone && (reloadHistory || !errorHandled)) {
        renderMessages(state.currentSlotData.history || []);
      }
      if (!gotDone && errorText && !isCancelled) {
        addErrorMessage(errorText, retryText);
      }
      updateSidebarInfo();
    } catch (_) { /* 静默失败 */ }
  } else if (typeof restoreText === "string") {
    fillMessageInput(restoreText);
  }
  setStreaming(false);
  state.streamCancelled = false;
  state.currentReader = null;
}

function rollbackMessages(text, { keepUser = false, userDiv = null, bubbles = [], restoreInput = true } = {}) {
  if (Array.isArray(bubbles)) {
    bubbles.forEach(b => {
      if (b && b.el) {
        const d = getMsgDiv(b.el);
        if (d) d.remove();
      }
    });
  }

  if (!keepUser && userDiv) {
    userDiv.remove();
  }

  const allMsgs = document.querySelectorAll("#chat-messages > .message");
  for (let i = allMsgs.length - 1; i >= Math.max(0, allMsgs.length - 5); i--) {
    const m = allMsgs[i];
    if (m && m.classList.contains("assistant")) {
      m.remove();
      continue;
    }
    if (m && m.classList.contains("user")) {
      if (!keepUser) m.remove();
      break;
    }
  }

  if (!document.querySelector("#chat-messages .message")) {
    document.getElementById("chat-messages").innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">✦</div>
        <div class="empty-title">开始新的对话</div>
        <div class="empty-desc">在下方输入消息，与 AI 开始交流</div>
      </div>`;
  }

  if (!keepUser && state.currentSlotData && Array.isArray(state.currentSlotData.history)) {
    const hist = state.currentSlotData.history;
    if (hist.length > 0 && hist[hist.length - 1].role === "user") {
      hist.pop();
    }
  }

  if (keepUser || !restoreInput) return;

  const msgInput = document.getElementById("message-input");
  if (msgInput) {
    msgInput.value = text || "";
    msgInput.style.height = "auto";
    if (text) {
      msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + "px";
    }
    msgInput.focus();
    try {
      const len = (text || "").length;
      msgInput.setSelectionRange(len, len);
    } catch (_) { /* 忽略不支持选择范围的场景 */ }
  }
}

// ── 继续回复（不输入消息，让 AI 继续对话） ──

export async function continueLastReply() {
  if (state.streaming) return;
  if (state.currentSlotIndex === null) return;

  // 双模型：相当于用户留空，两个模型按配置正常回复一轮（各新建气泡）
  if (state.dualEnabled) {
    await continueDualTurn();
    return;
  }

  // ── 单模型：延续最后一条回复（合并式） ──
  const msgs = document.querySelectorAll("#chat-messages > .message.assistant");
  const lastMsgDiv = msgs[msgs.length - 1];
  if (!lastMsgDiv) {
    showToast("暂无可继续的回复", "warning");
    return;
  }
  const bubble = lastMsgDiv.querySelector(".bubble");
  const contentDiv = bubble ? getContent(bubble) : null;
  if (!contentDiv) {
    showToast("无法定位上一条回复内容", "error");
    return;
  }
  const messageId = parseInt(lastMsgDiv.dataset.messageId, 10) || null;
  if (!messageId) {
    showToast("无法定位消息 ID，请刷新对话后重试", "error");
    return;
  }

  const originalText = bubble.dataset.rawContent ?? contentDiv.textContent ?? "";
  let fullContent = originalText;
  let gotDone = false;
  let errorHandled = false;
  let aborted = false;
  let streamError = null;

  setStreaming(true);
  state.abortController = new AbortController();
  state.streamCancelled = false;
  state.currentReader = null;
  if (bubble) bubble.classList.add("streaming");

  try {
    await postSse(`/api/slots/${state.currentSlotIndex}/chat/continue`, {}, (event) => {
      switch (event.type) {
        case "chunk": {
          fullContent += event.content || "";
          contentDiv.textContent = fullContent;
          scrollToBottom();
          break;
        }
        case "done": {
          gotDone = true;
          bubble.dataset.rawContent = fullContent;
          contentDiv.innerHTML = renderMarkdown(fullContent);
          enhanceCodeBlocks(contentDiv);
          break;
        }
        case "error": {
          errorHandled = true;
          contentDiv.innerHTML = renderMarkdown(originalText);
          enhanceCodeBlocks(contentDiv);
          streamError = streamErrorText(event.code, event.content);
          break;
        }
      }
    });
  } catch (e) {
    if (state.streamCancelled || e.name === "AbortError") {
      aborted = true;
    } else {
      errorHandled = true;
      contentDiv.innerHTML = renderMarkdown(originalText);
      enhanceCodeBlocks(contentDiv);
      streamError = `请求失败: ${e.message}`;
    }
  } finally {
    if (state.streamCancelled) {
      contentDiv.innerHTML = renderMarkdown(originalText);
      enhanceCodeBlocks(contentDiv);
      showToast("已取消", "info");
    } else if (aborted) {
      contentDiv.innerHTML = renderMarkdown(originalText);
      enhanceCodeBlocks(contentDiv);
    }
    finishStreaming(bubble);
  }

  await refreshSlotAfterStream({
    gotDone, errorHandled, aborted, errorText: streamError,
  });
}

/** 双模型「继续」：跳过用户消息，两个模型按 response_mode / first_model 正常回复一轮 */
async function continueDualTurn() {
  let bubbles = [];
  let currentBubble = null;
  let gotDone = false;
  let errorHandled = false;
  let aborted = false;
  let streamError = null;

  const removeNewBubbles = () => {
    bubbles.forEach((b) => {
      const d = getMsgDiv(b.el);
      if (d) d.remove();
    });
    bubbles = [];
    if (currentBubble) {
      const d = getMsgDiv(currentBubble);
      if (d) d.remove();
      currentBubble = null;
    }
  };

  setStreaming(true);
  state.abortController = new AbortController();
  state.streamCancelled = false;
  state.currentReader = null;

  try {
    await postSse(`/api/slots/${state.currentSlotIndex}/chat/continue`, {}, (event) => {
      switch (event.type) {
        case "model_start": {
          const icon = event.icon || "🤖";
          const name = event.name || "";
          const label = `${icon} ${name}`.trim();
          currentBubble = addMessage("assistant", "", true, null, label);
          bubbles.push({ el: currentBubble, role: event.role, fullContent: "", msgId: null });
          scrollToBottom();
          break;
        }
        case "chunk": {
          const entry = currentBubble ? bubbles[bubbles.length - 1] : null;
          if (!entry) break;
          entry.fullContent += event.content || "";
          const contentDiv = getContent(entry.el);
          if (contentDiv) contentDiv.textContent = entry.fullContent;
          scrollToBottom();
          break;
        }
        case "model_done": {
          const entry = bubbles.find((b) => b.role === event.role || b.el === currentBubble);
          if (entry) {
            entry.msgId = event.message_id;
            if (event.message_id) {
              const msgDiv = getMsgDiv(entry.el);
              if (msgDiv) msgDiv.dataset.messageId = event.message_id;
            }
            const contentDiv = getContent(entry.el);
            if (contentDiv) {
              entry.el.dataset.rawContent = entry.fullContent;
              contentDiv.innerHTML = renderMarkdown(entry.fullContent);
              enhanceCodeBlocks(contentDiv);
            }
            finishStreaming(entry.el);
          }
          currentBubble = null;
          break;
        }
        case "done": {
          gotDone = true;
          if (event.message_ids) {
            bubbles.forEach((b, i) => {
              if (event.message_ids[i]) {
                const d = getMsgDiv(b.el);
                if (d) d.dataset.messageId = event.message_ids[i];
              }
            });
          }
          break;
        }
        case "error": {
          errorHandled = true;
          bubbles.forEach((b) => {
            if (!b.msgId) {
              const d = getMsgDiv(b.el);
              if (d) d.remove();
            }
          });
          bubbles = bubbles.filter((b) => b.msgId);
          if (currentBubble) {
            const d = getMsgDiv(currentBubble);
            if (d) d.remove();
            currentBubble = null;
          }
          streamError = streamErrorText(event.code, event.content);
          break;
        }
      }
    });
  } catch (e) {
    if (state.streamCancelled || e.name === "AbortError") {
      aborted = true;
    } else {
      errorHandled = true;
      removeNewBubbles();
      streamError = `请求失败: ${e.message}`;
    }
  } finally {
    if (state.streamCancelled || aborted) {
      removeNewBubbles();
      if (state.streamCancelled) showToast("已取消", "info");
    }
  }

  await refreshSlotAfterStream({
    gotDone, errorHandled, aborted, errorText: streamError,
  });
}

// ── 重新生成（单/双模型通用） ──

export async function regenerate(userMsgId) {
  if (state.streaming) return;

  const regenBtn = document.querySelector(`.regenerate-btn[data-user-msg-id="${userMsgId}"]`);
  if (!regenBtn) return;
  const assistantDiv = regenBtn.closest(".message");

  let userDiv = assistantDiv ? assistantDiv.previousElementSibling : null;
  while (userDiv && !userDiv.classList.contains("user")) {
    userDiv = userDiv.previousElementSibling;
  }
  if (!userDiv) return;

  const userBubble = userDiv.querySelector(".bubble");
  if (!userBubble) {
    showToast("无法找到用户消息内容", "error");
    return;
  }
  const userText = userBubble.dataset.rawContent
    || userBubble.querySelector(".bubble-content")?.textContent
    || "";

  let current = userDiv.nextElementSibling;
  while (current) {
    const next = current.nextElementSibling;
    current.remove();
    current = next;
  }

  sendMessage({ text: userText, fromId: userMsgId, userDiv });
}

// ── 取消流式回复 ──

export function cancelStream() {
  if (!state.streaming || state.streamCancelled) return;
  state.streamCancelled = true;
  lastCancelRolledBack = false;
  const currentSlot = state.currentSlotIndex;
  // 先等服务端停掉模型并回滚，再断开本地读取。先断开的话，清理会被取消打断，存档锁会卡住。
  currentCancelPromise = (async () => {
    try {
      if (currentSlot !== null) {
        const resp = await apiPost(`/api/slots/${currentSlot}/chat/cancel`, {});
        lastCancelRolledBack = !!resp?.rolled_back;
      }
    } catch (_) { /* 忽略 */ }
    if (state.currentReader) {
      try { await state.currentReader.cancel(); } catch (_) { /* 忽略 */ }
      state.currentReader = null;
    }
    if (state.abortController) {
      try { state.abortController.abort(); } catch (_) { /* 忽略 */ }
      state.abortController = null;
    }
  })();
}

// ── 编辑用户消息 ──

export function editAndResend(msgElement) {
  if (state.streaming) {
    showToast("请等待当前回复完成再编辑", "warning");
    return;
  }

  const bubble = msgElement.querySelector(".bubble");
  if (!bubble) return;

  const originalText = bubble.dataset.rawContent
    || bubble.querySelector(".bubble-content")?.textContent
    || "";
  const hasAttachments = !!msgElement.querySelector(".bubble-attachment");
  if (!originalText && !hasAttachments) return;

  const textarea = document.createElement("textarea");
  textarea.className = "edit-textarea";
  textarea.value = originalText;

  const actions = document.createElement("div");
  actions.className = "edit-actions";

  const saveBtn = document.createElement("button");
  saveBtn.className = "edit-save-btn";
  saveBtn.textContent = "保存";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "edit-cancel-btn";
  cancelBtn.textContent = "取消";

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);

  const editBtn = msgElement.querySelector(".user-edit-btn");
  if (editBtn) editBtn.style.visibility = "hidden";

  const contentDiv = bubble.querySelector(".bubble-content") || bubble;
  contentDiv.innerHTML = "";
  contentDiv.appendChild(textarea);
  contentDiv.appendChild(actions);
  bubble.classList.add("editing");

  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  textarea.addEventListener("input", () => {
    textarea.style.height = "auto";
    textarea.style.height = textarea.scrollHeight + "px";
  });
  textarea.style.height = textarea.scrollHeight + "px";

  saveBtn.onclick = async () => {
    const newText = textarea.value.trim();
    if (!newText && !hasAttachments) {
      showToast("内容不能为空", "warning");
      return;
    }
    if (newText === originalText) {
      cancelEdit(contentDiv, originalText, editBtn, bubble);
      return;
    }

    const messageId = parseInt(msgElement.dataset.messageId, 10);
    if (!messageId) {
      showToast("无法定位消息 ID", "error");
      cancelEdit(contentDiv, originalText, editBtn, bubble);
      return;
    }

    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    bubble.classList.remove("editing");
    contentDiv.textContent = newText;
    bubble.dataset.rawContent = newText;
    if (editBtn) editBtn.style.visibility = "";

    let current = msgElement.nextElementSibling;
    while (current) {
      const next = current.nextElementSibling;
      current.remove();
      current = next;
    }

    sendMessage({ text: newText, fromId: messageId, userDiv: msgElement });
  };

  cancelBtn.onclick = () => {
    cancelEdit(contentDiv, originalText, editBtn, bubble);
  };
}

function cancelEdit(contentDiv, originalText, editBtn, bubble) {
  bubble.classList.remove("editing");
  contentDiv.textContent = originalText;
  if (editBtn) editBtn.style.visibility = "";
}

// ── 切换双模型回复模式 ──

export async function setDualResponseMode(mode, firstModel) {
  if (state.streaming) {
    showToast("请等待当前回复完成", "warning");
    return;
  }
  const idx = state.currentSlotIndex;
  if (idx === null) return;

  try {
    const resp = await apiPatch(`/api/slots/${idx}/dual-toggle`, {
      response_mode: mode,
      first_model: firstModel || state.firstModel || "model1",
    });
    state.responseMode = resp.dual_config?.response_mode || mode;
    state.firstModel = resp.dual_config?.first_model || firstModel || "model1";
    // 同步本地存档数据
    if (state.currentSlotData) {
      state.currentSlotData.dual_config = resp.dual_config || state.currentSlotData.dual_config;
    }
    updateSidebarInfo();
    showToast(
      mode === "both" ? "已设为同时回复" :
      mode === "model1" ? "已设为仅模型1回复" : "已设为仅模型2回复",
      "success"
    );
  } catch (e) {
    showToast("切换失败: " + e.message, "error");
  }
}

// ── 辅助：关闭侧边栏 ──

function closeSidebar() {
  const sidebar = document.getElementById("sidebar");
  const overlay = document.getElementById("sidebar-overlay");
  if (sidebar) sidebar.classList.remove("open");
  if (overlay) overlay.classList.remove("active");
}
