/* ============================================================================
   AIsys · Dual-Model Co-Creation Studio (Frontend Application)
   Claude-Minimalist × Obsidian-Emerald Bioluminescence + Real Kernel Telemetry
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

const state = {
  mode: "dev",
  sessions: [],
  currentId: null,
  session: null,
  events: [],
  expandedEventIds: new Set(),
  kernelOpen: false,
  kernelFilter: "all",
  backends: null,
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
    const rows = tableBuf.map((r) =>
      r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim())
    );
    const bodyRows = rows.filter((_, i) => !(i === 1 && rows[1].every((c) => /^:?-+:?$/.test(c))));
    if (bodyRows.length) {
      html.push("<table><thead><tr>");
      bodyRows[0].forEach((cell) => html.push(`<th>${inline(cell)}</th>`));
      html.push("</tr></thead><tbody>");
      bodyRows.slice(1).forEach((row) => {
        html.push("<tr>");
        row.forEach((cell) => html.push(`<td>${inline(cell)}</td>`));
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
      if (collectToc && lvl >= 2 && lvl <= 3) {
        toc.push({ id: hid, level: lvl, text });
      }
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

    if (!trimmed) {
      closeLists();
      continue;
    }

    closeLists();
    html.push(`<p>${inline(trimmed)}</p>`);
  }

  closeLists();
  flushTable();

  return { html: html.join("\n"), toc };
}

/* ==================== LIVING AMBIENT CANVAS & SPOTLIGHT ==================== */
function pulseAtmosphere(strength = 1.0) {
  state.ambientBurst = Math.min(2.4, state.ambientBurst + strength);
}

