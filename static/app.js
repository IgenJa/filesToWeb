const STORAGE_KEY = "optimize-for-web-job";

const h264Crf = { high: 20, balanced: 24, small: 28 };
const vp9Crf = { high: 28, balanced: 32, small: 36 };

let files = [];
let job = null;
let jobId = null;
let pollTimer = null;
let dialogBusy = false;

const notice = document.querySelector("#notice");
const fileList = document.querySelector("#file-list");
const optimizeButton = document.querySelector("#optimize");
const cancelButton = document.querySelector("#cancel");
const openButton = document.querySelector("#open-folder");
const anotherButton = document.querySelector("#another");
const summary = document.querySelector("#summary");
const summaryText = document.querySelector("#summary-text");
const overall = document.querySelector("#overall");
const overallBar = document.querySelector("#overall-bar");
const overallLabel = document.querySelector("#overall-label");

document.querySelector("#select-files").addEventListener("click", () => choose("files"));
document.querySelector("#select-folder").addEventListener("click", () => choose("folder"));
document.querySelector("#browse-output").addEventListener("click", () => choose("output"));
document.querySelector("#add-paths").addEventListener("click", addPastedPaths);
document.querySelector("#optimize").addEventListener("click", startJob);
document.querySelector("#cancel").addEventListener("click", cancelJob);
document.querySelector("#open-folder").addEventListener("click", openFolder);
document.querySelector("#another").addEventListener("click", anotherBatch);
document.querySelector("#file-input").addEventListener("change", (event) => {
  uploadFiles(event.target.files);
  event.target.value = "";
});
document.querySelector("#quality").addEventListener("input", (event) => {
  document.querySelector("#quality-value").textContent = event.target.value;
});
document.querySelector("#keep-dimensions").addEventListener("change", syncDimensionInputs);
document.querySelector("#video-format").addEventListener("change", updatePresetHelp);
document.querySelector("#video-quality").addEventListener("change", updatePresetHelp);

const dropzone = document.querySelector("#dropzone");
dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragover");
  handleDrop(event);
});
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

init();

async function init() {
  await Promise.all([loadHealth(), loadDefaults()]);
  updatePresetHelp();
  syncDimensionInputs();
  const saved = sessionStorage.getItem(STORAGE_KEY);
  if (saved) {
    jobId = saved;
    poll();
    return;
  }
  try {
    const data = await request("/api/files");
    files = data.files || [];
  } catch (_error) {
    files = [];
  }
  render();
}

async function loadHealth() {
  const status = document.querySelector("#capability");
  try {
    const health = await request("/api/health");
    const parts = [];
    parts.push(health.ffmpeg ? `FFmpeg ${health.ffmpeg_version || "ready"}` : "FFmpeg not found — videos will fail until it is installed");
    parts.push(health.heic ? "HEIC input ready" : "HEIC input unavailable");
    parts.push(health.avif ? "AVIF output ready" : "AVIF output unavailable");
    status.textContent = parts.join(" · ");
    if (!health.avif) {
      const option = document.querySelector('#image-format option[value="avif"]');
      option.disabled = true;
      option.textContent = "AVIF (not available)";
    }
  } catch (error) {
    status.textContent = error.message;
  }
}

async function loadDefaults() {
  try {
    const data = await request("/api/defaults");
    const settings = data.settings || {};
    setValue("#image-format", settings.image_format);
    setValue("#quality", settings.image_quality);
    document.querySelector("#quality-value").textContent = settings.image_quality ?? 82;
    setValue("#max-width", settings.max_width);
    setValue("#max-height", settings.max_height);
    document.querySelector("#keep-dimensions").checked = Boolean(settings.keep_image_dimensions);
    document.querySelector("#preserve-metadata").checked = Boolean(settings.preserve_metadata);
    setValue("#video-format", settings.video_format);
    setValue("#video-quality", settings.video_quality);
    setValue("#encoder-preset", settings.encoder_preset);
    setValue("#max-resolution", settings.max_resolution);
    document.querySelector("#remove-audio").checked = Boolean(settings.remove_audio);
    document.querySelector("#skip-suitable").checked = settings.skip_suitable !== false;
    if (data.output_dir) document.querySelector("#output-dir").value = data.output_dir;
  } catch (_error) {
    // The form already shows the built-in defaults.
  }
}

