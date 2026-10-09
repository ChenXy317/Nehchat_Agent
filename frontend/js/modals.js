/**
 * 弹窗管理 — 创建存档向导（单/双模型）与导出弹窗。
 */

import { state } from "./state.js";
import { apiGet, apiPost, apiPatch } from "./api.js";
import { showToast } from "./toast.js";
import { loadSlots, updateSidebarInfo } from "./ui.js";
import { openSlot } from "./chat.js";

// ── 创建存档弹窗 ──

export let pendingCreateIndex = null;
let currentStep = 0;
let isDualMode = false;
let isEditMode = false;
let editSlotIndex = null;

/** 默认参数缓存 */
let defaultParams = null;

const MODEL1_ICON = "🎭";
const MODEL2_ICON = "🌟";

async function loadDefaultParams() {
  if (defaultParams) return defaultParams;
  try {
    defaultParams = await apiGet("/api/default-params");
  } catch (_) {
    defaultParams = {
      temperature: 1.1, min_p: 0.1, top_k: 100, top_p: 0.95,
      repeat_penalty: 1.25, presence_penalty: 0.4, frequency_penalty: 0.0,
      num_ctx: 131072, num_predict: 4096,
    };
  }
  return defaultParams;
}

function getParamValues(suffix = "") {
  const el = (id) => document.getElementById(`param${suffix}-${id}`);
  return {
    temperature: parseFloat(el("temperature").value),
    top_p: parseFloat(el("top-p").value),
    min_p: parseFloat(el("min-p").value),
    top_k: parseInt(el("top-k").value, 10),
    repeat_penalty: parseFloat(el("repeat-penalty").value),
    presence_penalty: parseFloat(el("presence-penalty").value),
    frequency_penalty: parseFloat(el("frequency-penalty").value),
    num_ctx: parseInt(el("num-ctx").value, 10),
    num_predict: parseInt(el("num-predict").value, 10),
  };
}

function showStep(step) {
  currentStep = step;

  // 处理步骤指示器 — 编辑模式下隐藏模式选择（step 0）
  const steps = document.querySelectorAll(".wizard-step");
  steps.forEach((el) => {
    const s = parseInt(el.dataset.step, 10);
    let isVisible;
    if (isEditMode) {
      // 编辑：跳过 step 0，直接从 1 开始
      isVisible = isDualMode ? (s >= 1) : (s >= 1 && s <= 3);
      if (s === 0) isVisible = false;
    } else {
      isVisible = isDualMode
        ? (s === 0 || s >= 1)  // 双模型: 显示所有步骤
        : (s >= 0 && s <= 3);  // 单模型: 只显示 0-3
    }
    el.style.display = isVisible ? "" : "none";

    el.classList.toggle("active", s === step);
    el.classList.toggle("done", s < step);
  });

  // 隐藏所有面板，显示当前
  document.querySelectorAll(".wizard-panel").forEach((el) => el.classList.add("hidden"));
  const panel = document.getElementById(`wizard-step-${step}`);
  if (panel) panel.classList.remove("hidden");

  // 按钮控制 — 编辑模式下 step 0 不存在，prev 逻辑需调整
  if (isEditMode) {
    const firstStep = 1;
    if (isDualMode) {
      if (step === 5) {
        document.getElementById("wizard-prev").style.display = "";
        document.getElementById("wizard-next").style.display = "none";
        document.getElementById("wizard-create").style.display = "";
      } else {
        document.getElementById("wizard-prev").style.display = step > firstStep ? "" : "none";
        document.getElementById("wizard-next").style.display = "";
        document.getElementById("wizard-create").style.display = "none";
      }
    } else {
      if (step === 3) {
        document.getElementById("wizard-prev").style.display = "";
        document.getElementById("wizard-next").style.display = "none";
        document.getElementById("wizard-create").style.display = "";
      } else {
        document.getElementById("wizard-prev").style.display = step > firstStep ? "" : "none";
        document.getElementById("wizard-next").style.display = "";
        document.getElementById("wizard-create").style.display = "none";
      }
    }
    // 更新创建按钮文案
    const createBtn = document.getElementById("wizard-create");
    if (createBtn) createBtn.textContent = isDualMode ? "保存修改" : "保存修改";
    return;
  }
  // 创建模式原逻辑
  if (step === 0) {
    document.getElementById("wizard-prev").style.display = "none";
    document.getElementById("wizard-next").style.display = "";
    document.getElementById("wizard-create").style.display = "none";
  } else if (isDualMode) {
    if (step === 5) {
      document.getElementById("wizard-prev").style.display = "";
      document.getElementById("wizard-next").style.display = "none";
      document.getElementById("wizard-create").style.display = "";
    } else {
      document.getElementById("wizard-prev").style.display = "";
      document.getElementById("wizard-next").style.display = "";
      document.getElementById("wizard-create").style.display = "none";
    }
  } else {
    // 单模型
    if (step === 3) {
      document.getElementById("wizard-prev").style.display = "";
      document.getElementById("wizard-next").style.display = "none";
      document.getElementById("wizard-create").style.display = "";
    } else {
      document.getElementById("wizard-prev").style.display = step > 1 ? "" : "none";
      document.getElementById("wizard-next").style.display = "";
      document.getElementById("wizard-create").style.display = "none";
    }
  }
  const createBtn = document.getElementById("wizard-create");
  if (createBtn) createBtn.textContent = "创建存档";
}

