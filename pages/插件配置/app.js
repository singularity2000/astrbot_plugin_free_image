const bridge = window.AstrBotPluginPage || {
  ready: async () => ({}),
  apiGet: async () => ({ success: true }),
  apiPost: async () => ({ success: true }),
  upload: async () => ({ success: false, message: "当前不在 AstrBot Dashboard 中。" }),
};

const CACHE_PAGE_SIZE_KEY = "freeimage.cache.pageSize";
const HISTORY_PAGE_SIZE_KEY = "freeimage.history.pageSize";
const LAST_TAB_KEY = "freeimage.lastTab";
const PAGE_API_TIMEOUT_MS = 25000;
const PREVIEW_API_TIMEOUT_MS = 20000;
const FULL_IMAGE_TIMEOUT_MS = 45000;
const PREVIEW_CONCURRENCY = 3;
const THEME_KEY = "freeimage.theme";
const THEME_VALUES = new Set(["system", "light", "dark"]);
const TAB_VALUES = new Set(["pipeline", "templates", "selfie", "history"]);
const DEFAULT_THEME = "system";
const DEFAULT_CACHE_PAGE_SIZE = 24;
const DEFAULT_HISTORY_PAGE_SIZE = 20;

let memoryStorageFallback = {};
let sortIdentityCounter = 0;
const sortIdentities = new WeakMap();

function sortIdForObject(prefix, item) {
  if (!item || typeof item !== "object") return `${prefix}-primitive-${sortIdentityCounter += 1}`;
  if (!sortIdentities.has(item)) {
    sortIdentityCounter += 1;
    sortIdentities.set(item, `${prefix}-${sortIdentityCounter}`);
  }
  return sortIdentities.get(item);
}

function safeStorageGet(key, fallback = "") {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      const value = window.localStorage.getItem(key);
      return value ?? fallback;
    }
  } catch (error) {
    console.warn(`[FreeImage Pages] localStorage get blocked for ${key}:`, error);
  }
  return Object.prototype.hasOwnProperty.call(memoryStorageFallback, key)
    ? memoryStorageFallback[key]
    : fallback;
}

function safeStorageSet(key, value) {
  memoryStorageFallback[key] = String(value);
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      window.localStorage.setItem(key, String(value));
      return true;
    }
  } catch (error) {
    console.warn(`[FreeImage Pages] localStorage set blocked for ${key}:`, error);
  }
  return false;
}

const state = {
  schema: {},
  pipeline: [],
  promptTemplates: [],
  history: [],
  filteredHistory: [],
  historyPage: 1,
  historyPageSize: DEFAULT_HISTORY_PAGE_SIZE,
  historyTotal: 0,
  historyTotalPages: 1,
  historyStats: null,
  historyFacets: {
    modes: [],
    models: [],
  },
  cache: {
    enabled: false,
    max_mb: "",
    max_hours: "",
    max_count: "",
    total_count: 0,
    total_bytes: 0,
    images: [],
  },
  cachePage: 1,
  cachePageSize: DEFAULT_CACHE_PAGE_SIZE,
  cacheTotalPages: 1,
  pagePrefs: {
    theme: DEFAULT_THEME,
    cache_page_size: DEFAULT_CACHE_PAGE_SIZE,
    history_page_size: DEFAULT_HISTORY_PAGE_SIZE,
    last_tab: "pipeline",
  },
  selfie: {
    binding_mode: "优先 AstrBot persona",
    manual_override: "",
    default_persona_id: "",
    style_mode: "自动",
    selected_style_id: "",
    personas: [],
    styles: [],
  },
  activePersonaIndex: 0,
  activeStyleIndex: 0,
  activeTab: "pipeline",
  slideItems: [],
  slideIndex: 0,
  expandedPipeline: new Set(),
  expandedTemplates: new Set(),
};

const byId = (id) => document.getElementById(id);

function getThemeValue() {
  const stateTheme = String(state.pagePrefs?.theme || "").trim().toLowerCase();
  if (THEME_VALUES.has(stateTheme)) return stateTheme;
  const value = safeStorageGet(THEME_KEY, DEFAULT_THEME);
  return THEME_VALUES.has(value) ? value : "system";
}

function syncThemeButtons() {
  const theme = getThemeValue();
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.classList.toggle("active", button.dataset.themeChoice === theme);
  });
}

function applyTheme(theme) {
  const nextTheme = THEME_VALUES.has(theme) ? theme : "system";
  state.pagePrefs.theme = nextTheme;
  safeStorageSet(THEME_KEY, nextTheme);
  document.documentElement.dataset.theme = nextTheme;
  syncThemeButtons();
}

function normalizeCachePageSize(value) {
  const parsed = Number(value);
  return [12, 24, 48, 96].includes(parsed) ? parsed : DEFAULT_CACHE_PAGE_SIZE;
}

function normalizeHistoryPageSize(value) {
  const parsed = Number(value);
  return [10, 20, 50, 100].includes(parsed) ? parsed : DEFAULT_HISTORY_PAGE_SIZE;
}

function normalizeTab(value) {
  const tab = String(value || "").trim();
  return TAB_VALUES.has(tab) ? tab : "pipeline";
}

async function persistPagePrefs(updates, successMessage = "") {
  const result = await callApi("保存页面偏好", () => bridge.apiPost("save_page_prefs", updates));
  if (result?.success === false) return false;
  const prefs = result?.prefs || {};
  state.pagePrefs = {
    ...state.pagePrefs,
    ...prefs,
    theme: Object.prototype.hasOwnProperty.call(prefs, "theme")
      && THEME_VALUES.has(String(prefs.theme || "").trim().toLowerCase())
      ? String(prefs.theme).trim().toLowerCase()
      : state.pagePrefs.theme,
    cache_page_size: Object.prototype.hasOwnProperty.call(prefs, "cache_page_size")
      ? normalizeCachePageSize(prefs.cache_page_size)
      : state.pagePrefs.cache_page_size,
    history_page_size: Object.prototype.hasOwnProperty.call(prefs, "history_page_size")
      ? normalizeHistoryPageSize(prefs.history_page_size)
      : state.pagePrefs.history_page_size,
    last_tab: Object.prototype.hasOwnProperty.call(prefs, "last_tab")
      ? normalizeTab(prefs.last_tab)
      : state.pagePrefs.last_tab,
  };
  state.cachePageSize = state.pagePrefs.cache_page_size;
  state.historyPageSize = state.pagePrefs.history_page_size;
  if (successMessage) showToast(successMessage);
  return true;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function attrHtml(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value ?? null));
}

