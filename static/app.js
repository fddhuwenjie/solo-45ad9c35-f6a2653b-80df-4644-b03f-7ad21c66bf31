/* Tape speed & splice audit console — frontend.
 * Web Audio API for decode/playback/live spectrum; canvases for waveform,
 * issue evidence, ruler and spectrum; all audit logic lives on the server.
 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state0 = {
  project: null,            // GET /api/projects/:id payload
  duration: 0,
  audioCtx: null,
  origBuffer: null,         // AudioBuffer of source WAV
  corrBuffer: null,         // AudioBuffer of latest corrected revision
  corrRevId: null,
  peaksOrig: null,          // precomputed {min,max} per pixel-bucket
  peaksCorr: null,
  view: { start: 0, end: 0 },
  tool: "select",
  selection: { start: 0, end: 0 },
  dragging: null,           // {kind,id,...}
  selected: { kind: null, id: null },
  play: { on: false, src: "original", when: 0, offset: 0, raf: 0,
          startCtx: 0, loop: true, node: null, gain: null },
  spectroAt: null,          // static analysis {t, src, img}
  busySeq: 0,
};

/* ------------------------------------------------------------------ API */

async function api(method, url, body, isForm) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) { opt.body = body; }
    else { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  }
  setStatus(method === "GET" ? "" : "处理中…", "busy");
  const res = await fetch(url, opt);
  const txt = await res.text();
  let doc; try { doc = txt ? JSON.parse(txt) : {}; } catch { doc = { error: txt }; }
  if (!res.ok) { setStatus(doc.error || ("HTTP " + res.status), "err"); throw new Error(doc.error || res.status); }
  setStatus("就绪", "ok");
  return doc;
}

function setStatus(msg, cls) {
  const el = $("#globalStatus");
  el.textContent = msg;
  el.className = cls || "";
}

/* ------------------------------------------------------------- formatting */