// ── 提供商配置 ──

function providerSelectIdFor(modelSelectId) {
  return modelSelectId === "create-model2-select" ? "create-provider2-select" : "create-provider-select";
}

function getProviders() {
  const seen = [];
  const set = new Set();
  for (const m of state.models || []) {
    if (!m.provider || set.has(m.provider)) continue;
    set.add(m.provider);
    seen.push({ slug: m.provider, name: m.provider_name || m.provider });
  }
  return seen;
}

function populateModelSelect(selectId, selectedKey) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const provider = document.getElementById(providerSelectIdFor(selectId))?.value || "";
  const list = (state.models || []).filter((m) => !provider || m.provider === provider);
  select.innerHTML = "";
  let firstKey = null;
  list.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.key;
    opt.textContent = m.display_name || m.id || m.key;
    if (m.key === selectedKey) opt.selected = true;
    if (firstKey === null) firstKey = m.key;
    select.appendChild(opt);
  });
  if (list.length && !list.some((m) => m.key === select.value)) {
    select.value = firstKey;
  }
}

function populateProviderSelect(providerSelectId, modelSelectId, selectedKey) {
  const pSelect = document.getElementById(providerSelectId);
  if (!pSelect) return;
  const providers = getProviders();
  pSelect.innerHTML = "";
  providers.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.slug;
    opt.textContent = p.name;
    pSelect.appendChild(opt);
  });
  const slugs = providers.map((p) => p.slug);
  const target = (state.models || []).find((m) => m.key === selectedKey);
  if (target && slugs.includes(target.provider)) {
    pSelect.value = target.provider;
  } else if (slugs.length) {
    pSelect.value = slugs[0];
  }
  populateModelSelect(modelSelectId, selectedKey);
}

function updateParamDisplays(suffix = "") {
  const ids = [
    "temperature", "top-p", "min-p", "top-k",
    "repeat-penalty", "presence-penalty", "frequency-penalty",
  ];
  ids.forEach((id) => {
    const input = document.getElementById(`param${suffix}-${id}`);
    const display = document.getElementById(`param${suffix}-${id}-val`);
    if (input && display) display.textContent = input.value;
  });
}

