/* ============================================================================
   AIsys · Studio V2 Frontend
   Pure Claude-Minimal Composer · Editable Dual-Model Logic Graph · Module Control
   ============================================================================ */

const CSRF_TOKEN = document.querySelector('meta[name="aisys-csrf"]')?.content || "";

const STAGE_META = {
  proposing: { step: 1, label: "阶段 1 · 独立初案" },
  critiquing: { step: 2, label: "阶段 2 · 交叉互审与分工" },
  deciding: { step: 3, label: "阶段 3 · 你的拍板" },
  merging: { step: 3, label: "阶段 3 · 融合微调中" },
  executing: { step: 4, label: "阶段 4 · 互斥协同执行" },
  reviewing: { step: 5, label: "阶段 5 · 互相监督审查" },
  completed: { step: 5, label: "已完成交付" },
  failed: { step: 0, label: "执行失败" },
  cancelled: { step: 0, label: "已取消" },
};

const CAT_LABELS = {
  codex: "CODEX",
  gemini: "GEMINI",
  engine: "KERNEL",
  lock: "LOCK",
  workspace: "SANDBOX",
};

const NODE_STATUS_LABELS = {
  idle: ["待命", "#7f978e"],
  waiting: ["等待触发", "#08f1d4"],
  running: ["运行中", "#00f59b"],
  completed: ["已完成", "#34d399"],
  rework: ["定向返工", "#fbbf24"],
  skipped: ["已跳过", "#566d64"],
  failed: ["异常", "#fb7185"],
};

const MOD_STATUS_LABELS = {
  running: ["运行中", "pill-codex"],
  ready: ["已就绪", "pill-gemini"],
  stopped: ["已暂停", "pill"],
  degraded: ["需注意", "pill-warn"],
  unavailable: ["不可用", "pill-err"],
};

// 12-node interactive architecture coordinates (Gemini top · Shared center · Codex bottom)
const GRAPH_NODES = [
  // Shared row (y=174)
  { id: "input_dispatch", label: "共享意图与全局约定", owner: "shared", x: 18, y: 174, w: 148, h: 66 },
  { id: "division_gate", label: "防重叠锁 & 拍板融合", owner: "shared", x: 374, y: 174, w: 158, h: 66 },
  { id: "writelock_sandbox", label: "硬写锁 & Seatbelt", owner: "shared", x: 564, y: 174, w: 152, h: 66 },
  { id: "final_delivery", label: "汇编交付归档", owner: "shared", x: 748, y: 174, w: 138, h: 66 },
  // Gemini track (y=36)
  { id: "gemini_propose", label: "Gemini 独立初案", owner: "gemini", x: 196, y: 36, w: 148, h: 64 },
  { id: "gemini_critique", label: "Gemini 互审分工", owner: "gemini", x: 378, y: 36, w: 150, h: 64 },
  { id: "gemini_exec", label: "Gemini 独占执行", owner: "gemini", x: 564, y: 36, w: 152, h: 64 },
  { id: "gemini_review", label: "Gemini 审查 Codex", owner: "gemini", x: 748, y: 36, w: 138, h: 64 },
  // Codex track (y=312)
  { id: "codex_propose", label: "Codex 独立初案", owner: "codex", x: 196, y: 312, w: 148, h: 64 },
  { id: "codex_critique", label: "Codex 互审分工", owner: "codex", x: 378, y: 312, w: 150, h: 64 },
  { id: "codex_exec", label: "Codex 独占执行", owner: "codex", x: 564, y: 312, w: 152, h: 64 },
  { id: "codex_review", label: "Codex 审查 Gemini", owner: "codex", x: 748, y: 312, w: 138, h: 64 },
];