function fmtTime(s, ms = true) {
  if (!isFinite(s)) return "--:--";
  s = Math.max(0, s);
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return ms ? `${String(m).padStart(2, "0")}:${sec.toFixed(1).padStart(4, "0")}`
            : `${String(m).padStart(2, "0")}:${String(Math.floor(sec)).padStart(2, "0")}`;
}
function parseTime(s) {
  s = String(s).trim();
  const m = s.match(/^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$/);
  if (m) return (+m[1] || 0) * 3600 + +m[2] * 60 + +m[3];
  const v = parseFloat(s);
  return isNaN(v) ? null : v;
}
function fmtPct(r) { return ((r - 1) * 100).toFixed(2) + "%"; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ----------------------------------------------------------- project load */

async function loadProjects(selectId) {
  const doc = await api("GET", "/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = '<option value="">— 选择项目 —</option>' +
    doc.projects.map(p => `<option value="${p.id}">${esc(p.name)} · ${p.wav_name || ""} ${p.duration ? "(" + fmtTime(p.duration, false) + ")" : ""} · ${p.revisions}修订</option>`).join("");
  if (selectId) sel.value = selectId;
}

$("#newProjectBtn").onclick = () => $("#newProjectDialog").showModal();
$("#projectSelect").onchange = (e) => { if (e.target.value) openProject(e.target.value); };

$("#newProjectForm").onsubmit = async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const wav = $("#npWav").files[0], log = $("#npLog").files[0];
  if (!wav) { alert("请选择 PCM WAV 文件"); return; }
  const fd = new FormData();
  fd.append("name", $("#npName").value || wav.name);
  fd.append("wav", wav);
  if (log) fd.append("log", log);
  $("#newProjectDialog").close();
  const doc = await api("POST", "/api/projects", fd, true);
  await loadProjects(doc.id);
  await openProject(doc.id);
};

async function openProject(id) {
  stopPlay();
  const doc = await api("GET", `/api/projects/${id}`);
  state0.project = doc;
  state0.duration = doc.wav ? doc.wav.info.duration : 0;
  state0.corrBuffer = null; state0.corrRevId = null; state0.peaksCorr = null;
  state0.view = { start: 0, end: state0.duration || 1 };
  state0.selection = { start: 0, end: 0 };
  state0.selected = { kind: null, id: null };
  $("#workspace").classList.remove("hidden");
  $("#projMeta").textContent = doc.wav
    ? `${doc.wav.name} · ${doc.wav.info.channels}ch · ${doc.wav.info.sample_width * 8}-bit · ${doc.wav.info.frame_rate}Hz · ${fmtTime(state0.duration)}`
    : "（尚未上传 WAV）";
  syncParamsUI();
  await decodeOrig();
  sizeCanvases();
  renderAll();
  loadRevisions();
}

/* ----------------------------------------------------------------- audio */

async function decodeOrig() {
  const p = state0.project;
  if (!p || !p.wav) return;
  if (!state0.audioCtx) state0.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  setStatus("解码原始 WAV…", "busy");
  const buf = await fetchSourceWav(p.id);
  state0.origBuffer = await state0.audioCtx.decodeAudioData(buf.slice(0));
  state0.peaksOrig = computePeaks(state0.origBuffer, 2400);
  setStatus("就绪", "ok");
}

async function fetchSourceWav(pid) {
  // source bytes: reconstruct via revision-free route — the server keeps the
  // upload inside data/<pid>; expose it read-only through an endpoint.
  const r = await fetch(`/api/projects/${pid}/source.wav`);
  if (!r.ok) throw new Error("无法获取原始 WAV");
  return await r.arrayBuffer();
}

function computePeaks(audioBuffer, buckets) {
  const nch = audioBuffer.numberOfChannels;
  const n = audioBuffer.length;
  const per = Math.max(1, Math.floor(n / buckets));
  const min = new Float32Array(buckets), max = new Float32Array(buckets);
  for (let b = 0; b < buckets; b++) {
    let lo = 1, hi = -1;
    const s0 = b * per, s1 = Math.min(n, s0 + per);
    for (let c = 0; c < nch; c++) {
      const d = audioBuffer.getChannelData(c);
      for (let i = s0; i < s1; i += Math.max(1, Math.floor(per / 400) || 1)) {
        const v = d[i];
        if (v < lo) lo = v; if (v > hi) hi = v;
      }
    }
    min[b] = lo; max[b] = hi;
  }
  return { min, max, per, buckets, n };
}

async function loadCorrected(revId) {
  const p = state0.project;
  setStatus("载入校正结果…", "busy");
  const r = await fetch(`/api/projects/${p.id}/revisions/${revId}/corrected.wav`);
  if (!r.ok) { setStatus("校正 WAV 读取失败", "err"); return; }
  const buf = await r.arrayBuffer();
  state0.corrBuffer = await state0.audioCtx.decodeAudioData(buf);
  state0.corrRevId = revId;
  state0.peaksCorr = computePeaks(state0.corrBuffer, 2400);
  setStatus("校正结果已载入", "ok");
  renderAll();
}

/* ------------------------------------------------------------- playback */

function currentBuffer() {
  return state0.play.src === "corrected" && state0.corrBuffer
    ? state0.corrBuffer : state0.origBuffer;
}

function mapToSource(playT) {
  // corrected timeline -> source seconds for the playhead overlay
  if (state0.play.src !== "corrected" || !state0.project) return playT;
  for (const s of state0.project.segments) {
    if (playT >= (s.corrected_start_s ?? 0) - 1e-9 && playT <= (s.corrected_end_s ?? 0) + 1e-9) {
      const a = s.corrected_start_s ?? 0, b = s.corrected_end_s ?? a;
      const f = b > a ? (playT - a) / (b - a) : 0;
      return s.start_s + f * (s.end_s - s.start_s);
    }
  }
  return playT;
}

function playRange(start, end) {
  stopPlay();
  const buf = currentBuffer();
  if (!buf) return;
  const ctx = state0.audioCtx;
  ctx.resume();
  const dur = buf.duration;
  start = Math.max(0, Math.min(start, dur));
  end = end > start ? Math.min(end, dur) : dur;
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const gain = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 4096;
  src.connect(gain).connect(analyser).connect(ctx.destination);
  src.start(0, start, end - start);
  state0.play = { ...state0.play, on: true, when: start, startCtx: ctx.currentTime,
                  node: src, gain, analyser, loopEnd: end };
  src.onended = () => {
    if (!state0.play.on) return;
    if (state0.play.loop) { playRange(start, end); }
    else { stopPlay(); renderAll(); }
  };
  tickPlay();
}

function stopPlay() {
  if (state0.play.node) {
    const n = state0.play.node;
    n.onended = null;
    try { n.stop(); } catch {}
    try { n.disconnect(); } catch {}
  }
  cancelAnimationFrame(state0.play.raf);
  state0.play.on = false; state0.play.node = null;
  $("#playBtn").textContent = "▶";
}

function playheadT() {
  if (!state0.play.on) return null;
  return state0.play.when + (state0.audioCtx.currentTime - state0.play.startCtx);
}

function tickPlay() {
  const t = playheadT();
  if (t != null) {
    $("#playClock").textContent = fmtTime(t);
    drawSpectrumLive();
    drawPlayhead(mapToSource(t));
    // auto-scroll with playhead
    if (t > state0.view.end - 0.2) {
      const span = state0.view.end - state0.view.start;
      setView(state0.view.end - span * 0.15, state0.view.end + span * 0.85);
    }
  }
  state0.play.raf = requestAnimationFrame(tickPlay);
}

$("#playBtn").onclick = () => {
  if (state0.play.on) { stopPlay(); return; }
  $("#playBtn").textContent = "⏸";
  const s = state0.selection;
  if (s.end > s.start + 0.02) playRange(playTime(s.start), playTime(s.end));
  else playRange(playTime(clampPlay(state0.view.start)), 0);
};
$("#playSelBtn").onclick = () => {
  const s = state0.selection;
  if (!(s.end > s.start + 0.02)) { alert("先在波形上框选一段（或点击一个分段）"); return; }
  $("#playBtn").textContent = "⏸";
  playRange(playTime(s.start), playTime(s.end));
};
$$("#sourceSwitch .seg").forEach(b => b.onclick = () => {
  $$("#sourceSwitch .seg").forEach(x => x.classList.toggle("active", x === b));
  state0.play.src = b.dataset.src;
  if (state0.play.src === "corrected" && !state0.corrBuffer) {
    const rid = latestRevId();
    if (rid) loadCorrected(rid);
    else { alert("还没有修订。点击“生成修订”后再切换。");
      $$("#sourceSwitch .seg").forEach(x => x.classList.toggle("active", x.dataset.src === "original"));
      state0.play.src = "original"; }
  } else renderAll();
});
$("#loopChk").onchange = (e) => state0.play.loop = e.target.checked;
$("#segStart").onchange = () => { const v = parseTime($("#segStart").value); if (v != null) { state0.selection.start = v; renderAll(); } };
$("#segEnd").onchange = () => { const v = parseTime($("#segEnd").value); if (v != null) { state0.selection.end = v; renderAll(); } };

function clampPlay(t) { return Math.max(0, Math.min(t, (currentBuffer()?.duration || state0.duration) - 0.05)); }
function latestRevId() { return state0._revs && state0._revs.length ? state0._revs[state0._revs.length - 1].id : null; }

/* ------------------------------------------------------------- canvases */

const waveCv = $("#waveCanvas"), ovCv = $("#overlayCanvas"),
      rulerCv = $("#rulerCanvas"), specCv = $("#spectrumCanvas");
let cvGeom = { w: 1, h: 1, rw: 1, rh: 1, sw: 1, sh: 1 };

function sizeCanvases() {
  const dpr = window.devicePixelRatio || 1;
  for (const [cv, key] of [[waveCv, "w"], [ovCv, "w"], [rulerCv, "r"], [specCv, "s"]]) {
    const r = cv.parentElement.getBoundingClientRect();
    cv.width = Math.max(50, r.width * dpr);
    cv.height = Math.max(30, r.height * dpr);
    cv.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    cvGeom[key + "w"] = r.width; cvGeom[key + "h"] = r.height;
  }
}
window.addEventListener("resize", () => { sizeCanvases(); renderAll(); });

function xToT(x) { return state0.view.start + (x / cvGeom.ww) * (state0.view.end - state0.view.start); }
function tToX(t) {
  const { start, end } = state0.view;
  return cvGeom.ww * ((t - start) / Math.max(1e-9, end - start));
}

function setView(start, end) {
  const d = state0.duration || 1;
  start = Math.max(0, start);
  end = Math.min(d, end);
  if (end - start < 0.05) end = Math.min(d, start + 0.05);
  state0.view = { start, end };
  renderAll();
}

/* ----- waveform */
function drawWave() {
  const ctx = waveCv.getContext("2d");
  const W = cvGeom.ww, H = cvGeom.wh;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0e1216"; ctx.fillRect(0, 0, W, H);

  // segment backgrounds (corrected mapping status) on original-time axis
  const segs = state0.project?.segments || [];
  for (const s of segs) {
    const x0 = tToX(s.start_s), x1 = tToX(s.end_s);
    if (x1 < 0 || x0 > W) continue;
    ctx.fillStyle = s.confirmed ? "rgba(76,195,138,0.07)" : "rgba(224,169,78,0.07)";
    ctx.fillRect(x0, 0, x1 - x0, H);
  }

  ctx.lineWidth = 1;
  if ($("#showOrig").checked) drawPeaks(ctx, state0.peaksOrig, "#7fa8d9", 1.0);
  if ($("#showCorr").checked && state0.peaksCorr)
    drawPeaks(ctx, state0.peaksCorr, "#4cc38a", 0.8, true);

  // center line
  ctx.strokeStyle = "#26303d"; ctx.beginPath();
  ctx.moveTo(0, H / 2); ctx.lineTo(W, H / 2); ctx.stroke();

  // side labels A/B backdrop at top
  drawSideMarkers(ctx, W);
}

function drawPeaks(ctx, peaks, color, alpha = 1, corrected = false) {
  if (!peaks) return;
  const H = cvGeom.wh, W = cvGeom.ww;
  const { start, end } = state0.view;
  const dur = corrected && state0.corrBuffer ? state0.corrBuffer.duration : state0.duration;
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.beginPath();
  const t2b = (t) => corrected ? correctedToX(t) : tToX(t);
  for (let px = 0; px < W; px++) {
    const t0 = start + (px / W) * (end - start);
    const t1 = start + ((px + 1) / W) * (end - start);
    let lo = 1, hi = -1;
    if (corrected) {
      // corrected peaks stored in corrected-time buckets
      const bs = Math.floor(t0 / dur * peaks.buckets);
      const be = Math.ceil(t1 / dur * peaks.buckets);
      for (let b = Math.max(0, bs); b < Math.min(peaks.buckets, be + 1); b++) {
        if (peaks.min[b] < lo) lo = peaks.min[b];
        if (peaks.max[b] > hi) hi = peaks.max[b];
      }
    } else {
      const bs = Math.floor(t0 / dur * peaks.buckets);
      const be = Math.ceil(t1 / dur * peaks.buckets);
      for (let b = Math.max(0, bs); b < Math.min(peaks.buckets, be + 1); b++) {
        if (peaks.min[b] < lo) lo = peaks.min[b];
        if (peaks.max[b] > hi) hi = peaks.max[b];
      }
    }
    if (hi >= lo) {
      const y0 = H / 2 - (hi * H * 0.46), y1 = H / 2 - (lo * H * 0.46);
      ctx.moveTo(px + .5, y0); ctx.lineTo(px + .5, y1);
    }
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function correctedToX(corrT) {
  const s = sourceAtCorrected(corrT);
  return tToX(s);
}
function sourceAtCorrected(ct) {
  for (const s of state0.project.segments) {
    const a = s.corrected_start_s ?? 0, b = s.corrected_end_s ?? a;
    if (ct >= a - 1e-9 && ct <= b + 1e-9) {
      const f = b > a ? (ct - a) / (b - a) : 0;
      return s.start_s + f * (s.end_s - s.start_s);
    }
  }
  return ct;
}
function correctedAtSource(st) {
  for (const s of state0.project.segments) {
    if (st >= s.start_s - 1e-9 && st <= s.end_s + 1e-9) {
      const f = s.end_s > s.start_s ? (st - s.start_s) / (s.end_s - s.start_s) : 0;
      return (s.corrected_start_s ?? 0) + f * ((s.corrected_end_s ?? 0) - (s.corrected_start_s ?? 0));
    }
  }
  return st;
}
function playTime(srcT) {
  return state0.play.src === "corrected" ? correctedAtSource(srcT) : srcT;
}

function drawSideMarkers(ctx, W) {
  const anchors = state0.project?.state.anchors || [];
  const sides = anchors.map(a => a.side).filter(Boolean);
  if (!sides.length) return;
  ctx.font = "10px monospace"; ctx.textBaseline = "top";
  // side A from start to first B anchor
  const firstB = anchors.find(a => (a.side || "").toUpperCase().startsWith("B"));
  const bx = firstB ? tToX(firstB.pos_s) : W;
  ctx.fillStyle = "rgba(78,161,255,0.10)";
  ctx.fillRect(0, 0, Math.min(W, bx), 14);
  ctx.fillStyle = "#8fc0ff"; ctx.fillText("A 面", 4, 2);
  if (firstB) {
    ctx.fillStyle = "rgba(185,140,255,0.10)";
    ctx.fillRect(Math.max(0, bx), 0, W - Math.max(0, bx), 14);
    ctx.fillStyle = "#d2b6ff"; ctx.fillText("B 面", bx + 4, 2);
  }
}

/* ----- overlay: anchors, splices, zones, issues, selection, playhead */
function drawOverlay() {
  const ctx = ovCv.getContext("2d");
  const W = cvGeom.ww, H = cvGeom.wh;
  ctx.clearRect(0, 0, W, H);
  const st = state0.project?.state;
  if (!st) return;

  // suspect zones
  for (const z of st.suspect_zones || []) {
    const x0 = tToX(z.start_s), x1 = tToX(z.end_s);
    ctx.fillStyle = "rgba(224,169,78,0.13)";
    ctx.fillRect(x0, 16, x1 - x0, H - 16);
    ctx.strokeStyle = state0.selected.id === z.id ? "#ffd88a" : "#c98f35";
    ctx.setLineDash([5, 4]); ctx.strokeRect(x0 + .5, 16.5, x1 - x0 - 1, H - 17);
    ctx.setLineDash([]);
    ctx.fillStyle = "#e0a94e"; ctx.font = "10px sans-serif";
    ctx.fillText("疑似掉速 " + esc(z.label || ""), x0 + 3, 18);
  }

  // issue evidence circles/rects
  if ($("#showEvidence").checked) {
    for (const iss of state0.project.issues || []) {
      drawIssueEvidence(ctx, iss, W, H);
    }
  }

  // selection
  const sel = state0.selection;
  if (sel.end > sel.start + 0.001) {
    const x0 = tToX(sel.start), x1 = tToX(sel.end);
    ctx.fillStyle = "rgba(78,161,255,0.14)";
    ctx.fillRect(x0, 0, x1 - x0, H);
    ctx.strokeStyle = "rgba(78,161,255,.8)";
    ctx.setLineDash([3, 3]); ctx.strokeRect(x0 + .5, .5, x1 - x0 - 1, H - 1); ctx.setLineDash([]);
  }

  // splices
  for (const sp of st.splices || []) drawSplice(ctx, sp, H);
  // anchors on top
  for (const a of st.anchors || []) drawAnchor(ctx, a, H);

  // playhead
  const ph = playheadT();
  if (ph != null) drawPlayheadAt(ctx, mapToSource(ph), W, H, "#56d4dd");
}

function drawIssueEvidence(ctx, iss, W, H) {
  const resolved = iss.severity === "block" && iss.adopted;
  const col = iss.severity === "block" ? (resolved ? "#4cc38a" : "#ef6b6b")
            : iss.severity === "warn" ? "#e0a94e" : "#56d4dd";
  const x0 = tToX(iss.start_s), x1 = tToX(iss.end_s);
  if (x1 < -20 || x0 > W + 20) return;
  ctx.save();
  ctx.strokeStyle = col;
  ctx.lineWidth = iss.code === "speed_jump" ? 2.5 : 1.5;
  ctx.globalAlpha = resolved ? 0.45 : 0.95;
  if (x1 - x0 < 3) {
    // narrow evidence: circle the spot
    ctx.beginPath(); ctx.arc(x0, H / 2, 12, 0, Math.PI * 2); ctx.stroke();
  } else if (iss.code === "speed_jump") {
    // bracket across the whole suspect interval
    ctx.setLineDash([7, 4]);
    ctx.strokeRect(x0 + 1, 24, x1 - x0 - 2, H - 40);
    ctx.setLineDash([]);
  } else {
    roundedRect(ctx, x0 + 1, 20, Math.max(2, x1 - x0 - 2), H - 34, 4);
    ctx.stroke();
  }
  // tag
  if (x1 - x0 > 26) {
    ctx.fillStyle = col; ctx.font = "10px monospace";
    ctx.fillText(iss.code + (resolved ? " ✓已采纳" : ""), x0 + 3, 22);
  }
  ctx.restore();
}

function drawAnchor(ctx, a, H) {
  const x = tToX(a.pos_s);
  if (x < -20 || x > cvGeom.ww + 20) return;
  const ratio = anchorRatio(a);
  const sel = state0.selected.kind === "anchor" && state0.selected.id === a.id;
  const bad = (state0.project.issues || []).some(i =>
    i.severity === "block" && !i.adopted && i.refs.includes(a.id));
  const col = !a.analysis ? "#8a95a5" : bad ? "#ef6b6b" : "#4cc38a";
  // diamond handle
  ctx.save();
  ctx.translate(x, 16);
  ctx.fillStyle = col; ctx.strokeStyle = sel ? "#fff" : "#0e1216";
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(7, 9); ctx.lineTo(0, 18); ctx.lineTo(-7, 9);
  ctx.closePath(); ctx.fill(); ctx.lineWidth = sel ? 2 : 1; ctx.stroke();
  ctx.restore();
  ctx.strokeStyle = col; ctx.globalAlpha = 0.5;
  ctx.beginPath(); ctx.moveTo(x, 34); ctx.lineTo(x, H); ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.fillStyle = col; ctx.font = "10px monospace";
  const label = `${a.side}${ratio != null ? " " + fmtPct(ratio) : ""}`;
  ctx.fillText(label, x + 9, 30);
}

function drawSplice(ctx, sp, H) {
  const x = tToX(sp.at_s);
  const sel = state0.selected.kind === "splice" && state0.selected.id === sp.id;
  const bad = (state0.project.issues || []).some(i =>
    i.code === "splice_overlap" && !i.adopted && i.refs.includes(sp.id));
  ctx.save();
  ctx.strokeStyle = bad ? "#ef6b6b" : "#b98cff";
  ctx.lineWidth = sel ? 2.5 : 1.5;
  ctx.beginPath(); ctx.moveTo(x, 16); ctx.lineTo(x - 6, H); ctx.stroke();
  ctx.fillStyle = ctx.strokeStyle;
  ctx.font = "10px monospace";
  ctx.fillText("✂" + (sp.tc_in ? ` ${sp.tc_in}→${sp.tc_out}` : ""), x + 4, H - 16);
  ctx.restore();
}

function drawPlayheadAt(ctx, t, W, H, color) {
  const x = tToX(t);
  ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  ctx.restore();
}
function drawPlayhead(t) { drawOverlay(); }  // redraw overlay with playhead

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

/* ----- ruler */
function drawRuler() {
  const ctx = rulerCv.getContext("2d");
  const W = cvGeom.rw, H = cvGeom.rh;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#171c23"; ctx.fillRect(0, 0, W, H);
  const span = state0.view.end - state0.view.start;
  const raw = span / 12;
  const step = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = [1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600]
    .find(v => v >= span / 12) || step;
  ctx.strokeStyle = "#333e4d"; ctx.fillStyle = "#9aa7b6"; ctx.font = "10px monospace";
  const first = Math.ceil(state0.view.start / nice) * nice;
  for (let t = first; t <= state0.view.end; t += nice) {
    const x = tToX(t);
    ctx.beginPath(); ctx.moveTo(x, H - 8); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillText(fmtTime(t, nice < 30), x + 2, 3);
  }
}

/* ----- spectrum */
function drawSpectrumLive() {
  const an = state0.play.analyser;
  if (!an) return;
  const ctx = specCv.getContext("2d");
  const W = cvGeom.sw, H = cvGeom.sh;
  const n = an.frequencyBinCount;
  const mag = new Uint8Array(n);
  an.getByteFrequencyData(mag);
  const sr = state0.audioCtx.sampleRate;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0c0f13"; ctx.fillRect(0, 0, W, H);
  // mark nominal tone frequency
  const nominal = +state0.project.state.params.tone_hz || 1000;
  drawSpecGrid(ctx, W, H, sr, nominal);
  ctx.fillStyle = "#56d4dd";
  const fMax = Math.min(sr / 2, nominal * 2.2);
  for (let x = 0; x < W; x++) {
    const f = (x / W) * fMax;
    const b = Math.min(n - 1, Math.floor(f / sr * an.fftSize));
    const v = mag[b] / 255;
    ctx.fillRect(x, H - v * (H - 14), 1, v * (H - 14));
  }
  const t = playheadT();
  $("#specTitle").textContent = `实时频谱 · ${state0.play.src === "corrected" ? "校正结果" : "原声"}`;
  $("#specReadout").textContent = t != null ? fmtTime(t) + (state0.play.src === "corrected" ? " (校正轴)" : "") : "";
}

function drawSpecGrid(ctx, W, H, sr, nominal) {
  ctx.strokeStyle = "#1d2530"; ctx.fillStyle = "#768395"; ctx.font = "9px monospace";
  const fMax = Math.min(sr / 2, nominal * 2.2);
  for (let f = 0; f <= fMax; f += 100) {
    const x = f / fMax * W;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    if (f % 200 === 0) ctx.fillText(f + "", x + 1, H - 2);
  }
  const xn = nominal / fMax * W;
  ctx.strokeStyle = "#4cc38a"; ctx.setLineDash([4, 3]);
  ctx.beginPath(); ctx.moveTo(xn, 0); ctx.lineTo(xn, H); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = "#4cc38a"; ctx.fillText(nominal + "Hz", xn + 2, 9);
}

async function drawStaticSpectrum(t) {
  const buf = currentBuffer();
  if (!buf) return;
  const sr = buf.sampleRate;
  const win = Math.min(1.0, buf.duration / 4);
  const n = Math.min(buf.length, 2 ** 16);
  const off = Math.max(0, Math.floor((t - win / 2) * sr));
  const ch0 = buf.getChannelData(0);
  const N = 1 << Math.floor(Math.log2(Math.min(n, ch0.length - off)));
  const re = new Float64Array(N), im = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const v = ch0[off + i] || 0;
    re[i] = v * (0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1)));
  }
  // offline FFT via OfflineAudioContext trick is awkward; use small DFT band
  const nominal = +state0.project.state.params.tone_hz || 1000;
  const fMax = nominal * 2.2, nbins = 600;
  const mags = new Float32Array(nbins);
  for (let k = 0; k < nbins; k++) {
    const f = (k / nbins) * fMax;
    let rr = 0, ii = 0;
    // coarse stride for speed
    const stride = Math.max(1, Math.floor(N / 4096));
    for (let i = 0; i < N; i += stride) {
      const ph = 2 * Math.PI * f * i / sr;
      rr += re[i] * Math.cos(ph); ii += re[i] * Math.sin(ph);
    }
    mags[k] = Math.hypot(rr, ii);
  }
  let peak = 1e-12; for (const m of mags) if (m > peak) peak = m;
  const ctx = specCv.getContext("2d");
  const W = cvGeom.sw, H = cvGeom.sh;
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = "#0c0f13"; ctx.fillRect(0, 0, W, H);
  drawSpecGrid(ctx, W, H, sr, nominal);
  ctx.fillStyle = "#7fa8d9";
  const bw = W / nbins;
  for (let k = 0; k < nbins; k++) ctx.fillRect(k * bw, H - (mags[k] / peak) * (H - 14), bw + .5, (mags[k] / peak) * (H - 14));
  // mark selected anchor candidates if the cursor is at one
  const anc = (state0.project?.state.anchors || []).find(a => Math.abs(a.pos_s - t) < win);
  if (anc?.analysis?.candidates) {
    anc.analysis.candidates.forEach((c, i) => {
      const x = c.hz / fMax * W;
      ctx.strokeStyle = i === anc.chosen_index ? "#4cc38a" : "#e0a94e";
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      ctx.fillStyle = ctx.strokeStyle;
      ctx.fillText(c.hz.toFixed(1), x + 2, 20 + i * 11);
    });
  }
  $("#specTitle").textContent = `静态频谱 @ ${fmtTime(t)} · ${state0.play.src === "corrected" ? "校正结果" : "原声"}`;
}

