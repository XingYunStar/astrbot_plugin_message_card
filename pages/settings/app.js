const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const inputs = () => Array.from(document.querySelectorAll("[data-path]"));

let busy = false;

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function setBusy(value) {
  busy = value;
  for (const id of ["saveButton", "previewButton"]) {
    const el = $(id);
    if (el) el.disabled = value;
  }
}

function readValues() {
  const values = {};
  for (const el of inputs()) {
    const path = el.dataset.path;
    if (!path) continue;
    if (el.type === "checkbox") values[path] = el.checked;
    else if (el.type === "number") values[path] = el.value === "" ? null : Number(el.value);
    else values[path] = el.value;
  }
  return values;
}

function applyValues(values) {
  for (const el of inputs()) {
    const path = el.dataset.path;
    if (!(path in (values || {}))) continue;
    const value = values[path];
    if (el.type === "checkbox") el.checked = Boolean(value);
    else if (value === null || value === undefined) el.value = "";
    else el.value = value;
  }
}

function applyStatus(status) {
  if (!status) return;
  $("statusText").textContent = `${status.theme_label || status.theme || "主题"} · ${status.now || ""}`;
  const dl = $("statusList");
  const rows = [
    ["主题判定", status.reason || "—"],
    ["时区", status.timezone || "—"],
    ["浏览器", status.browser_path || "未检测到"],
    ["已渲染", `${status.rendered ?? 0} 次`],
    ["失败", `${status.failed ?? 0} 次`],
    ["最近错误", status.last_error || "无"],
  ];
  dl.innerHTML = "";
  for (const [key, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    dl.append(dt, dd);
  }
}

async function loadSettings() {
  const context = await bridge.ready();
  document.title = context?.pageTitle || "消息卡片设置";
  const data = await bridge.apiGet("settings");
  if (!data?.ok) throw new Error(data?.message || "读取配置失败");
  applyValues(data.values || {});
  applyStatus(data.status);
  return data;
}

async function saveSettings() {
  if (busy) return;
  setBusy(true);
  $("saveHint").textContent = "正在保存…";
  try {
    const data = await bridge.apiPost("settings/save", { values: readValues() });
    if (!data?.ok) throw new Error(data?.message || "保存失败");
    applyValues(data.values || {});
    applyStatus(data.status);
    $("saveHint").textContent = "已保存，配置立即生效。";
    toast("已保存");
  } catch (error) {
    $("saveHint").textContent = "保存失败";
    toast(error?.message || "保存失败");
  } finally {
    setBusy(false);
  }
}

async function renderPreview() {
  if (busy) return;
  setBusy(true);
  const image = $("previewImage");
  const placeholder = $("previewPlaceholder");
  placeholder.style.display = "flex";
  placeholder.innerHTML = '<div class="spinner"></div><span>正在渲染，首次启动浏览器可能需要几秒…</span>';
  image.style.display = "none";
  try {
    const data = await bridge.apiPost("preview", { text: $("previewText").value || "" });
    if (!data?.ok || !data.image) throw new Error(data?.message || "预览失败");
    image.src = data.image;
    image.style.display = "block";
    placeholder.style.display = "none";
    toast("预览已更新");
  } catch (error) {
    image.style.display = "none";
    placeholder.style.display = "flex";
    placeholder.innerHTML = `<span>${escapeHtml(error?.message || "预览失败")}</span>`;
    toast(error?.message || "预览失败");
  } finally {
    setBusy(false);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

$("saveButton").addEventListener("click", saveSettings);
$("previewButton").addEventListener("click", renderPreview);

loadSettings()
  .then(() => renderPreview())
  .catch((error) => {
    $("statusText").textContent = "读取配置失败";
    $("saveHint").textContent = error?.message || "读取配置失败";
    toast(error?.message || "读取配置失败");
  });