function setValue(selector, value) {
  if (value === undefined || value === null) return;
  const element = document.querySelector(selector);
  if (element) element.value = value;
}

function updatePresetHelp() {
  const format = document.querySelector("#video-format").value;
  const quality = document.querySelector("#video-quality").value;
  const crf = format === "webm" ? vp9Crf[quality] : h264Crf[quality];
  const codec = format === "webm" ? "VP9" : "H.264";
  document.querySelector("#preset-help").textContent =
    `${codec} CRF ${crf}. A higher CRF makes a smaller file. Medium encoder speed is a good balance.`;
}

function syncDimensionInputs() {
  const keep = document.querySelector("#keep-dimensions").checked;
  document.querySelector("#max-width").disabled = keep;
  document.querySelector("#max-height").disabled = keep;
}

async function choose(mode) {
  if (dialogBusy || isRunning()) return;
  dialogBusy = true;
  setNotice(mode === "output" ? "Choose a folder in the window that opened." : "Choose files in the window that opened.");
  try {
    const data = await request("/api/dialog", {
      method: "POST",
      body: { mode },
    });
    if (mode === "output") {
      if (data.path) document.querySelector("#output-dir").value = data.path;
      setNotice("");
      return;
    }
    mergeFiles(data.files || []);
    setNotice(data.notice || "");
    render();
  } catch (error) {
    setNotice(error.message);
  } finally {
    dialogBusy = false;
  }
}

async function addPastedPaths() {
  const lines = document.querySelector("#path-input").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.length || isRunning()) return;
  try {
    const data = await request("/api/paths", { method: "POST", body: { paths: lines } });
    mergeFiles(data.files || []);
    document.querySelector("#path-input").value = "";
    setNotice(data.notice || "");
    render();
  } catch (error) {
    setNotice(error.message);
  }
}

async function handleDrop(event) {
  if (isRunning()) return;
  const items = [...(event.dataTransfer.items || [])];
  const collected = [];
  if (items.length && items[0].webkitGetAsEntry) {
    for (const item of items) {
      const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
      if (entry) collected.push(...(await walkEntry(entry)));
    }
  }
  if (!collected.length) collected.push(...event.dataTransfer.files);
  uploadFiles(collected);
}

async function walkEntry(entry) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    return file ? [file] : [];
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const children = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    children.push(...batch);
  }
  const filesInFolder = [];
  for (const child of children) filesInFolder.push(...(await walkEntry(child)));
  return filesInFolder;
}

function uploadFiles(fileList) {
  const chosen = [...fileList];
  if (!chosen.length || isRunning()) return;
  const form = new FormData();
  for (const file of chosen) form.append("files", file, file.name);
  setNotice(`Uploading ${chosen.length} file${chosen.length === 1 ? "" : "s"}…`);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round((event.loaded / event.total) * 100);
    setNotice(`Uploading… ${percent}%`);
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const data = JSON.parse(xhr.responseText);
      mergeFiles(data.files || []);
      setNotice(data.notice || "");
      render();
      return;
    }
    let message = "Upload failed.";
    try {
      const data = JSON.parse(xhr.responseText);
      if (typeof data.detail === "string") message = data.detail;
    } catch (_error) {
      // The status text is enough.
    }
    setNotice(message);
  };
  xhr.onerror = () => setNotice("Upload failed.");
  xhr.send(form);
}

function mergeFiles(incoming) {
  for (const file of incoming) {
    if (!files.some((item) => item.id === file.id)) files.push(file);
  }
}

async function removeFile(fileId) {
  try {
    await request(`/api/files/${fileId}`, { method: "DELETE" });
    files = files.filter((file) => file.id !== fileId);
    render();
  } catch (error) {
    setNotice(error.message);
  }
}

async function startJob() {
  const selected = visibleFiles();
  if (!selected.length || isRunning()) return;
  try {
    const created = await request("/api/jobs", {
      method: "POST",
      body: {
        file_ids: selected.map((file) => file.id),
        output_dir: document.querySelector("#output-dir").value.trim(),
        settings: collectSettings(),
      },
    });
    job = created;
    jobId = created.id;
    sessionStorage.setItem(STORAGE_KEY, jobId);
    setNotice("");
    render();
    poll();
  } catch (error) {
    setNotice(error.message);
  }
}