function splitLines(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }
  return String(value || "")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatBytes(size) {
  let value = Math.max(0, Number(size) || 0);
  const units = ["B", "KB", "MB", "GB"];
  for (const unit of units) {
    if (value < 1024 || unit === "GB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${value.toFixed(1)} GB`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function modeLabel(mode) {
  return ({
    text2img: "文生图",
    image2img: "图生图",
    template: "模板",
    selfie: "自拍",
    video: "视频",
  }[mode] || mode || "-");
}

function truncate(value, length = 96) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

function bindPreviewImageFailures(root = document) {
  root.querySelectorAll("[data-preview-card] img").forEach((img) => {
    if (img.dataset.previewBound === "true") return;
    img.dataset.previewBound = "true";
    img.addEventListener("error", () => {
      const card = img.closest("[data-preview-card]");
      if (card) card.classList.add("is-broken");
    });
    img.addEventListener("load", () => {
      const card = img.closest("[data-preview-card]");
      if (card) card.classList.remove("is-broken");
    });
    if (img.complete && img.naturalWidth === 0) {
      const card = img.closest("[data-preview-card]");
      if (card) card.classList.add("is-broken");
    }
  });
}

let previewQueueActive = 0;
const previewQueue = [];
let historyRequestSerial = 0;
let cacheRequestSerial = 0;
let historyFilterTimer = 0;

function runQueuedPreview(task) {
  return new Promise((resolve, reject) => {
    previewQueue.push({ task, resolve, reject });
    drainPreviewQueue();
  });
}

function drainPreviewQueue() {
  while (previewQueueActive < PREVIEW_CONCURRENCY && previewQueue.length) {
    const job = previewQueue.shift();
    previewQueueActive += 1;
    Promise.resolve()
      .then(job.task)
      .then(job.resolve, job.reject)
      .finally(() => {
        previewQueueActive -= 1;
        drainPreviewQueue();
      });
  }
}

function imageDataParams(item, { thumbnail = false } = {}) {
  if (!item) return null;
  if (thumbnail && item.thumb_data_url) return null;
  if (!thumbnail && item.data_url) return null;
  if (item.preview_kind === "persona" && item.path) return { persona_path: item.path, thumbnail: thumbnail ? 1 : 0 };
  if (item.path) return { persona_path: item.path, thumbnail: thumbnail ? 1 : 0 };
  if (item.id) return { cache_id: item.id, thumbnail: thumbnail ? 1 : 0 };
  return null;
}

async function ensureImageDataUrl(item, { thumbnail = false } = {}) {
  if (!item) return "";
  if (thumbnail && item.thumb_data_url) return item.thumb_data_url;
  if (!thumbnail && item.data_url) return item.data_url;
  const params = imageDataParams(item, { thumbnail });
  if (!params) return thumbnail ? "" : item.url || "";
  try {
    const result = await promiseWithTimeout(
      bridge.apiGet("get_image_data", params),
      "加载图片预览",
      thumbnail ? PREVIEW_API_TIMEOUT_MS : FULL_IMAGE_TIMEOUT_MS,
    );
    if (result?.success !== false && result?.data_url) {
      if (thumbnail) item.thumb_data_url = result.data_url;
      else item.data_url = result.data_url;
      return result.data_url;
    }
  } catch (error) {
    console.warn("[FreeImage Pages] image preview load failed:", error);
  }
  return thumbnail ? "" : item.url || "";
}

function resolvePreviewItem(card) {
  const kind = card.dataset.previewKind;
  if (kind === "cache") {
    const cacheId = card.dataset.cacheId || "";
    return (state.cache.images || []).find((item) => String(item.id || "") === cacheId) || null;
  }
  if (kind === "persona") {
    const persona = activePersona();
    const index = Number(card.dataset.index);
    return persona?.ref_image_items?.[index] || null;
  }
  return null;
}

function loadPreviewImages(root = document) {
  bindPreviewImageFailures(root);
  root.querySelectorAll("[data-preview-card]").forEach((card) => {
    if (card.dataset.previewLoading === "true") return;
    const img = card.querySelector("img");
    const item = resolvePreviewItem(card);
    if (!img || !item) return;
    card.dataset.previewLoading = "true";
    void runQueuedPreview(() => ensureImageDataUrl(item, { thumbnail: true })).then((src) => {
      if (src) {
        img.src = src;
        card.classList.remove("is-broken");
      } else {
        card.classList.add("is-broken");
      }
    }).finally(() => {
      card.dataset.previewLoading = "false";
    });
  });
}

function showToast(message, type = "success") {
  const host = byId("toast-host");
  if (!host) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type === "error" ? "error" : ""}`;
  toast.textContent = message;
  host.appendChild(toast);
  setTimeout(() => toast.remove(), 3200);
}

function setPageStatus(message = "", type = "info") {
  const status = byId("page-status");
  if (!status) return;
  if (!message) {
    status.textContent = "";
    status.className = "page-status hidden";
    return;
  }
  status.textContent = message;
  status.className = `page-status ${type}`;
}

function promiseWithTimeout(promise, label, timeoutMs = PAGE_API_TIMEOUT_MS) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      window.setTimeout(() => reject(new Error(`${label}超时`)), timeoutMs);
    }),
  ]);
}

async function callApi(action, fn, successMessage) {
  try {
    const result = await promiseWithTimeout(Promise.resolve().then(fn), action);
    if (result?.success === false) {
      showToast(result.message || `${action}失败`, "error");
      return result;
    }
    if (successMessage) showToast(successMessage);
    return result || {};
  } catch (error) {
    console.error(error);
    showToast(`${action}失败：${error?.message || error}`, "error");
    return { success: false, error };
  }
}

function setBusy(button, busy, text) {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = text || "处理中...";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

let pendingConfirmResolve = null;

function closeConfirmModal(confirmed) {
  const modal = byId("confirm-modal");
  modal?.classList.remove("active");
  modal?.setAttribute("aria-hidden", "true");
  const resolve = pendingConfirmResolve;
  pendingConfirmResolve = null;
  if (resolve) resolve(Boolean(confirmed));
}

function askConfirm({
  title = "确认操作",
  message = "确定继续吗？",
  confirmText = "确认",
} = {}) {
  const modal = byId("confirm-modal");
  const titleEl = byId("confirm-title");
  const messageEl = byId("confirm-message");
  const okButton = byId("confirm-ok");
  if (!modal || !titleEl || !messageEl || !okButton) {
    return Promise.resolve(false);
  }
  if (pendingConfirmResolve) closeConfirmModal(false);
  titleEl.textContent = title;
  messageEl.textContent = message;
  okButton.textContent = confirmText;
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  okButton.focus();
  return new Promise((resolve) => {
    pendingConfirmResolve = resolve;
  });
}

function getPipelineTemplates() {
  return state.schema?.api_pipeline?.templates || {};
}

function inferCompatField(key, value) {
  if (typeof value === "boolean") {
    return { description: `${key}（兼容字段）`, type: "bool", hint: "旧配置中的附加字段。" };
  }
  if (Array.isArray(value)) {
    return { description: `${key}（兼容字段）`, type: "list", hint: "旧配置中的附加字段。" };
  }
  if (value && typeof value === "object") {
    return { description: `${key}（兼容字段）`, type: "object", hint: "旧配置中的附加字段。" };
  }
  if (typeof value === "number") {
    return {
      description: `${key}（兼容字段）`,
      type: Number.isInteger(value) ? "int" : "float",
      hint: "旧配置中的附加字段。",
    };
  }
  return { description: `${key}（兼容字段）`, type: "string", hint: "旧配置中的附加字段。" };
}

function getTemplateMeta(templateKey) {
  return getPipelineTemplates()[templateKey] || { name: templateKey || "未知提供商", items: {} };
}

function defaultValueForField(field) {
  if (field && Object.prototype.hasOwnProperty.call(field, "default")) {
    return deepClone(field.default);
  }
  if (field?.type === "bool") return false;
  if (field?.type === "list") return [];
  if (field?.type === "int" || field?.type === "float") return 0;
  if (field?.type === "object") return {};
  return "";
}

function createPipelineNode(templateKey) {
  const meta = getTemplateMeta(templateKey);
  const node = { __template_key: templateKey };
  Object.entries(meta.items || {}).forEach(([key, field]) => {
    node[key] = defaultValueForField(field);
  });
  return node;
}

function fieldControlHtml(section, index, key, field, value) {
  const type = field?.type || "string";
  const baseAttrs = `data-bind="${section}" data-index="${index}" data-key="${escapeHtml(key)}" data-type="${escapeHtml(type)}"`;
  if (field?.options?.length) {
    const options = [...field.options];
    if (value && !options.includes(value)) options.unshift(value);
    return `<select ${baseAttrs}>${options.map((option) => (
      `<option value="${escapeHtml(option)}" ${String(option) === String(value) ? "selected" : ""}>${escapeHtml(option)}</option>`
    )).join("")}</select>`;
  }
  if (type === "bool") {
    return `<label class="inline-check"><input type="checkbox" ${baseAttrs} ${value ? "checked" : ""} /><span>${escapeHtml(field?.description || key)}</span></label>`;
  }
  if (type === "text") {
    return `<textarea ${baseAttrs} rows="4">${escapeHtml(value ?? "")}</textarea>`;
  }
  if (type === "list") {
    return `<textarea ${baseAttrs} rows="4" placeholder="每行一个值">${escapeHtml((Array.isArray(value) ? value : splitLines(value)).join("\n"))}</textarea>`;
  }
  if (type === "object") {
    return `<textarea ${baseAttrs} rows="5" placeholder="JSON 对象">${escapeHtml(JSON.stringify(value ?? {}, null, 2))}</textarea>`;
  }
  if (type === "int" || type === "float") {
    const step = type === "float" ? "0.1" : "1";
    return `<input ${baseAttrs} type="number" step="${step}" value="${escapeHtml(value ?? "")}" />`;
  }
  return `<input ${baseAttrs} type="text" value="${escapeHtml(value ?? "")}" />`;
}

function fieldHtml(section, index, key, field, value) {
  const type = field?.type || "string";
  const label = field?.description || key;
  const wide = type === "text" || type === "list" || type === "object" ? " field-wide" : "";
  if (type === "bool") {
    return `<div class="form-field${wide}">${fieldControlHtml(section, index, key, field, value)}${field?.hint ? `<small>${escapeHtml(field.hint)}</small>` : ""}</div>`;
  }
  return `
    <label class="form-field${wide}">
      <span>${escapeHtml(label)}</span>
      ${fieldControlHtml(section, index, key, field, value)}
      ${field?.hint ? `<small>${escapeHtml(field.hint)}</small>` : ""}
    </label>
  `;
}

function updateBoundValue(input) {
  const section = input.dataset.bind;
  const index = Number(input.dataset.index);
  const key = input.dataset.key;
  const type = input.dataset.type;
  let value = input.value;
  if (type === "bool") value = input.checked;
  if (type === "list") value = splitLines(input.value);
  if (type === "int") value = input.value.trim() === "" ? 0 : parseInt(input.value, 10) || 0;
  if (type === "float") value = input.value.trim() === "" ? 0 : parseFloat(input.value) || 0;
  if (type === "object") {
    try {
      value = input.value.trim() ? JSON.parse(input.value) : {};
    } catch {
      input.classList.add("input-error");
      return;
    }
    input.classList.remove("input-error");
  }

  if (section === "pipeline" && state.pipeline[index]) state.pipeline[index][key] = value;
  if (section === "style" && state.selfie.styles[index]) {
    state.selfie.styles[index][key] = value;
    if (["id", "name", "keywords", "enabled"].includes(key)) renderStyleList();
  }
}

function moveItem(list, from, to, expandedSet = null) {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return;
  const [item] = list.splice(from, 1);
  list.splice(to, 0, item);
  if (!expandedSet) return;
  const next = new Set();
  expandedSet.forEach((index) => {
    let mapped = index;
    if (index === from) mapped = to;
    else if (from < to && index > from && index <= to) mapped = index - 1;
    else if (to < from && index >= to && index < from) mapped = index + 1;
    next.add(mapped);
  });
  expandedSet.clear();
  next.forEach((index) => expandedSet.add(index));
}

let dragState = null;

function sortablePositions(container) {
  const positions = new Map();
  container?.querySelectorAll("[data-sort-id]").forEach((item) => {
    positions.set(item.dataset.sortId, item.getBoundingClientRect());
  });
  return positions;
}

function playReorderAnimation(container, before) {
  if (!container || !before?.size) return;
  container.querySelectorAll("[data-sort-id]").forEach((item) => {
    const first = before.get(item.dataset.sortId);
    if (!first) return;
    const last = item.getBoundingClientRect();
    const deltaY = first.top - last.top;
    if (!deltaY) return;
    item.style.transform = `translateY(${deltaY}px)`;
    item.style.transition = "transform 0s";
    requestAnimationFrame(() => {
      item.style.transform = "";
      item.style.transition = "";
    });
  });
}

function reorderWithAnimation(container, mutate, render) {
  const before = sortablePositions(container);
  mutate();
  render();
  requestAnimationFrame(() => playReorderAnimation(container, before));
}

function clearDropMarkers(container) {
  container?.querySelectorAll(".drop-before, .drop-after").forEach((item) => {
    item.classList.remove("drop-before", "drop-after");
  });
}

function bindSortable(container, type) {
  if (!container) return;
  container.querySelectorAll("[data-sort-id]").forEach((item) => {
    const handle = item.querySelector("[data-drag-handle]");
    if (handle) {
      handle.draggable = true;
      handle.addEventListener("dragstart", (event) => {
        dragState = { type, index: Number(item.dataset.index) };
        item.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(item.dataset.index));
      });
      handle.addEventListener("dragend", () => {
        item.classList.remove("dragging");
        clearDropMarkers(container);
        dragState = null;
      });
    }
    item.addEventListener("dragover", (event) => {
      if (!dragState || dragState.type !== type) return;
      event.preventDefault();
      clearDropMarkers(container);
      const targetIndex = Number(item.dataset.index);
      if (targetIndex === dragState.index) return;
      item.classList.add(targetIndex > dragState.index ? "drop-after" : "drop-before");
    });
    item.addEventListener("dragleave", () => {
      item.classList.remove("drop-before", "drop-after");
    });
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!dragState || dragState.type !== type) return;
      const to = Number(item.dataset.index);
      clearDropMarkers(container);
      if (type === "pipeline") {
        reorderWithAnimation(container, () => {
          moveItem(state.pipeline, dragState.index, to, state.expandedPipeline);
        }, renderPipeline);
      }
      if (type === "template") {
        reorderWithAnimation(container, () => {
          moveItem(state.promptTemplates, dragState.index, to, state.expandedTemplates);
        }, renderPromptTemplates);
      }
    });
  });
}

function renderPipeline() {
  const list = byId("pipeline-list");
  if (!list) return;
  if (!state.pipeline.length) {
    list.innerHTML = `<div class="empty">当前管线为空，点击“添加节点”选择一个生图提供商。</div>`;
    return;
  }
  list.innerHTML = state.pipeline.map((node, index) => {
    const key = node.__template_key || "";
    const meta = getTemplateMeta(key);
    const expanded = state.expandedPipeline.has(index);
    const model = node.model || key;
    const status = node.enabled === false ? "已关闭" : "启用中";
    const knownFields = meta.items || {};
    const compatFields = Object.fromEntries(
      Object.entries(node)
        .filter(([fieldKey]) => fieldKey !== "__template_key" && fieldKey !== "enabled" && !(fieldKey in knownFields))
        .map(([fieldKey, value]) => [fieldKey, inferCompatField(fieldKey, value)]),
    );
    const mergedFields = Object.fromEntries(
      Object.entries({ ...knownFields, ...compatFields })
        .filter(([fieldKey]) => fieldKey !== "enabled"),
    );
    const fields = Object.entries(mergedFields)
      .map(([fieldKey, field]) => fieldHtml("pipeline", index, fieldKey, field, node[fieldKey] ?? defaultValueForField(field)))
      .join("");
    return `
      <article class="node-card ${expanded ? "expanded" : ""}" data-index="${index}" data-sort-id="${sortIdForObject("pipeline", node)}">
        <div class="node-head">
          <div class="node-title" data-drag-handle>
            <span class="node-index">${index + 1}</span>
            <div class="title-text">
              <strong>${escapeHtml(meta.name || key)}</strong>
              <small>${escapeHtml(model)} · ${escapeHtml(status)}</small>
            </div>
          </div>
          <div class="node-actions">
            <button class="icon-btn" data-action="pipeline-up" data-index="${index}" type="button" title="上移">↑</button>
            <button class="icon-btn" data-action="pipeline-down" data-index="${index}" type="button" title="下移">↓</button>
            <button class="btn" data-action="pipeline-toggle" data-index="${index}" type="button">${expanded ? "收起" : "展开"}</button>
            <label class="switch-row node-enable-switch" title="启用此节点">
              <input data-bind="pipeline" data-index="${index}" data-key="enabled" data-type="bool" type="checkbox" ${node.enabled === false ? "" : "checked"} />
              <span class="switch-ui"></span>
            </label>
          </div>
        </div>
        <div class="node-body">
          <div class="provider-note">${escapeHtml(meta.hint || "")}</div>
          <div class="form-grid">${fields}</div>
          <div class="danger-zone">
            <button class="btn danger" data-action="pipeline-delete" data-index="${index}" type="button">删除此节点</button>
          </div>
        </div>
      </article>
    `;
  }).join("");
  bindSortable(list, "pipeline");
}

function openProviderModal() {
  const modal = byId("provider-modal");
  const grid = byId("provider-options");
  const templates = getPipelineTemplates();
  if (!modal || !grid) return;
  grid.innerHTML = Object.entries(templates).map(([key, meta]) => `
    <button class="provider-option" data-provider-key="${escapeHtml(key)}" type="button">
      <strong>${escapeHtml(meta.name || key)}</strong>
      <small>${escapeHtml(meta.hint || key)}</small>
    </button>
  `).join("");
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
}

function closeProviderModal() {
  const modal = byId("provider-modal");
  modal?.classList.remove("active");
  modal?.setAttribute("aria-hidden", "true");
}

async function savePipeline(button) {
  setBusy(button, true, "保存中...");
  const result = await callApi("保存管线", () => bridge.apiPost("save_pipeline", { pipeline: state.pipeline }));
  setBusy(button, false);
  if (result?.success !== false) showToast("管线已保存，运行时已刷新。");
}

function renderPromptTemplates() {
  const list = byId("template-list");
  if (!list) return;
  if (!state.promptTemplates.length) {
    list.innerHTML = `<div class="empty">当前没有图生图模板。</div>`;
    return;
  }
  list.innerHTML = state.promptTemplates.map((item, index) => {
    const expanded = state.expandedTemplates.has(index);
    return `
      <article class="template-card ${expanded ? "expanded" : ""}" data-index="${index}" data-sort-id="${sortIdForObject("template", item)}">
        <div class="template-head">
          <div class="template-title" data-drag-handle>
            <span class="node-index">${index + 1}</span>
            <div class="title-text">
              <strong>${escapeHtml(item.trigger || "未命名模板")}</strong>
              <small>${escapeHtml(truncate(item.prompt, 120))}</small>
            </div>
          </div>
          <div class="row-actions">
            <button class="icon-btn" data-action="template-up" data-index="${index}" type="button" title="上移">↑</button>
            <button class="icon-btn" data-action="template-down" data-index="${index}" type="button" title="下移">↓</button>
            <button class="btn" data-action="template-toggle" data-index="${index}" type="button">${expanded ? "收起" : "展开"}</button>
          </div>
        </div>
        <div class="template-body">
          <div class="form-grid">
            <label class="form-field">
              <span>触发词</span>
              <input type="text" data-template-field="trigger" data-index="${index}" value="${escapeHtml(item.trigger || "")}" />
            </label>
            <label class="form-field field-wide">
              <span>完整提示词</span>
              <textarea rows="6" data-template-field="prompt" data-index="${index}">${escapeHtml(item.prompt || "")}</textarea>
            </label>
          </div>
          <div class="danger-zone">
            <button class="btn danger" data-action="template-delete" data-index="${index}" type="button">删除此模板</button>
          </div>
        </div>
      </article>
    `;
  }).join("");
  bindSortable(list, "template");
}

async function savePromptTemplates(button) {
  setBusy(button, true, "保存中...");
  const templates = state.promptTemplates
    .map((item) => ({ trigger: String(item.trigger || "").trim(), prompt: String(item.prompt || "").trim() }))
    .filter((item) => item.trigger && item.prompt);
  const result = await callApi("保存模板", () => bridge.apiPost("save_templates", { templates }));
  setBusy(button, false);
  if (result?.success !== false) {
    state.promptTemplates = templates;
    renderPromptTemplates();
    showToast("模板已保存，命令映射已刷新。");
  }
}

function getRecordDate(record) {
  return String(record.created_at || record.time || "");
}

function updateHistoryFilters(records) {
  const modeSelect = byId("filter-mode");
  const modelSelect = byId("filter-model");
  const currentMode = modeSelect?.value || "";
  const currentModel = modelSelect?.value || "";
  const modes = state.historyFacets.modes?.length
    ? state.historyFacets.modes
    : [...new Set(records.map((item) => item.mode).filter(Boolean))];
  const models = state.historyFacets.models?.length
    ? state.historyFacets.models
    : [...new Set(records.map((item) => item.model).filter(Boolean))];
  if (modeSelect) {
    modeSelect.innerHTML = `<option value="">全部模式</option>${modes.map((mode) => `<option value="${escapeHtml(mode)}">${escapeHtml(modeLabel(mode))}</option>`).join("")}`;
    modeSelect.value = modes.includes(currentMode) ? currentMode : "";
  }
  if (modelSelect) {
    modelSelect.innerHTML = `<option value="">全部模型</option>${models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")}`;
    modelSelect.value = models.includes(currentModel) ? currentModel : "";
  }
}

function applyHistoryFilters() {
  state.historyPage = 1;
  void loadHistory();
}

function scheduleHistoryFilter() {
  window.clearTimeout(historyFilterTimer);
  historyFilterTimer = window.setTimeout(applyHistoryFilters, 350);
}

function groupCounts(records, key) {
  const counts = new Map();
  records.forEach((record) => {
    const value = record[key] || "未记录";
    counts.set(value, (counts.get(value) || 0) + 1);
  });
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
}

function renderBars(containerId, groups, labelFormatter = (value) => value) {
  const container = byId(containerId);
  if (!container) return;
  if (!groups.length) {
    container.innerHTML = `<div class="empty compact">暂无数据</div>`;
    return;
  }
  const max = Math.max(...groups.map(([, count]) => count), 1);
  container.innerHTML = groups.map(([label, count]) => `
    <div class="bar-row">
      <span title="${escapeHtml(labelFormatter(label))}">${escapeHtml(labelFormatter(label))}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(4, (count / max) * 100)}%"></div></div>
      <strong>${count}</strong>
    </div>
  `).join("");
}

function slideInfoHtml(item) {
  const prompt = item.prompt ? `<br><span>${escapeHtml(item.prompt)}</span>` : "";
  return `
    <strong>${escapeHtml(modeLabel(item.mode))}</strong>
    · ${escapeHtml(item.model || "未记录模型")}
    · ${escapeHtml(item.elapsed ? `${Number(item.elapsed).toFixed(1)}s` : "-")}
    · ${escapeHtml(formatDate(item.created_at))}
    <br>触发人：${escapeHtml(item.user_id || "-")}
    ${prompt}
  `;
}

function historySlideItems(records) {
  const items = [];
  records.forEach((record) => {
    (record.cache_items || []).forEach((image) => {
      items.push({
        ...image,
        created_at: record.created_at,
        user_id: record.user_id,
        group_id: record.group_id,
        mode: record.mode,
        prompt: record.prompt,
        elapsed: record.elapsed,
        model: record.model,
        display_name: record.display_name,
      });
    });
  });
  return items;
}

function renderMetrics(records) {
  if (state.historyStats) {
    const stats = state.historyStats;
    if (byId("metric-total")) byId("metric-total").textContent = stats.total || 0;
    if (byId("metric-today")) byId("metric-today").textContent = stats.today || 0;
    if (byId("metric-avg")) byId("metric-avg").textContent = `${Number(stats.avg_elapsed || 0).toFixed(1)}s`;
    if (byId("metric-users")) byId("metric-users").textContent = stats.users || 0;
    renderBars("mode-bars", stats.mode_counts || [], modeLabel);
    renderBars("model-bars", stats.model_counts || []);
    return;
  }
  const total = records.length;
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = records.filter((item) => getRecordDate(item).slice(0, 10) === today).length;
  const avg = total ? records.reduce((sum, item) => sum + (Number(item.elapsed) || 0), 0) / total : 0;
  const users = new Set(records.map((item) => item.user_id).filter(Boolean)).size;
  if (byId("metric-total")) byId("metric-total").textContent = total;
  if (byId("metric-today")) byId("metric-today").textContent = todayCount;
  if (byId("metric-avg")) byId("metric-avg").textContent = `${avg.toFixed(1)}s`;
  if (byId("metric-users")) byId("metric-users").textContent = users;
}

function renderHistory() {
  const records = state.filteredHistory;
  renderMetrics(records);
  if (!state.historyStats) {
    renderBars("mode-bars", groupCounts(records, "mode"), modeLabel);
    renderBars("model-bars", groupCounts(records, "model"));
  }
  const tbody = byId("history-body");
  if (!tbody) return;
  const sizeSelect = byId("history-page-size");
  if (sizeSelect) sizeSelect.value = String(normalizeHistoryPageSize(state.historyPageSize));
  const totalPages = Math.max(1, Number(state.historyTotalPages || 1));
  state.historyPage = Math.min(Math.max(1, state.historyPage), totalPages);
  const pageRecords = records;
  byId("history-page-label").textContent = `${state.historyPage} / ${totalPages}`;
  byId("history-prev").disabled = state.historyPage <= 1;
  byId("history-next").disabled = state.historyPage >= totalPages;
  if (!records.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">暂无生图记录。</td></tr>`;
    return;
  }
  tbody.innerHTML = pageRecords.map((record, offset) => {
    const index = offset;
    const hasImages = Array.isArray(record.cache_items) && record.cache_items.length > 0;
    return `
      <tr>
        <td>${escapeHtml(formatDate(record.created_at))}</td>
        <td><span class="tag">${escapeHtml(record.user_id || "-")}</span></td>
        <td>${escapeHtml(modeLabel(record.mode))}</td>
        <td>${escapeHtml(record.model || "-")}</td>
        <td>${Number(record.elapsed || 0).toFixed(2)}s</td>
        <td class="prompt-cell">
          <details class="prompt-detail">
            <summary>${escapeHtml(truncate(record.prompt, 72))}</summary>
            <pre>${escapeHtml(record.prompt || "")}</pre>
          </details>
        </td>
        <td>
          ${hasImages
            ? `<button class="btn" data-action="history-slide" data-index="${index}" type="button">浏览大图</button>`
            : `<span class="muted-text">图片未保存</span>`}
        </td>
      </tr>
    `;
  }).join("");
}

function historyQueryParams() {
  return {
    page: state.historyPage,
    page_size: state.historyPageSize,
    start: byId("filter-start")?.value || "",
    end: byId("filter-end")?.value || "",
    user: (byId("filter-user")?.value || "").trim(),
    mode: byId("filter-mode")?.value || "",
    model: byId("filter-model")?.value || "",
  };
}

async function loadHistory() {
  setPageStatus("正在加载生图统计...");
  const requestId = ++historyRequestSerial;
  const result = await callApi("加载生图统计", () => bridge.apiGet("get_history", historyQueryParams()));
  if (requestId !== historyRequestSerial) return;
  if (result?.success === false) return;
  state.history = Array.isArray(result.records) ? result.records : [];
  state.filteredHistory = state.history;
  state.historyPage = Number(result.page || state.historyPage || 1);
  state.historyPageSize = normalizeHistoryPageSize(result.page_size || state.historyPageSize);
  state.historyTotal = Number(result.total_count || state.history.length || 0);
  state.historyTotalPages = Number(result.total_pages || 1);
  state.historyStats = result.stats || null;
  state.historyFacets = {
    modes: Array.isArray(result.facets?.modes) ? result.facets.modes : [],
    models: Array.isArray(result.facets?.models) ? result.facets.models : [],
  };
  updateHistoryFilters(state.history);
  renderHistory();
  setPageStatus();
}

function renderCacheSettings() {
  if (byId("cache-enabled")) byId("cache-enabled").checked = Boolean(state.cache.enabled);
  if (byId("cache-max-mb")) byId("cache-max-mb").value = state.cache.max_mb || "";
  if (byId("cache-max-hours")) byId("cache-max-hours").value = state.cache.max_hours || "";
  if (byId("cache-max-count")) byId("cache-max-count").value = state.cache.max_count || "";
  if (byId("cache-total")) byId("cache-total").textContent = state.cache.total_count || state.cache.images.length || 0;
  if (byId("cache-size")) byId("cache-size").textContent = formatBytes(state.cache.total_bytes || 0);
  const sizeSelect = byId("cache-page-size");
  if (sizeSelect) sizeSelect.value = String(normalizeCachePageSize(state.cachePageSize));
}

function renderCacheGrid() {
  renderCacheSettings();
  const grid = byId("cache-grid");
  if (!grid) return;
  const images = state.cache.images || [];
  const totalPages = Math.max(1, Number(state.cacheTotalPages || 1));
  state.cachePage = Math.min(Math.max(1, state.cachePage), totalPages);
  const pageItems = images;
  byId("cache-page-label").textContent = `${state.cachePage} / ${totalPages}`;
  byId("cache-prev").disabled = state.cachePage <= 1;
  byId("cache-next").disabled = state.cachePage >= totalPages;
  if (!pageItems.length) {
    grid.innerHTML = `<div class="empty">当前没有已保存的缓存图片。</div>`;
    return;
  }
  grid.innerHTML = pageItems.map((image, offset) => {
    const displayIndex = (state.cachePage - 1) * state.cachePageSize + offset + 1;
    return `
    <figure class="image-tile" data-preview-card data-preview-kind="cache" data-cache-id="${attrHtml(image.id || "")}" title="${attrHtml(image.prompt || "")}">
      <button class="image-preview" data-action="cache-slide" data-index="${offset}" type="button" aria-label="浏览缓存图片 ${displayIndex}">
        <img alt="缓存图片 ${displayIndex}" loading="lazy" />
        <span class="preview-placeholder">图片未加载成功</span>
        <span class="image-tile-label">${escapeHtml(image.display_name || modeLabel(image.mode))}</span>
      </button>
      <button class="thumb-action" data-action="cache-image-delete" data-cache-id="${attrHtml(image.id || "")}" type="button" title="删除缓存图片" aria-label="删除缓存图片">×</button>
    </figure>
  `;
  }).join("");
  loadPreviewImages(grid);
}

async function loadCache() {
  setPageStatus("正在加载缓存...");
  const requestId = ++cacheRequestSerial;
  const result = await callApi("加载缓存", () => bridge.apiGet("get_cache", {
    page: state.cachePage,
    page_size: state.cachePageSize,
  }));
  if (requestId !== cacheRequestSerial) return;
  if (result?.success === false) return;
  state.cachePage = Number(result.page || state.cachePage || 1);
  state.cachePageSize = normalizeCachePageSize(result.page_size || state.cachePageSize);
  state.cacheTotalPages = Number(result.total_pages || 1);
  state.cache = {
    ...state.cache,
    enabled: Boolean(result.enabled),
    max_mb: result.max_mb || "",
    max_hours: result.max_hours || "",
    max_count: result.max_count || "",
    total_count: Number(result.total_count || 0),
    total_bytes: Number(result.total_bytes || 0),
    images: Array.isArray(result.images) ? result.images : [],
  };
  renderCacheGrid();
  setPageStatus();
}

async function refreshStatisticsView(button) {
  setBusy(button, true, "刷新中...");
  await loadHistory();
  await loadCache();
  setBusy(button, false);
}

async function toggleCache() {
  const enabled = byId("cache-enabled").checked;
  const result = await callApi("切换缓存", () => bridge.apiPost("set_cache_enabled", { enabled }));
  if (result?.success === false) {
    byId("cache-enabled").checked = !enabled;
    return;
  }
  state.cache.enabled = Boolean(result.enabled);
  renderCacheSettings();
  showToast(state.cache.enabled ? "画图缓存已开启。" : "画图缓存已关闭。");
}

async function saveCacheConfig(button) {
  setBusy(button, true, "保存中...");
  const payload = {
    enabled: byId("cache-enabled").checked,
    max_mb: byId("cache-max-mb").value.trim(),
    max_hours: byId("cache-max-hours").value.trim(),
    max_count: byId("cache-max-count").value.trim(),
  };
  const result = await callApi("保存缓存配置", () => bridge.apiPost("save_cache_config", payload));
  setBusy(button, false);
  if (result?.success !== false) {
    showToast("缓存限制已保存。");
    await loadCache();
  }
}

async function clearCache(button) {
  const confirmed = await askConfirm({
    title: "清理全部缓存",
    message: "确定清理全部生图缓存吗？历史记录会保留，但已缓存图片会被删除。",
    confirmText: "清理",
  });
  if (!confirmed) return;
  setBusy(button, true, "清理中...");
  const result = await callApi("清理缓存", () => bridge.apiPost("clear_cache", {}));
  setBusy(button, false);
  if (result?.success !== false) {
    const deleted = result.cleanup?.deleted_count ?? result.deleted_count ?? 0;
    showToast(`已清理 ${deleted} 张缓存图片。`);
    await loadCache();
    await loadHistory();
  }
}

function normalizePersonas(personas) {
  return (Array.isArray(personas) ? personas : []).map((persona, index) => {
    const refs = splitLines(persona.ref_images || []);
    const refItems = Array.isArray(persona.ref_image_items) && persona.ref_image_items.length
      ? persona.ref_image_items
      : refs.map((path) => ({ path, url: `/api/plug/astrbot_plugin_free_image/get_image?persona_path=${encodeURIComponent(path)}`, exists: true }));
    return {
      __template_key: "selfie_persona",
      id: persona.id || `persona_${index + 1}`,
      name: persona.name || persona.id || `人设 ${index + 1}`,
      description: persona.description || "",
      ref_images: refs,
      ref_image_items: refItems,
      bound_sids: splitLines(persona.bound_sids || []),
      bound_astrbot_personas: splitLines(persona.bound_astrbot_personas || []),
    };
  });
}

function normalizeStyles(styles) {
  return (Array.isArray(styles) ? styles : []).map((style, index) => ({
    __template_key: "selfie_style",
    id: style.id || `style_${index + 1}`,
    name: style.name || style.id || `风格 ${index + 1}`,
    prompt: style.prompt || "",
    keywords: splitLines(style.keywords || []),
    enabled: style.enabled !== false,
  }));
}

function activePersona() {
  if (!state.selfie.personas.length) return null;
  state.activePersonaIndex = Math.min(Math.max(0, state.activePersonaIndex), state.selfie.personas.length - 1);
  return state.selfie.personas[state.activePersonaIndex];
}

function activeStyle() {
  if (!state.selfie.styles.length) return null;
  state.activeStyleIndex = Math.min(Math.max(0, state.activeStyleIndex), state.selfie.styles.length - 1);
  return state.selfie.styles[state.activeStyleIndex];
}

function renderPersonaList() {
  const list = byId("persona-list");
  if (!list) return;
  if (!state.selfie.personas.length) {
    list.innerHTML = `<div class="empty compact">暂无人设。</div>`;
    return;
  }
  list.innerHTML = state.selfie.personas.map((persona, index) => `
    <button class="persona-button ${index === state.activePersonaIndex ? "active" : ""}" data-action="persona-switch" data-index="${index}" type="button">
      <strong>${escapeHtml(persona.name || persona.id)}</strong>
      <small>${escapeHtml(persona.id || "")} · ${persona.ref_images.length} 张</small>
    </button>
  `).join("");
}

function renderPersonaEditor() {
  const editor = byId("persona-editor");
  if (!editor) return;
  const persona = activePersona();
  if (!persona) {
    editor.innerHTML = `<div class="empty">还没有自拍人设，点击“添加人设”创建第一套参考图。</div>`;
    renderPersonaList();
    return;
  }
  editor.innerHTML = `
    <div class="form-grid">
      <label class="form-field">
        <span>绑定模式</span>
        <select id="selfie-binding-mode">
          ${["优先 AstrBot persona", "优先会话 SID", "只使用手动指定的selfie人设"].map((item) => (
            `<option value="${escapeHtml(item)}" ${item === state.selfie.binding_mode ? "selected" : ""}>${escapeHtml(item)}</option>`
          )).join("")}
        </select>
      </label>
      <label class="form-field">
        <span>手动指定人设 ID</span>
        <input id="selfie-manual-override" type="text" value="${escapeHtml(state.selfie.manual_override || "")}" />
      </label>
      <label class="form-field">
        <span>全局默认人设 ID</span>
        <input id="selfie-default-persona-id" type="text" value="${escapeHtml(state.selfie.default_persona_id || "")}" />
      </label>
      <label class="form-field">
        <span>人设 ID</span>
        <input data-persona-field="id" type="text" value="${escapeHtml(persona.id || "")}" />
      </label>
      <label class="form-field">
        <span>名称</span>
        <input data-persona-field="name" type="text" value="${escapeHtml(persona.name || "")}" />
      </label>
      <label class="form-field field-wide">
        <span>人设文字描述</span>
        <textarea data-persona-field="description" rows="4">${escapeHtml(persona.description || "")}</textarea>
      </label>
      <label class="form-field field-wide">
        <span>绑定会话 SID</span>
        <textarea data-persona-field="bound_sids" data-persona-type="list" rows="3" placeholder="每行一个会话 SID">${escapeHtml((persona.bound_sids || []).join("\n"))}</textarea>
      </label>
      <label class="form-field field-wide">
        <span>绑定 AstrBot Persona</span>
        <textarea data-persona-field="bound_astrbot_personas" data-persona-type="list" rows="3" placeholder="每行一个 Persona 名称">${escapeHtml((persona.bound_astrbot_personas || []).join("\n"))}</textarea>
      </label>
    </div>
    <div class="persona-editor-actions">
      <button class="btn danger" data-action="persona-delete" type="button">删除当前人设</button>
    </div>
    <div class="upload-zone" id="persona-upload-zone" tabindex="0">
      <span>拖拽参考图到这里，或点击上传</span>
      <input id="persona-upload-input" type="file" accept="image/*" multiple hidden />
    </div>
    <div class="persona-images">
      ${(persona.ref_image_items || []).length
        ? persona.ref_image_items.map((image, index) => `
          <figure class="persona-image" data-preview-card data-preview-kind="persona" data-index="${index}">
            ${image.exists === false
              ? `<div class="missing-thumb">文件不存在</div>`
              : `
                <button class="persona-preview" data-action="persona-image-preview" data-index="${index}" type="button" title="${attrHtml(image.path || `参考图 ${index + 1}`)}">
                  <img alt="参考图 ${index + 1}" loading="lazy" />
                  <span class="preview-placeholder">图片未加载成功</span>
                </button>
              `}
            <button class="thumb-action" data-action="persona-image-delete" data-index="${index}" type="button" title="删除参考图" aria-label="删除参考图">×</button>
          </figure>
        `).join("")
        : `<div class="empty compact">暂无参考图。上传的图片会保存到插件 data/selfie_personas/pages_uploads，不属于缓存。</div>`}
    </div>
  `;
  loadPreviewImages(editor);
  renderPersonaList();
}

function updateSelfieControlsFromEditor() {
  state.selfie.binding_mode = byId("selfie-binding-mode")?.value || state.selfie.binding_mode;
  state.selfie.manual_override = byId("selfie-manual-override")?.value || "";
  state.selfie.default_persona_id = byId("selfie-default-persona-id")?.value || "";
}

async function savePersonas(button, showMessage = true) {
  updateSelfieControlsFromEditor();
  setBusy(button, true, "保存中...");
  const payload = {
    binding_mode: state.selfie.binding_mode,
    manual_override: state.selfie.manual_override,
    default_persona_id: state.selfie.default_persona_id,
    personas: state.selfie.personas.map((persona) => ({
      __template_key: "selfie_persona",
      id: persona.id,
      name: persona.name,
      description: persona.description,
      ref_images: persona.ref_images,
      bound_sids: persona.bound_sids || [],
      bound_astrbot_personas: persona.bound_astrbot_personas || [],
    })),
  };
  const result = await callApi("保存自拍人设", () => bridge.apiPost("save_personas", payload));
  setBusy(button, false);
  if (result?.success !== false && showMessage) showToast("自拍人设已保存。");
  return result;
}

async function uploadPersonaFiles(files) {
  const persona = activePersona();
  if (!persona) {
    showToast("请先创建一个自拍人设。", "error");
    return;
  }
  const imageFiles = Array.from(files || []).filter((file) => file.type.startsWith("image/"));
  if (!imageFiles.length) {
    showToast("请选择图片文件。", "error");
    return;
  }
  let count = 0;
  for (const file of imageFiles) {
    const result = await callApi("上传参考图", () => bridge.upload("upload_persona_image", file));
    if (result?.success) {
      persona.ref_images.push(result.path);
      persona.ref_image_items.push({ path: result.path, url: result.url, exists: true });
      count += 1;
    }
  }
  if (count) {
    await savePersonas(null, false);
    renderPersonaEditor();
    showToast(`已上传并写入 ${count} 张参考图。`);
  }
}

async function deletePersonaImage(index) {
  const persona = activePersona();
  const image = persona?.ref_image_items?.[index];
  if (!persona || !image) return;
  const confirmed = await askConfirm({
    title: "删除自拍参考图",
    message: "确定删除这张自拍参考图吗？这是高价值资料，删除后不会进入缓存回收站。",
    confirmText: "删除",
  });
  if (!confirmed) return;
  const result = await callApi("删除参考图", () => bridge.apiPost("delete_persona_image", {
    persona_id: persona.id,
    path: image.path,
  }));
  if (result?.success === false) return;
  if (result?.removed === false) {
    showToast("后端没有找到这张参考图，请先保存人设后再试。", "error");
    return;
  }
  persona.ref_images = persona.ref_images.filter((path) => path !== image.path);
  persona.ref_image_items.splice(index, 1);
  renderPersonaEditor();
  showToast("参考图已删除。");
}

function renderStyleControls() {
  const mode = byId("style-mode");
  const selected = byId("style-selected-id");
  if (mode) {
    mode.innerHTML = ["不注入", "自动", "指定"].map((item) => `<option value="${item}" ${item === state.selfie.style_mode ? "selected" : ""}>${item}</option>`).join("");
  }
  if (selected) selected.value = state.selfie.selected_style_id || "";
}

function renderStyles() {
  renderStyleControls();
  renderStyleList();
  renderStyleEditor();
}

function renderStyleList() {
  const list = byId("style-list");
  if (!list) return;
  if (!state.selfie.styles.length) {
    list.innerHTML = `<div class="empty">当前没有自拍风格模板。</div>`;
    return;
  }
  list.innerHTML = state.selfie.styles.map((style, index) => `
    <button class="persona-button ${index === state.activeStyleIndex ? "active" : ""}" data-action="style-switch" data-index="${index}" type="button">
      <strong>${escapeHtml(style.name || style.id)}</strong>
      <small>${escapeHtml(style.id || "")} · ${style.enabled === false ? "已关闭" : "启用中"}</small>
    </button>
  `).join("");
}

function renderStyleEditor() {
  const editor = byId("style-editor");
  if (!editor) return;
  const style = activeStyle();
  if (!style) {
    editor.innerHTML = `<div class="empty">还没有自拍风格，点击“添加风格”创建第一套模板。</div>`;
    renderStyleList();
    return;
  }
  const selfieSchemaItems = state.schema?.selfie?.items || {};
  const styleSchema = (
    selfieSchemaItems.selfie_styles || state.schema?.selfie_styles || {}
  ).templates?.selfie_style?.items || {};
  const index = state.activeStyleIndex;
  const compatFields = Object.fromEntries(
    Object.entries(style)
      .filter(([fieldKey]) => fieldKey !== "__template_key" && !(fieldKey in styleSchema))
      .map(([fieldKey, value]) => [fieldKey, inferCompatField(fieldKey, value)]),
  );
  const mergedFields = { ...styleSchema, ...compatFields };
  const fields = Object.entries(mergedFields)
    .map(([key, field]) => fieldHtml("style", index, key, field, style[key] ?? defaultValueForField(field)))
    .join("");
  editor.innerHTML = `
    <div class="style-card-head">
      <div>
        <strong>${escapeHtml(style.name || style.id)}</strong>
        <small>${style.enabled === false ? "已关闭" : "启用中"}</small>
      </div>
      <button class="btn danger" data-action="style-delete" data-index="${index}" type="button">删除当前风格</button>
    </div>
    <div class="form-grid">${fields}</div>
  `;
  renderStyleList();
}

async function saveStyles(button) {
  state.selfie.style_mode = byId("style-mode")?.value || "自动";
  state.selfie.selected_style_id = byId("style-selected-id")?.value || "";
  setBusy(button, true, "保存中...");
  const payload = {
    mode: state.selfie.style_mode,
    selected_style_id: state.selfie.selected_style_id,
    styles: state.selfie.styles.map((style) => ({
      __template_key: "selfie_style",
      id: style.id,
      name: style.name,
      prompt: style.prompt,
      keywords: style.keywords || [],
      enabled: style.enabled !== false,
    })),
  };
  const result = await callApi("保存自拍风格", () => bridge.apiPost("save_styles", payload));
  setBusy(button, false);
  if (result?.success !== false) showToast("自拍风格已保存。");
}

function openSlideshow(items, startIndex = 0) {
  if (!items.length) {
    showToast("没有可浏览的图片。", "error");
    return;
  }
  state.slideItems = items;
  state.slideIndex = Math.min(Math.max(0, startIndex), items.length - 1);
  updateSlide();
  const modal = byId("slideshow");
  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  document.addEventListener("keydown", onSlideKeydown);
}

function closeSlideshow() {
  const modal = byId("slideshow");
  modal.classList.remove("active");
  modal.setAttribute("aria-hidden", "true");
  document.removeEventListener("keydown", onSlideKeydown);
}

async function updateSlide() {
  const item = state.slideItems[state.slideIndex];
  if (!item) return;
  const expectedIndex = state.slideIndex;
  const image = byId("slide-img");
  image.removeAttribute("src");
  image.alt = item.display_name || "图片预览";
  byId("slide-info").innerHTML = slideInfoHtml(item);
  byId("slide-count").textContent = `${state.slideIndex + 1} / ${state.slideItems.length}`;
  const src = await ensureImageDataUrl(item);
  if (expectedIndex !== state.slideIndex) return;
  if (src) {
    image.src = src;
  } else {
    showToast("这张图片未加载成功。", "error");
  }
}

async function deleteCacheImage(cacheId) {
  const image = (state.cache.images || []).find((item) => String(item.id || "") === String(cacheId || ""));
  if (!image) return;
  const confirmed = await askConfirm({
    title: "删除缓存图片",
    message: "确定删除这张缓存图片吗？生图统计记录会保留，但这张本地图片会从图库和统计预览中移除。",
    confirmText: "删除",
  });
  if (!confirmed) return;
  const result = await callApi("删除缓存图片", () => bridge.apiPost("delete_cache_image", {
    cache_id: image.id,
  }));
  if (result?.success === false) return;
  await loadCache();
  await loadHistory();
  showToast("缓存图片已删除。");
}

function personaSlideItems(persona) {
  if (!persona?.ref_image_items) return [];
  return persona.ref_image_items
    .filter((image) => image?.exists !== false && image?.url)
    .map((image, index) => ({
      id: `persona-${index + 1}`,
      preview_kind: "persona",
      path: image.path || "",
      url: image.url,
      mode: "自拍",
      model: persona.name || persona.id || "自拍参考图",
      elapsed: "",
      created_at: "",
      user_id: "",
      prompt: image.path || "",
      display_name: persona.name || persona.id || `参考图 ${index + 1}`,
    }));
}

function changeSlide(offset) {
  if (!state.slideItems.length) return;
  state.slideIndex = (state.slideIndex + offset + state.slideItems.length) % state.slideItems.length;
  updateSlide();
}

function onSlideKeydown(event) {
  if (event.key === "Escape") closeSlideshow();
  if (event.key === "ArrowLeft") changeSlide(-1);
  if (event.key === "ArrowRight") changeSlide(1);
}

function renderAllConfig() {
  renderPipeline();
  renderPromptTemplates();
  renderCacheSettings();
  renderPersonaEditor();
  renderStyles();
}

function switchTab(tab, { persist = true } = {}) {
  const nextTab = normalizeTab(tab);
  state.activeTab = nextTab;
  state.pagePrefs.last_tab = nextTab;
  safeStorageSet(LAST_TAB_KEY, nextTab);
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.tab === nextTab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === nextTab);
  });
  if (persist) void persistPagePrefs({ last_tab: nextTab });
}