/* ------------------------------------------------------- mouse interaction */

ovCv.addEventListener("mousedown", onMouseDown);
window.addEventListener("mousemove", onMouseMove);
window.addEventListener("mouseup", onMouseUp);
ovCv.addEventListener("dblclick", onDblClick);

function eventPos(e) {
  const r = ovCv.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}
function hitAnchor(t) {
  const anc = state0.project.state.anchors || [];
  for (let i = anc.length - 1; i >= 0; i--)
    if (Math.abs(tToX(anc[i].pos_s) - t.x) < 9) return { kind: "anchor", item: anc[i] };
  return null;
}
function hitSplice(t) {
  for (const sp of [...(state0.project.state.splices || [])].reverse())
    if (Math.abs(tToX(sp.at_s) - t.x) < 8) return { kind: "splice", item: sp };
  return null;
}
function hitZone(t) {
  for (const z of [...(state0.project.state.suspect_zones || [])].reverse())
    if (t.x >= tToX(z.start_s) && t.x <= tToX(z.end_s)) return { kind: "zone", item: z };
  return null;
}

function onMouseDown(e) {
  const p = eventPos(e);
  const t = xToT(p.x);
  if (state0.tool === "select") {
    const h = hitAnchor(p) || hitSplice(p) || hitZone(p);
    if (h) {
      state0.selected = { kind: h.kind, id: h.item.id };
      state0.dragging = { kind: h.kind, id: h.item.id, moved: false };
      renderAll();
      return;
    }
    // begin rubber-band selection
    state0.dragging = { kind: "rubber", t0: t, moved: false };
    state0.selected = { kind: null, id: null };
  } else if (state0.tool === "anchor") {
    addAnchor(t);
  } else if (state0.tool === "splice") {
    addSplice(t);
  } else if (state0.tool === "zone") {
    state0.dragging = { kind: "newzone", t0: t };
  } else if (state0.tool === "zoom") {
    state0.dragging = { kind: "zoomrect", t0: t };
  }
  renderAll();
}

