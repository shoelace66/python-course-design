"use strict";

const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

const state = {
  selectedFile: null,
  currentRecord: null,
  records: [],
  stats: null,
  progressTimer: null,
  busy: false,
};

const pageTitles = {
  overview: "数据概览",
  analyze: "音乐分析",
  history: "分析记录",
  data: "数据管理",
  about: "原理与帮助",
};

function escapeHtml(value) {
  const replacements = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
  return String(value ?? "").replace(/[&<>"']/g, (character) => replacements[character]);
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.max(minimum, Math.min(maximum, Number(value) || 0));
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(2)} MB`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务器返回了无法识别的内容（HTTP ${response.status}）`);
  }
  if (!response.ok || payload.ok === false) {
    const suffix = payload.detail ? `：${payload.detail}` : "";
    throw new Error(`${payload.error || "请求失败"}${suffix}`);
  }
  return payload;
}

function toast(title, message = "", type = "success", duration = 4200) {
  const stack = $("#toastStack");
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.innerHTML = `<i>${type === "error" ? "×" : "✓"}</i><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div><button aria-label="关闭">×</button>`;
  const remove = () => element.remove();
  $("button", element).addEventListener("click", remove);
  stack.appendChild(element);
  window.setTimeout(remove, duration);
}

function navigate(page) {
  if (!pageTitles[page]) return;
  $$(".page").forEach((section) => section.classList.toggle("active", section.id === `page-${page}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $("#pageTitle").textContent = pageTitles[page];
  $(".sidebar").classList.remove("open");
  $("#mobileOverlay").classList.remove("open");
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (page === "history") loadHistory();
  if (page === "overview") loadDashboard();
  if (page === "analyze" && state.currentRecord) window.setTimeout(drawAllCharts, 80);
}

function setBusy(value) {
  state.busy = Boolean(value);
  $("#dropZone").classList.toggle("disabled", state.busy);
  $("#audioInput").disabled = state.busy;
  $("#analyzeButton").disabled = state.busy || !state.selectedFile;
  $("#demoButton").disabled = state.busy;
  $("#overviewDemo").disabled = state.busy;
  $("#importButton").disabled = state.busy;
  document.body.setAttribute("aria-busy", String(state.busy));
}

function setupNavigation() {
  $$('[data-page]').forEach((element) => element.addEventListener("click", () => navigate(element.dataset.page)));
  $$('[data-go-analyze]').forEach((element) => element.addEventListener("click", () => navigate("analyze")));
  $$('[data-page-link]').forEach((element) => element.addEventListener("click", (event) => { event.preventDefault(); navigate(element.dataset.pageLink); }));
  $("#mobileMenu").addEventListener("click", () => { $(".sidebar").classList.add("open"); $("#mobileOverlay").classList.add("open"); });
  $("#mobileOverlay").addEventListener("click", () => { $(".sidebar").classList.remove("open"); $("#mobileOverlay").classList.remove("open"); });
}

async function loadDashboard() {
  try {
    const [statsPayload, recordsPayload] = await Promise.all([api("/api/stats"), api("/api/analyses?limit=4")]);
    state.stats = statsPayload.stats;
    renderStats(statsPayload.stats);
    renderRecent(recordsPayload.records);
  } catch (error) {
    toast("无法读取概览", error.message, "error");
  }
}

function renderStats(stats) {
  $("#statCount").textContent = stats.analysis_count;
  $("#statDuration").textContent = formatDuration(stats.total_duration);
  $("#statTempo").textContent = stats.analysis_count && stats.average_tempo ? stats.average_tempo.toFixed(1) : "—";
  $("#statRms").textContent = stats.analysis_count ? stats.average_rms.toFixed(1) : "—";
  $("#lastUpdated").textContent = stats.last_analyzed_at ? `最近更新 ${formatDate(stats.last_analyzed_at)}` : "等待第一次分析";
  renderCategoryChart(stats.classifications || []);
  fillClassificationFilter(stats.classifications || []);
}

function renderCategoryChart(categories) {
  const target = $("#categoryChart");
  if (!categories.length) {
    target.className = "category-chart empty-chart";
    target.innerHTML = '<div class="empty-inline"><span>◎</span><p>完成分析后，这里会显示分类分布</p></div>';
    return;
  }
  const total = categories.reduce((sum, item) => sum + item.count, 0) || 1;
  target.className = "category-chart category-bars";
  target.innerHTML = categories.slice(0, 6).map((item) => {
    const percentage = Math.round(item.count / total * 100);
    return `<div class="category-row"><span>${escapeHtml(item.label)}</span><div class="category-track"><i style="width:${percentage}%"></i></div><b>${percentage}%</b></div>`;
  }).join("");
}

function fillClassificationFilter(categories) {
  const select = $("#historyFilter");
  const value = select.value;
  select.innerHTML = '<option value="">全部听感分类</option>' + categories.map((item) => `<option value="${escapeHtml(item.label)}">${escapeHtml(item.label)} (${item.count})</option>`).join("");
  if ([...select.options].some((option) => option.value === value)) select.value = value;
}

function renderRecent(records) {
  const target = $("#recentList");
  if (!records.length) {
    target.innerHTML = '<div class="empty-inline"><span>♩</span><p>还没有分析记录</p></div>';
    return;
  }
  target.innerHTML = records.map((record) => `<div class="recent-item" data-record-id="${record.id}" role="button" tabindex="0"><span class="recent-note">♫</span><div><strong title="${escapeHtml(record.file_name)}">${escapeHtml(record.file_name)}</strong><small>${formatDate(record.analyzed_at)} · ${record.tempo ? `${Number(record.tempo).toFixed(1)} BPM` : "节奏未知"}</small></div><span>${escapeHtml(record.classification)}</span></div>`).join("");
  $$(".recent-item", target).forEach((item) => {
    const open = () => openRecord(Number(item.dataset.recordId));
    item.addEventListener("click", open);
    item.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
  });
}

function setupUpload() {
  const dropZone = $("#dropZone");
  const input = $("#audioInput");
  const choose = () => input.click();
  dropZone.addEventListener("click", choose);
  dropZone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); } });
  input.addEventListener("change", () => selectAudio(input.files[0]));
  ["dragenter", "dragover"].forEach((type) => dropZone.addEventListener(type, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((type) => dropZone.addEventListener(type, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
  dropZone.addEventListener("drop", (event) => selectAudio(event.dataTransfer.files[0]));
  $("#clearFile").addEventListener("click", (event) => { event.stopPropagation(); clearAudio(); });
  $("#analyzeButton").addEventListener("click", analyzeSelected);
  $("#demoButton").addEventListener("click", analyzeDemo);
  $("#overviewDemo").addEventListener("click", () => { navigate("analyze"); analyzeDemo(); });
}

function selectAudio(file) {
  if (state.busy) { toast("分析正在进行", "请等待当前任务完成后再选择文件。", "error"); return; }
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".wav")) {
    toast("格式不支持", "请选择 PCM WAV 文件。MP3/M4A 可先转换为 WAV。", "error");
    return;
  }
  if (file.size > 80 * 1024 * 1024) {
    toast("文件过大", "单个音频最大允许 80 MB。", "error");
    return;
  }
  state.selectedFile = file;
  $("#selectedName").textContent = file.name;
  $("#selectedMeta").textContent = `${formatBytes(file.size)} · ${file.type || "audio/wav"}`;
  $("#selectedFile").classList.remove("hidden");
  $("#analyzeButton").disabled = false;
}

function clearAudio() {
  state.selectedFile = null;
  $("#audioInput").value = "";
  $("#selectedFile").classList.add("hidden");
  $("#analyzeButton").disabled = true;
}

function beginProgress() {
  window.clearInterval(state.progressTimer);
  const panel = $("#progressPanel");
  panel.classList.remove("hidden");
  let value = 7;
  const stages = [
    [16, "正在读取 PCM 采样…"],
    [32, "正在计算音量与动态范围…"],
    [51, "正在检测节奏周期…"],
    [68, "正在执行 FFT 与音高估计…"],
    [82, "正在匹配调性与听感分类…"],
    [91, "正在写入 SQLite 数据库…"],
  ];
  const update = () => {
    value = Math.min(92, value + Math.max(1, Math.round((94 - value) * .08)));
    let stage = [0, "正在准备分析…"];
    for (let index = 0; index < stages.length; index += 1) {
      if (value >= stages[index][0]) stage = stages[index];
    }
    $("#progressBar").style.width = `${value}%`;
    $("#progressPercent").textContent = `${value}%`;
    $("#progressStage").textContent = stage[1];
  };
  update();
  state.progressTimer = window.setInterval(update, 480);
}

function finishProgress(success = true) {
  window.clearInterval(state.progressTimer);
  state.progressTimer = null;
  if (success) {
    $("#progressBar").style.width = "100%";
    $("#progressPercent").textContent = "100%";
    $("#progressStage").textContent = "分析完成，已保存到数据库";
  }
  window.setTimeout(() => $("#progressPanel").classList.add("hidden"), success ? 650 : 50);
}

async function analyzeSelected() {
  if (!state.selectedFile || state.busy) return;
  setBusy(true);
  try {
    beginProgress();
    const payload = await api(`/api/analyze?filename=${encodeURIComponent(state.selectedFile.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: state.selectedFile,
    });
    finishProgress(true);
    renderRecord(payload.record);
    toast(payload.duplicate ? "已找到相同音频" : "音乐分析完成", payload.duplicate ? payload.message : "特征、图表与分类已保存到数据库。");
    await refreshAfterChange();
  } catch (error) {
    finishProgress(false);
    toast("分析失败", error.message, "error", 6500);
  } finally {
    setBusy(false);
  }
}

async function analyzeDemo() {
  if (state.busy) return;
  setBusy(true);
  try {
    beginProgress();
    const payload = await api("/api/demo", { method: "POST" });
    finishProgress(true);
    renderRecord(payload.record);
    toast("演示音频已生成", "已创建 C 大调、120 BPM 的合成音频并完成分析。");
    await refreshAfterChange();
  } catch (error) {
    finishProgress(false);
    toast("演示生成失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function refreshAfterChange() {
  await Promise.all([loadDashboard(), loadHistory()]);
}

function renderRecord(record) {
  if (!record) return;
  state.currentRecord = record;
  $("#analysisResults").classList.remove("hidden");
  $("#resultFileName").textContent = record.file_name || "导入记录";
  $("#resultTempo").textContent = record.tempo ? Number(record.tempo).toFixed(1) : "—";
  $("#tempoHint").textContent = record.tempo ? `节奏可信度 ${Math.round(clamp(record.tempo_confidence) * 100)}%` : "未检测到稳定节拍";
  $("#tempoConfidence").style.width = `${clamp(record.tempo_confidence) * 100}%`;
  $("#resultKey").textContent = record.key_name || "未知";
  $("#resultPitch").textContent = record.avg_pitch_hz ? `${Number(record.avg_pitch_hz).toFixed(1)} Hz · ${record.pitch_note}` : "主导音高未知";
  $("#keyConfidence").style.width = `${clamp(record.key_confidence) * 100}%`;
  $("#resultRms").textContent = Number(record.rms_db).toFixed(1);
  $("#resultPeak").textContent = `${Number(record.peak_db).toFixed(1)} dB`;
  $("#resultDynamic").textContent = `${Number(record.dynamic_range_db).toFixed(1)} dB`;
  $("#volumeMeter").style.width = `${clamp((Number(record.rms_db) + 60) / 60) * 100}%`;
  $("#resultClass").textContent = record.classification || "未分类";
  $("#resultReasons").textContent = record.explanation || "规则融合分类";
  $("#classConfidence").textContent = `规则匹配度 ${Math.round(clamp(record.classification_confidence) * 100)}%`;

  const miniMetrics = [
    ["音频时长", formatDuration(record.duration)],
    ["采样率", `${Number(record.sample_rate).toLocaleString()} Hz`],
    ["声道 / 位深", `${record.channels} ch / ${Number(record.sample_width) * 8} bit`],
    ["频谱重心", `${Number(record.spectral_centroid).toFixed(0)} Hz`],
    ["85% 滚降点", `${Number(record.spectral_rolloff).toFixed(0)} Hz`],
    ["过零率", Number(record.zero_crossing_rate).toFixed(4)],
  ];
  $("#secondaryMetrics").innerHTML = miniMetrics.map(([label, value]) => `<div class="mini-metric"><span>${label}</span><b>${escapeHtml(value)}</b></div>`).join("");

  if (record.audio_available) {
    $("#playerRow").classList.remove("hidden");
    $("#audioPlayer").src = `/api/audio/${record.id}`;
  } else {
    stopAudioPlayer();
    $("#playerRow").classList.add("hidden");
  }
  window.requestAnimationFrame(drawAllCharts);
  window.setTimeout(() => $("#analysisResults").scrollIntoView({ behavior: "smooth", block: "start" }), 250);
}

function stopAudioPlayer() {
  const player = $("#audioPlayer");
  player.pause();
  player.removeAttribute("src");
  player.load();
}

function prepareCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(280, Math.round(rect.width));
  const height = Math.max(140, Math.round(rect.height));
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function drawGrid(context, width, height, padding = 26) {
  context.strokeStyle = "rgba(155, 178, 210, .09)";
  context.lineWidth = 1;
  for (let index = 0; index <= 5; index += 1) {
    const y = padding + (height - padding * 2) * index / 5;
    context.beginPath(); context.moveTo(padding, y); context.lineTo(width - padding, y); context.stroke();
  }
  for (let index = 0; index <= 8; index += 1) {
    const x = padding + (width - padding * 2) * index / 8;
    context.beginPath(); context.moveTo(x, padding); context.lineTo(x, height - padding); context.stroke();
  }
}

function drawEmptyChart(canvas, message = "暂无可视化序列") {
  const { context, width, height } = prepareCanvas(canvas);
  context.fillStyle = "#68778d";
  context.font = '11px "Segoe UI", sans-serif';
  context.textAlign = "center";
  context.fillText(message, width / 2, height / 2);
}

function drawWaveform(canvas, points) {
  if (!Array.isArray(points) || !points.length) return drawEmptyChart(canvas);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 26;
  drawGrid(context, width, height, padding);
  const center = height / 2;
  const usableHeight = height - padding * 2;
  const gradient = context.createLinearGradient(0, padding, 0, height - padding);
  gradient.addColorStop(0, "rgba(85, 234, 212, .86)"); gradient.addColorStop(.5, "rgba(85, 234, 212, .30)"); gradient.addColorStop(1, "rgba(85, 234, 212, .86)");
  context.strokeStyle = gradient; context.lineWidth = 1.25; context.beginPath();
  points.forEach((point, index) => {
    const x = padding + index / Math.max(1, points.length - 1) * (width - padding * 2);
    const top = center - clamp(point.max, -1, 1) * usableHeight / 2;
    const bottom = center - clamp(point.min, -1, 1) * usableHeight / 2;
    context.moveTo(x, top); context.lineTo(x, bottom);
  });
  context.stroke();
  context.strokeStyle = "rgba(85,234,212,.25)"; context.beginPath(); context.moveTo(padding, center); context.lineTo(width - padding, center); context.stroke();
}

function drawLineChart(canvas, values, color = "#ffad66") {
  if (!Array.isArray(values) || !values.length) return drawEmptyChart(canvas);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 26;
  drawGrid(context, width, height, padding);
  const area = context.createLinearGradient(0, padding, 0, height - padding);
  area.addColorStop(0, `${color}50`); area.addColorStop(1, `${color}02`);
  const points = values.map((value, index) => ({ x: padding + index / Math.max(1, values.length - 1) * (width - padding * 2), y: height - padding - clamp(value) * (height - padding * 2) }));
  context.beginPath(); context.moveTo(points[0].x, height - padding); points.forEach((point) => context.lineTo(point.x, point.y)); context.lineTo(points[points.length - 1].x, height - padding); context.closePath(); context.fillStyle = area; context.fill();
  context.beginPath(); points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y)); context.strokeStyle = color; context.lineWidth = 1.6; context.stroke();
}

function drawSpectrum(canvas, bands) {
  if (!Array.isArray(bands) || !bands.length) return drawEmptyChart(canvas);
  const { context, width, height } = prepareCanvas(canvas);
  const padding = 26;
  drawGrid(context, width, height, padding);
  const usableWidth = width - padding * 2;
  const gap = Math.max(1, usableWidth / bands.length * .26);
  const barWidth = usableWidth / bands.length - gap;
  const gradient = context.createLinearGradient(0, padding, 0, height - padding);
  gradient.addColorStop(0, "#a499ff"); gradient.addColorStop(1, "rgba(139,125,255,.15)");
  context.fillStyle = gradient;
  bands.forEach((band, index) => {
    const barHeight = clamp(band.magnitude) * (height - padding * 2);
    const x = padding + index / bands.length * usableWidth;
    context.fillRect(x, height - padding - barHeight, Math.max(1, barWidth), barHeight);
  });
}

function drawChroma(canvas, chroma) {
  if (!Array.isArray(chroma) || !chroma.length) return drawEmptyChart(canvas, "CSV 摘要记录不含十二音级序列");
  const { context, width, height } = prepareCanvas(canvas);
  const left = 26, right = 18, top = 20, bottom = 30;
  drawGrid(context, width, height, 26);
  const maximum = Math.max(...chroma.map((item) => Number(item.value) || 0), .001);
  const usableWidth = width - left - right;
  const slot = usableWidth / chroma.length;
  chroma.forEach((item, index) => {
    const normalized = clamp((Number(item.value) || 0) / maximum);
    const barHeight = normalized * (height - top - bottom);
    const gradient = context.createLinearGradient(0, height - bottom - barHeight, 0, height - bottom);
    gradient.addColorStop(0, index % 2 ? "#ff77a8" : "#8b7dff"); gradient.addColorStop(1, "rgba(139,125,255,.13)");
    context.fillStyle = gradient;
    context.fillRect(left + slot * index + slot * .16, height - bottom - barHeight, slot * .68, barHeight);
    context.fillStyle = "#73829a"; context.font = '9px "Segoe UI", sans-serif'; context.textAlign = "center";
    context.fillText(item.note, left + slot * index + slot / 2, height - 11);
  });
}

function drawAllCharts() {
  const features = state.currentRecord?.feature_data || {};
  drawWaveform($("#waveformCanvas"), features.waveform);
  drawLineChart($("#energyCanvas"), features.energy, "#ffad66");
  drawSpectrum($("#spectrumCanvas"), features.spectrum);
  drawChroma($("#chromaCanvas"), features.chroma);
}

async function loadHistory() {
  try {
    const search = encodeURIComponent($("#historySearch")?.value.trim() || "");
    const classification = encodeURIComponent($("#historyFilter")?.value || "");
    const payload = await api(`/api/analyses?limit=500&search=${search}&classification=${classification}`);
    state.records = payload.records;
    renderHistory(payload.records);
  } catch (error) {
    toast("读取历史失败", error.message, "error");
  }
}

function renderHistory(records) {
  const body = $("#historyBody");
  const empty = $("#historyEmpty");
  $("#historyCount").textContent = `共 ${records.length} 条记录`;
  if (!records.length) {
    body.innerHTML = "";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  body.innerHTML = records.map((record) => `<tr><td><div class="track-cell"><i>♫</i><strong title="${escapeHtml(record.file_name)}">${escapeHtml(record.file_name)}</strong></div></td><td>${formatDate(record.analyzed_at)}</td><td>${formatDuration(record.duration)}</td><td>${record.tempo ? Number(record.tempo).toFixed(1) : "—"}</td><td>${escapeHtml(record.key_name)}</td><td>${Number(record.rms_db).toFixed(1)} dB</td><td><span class="class-tag">${escapeHtml(record.classification)}</span></td><td><div class="row-actions"><button data-action="view" data-id="${record.id}">查看</button><button class="delete-row" data-action="delete" data-id="${record.id}" data-name="${escapeHtml(record.file_name)}">删除</button></div></td></tr>`).join("");
}

async function openRecord(id) {
  try {
    const payload = await api(`/api/analyses/${id}`);
    navigate("analyze");
    renderRecord(payload.record);
    toast("已恢复历史结果", "图表数据直接从 SQLite 读取，无需重新分析。");
  } catch (error) {
    toast("无法打开记录", error.message, "error");
  }
}

function confirmDeletion(fileName) {
  const dialog = $("#confirmDialog");
  $("#confirmText").textContent = `“${fileName}”的数据库记录和保存的本地音频将被删除，此操作无法撤销。`;
  if (typeof dialog.showModal !== "function") return Promise.resolve(window.confirm($("#confirmText").textContent));
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }));
}