const state = {
  view: "studio",
  mode: "dev",
  sessions: [],
  currentId: null,
  session: null,
  events: [],
  expandedEventIds: new Set(),
  kernelOpen: false,
  kernelFilter: "all",
  pipeline: null,
  pipelineDraft: null,
  profiles: [],
  selectedNodeId: "input_dispatch",
  modulesSnapshot: null,
  decisionChoice: "gemini",
  fusionBase: "gemini",
  studyGuideCache: null,
  diffCache: null,
  sse: null,
  ambientBurst: 0,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function fmtTokens(n) {
  const num = Number(n || 0);
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}k`;
  return String(num);
}

function fmtTimeMs(ts) {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  const pad = (x, len = 2) => String(x).padStart(len, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

function fmtRelTime(ts) {
  if (!ts) return "";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return `${diff}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (method !== "GET" && method !== "HEAD") {
    headers["X-AIsys-Token"] = CSRF_TOKEN;
    if (options.body !== undefined && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
  }
  const resp = await fetch(path, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!resp.ok) {
    const msg = (data && typeof data === "object" && data.detail) || `请求失败 (${resp.status})`;
    throw new Error(msg);
  }
  return data;
}

function toast(message, kind = "info") {
  const stack = $("toast-stack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "toast-error" : ""}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(-6px)";
    el.style.transition = "all 0.25s ease";
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

/* ==================== SAFE EDITORIAL MARKDOWN + TOC ==================== */
function renderMarkdown(raw, { collectToc = false } = {}) {
  const src = String(raw || "").replace(/\r\n/g, "\n");
  const toc = [];
  const codeBlocks = [];

  const withPlaceholders = src.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_, lang, body) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || "", body });
    return `@@CODEBLOCK_${idx}@@`;
  });

  const inline = (line) => {
    let out = esc(line);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    return out;
  };

  const lines = withPlaceholders.split("\n");
  const html = [];
  let inUl = false;
  let inOl = false;
  let tableBuf = [];

  const closeLists = () => {
    if (inUl) { html.push("</ul>"); inUl = false; }
    if (inOl) { html.push("</ol>"); inOl = false; }
  };

  const flushTable = () => {
    if (!tableBuf.length) return;
    const rows = tableBuf.map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
    const bodyRows = rows.filter((_, i) => !(i === 1 && rows[1].every((c) => /^:?-+:?$/.test(c))));
    if (bodyRows.length) {
      html.push("<table><thead><tr>");
      bodyRows[0].forEach((c) => html.push(`<th>${inline(c)}</th>`));
      html.push("</tr></thead><tbody>");
      bodyRows.slice(1).forEach((row) => {
        html.push("<tr>");
        row.forEach((c) => html.push(`<td>${inline(c)}</td>`));
        html.push("</tr>");
      });
      html.push("</tbody></table>");
    }
    tableBuf = [];
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (/^\|.*\|$/.test(trimmed)) {
      closeLists();
      tableBuf.push(trimmed);
      continue;
    }
    flushTable();

    const codeMatch = /^@@CODEBLOCK_(\d+)@@$/.exec(trimmed);
    if (codeMatch) {
      closeLists();
      const blk = codeBlocks[Number(codeMatch[1])];
      html.push(`<pre><code class="lang-${esc(blk.lang)}">${esc(blk.body)}</code></pre>`);
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      closeLists();
      const lvl = heading[1].length;
      const text = heading[2].trim();
      const hid = `h-${toc.length}-${text.slice(0, 18).replace(/[^a-zA-Z0-9\u4e00-\u9fff]+/g, "-")}`;
      if (collectToc && lvl >= 2 && lvl <= 3) toc.push({ id: hid, level: lvl, text });
      html.push(`<h${lvl} id="${esc(hid)}">${inline(text)}</h${lvl}>`);
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      closeLists();
      html.push(`<blockquote>${inline(trimmed.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }
    if (/^[-*]\s+/.test(trimmed)) {
      if (inOl) { html.push("</ol>"); inOl = false; }
      if (!inUl) { html.push("<ul>"); inUl = true; }
      html.push(`<li>${inline(trimmed.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^\d+\.\s+/.test(trimmed)) {
      if (inUl) { html.push("</ul>"); inUl = false; }
      if (!inOl) { html.push("<ol>"); inOl = true; }
      html.push(`<li>${inline(trimmed.replace(/^\d+\.\s+/, ""))}</li>`);
      continue;
    }
    if (!trimmed) { closeLists(); continue; }
    closeLists();
    html.push(`<p>${inline(trimmed)}</p>`);
  }

  closeLists();
  flushTable();
  return { html: html.join("\n"), toc };
}

/* ==================== HIGH-DENSITY PARTICLES + FLOWING GREEN LASER CIRCUITS ==================== */
function pulseAtmosphere(strength = 1.1) {
  state.ambientBurst = Math.min(2.8, state.ambientBurst + strength);
}

function initAmbientCanvas() {
  const canvas = $("ambient-canvas");
  if (!canvas) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  const pointer = { x: -9999, y: -9999, active: false };
  const ripples = [];
  let tracks = [];

  const buildCircuitTracks = () => {
    tracks = [];
    const lanes = 11;
    for (let i = 0; i < lanes; i++) {
      const yBase = ((i + 0.6) / lanes) * height;
      const kinkX1 = width * (0.18 + ((i * 19) % 30) / 100);
      const kinkX2 = width * (0.56 + ((i * 23) % 28) / 100);
      const step = (i % 2 === 0 ? 1 : -1) * (24 + (i % 3) * 16);
      const pts = [
        { x: -40, y: yBase },
        { x: kinkX1, y: yBase },
        { x: kinkX1 + Math.abs(step), y: yBase + step },
        { x: kinkX2, y: yBase + step },
        { x: kinkX2 + Math.abs(step), y: yBase },
        { x: width + 40, y: yBase },
      ];
      // Precompute segment lengths
      const segs = [];
      let totalLen = 0;
      for (let k = 0; k < pts.length - 1; k++) {
        const len = Math.hypot(pts[k + 1].x - pts[k].x, pts[k + 1].y - pts[k].y);
        segs.push({ a: pts[k], b: pts[k + 1], start: totalLen, len });
        totalLen += len;
      }
      tracks.push({
        pts,
        segs,
        totalLen,
        pos: Math.random() * totalLen,
        speed: 75 + Math.random() * 115,
        beamLen: 90 + Math.random() * 110,
        color: i % 2 === 0 ? "8, 241, 212" : "0, 245, 155",
      });
    }
  };

  const resize = () => {
    width = window.innerWidth;
    height = window.innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    buildCircuitTracks();
  };
  resize();
  window.addEventListener("resize", resize);

  window.addEventListener("pointermove", (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.active = true;
  }, { passive: true });
  window.addEventListener("pointerleave", () => { pointer.active = false; });

  // Denser multi-depth emerald-mint particles (~108)
  const count = 108;
  const particles = Array.from({ length: count }, (_, i) => {
    const depth = i < 24 ? 1.45 : i < 72 ? 1.0 : 0.55;
    return {
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.34 * depth,
      vy: ((Math.random() - 0.5) * 0.30 - 0.05) * depth,
      r: (0.95 + Math.random() * 1.9) * depth,
      depth,
      hue: i % 3 === 0 ? "8, 241, 212" : i % 3 === 1 ? "0, 245, 155" : "52, 211, 153",
      phase: Math.random() * Math.PI * 2,
    };
  });

  const sampleTrackPoint = (track, dist) => {
    const clamped = Math.max(0, Math.min(track.totalLen, dist));
    for (const s of track.segs) {
      if (clamped <= s.start + s.len) {
        const t = s.len ? (clamped - s.start) / s.len : 0;
        return { x: s.a.x + (s.b.x - s.a.x) * t, y: s.a.y + (s.b.y - s.a.y) * t };
      }
    }
    return track.pts[track.pts.length - 1];
  };

  let lastFrame = performance.now();
  const loop = (now) => {
    const dt = Math.min(0.05, (now - lastFrame) / 1000);
    lastFrame = now;
    ctx.clearRect(0, 0, width, height);

    const active = Boolean(state.session?.codex_busy || state.session?.active_process);
    document.body.classList.toggle("engine-active", active);

    if (state.ambientBurst > 0.05) {
      if (ripples.length < 4 && state.ambientBurst > 0.65) {
        ripples.push({ x: width * 0.5, y: height * 0.32, r: 22, alpha: 0.34 });
      }
      state.ambientBurst = Math.max(0, state.ambientBurst - dt * 1.3);
    }

    // 1. Subtle circuit rails + bright flowing green laser photons!
    const laserBoost = active ? 1.75 : 1 + state.ambientBurst * 0.55;
    for (const tr of tracks) {
      // Base hairline circuit trace
      ctx.beginPath();
      ctx.moveTo(tr.pts[0].x, tr.pts[0].y);
      for (let k = 1; k < tr.pts.length; k++) ctx.lineTo(tr.pts[k].x, tr.pts[k].y);
      ctx.strokeStyle = `rgba(${tr.color}, 0.055)`;
      ctx.lineWidth = 1;
      ctx.stroke();

      // Flowing laser beam segment along the stepped circuit trace
      tr.pos = (tr.pos + tr.speed * laserBoost * dt) % (tr.totalLen + tr.beamLen);
      const head = tr.pos;
      const tail = tr.pos - tr.beamLen;
      const pTail = sampleTrackPoint(tr, tail);
      const pHead = sampleTrackPoint(tr, head);

      const grad = ctx.createLinearGradient(pTail.x, pTail.y, pHead.x, pHead.y);
      grad.addColorStop(0, `rgba(${tr.color}, 0)`);
      grad.addColorStop(0.65, `rgba(${tr.color}, ${active ? 0.42 : 0.28})`);
      grad.addColorStop(1, `rgba(218, 255, 246, ${active ? 0.85 : 0.62})`);

      ctx.beginPath();
      const steps = 10;
      for (let sIdx = 0; sIdx <= steps; sIdx++) {
        const pt = sampleTrackPoint(tr, tail + (tr.beamLen * sIdx) / steps);
        if (sIdx === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
      }
      ctx.strokeStyle = grad;
      ctx.lineWidth = active ? 2.1 : 1.5;
      ctx.stroke();

      // Glowing photon tip
      if (head > 0 && head < tr.totalLen) {
        ctx.beginPath();
        ctx.arc(pHead.x, pHead.y, active ? 2.6 : 2.0, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(220, 255, 248, ${active ? 0.92 : 0.72})`;
        ctx.fill();
      }
    }

    // 2. Energy ripples on live SSE events
    for (let i = ripples.length - 1; i >= 0; i--) {
      const rp = ripples[i];
      rp.r += dt * 240;
      rp.alpha -= dt * 0.24;
      if (rp.alpha <= 0) { ripples.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.arc(rp.x, rp.y, rp.r, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(8, 241, 212, ${rp.alpha.toFixed(3)})`;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }

    // 3. Dense luminous emerald-mint spores + synaptic network
    const speedBoost = active ? 1.8 : 1 + state.ambientBurst * 0.55;
    for (const p of particles) {
      p.phase += dt * (1.35 + (active ? 0.9 : 0));
      p.x += p.vx * speedBoost;
      p.y += p.vy * speedBoost;

      if (pointer.active) {
        const dx = pointer.x - p.x;
        const dy = pointer.y - p.y;
        const d2 = dx * dx + dy * dy;
        if (d2 > 1400 && d2 < 42000) {
          p.x += dx * 0.0024 * p.depth;
          p.y += dy * 0.0024 * p.depth;
        }
      }

      if (p.x < -18) p.x = width + 18;
      if (p.x > width + 18) p.x = -18;
      if (p.y < -18) p.y = height + 18;
      if (p.y > height + 18) p.y = -18;

      const alpha = (0.28 + 0.34 * Math.sin(p.phase)) * (active ? 1.28 : 1);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${p.hue}, ${Math.min(0.92, alpha).toFixed(3)})`;
      ctx.fill();
    }

    const maxDist = active ? 132 : 114;
    const maxDist2 = maxDist * maxDist;
    ctx.lineWidth = 0.72;
    for (let i = 0; i < count; i++) {
      const a = particles[i];
      for (let j = i + 1; j < count; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < maxDist2) {
          const alpha = (1 - Math.sqrt(d2) / maxDist) * (active ? 0.22 : 0.13);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(8, 241, 212, ${alpha.toFixed(3)})`;
          ctx.stroke();
        }
      }
    }

    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

function initSpotlightCards() {
  document.addEventListener("pointermove", (e) => {
    const card = e.target.closest?.(".spotlight-card");
    if (!card) return;
    const rect = card.getBoundingClientRect();
    card.style.setProperty("--mx", `${(e.clientX - rect.left).toFixed(0)}px`);
    card.style.setProperty("--my", `${(e.clientY - rect.top).toFixed(0)}px`);
  }, { passive: true });
}

/* ==================== 3-VIEW NAVIGATION ==================== */
function switchView(viewName) {
  state.view = viewName;
  document.querySelectorAll("#main-nav .nav-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === viewName);
  });
  ["studio", "logic", "modules"].forEach((v) => {
    $(`view-${v}`)?.classList.toggle("hidden", v !== viewName);
  });
  if (viewName === "logic") {
    renderLogicView();
  } else if (viewName === "modules") {
    renderModulesView();
  }
  updateKernelDockVisibility();
}

function updateKernelDockVisibility() {
  const dock = $("kernel-dock");
  if (!dock) return;
  const shouldShow = Boolean(state.currentId && state.session);
  dock.classList.toggle("hidden", !shouldShow);
}

/* ==================== PIPELINE CONFIG & MODULE LOADING ==================== */
async function loadPipeline() {
  try {
    const data = await api("/api/pipeline");
    state.pipeline = data.config;
    state.pipelineDraft = deepClone(data.config);
    state.profiles = data.profiles || [];
    renderComposerActiveModels();
    updateBrandProfileTag();
    if (state.view === "logic") renderLogicView();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadModules() {
  try {
    state.modulesSnapshot = await api("/api/modules");
    renderTopEnginePills();
    renderModulesBadge();
    if (state.view === "modules") renderModulesView();
  } catch {
    // non-fatal
  }
}

function updateBrandProfileTag() {
  const cfg = state.pipeline;
  if (!cfg) return;
  const prof = state.profiles.find((p) => p.id === cfg.shared.profile_name);
  const label = prof ? prof.label : cfg.shared.profile_name;
  const tag = $("brand-profile-tag");
  if (tag) tag.textContent = label;
  const compLbl = $("composer-profile-label");
  if (compLbl) compLbl.textContent = `引擎逻辑 · ${label}`;
}

function renderComposerActiveModels() {
  const el = $("composer-active-models");
  const cfg = state.pipeline;
  if (!el || !cfg) return;
  const gOn = cfg.modules.gemini_enabled;
  const cOn = cfg.modules.codex_enabled;
  const stageFlags = [
    cfg.stages.enable_critique ? "互审" : "跳过互审",
    cfg.stages.require_user_decision ? "人工拍板" : "自动锁定",
    cfg.stages.enable_mutual_review ? `${cfg.stages.max_review_rounds} 轮监督` : "免复审",
  ].join(" · ");

  el.innerHTML = `
    <span class="meta-tag"><span class="mini-dot ${gOn ? "mint" : "off"}"></span>Gemini ${gOn ? "已启用" : "已暂停"}</span>
    <span class="meta-tag"><span class="mini-dot ${cOn ? "emerald" : "off"}"></span>Codex ${cOn ? (cfg.codex.model || "已启用") : "已暂停"}</span>
    <span class="meta-tag" style="color:var(--text-faint)">${esc(stageFlags)}</span>
  `;
}

function renderModulesBadge() {
  const badge = $("nav-modules-badge");
  const snap = state.modulesSnapshot;
  if (!badge || !snap) return;
  badge.textContent = `${snap.summary.online}/${snap.summary.total}`;
}

function renderTopEnginePills() {
  const host = $("backend-pills");
  const snap = state.modulesSnapshot;
  if (!host || !snap) return;
  const mods = Object.fromEntries((snap.modules || []).map((m) => [m.id, m]));
  const gm = mods.antigravity_bridge || {};
  const cm = mods.codex_engine || {};
  const codexLive = cm.status === "running" || Boolean(state.session?.codex_busy);
  const geminiWaiting = Boolean(state.session?.next_action?.pending_gemini?.length);

  host.innerHTML = `
    <button type="button" class="engine-dot-pill ${gm.enabled ? "is-mint" : "is-off"} ${geminiWaiting ? "is-running" : ""}" data-jump-mod="antigravity_bridge" title="${esc(gm.detail || "")}">
      <span class="pulse-dot"></span>G ${gm.enabled ? (geminiWaiting ? "待确认" : "ON") : "OFF"}
    </button>
    <button type="button" class="engine-dot-pill ${cm.enabled ? "is-on" : "is-off"} ${codexLive ? "is-running" : ""}" data-jump-mod="codex_engine" title="${esc(cm.detail || "")}">
      <span class="pulse-dot"></span>C ${cm.enabled ? (codexLive ? "RUN" : "ON") : "OFF"}
    </button>
  `;
}

/* ==================== ULTRA-MINIMAL ANTIGRAVITY JUMP (ONLY 1 CONFIRMATION) ==================== */
async function jumpToAntigravity(triggerOverride = null) {
  const trigger = triggerOverride || state.pipeline?.gemini?.trigger_command || "/aisys";
  try {
    await navigator.clipboard.writeText(trigger);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = trigger;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  try {
    const res = await api("/api/focus-antigravity", { method: "POST" });
    pulseAtmosphere(1.2);
    if (res.focused) {
      toast(`已写入剪贴板 ${res.trigger || trigger} 并切到 ${res.app} — 直接 ⌘V 回车确认即可`);
    } else {
      toast(`已复制 ${res.trigger || trigger}，切回 Antigravity 直接 ⌘V 回车确认`);
    }
  } catch {
    toast(`已复制 ${trigger}，切回 Antigravity 直接 ⌘V 回车确认`);
  }
}

/* ==================== VIEW 2: INTERACTIVE BACKEND LOGIC & NODE EDITOR ==================== */
function getLiveNodeData(nodeId) {
  const sessionNodes = state.session?.nodes;
  if (sessionNodes && sessionNodes[nodeId]) return sessionNodes[nodeId];

  const cfg = state.pipelineDraft || state.pipeline;
  if (!cfg) return { id: nodeId, status: "idle", enabled: true, summary: "配置待载入" };

  const map = {
    input_dispatch: { enabled: true, status: "completed", summary: `预设 ${cfg.shared.profile_name}` },
    gemini_propose: { enabled: cfg.modules.gemini_enabled, status: cfg.modules.gemini_enabled ? "idle" : "skipped", summary: cfg.gemini.role_title },
    codex_propose: { enabled: cfg.modules.codex_enabled, status: cfg.modules.codex_enabled ? "idle" : "skipped", summary: `${cfg.codex.model || "默认模型"} · ${cfg.codex.timeout_s}s` },
    gemini_critique: { enabled: cfg.modules.gemini_enabled && cfg.stages.enable_critique, status: (cfg.modules.gemini_enabled && cfg.stages.enable_critique) ? "idle" : "skipped", summary: cfg.stages.enable_critique ? "审视 Codex 并划分独占路径" : "阶段 2 已关闭" },
    codex_critique: { enabled: cfg.modules.codex_enabled && cfg.stages.enable_critique, status: (cfg.modules.codex_enabled && cfg.stages.enable_critique) ? "idle" : "skipped", summary: cfg.stages.enable_critique ? "审视 Gemini 并划分独占路径" : "阶段 2 已关闭" },
    division_gate: { enabled: true, status: "idle", summary: cfg.stages.require_user_decision ? `人工拍板/融合 · 最多 ${cfg.stages.max_subtasks} 任务` : `自动锁定 (${cfg.stages.default_winner})` },
    gemini_exec: { enabled: cfg.modules.gemini_enabled, status: cfg.modules.gemini_enabled ? "idle" : "skipped", summary: `触发词 ${cfg.gemini.trigger_command}` },
    codex_exec: { enabled: cfg.modules.codex_enabled, status: cfg.modules.codex_enabled ? "idle" : "skipped", summary: cfg.stages.auto_start_codex ? "自动后台执行" : "手动触发执行" },
    writelock_sandbox: { enabled: true, status: "completed", summary: cfg.modules.sandbox_checks_enabled ? "硬写锁 + Seatbelt 沙箱" : "硬写锁生效（命令检查已关）" },
    gemini_review: { enabled: cfg.modules.gemini_enabled && cfg.stages.enable_mutual_review, status: (cfg.modules.gemini_enabled && cfg.stages.enable_mutual_review) ? "idle" : "skipped", summary: cfg.stages.enable_mutual_review ? `最多 ${cfg.stages.max_review_rounds} 轮返工` : "阶段 5 已关闭" },
    codex_review: { enabled: cfg.modules.codex_enabled && cfg.stages.enable_mutual_review, status: (cfg.modules.codex_enabled && cfg.stages.enable_mutual_review) ? "idle" : "skipped", summary: cfg.stages.enable_mutual_review ? `最多 ${cfg.stages.max_review_rounds} 轮返工` : "阶段 5 已关闭" },
    final_delivery: { enabled: true, status: "idle", summary: "Study Guide Markdown / Git Diff" },
  };
  return { id: nodeId, ...(map[nodeId] || { enabled: true, status: "idle", summary: "" }) };
}

function renderLogicView() {
  const cfg = state.pipelineDraft || state.pipeline;
  if (!cfg) return;

  // 1. Profile selector pills
  const pHost = $("profile-pills");
  if (pHost) {
    pHost.innerHTML = state.profiles.map((p) => `
      <button type="button" class="profile-pill ${cfg.shared.profile_name === p.id ? "active" : ""}" data-profile="${esc(p.id)}" title="${esc(p.description)}">
        ${esc(p.label)}
      </button>
    `).join("");
  }

  // 2. Quick stage & model switches bar
  const sBar = $("logic-switches-bar");
  if (sBar) {
    const sw = (key, label, checked) => `
      <label class="inline-toggle">
        <button type="button" class="toggle-switch" role="switch" aria-checked="${checked}" data-cfg-toggle="${key}"></button>
        <span>${label}</span>
      </label>
    `;
    sBar.innerHTML = `
      <div class="switch-group">
        ${sw("modules.gemini_enabled", "Gemini 轨道", cfg.modules.gemini_enabled)}
        ${sw("modules.codex_enabled", "Codex 轨道", cfg.modules.codex_enabled)}
      </div>
      <div class="switch-group">
        ${sw("stages.enable_critique", "阶段 2 交叉互审", cfg.stages.enable_critique)}
        ${sw("stages.require_user_decision", "阶段 3 人工拍板", cfg.stages.require_user_decision)}
        ${sw("stages.enable_mutual_review", "阶段 5 互相监督", cfg.stages.enable_mutual_review)}
        ${sw("stages.auto_start_codex", "自动拉起 Codex", cfg.stages.auto_start_codex)}
      </div>
    `;
  }

  const bindEl = $("logic-session-bind");
  if (bindEl) {
    bindEl.textContent = state.session
      ? `实时绑定会话 #${state.session.id.slice(0, 8)} · ${STAGE_META[state.session.stage]?.label || state.session.stage}`
      : `当前预设：${cfg.shared.profile_name} · 点击任意节点编辑后端逻辑`;
  }

  // 3. Interactive Flowing-Laser Dual-Model + Shared SVG Canvas
  renderLogicGraph(cfg);
  // 4. Right-Side Node Inspector & Live Editor
  renderNodeInspector(cfg);
}

function renderLogicGraph(cfg) {
  const wrap = $("logic-graph-wrap");
  if (!wrap) return;

  const gOn = cfg.modules.gemini_enabled;
  const cOn = cfg.modules.codex_enabled;
  const critOn = cfg.stages.enable_critique;
  const revOn = cfg.stages.enable_mutual_review;
  const reworkOn = revOn && cfg.stages.max_review_rounds > 0;

  const edge = (d, cls, active = true) => `
    <path class="laser-base" d="${d}"/>
    <path class="laser-flow ${cls} ${active ? "" : "is-dim"}" d="${d}"/>
  `;

  const edgesSvg = [
    // Input dispatch -> Gemini & Codex proposals
    edge("M166 196 C182 196, 178 68, 196 68", "lane-gemini", gOn),
    edge("M166 218 C182 218, 178 344, 196 344", "lane-codex", cOn),
    // Stage 1 -> Stage 2
    edge("M344 68 L378 68", "lane-gemini", gOn && critOn),
    edge("M344 344 L378 344", "lane-codex", cOn && critOn),
    // Cross-critique X exchange between Gemini & Codex
    edge("M336 100 C356 174, 366 238, 386 312", "lane-gemini", gOn && cOn && critOn),
    edge("M336 312 C356 238, 366 174, 386 100", "lane-codex", gOn && cOn && critOn),
    // Stage 2 (or Stage 1 if skipped) -> Shared Division Gate
    edge("M453 100 L453 174", "lane-gemini", gOn),
    edge("M453 312 L453 240", "lane-codex", cOn),
    // Division Gate -> Stage 4 Gemini & Codex Exec
    edge("M532 196 C548 196, 546 68, 564 68", "lane-gemini", gOn),
    edge("M532 218 C548 218, 546 344, 564 344", "lane-codex", cOn),
    // Stage 4 Exec <-> Shared Write-Lock & Seatbelt Sandbox
    edge("M640 100 L640 174", "lane-shared", gOn),
    edge("M640 312 L640 240", "lane-shared", cOn),
    // Stage 4 -> Stage 5 Cross-Review (Gemini audits Codex, Codex audits Gemini)
    edge("M716 68 L748 68", "lane-gemini", gOn && revOn),
    edge("M716 344 L748 344", "lane-codex", cOn && revOn),
    // Write-Lock / Reviews -> Final Delivery
    edge("M716 207 L748 207", "lane-shared", true),
    edge("M817 100 L817 174", "lane-gemini", gOn && revOn),
    edge("M817 312 L817 240", "lane-codex", cOn && revOn),
    // Rework loop arcs
    edge("M817 36 C817 8, 640 8, 640 36", "lane-rework", reworkOn && gOn),
    edge("M817 376 C817 404, 640 404, 640 376", "lane-rework", reworkOn && cOn),
  ].join("");

  const nodesSvg = GRAPH_NODES.map((n) => {
    const live = getLiveNodeData(n.id);
    const [stLbl, stColor] = NODE_STATUS_LABELS[live.status] || [live.status, "#7f978e"];
    const selected = state.selectedNodeId === n.id;
    const laneAccent = n.owner === "gemini" ? "#08f1d4" : n.owner === "codex" ? "#00f59b" : "#a7f3d0";
    const subLine = live.pid
      ? `PID ${live.pid} · ${Number(live.duration_s || 0).toFixed(1)}s`
      : live.tokens
        ? `${fmtTokens(live.tokens)} tok · ${Number(live.duration_s || 0).toFixed(1)}s`
        : (live.summary || "").slice(0, 17);

    return `
      <g class="gnode st-${esc(live.status)} ${selected ? "is-selected" : ""}" data-select-node="${esc(n.id)}">
        <rect class="gnode-box" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="12"/>
        <rect x="${n.x}" y="${n.y + 10}" width="3.5" height="${n.h - 20}" rx="2" fill="${laneAccent}" opacity="${live.enabled === false ? "0.25" : "0.95"}"/>
        <circle cx="${n.x + n.w - 14}" cy="${n.y + 15}" r="4" fill="${stColor}"/>
        <text class="gnode-title" x="${n.x + 13}" y="${n.y + 24}">${esc(n.label)}</text>
        <text class="gnode-status" x="${n.x + 13}" y="${n.y + 40}" fill="${stColor}">${esc(stLbl)}</text>
        <text class="gnode-sub" x="${n.x + 13}" y="${n.y + 54}">${esc(subLine)}</text>
      </g>
    `;
  }).join("");

  wrap.innerHTML = `
    <svg class="logic-svg" viewBox="0 0 906 416" aria-label="双模型后端执行逻辑图">
      <text class="gnode-lane-lbl" x="18" y="22">GEMINI 轨道 · ANTIGRAVITY</text>
      <text class="gnode-lane-lbl" x="18" y="160">SHARED 共享仲裁与守卫轨道</text>
      <text class="gnode-lane-lbl" x="18" y="298">CODEX 轨道 · ISOLATED CLI</text>
      ${edgesSvg}
      ${nodesSvg}
    </svg>
  `;
}

function renderNodeInspector(cfg) {
  const host = $("logic-inspector-body");
  if (!host) return;
  const nodeId = state.selectedNodeId || "input_dispatch";
  const nodeDef = GRAPH_NODES.find((n) => n.id === nodeId) || GRAPH_NODES[0];
  const live = getLiveNodeData(nodeId);
  const [stLbl] = NODE_STATUS_LABELS[live.status] || [live.status];

  const ownerBadge = nodeDef.owner === "gemini"
    ? '<span class="pill pill-gemini">Gemini 专属配置</span>'
    : nodeDef.owner === "codex"
      ? '<span class="pill pill-codex">Codex 专属配置</span>'
      : '<span class="pill pill-ok">双方共享配置</span>';

  const liveBlock = `
    <div class="live-node-telemetry">
      <div>状态：<strong>${esc(stLbl)}</strong> · 启用：${live.enabled === false ? "OFF（跳过）" : "ON"}</div>
      ${live.pid ? `<div>实时进程：PID ${esc(live.pid)} · 耗时 ${Number(live.duration_s || 0).toFixed(1)}s</div>` : ""}
      ${live.tokens ? `<div>真实消耗：${fmtTokens(live.tokens)} tokens · 累计 ${Number(live.duration_s || 0).toFixed(1)}s</div>` : ""}
      <div>摘要：${esc(live.summary || "等待任务运行")}</div>
    </div>
  `;

  let fieldsHtml = "";

  if (nodeId === "input_dispatch") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">双方共享全局约定（同时注入 Gemini 与 Codex）<small>shared.global_directives</small></label>
        <textarea class="editor-textarea" rows="4" data-bind="shared.global_directives">${esc(cfg.shared.global_directives)}</textarea>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">无重叠分工原则（注入阶段 2 & 3）<small>shared.division_policy</small></label>
        <textarea class="editor-textarea" rows="4" data-bind="shared.division_policy">${esc(cfg.shared.division_policy)}</textarea>
      </div>
    `;
  } else if (nodeId === "gemini_propose") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">Gemini 角色定位<small>gemini.role_title</small></label>
        <input class="editor-input" data-bind="gemini.role_title" value="${esc(cfg.gemini.role_title)}" />
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Gemini 专属系统人设（所有阶段生效）<small>gemini.system_persona</small></label>
        <textarea class="editor-textarea" rows="3" data-bind="gemini.system_persona">${esc(cfg.gemini.system_persona)}</textarea>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">阶段 1 提案 Prompt 模板<small>{task} {mode_note} {repo_overview}</small></label>
        <textarea class="editor-textarea" rows="6" data-bind="gemini.proposal_prompt">${esc(cfg.gemini.proposal_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "codex_propose") {
    fieldsHtml = `
      <div class="inspector-row-2">
        <div class="inspector-field">
          <label class="inspector-label">指定 Codex 模型（留空=默认）</label>
          <input class="editor-input" placeholder="如 gpt-5-codex" data-bind="codex.model" value="${esc(cfg.codex.model)}" />
        </div>
        <div class="inspector-field">
          <label class="inspector-label">单步超时（秒）</label>
          <input class="editor-input" type="number" min="30" max="600" data-bind="codex.timeout_s" data-type="int" value="${esc(cfg.codex.timeout_s)}" />
        </div>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Codex 专属系统人设（所有阶段生效）<small>codex.system_persona</small></label>
        <textarea class="editor-textarea" rows="3" data-bind="codex.system_persona">${esc(cfg.codex.system_persona)}</textarea>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">阶段 1 提案 Prompt 模板<small>{task} {mode_note} {repo_overview}</small></label>
        <textarea class="editor-textarea" rows="6" data-bind="codex.proposal_prompt">${esc(cfg.codex.proposal_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "gemini_critique") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">Gemini 阶段 2 交叉互审与分工 Prompt<small>{own_json} {peer_json} {division_policy}</small></label>
        <textarea class="editor-textarea" rows="8" data-bind="gemini.critique_prompt">${esc(cfg.gemini.critique_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "codex_critique") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">Codex 阶段 2 交叉互审与分工 Prompt<small>{own_json} {peer_json} {division_policy}</small></label>
        <textarea class="editor-textarea" rows="8" data-bind="codex.critique_prompt">${esc(cfg.codex.critique_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "division_gate") {
    fieldsHtml = `
      <div class="inspector-row-2">
        <div class="inspector-field">
          <label class="inspector-label">自动锁定默认胜出方</label>
          <select class="editor-input" data-bind="stages.default_winner">
            <option value="gemini"${cfg.stages.default_winner === "gemini" ? " selected" : ""}>Gemini</option>
            <option value="codex"${cfg.stages.default_winner === "codex" ? " selected" : ""}>Codex</option>
          </select>
        </div>
        <div class="inspector-field">
          <label class="inspector-label">最大子任务数 (1–12)</label>
          <input class="editor-input" type="number" min="1" max="12" data-bind="stages.max_subtasks" data-type="int" value="${esc(cfg.stages.max_subtasks)}" />
        </div>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">阶段 3 融合微调 Prompt 模板<small>{fusion_notes} {gemini_refined} {codex_refined}</small></label>
        <textarea class="editor-textarea" rows="7" data-bind="gemini.merge_prompt">${esc(cfg.gemini.merge_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "gemini_exec") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">Antigravity 极简触发指令<small>自动写入剪贴板</small></label>
        <input class="editor-input" data-bind="gemini.trigger_command" value="${esc(cfg.gemini.trigger_command)}" />
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Gemini 阶段 4 独占执行 Prompt<small>{task_id} {owned_paths} {feedback_block}</small></label>
        <textarea class="editor-textarea" rows="7" data-bind="gemini.subtask_prompt">${esc(cfg.gemini.subtask_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "codex_exec") {
    fieldsHtml = `
      <div class="inspector-row-2">
        <div class="inspector-field">
          <label class="inspector-label">Codex 角色定位</label>
          <input class="editor-input" data-bind="codex.role_title" value="${esc(cfg.codex.role_title)}" />
        </div>
        <div class="inspector-field">
          <label class="inspector-label">单步超时（秒）</label>
          <input class="editor-input" type="number" min="30" max="600" data-bind="codex.timeout_s" data-type="int" value="${esc(cfg.codex.timeout_s)}" />
        </div>
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Codex 阶段 4 独占执行 Prompt<small>{task_id} {owned_paths} {feedback_block}</small></label>
        <textarea class="editor-textarea" rows="7" data-bind="codex.subtask_prompt">${esc(cfg.codex.subtask_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "writelock_sandbox") {
    fieldsHtml = `
      <div class="callout-box">
        <div class="callout-label">硬安全边界（始终生效）</div>
        <div>每次文件写入均由 <code>_apply_edits_audited()</code> 校验目标路径是否位于子任务的独占 <code>owned_paths</code> 白名单内；跨模型越界写入将立即拦截并写入 <code>writelock.audit (DENY)</code> 审计日志。</div>
      </div>
      <label class="inline-toggle">
        <button type="button" class="toggle-switch" role="switch" aria-checked="${cfg.modules.sandbox_checks_enabled}" data-cfg-toggle="modules.sandbox_checks_enabled"></button>
        <span>启用子任务 macOS Seatbelt 命令沙箱验收</span>
      </label>
    `;
  } else if (nodeId === "gemini_review") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">互审最大定向返工轮次（0 = 仅审查不返工）</label>
        <input class="editor-input" type="number" min="0" max="4" data-bind="stages.max_review_rounds" data-type="int" value="${esc(cfg.stages.max_review_rounds)}" />
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Gemini 审查 Codex 产出 Prompt<small>{review_round} {target_blocks}</small></label>
        <textarea class="editor-textarea" rows="7" data-bind="gemini.review_prompt">${esc(cfg.gemini.review_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "codex_review") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">互审最大定向返工轮次（0 = 仅审查不返工）</label>
        <input class="editor-input" type="number" min="0" max="4" data-bind="stages.max_review_rounds" data-type="int" value="${esc(cfg.stages.max_review_rounds)}" />
      </div>
      <div class="inspector-field">
        <label class="inspector-label">Codex 审查 Gemini 产出 Prompt<small>{review_round} {target_blocks}</small></label>
        <textarea class="editor-textarea" rows="7" data-bind="codex.review_prompt">${esc(cfg.codex.review_prompt)}</textarea>
      </div>
    `;
  } else if (nodeId === "final_delivery") {
    fieldsHtml = `
      <div class="inspector-field">
        <label class="inspector-label">学习文档顶部说明语（汇编进 study-guide.md）<small>shared.study_guide_header</small></label>
        <textarea class="editor-textarea" rows="4" data-bind="shared.study_guide_header">${esc(cfg.shared.study_guide_header)}</textarea>
      </div>
    `;
  }

  host.innerHTML = `
    <div class="inspector-head">
      <div>
        <div class="badge-row" style="margin-bottom:6px">${ownerBadge}</div>
        <h3>${esc(nodeDef.label)}</h3>
      </div>
      <button type="button" class="btn btn-primary btn-xs" id="btn-inspector-save">保存生效</button>
    </div>
    ${liveBlock}
    ${fieldsHtml}
  `;

  $("btn-inspector-save")?.addEventListener("click", savePipelineDraft);
}

function setNestedValue(obj, dottedPath, value) {
  const parts = dottedPath.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]];
  cur[parts[parts.length - 1]] = value;
}

function getNestedValue(obj, dottedPath) {
  return dottedPath.split(".").reduce((acc, k) => (acc ? acc[k] : undefined), obj);
}

async function savePipelineDraft() {
  if (!state.pipelineDraft) return;
  try {
    const res = await api("/api/pipeline", { method: "PUT", body: state.pipelineDraft });
    state.pipeline = res.config;
    state.pipelineDraft = deepClone(res.config);
    pulseAtmosphere(1.2);
    updateBrandProfileTag();
    renderComposerActiveModels();
    await loadModules();
    renderLogicView();
    if (state.currentId) await openSession(state.currentId);
    toast("后端执行逻辑已持久化并立即生效");
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ==================== VIEW 3: MODULE CONTROL CENTER ==================== */
function renderModulesView() {
  const grid = $("modules-grid");
  const snap = state.modulesSnapshot;
  if (!grid || !snap) return;

  const mods = snap.modules || [];
  const ag = mods.find((m) => m.id === "antigravity_bridge");
  if (ag) {
    const pill = $("ag-live-pill");
    if (pill) {
      pill.textContent = ag.metrics?.app_running
        ? `${ag.metrics.app} 客户端运行中 · 全局插件正常`
        : `${ag.metrics?.app || "Antigravity"} 待唤起 · 全局插件${ag.metrics?.global_plugin ? "已就绪" : "待补齐"}`;
    }
    const code = $("ag-trigger-code");
    if (code) code.textContent = ag.metrics?.trigger || "/aisys";
  }

  const actionMeta = {
    start: { label: "启动", cls: "btn-primary" },
    probe: { label: "实测连通", cls: "btn-secondary" },
    repair: { label: "校验修复", cls: "btn-secondary" },
    open: { label: "打开目录", cls: "btn-secondary" },
    stop: { label: "暂停", cls: "btn-ghost" },
  };

  grid.innerHTML = mods.map((m) => {
    const [stLbl, stCls] = MOD_STATUS_LABELS[m.status] || [m.status, "pill"];
    const kvs = Object.entries(m.metrics || {}).slice(0, 4).map(([k, v]) => `
      <div class="mod-kv">
        <span>${esc(k)}</span>
        <strong>${esc(typeof v === "boolean" ? (v ? "YES" : "NO") : (v === "" || v === null || v === undefined ? "—" : v))}</strong>
      </div>
    `).join("");

    const actionBtns = (m.actions || []).map((act) => {
      const am = actionMeta[act] || { label: act, cls: "btn-secondary" };
      const lbl = (m.id === "antigravity_bridge" && act === "start") ? "启动并跳转" : am.label;
      return `<button type="button" class="btn ${am.cls} btn-xs" data-mod-id="${esc(m.id)}" data-mod-action="${esc(act)}">${esc(lbl)}</button>`;
    }).join("");

    return `
      <article class="module-card glass-surface spotlight-card mod-${esc(m.status)}">
        <div class="mod-head">
          <div>
            <div class="mod-title">${esc(m.name)}</div>
            <div class="mod-role">${esc(m.role)}</div>
          </div>
          ${m.can_toggle
            ? `<button type="button" class="toggle-switch" role="switch" aria-checked="${m.enabled}" data-mod-toggle="${esc(m.id)}" title="独立开关此模块"></button>`
            : `<span class="pill ${stCls}">核心常驻</span>`}
        </div>
        <div class="mod-detail">${esc(m.detail)}</div>
        <div class="mod-metrics">${kvs}</div>
        <div class="mod-footer">
          <div class="badge-row" style="margin:0">
            <span class="pill ${stCls}">${esc(stLbl)}</span>
            ${m.latency_ms !== null && m.latency_ms !== undefined ? `<span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(m.latency_ms)} ms</span>` : ""}
          </div>
          <div class="mod-actions">${actionBtns}</div>
        </div>
      </article>
    `;
  }).join("");
}

/* ==================== SESSIONS LIST & ACTIVE WORKSPACE ==================== */
async function loadSessions() {
  const data = await api("/api/sessions");
  state.sessions = data.sessions || [];
  renderSessionSelector();
  renderRecentStrip();
}

function renderSessionSelector() {
  const sel = $("session-select");
  if (!sel) return;
  const opts = ['<option value="">+ 新共创会话</option>'];
  for (const s of state.sessions) {
    const modeTag = s.mode === "learn" ? "研学" : "开发";
    const stageLbl = STAGE_META[s.stage]?.label || s.stage;
    const title = s.task.length > 22 ? `${s.task.slice(0, 22)}…` : s.task;
    opts.push(`<option value="${esc(s.id)}"${s.id === state.currentId ? " selected" : ""}>[${modeTag}] ${esc(title)} · ${esc(stageLbl)}</option>`);
  }
  sel.innerHTML = opts.join("");
}

function renderRecentStrip() {
  const wrap = $("recent-strip");
  const list = $("recent-list");
  if (!wrap || !list) return;
  if (!state.sessions.length) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  list.innerHTML = state.sessions.slice(0, 4).map((s) => `
    <div class="recent-item" data-open-session="${esc(s.id)}">
      <div class="recent-title">${esc(s.task)}</div>
      <div class="recent-meta">
        <span class="pill ${s.mode === "learn" ? "pill-mode" : "pill-stage"}">${s.mode === "learn" ? "研学" : "开发"}</span>
        <span>${esc(STAGE_META[s.stage]?.label || s.stage)}</span>
        <span class="mono">${fmtRelTime(s.updated_at)}</span>
      </div>
    </div>
  `).join("");
}

function showComposer() {
  state.currentId = null;
  state.session = null;
  state.studyGuideCache = null;
  state.diffCache = null;
  $("composer-section").classList.remove("hidden");
  $("session-section").classList.add("hidden");
  switchView("studio");
  renderSessionSelector();
  renderTopEnginePills();
  updateKernelDockVisibility();
}

async function openSession(sessionId, { resetCache = false, focusStudio = false } = {}) {
  if (!sessionId) {
    showComposer();
    return;
  }
  if (resetCache || state.currentId !== sessionId) {
    state.studyGuideCache = null;
    state.diffCache = null;
  }
  state.currentId = sessionId;
  const view = await api(`/api/sessions/${sessionId}`);
  state.session = view;
  state.events = view.events || [];
  $("composer-section").classList.add("hidden");
  $("session-section").classList.remove("hidden");
  if (focusStudio) switchView("studio");
  renderSessionSelector();
  renderTopEnginePills();
  renderWorkspace();
  renderKernelDock();
  updateKernelDockVisibility();
  if (state.view === "logic") renderLogicView();

  if (view.stage === "completed") {
    if (view.mode === "learn" && !state.studyGuideCache) loadStudyGuide(sessionId);
    else if (view.mode === "dev" && !state.diffCache) loadDiff(sessionId);
  }
}

async function loadStudyGuide(sessionId) {
  try {
    state.studyGuideCache = await api(`/api/sessions/${sessionId}/study-guide`);
    if (state.currentId === sessionId) renderStageContent();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadDiff(sessionId) {
  try {
    state.diffCache = await api(`/api/sessions/${sessionId}/diff`);
    if (state.currentId === sessionId) renderStageContent();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ==================== WORKSPACE HEADER, TOPOLOGY & STAGE CONTENT ==================== */
function renderWorkspace() {
  const s = state.session;
  if (!s) return;
  const meta = STAGE_META[s.stage] || { step: 0, label: s.stage };
  $("s-mode-badge").textContent = s.mode === "learn" ? "深度研学 · Markdown" : "开发协作 · 隔离快照";
  $("s-stage-badge").textContent = meta.label;
  $("s-id-label").textContent = `#${s.id.slice(0, 8)}`;
  $("s-task-title").textContent = s.task;

  const m = s.metrics || {};
  $("s-kpi-strip").innerHTML = `
    <span class="kpi-pill">TOKENS <strong>${fmtTokens(m.total_tokens || 0)}</strong></span>
    <span class="kpi-pill">CODEX <strong>${Number(m.codex_duration_s || 0).toFixed(1)}s</strong></span>
    <span class="kpi-pill">写锁 <strong>${m.lock_allows || 0}✓</strong> / ${m.lock_denies || 0}✕</span>
  `;

  document.querySelectorAll("#stepper li").forEach((li) => {
    const step = Number(li.dataset.step);
    li.classList.remove("active", "done");
    if (s.stage === "completed") li.classList.add("done");
    else if (step < meta.step) li.classList.add("done");
    else if (step === meta.step) li.classList.add("active");
  });

  const errEl = $("s-error-banner");
  if (s.error) { errEl.textContent = s.error; errEl.classList.remove("hidden"); }
  else errEl.classList.add("hidden");

  const codexBtn = $("btn-trigger-codex");
  const canRunCodex = Boolean(s.next_action?.codex_ready) && !s.codex_busy && s.stage !== "completed" && s.stage !== "cancelled";
  codexBtn.disabled = !canRunCodex;
  codexBtn.classList.toggle("hidden", s.stage === "completed" || s.stage === "cancelled");
  $("btn-cancel-session").classList.toggle("hidden", s.stage === "completed" || s.stage === "cancelled");

  renderTopology(s);
  renderGeminiActionBar(s);
  renderStageContent();
}

function renderTopology(s) {
  const host = $("topology-ribbon");
  if (!host) return;
  const proc = s.active_process;
  const codexLive = Boolean(s.codex_busy || proc);
  const geminiPending = (s.next_action?.pending_gemini || []).length > 0;
  const m = s.metrics || {};
  const lockCount = (m.lock_allows || 0) + (m.lock_denies || 0);

  const codexSub = proc
    ? `PID ${proc.pid || "…"} · ${Number(proc.elapsed_s || 0).toFixed(1)}s`
    : s.next_action?.codex_ready ? "等待调度"
      : s.stage === "completed" ? `已完成 · ${Number(m.codex_duration_s || 0).toFixed(1)}s` : "待命";

  const codexDetail = proc?.latest_event
    ? proc.latest_event.slice(0, 24)
    : proc?.thread_id ? `thread ${proc.thread_id.slice(0, 8)}` : `${fmtTokens(m.total_tokens || 0)} tok`;

  const geminiSub = geminiPending
    ? `待确认 ${s.next_action.pending_gemini.length} 项 (${state.pipeline?.gemini?.trigger_command || "/aisys"})`
    : s.stage === "deciding" ? "等待你的拍板"
      : s.stage === "completed" ? "已归档" : "已同步";

  const lockSub = lockCount
    ? `ALLOW ${m.lock_allows || 0} · DENY ${m.lock_denies || 0}`
    : s.locked_plan ? `${s.locked_plan.subtasks.length} 路径锁生效` : "防重叠校验待命";

  const liveNote = proc
    ? `LIVE · ${proc.step} · ${Number(proc.elapsed_s || 0).toFixed(1)}s`
    : geminiPending ? `等待 Antigravity 确认 (${s.next_action.pending_gemini[0].action})`
      : `预设 ${s.pipeline_profile || "balanced"} · ${STAGE_META[s.stage]?.label || s.stage}`;

  const eCodex = codexLive ? "active-codex" : (m.codex_duration_s > 0 ? "done-edge" : "");
  const eGemini = geminiPending ? "active-gemini" : (Object.keys(s.proposals || {}).includes("gemini") ? "done-edge" : "");
  const eLock = s.locked_plan ? (codexLive ? "active-codex" : "done-edge") : "";

  host.innerHTML = `
    <div class="topo-head">
      <div class="topo-title">
        <span class="pulse-dot ${codexLive || geminiPending ? "dot-live" : ""}"></span>
        <span>LIVE DUAL-MODEL TOPOLOGY · 点击「透视并编辑节点逻辑」可实时定制每步 Prompt</span>
      </div>
      <div class="topo-live-note" id="topo-live-note">${esc(liveNote)}</div>
    </div>
    <div class="topo-svg-wrap">
      <svg class="topo-svg" viewBox="0 0 920 112" fill="none">
        <path class="topo-edge done-edge" d="M168 56 L228 56"/>
        <path class="topo-edge ${eGemini}" d="M402 44 C444 44, 444 26, 492 26"/>
        <path class="topo-edge ${eCodex}" d="M402 68 C444 68, 444 86, 492 86"/>
        <path class="topo-edge ${eLock}" d="M688 26 C722 26, 722 48, 748 50"/>
        <path class="topo-edge ${eLock}" d="M688 86 C722 86, 722 64, 748 62"/>

        <rect class="topo-node-box" x="6" y="26" width="162" height="60" rx="12"/>
        <text class="topo-node-title" x="22" y="51">共享任务意图</text>
        <text class="topo-node-sub" x="22" y="70">${s.mode === "learn" ? "LEARN · Markdown" : "DEV · Git Snapshot"}</text>

        <rect class="topo-node-box ${codexLive ? "is-codex-live" : ""}" x="228" y="22" width="174" height="68" rx="12"/>
        <text class="topo-node-title" x="244" y="47">AIsys 编排内核</text>
        <text class="topo-node-sub accent-jade" x="244" y="65">${esc(s.stage.toUpperCase())}</text>
        <text class="topo-node-sub" x="244" y="80">${m.event_count || 0} 条真实审计事件</text>

        <rect class="topo-node-box ${geminiPending ? "is-gemini-live" : ""}" x="492" y="4" width="196" height="46" rx="11"/>
        <text class="topo-node-title" x="508" y="24">Gemini · Antigravity</text>
        <text class="topo-node-sub accent-mint" x="508" y="41">${esc(geminiSub)}</text>

        <rect class="topo-node-box ${codexLive ? "is-codex-live" : ""}" x="492" y="62" width="196" height="46" rx="11"/>
        <text class="topo-node-title" x="508" y="82">Codex · CLI 沙箱</text>
        <text class="topo-node-sub accent-jade" id="topo-codex-sub" x="508" y="99">${esc(codexSub)} · ${esc(codexDetail)}</text>

        <rect class="topo-node-box" x="748" y="22" width="166" height="68" rx="12"/>
        <text class="topo-node-title" x="764" y="47">写锁与隔离工作区</text>
        <text class="topo-node-sub accent-jade" x="764" y="66">${esc(lockSub)}</text>
        <text class="topo-node-sub" x="764" y="81">${(s.artifacts || []).length} 个独占产物</text>
      </svg>
    </div>
  `;
}

function renderGeminiActionBar(s) {
  const bar = $("gemini-action-bar");
  const pending = s.next_action?.pending_gemini || [];
  if (!pending.length || s.stage === "completed" || s.stage === "cancelled") {
    bar.classList.add("hidden");
    return;
  }
  const trigger = state.pipeline?.gemini?.trigger_command || "/aisys";
  const actionLabels = {
    propose: "提交阶段 1 独立初案",
    critique: "审阅 Codex 初案并提出无重叠分工",
    merge: "按你的融合指令锁定最终分工",
    execute_subtask: `完成独占子任务（${pending.map((p) => p.task_id).join(" / ")}）`,
    review_peer: `对 Codex 产出执行第 ${s.review_round} 轮交叉审查`,
  };
  bar.classList.remove("hidden");
  bar.innerHTML = `
    <div class="handshake-inner">
      <div class="handshake-copy">
        <div class="handshake-title">
          <span class="pulse-dot dot-live" style="color: var(--jade-glow)"></span>
          <span>待 Gemini 推进：${esc(actionLabels[pending[0].action] || pending[0].action)}</span>
        </div>
        <div class="handshake-sub">点击右侧按钮自动写入剪贴板 <code>${esc(trigger)}</code> 并直达 Antigravity，跳过去只需按 <code>⌘V</code> + 回车确认即可全自动推进。</div>
      </div>
      <div class="handshake-actions">
        <button type="button" class="btn btn-mint btn-sm" id="btn-launch-antigravity">一键跳转 Antigravity（仅需确认）</button>
      </div>
    </div>
  `;
  $("btn-launch-antigravity")?.addEventListener("click", () => jumpToAntigravity(trigger));
}

function renderDivisionMatrix(subtasks, divisionCheck) {
  if (!subtasks || !subtasks.length) return `<div class="callout-box">尚未提供结构化子任务分工表。</div>`;
  const byId = Object.fromEntries(subtasks.map((t) => [t.id, t]));
  const check = divisionCheck || { valid: false, conflicts: ["尚未校验"], waves: [] };
  const waves = check.waves && check.waves.length ? check.waves : [subtasks.map((t) => t.id)];
  const sealHtml = check.valid
    ? `<div class="division-seal valid"><span>机械防重叠校验通过 · 0 路径冲突 · 无环依赖 · 共 ${waves.length} 波次</span><span class="mono">${subtasks.length} TASKS LOCKED</span></div>`
    : `<div class="division-seal invalid"><span>分工冲突：${esc((check.conflicts || []).join("；"))}</span><span class="mono">BLOCKED</span></div>`;

  const lanesHtml = waves.map((waveIds, idx) => `
    <div class="wave-lane">
      <div class="wave-header"><span>WAVE 0${idx + 1} · ${waveIds.length > 1 ? "并行互斥波次" : "顺序波次"}</span><span>${waveIds.length} 项子任务</span></div>
      <div class="wave-tasks">
        ${waveIds.map((tid) => {
          const st = byId[tid];
          if (!st) return "";
          return `
            <div class="wave-task-card owner-${esc(st.owner)}">
              <div class="wt-top">
                <span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(st.id)}</span>
                <span class="pill ${st.owner === "gemini" ? "pill-gemini" : "pill-codex"}">${st.owner === "gemini" ? "Gemini" : "Codex"}</span>
              </div>
              <div class="wt-title">${esc(st.title)}</div>
              <div class="wt-paths">${(st.owned_paths || []).map((p) => `<span class="lock-chip">${esc(p)}</span>`).join("")}</div>
              ${st.depends_on?.length ? `<div class="dep-chip">前置依赖：${esc(st.depends_on.join(", "))}</div>` : ""}
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `).join("");
  return `<div class="division-block">${sealHtml}${lanesHtml}</div>`;
}

function renderModelWaitBox(model, s) {
  const isCodex = model === "codex";
  const proc = isCodex ? s.active_process : null;
  if (isCodex && (s.codex_busy || proc)) {
    return `
      <div class="live-wait-box">
        <div class="wait-status-row"><span class="radar-orb"></span><span>${esc(proc?.step || "Codex 隔离沙箱推演中…")}</span></div>
        <div class="wait-telemetry" id="wait-codex-telemetry">
          ${proc?.pid ? `<span>PID ${esc(proc.pid)}</span>` : ""}
          ${proc?.thread_id ? `<span>thread ${esc(proc.thread_id.slice(0, 8))}</span>` : ""}
          <span>耗时 ${Number(proc?.elapsed_s || 0).toFixed(1)}s</span>
          ${proc?.latest_event ? `<span>${esc(proc.latest_event)}</span>` : ""}
        </div>
      </div>
    `;
  }
  return `
    <div class="live-wait-box">
      <div class="wait-status-row"><span class="radar-orb ${isCodex ? "" : "mint"}"></span><span>${isCodex ? "等待 Codex 后台执行" : "等待 Antigravity 确认同步"}</span></div>
      <div class="wait-telemetry"><span>${isCodex ? "点击右上角「运行 Codex」立即调度" : "点击上方绿色光钮直达 Antigravity，⌘V 回车确认"}</span></div>
    </div>
  `;
}

function renderProposalCard(model, prop, s) {
  const isG = model === "gemini";
  const header = `
    <div class="model-card-head">
      <div class="model-identity">
        <span class="model-badge-ring ${model}">
          <svg class="ico" viewBox="0 0 16 16" fill="none">${isG
            ? '<path d="M8 2.2l1.6 4.2L13.8 8l-4.2 1.6L8 13.8l-1.6-4.2L2.2 8l4.2-1.6L8 2.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
            : '<path d="M5 4.5L2 8l3 3.5M11 4.5L14 8l-3 3.5M9 3.5L7 12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'}</svg>
        </span>
        <div>
          <div class="model-name">${isG ? "Gemini · Antigravity" : "Codex · CLI 沙箱"}</div>
          <div class="model-meta">${prop ? `${prop.duration ? `${prop.duration}s · ` : ""}${prop.usage?.output_tokens ? `${fmtTokens((prop.usage.input_tokens || 0) + (prop.usage.output_tokens || 0))} tok` : "独立初案已提交"}` : "阶段 1 推演中"}</div>
        </div>
      </div>
      <span class="pill ${prop ? "pill-ok" : "pill-warn"}">${prop ? "已提交" : "进行中"}</span>
    </div>
  `;
  if (!prop) return `<article class="model-card glass-surface spotlight-card is-${model}">${header}${renderModelWaitBox(model, s)}</article>`;
  return `
    <article class="model-card glass-surface spotlight-card is-${model}">
      ${header}
      <p class="summary-lead">${esc(prop.summary)}</p>
      <div class="md-body">${renderMarkdown(prop.approach).html}</div>
      ${(prop.strengths?.length || prop.risks?.length) ? `
        <div class="critique-grid">
          <div class="callout-box"><div class="callout-label">核心亮点</div><ul class="tag-list">${(prop.strengths || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
          <div class="callout-box callout-amber"><div class="callout-label">风险与边界</div><ul class="tag-list risks">${(prop.risks || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
        </div>
      ` : ""}
    </article>
  `;
}

function renderRefinedCard(model, ref, s) {
  const isG = model === "gemini";
  const header = `
    <div class="model-card-head">
      <div class="model-identity">
        <span class="model-badge-ring ${model}">
          <svg class="ico" viewBox="0 0 16 16" fill="none">${isG
            ? '<path d="M8 2.2l1.6 4.2L13.8 8l-4.2 1.6L8 13.8l-1.6-4.2L2.2 8l4.2-1.6L8 2.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
            : '<path d="M5 4.5L2 8l3 3.5M11 4.5L14 8l-3 3.5M9 3.5L7 12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'}</svg>
        </span>
        <div>
          <div class="model-name">${isG ? "Gemini 互审升级方案" : "Codex 互审升级方案"}</div>
          <div class="model-meta">${ref ? `${ref.subtasks?.length || 0} 项子任务${ref.duration ? ` · ${ref.duration}s` : ""}` : "等待交叉互审"}</div>
        </div>
      </div>
      <span class="pill ${ref ? (ref.division_check?.valid ? "pill-ok" : "pill-err") : "pill-warn"}">${ref ? (ref.division_check?.valid ? "零重叠" : "待修正") : "审查中"}</span>
    </div>
  `;
  if (!ref) return `<article class="model-card glass-surface spotlight-card is-${model}">${header}${renderModelWaitBox(model, s)}</article>`;
  return `
    <article class="model-card glass-surface spotlight-card is-${model}">
      ${header}
      <p class="summary-lead">${esc(ref.summary)}</p>
      <div class="critique-grid">
        <div class="callout-box callout-amber"><div class="callout-label">对对方的审视与挑刺</div><div>${esc(ref.critique_of_peer)}</div></div>
        <div class="callout-box"><div class="callout-label">吸收对方的优点</div><div>${esc(ref.adopted_from_peer)}</div></div>
      </div>
      <div class="md-body">${renderMarkdown(ref.approach).html}</div>
      ${renderDivisionMatrix(ref.subtasks, ref.division_check)}
    </article>
  `;
}

function renderDecisionDock(s) {
  if (s.stage === "merging") {
    return `
      <section class="decision-card glass-surface">
        <div class="decision-head"><h3>正在按融合微调意见生成最终锁定分工</h3><p>微调指令：${esc(s.decision?.fusion_notes || "修正冲突并融合双方方案")}</p></div>
        <div class="live-wait-box"><div class="wait-status-row"><span class="radar-orb mint"></span><span>等待 Antigravity 确认融合分工</span></div></div>
      </section>
    `;
  }
  if (s.stage !== "deciding") return "";
  const isMerge = state.decisionChoice === "merge";
  return `
    <section class="decision-card glass-surface">
      <div class="decision-head"><h3>由你拍板：确定最终执行方案</h3><p>可直接采纳任意一方，或输入一句融合微调意见，由系统机械锁死互斥路径后立即开工。</p></div>
      <div class="decision-buttons">
        <button type="button" class="decision-option ${state.decisionChoice === "gemini" ? "is-active" : ""}" data-choice="gemini"><strong>采纳 Gemini 方案</strong><span>按 Gemini 的无重叠分工立即执行</span></button>
        <button type="button" class="decision-option ${state.decisionChoice === "codex" ? "is-active" : ""}" data-choice="codex"><strong>采纳 Codex 方案</strong><span>按 Codex 的无重叠分工立即执行</span></button>
        <button type="button" class="decision-option ${state.decisionChoice === "merge" ? "is-active" : ""}" data-choice="merge"><strong>融合微调</strong><span>自由组合双方亮点并调整归属</span></button>
      </div>
      ${isMerge ? `
        <div class="fusion-panel">
          <textarea id="fusion-notes-input" class="fusion-textarea" placeholder="写下你的融合微调指令，例如：采用 Gemini 的章节框架，但把代码实例与压测全部交给 Codex…"></textarea>
          <div class="fusion-footer">
            <label class="meta-tag"><span>基础底稿：</span><select id="fusion-base-select" class="hud-select"><option value="gemini"${state.fusionBase === "gemini" ? " selected" : ""}>Gemini</option><option value="codex"${state.fusionBase === "codex" ? " selected" : ""}>Codex</option></select></label>
            <button type="button" class="btn btn-primary" id="btn-confirm-decision">提交融合微调</button>
          </div>
        </div>
      ` : `
        <div class="fusion-footer" style="margin-top:14px">
          <span class="meta-tag">确认后立即启用独占文件写锁，并自动拉起 Codex 执行就绪波次。</span>
          <button type="button" class="btn btn-primary" id="btn-confirm-decision">锁定并启动协同执行</button>
        </div>
      `}
    </section>
  `;
}

function renderExecutionStage(s) {
  const plan = s.locked_plan;
  if (!plan) return "";
  const statusLabels = { pending: ["待启动", "pill"], running: ["执行中", "pill-codex"], completed: ["已完成", "pill-ok"], needs_fix: ["待返工", "pill-warn"], failed: ["失败", "pill-err"] };
  const cards = plan.subtasks.map((st) => {
    const ex = s.executions?.[st.id] || { status: "pending" };
    const [lbl, cls] = statusLabels[ex.status] || [ex.status, "pill"];
    return `
      <article class="exec-card glass-surface spotlight-card status-${esc(ex.status)}">
        <div class="model-card-head">
          <div class="badge-row" style="margin:0"><span class="mono" style="font-size:12px;color:var(--text-muted)">${esc(st.id)}</span><span class="pill ${st.owner === "gemini" ? "pill-gemini" : "pill-codex"}">${st.owner === "gemini" ? "Gemini" : "Codex"}</span></div>
          <span class="pill ${cls}">${esc(lbl)}</span>
        </div>
        <div style="font-size:15px;font-weight:600">${esc(st.title)}</div>
        <div style="font-size:13px;color:var(--text-muted)">${esc(st.objective)}</div>
        <div class="wt-paths">${(st.owned_paths || []).map((p) => `<span class="lock-chip">${esc(p)}</span>`).join("")}</div>
        ${ex.review_feedback ? `<div class="exec-feedback">交叉审查定向返工意见：\n${esc(ex.review_feedback)}</div>` : ""}
        ${ex.summary ? `<div class="callout-box"><div class="callout-label">执行摘要</div><div>${esc(ex.summary)}</div></div>` : ""}
        ${ex.error ? `<div class="alert-banner alert-error">${esc(ex.error)}</div>` : ""}
        ${ex.duration ? `<div class="model-meta">耗时 ${ex.duration}s · 尝试 ${ex.attempts || 1} 次</div>` : ""}
      </article>
    `;
  }).join("");
  return `
    <section class="glass-surface" style="padding:18px 20px;">
      <div class="badge-row" style="margin-bottom:10px"><span class="pill pill-ok">锁定来源：${esc(plan.source.toUpperCase())}</span>${plan.fusion_notes ? `<span class="meta-tag">微调意见：${esc(plan.fusion_notes)}</span>` : ""}</div>
      ${renderDivisionMatrix(plan.subtasks, plan.division_check)}
    </section>
    <div class="exec-grid">${cards}</div>
  `;
}

function renderReviewAndDelivery(s) {
  const renderRevCard = (reviewer) => {
    const rev = s.reviews?.[reviewer] || [...(s.review_history || [])].reverse().find((r) => r.reviewer === reviewer);
    const target = reviewer === "gemini" ? "Codex" : "Gemini";
    if (!rev) {
      return `<article class="model-card glass-surface spotlight-card is-${reviewer}"><div class="model-card-head"><div class="model-name">${reviewer === "gemini" ? "Gemini" : "Codex"} 审查 ${target}</div><span class="pill pill-warn">审查中</span></div>${renderModelWaitBox(reviewer, s)}</article>`;
    }
    return `
      <article class="model-card glass-surface spotlight-card is-${reviewer}">
        <div class="model-card-head">
          <div><div class="model-name">${reviewer === "gemini" ? "Gemini" : "Codex"} 审查 ${target}</div><div class="model-meta">第 ${rev.round} 轮${rev.duration ? ` · ${rev.duration}s` : ""}</div></div>
          <span class="pill ${rev.verdict === "pass" ? "pill-ok" : "pill-warn"}">${rev.verdict === "pass" ? "PASS" : "REWORK"}</span>
        </div>
        <p class="summary-lead">${esc(rev.summary)}</p>
        ${rev.findings?.length ? `<ul class="tag-list">${rev.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      </article>
    `;
  };

  let deliverableHtml = "";
  if (s.stage === "completed") {
    if (s.mode === "learn") {
      if (state.studyGuideCache) {
        const rendered = renderMarkdown(state.studyGuideCache.markdown, { collectToc: true });
        const tocHtml = rendered.toc.length
          ? `<nav class="guide-toc"><div class="toc-title">学习大纲</div>${rendered.toc.map((t) => `<a class="toc-link lvl-${t.level}" href="#${esc(t.id)}">${esc(t.text)}</a>`).join("")}</nav>`
          : "";
        deliverableHtml = `
          <section class="deliverable-card glass-surface">
            <div class="deliverable-head">
              <div><h3>结构化 Markdown 学习文档</h3><div class="model-meta">${esc(s.final_summary || "")}</div></div>
              <a class="btn btn-primary btn-sm" href="/api/sessions/${esc(s.id)}/study-guide?download=true" download>下载 .md 学习文档</a>
            </div>
            <div class="${tocHtml ? "guide-layout" : ""}">${tocHtml}<article class="guide-article md-body">${rendered.html}</article></div>
          </section>
        `;
      } else {
        deliverableHtml = `<section class="deliverable-card glass-surface"><div class="model-meta">正在加载编译后的学习文档…</div></section>`;
      }
    } else if (state.diffCache) {
      const lines = (state.diffCache.diff || "(无文件变更)").split("\n").map((line) => {
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "diff-add"
          : line.startsWith("-") && !line.startsWith("---") ? "diff-del"
            : line.startsWith("@@") || line.startsWith("diff ") ? "diff-hunk" : "diff-ctx";
        return `<span class="${cls}">${esc(line)}</span>`;
      }).join("");
      deliverableHtml = `
        <section class="deliverable-card glass-surface">
          <div class="deliverable-head">
            <div><h3>隔离工作区最终变更（Git Diff）</h3><div class="model-meta">${esc(s.final_summary || "")}</div></div>
            <div class="wt-paths">${(state.diffCache.changed_paths || []).map((p) => `<span class="lock-chip">${esc(p)}</span>`).join("")}</div>
          </div>
          <div class="diff-box">${lines}</div>
        </section>
      `;
    }
  }
  return `${deliverableHtml}<div class="dual-grid">${renderRevCard("gemini")}${renderRevCard("codex")}</div>${renderExecutionStage(s)}`;
}

function renderStageContent() {
  const host = $("stage-content");
  const s = state.session;
  if (!host || !s) return;
  if (s.stage === "proposing") {
    host.innerHTML = `<div class="dual-grid">${renderProposalCard("gemini", s.proposals?.gemini, s)}${renderProposalCard("codex", s.proposals?.codex, s)}</div>`;
    return;
  }
  if (s.stage === "critiquing" || s.stage === "deciding" || s.stage === "merging") {
    host.innerHTML = `${renderDecisionDock(s)}<div class="dual-grid">${renderRefinedCard("gemini", s.refined?.gemini, s)}${renderRefinedCard("codex", s.refined?.codex, s)}</div>`;
    bindDecisionHandlers();
    return;
  }
  if (s.stage === "executing") { host.innerHTML = renderExecutionStage(s); return; }
  if (s.stage === "reviewing" || s.stage === "completed") { host.innerHTML = renderReviewAndDelivery(s); return; }
  host.innerHTML = `<div class="glass-surface" style="padding:20px;color:var(--text-muted)">会话状态：${esc(s.stage)}</div>`;
}

function bindDecisionHandlers() {
  document.querySelectorAll(".decision-option").forEach((btn) => {
    btn.addEventListener("click", () => { state.decisionChoice = btn.dataset.choice; renderStageContent(); });
  });
  $("fusion-base-select")?.addEventListener("change", (e) => { state.fusionBase = e.target.value; });
  $("btn-confirm-decision")?.addEventListener("click", async () => {
    if (!state.currentId) return;
    const notes = $("fusion-notes-input")?.value?.trim() || "";
    if (state.decisionChoice === "merge" && !notes) { toast("请填写融合微调指令", "error"); return; }
    try {
      const view = await api(`/api/sessions/${state.currentId}/decide`, {
        method: "POST",
        body: { choice: state.decisionChoice, base_model: state.decisionChoice === "merge" ? state.fusionBase : state.decisionChoice, fusion_notes: notes },
      });
      state.session = view;
      state.events = view.events || state.events;
      pulseAtmosphere(1.4);
      renderWorkspace();
      renderKernelDock();
      toast(view.stage === "merging" ? "已进入融合微调阶段" : "方案已锁定，互斥协同启动");
    } catch (err) { toast(err.message, "error"); }
  });
}

/* ==================== CONTEXTUAL KERNEL TELEMETRY DOCK ==================== */
function renderKernelDock() {
  const dock = $("kernel-dock");
  const led = $("kernel-led");
  const ticker = $("kernel-ticker");
  const statTok = $("kernel-stat-tokens");
  const statLocks = $("kernel-stat-locks");
  const statCount = $("kernel-stat-count");
  const stream = $("kernel-stream");
  if (!dock || !stream) return;

  dock.dataset.open = String(state.kernelOpen);
  $("kernel-dock-toggle")?.setAttribute("aria-expanded", String(state.kernelOpen));
  const s = state.session;
  const events = state.events || [];
  const latest = events[events.length - 1];
  const active = Boolean(s?.codex_busy || s?.active_process);

  led.classList.toggle("is-live", active);
  if (s?.active_process) {
    const p = s.active_process;
    ticker.textContent = `CODEX · ${p.step} · PID ${p.pid || "…"} · ${Number(p.elapsed_s || 0).toFixed(1)}s · ${p.latest_event || ""}`;
  } else if (latest) {
    ticker.textContent = `${CAT_LABELS[latest.category] || latest.category} · ${latest.event_type} · ${latest.summary}`;
  } else {
    ticker.textContent = "等待后台内核事件…";
  }

  const m = s?.metrics || {};
  statTok.textContent = `${fmtTokens(m.total_tokens || 0)} tok`;
  statLocks.textContent = `LOCK ${m.lock_allows || 0}✓ ${m.lock_denies || 0}✕`;
  statCount.textContent = `${events.length} 事件`;

  const filtered = state.kernelFilter === "all" ? events : events.filter((ev) => ev.category === state.kernelFilter);
  if (!filtered.length) {
    stream.innerHTML = `<div style="padding:18px;text-align:center;color:var(--text-faint);font-size:12.5px">当前筛选下暂无事件。</div>`;
    return;
  }
  stream.innerHTML = [...filtered].reverse().map((ev) => {
    const expanded = state.expandedEventIds.has(ev.id);
    const cat = ev.category || "engine";
    return `
      <div class="trace-row" data-ev-id="${esc(ev.id)}">
        <div class="trace-main">
          <span class="trace-ts">${fmtTimeMs(ev.ts)}</span>
          <span class="trace-cat cat-${esc(cat)}">${esc(CAT_LABELS[cat] || cat)}</span>
          <span class="trace-type">${esc(ev.event_type)}</span>
          <span class="trace-summary">${esc(ev.summary)}</span>
        </div>
        ${expanded ? `<pre class="trace-detail">${esc(JSON.stringify(ev.detail || {}, null, 2))}</pre>` : ""}
      </div>
    `;
  }).join("");
}

/* ==================== SSE REAL-TIME STREAM ==================== */
function connectSSE() {
  if (state.sse) state.sse.close();
  const es = new EventSource("/api/events");
  state.sse = es;

  es.addEventListener("trace_event", (msg) => {
    try {
      const payload = JSON.parse(msg.data).data || {};
      if (!state.currentId || payload.session_id !== state.currentId || !payload.event) return;
      const ev = payload.event;
      if (!state.events.some((x) => x.id === ev.id)) {
        state.events.push(ev);
        if (state.events.length > 150) state.events.shift();
      }
      if (state.session) {
        if (ev.event_type === "process.spawned" && state.session.active_process) {
          state.session.active_process.pid = ev.detail?.pid;
          state.session.active_process.latest_event = ev.summary;
        } else if (ev.event_type === "codex.thread.started" && state.session.active_process) {
          state.session.active_process.thread_id = ev.detail?.thread_id;
          state.session.active_process.latest_event = ev.summary;
        } else if (ev.category === "codex" && state.session.active_process) {
          state.session.active_process.latest_event = ev.summary;
        }
        renderTopology(state.session);
      }
      pulseAtmosphere(1.0);
      renderKernelDock();
    } catch { /* ignore */ }
  });

  es.addEventListener("sessions_changed", async (msg) => {
    try {
      const ids = JSON.parse(msg.data).data?.ids || [];
      await Promise.all([loadSessions(), loadModules()]);
      if (state.currentId && (!ids.length || ids.includes(state.currentId))) {
        await openSession(state.currentId);
      }
    } catch { /* ignore */ }
  });

  es.addEventListener("pipeline_changed", async () => { await loadPipeline(); });
  es.addEventListener("modules_changed", async () => { await Promise.all([loadModules(), loadPipeline()]); });
}

/* ==================== MODAL & EVENT WIRING ==================== */
function showModal({ title, bodyHtml, actions }) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHtml;
  const footer = $("modal-footer");
  footer.innerHTML = "";
  for (const act of actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `btn ${act.cls || "btn-secondary"} btn-sm`;
    btn.textContent = act.label;
    btn.addEventListener("click", async () => { closeModal(); if (act.onClick) await act.onClick(); });
    footer.appendChild(btn);
  }
  $("modal-backdrop").classList.remove("hidden");
}
function closeModal() { $("modal-backdrop").classList.add("hidden"); }

function initTheme() {
  const saved = localStorage.getItem("aisys-theme") || "dark";
  document.documentElement.dataset.theme = saved;
  $("btn-theme")?.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("aisys-theme", next);
  });
}

function bindEvents() {
  // 3-View Navigation
  $("main-nav")?.addEventListener("click", (e) => {
    const tab = e.target.closest?.(".nav-tab");
    if (tab?.dataset.view) switchView(tab.dataset.view);
  });
  $("btn-home")?.addEventListener("click", showComposer);
  $("btn-new-session")?.addEventListener("click", showComposer);
  $("btn-goto-logic-inline")?.addEventListener("click", () => switchView("logic"));
  $("btn-inspect-live-logic")?.addEventListener("click", () => switchView("logic"));

  $("backend-pills")?.addEventListener("click", () => switchView("modules"));

  $("modal-close")?.addEventListener("click", closeModal);
  $("modal-backdrop")?.addEventListener("click", (e) => { if (e.target === $("modal-backdrop")) closeModal(); });

  $("session-select")?.addEventListener("change", (e) => {
    openSession(e.target.value || null, { resetCache: true, focusStudio: true });
  });

  // Mode toggle
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.mode = btn.dataset.mode;
      document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b === btn));
      $("btn-toggle-project")?.classList.toggle("hidden", state.mode === "learn");
      if (state.mode === "learn") $("project-row")?.classList.add("hidden");
    });
  });

  $("btn-toggle-project")?.addEventListener("click", () => $("project-row")?.classList.toggle("hidden"));
  $("btn-validate-project")?.addEventListener("click", async () => {
    const path = $("project-input").value.trim();
    const hint = $("project-hint");
    if (!path) { hint.textContent = "请输入本地 Git 仓库绝对路径。"; return; }
    try {
      const info = await api("/api/validate-project", { method: "POST", body: { path } });
      hint.textContent = `校验通过 · 分支 ${info.branch} · HEAD ${info.head.slice(0, 8)}`;
    } catch (err) { hint.textContent = err.message; }
  });

  $("recent-list")?.addEventListener("click", (e) => {
    const item = e.target.closest?.("[data-open-session]");
    if (item) openSession(item.dataset.openSession, { resetCache: true, focusStudio: true });
  });

  // Launch new session
  const launchSession = async () => {
    const task = $("task-input").value.trim();
    if (!task) { toast("请先输入你的任务或研学命题", "error"); $("task-input").focus(); return; }
    const project = state.mode === "dev" ? ($("project-input").value.trim() || null) : null;
    const btn = $("btn-create-session");
    btn.disabled = true;
    try {
      const view = await api("/api/sessions", {
        method: "POST",
        body: { task, mode: state.mode, project, auto_start_codex: true },
      });
      $("task-input").value = "";
      pulseAtmosphere(1.8);
      await loadSessions();
      await openSession(view.id, { resetCache: true, focusStudio: true });
      toast("已按当前后端逻辑启动推演");
    } catch (err) { toast(err.message, "error"); }
    finally { btn.disabled = false; }
  };
  $("btn-create-session")?.addEventListener("click", launchSession);
  $("task-input")?.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); launchSession(); }
  });

  $("btn-trigger-codex")?.addEventListener("click", async () => {
    if (!state.currentId) return;
    try {
      state.session = await api(`/api/sessions/${state.currentId}/run-codex`, { method: "POST" });
      pulseAtmosphere(1.3);
      renderWorkspace();
      renderKernelDock();
      toast("已调度 Codex 后台沙箱执行");
    } catch (err) { toast(err.message, "error"); }
  });

  $("btn-cancel-session")?.addEventListener("click", () => {
    if (!state.currentId) return;
    showModal({
      title: "取消当前协作会话？",
      bodyHtml: "<p>取消后会话将归档，已有隔离产物保留且不再推进后续步骤。</p>",
      actions: [
        { label: "保留", cls: "btn-secondary" },
        {
          label: "确认取消",
          cls: "btn-primary",
          onClick: async () => {
            state.session = await api(`/api/sessions/${state.currentId}/cancel`, { method: "POST" });
            await loadSessions();
            renderWorkspace();
            toast("会话已取消");
          },
        },
      ],
    });
  });

  $("btn-stop-service")?.addEventListener("click", async () => {
    let activeCount = 0;
    try { activeCount = (await api("/api/health")).active_runs || 0; } catch { /* ignore */ }
    showModal({
      title: "停止 AIsys 本地服务？",
      bodyHtml: activeCount > 0 ? `<p>当前有 <strong>${activeCount}</strong> 个后台模型任务在运行，确认停服？</p>` : "<p>将优雅关闭本地服务，所有配置与会话均已持久化。</p>",
      actions: [
        { label: "取消", cls: "btn-secondary" },
        {
          label: "停止服务",
          cls: "btn-primary",
          onClick: async () => {
            await api("/api/server/shutdown", { method: "POST", body: { confirm_active: true } });
            toast("本地服务正在关闭…");
          },
        },
      ],
    });
  });

  // ===== VIEW 2: LOGIC CANVAS & EDITOR DELEGATION =====
  $("profile-pills")?.addEventListener("click", async (e) => {
    const pill = e.target.closest?.("[data-profile]");
    if (!pill) return;
    try {
      const res = await api("/api/pipeline/apply-profile", { method: "POST", body: { profile_id: pill.dataset.profile } });
      state.pipeline = res.config;
      state.pipelineDraft = deepClone(res.config);
      pulseAtmosphere(1.3);
      updateBrandProfileTag();
      renderComposerActiveModels();
      await loadModules();
      renderLogicView();
      toast(`已切换至预设：${pill.textContent.trim()}`);
    } catch (err) { toast(err.message, "error"); }
  });

  $("btn-reset-pipeline")?.addEventListener("click", async () => {
    try {
      const res = await api("/api/pipeline/reset", { method: "POST" });
      state.pipeline = res.config;
      state.pipelineDraft = deepClone(res.config);
      updateBrandProfileTag();
      renderComposerActiveModels();
      await loadModules();
      renderLogicView();
      toast("后端逻辑已恢复默认配置");
    } catch (err) { toast(err.message, "error"); }
  });

  $("btn-save-pipeline")?.addEventListener("click", savePipelineDraft);

  const handleCfgToggle = async (e) => {
    const sw = e.target.closest?.("[data-cfg-toggle]");
    if (!sw || !state.pipelineDraft) return;
    const path = sw.dataset.cfgToggle;
    const prev = Boolean(getNestedValue(state.pipelineDraft, path));
    setNestedValue(state.pipelineDraft, path, !prev);
    try {
      const res = await api("/api/pipeline", { method: "PUT", body: state.pipelineDraft });
      state.pipeline = res.config;
      state.pipelineDraft = deepClone(res.config);
      pulseAtmosphere(0.9);
      renderComposerActiveModels();
      await loadModules();
      renderLogicView();
    } catch (err) {
      setNestedValue(state.pipelineDraft, path, prev);
      renderLogicView();
      toast(err.message, "error");
    }
  };
  $("logic-switches-bar")?.addEventListener("click", handleCfgToggle);
  $("logic-inspector-body")?.addEventListener("click", handleCfgToggle);

  $("logic-graph-wrap")?.addEventListener("click", (e) => {
    const node = e.target.closest?.("[data-select-node]");
    if (!node) return;
    state.selectedNodeId = node.dataset.selectNode;
    renderLogicView();
  });

  $("logic-inspector-body")?.addEventListener("input", (e) => {
    const field = e.target.closest?.("[data-bind]");
    if (!field || !state.pipelineDraft) return;
    const val = field.dataset.type === "int" ? Number.parseInt(field.value || "0", 10) : field.value;
    setNestedValue(state.pipelineDraft, field.dataset.bind, val);
  });

  // ===== VIEW 3: MODULE CONTROL DELEGATION =====
  $("btn-refresh-modules")?.addEventListener("click", async () => {
    await loadModules();
    toast("已刷新全部模块实时状态");
  });

  const runStartAll = async (focusAntigravity) => {
    try {
      if (focusAntigravity) {
        const trigger = state.pipeline?.gemini?.trigger_command || "/aisys";
        try { await navigator.clipboard.writeText(trigger); } catch { /* fallback via pbcopy */ }
      }
      const res = await api("/api/modules/start-all", {
        method: "POST",
        body: { focus_antigravity: focusAntigravity },
      });
      state.modulesSnapshot = res.snapshot;
      pulseAtmosphere(2.0);
      await loadPipeline();
      renderTopEnginePills();
      renderModulesBadge();
      renderModulesView();
      toast(focusAntigravity
        ? "全部模块已启动并通过实测，已跳转 Antigravity — 按 ⌘V 回车确认即可"
        : "全部模块已一键启动并完成真实连通实测");
    } catch (err) { toast(err.message, "error"); }
  };

  $("btn-start-all-modules")?.addEventListener("click", () => runStartAll(false));
  $("btn-start-all-and-antigravity")?.addEventListener("click", () => runStartAll(true));
  $("btn-express-antigravity")?.addEventListener("click", () => jumpToAntigravity());

  $("modules-grid")?.addEventListener("click", async (e) => {
    const toggleBtn = e.target.closest?.("[data-mod-toggle]");
    if (toggleBtn) {
      const modId = toggleBtn.dataset.modToggle;
      const next = toggleBtn.getAttribute("aria-checked") !== "true";
      try {
        const res = await api(`/api/modules/${modId}/toggle`, { method: "POST", body: { enabled: next } });
        state.modulesSnapshot = res.snapshot;
        state.pipeline = res.config;
        state.pipelineDraft = deepClone(res.config);
        pulseAtmosphere(1.0);
        renderTopEnginePills();
        renderModulesBadge();
        renderComposerActiveModels();
        renderModulesView();
        toast(`${modId} 已${next ? "开启" : "暂停"}`);
      } catch (err) { toast(err.message, "error"); }
      return;
    }

    const actBtn = e.target.closest?.("[data-mod-action]");
    if (actBtn) {
      const modId = actBtn.dataset.modId;
      const action = actBtn.dataset.modAction;
      actBtn.disabled = true;
      try {
        if (modId === "antigravity_bridge" && action === "start") {
          const trigger = state.pipeline?.gemini?.trigger_command || "/aisys";
          try { await navigator.clipboard.writeText(trigger); } catch { /* pbcopy handles */ }
        }
        const res = await api(`/api/modules/${modId}/action`, { method: "POST", body: { action } });
        state.modulesSnapshot = res.snapshot;
        pulseAtmosphere(1.2);
        await loadPipeline();
        renderTopEnginePills();
        renderModulesBadge();
        renderModulesView();
        const detail = res.result?.detail || (res.result?.focused ? `已唤起 ${res.result.app}，按 ⌘V 回车确认` : "操作已完成");
        toast(detail, res.result?.ok === false ? "error" : "info");
      } catch (err) { toast(err.message, "error"); }
      finally { actBtn.disabled = false; }
    }
  });

  // ===== KERNEL TELEMETRY DOCK =====
  const toggleKernel = () => { state.kernelOpen = !state.kernelOpen; renderKernelDock(); };
  $("kernel-dock-toggle")?.addEventListener("click", toggleKernel);
  $("kernel-dock-toggle")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleKernel(); }
  });
  $("kernel-filters")?.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".kfilter");
    if (!btn) return;
    state.kernelFilter = btn.dataset.cat || "all";
    document.querySelectorAll(".kfilter").forEach((b) => b.classList.toggle("active", b === btn));
    renderKernelDock();
  });
  $("kernel-stream")?.addEventListener("click", (e) => {
    const row = e.target.closest?.(".trace-row");
    if (!row) return;
    const id = row.dataset.evId;
    if (state.expandedEventIds.has(id)) state.expandedEventIds.delete(id);
    else state.expandedEventIds.add(id);
    renderKernelDock();
  });

  // Smooth live elapsed timer while Codex subprocess runs
  setInterval(() => {
    const proc = state.session?.active_process;
    if (!proc || !proc.started_at) return;
    proc.elapsed_s = Math.max(0, Date.now() / 1000 - proc.started_at);
    const liveEl = $("topo-live-note");
    if (liveEl) liveEl.textContent = `LIVE · ${proc.step} · ${proc.elapsed_s.toFixed(1)}s`;
    const subEl = $("topo-codex-sub");
    if (subEl) subEl.textContent = `PID ${proc.pid || "…"} · ${proc.elapsed_s.toFixed(1)}s · ${(proc.latest_event || "").slice(0, 22)}`;
    const ticker = $("kernel-ticker");
    if (ticker) ticker.textContent = `CODEX · ${proc.step} · PID ${proc.pid || "…"} · ${proc.elapsed_s.toFixed(1)}s · ${proc.latest_event || ""}`;
    const waitEl = $("wait-codex-telemetry");
    if (waitEl) {
      waitEl.innerHTML = [
        proc.pid ? `<span>PID ${esc(proc.pid)}</span>` : "",
        proc.thread_id ? `<span>thread ${esc(proc.thread_id.slice(0, 8))}</span>` : "",
        `<span>耗时 ${proc.elapsed_s.toFixed(1)}s</span>`,
        proc.latest_event ? `<span>${esc(proc.latest_event)}</span>` : "",
      ].filter(Boolean).join("");
    }
  }, 500);
}

async function boot() {
  initTheme();
  initAmbientCanvas();
  initSpotlightCards();
  bindEvents();
  connectSSE();
  await Promise.all([loadPipeline(), loadModules(), loadSessions()]);
  updateKernelDockVisibility();
}

boot();