function onMouseMove(e) {
  const p = eventPos(e);
  const t = xToT(Math.max(0, Math.min(cvGeom.ww, p.x)));
  ovCv.style.cursor = "default";
  if (!state0.dragging) {
    if (state0.tool === "select" && (hitAnchor(p) || hitSplice(p))) ovCv.style.cursor = "ew-resize";
    return;
  }
  const d = state0.dragging;
  if (d.kind === "anchor" || d.kind === "splice") {
    d.moved = true;
    const list = d.kind === "anchor" ? state0.project.state.anchors : state0.project.state.splices;
    const it = list.find(x => x.id === d.id);
    if (it) {
      const field = d.kind === "anchor" ? "pos_s" : "at_s";
      it[field] = Math.max(0, Math.min(state0.duration, t));
      if (d.kind === "anchor") it.analysis = null; // moved => re-calibrate
      renderAll();
    }
  } else if (d.kind === "rubber") {
    d.moved = true;
    state0.selection = { start: Math.min(d.t0, t), end: Math.max(d.t0, t) };
    renderAll();
  } else if (d.kind === "newzone") {
    d.t1 = t; renderAll();
  } else if (d.kind === "zoomrect") {
    d.t1 = t; renderAll();
  }
}

function onMouseUp() {
  const d = state0.dragging;
  if (!d) return;
  state0.dragging = null;
  if (d.kind === "rubber" && d.moved) {
    $("#segStart").value = state0.selection.start.toFixed(2);
    $("#segEnd").value = state0.selection.end.toFixed(2);
  }
  if (d.kind === "anchor" || d.kind === "splice") {
    if (d.moved) saveState();
  }
  if (d.kind === "newzone" && d.t1 != null) {
    const s = Math.min(d.t0, d.t1), e = Math.max(d.t0, d.t1);
    if (e - s > 0.05) {
      state0.project.state.suspect_zones.push(
        { id: "z" + Math.random().toString(36).slice(2, 8), start_s: s, end_s: e, label: "" });
      saveState();
    }
  }
  if (d.kind === "zoomrect" && d.t1 != null) {
    setView(Math.min(d.t0, d.t1), Math.max(d.t0, d.t1));
  }
  renderAll();
}