function applyLocalPagePrefs() {
  state.pagePrefs.last_tab = normalizeTab(safeStorageGet(LAST_TAB_KEY, state.pagePrefs.last_tab));
  state.pagePrefs.cache_page_size = normalizeCachePageSize(
    safeStorageGet(CACHE_PAGE_SIZE_KEY, state.pagePrefs.cache_page_size),
  );
  state.pagePrefs.history_page_size = normalizeHistoryPageSize(
    safeStorageGet(HISTORY_PAGE_SIZE_KEY, state.pagePrefs.history_page_size),
  );
  state.cachePageSize = state.pagePrefs.cache_page_size;
  state.historyPageSize = state.pagePrefs.history_page_size;
}

async function waitForBridgeReady() {
  if (!window.AstrBotPluginPage) {
    setPageStatus("当前未检测到 AstrBot Dashboard bridge，页面以只读空壳模式启动。", "warn");
    return false;
  }
  setPageStatus("正在连接 AstrBot Dashboard...");
  try {
    await promiseWithTimeout(bridge.ready(), "连接 Dashboard");
    setPageStatus();
    return true;
  } catch (error) {
    console.error(error);
    setPageStatus(`Dashboard 连接失败：${error?.message || error}`, "error");
    showToast(`Dashboard 连接失败：${error?.message || error}`, "error");
    return false;
  }
}