function collectSettings() {
  return {
    image_format: document.querySelector("#image-format").value,
    image_quality: Number(document.querySelector("#quality").value),
    max_width: Number(document.querySelector("#max-width").value),
    max_height: Number(document.querySelector("#max-height").value),
    keep_image_dimensions: document.querySelector("#keep-dimensions").checked,
    preserve_metadata: document.querySelector("#preserve-metadata").checked,
    video_format: document.querySelector("#video-format").value,
    video_quality: document.querySelector("#video-quality").value,
    encoder_preset: document.querySelector("#encoder-preset").value,
    max_resolution: document.querySelector("#max-resolution").value,
    remove_audio: document.querySelector("#remove-audio").checked,
    skip_suitable: document.querySelector("#skip-suitable").checked,
  };
}

async function poll() {
  if (!jobId) return;
  clearTimeout(pollTimer);
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (response.status === 404) {
      sessionStorage.removeItem(STORAGE_KEY);
      job = null;
      jobId = null;
      setNotice("The previous batch is no longer available because the app was restarted. Finished files are still in the output folder.");
      render();
      return;
    }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not read batch status.");
    job = data;
    render();
    if (job.status === "pending" || job.status === "running") pollTimer = setTimeout(poll, 400);
  } catch (error) {
    setNotice(error.message);
    pollTimer = setTimeout(poll, 1500);
  }
}

async function cancelJob() {
  if (!jobId) return;
  try {
    job = await request(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    render();
    poll();
  } catch (error) {
    setNotice(error.message);
  }
}

async function decide(fileId, decision) {
  try {
    job = await request(`/api/jobs/${jobId}/files/${fileId}/${decision}`, { method: "POST" });
    render();
  } catch (error) {
    setNotice(error.message);
  }
}

async function openFolder() {
  try {
    if (jobId) await request(`/api/jobs/${jobId}/open`, { method: "POST" });
    else await request("/api/open-output", { method: "POST", body: { path: document.querySelector("#output-dir").value.trim() } });
  } catch (error) {
    setNotice(error.message);
  }
}

async function anotherBatch() {
  try {
    await request("/api/reset", { method: "POST" });
    sessionStorage.removeItem(STORAGE_KEY);
    clearTimeout(pollTimer);
    job = null;
    jobId = null;
    files = [];
    setNotice("");
    render();
  } catch (error) {
    setNotice(error.message);
  }
}

function render() {
  const rows = visibleFiles();
  const running = isRunning();
  fileList.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "No files yet.";
    fileList.append(empty);
  }
  for (const file of rows) fileList.append(renderFile(file, running));

  document.querySelectorAll("fieldset").forEach((fieldset) => {
    fieldset.disabled = running;
  });
  syncDimensionInputs();
  if (running) {
    document.querySelector("#max-width").disabled = true;
    document.querySelector("#max-height").disabled = true;
  }

  optimizeButton.disabled = running || !rows.length || Boolean(job);
  optimizeButton.hidden = Boolean(job);
  cancelButton.hidden = !running;
  openButton.hidden = !job && !document.querySelector("#output-dir").value;
  anotherButton.hidden = !job || running;

  if (job && (job.status === "pending" || job.status === "running")) {
    const done = rows.filter((file) => ["completed", "skipped", "error", "cancelled", "discarded"].includes(file.status)).length;
    overall.hidden = false;
    overallBar.max = 100;
    overallBar.value = rows.length ? Math.round((done / rows.length) * 100) : 0;
    overallLabel.textContent = `${done} of ${rows.length} files finished`;
  } else {
    overall.hidden = true;
  }

  if (job && !running) {
    summary.hidden = false;
    summaryText.textContent = summaryMessage(job);
  } else if (!job) {
    summary.hidden = true;
  }
}

function visibleFiles() {
  return job ? job.files : files;
}

function isRunning() {
  return Boolean(job && (job.status === "pending" || job.status === "running"));
}