let dblClickTimer = 0;
function onDblClick(e) {
  const p = eventPos(e), t = xToT(p.x);
  if (state0.tool === "select") { drawStaticSpectrum(t); return; }
  drawStaticSpectrum(t);
}

/* ----- tools */
$$("#toolGroup .tool").forEach(b => b.onclick = () => {
  state0.tool = b.dataset.tool;
  $$("#toolGroup .tool").forEach(x => x.classList.toggle("active", x === b));
});
$("#zoomInBtn").onclick = () => { const c = (state0.view.start + state0.view.end) / 2, sp = (state0.view.end - state0.view.start) / 2.2; setView(c - sp, c + sp); };
$("#zoomOutBtn").onclick = () => { const c = (state0.view.start + state0.view.end) / 2, sp = (state0.view.end - state0.view.start) * 1.6; setView(c - sp, c + sp); };
$("#zoomFitBtn").onclick = () => setView(0, state0.duration);
$("#zoomSelectionBtn").onclick = () => { const s = state0.selection; if (s.end > s.start) setView(s.start, s.end); };

/* ----------------------------------------------------- state mutations */

async function saveState() {
  const p = state0.project;
  const doc = await api("PUT", `/api/projects/${p.id}/state`, { state: p.state });
  state0.project = doc;
  renderAll();
}