export async function openCreateModal(index) {
  if (!(state.models || []).length) {
    showToast("请先在右上角「模型配置」中添加供应商和模型", "warning");
    return;
  }
  pendingCreateIndex = index;
  isDualMode = false;
  isEditMode = false;
  editSlotIndex = null;
  currentStep = 0;

  // 重置模式选择
  document.getElementById("dual-option-single").querySelector("input").checked = true;
  document.getElementById("dual-option-dual").querySelector("input").checked = false;

  // 重置 step3 显示
  document.getElementById("step3-single").style.display = "";
  document.getElementById("step3-dual").style.display = "none";

  // 隐藏模型1名称字段和 step2 系统提示词（单模型默认）
  document.getElementById("field-model1-name").style.display = "none";
  document.getElementById("step2-prompt-field").style.display = "none";
  document.getElementById("step1-icon").textContent = "🤖";
  document.getElementById("step1-title").textContent = "选择模型";

  // 填充模型下拉框
  const defaultKey = (state.models[0] && state.models[0].key) || "";
  populateProviderSelect("create-provider-select", "create-model-select", defaultKey);
  populateProviderSelect("create-provider2-select", "create-model2-select", defaultKey);

  // 重置值
  document.getElementById("create-model1-name").value = "";
  document.getElementById("create-model2-name").value = "";
  document.getElementById("create-title").value = "";
  document.getElementById("create-title-dual").value = "";
  document.getElementById("create-system-prompt").value = "使用中文回答";
  document.getElementById("create-system-prompt-single").value = "使用中文回答";
  document.getElementById("create-system-prompt-2").value = "使用中文回答";

  const params = await loadDefaultParams();
  // 模型1参数
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = String(val);
  };
  setVal("param-temperature", params.temperature ?? 1.1);
  setVal("param-top-p", params.top_p ?? 0.95);
  setVal("param-min-p", params.min_p ?? 0.1);
  setVal("param-top-k", params.top_k ?? 100);
  setVal("param-repeat-penalty", params.repeat_penalty ?? 1.25);
  setVal("param-presence-penalty", params.presence_penalty ?? 0.4);
  setVal("param-frequency-penalty", params.frequency_penalty ?? 0.0);
  setVal("param-num-ctx", params.num_ctx ?? 131072);
  setVal("param-num-predict", params.num_predict ?? 4096);

  // 模型2参数
  setVal("param2-temperature", params.temperature ?? 1.1);
  setVal("param2-top-p", params.top_p ?? 0.95);
  setVal("param2-min-p", params.min_p ?? 0.1);
  setVal("param2-top-k", params.top_k ?? 100);
  setVal("param2-repeat-penalty", params.repeat_penalty ?? 1.25);
  setVal("param2-presence-penalty", params.presence_penalty ?? 0.4);
  setVal("param2-frequency-penalty", params.frequency_penalty ?? 0.0);
  setVal("param2-num-ctx", params.num_ctx ?? 131072);
  setVal("param2-num-predict", params.num_predict ?? 4096);

  updateParamDisplays();
  updateParamDisplays("2");

  showStep(0);
  document.getElementById("create-modal").classList.remove("hidden");
}

export function closeCreateModal() {
  document.getElementById("create-modal").classList.add("hidden");
  pendingCreateIndex = null;
  // 重置编辑状态
  isEditMode = false;
  editSlotIndex = null;
  const createBtn = document.getElementById("wizard-create");
  if (createBtn) createBtn.textContent = "创建存档";
}