function renderFile(file, running) {
  const card = document.createElement("article");
  card.className = "file";

  const top = document.createElement("div");
  top.className = "file-top";
  const title = document.createElement("h3");
  title.textContent = file.name;
  top.append(title);
  if (!job && !running) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeFile(file.id));
    top.append(remove);
  }
  card.append(top);

  const meta = document.createElement("p");
  meta.className = "file-meta";
  meta.textContent = metaLine(file);
  card.append(meta);

  const folder = parentLabel(file.path);
  if (folder) {
    const path = document.createElement("p");
    path.className = "file-path";
    path.textContent = folder;
    card.append(path);
  }

  if (file.output_size != null || file.status === "skipped") {
    const sizes = document.createElement("p");
    sizes.className = "sizes";
    sizes.textContent = sizeLine(file);
    card.append(sizes);
  }

  if (file.status === "running") {
    const bar = document.createElement("progress");
    bar.max = 100;
    if (file.progress >= 0) bar.value = Math.max(0, Math.min(100, file.progress));
    card.append(bar);
  }

  const message = document.createElement("p");
  message.textContent = file.message || statusLabel(file.status);
  if (file.status === "error") message.className = "file-error";
  else if (file.larger && file.decision === "pending") message.className = "file-warn";
  else if (file.status === "completed" || file.status === "skipped") message.className = "file-ok";
  card.append(message);

  if (file.decision === "pending") {
    const actions = document.createElement("div");
    actions.className = "button-row";
    const keep = document.createElement("button");
    keep.type = "button";
    keep.textContent = "Keep";
    keep.addEventListener("click", () => decide(file.id, "keep"));
    const discard = document.createElement("button");
    discard.type = "button";
    discard.className = "danger";
    discard.textContent = "Discard";
    discard.addEventListener("click", () => decide(file.id, "discard"));
    actions.append(keep, discard);
    card.append(actions);
  }
  return card;
}

function metaLine(file) {
  const parts = [];
  if (file.format_label) parts.push(file.format_label);
  if (file.width && file.height) parts.push(`${file.width}×${file.height}`);
  if (file.duration) parts.push(formatDuration(file.duration));
  parts.push(formatBytes(file.size || 0));
  return parts.join(" · ");
}

function sizeLine(file) {
  if (file.status === "skipped") return `Original ${formatBytes(file.size)} · no new file written`;
  if (file.output_size == null) return "";
  const saved = file.size - file.output_size;
  const percent = file.saved_percent == null ? 0 : Math.abs(file.saved_percent);
  const change = saved >= 0
    ? `${savedLabel(saved)} saved (${percent.toFixed(0)}% smaller)`
    : `${savedLabel(saved)} larger (${percent.toFixed(0)}%)`;
  return `${formatBytes(file.size)} → ${formatBytes(file.output_size)} · ${change}`;
}

function summaryMessage(current) {
  const stats = current.summary;
  const lines = [
    `${stats.total} files: ${stats.completed} optimized, ${stats.skipped} skipped, ${stats.failed} failed, ${stats.cancelled} cancelled.`,
    `Space saved: ${savedLabel(stats.saved_bytes)}${stats.saved_percent ? ` (${stats.saved_percent}% smaller on the files that shrank)` : ""}.`,
  ];
  if (stats.pending_decision) lines.push(`${stats.pending_decision} file(s) got larger. Choose Keep or Discard. They stay in the output folder until you discard them.`);
  if (stats.discarded) lines.push(`${stats.discarded} larger file(s) discarded.`);
  lines.push(`Output folder: ${current.output_dir}`);
  return lines.join("\n");
}

function statusLabel(status) {
  return {
    ready: "Ready",
    pending: "Waiting",
    running: "Working",
    completed: "Done",
    skipped: "Skipped",
    error: "Failed",
    cancelled: "Cancelled",
    discarded: "Discarded",
  }[status] || status;
}

function parentLabel(path) {
  if (!path) return "";
  const parts = String(path).split(/[\\/]/);
  parts.pop();
  return parts.slice(-2).join("\\");
}

function savedLabel(bytes) {
  const amount = Math.abs(Number(bytes) || 0);
  if (amount >= 1024 * 1024 * 0.01) return `${(amount / (1024 * 1024)).toFixed(2)} MB`;
  return formatBytes(amount);
}

function formatBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let number = value / 1024;
  let index = 0;
  while (number >= 1024 && index < units.length - 1) {
    number /= 1024;
    index += 1;
  }
  return `${number.toFixed(number >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatDuration(seconds) {
  const total = Math.round(Number(seconds) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function setNotice(text) {
  notice.hidden = !text;
  notice.textContent = text || "";
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 204) return {};
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : `Request failed (${response.status}).`;
    throw new Error(message);
  }
  return data;
}