async function addAnchor(t) {
  const st = state0.project.state;
  const nominal = +st.params.tone_hz || 1000;
  const a = { id: "a" + Math.random().toString(36).slice(2, 8),
              pos_s: Math.max(0, Math.min(state0.duration, t)),
              tone_hz: nominal, side: currentSide(t), label: "",
              window_s: +st.params.window_s || 1, analysis: null, chosen_index: null };
  st.anchors.push(a);
  await saveState();
  state0.selected = { kind: "anchor", id: a.id };
  renderEditor();
  analyzeAnchor(a.id);
}
async function addSplice(t) {
  const st = state0.project.state;
  const sp = { id: "s" + Math.random().toString(36).slice(2, 8), at_s: t,
               side: currentSide(t), tc_in: "", tc_out: "", tc_in_s: null,
               tc_out_s: null, label: "" };
  st.splices.push(sp);
  await saveState();
  state0.selected = { kind: "splice", id: sp.id };
  renderAll();
}
function currentSide(t) {
  const anc = state0.project.state.anchors;
  const firstB = anc.find(a => (a.side || "").toUpperCase().startsWith("B"));
  return firstB && t >= firstB.pos_s ? "B" : "A";
}
function anchorRatio(a) {
  if (!a.analysis?.candidates?.length) return null;
  const i = (a.chosen_index ?? 0);
  return a.analysis.candidates[i]?.ratio ?? null;
}

async function analyzeAnchor(id) {
  const p = state0.project;
  setStatus("FFT 分析校准音中…", "busy");
  try {
    const doc = await api("POST", `/api/projects/${p.id}/analyze`, { anchor_id: id });
    state0.project = doc;
    setStatus("分析完成", "ok");
    renderAll();
  } catch (e) { setStatus("分析失败: " + e.message, "err"); }
}
$("#analyzeAllBtn").onclick = async () => {
  const p = state0.project;
  setStatus("逐个分析锚点（纯 Python FFT）…", "busy");
  state0.project = await api("POST", `/api/projects/${p.id}/analyze_all`, {});
  renderAll();
};

/* params panel */
function syncParamsUI() {
  const q = state0.project?.state.params;
  if (!q) return;
  $("#pTone").value = q.tone_hz; $("#pWin").value = q.window_s;
  $("#pBand").value = q.band_hz; $("#pJump").value = q.speed_jump_limit;
  $("#pGap").value = q.max_gap_s;
}
["pTone", "pWin", "pBand", "pJump", "pGap"].forEach(id =>
  $("#" + id).onchange = async () => {
    const st = state0.project.state;
    st.params.tone_hz = +$("#pTone").value;
    st.params.window_s = +$("#pWin").value;
    st.params.band_hz = +$("#pBand").value;
    st.params.speed_jump_limit = +$("#pJump").value;
    st.params.max_gap_s = +$("#pGap").value;
    await saveState();
  });

/* -------------------------------------------------------------- tables */

function renderAnchorTable() {
  const st = state0.project.state;
  const tb = $("#anchorTable tbody");
  $("#anchorCount").textContent = st.anchors.length;
  tb.innerHTML = st.anchors.map(a => {
    const r = anchorRatio(a);
    const bad = state0.project.issues.some(i => i.severity === "block" && !i.adopted && i.refs.includes(a.id));
    const cls = !a.analysis ? "idle" : bad ? "bad" : "ok";
    const stat = !a.analysis ? "待分析" : bad ? "存疑" : "可用";
    const meas = a.analysis?.candidates?.length
      ? a.analysis.candidates[a.chosen_index ?? 0].hz.toFixed(1) : "—";
    return `<tr data-id="${a.id}" class="${state0.selected.id === a.id ? "sel" : ""}">
      <td><span class="dot ${cls}"></span>${fmtTime(a.pos_s)}</td>
      <td>${esc(a.side)}</td><td>${a.tone_hz}</td><td>${meas}</td>
      <td>${r != null ? fmtPct(r) : "—"}</td><td>${stat}</td>
      <td><button class="btn mini" data-act="an">分析</button></td></tr>`;
  }).join("");
  tb.querySelectorAll("tr").forEach(tr => tr.onclick = (e) => {
    if (e.target.dataset.act === "an") { analyzeAnchor(tr.dataset.id); return; }
    state0.selected = { kind: "anchor", id: tr.dataset.id };
    centerOn(state0.project.state.anchors.find(a => a.id === tr.dataset.id).pos_s);
    renderAll();
  });
}

function centerOn(t) {
  const sp = state0.view.end - state0.view.start;
  if (t < state0.view.start || t > state0.view.end) setView(t - sp / 2, t + sp / 2);
}