/** 模型更换 — 打开编辑弹窗（复用创建向导，但数据回填且仅更新提供的字段） */
export async function openEditModal() {
  if (state.streaming) {
    showToast("请等待当前回复完成后再更换模型", "warning");
    return;
  }
  const idx = state.currentSlotIndex;
  const data = state.currentSlotData;
  if (idx === null || !data) {
    showToast("未找到当前存档信息", "error");
    return;
  }

  isEditMode = true;
  editSlotIndex = idx;
  isDualMode = !!data.dual_enabled;
  pendingCreateIndex = null;

  // 切换模式显示：编辑时隐藏模式选择，仅展示模型配置
  document.getElementById("dual-option-single").querySelector("input").checked = !isDualMode;
  document.getElementById("dual-option-dual").querySelector("input").checked = isDualMode;

  if (isDualMode) {
    document.getElementById("field-model1-name").style.display = "block";
    document.getElementById("step1-icon").textContent = MODEL1_ICON;
    document.getElementById("step1-title").textContent = "模型 1 配置";
    document.getElementById("step2-prompt-field").style.display = "";
    document.getElementById("step3-single").style.display = "none";
    document.getElementById("step3-dual").style.display = "block";
    document.getElementById("step3-icon").textContent = MODEL2_ICON;
    document.getElementById("step3-title").textContent = "模型 2 配置";
  } else {
    document.getElementById("field-model1-name").style.display = "none";
    document.getElementById("step1-icon").textContent = "🤖";
    document.getElementById("step1-title").textContent = "选择模型";
    document.getElementById("step2-prompt-field").style.display = "none";
    document.getElementById("step3-single").style.display = "";
    document.getElementById("step3-dual").style.display = "none";
    document.getElementById("step3-icon").textContent = "💬";
    document.getElementById("step3-title").textContent = "设置提示词";
  }

  // ——— 填充模型1 ———
  const curModel = data.model || "";
  const curDual = data.dual_config || {};
  const curParams = data.params || await loadDefaultParams();
  const curPrompt = data.system_prompt || "使用中文回答";
  const curTitle = data.title || "";

  // 模型1 下拉
  populateProviderSelect("create-provider-select", "create-model-select", curModel);
  // 若模型不存在于列表（历史遗留），仍尝试选中
  const m1Select = document.getElementById("create-model-select");
  if (m1Select && curModel && !Array.from(m1Select.options).some(o => o.value === curModel)) {
    const opt = document.createElement("option");
    opt.value = curModel;
    opt.textContent = curModel.split(":").pop() || curModel;
    opt.selected = true;
    m1Select.appendChild(opt);
  }
  if (m1Select) m1Select.value = curModel;
  // 同步提供商下拉（若模型含 : 则解析 provider）
  const m1Provider = (state.models.find(m => m.key === curModel)?.provider) || curModel.split(":")[0];
  const p1Select = document.getElementById("create-provider-select");
  if (p1Select && m1Provider) {
    // 若提供商不在列表，仍保留
    if (!Array.from(p1Select.options).some(o => o.value === m1Provider)) {
      const opt = document.createElement("option");
      opt.value = m1Provider;
      opt.textContent = (state.models.find((m) => m.provider === m1Provider)?.provider_name) || m1Provider;
      p1Select.appendChild(opt);
    }
    p1Select.value = m1Provider;
    // 重新填充模型下拉以保证一致性
    populateModelSelect("create-model-select", curModel);
    m1Select.value = curModel;
  }

  document.getElementById("create-model1-name").value = curDual.model1_name || "";
  document.getElementById("create-system-prompt").value = curPrompt;
  document.getElementById("create-system-prompt-single").value = curPrompt;
  document.getElementById("create-title").value = curTitle;
  document.getElementById("create-title-dual").value = curTitle;

  // 填充模型1参数
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = String(val);
  };
  const p = curParams || {};
  setVal("param-temperature", p.temperature ?? 1.1);
  setVal("param-top-p", p.top_p ?? 0.95);
  setVal("param-min-p", p.min_p ?? 0.1);
  setVal("param-top-k", p.top_k ?? 100);
  setVal("param-repeat-penalty", p.repeat_penalty ?? 1.25);
  setVal("param-presence-penalty", p.presence_penalty ?? 0.4);
  setVal("param-frequency-penalty", p.frequency_penalty ?? 0.0);
  setVal("param-num-ctx", p.num_ctx ?? 131072);
  setVal("param-num-predict", p.num_predict ?? 4096);

  // ——— 填充模型2（仅双模型） ———
  if (isDualMode) {
    const m2 = curDual.model2 || {};
    const m2Model = m2.model || curModel;
    const m2Params = m2.params || p;
    const m2Prompt = m2.system_prompt || "使用中文回答";
    populateProviderSelect("create-provider2-select", "create-model2-select", m2Model);
    const m2Select = document.getElementById("create-model2-select");
    if (m2Select && m2Model && !Array.from(m2Select.options).some(o => o.value === m2Model)) {
      const opt = document.createElement("option");
      opt.value = m2Model;
      opt.textContent = m2Model.split(":").pop() || m2Model;
      opt.selected = true;
      m2Select.appendChild(opt);
    }
    if (m2Select) m2Select.value = m2Model;
    const m2Provider = (state.models.find(m => m.key === m2Model)?.provider) || m2Model.split(":")[0];
    const p2Select = document.getElementById("create-provider2-select");
    if (p2Select && m2Provider) {
      if (!Array.from(p2Select.options).some(o => o.value === m2Provider)) {
        const opt = document.createElement("option");
        opt.value = m2Provider;
        opt.textContent = (state.models.find((m) => m.provider === m2Provider)?.provider_name) || m2Provider;
        p2Select.appendChild(opt);
      }
      p2Select.value = m2Provider;
      populateModelSelect("create-model2-select", m2Model);
      m2Select.value = m2Model;
    }

    document.getElementById("create-model2-name").value = curDual.model2_name || "";
    document.getElementById("create-system-prompt-2").value = m2Prompt;
    setVal("param2-temperature", m2Params.temperature ?? 1.1);
    setVal("param2-top-p", m2Params.top_p ?? 0.95);
    setVal("param2-min-p", m2Params.min_p ?? 0.1);
    setVal("param2-top-k", m2Params.top_k ?? 100);
    setVal("param2-repeat-penalty", m2Params.repeat_penalty ?? 1.25);
    setVal("param2-presence-penalty", m2Params.presence_penalty ?? 0.4);
    setVal("param2-frequency-penalty", m2Params.frequency_penalty ?? 0.0);
    setVal("param2-num-ctx", m2Params.num_ctx ?? 131072);
    setVal("param2-num-predict", m2Params.num_predict ?? 4096);

    // pass_mode
    const passMode = curDual.pass_mode || "user";
    const passRadio = document.querySelector(`input[name="pass-mode"][value="${passMode}"]`);
    if (passRadio) passRadio.checked = true;
  } else {
    // 单模型重置模型2字段，避免误提交
    document.getElementById("create-model2-name").value = "";
    document.getElementById("create-system-prompt-2").value = "使用中文回答";
  }

  updateParamDisplays();
  updateParamDisplays("2");

  showStep(1);
  document.getElementById("create-modal").classList.remove("hidden");
}