function initAmbientCanvas() {
  const canvas = $("ambient-canvas");
  if (!canvas) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  const pointer = { x: -9999, y: -9999, active: false };
  const ripples = [];

  const resize = () => {
    width = window.innerWidth;
    height = window.innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  resize();
  window.addEventListener("resize", resize);

  window.addEventListener("pointermove", (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.active = true;
  }, { passive: true });

  window.addEventListener("pointerleave", () => { pointer.active = false; });

  const count = 48;
  const particles = Array.from({ length: count }, (_, i) => ({
    x: Math.random() * width,
    y: Math.random() * height,
    vx: (Math.random() - 0.5) * 0.24,
    vy: (Math.random() - 0.5) * 0.22 - 0.04,
    r: 1.1 + Math.random() * 1.9,
    hue: i % 3 === 0 ? "8, 241, 212" : "52, 211, 153",
    phase: Math.random() * Math.PI * 2,
  }));

  let lastFrame = performance.now();
  const loop = (now) => {
    const dt = Math.min(0.05, (now - lastFrame) / 1000);
    lastFrame = now;

    ctx.clearRect(0, 0, width, height);

    const active = Boolean(state.session?.codex_busy || state.session?.active_process);
    document.body.classList.toggle("engine-active", active);

    if (state.ambientBurst > 0.05) {
      if (ripples.length < 3 && state.ambientBurst > 0.7) {
        ripples.push({ x: width * 0.5, y: 185, r: 24, alpha: 0.26 });
      }
      state.ambientBurst = Math.max(0, state.ambientBurst - dt * 1.35);
    }

    // Subtle energy ripples on real backend SSE events
    for (let i = ripples.length - 1; i >= 0; i--) {
      const rp = ripples[i];
      rp.r += dt * 210;
      rp.alpha -= dt * 0.22;
      if (rp.alpha <= 0) {
        ripples.splice(i, 1);
        continue;
      }
      ctx.beginPath();
      ctx.arc(rp.x, rp.y, rp.r, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(8, 241, 212, ${rp.alpha.toFixed(3)})`;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }

    const speedBoost = active ? 1.85 : 1 + state.ambientBurst * 0.65;
    for (const p of particles) {
      p.phase += dt * (1.1 + (active ? 0.8 : 0));
      p.x += p.vx * speedBoost;
      p.y += p.vy * speedBoost;

      if (pointer.active) {
        const dx = pointer.x - p.x;
        const dy = pointer.y - p.y;
        const dist2 = dx * dx + dy * dy;
        if (dist2 > 1600 && dist2 < 32000) {
          p.x += dx * 0.0018;
          p.y += dy * 0.0018;
        }
      }

      if (p.x < -16) p.x = width + 16;
      if (p.x > width + 16) p.x = -16;
      if (p.y < -16) p.y = height + 16;
      if (p.y > height + 16) p.y = -16;

      const glowAlpha = (0.24 + 0.24 * Math.sin(p.phase)) * (active ? 1.3 : 1);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${p.hue}, ${Math.min(0.82, glowAlpha).toFixed(3)})`;
      ctx.fill();
    }

    // Subtle synaptic filaments between close spores
    const maxDist = active ? 132 : 108;
    const maxDist2 = maxDist * maxDist;
    ctx.lineWidth = 0.7;
    for (let i = 0; i < count; i++) {
      const a = particles[i];
      for (let j = i + 1; j < count; j++) {
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < maxDist2) {
          const alpha = (1 - Math.sqrt(d2) / maxDist) * (active ? 0.14 : 0.075);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(52, 211, 153, ${alpha.toFixed(3)})`;
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

/* ==================== BACKEND PILLS & TOP HUD ==================== */
async function loadBackends() {
  try {
    state.backends = await api("/api/backends");
    renderBackendPills();
  } catch {
    // non-fatal
  }
}

function renderBackendPills() {
  const el = $("backend-pills");
  if (!el || !state.backends) return;
  const g = state.backends.google || {};
  const c = state.backends.codex || {};

  const codexRunning = c.status === "running" || Boolean(state.session?.codex_busy);
  const codexCls = codexRunning ? "chip-ok is-running" : c.status === "ready" ? "chip-ok" : "chip-err";
  const codexText = codexRunning ? "Codex · 沙箱推演中" : c.status === "ready" ? "Codex · 就绪" : `Codex · ${c.status || "未就绪"}`;

  const geminiWaiting = Boolean(state.session?.next_action?.pending_gemini?.length);
  const geminiCls = geminiWaiting ? "chip-mint is-running" : "chip-mint";
  const geminiText = geminiWaiting ? "Gemini · 等待 /aisys 同步" : "Gemini · Antigravity 已配置";

  el.innerHTML = `
    <span class="status-chip ${geminiCls}" title="${esc(g.detail || "")}">
      <span class="pulse-dot"></span>${esc(geminiText)}
    </span>
    <span class="status-chip ${codexCls}" title="${esc(c.detail || "")}">
      <span class="pulse-dot"></span>${esc(codexText)}
    </span>
  `;
}

/* ==================== SESSIONS LIST & SWITCHING ==================== */
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
    const title = s.task.length > 24 ? `${s.task.slice(0, 24)}…` : s.task;
    const selected = s.id === state.currentId ? " selected" : "";
    opts.push(`<option value="${esc(s.id)}"${selected}>[${modeTag}] ${esc(title)} · ${esc(stageLbl)}</option>`);
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
  list.innerHTML = state.sessions.slice(0, 5).map((s) => `
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
  renderSessionSelector();
  renderBackendPills();
  renderKernelDock();
}

async function openSession(sessionId, { resetCache = false } = {}) {
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
  renderSessionSelector();
  renderBackendPills();
  renderWorkspace();
  renderKernelDock();

  if (view.stage === "completed") {
    if (view.mode === "learn" && !state.studyGuideCache) {
      loadStudyGuide(sessionId);
    } else if (view.mode === "dev" && !state.diffCache) {
      loadDiff(sessionId);
    }
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

/* ==================== WORKSPACE HEADER, STEPPER & LIVE TOPOLOGY ==================== */
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

  // Pipeline stepper
  document.querySelectorAll("#stepper li").forEach((li) => {
    const step = Number(li.dataset.step);
    li.classList.remove("active", "done");
    if (s.stage === "completed") {
      li.classList.add("done");
    } else if (step < meta.step) {
      li.classList.add("done");
    } else if (step === meta.step) {
      li.classList.add("active");
    }
  });

  // Error banner
  const errEl = $("s-error-banner");
  if (s.error) {
    errEl.textContent = s.error;
    errEl.classList.remove("hidden");
  } else {
    errEl.classList.add("hidden");
  }

  // Trigger Codex button state
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
    : s.next_action?.codex_ready
      ? "等待调度"
      : s.stage === "completed"
        ? `已完成 · ${Number(m.codex_duration_s || 0).toFixed(1)}s`
        : "空闲待命";

  const codexDetail = proc?.latest_event
    ? proc.latest_event.slice(0, 26)
    : proc?.thread_id
      ? `thread ${proc.thread_id.slice(0, 8)}`
      : `${fmtTokens(m.total_tokens || 0)} tok`;

  const geminiSub = geminiPending
    ? `待执行 ${s.next_action.pending_gemini.length} 项 (/aisys)`
    : s.stage === "deciding"
      ? "等待你的拍板"
      : s.stage === "completed"
        ? "全部步骤已归档"
        : "已同步待命";

  const lockSub = lockCount
    ? `ALLOW ${m.lock_allows || 0} · DENY ${m.lock_denies || 0}`
    : s.locked_plan
      ? `${s.locked_plan.subtasks.length} 路径锁生效`
      : "防重叠校验待命";

  const liveNote = proc
    ? `LIVE · ${proc.step} · ${Number(proc.elapsed_s || 0).toFixed(1)}s`
    : geminiPending
      ? `等待 Antigravity 接入 (${s.next_action.pending_gemini[0].action})`
      : s.stage === "deciding"
        ? "双模型方案就绪 · 等待你的拍板或融合微调"
        : `状态机：${STAGE_META[s.stage]?.label || s.stage}`;

  const eKernelToCodex = codexLive ? "active-codex" : (m.codex_duration_s > 0 ? "done-edge" : "");
  const eKernelToGemini = geminiPending ? "active-gemini" : (Object.keys(s.proposals || {}).includes("gemini") ? "done-edge" : "");
  const eToLock = s.locked_plan ? (codexLive ? "active-codex" : "done-edge") : "";

  host.innerHTML = `
    <div class="topo-head">
      <div class="topo-title">
        <span class="pulse-dot ${codexLive || geminiPending ? "dot-live" : ""}"></span>
        <span>LIVE EXECUTION TOPOLOGY · 真实后台链路</span>
      </div>
      <div class="topo-live-note" id="topo-live-note">${esc(liveNote)}</div>
    </div>
    <div class="topo-svg-wrap">
      <svg class="topo-svg" viewBox="0 0 920 108" fill="none" aria-label="实时后台执行拓扑">
        <!-- Edges -->
        <path class="topo-edge done-edge" d="M168 54 L228 54"/>
        <path class="topo-edge ${eKernelToGemini}" d="M402 44 C444 44, 444 26, 492 26"/>
        <path class="topo-edge ${eKernelToCodex}" d="M402 64 C444 64, 444 82, 492 82"/>
        <path class="topo-edge ${eToLock}" d="M688 26 C722 26, 722 46, 748 48"/>
        <path class="topo-edge ${eToLock}" d="M688 82 C722 82, 722 62, 748 60"/>

        <!-- Node 1: Intent -->
        <rect class="topo-node-box" x="6" y="24" width="162" height="60" rx="12"/>
        <text class="topo-node-title" x="22" y="49">任务与模式</text>
        <text class="topo-node-sub" x="22" y="68">${s.mode === "learn" ? "LEARN · Markdown" : "DEV · Git Snapshot"}</text>

        <!-- Node 2: AIsys Kernel -->
        <rect class="topo-node-box ${codexLive ? "is-codex-live" : ""}" x="228" y="20" width="174" height="68" rx="12"/>
        <text class="topo-node-title" x="244" y="45">AIsys 状态引擎</text>
        <text class="topo-node-sub accent-jade" x="244" y="63">${esc(s.stage.toUpperCase())}</text>
        <text class="topo-node-sub" x="244" y="78">${m.event_count || 0} 条真实事件</text>

        <!-- Node 3A: Gemini -->
        <rect class="topo-node-box ${geminiPending ? "is-gemini-live" : ""}" x="492" y="4" width="196" height="44" rx="11"/>
        <text class="topo-node-title" x="508" y="23">Gemini · Antigravity</text>
        <text class="topo-node-sub accent-mint" x="508" y="39">${esc(geminiSub)}</text>

        <!-- Node 3B: Codex -->
        <rect class="topo-node-box ${codexLive ? "is-codex-live" : ""}" x="492" y="60" width="196" height="44" rx="11"/>
        <text class="topo-node-title" x="508" y="79">Codex · CLI 沙箱</text>
        <text class="topo-node-sub accent-jade" id="topo-codex-sub" x="508" y="95">${esc(codexSub)} · ${esc(codexDetail)}</text>

        <!-- Node 4: Write-Lock Sandbox -->
        <rect class="topo-node-box" x="748" y="20" width="166" height="68" rx="12"/>
        <text class="topo-node-title" x="764" y="45">互斥写锁与工作区</text>
        <text class="topo-node-sub accent-jade" x="764" y="64">${esc(lockSub)}</text>
        <text class="topo-node-sub" x="764" y="79">${(s.artifacts || []).length} 个隔离产物</text>
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

  const actionLabels = {
    propose: "提交阶段 1 独立初案",
    critique: "审阅 Codex 初案并给出无重叠分工",
    merge: "按你的微调指令融合生成最终锁定分工",
    execute_subtask: `完成独占子任务（${pending.map((p) => p.task_id).join(" / ")}）`,
    review_peer: `对 Codex 产出执行第 ${s.review_round} 轮交叉审查`,
  };
  const label = actionLabels[pending[0].action] || pending[0].action;

  bar.classList.remove("hidden");
  bar.innerHTML = `
    <div class="handshake-copy">
      <div class="handshake-title">
        <span class="pulse-dot dot-live" style="color: var(--jade-glow)"></span>
        <span>待 Gemini 推进：${esc(label)}</span>
      </div>
      <div class="handshake-sub">
        Antigravity 全局桥接已预配置就绪 — 点击右侧按钮自动复制 <code>/aisys</code> 并切到 Antigravity，按下 <code>⌘V</code> 回车即可全自动连续推进。
      </div>
    </div>
    <div class="handshake-actions">
      <button type="button" class="btn btn-mint btn-sm" id="btn-launch-antigravity">
        <svg class="ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M9.5 2.5h4v4M13.5 2.5L7.5 8.5M6.5 3.5H4A1.5 1.5 0 0 0 2.5 5v7A1.5 1.5 0 0 0 4 13.5h7a1.5 1.5 0 0 0 1.5-1.5V9.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>一键复制 /aisys 并唤起 Antigravity</span>
      </button>
      <button type="button" class="btn btn-secondary btn-sm" id="btn-copy-slash">仅复制 /aisys</button>
    </div>
  `;

  $("btn-launch-antigravity")?.addEventListener("click", async () => {
    await copySlashTrigger();
    try {
      const res = await api("/api/focus-antigravity", { method: "POST" });
      if (res.focused) {
        toast(`已复制 /aisys 并切到 ${res.app}，直接 ⌘V 回车即可`);
      } else {
        toast("已复制 /aisys，切回 Antigravity 粘贴回车即可");
      }
    } catch {
      toast("已复制 /aisys，切回 Antigravity 粘贴回车即可");
    }
  });

  $("btn-copy-slash")?.addEventListener("click", async () => {
    await copySlashTrigger();
    toast("已复制极简指令：/aisys");
  });
}

async function copySlashTrigger() {
  const cmd = "/aisys";
  try {
    await navigator.clipboard.writeText(cmd);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = cmd;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

/* ==================== WAVE SWIMLANE ANTI-OVERLAP MATRIX ==================== */
function renderDivisionMatrix(subtasks, divisionCheck) {
  if (!subtasks || !subtasks.length) {
    return `<div class="callout-box">尚未提供结构化子任务分工表。</div>`;
  }
  const byId = Object.fromEntries(subtasks.map((t) => [t.id, t]));
  const check = divisionCheck || { valid: false, conflicts: ["尚未校验"], waves: [] };
  const waves = check.waves && check.waves.length ? check.waves : [subtasks.map((t) => t.id)];

  const sealHtml = check.valid
    ? `<div class="division-seal valid">
         <span>机械防重叠校验通过 · 0 路径冲突 · 无环依赖 · 共 ${waves.length} 个执行波次</span>
         <span class="mono">${subtasks.length} TASKS LOCKED</span>
       </div>`
    : `<div class="division-seal invalid">
         <span>检测到分工冲突：${esc((check.conflicts || []).join("；"))}</span>
         <span class="mono">BLOCKED</span>
       </div>`;

  const lanesHtml = waves.map((waveIds, idx) => {
    const cards = waveIds.map((tid) => {
      const st = byId[tid];
      if (!st) return "";
      const isG = st.owner === "gemini";
      return `
        <div class="wave-task-card owner-${esc(st.owner)}">
          <div class="wt-top">
            <span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(st.id)}</span>
            <span class="pill ${isG ? "pill-gemini" : "pill-codex"}">${isG ? "Gemini" : "Codex"}</span>
          </div>
          <div class="wt-title">${esc(st.title)}</div>
          <div class="wt-paths">
            ${(st.owned_paths || []).map((p) => `<span class="lock-chip">🔒 ${esc(p)}</span>`).join("")}
          </div>
          ${st.depends_on?.length ? `<div class="dep-chip">前置依赖：${esc(st.depends_on.join(", "))}</div>` : ""}
        </div>
      `;
    }).join("");

    return `
      <div class="wave-lane">
        <div class="wave-header">
          <span>WAVE 0${idx + 1} · ${waveIds.length > 1 ? "可并行互斥推进" : "顺序波次"}</span>
          <span>${waveIds.length} 项独占子任务</span>
        </div>
        <div class="wave-tasks">${cards}</div>
      </div>
    `;
  }).join("");

  return `<div class="division-block">${sealHtml}${lanesHtml}</div>`;
}

/* ==================== STAGE RENDERERS ==================== */
function renderModelWaitBox(model, s) {
  const isCodex = model === "codex";
  const proc = isCodex ? s.active_process : null;
  const running = isCodex && Boolean(s.codex_busy || proc);

  if (isCodex && running) {
    return `
      <div class="live-wait-box">
        <div class="wait-status-row">
          <span class="radar-orb"></span>
          <span>${esc(proc?.step || "Codex 正在隔离沙箱中推演…")}</span>
        </div>
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
      <div class="wait-status-row">
        <span class="radar-orb ${isCodex ? "" : "mint"}"></span>
        <span>${isCodex ? "等待 Codex 后台执行" : "等待 Gemini 通过 Antigravity (/aisys) 同步结果"}</span>
      </div>
      <div class="wait-telemetry">
        <span>${isCodex ? "可点击右上角「运行 Codex」立即触发" : "点击上方绿色按钮一键复制 /aisys 即可"}</span>
      </div>
    </div>
  `;
}

function renderProposalCard(model, prop, s) {
  const isG = model === "gemini";
  const header = `
    <div class="model-card-head">
      <div class="model-identity">
        <span class="model-badge-ring ${model}">
          <svg class="ico" viewBox="0 0 16 16" fill="none">
            ${isG
              ? '<path d="M8 2.2l1.6 4.2L13.8 8l-4.2 1.6L8 13.8l-1.6-4.2L2.2 8l4.2-1.6L8 2.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
              : '<path d="M5 4.5L2 8l3 3.5M11 4.5L14 8l-3 3.5M9 3.5L7 12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'}
          </svg>
        </span>
        <div>
          <div class="model-name">${isG ? "Gemini · Antigravity" : "Codex · ChatGPT CLI"}</div>
          <div class="model-meta">${prop ? `${prop.duration ? `${prop.duration}s · ` : ""}${prop.usage?.output_tokens ? `${fmtTokens((prop.usage.input_tokens || 0) + (prop.usage.output_tokens || 0))} tok` : "独立提案已提交"}` : "阶段 1 独立思考中"}</div>
        </div>
      </div>
      <span class="pill ${prop ? "pill-ok" : "pill-warn"}">${prop ? "已提交" : "进行中"}</span>
    </div>
  `;

  if (!prop) {
    return `<article class="model-card glass-surface spotlight-card is-${model}">${header}${renderModelWaitBox(model, s)}</article>`;
  }

  return `
    <article class="model-card glass-surface spotlight-card is-${model}">
      ${header}
      <p class="summary-lead">${esc(prop.summary)}</p>
      <div class="md-body">${renderMarkdown(prop.approach).html}</div>
      ${(prop.strengths?.length || prop.risks?.length) ? `
        <div class="critique-grid">
          <div class="callout-box">
            <div class="callout-label">核心亮点</div>
            <ul class="tag-list">${(prop.strengths || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
          </div>
          <div class="callout-box callout-amber">
            <div class="callout-label">风险与边界</div>
            <ul class="tag-list risks">${(prop.risks || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
          </div>
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
          <svg class="ico" viewBox="0 0 16 16" fill="none">
            ${isG
              ? '<path d="M8 2.2l1.6 4.2L13.8 8l-4.2 1.6L8 13.8l-1.6-4.2L2.2 8l4.2-1.6L8 2.2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'
              : '<path d="M5 4.5L2 8l3 3.5M11 4.5L14 8l-3 3.5M9 3.5L7 12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'}
          </svg>
        </span>
        <div>
          <div class="model-name">${isG ? "Gemini 互审升级方案" : "Codex 互审升级方案"}</div>
          <div class="model-meta">${ref ? `${ref.subtasks?.length || 0} 个子任务${ref.duration ? ` · ${ref.duration}s` : ""}` : "等待交叉互审"}</div>
        </div>
      </div>
      <span class="pill ${ref ? (ref.division_check?.valid ? "pill-ok" : "pill-err") : "pill-warn"}">
        ${ref ? (ref.division_check?.valid ? "分工零重叠" : "分工待修正") : "审查中"}
      </span>
    </div>
  `;

  if (!ref) {
    return `<article class="model-card glass-surface spotlight-card is-${model}">${header}${renderModelWaitBox(model, s)}</article>`;
  }

  return `
    <article class="model-card glass-surface spotlight-card is-${model}">
      ${header}
      <p class="summary-lead">${esc(ref.summary)}</p>
      <div class="critique-grid">
        <div class="callout-box callout-amber">
          <div class="callout-label">对对方的审视与挑刺</div>
          <div>${esc(ref.critique_of_peer)}</div>
        </div>
        <div class="callout-box">
          <div class="callout-label">吸收对方的优点</div>
          <div>${esc(ref.adopted_from_peer)}</div>
        </div>
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
        <div class="decision-head">
          <h3>正在按你的融合微调指令生成最终锁定分工</h3>
          <p>微调指令：${esc(s.decision?.fusion_notes || "修正分工冲突并融合双方方案")}</p>
        </div>
        <div class="live-wait-box">
          <div class="wait-status-row">
            <span class="radar-orb mint"></span>
            <span>等待 Gemini 提交最终无重叠锁定方案（点击上方一键复制 /aisys 推进）</span>
          </div>
        </div>
      </section>
    `;
  }

  if (s.stage !== "deciding") return "";

  const isMerge = state.decisionChoice === "merge";
  return `
    <section class="decision-card glass-surface">
      <div class="decision-head">
        <h3>由你拍板：确定最终执行方案</h3>
        <p>双方已完成独立思考与交叉互审。你可以直接采纳其中一方，或写下融合微调意见，由系统机械锁死互斥路径后立即开工。</p>
      </div>
      <div class="decision-buttons">
        <button type="button" class="decision-option ${state.decisionChoice === "gemini" ? "is-active" : ""}" data-choice="gemini">
          <strong>采纳 Gemini 升级方案</strong>
          <span>直接按 Gemini 的零重叠分工锁定并进入协同执行</span>
        </button>
        <button type="button" class="decision-option ${state.decisionChoice === "codex" ? "is-active" : ""}" data-choice="codex">
          <strong>采纳 Codex 升级方案</strong>
          <span>直接按 Codex 的零重叠分工锁定并进入协同执行</span>
        </button>
        <button type="button" class="decision-option ${state.decisionChoice === "merge" ? "is-active" : ""}" data-choice="merge">
          <strong>融合微调（结合双方所长）</strong>
          <span>自由指定保留哪部分架构、调整谁负责哪个模块</span>
        </button>
      </div>

      ${isMerge ? `
        <div class="fusion-panel">
          <textarea id="fusion-notes-input" class="fusion-textarea"
            placeholder="例如：采用 Gemini 的章节框架，但把第 2 章代码案例与边界陷阱全部交给 Codex 实现，并增加一节并发调试实战…"></textarea>
          <div class="fusion-footer">
            <label class="meta-tag">
              <span>基础底稿：</span>
              <select id="fusion-base-select" class="hud-select">
                <option value="gemini"${state.fusionBase === "gemini" ? " selected" : ""}>Gemini 升级方案</option>
                <option value="codex"${state.fusionBase === "codex" ? " selected" : ""}>Codex 升级方案</option>
              </select>
            </label>
            <button type="button" class="btn btn-primary" id="btn-confirm-decision">提交融合微调并生成锁定分工</button>
          </div>
        </div>
      ` : `
        <div class="fusion-footer" style="margin-top: 14px;">
          <span class="meta-tag">确认后立即启用机械文件写锁，并自动拉起 Codex 执行就绪波次。</span>
          <button type="button" class="btn btn-primary" id="btn-confirm-decision">锁定方案并启动协同执行</button>
        </div>
      `}
    </section>
  `;
}

function renderExecutionStage(s) {
  const plan = s.locked_plan;
  if (!plan) return "";

  const statusLabels = {
    pending: ["待启动", "pill"],
    running: ["执行中", "pill-codex"],
    completed: ["已完成", "pill-ok"],
    needs_fix: ["待定向返工", "pill-warn"],
    failed: ["失败", "pill-err"],
  };

  const cards = plan.subtasks.map((st) => {
    const ex = s.executions?.[st.id] || { status: "pending" };
    const [lbl, cls] = statusLabels[ex.status] || [ex.status, "pill"];
    const isG = st.owner === "gemini";
    return `
      <article class="exec-card glass-surface spotlight-card status-${esc(ex.status)}">
        <div class="model-card-head">
          <div class="badge-row" style="margin:0">
            <span class="mono" style="font-size:12px;color:var(--text-muted)">${esc(st.id)}</span>
            <span class="pill ${isG ? "pill-gemini" : "pill-codex"}">${isG ? "Gemini" : "Codex"}</span>
          </div>
          <span class="pill ${cls}">${esc(lbl)}</span>
        </div>
        <div style="font-size:15px;font-weight:600;color:var(--text)">${esc(st.title)}</div>
        <div style="font-size:13px;color:var(--text-muted)">${esc(st.objective)}</div>
        <div class="wt-paths">
          ${(st.owned_paths || []).map((p) => `<span class="lock-chip">🔒 ${esc(p)}</span>`).join("")}
        </div>
        ${ex.review_feedback ? `<div class="exec-feedback">交叉审查定向返工意见：\n${esc(ex.review_feedback)}</div>` : ""}
        ${ex.summary ? `<div class="callout-box"><div class="callout-label">执行摘要</div><div>${esc(ex.summary)}</div></div>` : ""}
        ${ex.error ? `<div class="alert-banner alert-error">${esc(ex.error)}</div>` : ""}
        ${ex.duration ? `<div class="model-meta">耗时 ${ex.duration}s · 尝试 ${ex.attempts || 1} 次</div>` : ""}
      </article>
    `;
  }).join("");

  return `
    <section class="glass-surface" style="padding:18px 20px;">
      <div class="badge-row" style="margin-bottom:10px">
        <span class="pill pill-ok">锁定来源：${esc(plan.source.toUpperCase())}</span>
        ${plan.fusion_notes ? `<span class="meta-tag">微调意见：${esc(plan.fusion_notes)}</span>` : ""}
      </div>
      ${renderDivisionMatrix(plan.subtasks, plan.division_check)}
    </section>
    <div class="exec-grid">${cards}</div>
  `;
}

function renderReviewAndDelivery(s) {
  const renderRevCard = (reviewer) => {
    const rev = s.reviews?.[reviewer] || [...(s.review_history || [])].reverse().find((r) => r.reviewer === reviewer);
    const target = reviewer === "gemini" ? "Codex" : "Gemini";
    const isG = reviewer === "gemini";
    if (!rev) {
      return `
        <article class="model-card glass-surface spotlight-card is-${reviewer}">
          <div class="model-card-head">
            <div class="model-name">${isG ? "Gemini" : "Codex"} 审查 ${target}</div>
            <span class="pill pill-warn">审查中</span>
          </div>
          ${renderModelWaitBox(reviewer, s)}
        </article>
      `;
    }
    return `
      <article class="model-card glass-surface spotlight-card is-${reviewer}">
        <div class="model-card-head">
          <div>
            <div class="model-name">${isG ? "Gemini" : "Codex"} 独立审查 ${target}</div>
            <div class="model-meta">第 ${rev.round} 轮审查${rev.duration ? ` · ${rev.duration}s` : ""}</div>
          </div>
          <span class="pill ${rev.verdict === "pass" ? "pill-ok" : "pill-warn"}">${rev.verdict === "pass" ? "审查通过 PASS" : "要求返工 REWORK"}</span>
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
          ? `<nav class="guide-toc" aria-label="文档目录">
               <div class="toc-title">学习大纲</div>
               ${rendered.toc.map((t) => `<a class="toc-link lvl-${t.level}" href="#${esc(t.id)}">${esc(t.text)}</a>`).join("")}
             </nav>`
          : "";
        deliverableHtml = `
          <section class="deliverable-card glass-surface">
            <div class="deliverable-head">
              <div>
                <h3>结构化 Markdown 学习文档</h3>
                <div class="model-meta">${esc(s.final_summary || "双模型协同编译与交叉审查归档")}</div>
              </div>
              <a class="btn btn-primary btn-sm" href="/api/sessions/${esc(s.id)}/study-guide?download=true" download>
                <svg class="ico" viewBox="0 0 16 16" fill="none">
                  <path d="M8 2.5v7.5M5 7.25L8 10.25L11 7.25M2.75 12.75h10.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>下载 .md 学习文档</span>
              </a>
            </div>
            <div class="${tocHtml ? "guide-layout" : ""}">
              ${tocHtml}
              <article class="guide-article md-body">${rendered.html}</article>
            </div>
          </section>
        `;
      } else {
        deliverableHtml = `<section class="deliverable-card glass-surface"><div class="model-meta">正在加载编译后的学习文档…</div></section>`;
      }
    } else {
      if (state.diffCache) {
        const lines = (state.diffCache.diff || "(无文件变更)").split("\n").map((line) => {
          const cls = line.startsWith("+") && !line.startsWith("+++")
            ? "diff-add"
            : line.startsWith("-") && !line.startsWith("---")
              ? "diff-del"
              : line.startsWith("@@") || line.startsWith("diff ")
                ? "diff-hunk"
                : "diff-ctx";
          return `<span class="${cls}">${esc(line)}</span>`;
        }).join("");
        deliverableHtml = `
          <section class="deliverable-card glass-surface">
            <div class="deliverable-head">
              <div>
                <h3>隔离工作区最终变更（Git Diff）</h3>
                <div class="model-meta">${esc(s.final_summary || "")} · ${(state.diffCache.changed_paths || []).length} 个文件</div>
              </div>
              <div class="wt-paths">
                ${(state.diffCache.changed_paths || []).map((p) => `<span class="lock-chip">${esc(p)}</span>`).join("")}
              </div>
            </div>
            <div class="diff-box">${lines}</div>
          </section>
        `;
      } else {
        deliverableHtml = `<section class="deliverable-card glass-surface"><div class="model-meta">正在加载隔离工作区 Diff…</div></section>`;
      }
    }
  }

  return `
    ${deliverableHtml}
    <div class="dual-grid">
      ${renderRevCard("gemini")}
      ${renderRevCard("codex")}
    </div>
    ${renderExecutionStage(s)}
  `;
}

function renderStageContent() {
  const host = $("stage-content");
  const s = state.session;
  if (!host || !s) return;

  if (s.stage === "proposing") {
    host.innerHTML = `
      <div class="dual-grid">
        ${renderProposalCard("gemini", s.proposals?.gemini, s)}
        ${renderProposalCard("codex", s.proposals?.codex, s)}
      </div>
    `;
    return;
  }

  if (s.stage === "critiquing" || s.stage === "deciding" || s.stage === "merging") {
    host.innerHTML = `
      ${renderDecisionDock(s)}
      <div class="dual-grid">
        ${renderRefinedCard("gemini", s.refined?.gemini, s)}
        ${renderRefinedCard("codex", s.refined?.codex, s)}
      </div>
    `;
    bindDecisionHandlers();
    return;
  }

  if (s.stage === "executing") {
    host.innerHTML = renderExecutionStage(s);
    return;
  }

  if (s.stage === "reviewing" || s.stage === "completed") {
    host.innerHTML = renderReviewAndDelivery(s);
    return;
  }

  host.innerHTML = `<div class="glass-surface" style="padding:20px;color:var(--text-muted)">会话当前处于状态：${esc(s.stage)}</div>`;
}

function bindDecisionHandlers() {
  document.querySelectorAll(".decision-option").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.decisionChoice = btn.dataset.choice;
      renderStageContent();
    });
  });

  $("fusion-base-select")?.addEventListener("change", (e) => {
    state.fusionBase = e.target.value;
  });

  $("btn-confirm-decision")?.addEventListener("click", async () => {
    if (!state.currentId) return;
    const notes = $("fusion-notes-input")?.value?.trim() || "";
    if (state.decisionChoice === "merge" && !notes) {
      toast("请填写融合微调指令，说明你想结合双方哪些亮点或如何调整分工", "error");
      return;
    }
    try {
      const view = await api(`/api/sessions/${state.currentId}/decide`, {
        method: "POST",
        body: {
          choice: state.decisionChoice,
          base_model: state.decisionChoice === "merge" ? state.fusionBase : state.decisionChoice,
          fusion_notes: notes,
        },
      });
      state.session = view;
      state.events = view.events || state.events;
      pulseAtmosphere(1.2);
      renderWorkspace();
      renderKernelDock();
      toast(view.stage === "merging" ? "已进入融合微调阶段，等待 Gemini 生成最终锁定分工" : "方案已锁定，双模型互斥协同启动");
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

/* ==================== FLOATING KERNEL TELEMETRY DOCK ==================== */
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
    ticker.textContent = s ? "实时遥测监听中 · 等待首个后台内核事件" : "打开或新建协作会话，实时透视 Codex 进程流、状态机跃迁与文件写锁审计";
  }

  const m = s?.metrics || {};
  statTok.textContent = `${fmtTokens(m.total_tokens || 0)} tok`;
  statLocks.textContent = `LOCK ${m.lock_allows || 0}✓ ${m.lock_denies || 0}✕`;
  statCount.textContent = `${events.length} 事件`;

  const filtered = state.kernelFilter === "all"
    ? events
    : events.filter((ev) => ev.category === state.kernelFilter);

  if (!filtered.length) {
    stream.innerHTML = `<div style="padding:18px;text-align:center;color:var(--text-faint);font-size:12.5px">当前筛选下暂无后台事件。</div>`;
    return;
  }

  // Render newest first so live execution jumps cleanly at the top
  const ordered = [...filtered].reverse();
  stream.innerHTML = ordered.map((ev) => {
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
  if (state.sse) {
    state.sse.close();
  }
  const es = new EventSource("/api/events");
  state.sse = es;

  es.addEventListener("trace_event", (msg) => {
    try {
      const parsed = JSON.parse(msg.data);
      const payload = parsed.data || {};
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
        if (ev.event_type === "writelock.audit" && state.session.metrics) {
          if (ev.detail?.decision === "ALLOW") state.session.metrics.lock_allows = (state.session.metrics.lock_allows || 0) + 1;
          if (ev.detail?.decision === "DENY") state.session.metrics.lock_denies = (state.session.metrics.lock_denies || 0) + 1;
        }
        if (state.session.metrics) {
          state.session.metrics.event_count = state.events.length;
        }
        renderTopology(state.session);
      }
      pulseAtmosphere(0.95);
      renderKernelDock();
    } catch {
      // ignore malformed
    }
  });

  es.addEventListener("sessions_changed", async (msg) => {
    try {
      const parsed = JSON.parse(msg.data);
      const ids = parsed.data?.ids || [];
      await loadSessions();
      await loadBackends();
      if (state.currentId && (!ids.length || ids.includes(state.currentId))) {
        await openSession(state.currentId);
      }
    } catch {
      // ignore transient reload
    }
  });

  es.onerror = () => {
    // EventSource auto-reconnects; keep quiet unless persistent
  };
}

/* ==================== MODAL & SERVICE LIFECYCLE ==================== */
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
    btn.addEventListener("click", async () => {
      closeModal();
      if (act.onClick) await act.onClick();
    });
    footer.appendChild(btn);
  }
  $("modal-backdrop").classList.remove("hidden");
}

function closeModal() {
  $("modal-backdrop").classList.add("hidden");
}

/* ==================== BOOTSTRAP & EVENT WIRING ==================== */
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
  $("btn-home")?.addEventListener("click", showComposer);
  $("btn-new-session")?.addEventListener("click", showComposer);
  $("modal-close")?.addEventListener("click", closeModal);
  $("modal-backdrop")?.addEventListener("click", (e) => {
    if (e.target === $("modal-backdrop")) closeModal();
  });

  $("session-select")?.addEventListener("change", (e) => {
    openSession(e.target.value || null, { resetCache: true });
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

  $("btn-toggle-project")?.addEventListener("click", () => {
    $("project-row")?.classList.toggle("hidden");
  });

  $("btn-validate-project")?.addEventListener("click", async () => {
    const path = $("project-input").value.trim();
    const hint = $("project-hint");
    if (!path) {
      hint.textContent = "请输入本地 Git 仓库绝对路径。";
      return;
    }
    try {
      const info = await api("/api/validate-project", { method: "POST", body: { path } });
      hint.textContent = `校验通过 · 分支 ${info.branch} · HEAD ${info.head.slice(0, 8)}${info.dirty ? "（工作区有未提交变更，仅复制已提交 HEAD）" : ""}`;
    } catch (err) {
      hint.textContent = err.message;
    }
  });

  // Quick inspiration prompts
  document.querySelectorAll(".prompt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const targetMode = chip.dataset.mode || "learn";
      state.mode = targetMode;
      document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === targetMode));
      $("btn-toggle-project")?.classList.toggle("hidden", targetMode === "learn");
      if (targetMode === "learn") $("project-row")?.classList.add("hidden");
      const ta = $("task-input");
      ta.value = chip.dataset.prompt || "";
      ta.focus();
    });
  });

  // Recent sessions list delegation
  $("recent-list")?.addEventListener("click", (e) => {
    const item = e.target.closest?.("[data-open-session]");
    if (item) openSession(item.dataset.openSession, { resetCache: true });
  });

  // Create session
  const launchSession = async () => {
    const task = $("task-input").value.trim();
    if (!task) {
      toast("请先描述你的开发任务或学习目标", "error");
      $("task-input").focus();
      return;
    }
    const project = state.mode === "dev" ? ($("project-input").value.trim() || null) : null;
    const btn = $("btn-create-session");
    btn.disabled = true;
    try {
      const view = await api("/api/sessions", {
        method: "POST",
        body: { task, mode: state.mode, project, auto_start_codex: true },
      });
      $("task-input").value = "";
      pulseAtmosphere(1.6);
      await loadSessions();
      await openSession(view.id, { resetCache: true });
      toast("双模型协作已启动：Codex 正在后台隔离沙箱推演");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  };

  $("btn-create-session")?.addEventListener("click", launchSession);
  $("task-input")?.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      launchSession();
    }
  });

  // Trigger Codex
  $("btn-trigger-codex")?.addEventListener("click", async () => {
    if (!state.currentId) return;
    try {
      state.session = await api(`/api/sessions/${state.currentId}/run-codex`, { method: "POST" });
      pulseAtmosphere(1.2);
      renderWorkspace();
      renderKernelDock();
      toast("已调度 Codex 后台沙箱执行");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // Cancel session
  $("btn-cancel-session")?.addEventListener("click", () => {
    if (!state.currentId) return;
    showModal({
      title: "取消当前协作会话？",
      bodyHtml: "<p>取消后会话将归档为已取消状态，已有隔离目录产物保留但不再推进后续阶段。</p>",
      actions: [
        { label: "保留继续", cls: "btn-secondary" },
        {
          label: "确认取消",
          cls: "btn-primary",
          onClick: async () => {
            try {
              state.session = await api(`/api/sessions/${state.currentId}/cancel`, { method: "POST" });
              state.events = state.session.events || state.events;
              await loadSessions();
              renderWorkspace();
              renderKernelDock();
              toast("会话已取消");
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
      ],
    });
  });

  // Stop local service
  $("btn-stop-service")?.addEventListener("click", async () => {
    let activeCount = 0;
    try {
      const h = await api("/api/health");
      activeCount = h.active_runs || 0;
    } catch {
      // ignore
    }
    showModal({
      title: "停止 AIsys 本地服务？",
      bodyHtml: activeCount > 0
        ? `<p>当前仍有 <strong>${activeCount}</strong> 个后台模型任务正在执行。确认停止整个本地 Web 服务吗？</p>`
        : "<p>将优雅关闭本地 AIsys 服务（端口释放，所有会话与事件记录已安全持久化）。下次双击 AIsys.command 即可秒开。</p>",
      actions: [
        { label: "取消", cls: "btn-secondary" },
        {
          label: "停止服务",
          cls: "btn-primary",
          onClick: async () => {
            try {
              await api("/api/server/shutdown", { method: "POST", body: { confirm_active: true } });
              toast("AIsys 本地服务正在关闭…");
            } catch (err) {
              toast(err.message, "error");
            }
          },
        },
      ],
    });
  });

  // Kernel Telemetry Dock toggle & filters & row expand
  const toggleKernel = () => {
    state.kernelOpen = !state.kernelOpen;
    renderKernelDock();
  };
  $("kernel-dock-toggle")?.addEventListener("click", toggleKernel);
  $("kernel-dock-toggle")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleKernel();
    }
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

  // Smooth 1s elapsed timer tick while Codex subprocess is genuinely running
  setInterval(() => {
    const proc = state.session?.active_process;
    if (!proc || !proc.started_at) return;
    proc.elapsed_s = Math.max(0, Date.now() / 1000 - proc.started_at);
    const liveEl = $("topo-live-note");
    if (liveEl) liveEl.textContent = `LIVE · ${proc.step} · ${proc.elapsed_s.toFixed(1)}s`;
    const subEl = $("topo-codex-sub");
    if (subEl) subEl.textContent = `PID ${proc.pid || "…"} · ${proc.elapsed_s.toFixed(1)}s · ${(proc.latest_event || "").slice(0, 24)}`;
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
  await Promise.all([loadBackends(), loadSessions()]);
  renderKernelDock();
}

boot();
