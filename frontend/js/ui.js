/**
 * UI 渲染函数 — 视图切换、存档网格、消息渲染、
 * 侧边栏更新与流式状态指示。
 */

import { state } from "./state.js";
import { apiGet, apiPatch } from "./api.js";
import { $, scrollToBottom, setStreamingFlag, escapeHtml } from "./utils.js";
import { showToast } from "./toast.js";
import { renderMarkdown, enhanceCodeBlocks } from "./markdown.js";
import { openCreateModal } from "./modals.js";
import { deleteSlotAction, openSlot, editAndResend, setDualResponseMode } from "./chat.js";

// ── DOM 引用 ──

function el(id) {
  return document.getElementById(id);
}

/** 将存档中的模型 key 解析为目录里的显示名称；找不到时回退为 model-id。 */
function displayModel(key) {
  if (!key) return "";
  const found = (state.models || []).find((m) => m.key === key);
  if (found) {
    return found.display_name || found.id || "";
  }
  const idx = key.indexOf(":");
  return idx > 0 ? key.slice(idx + 1) : key;
}

// ── 视图切换 ──

export function showSlotView() {
  state.view = "slots";
  el("slot-view").classList.remove("hidden");
  el("chat-view").classList.add("hidden");
  el("sidebar").style.display = "none";
  loadSlots();
}

export function showChatView() {
  state.view = "chat";
  el("slot-view").classList.add("hidden");
  el("chat-view").classList.remove("hidden");
  el("sidebar").style.display = "flex";
  updateSidebarInfo();
}

// ── 存档网格 ──

export async function loadSlots() {
  try {
    state.slots = await apiGet("/api/slots");
  } catch (e) {
    state.slots = new Array(10).fill(null);
    showToast("加载存档失败: " + e.message, "error");
  }
  renderSlotGrid();
}

export function renderSlotGrid() {
  const grid = el("slot-grid");
  if (!grid) return;
  grid.innerHTML = "";

  const total = state.slots.length || 10;

  for (let i = 0; i < total; i++) {
    const info = state.slots[i];
    const card = document.createElement("div");
    card.className = "slot-card";

    if (info === null || info === undefined) {
      card.classList.add("empty");
      card.innerHTML = `
        <div class="slot-plus">+</div>
        <div class="slot-empty-label">空存档</div>
        <div class="slot-index-label">存档 #${i + 1}</div>
      `;
      card.addEventListener("click", () => openCreateModal(i));
    } else {
      card.classList.add("used");
      const displayTitle = info.title || `存档 #${i + 1}`;
      const isDual = info.dual_enabled;

      const fmtTime = (iso) => {
        if (!iso) return "";
        try {
          return new Date(iso).toLocaleString("zh-CN", {
            month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
          });
        } catch (_) { return ""; }
      };
      const created = fmtTime(info.created_at);
      const updated = fmtTime(info.updated_at);

      card.innerHTML = `
        <button class="slot-delete-btn" data-index="${i}" title="删除存档">✕</button>
        <div class="slot-card-content">
          <div class="slot-title">${escapeHtml(displayTitle.slice(0, 20))}</div>
          <div class="slot-model-badge">${isDual ? "🎭🌟 双模型" : escapeHtml(displayModel(info.model) || "未知")}</div>
          <div class="slot-time"><span class="slot-time-label">创建</span><span class="slot-time-value">${created || "未知"}</span></div>
          <div class="slot-time"><span class="slot-time-label">使用</span><span class="slot-time-value">${updated || "未知"}</span></div>
        </div>
      `;

      card.addEventListener("click", (e) => {
        if (e.target.closest(".slot-delete-btn")) return;
        openSlot(i);
      });

      const delBtn = card.querySelector(".slot-delete-btn");
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSlotAction(i);
      });
    }

    grid.appendChild(card);
  }
}

// ── 聊天消息 ──