// ── 导出弹窗 ──

export async function openExportModal() {
  const idx = state.currentSlotIndex;
  if (idx === null) return;

  if (document.querySelector(".export-content")) return;

  try {
    const data = await apiGet(`/api/slots/${idx}/chat/export`);

    const modelKey = data.model || "";
    const modelDisplay = modelKey.includes(":") ? modelKey.split(":").pop() : (modelKey || "未知");
    const lines = [
      `# ${data.title || "对话导出"}`,
      "",
      `**模型**: ${modelDisplay}`,
      `**系统提示词**: ${data.system_prompt || "无"}`,
      `**创建时间**: ${data.created_at || "未知"}`,
      `**更新时间**: ${data.updated_at || "未知"}`,
      "",
      "---",
      "",
    ];

    for (const msg of data.messages || []) {
      const roleLabel = msg.role === "user" ? "👤 **你**" : "🤖 **AI**";
      lines.push(`### ${roleLabel}`);
      lines.push("");
      const attachmentNames = (msg.attachments || []).map((item) => item.name).filter(Boolean);
      if (attachmentNames.length) {
        lines.push(`附件: ${attachmentNames.join("、")}`);
        lines.push("");
      }
      lines.push(msg.content || "");
      lines.push("");
    }

    const content = lines.join("\n");

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal-content export-content">
        <div class="modal-icon">📥</div>
        <div class="modal-title">导出对话</div>
        <p class="export-hint">复制下方内容或使用右下角的「复制全部」按钮</p>
        <textarea class="export-textarea" readonly>${content.replace(/</g, "&lt;").replace(/>/g, "&gt;")}</textarea>
        <div class="modal-actions">
          <button class="modal-btn modal-btn-cancel" id="export-close">关闭</button>
          <button class="modal-btn modal-btn-confirm" id="export-copy">📋 复制全部</button>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    overlay.querySelector("#export-close").onclick = () => overlay.remove();
    overlay.querySelector("#export-copy").onclick = () => {
      const textarea = overlay.querySelector(".export-textarea");
      if (textarea) {
        navigator.clipboard.writeText(textarea.value).then(() => {
          showToast("已复制到剪贴板", "success");
        }).catch(() => {
          showToast("复制失败，请手动选择复制", "warning");
        });
      }
    };
    overlay.onclick = (e) => {
      if (e.target === overlay) overlay.remove();
    };
  } catch (e) {
    showToast("导出失败: " + e.message, "error");
  }
}

