/**
 * 输入区附件 — 选择、粘贴、预览图片和文本文件。
 * 数量与大小限制与后端一致。
 */

import { showToast } from "./toast.js";
import { state } from "./state.js";

const MAX_COUNT = 4;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_TEXT_BYTES = 512 * 1024;
const IMAGE_EXT = new Set(["jpg", "jpeg", "png", "webp", "gif"]);
const TEXT_EXT = new Set(["txt", "md", "csv", "json", "log"]);

let pending = [];
let seq = 0;

function extOf(name) {
  const parts = String(name || "").toLowerCase().split(".");
  return parts.length > 1 ? parts.pop() : "";
}

function kindOf(file) {
  const ext = extOf(file.name);
  if (file.type.startsWith("image/") || IMAGE_EXT.has(ext)) {
    if (!IMAGE_EXT.has(ext) && !["image/jpeg", "image/png", "image/webp", "image/gif"].includes(file.type)) {
      return "";
    }
    if (IMAGE_EXT.has(ext) || ["image/jpeg", "image/png", "image/webp", "image/gif"].includes(file.type)) {
      return "image";
    }
  }
  if (TEXT_EXT.has(ext)) return "text";
  return "";
}

function renderPending() {
  const box = document.getElementById("attach-preview");
  if (!box) return;
  box.innerHTML = "";
  if (!pending.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  for (const item of pending) {
    const card = document.createElement("div");
    card.className = "attach-card";
    if (item.kind === "image") {
      const img = document.createElement("img");
      img.src = item.previewUrl;
      img.alt = item.name;
      card.appendChild(img);
    } else {
      const label = document.createElement("div");
      label.className = "attach-file-name";
      label.textContent = item.name;
      card.appendChild(label);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attach-remove";
    remove.title = "移除";
    remove.textContent = "×";
    remove.addEventListener("click", () => removePending(item.localId));
    card.appendChild(remove);
    box.appendChild(card);
  }
}

function removePending(localId) {
  const item = pending.find((entry) => entry.localId === localId);
  if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
  pending = pending.filter((entry) => entry.localId !== localId);
  renderPending();
}

export function hasPending() {
  return pending.length > 0;
}

export function takePending() {
  const items = pending;
  pending = [];
  renderPending();
  return items;
}

export function restorePending(items) {
  if (!items || !items.length) return;
  for (const item of items) {
    if (pending.length >= MAX_COUNT) break;
    if (pending.some((entry) => entry.localId === item.localId)) continue;
    pending.push(item);
  }
  renderPending();
}

function addFiles(fileList) {
  if (state.streaming) return;
  const files = [...fileList];
  if (!files.length) return;
  let rejected = false;
  for (const file of files) {
    if (pending.length >= MAX_COUNT) {
      showToast("最多添加 4 个附件", "warning");
      break;
    }
    const kind = kindOf(file);
    if (!kind) {
      rejected = true;
      continue;
    }
    const limit = kind === "image" ? MAX_IMAGE_BYTES : MAX_TEXT_BYTES;
    if (file.size > limit) {
      showToast(kind === "image" ? "单张图片不能超过 8MB" : "单个文本文件不能超过 512KB", "warning");
      continue;
    }
    if (file.size <= 0) continue;
    pending.push({
      localId: `f${++seq}`,
      file,
      name: file.name || (kind === "image" ? "图片" : "文件"),
      kind,
      previewUrl: kind === "image" ? URL.createObjectURL(file) : "",
    });
  }
  if (rejected) {
    showToast("只支持 jpg、png、webp、gif 图片，以及 txt、md、csv、json、log", "warning");
  }
  renderPending();
}

export async function uploadAttachment(file) {
  const body = new FormData();
  body.append("file", file, file.name);
  const res = await fetch("/api/uploads", { method: "POST", body });
  let detail = null;
  try { detail = await res.json(); } catch (_) { /* 忽略 */ }
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("app-unauthorized"));
  }
  if (!res.ok) {
    throw new Error(detail?.message || `上传失败: ${res.status}`);
  }
  return detail;
}

export function bindAttachmentUi() {
  const button = document.getElementById("attach-btn");
  const input = document.getElementById("attach-input");
  const area = document.getElementById("input-area");
  const messageInput = document.getElementById("message-input");
  if (button && input) {
    button.addEventListener("click", () => {
      if (!state.streaming) input.click();
    });
    input.addEventListener("change", () => {
      addFiles(input.files || []);
      input.value = "";
    });
  }
  if (messageInput) {
    messageInput.addEventListener("paste", (event) => {
      const files = [...(event.clipboardData?.files || [])];
      if (!files.length) return;
      event.preventDefault();
      addFiles(files);
    });
  }
  if (area) {
    area.addEventListener("dragover", (event) => {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      area.classList.add("dragover");
    });
    area.addEventListener("dragleave", () => area.classList.remove("dragover"));
    area.addEventListener("drop", (event) => {
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault();
      area.classList.remove("dragover");
      addFiles(event.dataTransfer.files);
    });
  }
}