async function deleteRecord(id, fileName) {
  if (!await confirmDeletion(fileName)) return;
  if (state.currentRecord?.id === id) {
    stopAudioPlayer();
    await new Promise((resolve) => window.setTimeout(resolve, 120));
  }
  try {
    await api(`/api/analyses/${id}`, { method: "DELETE" });
    if (state.currentRecord?.id === id) { state.currentRecord = null; stopAudioPlayer(); $("#analysisResults").classList.add("hidden"); }
    toast("记录已删除", "数据库关联数据和本地音频已清理。");
    await refreshAfterChange();
  } catch (error) {
    toast("删除失败", error.message, "error");
  }
}

function setupHistory() {
  let searchTimer;
  $("#historySearch").addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(loadHistory, 260); });
  $("#historyFilter").addEventListener("change", loadHistory);
  $("#refreshHistory").addEventListener("click", loadHistory);
  $("#historyBody").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = Number(button.dataset.id);
    if (button.dataset.action === "view") openRecord(id);
    if (button.dataset.action === "delete") deleteRecord(id, button.dataset.name);
  });
}

function setupDataManagement() {
  $("#exportJson").addEventListener("click", () => downloadEndpoint("/api/export?format=json", "JSON 完整数据"));
  $("#exportCsv").addEventListener("click", () => downloadEndpoint("/api/export?format=csv", "CSV 数据摘要"));
  $("#backupDb").addEventListener("click", () => downloadEndpoint("/api/database-backup", "SQLite 数据库备份"));
  const input = $("#importInput");
  $("#importButton").addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    if (!/\.(json|csv)$/i.test(file.name)) { toast("文件格式错误", "请选择本系统导出的 JSON 或 CSV。", "error"); input.value = ""; return; }
    if (state.busy) { toast("请稍候", "当前有任务正在运行。", "error"); input.value = ""; return; }
    const button = $("#importButton");
    setBusy(true); button.textContent = "正在导入…";
    try {
      const payload = await api(`/api/import?filename=${encodeURIComponent(file.name)}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      const result = payload.result;
      toast("数据导入完成", `${result.message}${result.errors?.length ? `；${result.errors[0]}` : ""}`, result.failed ? "error" : "success", 6500);
      await refreshAfterChange();
    } catch (error) {
      toast("导入失败", error.message, "error", 6500);
    } finally {
      input.value = ""; button.textContent = "选择数据文件"; setBusy(false);
    }
  });
}

async function downloadEndpoint(path, label) {
  if (state.busy) return;
  setBusy(true);
  try {
    const response = await fetch(path);
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { const payload = await response.json(); message = payload.error || message; } catch { /* 保留状态码 */ }
      throw new Error(message);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const fallbackMatch = disposition.match(/filename="?([^";]+)"?/i);
    const fileName = encodedMatch ? decodeURIComponent(encodedMatch[1]) : (fallbackMatch ? fallbackMatch[1] : "musicscope_export.dat");
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl; link.download = fileName; document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    toast(`${label}已生成`, "文件已发送到浏览器下载目录。");
  } catch (error) {
    toast(`${label}失败`, error.message, "error");
  } finally {
    setBusy(false);
  }
}

function setupResize() {
  let timer;
  window.addEventListener("resize", () => { window.clearTimeout(timer); timer = window.setTimeout(() => { if (state.currentRecord && $("#page-analyze").classList.contains("active")) drawAllCharts(); }, 120); });
}

async function initialize() {
  setupNavigation();
  setupUpload();
  setupHistory();
  setupDataManagement();
  setupResize();
  try {
    await api("/api/health");
    await Promise.all([loadDashboard(), loadHistory()]);
  } catch (error) {
    toast("本地服务连接失败", error.message, "error", 8000);
  }
}

document.addEventListener("DOMContentLoaded", initialize);