function renderIssues() {
  const issues = state0.project.issues || [];
  const blocks = issues.filter(i => i.severity === "block");
  const openBlocks = blocks.filter(i => !i.adopted).length;
  $("#issueCount").textContent = openBlocks ? `⛔ ${openBlocks} 未确认` : "无阻断";
  $("#issueCount").className = "tag " + (openBlocks ? "" : "");
  $("#confBadge").className = "badge " + (openBlocks ? "bad" : blocks.length ? "warn" : "ok");
  $("#confBadge").textContent = openBlocks
    ? `⛔ ${openBlocks} 处不能确认（${fmtTime(issues.find(i => i.severity==="block"&&!i.adopted).start_s)} 起）`
    : blocks.length ? "阻断均已附理由采纳" : "✓ 无阻断项（边缘超覆盖段仍为 unconfirmed）";

  $("#issueList").innerHTML = issues.length ? issues.map(i => `
    <div class="issue ${i.severity} ${i.severity === "block" && i.adopted ? "resolved" : ""}">
      <div class="row1"><span class="sev ${i.severity}">${i.severity === "block" ? "阻断" : i.severity === "warn" ? "警示" : "信息"}</span>
        <span class="code">${i.key}</span></div>
      <p>${esc(i.message)} ${i.detail ? `<span class="muted">(${esc(i.detail)})</span>` : ""}</p>
      <div><span class="ev" data-jump="${i.start_s},${i.end_s}">⌖ 波形证据 ${fmtTime(i.start_s)}–${fmtTime(i.end_s)}</span></div>
      ${i.severity === "block" ? `
        <textarea placeholder="采用此异常锚点/接续的理由（必填后该段方可确认）…" data-reason="${esc(i.key)}">${esc(i.reason || "")}</textarea>
        <div style="margin-top:3px">${i.adopted ? '<span style="color:var(--ok)">✓ 已附理由采纳</span>' : '<span style="color:var(--bad)">未附理由 → 该段不能确认</span>'}</div>` : ""}
    </div>`).join("") : '<p class="muted">无问题。</p>';

  $("#issueList").querySelectorAll(".ev").forEach(el => el.onclick = () => {
    const [s, e] = el.dataset.jump.split(",").map(Number);
    setView(Math.max(0, s - (e - s + 2) * 0.4), e + (e - s + 2) * 0.4);
    state0.selection = { start: s, end: e };
    renderAll();
  });
  $("#issueList").querySelectorAll("textarea[data-reason]").forEach(tx => {
    tx.onchange = async () => {
      const st = state0.project.state;
      st.adoptions = st.adoptions || {};
      if (tx.value.trim()) st.adoptions[tx.dataset.reason] = tx.value.trim();
      else delete st.adoptions[tx.dataset.reason];
      await saveState();
    };
  });
}

/* ----------------------------------------------------- inspector editor */

function renderEditor() {
  const st = state0.project?.state;
  const kind = state0.selected.kind, id = state0.selected.id;
  $("#editorKind").textContent = kind || "";
  const body = $("#editorBody");
  if (!st) return;
  if (kind === "anchor") {
    const a = st.anchors.find(x => x.id === id);
    if (!a) { body.className = "muted"; body.textContent = "锚点已删除。"; return; }
    body.classList.remove("muted");
    const cands = a.analysis?.candidates || [];
    body.innerHTML = `
      <div class="kv">
        <span>位置(秒)</span><input type="text" id="edPos" value="${a.pos_s.toFixed(3)}">
        <span>面别</span><select id="edSide"><option ${a.side === "A" ? "selected" : ""}>A</option><option ${a.side === "B" ? "selected" : ""}>B</option></select>
        <span>标称频率</span><input type="number" id="edHz" value="${a.tone_hz}" step="1">
        <span>标签</span><input type="text" id="edLab" value="${esc(a.label)}" placeholder="卷首校准 / 换面">
      </div>
      <div style="display:flex;gap:6px;margin:6px 0">
        <button class="btn" id="edAnalyze">重新分析</button>
        <button class="btn danger" id="edDel">删除锚点</button>
      </div>
      <div class="muted" style="font-size:11px">分析窗 ${a.analysis ? a.analysis.window_start_s.toFixed(2) + "–" + a.analysis.window_end_s.toFixed(2) + "s" : "—"}
        ${a.analysis ? ` · FFT ${a.analysis.fft_size} bins · ${a.analysis.fft_ms.toFixed(0)}ms` : ""}</div>
      <div id="candBox"></div>`;
    const cb = $("#candBox");
    if (a.analysis?.flags?.length)
      cb.innerHTML += `<p style="color:var(--bad);font-size:11.5px">标记: ${a.analysis.flags.join(", ")}</p>`;
    if (cands.length) {
      cb.innerHTML += "<div class='muted' style='font-size:11px;margin:4px 0'>校准音候选（点击选用；多选并列时选择即需在问题栏附理由）：</div>" +
        cands.map((c, i) => `<div class="candrow ${i === a.chosen_index ? "sel" : ""}" data-i="${i}">
          <input type="radio" name="cand" ${i === a.chosen_index ? "checked" : ""}>
          <span class="freq">${c.hz.toFixed(2)} Hz</span>
          <span class="muted">偏差 ${fmtPct(c.ratio)}</span>
          <span class="muted">峰 ${c.level_db.toFixed(1)}dB</span>
          ${c.ambiguous ? '<span style="color:var(--warn)">候选不唯一</span>' : ""}
        </div>`).join("");
      cb.querySelectorAll(".candrow").forEach(r => r.onclick = async () => {
        a.chosen_index = +r.dataset.i;
        // choosing among ambiguous candidates produces an adopted flag
        await saveState(); renderEditor();
      });
    } else if (a.analysis) {
      cb.innerHTML += '<p style="color:var(--bad);font-size:11.5px">该分析窗内未发现校准音。</p>';
    }
    $("#edPos").onchange = async () => { const v = parseTime($("#edPos").value); if (v != null) { a.pos_s = v; a.analysis = null; await saveState(); renderAll(); } };
    $("#edSide").onchange = async () => { a.side = $("#edSide").value; await saveState(); };
    $("#edHz").onchange = async () => { a.tone_hz = +$("#edHz").value; a.analysis = null; await saveState(); renderEditor(); };
    $("#edLab").onchange = async () => { a.label = $("#edLab").value; await saveState(); };
    $("#edAnalyze").onclick = () => analyzeAnchor(a.id);
    $("#edDel").onclick = async () => {
      st.anchors = st.anchors.filter(x => x.id !== a.id);
      state0.selected = { kind: null, id: null };
      await saveState();
    };
  } else if (kind === "splice") {
    const sp = st.splices.find(x => x.id === id);
    if (!sp) { body.className = "muted"; body.textContent = "接带点已删除。"; return; }
    body.classList.remove("muted");
    body.innerHTML = `
      <div class="kv">
        <span>接带位置(秒)</span><input type="text" id="edAt" value="${sp.at_s.toFixed(3)}">
        <span>面别</span><select id="edSide"><option>A</option><option>B</option><option></option></select>
        <span>前段时码 tc_in</span><input type="text" id="edTin" value="${esc(sp.tc_in ?? "")}" placeholder="00:09:00">
        <span>后段时码 tc_out</span><input type="text" id="edTout" value="${esc(sp.tc_out ?? "")}" placeholder="00:09:02">
        <span>标签</span><input type="text" id="edLab" value="${esc(sp.label)}">
      </div>
      <div class="muted" style="font-size:11px">同一面别下两处接带占用重叠时码，将圈为阻断证据。</div>
      <button class="btn danger" id="edDel" style="margin-top:6px">删除接带点</button>`;
    $("#edSide").value = sp.side ?? "";
    const commit = async () => {
      sp.at_s = parseTime($("#edAt").value) ?? sp.at_s;
      sp.side = $("#edSide").value;
      sp.tc_in = $("#edTin").value.trim(); sp.tc_out = $("#edTout").value.trim();
      sp.tc_in_s = parseTime(sp.tc_in); sp.tc_out_s = parseTime(sp.tc_out);
      sp.label = $("#edLab").value;
      await saveState();
    };
    ["edAt", "edSide", "edTin", "edTout", "edLab"].forEach(i => $("#" + i).onchange = commit);
    $("#edDel").onclick = async () => {
      st.splices = st.splices.filter(x => x.id !== sp.id);
      state0.selected = { kind: null, id: null };
      await saveState();
    };
  } else if (kind === "zone") {
    const z = st.suspect_zones.find(x => x.id === id);
    if (!z) { body.className = "muted"; body.textContent = "掉速区已删除。"; return; }
    body.classList.remove("muted");
    body.innerHTML = `
      <div class="kv">
        <span>起(秒)</span><input type="text" id="edZ0" value="${z.start_s.toFixed(3)}">
        <span>止(秒)</span><input type="text" id="edZ1" value="${z.end_s.toFixed(3)}">
        <span>标签</span><input type="text" id="edZlab" value="${esc(z.label)}">
      </div>
      <div class="muted" style="font-size:11px">区内若没有校准锚点，区间不能确认。</div>
      <button class="btn danger" id="edZdel" style="margin-top:6px">删除掉速区</button>`;
    $("#edZ0").onchange = async () => { z.start_s = parseTime($("#edZ0").value) ?? z.start_s; await saveState(); };
    $("#edZ1").onchange = async () => { z.end_s = parseTime($("#edZ1").value) ?? z.end_s; await saveState(); };
    $("#edZlab").onchange = async () => { z.label = $("#edZlab").value; await saveState(); };
    $("#edZdel").onclick = async () => {
      st.suspect_zones = st.suspect_zones.filter(x => x.id !== z.id);
      state0.selected = { kind: null, id: null };
      await saveState();
    };
  } else {
    body.className = "muted";
    body.innerHTML = "在时间轴上选择锚点 / 接带点 / 掉速区，或用上方工具新建。<br>双击波形可查看该点静态频谱。";
  }
}