export function showEmptyState() {
  const container = el("chat-messages");
  if (!container) return;
  if (!container.querySelector(".message")) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">✦</div>
        <div class="empty-title">开始新的对话</div>
        <div class="empty-desc">在下方输入消息，与 AI 开始交流</div>
      </div>
    `;
  }
  updateContinueBtn();
}

export function renderMessages(history) {
  const container = el("chat-messages");
  if (!container) return;
  container.innerHTML = "";

  if (!history || history.length === 0) {
    showEmptyState();
    return;
  }

  let lastUserMsgId = null;
  let isDual = state.dualEnabled;
  let dualCfg = state.currentSlotData?.dual_config || {};

  history.forEach((msg) => {
    const isUser = msg.role === "user";
    const messageId = msg.id || null;

    // 双模型时的消息标签（根据 source 识别回复来源模型）
    let label = null;
    if (!isUser && isDual) {
      if (msg.source === "model1") {
        label = `🎭 ${dualCfg.model1_name || "1号"}`;
      } else if (msg.source === "model2") {
        label = `🌟 ${dualCfg.model2_name || "2号"}`;
      }
    }

    const bubble = addMessage(
      isUser ? "user" : "assistant",
      msg.content,
      false,
      messageId,
      label,
      msg.attachments || null,
    );

    if (isUser) {
      lastUserMsgId = messageId;
    }

    // 给 assistant 消息加重试按钮（历史消息，单/双模型通用）
    if (!isUser && lastUserMsgId !== null) {
      const msgDiv = bubble ? bubble.closest(".message") : null;
      if (msgDiv) {
        let regenBtn = msgDiv.querySelector(".regenerate-btn");
        if (!regenBtn) {
          regenBtn = document.createElement("button");
          regenBtn.className = "regenerate-btn";
          regenBtn.textContent = "↻";
          regenBtn.title = "重新生成";
          msgDiv.appendChild(regenBtn);
        }
        regenBtn.dataset.userMsgId = lastUserMsgId;
      }
    }
  });
  updateContinueBtn();
}

function renderAttachments(bubble, attachments) {
  if (!attachments || !attachments.length) return;
  const wrap = document.createElement("div");
  wrap.className = "bubble-attachments";
  for (const att of attachments) {
    if (att.kind === "image" && att.url) {
      const img = document.createElement("img");
      img.className = "bubble-attachment bubble-attachment-image";
      img.src = att.url;
      img.alt = att.name || "图片";
      img.title = att.name || "图片";
      img.addEventListener("click", () => window.open(img.src, "_blank", "noopener"));
      wrap.appendChild(img);
    } else {
      const chip = document.createElement("div");
      chip.className = "bubble-attachment file-chip";
      chip.textContent = att.name || "文件";
      wrap.appendChild(chip);
    }
  }
  bubble.appendChild(wrap);
}

export function addMessage(role, content, isStreaming = false, messageId = null, label = null, attachments = null) {
  const container = el("chat-messages");
  if (!container) return null;

  const empty = container.querySelector(".empty-state");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = `message ${role}`;
  if (messageId !== null) {
    div.dataset.messageId = messageId;
  }

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  // 双模型时，如果提供了 label，提取图标
  if (label && label.startsWith("🎭")) {
    avatar.textContent = "🎭";
  } else if (label && label.startsWith("🌟")) {
    avatar.textContent = "🌟";
  } else {
    avatar.textContent = role === "user" ? "👤" : "🤖";
  }
  avatar.setAttribute("aria-hidden", "true");

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.dataset.rawContent = content ?? "";
  if (isStreaming) bubble.classList.add("streaming");

  // 标签栏（双模型时显示角色名）
  if (label) {
    const labelBar = document.createElement("div");
    labelBar.className = "model-label-bar";
    if (label.includes("🌟")) {
      labelBar.classList.add("model-label-bar--model2");
    }
    labelBar.textContent = label;
    bubble.appendChild(labelBar);
  }

  renderAttachments(bubble, attachments);

  const contentDiv = document.createElement("div");
  contentDiv.className = "bubble-content";

  if (role === "user" && !isStreaming) {
    contentDiv.textContent = content;
  } else if (role === "assistant" && !isStreaming) {
    contentDiv.innerHTML = renderMarkdown(content);
    enhanceCodeBlocks(contentDiv);
  }
  // 流式输出时 contentDiv 留空，边流边写

  bubble.appendChild(contentDiv);

  div.appendChild(avatar);
  div.appendChild(bubble);

  if (role === "user") {
    const editBtn = document.createElement("button");
    editBtn.className = "user-edit-btn";
    editBtn.textContent = "✏️";
    editBtn.title = "编辑消息";
    editBtn.addEventListener("click", () => editAndResend(div));
    div.appendChild(editBtn);
  }

  container.appendChild(div);
  scrollToBottom();
  return bubble;  // 返回 .bubble 元素（流式写入用 textContent，完成用 innerHTML）
}

export function finishStreaming(bubble) {
  if (!bubble) return;
  // bubble 可能是 contentDiv，需要找到父级 .bubble
  const parentBubble = bubble.closest ? bubble.closest(".bubble") : null;
  if (parentBubble) {
    parentBubble.classList.remove("streaming");
  } else if (bubble.classList) {
    bubble.classList.remove("streaming");
  }
}

/** 根据当前是否有 AI 回复及是否在流式输出中，更新「继续回复」按钮可用状态 */
function updateContinueBtn() {
  const btn = el("continue-btn");
  if (!btn) return;
  const hasAssistant = !!document.querySelector("#chat-messages .message.assistant");
  btn.disabled = state.streaming || !hasAssistant;
}

// ── 侧边栏 ──

export function updateSidebarInfo() {
  const idx = state.currentSlotIndex;
  if (idx === null) return;

  el("slot-number-display").textContent = `存档 #${idx + 1}`;
  if (state.currentSlotData) {
    const title = state.currentSlotData.title || "未命名";
    el("slot-title-text").textContent = title;
    el("slot-model-display").textContent = displayModel(state.currentSlotData.model) || "-";

    // 双模型信息
    const dualCfg = state.currentSlotData.dual_config || {};
    if (dualCfg.enabled) {
      const m1Name = dualCfg.model1_name || "1号";
      const m2Name = dualCfg.model2_name || "2号";
      // 显示模型2
      const m2Display = el("slot-model2-display");
      if (m2Display) {
        const m2Model = displayModel(dualCfg.model2?.model) || "";
        m2Display.textContent = `🌟 ${m2Name} (${m2Model})`;
        m2Display.style.display = "block";
        m2Display.className = "model-display model-display-2";
      }
      // 更新模型1显示
      el("slot-model-display").textContent = `🎭 ${m1Name} (${displayModel(state.currentSlotData.model) || "-"})`;

      // 双模型提示词
      el("slot-prompt-display").textContent = `🎭 ${m1Name}: ${state.currentSlotData.system_prompt || "-"}`;
      const p2 = el("slot-prompt2-display");
      if (p2) {
        const m2Prompt = dualCfg.model2?.system_prompt || "使用中文回答";
        p2.textContent = `🌟 ${m2Name}: ${m2Prompt}`;
        p2.style.display = "block";
        p2.className = "prompt-display";
      }

      // 显示回复模式控制区
      const section = el("dual-response-section");
      if (section) section.style.display = "block";

      // 同步 radio 状态
      const mode = state.responseMode || dualCfg.response_mode || "both";
      const first = state.firstModel || dualCfg.first_model || "model1";
      const modeRadio = document.querySelector(`input[name="response-mode"][value="${mode}"]`);
      if (modeRadio) modeRadio.checked = true;
      const firstRadio = document.querySelector(`input[name="first-model"][value="${first}"]`);
      if (firstRadio) firstRadio.checked = true;

      // 先后顺序只在「同时回复」时显示
      const firstCtrl = el("first-model-control");
      if (firstCtrl) {
        firstCtrl.style.display = mode === "both" ? "block" : "none";
      }
    } else {
      // 单模型：隐藏双模型元素，显示普通提示词
      const m2Display = el("slot-model2-display");
      if (m2Display) m2Display.style.display = "none";
      const p2 = el("slot-prompt2-display");
      if (p2) p2.style.display = "none";
      const section = el("dual-response-section");
      if (section) section.style.display = "none";
      // 单模型显示普通提示词
      el("slot-prompt-display").textContent = state.currentSlotData.system_prompt || "-";
    }
  }

  el("chat-slot-badge").textContent = `存档 #${idx + 1}`;
  // 模型徽章
  const dualCfg = state.currentSlotData?.dual_config || {};
  if (dualCfg.enabled) {
    const m1Name = dualCfg.model1_name || "🎭";
    const m2Name = dualCfg.model2_name || "🌟";
    el("current-model-badge").textContent = `${m1Name} + ${m2Name}`;
  } else {
    el("current-model-badge").textContent = state.currentSlotData
      ? displayModel(state.currentSlotData.model) || ""
      : "";
  }
}