async function loadConfigBundle() {
  setPageStatus("正在加载当前插件配置...");
  const result = await callApi("加载配置", () => bridge.apiGet("get_config_bundle"));
  if (result?.success === false) return;
  state.schema = result.schema || {};
  const config = result.config || {};
  state.pipeline = Array.isArray(config.pipeline) ? deepClone(config.pipeline) : [];
  state.promptTemplates = Array.isArray(config.prompt_templates) ? deepClone(config.prompt_templates) : [];
  state.cache = { ...state.cache, ...(config.cache || {}) };
  const pagePrefs = config.page_prefs || {};
  state.pagePrefs = {
    theme: THEME_VALUES.has(String(pagePrefs.theme || "").trim().toLowerCase())
      ? String(pagePrefs.theme).trim().toLowerCase()
      : DEFAULT_THEME,
    cache_page_size: normalizeCachePageSize(pagePrefs.cache_page_size),
    history_page_size: normalizeHistoryPageSize(pagePrefs.history_page_size),
    last_tab: normalizeTab(pagePrefs.last_tab),
  };
  state.cachePageSize = state.pagePrefs.cache_page_size;
  state.historyPageSize = state.pagePrefs.history_page_size;
  applyTheme(state.pagePrefs.theme);
  const selfie = config.selfie || {};
  state.selfie = {
    ...state.selfie,
    binding_mode: selfie.binding_mode || "优先 AstrBot persona",
    manual_override: selfie.manual_override || "",
    default_persona_id: selfie.default_persona_id || "",
    style_mode: selfie.style_mode || "自动",
    selected_style_id: selfie.selected_style_id || "",
    personas: normalizePersonas(selfie.personas || []),
    styles: normalizeStyles(selfie.styles || []),
  };
  state.activePersonaIndex = 0;
  state.activeStyleIndex = 0;
  renderAllConfig();
  switchTab(state.pagePrefs.last_tab, { persist: false });
  setPageStatus();
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      switchTab(button.dataset.tab);
    });
  });

  byId("add-pipeline")?.addEventListener("click", openProviderModal);
  byId("save-pipeline")?.addEventListener("click", (event) => savePipeline(event.currentTarget));
  byId("add-template")?.addEventListener("click", () => {
    const index = state.promptTemplates.length;
    state.promptTemplates.push({ trigger: "", prompt: "" });
    state.expandedTemplates.add(index);
    renderPromptTemplates();
  });
  byId("save-templates")?.addEventListener("click", (event) => savePromptTemplates(event.currentTarget));
  byId("refresh-history")?.addEventListener("click", (event) => refreshStatisticsView(event.currentTarget));
  byId("apply-filters")?.addEventListener("click", applyHistoryFilters);
  byId("cache-enabled")?.addEventListener("change", toggleCache);
  byId("save-cache-config")?.addEventListener("click", (event) => saveCacheConfig(event.currentTarget));
  byId("clear-cache")?.addEventListener("click", (event) => clearCache(event.currentTarget));
  byId("cache-prev")?.addEventListener("click", () => {
    state.cachePage = Math.max(1, state.cachePage - 1);
    void loadCache();
  });
  byId("cache-next")?.addEventListener("click", () => {
    state.cachePage += 1;
    void loadCache();
  });
  byId("cache-page-size")?.addEventListener("change", (event) => {
    state.cachePageSize = normalizeCachePageSize(event.target.value);
    state.pagePrefs.cache_page_size = state.cachePageSize;
    safeStorageSet(CACHE_PAGE_SIZE_KEY, state.cachePageSize);
    state.cachePage = 1;
    void loadCache();
    void persistPagePrefs({ cache_page_size: state.cachePageSize });
  });
  byId("history-prev")?.addEventListener("click", () => {
    state.historyPage = Math.max(1, state.historyPage - 1);
    void loadHistory();
  });
  byId("history-next")?.addEventListener("click", () => {
    state.historyPage += 1;
    void loadHistory();
  });
  byId("history-page-size")?.addEventListener("change", (event) => {
    state.historyPageSize = normalizeHistoryPageSize(event.target.value);
    state.pagePrefs.history_page_size = state.historyPageSize;
    safeStorageSet(HISTORY_PAGE_SIZE_KEY, state.historyPageSize);
    state.historyPage = 1;
    void loadHistory();
    void persistPagePrefs({ history_page_size: state.historyPageSize });
  });
  byId("add-persona")?.addEventListener("click", () => {
    const index = state.selfie.personas.length + 1;
    state.selfie.personas.push({
      __template_key: "selfie_persona",
      id: `persona_${index}`,
      name: `人设 ${index}`,
      description: "",
      ref_images: [],
      ref_image_items: [],
      bound_sids: [],
      bound_astrbot_personas: [],
    });
    state.activePersonaIndex = state.selfie.personas.length - 1;
    renderPersonaEditor();
  });
  byId("save-personas")?.addEventListener("click", (event) => savePersonas(event.currentTarget));
  byId("add-style")?.addEventListener("click", () => {
    const index = state.selfie.styles.length + 1;
    state.selfie.styles.push({
      __template_key: "selfie_style",
      id: `style_${index}`,
      name: `风格 ${index}`,
      prompt: "",
      keywords: [],
      enabled: true,
    });
    state.activeStyleIndex = state.selfie.styles.length - 1;
    renderStyles();
  });
  byId("save-styles")?.addEventListener("click", (event) => saveStyles(event.currentTarget));
  byId("slide-close")?.addEventListener("click", closeSlideshow);
  byId("slide-prev")?.addEventListener("click", () => changeSlide(-1));
  byId("slide-next")?.addEventListener("click", () => changeSlide(1));
  byId("confirm-cancel")?.addEventListener("click", () => closeConfirmModal(false));
  byId("confirm-ok")?.addEventListener("click", () => closeConfirmModal(true));
  byId("confirm-modal")?.addEventListener("click", (event) => {
    if (event.target.id === "confirm-modal") closeConfirmModal(false);
  });
  document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", closeProviderModal));
  byId("provider-modal")?.addEventListener("click", (event) => {
    if (event.target.id === "provider-modal") closeProviderModal();
  });

  document.body.addEventListener("click", async (event) => {
    const themeButton = event.target.closest("[data-theme-choice]");
    if (themeButton) {
      const nextTheme = themeButton.dataset.themeChoice;
      applyTheme(nextTheme);
      void persistPagePrefs({ theme: nextTheme });
      return;
    }

    const provider = event.target.closest("[data-provider-key]");
    if (provider) {
      const index = state.pipeline.length;
      state.pipeline.push(createPipelineNode(provider.dataset.providerKey));
      state.expandedPipeline.add(index);
      closeProviderModal();
      renderPipeline();
      return;
    }

    const actionButton = event.target.closest("[data-action]");
    if (!actionButton) return;
    const action = actionButton.dataset.action;
    const index = Number(actionButton.dataset.index);

    if (action === "pipeline-toggle") {
      state.expandedPipeline.has(index) ? state.expandedPipeline.delete(index) : state.expandedPipeline.add(index);
      renderPipeline();
    }
    if (action === "pipeline-delete") {
      const node = state.pipeline[index];
      const confirmed = await askConfirm({
        title: "删除管线节点",
        message: `确定删除「${getTemplateMeta(node?.__template_key || "").name || node?.__template_key || "这个管线节点"}」吗？`,
        confirmText: "删除",
      });
      if (confirmed) {
        state.pipeline.splice(index, 1);
        state.expandedPipeline.delete(index);
        renderPipeline();
      }
    }
    if (action === "pipeline-up") {
      reorderWithAnimation(byId("pipeline-list"), () => {
        moveItem(state.pipeline, index, index - 1, state.expandedPipeline);
      }, renderPipeline);
    }
    if (action === "pipeline-down") {
      reorderWithAnimation(byId("pipeline-list"), () => {
        moveItem(state.pipeline, index, index + 1, state.expandedPipeline);
      }, renderPipeline);
    }
    if (action === "template-toggle") {
      state.expandedTemplates.has(index) ? state.expandedTemplates.delete(index) : state.expandedTemplates.add(index);
      renderPromptTemplates();
    }
    if (action === "template-delete") {
      const item = state.promptTemplates[index];
      const confirmed = await askConfirm({
        title: "删除提示词模板",
        message: `确定删除「${item?.trigger || "这个提示词模板"}」吗？`,
        confirmText: "删除",
      });
      if (confirmed) {
        state.promptTemplates.splice(index, 1);
        state.expandedTemplates.delete(index);
        renderPromptTemplates();
      }
    }
    if (action === "template-up") {
      reorderWithAnimation(byId("template-list"), () => {
        moveItem(state.promptTemplates, index, index - 1, state.expandedTemplates);
      }, renderPromptTemplates);
    }
    if (action === "template-down") {
      reorderWithAnimation(byId("template-list"), () => {
        moveItem(state.promptTemplates, index, index + 1, state.expandedTemplates);
      }, renderPromptTemplates);
    }
    if (action === "history-slide") {
      const record = state.filteredHistory[index];
      const slides = historySlideItems(state.filteredHistory);
      const firstId = record?.cache_items?.[0]?.id;
      const startIndex = Math.max(0, slides.findIndex((item) => item.id === firstId));
      openSlideshow(slides, startIndex);
    }
    if (action === "cache-slide") {
      openSlideshow(state.cache.images || [], index);
    }
    if (action === "cache-image-delete") {
      await deleteCacheImage(actionButton.dataset.cacheId || "");
    }
    if (action === "persona-switch") {
      updateSelfieControlsFromEditor();
      state.activePersonaIndex = index;
      renderPersonaEditor();
    }
    if (action === "persona-delete") {
      const persona = activePersona();
      const confirmed = persona && await askConfirm({
        title: "删除自拍人设",
        message: `确定删除人设「${persona.name || persona.id}」吗？参考图文件不会自动删除。`,
        confirmText: "删除",
      });
      if (confirmed) {
        state.selfie.personas.splice(state.activePersonaIndex, 1);
        state.activePersonaIndex = Math.max(0, state.activePersonaIndex - 1);
        renderPersonaEditor();
      }
    }
    if (action === "persona-image-delete") {
      deletePersonaImage(index);
    }
    if (action === "persona-image-preview") {
      const persona = activePersona();
      const slides = personaSlideItems(persona);
      const targetPath = persona?.ref_image_items?.[index]?.path;
      const startIndex = Math.max(0, slides.findIndex((item) => item.path === targetPath));
      openSlideshow(slides, startIndex);
    }
    if (action === "style-switch") {
      state.activeStyleIndex = index;
      renderStyles();
    }
    if (action === "style-delete") {
      const style = state.selfie.styles[index];
      const confirmed = await askConfirm({
        title: "删除自拍风格",
        message: `确定删除「${style?.name || style?.id || "这个自拍风格"}」吗？`,
        confirmText: "删除",
      });
      if (confirmed) {
        state.selfie.styles.splice(index, 1);
        state.activeStyleIndex = Math.max(0, Math.min(state.activeStyleIndex, state.selfie.styles.length - 1));
        renderStyles();
      }
    }
  });

  document.body.addEventListener("input", (event) => {
    const bound = event.target.closest("[data-bind]");
    if (bound) updateBoundValue(bound);
    const template = event.target.closest("[data-template-field]");
    if (template && state.promptTemplates[Number(template.dataset.index)]) {
      state.promptTemplates[Number(template.dataset.index)][template.dataset.templateField] = template.value;
    }
    const personaField = event.target.closest("[data-persona-field]");
    if (personaField) {
      const persona = activePersona();
      if (!persona) return;
      const key = personaField.dataset.personaField;
      persona[key] = personaField.dataset.personaType === "list" ? splitLines(personaField.value) : personaField.value;
      if (key === "id" || key === "name") renderPersonaList();
    }
  });

  document.body.addEventListener("change", (event) => {
    const bound = event.target.closest("[data-bind]");
    if (bound) updateBoundValue(bound);
    if (event.target.id === "style-mode") state.selfie.style_mode = event.target.value;
    if (event.target.id === "style-selected-id") state.selfie.selected_style_id = event.target.value;
    if (event.target.id === "persona-upload-input") {
      uploadPersonaFiles(event.target.files);
      event.target.value = "";
    }
    if (["filter-start", "filter-end", "filter-mode", "filter-model"].includes(event.target.id)) applyHistoryFilters();
  });

  byId("filter-user")?.addEventListener("input", scheduleHistoryFilter);

  document.body.addEventListener("click", (event) => {
    if (event.target.closest("#persona-upload-zone")) byId("persona-upload-input")?.click();
  });
  document.body.addEventListener("dragover", (event) => {
    const zone = event.target.closest("#persona-upload-zone");
    if (!zone) return;
    event.preventDefault();
    zone.classList.add("dragover");
  });
  document.body.addEventListener("dragleave", (event) => {
    const zone = event.target.closest("#persona-upload-zone");
    if (zone) zone.classList.remove("dragover");
  });
  document.body.addEventListener("drop", (event) => {
    const zone = event.target.closest("#persona-upload-zone");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("dragover");
    uploadPersonaFiles(event.dataTransfer.files);
  });
}

async function init() {
  bindEvents();
  applyLocalPagePrefs();
  applyTheme(getThemeValue());
  renderAllConfig();
  switchTab(state.pagePrefs.last_tab, { persist: false });
  state.cachePageSize = normalizeCachePageSize(state.cachePageSize);
  state.historyPageSize = normalizeHistoryPageSize(state.historyPageSize);
  const bridgeReady = await waitForBridgeReady();
  if (!bridgeReady) return;
  await loadConfigBundle();
  await loadCache();
  await loadHistory();
}

init().catch((error) => {
  console.error(error);
  showToast(`配置页初始化失败：${error?.message || error}`, "error");
});