/* ------------------------------------------------------------- revisions */

async function loadRevisions(highlight) {
  const p = state0.project;
  const doc = await api("GET", `/api/projects/${p.id}/revisions`);
  state0._revs = doc.revisions;
  $("#revisionList").innerHTML = doc.revisions.length ? doc.revisions.map(r => `
    <div class="rev" data-id="${r.id}">
      <div class="rhead"><span>${r.created_at.replace("T", " ").replace("Z", "")}</span>
        <span class="${r.confirmed ? "ok" : "no"}">${r.confirmed ? "✓ 全卷确认" : `${r.confirmed_segments ?? "?"} 段确认（边缘不确认属正常）`}</span></div>
      <div style="margin:3px 0">${esc(r.note || "（无备注）")}
        <span class="pill">父修订 ${r.parent_id ? r.parent_id.slice(0, 6) : "—"}</span>
        <span class="pill">重渲染 ${r.rendered} / 复用 ${r.reused}</span></div>
      <a href="/api/projects/${p.id}/revisions/${r.id}/corrected.wav" target="_blank">校正 WAV</a>
      <a href="/api/projects/${p.id}/revisions/${r.id}/timecode_map.csv" target="_blank">时码 CSV</a>
      <a href="/api/projects/${p.id}/revisions/${r.id}/revision.json" target="_blank">修订 JSON</a>
      <button class="btn mini" data-act="listen">载入试听</button>
    </div>`).join("") : '<p class="muted">尚无修订。原始 WAV 不会被修改。</p>';
  $("#revisionList").querySelectorAll("button[data-act=listen]").forEach(b =>
    b.onclick = () => {
      $$("#sourceSwitch .seg").forEach(x => x.classList.toggle("active", x.dataset.src === "corrected"));
      state0.play.src = "corrected";
      loadCorrected(b.closest(".rev").dataset.id);
    });
  if (highlight) {
    const el = $("#revisionList").querySelector(`[data-id="${highlight}"]`);
    if (el) el.classList.add("flash");
  }
}

$("#revBtn").onclick = async () => {
  const note = prompt("修订备注（本次修改说明）：", "");
  if (note === null) return;
  const p = state0.project;
  try {
    const doc = await api("POST", `/api/projects/${p.id}/revisions`, {
      note, parent_id: state0.corrRevId || latestRevId() || null,
      state: p.state
    });
    setStatus(`修订 ${doc.revision_id.slice(0, 8)} 生成（重渲染 ${doc.incremental.segments_rendered} / 复用 ${doc.incremental.parent_segments_reused}）`, "ok");
    await openProjectRefresh();
    loadRevisions(doc.revision_id);
    if (!doc.confirmed)
      alert(`修订已生成，但并非全卷确认（确认段 ${doc.confirmed_segments}/${doc.total_segments}）。\n红色圈出的段落需要在右侧附理由采纳，或补充校准锚点。`);
  } catch (e) { alert("生成失败: " + e.message); }
};

async function openProjectRefresh() {
  await openProject(state0.project.id);
  $("#projectSelect").value = state0.project.id;
}

/* ---------------------------------------------------------------- render */

function renderAll() {
  if (!state0.project) return;
  drawWave();
  drawOverlay();
  drawRuler();
  renderAnchorTable();
  renderIssues();
  renderEditor();
  $("#segStart").value = state0.selection.start.toFixed(2);
  $("#segEnd").value = state0.selection.end.toFixed(2);
}

/* keyboard: space toggles, zoom */
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (e.code === "Space") { e.preventDefault(); $("#playBtn").click(); }
});

/* boot */
(async function init() {
  await loadProjects();
  setStatus("请新建或选择项目", "");
})();