export function openSidebar() {
  el("sidebar").classList.add("open");
  el("sidebar-overlay").classList.add("active");
}

export function closeSidebar() {
  el("sidebar").classList.remove("open");
  el("sidebar-overlay").classList.remove("active");
}

// ── 流式状态指示 ──

export function setStreaming(val) {
  state.streaming = val;
  setStreamingFlag(val);
  const sendBtn = el("send-btn");
  const cancelBtn = el("cancel-stream-btn");
  const statusBadge = el("streaming-status-badge");
  const inputStatus = el("input-status");
  const messageInput = el("message-input");

  if (sendBtn) sendBtn.disabled = val;
  if (sendBtn) sendBtn.classList.toggle("sending", val);
  const attachBtn = el("attach-btn");
  if (attachBtn) attachBtn.disabled = val;
  if (cancelBtn) cancelBtn.classList.toggle("hidden", !val);
  if (statusBadge) statusBadge.classList.toggle("hidden", !val);
  if (inputStatus) {
    inputStatus.textContent = val
      ? "AI 正在回复…"
      : "AI 可能会犯错，请验证重要信息";
  }
  if (messageInput) messageInput.disabled = val;
  updateContinueBtn();
}

// ── 在聊天区添加错误提示 ──

export function addErrorMessage(content, retryText = null) {
  const container = el("chat-messages");
  if (!container) return;

  const empty = container.querySelector(".empty-state");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "message error";
  div.innerHTML = `<div class="bubble">⚠️ ${escapeHtml(content)}</div>`;
  container.appendChild(div);

  if (retryText) {
    const bubble = div.querySelector(".bubble");
    const retryBtn = document.createElement("button");
    retryBtn.className = "retry-btn";
    retryBtn.textContent = "🔄 重试";
    bubble.appendChild(retryBtn);

    retryBtn.addEventListener("click", () => {
      const input = document.getElementById("message-input");
      if (input) {
        input.value = retryText;
        const evt = new Event("input", { bubbles: true });
        input.dispatchEvent(evt);
        input.focus();
        const sendBtn = document.getElementById("send-btn");
        if (sendBtn && !sendBtn.disabled) sendBtn.click();
      }
    });
  }

  scrollToBottom();
}