export async function exportSlotBackup() {
  const idx = state.currentSlotIndex;
  if (idx === null) return;
  try {
    const data = await apiGet(`/api/slots/${idx}/backup`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${data.title || `存档-${idx + 1}`}.json`;
    link.click();
    URL.revokeObjectURL(url);
    showToast("存档备份已导出", "success");
  } catch (e) {
    showToast("导出备份失败: " + e.message, "error");
  }
}

export function importSlotBackup(file) {
  const idx = state.slots.findIndex((slot) => slot === null || slot === undefined);
  if (idx < 0 || !file) {
    if (file) showToast("没有可用的空存档位", "warning");
    return;
  }
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const backup = JSON.parse(reader.result);
      await apiPost(`/api/slots/${idx}/backup`, backup);
      showToast("存档备份已导入，请重新打开存档", "success");
      window.location.reload();
    } catch (e) {
      showToast("导入备份失败: " + e.message, "error");
    }
  };
  reader.onerror = () => showToast("无法读取备份文件", "error");
  reader.readAsText(file);
}

// ── 帮助弹窗 ──

export function openHelpModal() {
  document.getElementById("help-modal").classList.remove("hidden");
}

export function closeHelpModal() {
  document.getElementById("help-modal").classList.add("hidden");
}

// ── 初始化弹窗事件监听 ──

function syncSystemPromptSource() {
  // 仅双模型模式同步（单模型模式的提示词在 step3 单独输入，不可被覆盖）
  if (!isDualMode) return;
  const sp = document.getElementById("create-system-prompt").value;
  const spSingle = document.getElementById("create-system-prompt-single");
  if (spSingle) spSingle.value = sp;
}

export function initModalListeners() {
  const createModal = document.getElementById("create-modal");

  // ── 模式选择 ──
  document.querySelectorAll('input[name="dual-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      if (isEditMode) {
        // 编辑模式下禁止切换单/双模型
        showToast("更换模型时不可切换单/双模式，请新建存档", "warning");
        // 回退选中状态
        document.getElementById("dual-option-single").querySelector("input").checked = !isDualMode;
        document.getElementById("dual-option-dual").querySelector("input").checked = isDualMode;
        return;
      }
      const singleRadio = document.getElementById("dual-option-single").querySelector("input");
      isDualMode = !singleRadio.checked;
      if (isDualMode) {
        // 双模型模式
        document.getElementById("field-model1-name").style.display = "block";
        document.getElementById("step1-icon").textContent = MODEL1_ICON;
        document.getElementById("step1-title").textContent = "模型 1 配置";
        // 步骤 2 显示模型 1 系统提示词
        document.getElementById("step2-prompt-field").style.display = "";
        // 步骤 3 显示模型 2 配置
        document.getElementById("step3-single").style.display = "none";
        document.getElementById("step3-dual").style.display = "block";
        document.getElementById("step3-icon").textContent = MODEL2_ICON;
        document.getElementById("step3-title").textContent = "模型 2 配置";
        const fallbackKey = (state.models[0] && state.models[0].key) || "";
        populateProviderSelect("create-provider2-select", "create-model2-select", fallbackKey);
      } else {
        document.getElementById("field-model1-name").style.display = "none";
        document.getElementById("step1-icon").textContent = "🤖";
        document.getElementById("step1-title").textContent = "选择模型";
        // 步骤 2 隐藏系统提示词（单模型的提示词在步骤 3）
        document.getElementById("step2-prompt-field").style.display = "none";
        document.getElementById("step3-single").style.display = "";
        document.getElementById("step3-dual").style.display = "none";
        document.getElementById("step3-icon").textContent = "💬";
        document.getElementById("step3-title").textContent = "设置提示词";
      }
      showStep(0);
    });
  });

  // ── 步骤导航 ──
  document.getElementById("wizard-next").addEventListener("click", () => {
    if (!isEditMode && currentStep === 0) {
      // 创建模式：从模式选择进入
      showStep(1);
      return;
    }
    if (currentStep === 1) {
      const modelVal = document.getElementById("create-model-select")?.value;
      if (!modelVal) {
        showToast("请选择模型，若没有可选项请先到右上角「模型配置」添加", "warning");
        return;
      }
      showStep(2);
    } else if (currentStep === 2) {
      if (isDualMode) {
        // 双模型: step 2 → step 3 (模型2)
        syncSystemPromptSource();
        showStep(3);
      } else {
        // 单模型: step 2 的参数结束 → step 3 提示词
        syncSystemPromptSource();
        showStep(3);
      }
    } else if (currentStep === 3) {
      if (isDualMode) {
        const model2Val = document.getElementById("create-model2-select")?.value;
        if (!model2Val) {
          showToast("请选择模型 2，若没有可选项请先到右上角「模型配置」添加", "warning");
          return;
        }
        showStep(4);
      }
    } else if (currentStep === 4) {
      if (isDualMode) {
        showStep(5); // 标题
      }
    }
  });

  document.getElementById("wizard-prev").addEventListener("click", () => {
    if (isEditMode) {
      if (currentStep > 1) showStep(currentStep - 1);
    } else {
      if (currentStep > 0) showStep(currentStep - 1);
    }
  });

  document.getElementById("wizard-cancel").addEventListener("click", closeCreateModal);

  // ── 创建/更换 存档 ──
  document.getElementById("wizard-create").addEventListener("click", async () => {
    // ——— 编辑模式：模型更换 ———
    if (isEditMode) {
      const idx = editSlotIndex;
      if (idx === null) {
        showToast("未找到要更新的存档", "error");
        return;
      }
      // 区分单/双：两者完全独立更新，互不覆盖
      if (isDualMode) {
        const model1Select = document.getElementById("create-model-select");
        const model1Model = model1Select.value;
        const model1NameRaw = document.getElementById("create-model1-name").value.trim();
        const model1Prompt = document.getElementById("create-system-prompt").value.trim() || "使用中文回答";
        const model1Params = getParamValues("");

        const model2Select = document.getElementById("create-model2-select");
        const model2Model = model2Select.value;
        const model2NameRaw = document.getElementById("create-model2-name").value.trim();
        const model2Prompt = document.getElementById("create-system-prompt-2").value.trim() || "使用中文回答";
        const model2Params = getParamValues("2");

        const titleRaw = document.getElementById("create-title-dual").value.trim();
        const passMode = document.querySelector('input[name="pass-mode"]:checked')?.value || "user";

        // 构造独立的更新体：model1 与 model2 完全分离
        const payload = {
          model: model1Model,
          system_prompt: model1Prompt,
          params: model1Params,
          model1_name: model1NameRaw,
          model2_name: model2NameRaw,
          pass_mode: passMode,
          model2: {
            model: model2Model,
            system_prompt: model2Prompt,
            params: model2Params,
          },
        };
        if (titleRaw !== "") payload.title = titleRaw;

        try {
          await apiPatch(`/api/slots/${idx}/config`, payload);
          closeCreateModal();
          showToast("模型已更换", "success");
          const fresh = await apiGet(`/api/slots/${idx}/chat`);
          state.currentSlotData = fresh;
          state.dualEnabled = fresh.dual_enabled || false;
          state.responseMode = fresh.response_mode || "both";
          state.firstModel = fresh.first_model || "model1";
          updateSidebarInfo();
          await loadSlots();
        } catch (e) {
          showToast("更换失败: " + e.message, "error");
        }
      } else {
        // 单模型更换
        const select = document.getElementById("create-model-select");
        const promptInput = document.getElementById("create-system-prompt-single");
        const titleInput = document.getElementById("create-title");

        const model = select.value;
        const systemPrompt = promptInput.value.trim() || "使用中文回答";
        const params = getParamValues("");
        const titleRaw = titleInput.value.trim();

        const payload = {
          model,
          system_prompt: systemPrompt,
          params,
        };
        if (titleRaw !== "") payload.title = titleRaw;

        try {
          await apiPatch(`/api/slots/${idx}/config`, payload);
          closeCreateModal();
          showToast("模型已更换", "success");
          const fresh = await apiGet(`/api/slots/${idx}/chat`);
          state.currentSlotData = fresh;
          state.dualEnabled = fresh.dual_enabled || false;
          updateSidebarInfo();
          await loadSlots();
        } catch (e) {
          showToast("更换失败: " + e.message, "error");
        }
      }
      return;
    }

    // ——— 创建模式 ———
    const idx = pendingCreateIndex;
    if (idx === null) return;

    if (isDualMode) {
      // ── 双模型创建 ──
      const model1Select = document.getElementById("create-model-select");
      const model1Name = document.getElementById("create-model1-name").value.trim() || "1号";
      const model1Model = model1Select.value;
      const model1Prompt = document.getElementById("create-system-prompt").value.trim() || "使用中文回答";
      const model1Params = getParamValues("");

      const model2Select = document.getElementById("create-model2-select");
      const model2Name = document.getElementById("create-model2-name").value.trim() || "2号";
      const model2Model = model2Select.value;
      const model2Prompt = document.getElementById("create-system-prompt-2").value.trim() || "使用中文回答";
      const model2Params = getParamValues("2");

      try {
        await apiPost(`/api/slots/${idx}`, {
          model: model1Model,
          system_prompt: model1Prompt,
          params: model1Params,
          title: document.getElementById("create-title-dual").value.trim(),
          dual_enabled: true,
          model1_name: model1Name,
          model2_name: model2Name,
          pass_mode: document.querySelector('input[name="pass-mode"]:checked')?.value || "user",
          model2: {
            model: model2Model,
            system_prompt: model2Prompt,
            params: model2Params,
          },
        });
        closeCreateModal();
        showToast("双模型存档创建成功", "success");
        await openSlot(idx);
      } catch (e) {
        showToast("创建失败: " + e.message, "error");
      }
    } else {
      // ── 单模型创建 ──
      const select = document.getElementById("create-model-select");
      const promptInput = document.getElementById("create-system-prompt-single");
      const titleInput = document.getElementById("create-title");

      const model = select.value;
      const systemPrompt = promptInput.value.trim() || "使用中文回答";

      try {
        await apiPost(`/api/slots/${idx}`, {
          model,
          system_prompt: systemPrompt,
          title: titleInput.value.trim(),
          params: getParamValues(""),
        });
        closeCreateModal();
        showToast("存档创建成功", "success");
        await openSlot(idx);
      } catch (e) {
        showToast("创建失败: " + e.message, "error");
      }
    }
  });

  // 关闭弹窗
  createModal.addEventListener("click", (e) => {
    if (e.target === createModal) closeCreateModal();
  });

  document.getElementById("create-provider-select").addEventListener("change", () => {
    populateModelSelect("create-model-select");
  });
  document.getElementById("create-provider2-select").addEventListener("change", () => {
    populateModelSelect("create-model2-select");
  });

  // 参数滑块联动
  const setupRangeListeners = (suffix = "") => {
    const ids = [
      "temperature", "top-p", "min-p", "top-k",
      "repeat-penalty", "presence-penalty", "frequency-penalty",
    ];
    ids.forEach((id) => {
      const input = document.getElementById(`param${suffix}-${id}`);
      if (input) {
        input.addEventListener("input", () => {
          const display = document.getElementById(`param${suffix}-${id}-val`);
          if (display) display.textContent = input.value;
        });
      }
    });
  };
  setupRangeListeners("");
  setupRangeListeners("2");

  // 确认弹窗
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target === document.getElementById("modal-overlay")) {
      document.getElementById("modal-overlay").classList.add("hidden");
    }
  });

  // 帮助弹窗
  document.getElementById("help-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("help-modal")) {
      closeHelpModal();
    }
  });
}
