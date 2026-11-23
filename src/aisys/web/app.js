const CSRF_TOKEN =
  document.querySelector('meta[name="aisys-csrf"]')?.getAttribute("content") || "";

const STAGE_STEP = {
  proposing: 1,
  critiquing: 2,
  deciding: 3,
  merging: 3,
  executing: 4,
  reviewing: 5,
  completed: 6,
  failed: 0,
  cancelled: 0,
};

const STAGE_LABELS = {
  proposing: "① 独立提案中",
  critiquing: "② 交叉互审与分工",
  deciding: "③ 等待你拍板",
  merging: "③ 融合微调中",
  executing: "④ 互斥协同执行",
  reviewing: "⑤ 互相审查监督",
  completed: "✓ 已完成交付",
  failed: "执行失败",
  cancelled: "已取消",
};

const state = {
  mode: "dev",
  sessions: [],
  activeId: null,
  session: null,
  sseConnected: false,
  eventSource: null,
};

function esc(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(msg, kind = "info") {
  const box = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function api(method, path, body = undefined) {
  const headers = {};
  if (method !== "GET" && method !== "HEAD") {
    headers["X-AIsys-Token"] = CSRF_TOKEN;
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    throw new Error(typeof data === "object" && data?.detail ? data.detail : String(data || resp.statusText));
  }
  return data;
}

function openModal({ title, bodyHtml, footerHtml, onMount }) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  document.getElementById("modal-footer").innerHTML = footerHtml || "";
  document.getElementById("modal-backdrop").classList.remove("hidden");
  if (onMount) onMount(document.getElementById("modal-backdrop"));
}

function closeModal() {
  document.getElementById("modal-backdrop").classList.add("hidden");
}

/* ---------- Theme & SSE ---------- */
function initTheme() {
  document.documentElement.dataset.theme = localStorage.getItem("aisys-theme") || "dark";
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("aisys-theme", next);
}

function connectSSE() {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource("/api/events");
  state.eventSource = es;
  es.addEventListener("connected", () => {
    state.sseConnected = true;
    loadBackends();
  });
  es.addEventListener("sessions_changed", async () => {
    await loadSessions(false);
    if (state.activeId) await loadSession(state.activeId, false);
    loadBackends();
  });
  es.addEventListener("server_shutdown", () => {
    state.sseConnected = false;
    toast("AIsys 本地服务已停止", "warning");
    loadBackends();
  });
  es.onerror = () => {
    state.sseConnected = false;
  };
}

/* ---------- Backends & Session List ---------- */
async function loadBackends() {
  try {
    const b = await api("GET", "/api/backends");
    const cLabel = b.codex.running
      ? "Codex · 运行中"
      : b.codex.callable
      ? "Codex · 可调用"
      : b.codex.installed
      ? "Codex · 未登录"
      : "Codex · 不可用";
    const cCls = b.codex.running ? "pill-running" : b.codex.callable ? "pill-codex" : "pill-danger";
    const gLabel = b.google.running ? "Gemini · 等待协同" : "Gemini · Antigravity 主控 ($0)";
    document.getElementById("backend-pills").innerHTML = `
      <span class="pill ${cCls}" title="${esc(b.codex.detail)}">${cLabel}</span>
      <span class="pill pill-gemini" title="${esc(b.google.detail)}">${gLabel}</span>
      <span class="pill ${state.sseConnected ? "pill-ok" : "pill-muted"}">${state.sseConnected ? "实时同步" : "重连中"}</span>
    `;
  } catch {
    // ignore during shutdown
  }
}

async function loadSessions(autoOpen = true) {
  try {
    const res = await api("GET", "/api/sessions");
    state.sessions = res.sessions || [];
    const sel = document.getElementById("session-select");
    const opts = ['<option value="">+ 新建协作会话</option>'].concat(
      state.sessions.map(
        (s) =>
          `<option value="${esc(s.id)}" ${s.id === state.activeId ? "selected" : ""}>[${s.mode === "learn" ? "学习" : "开发"}] ${esc(s.task.slice(0, 36))} · ${esc(STAGE_LABELS[s.stage] || s.stage)}</option>`
      )
    );
    sel.innerHTML = opts.join("");
    if (autoOpen && !state.activeId && state.sessions.length) {
      await openSession(state.sessions[0].id);
    }
  } catch (err) {
    toast(`读取会话列表失败：${err.message}`, "error");
  }
}

function showComposer() {
  state.activeId = null;
  state.session = null;
  document.getElementById("session-select").value = "";
  document.getElementById("composer-section").classList.remove("hidden");
  document.getElementById("session-section").classList.add("hidden");
}

async function openSession(id) {
  state.activeId = id;
  document.getElementById("session-select").value = id;
  document.getElementById("composer-section").classList.add("hidden");
  document.getElementById("session-section").classList.remove("hidden");
  await loadSession(id, true);
}

async function loadSession(id, showError = true) {
  try {
    state.session = await api("GET", `/api/sessions/${id}`);
    renderSession();
  } catch (err) {
    if (showError) toast(`加载会话失败：${err.message}`, "error");
  }
}

/* ---------- Render Session ---------- */
function renderDivisionTable(subtasks, check) {
  if (!subtasks || !subtasks.length) {
    return '<div class="muted small">尚未生成分工表</div>';
  }
  const badge = check?.valid
    ? `<div class="banner banner-ok">✓ 系统硬校验通过：所有子任务文件/章节完全互斥无重叠（共 ${check.waves?.length || 1} 个波次）</div>`
    : `<div class="banner banner-error">✕ 检测到分工冲突：${esc((check?.conflicts || []).join("；"))}</div>`;
  const warns = (check?.warnings || []).length
    ? `<div class="banner banner-warning" style="margin-top:6px;">⚠ ${esc(check.warnings.join("；"))}</div>`
    : "";
  const rows = subtasks
    .map(
      (t) => `
      <tr>
        <td class="mono"><strong>${esc(t.id)}</strong></td>
        <td><span class="pill pill-${esc(t.owner)}">${esc(t.owner)}</span></td>
        <td><strong>${esc(t.title)}</strong><div class="muted small">${esc(t.objective)}</div></td>
        <td class="mono small">${(t.owned_paths || []).map((p) => `<code>🔒 ${esc(p)}</code>`).join("<br/>")}</td>
        <td class="mono small">${t.depends_on?.length ? esc(t.depends_on.join(", ")) : "—"}</td>
      </tr>`
    )
    .join("");
  return `
    ${badge}${warns}
    <table class="div-table" style="margin-top:8px;">
      <thead><tr><th>编号</th><th>负责模型</th><th>子任务内容</th><th>独占路径/章节</th><th>依赖</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderModelPlanColumn(model, s) {
  const title = model === "gemini" ? "Gemini (Antigravity)" : "Codex (ChatGPT CLI)";
  const p1 = s.proposals?.[model];
  const p2 = s.refined?.[model];
  const busy = model === "codex" && s.codex_busy;

  if (!p1 && !p2) {
    return `
      <div class="card model-card ${model}">
        <div class="model-head">
          <span class="model-name">${title}</span>
          <span class="pill ${busy ? "pill-running" : "pill-muted"}">${busy ? "思考生成中…" : "等待提交"}</span>
        </div>
        <div class="muted">${model === "codex" ? "Codex 正在独立分析并提出初版方案…" : "等待 Antigravity / Gemini 提交独立初版方案…"}</div>
      </div>
    `;
  }

  let body = "";
  if (p2) {
    body += `
      <div>
        <div class="section-label">对对方初案的审查与吸收</div>
        <div class="prose-box"><strong>指出不足：</strong>${esc(p2.critique_of_peer)}\n\n<strong>吸收优点：</strong>${esc(p2.adopted_from_peer)}</div>
      </div>
      <div>
        <div class="section-label">升级版完整方案</div>
        <div class="prose-box"><strong>${esc(p2.summary)}</strong>\n\n${esc(p2.approach)}</div>
      </div>
      <div>
        <div class="section-label">拟议最优分工表（防重叠校验）</div>
        ${renderDivisionTable(p2.subtasks, p2.division_check)}
      </div>
    `;
  }
  if (p1) {
    const wrapped = p2
      ? `<details><summary style="cursor:pointer;font-size:12.5px;color:var(--text-muted);">展开查看阶段 1 独立初案</summary><div class="prose-box" style="margin-top:8px;"><strong>${esc(p1.summary)}</strong>\n\n${esc(p1.approach)}</div></details>`
      : `
        <div>
          <div class="section-label">阶段 1 · 独立初版方案</div>
          <div class="prose-box"><strong>${esc(p1.summary)}</strong>\n\n${esc(p1.approach)}</div>
        </div>
        ${p1.strengths?.length ? `<div class="small"><strong>亮点：</strong>${p1.strengths.map((x) => esc(x)).join(" · ")}</div>` : ""}
        ${p1.risks?.length ? `<div class="small muted"><strong>风险点：</strong>${p1.risks.map((x) => esc(x)).join(" · ")}</div>` : ""}
      `;
    body += wrapped;
  }

  const badgeText = p2 ? "升级方案就绪" : busy ? "互审升级中…" : "初案就绪";
  const badgeCls = p2 ? `pill-${model}` : busy ? "pill-running" : "pill-info";
  return `
    <div class="card model-card ${model}">
      <div class="model-head">
        <span class="model-name">${title}</span>
        <span class="pill ${badgeCls}">${badgeText}</span>
      </div>
      ${body}
    </div>
  `;
}

function renderGeminiActionBar(s) {
  const bar = document.getElementById("gemini-action-bar");
  const pending = s.next_action?.pending_gemini || [];
  if (!pending.length || ["completed", "cancelled", "failed", "deciding"].includes(s.stage)) {
    bar.classList.add("hidden");
    return;
  }
  const first = pending[0];
  const actionLabels = {
    propose: "阶段 1：Gemini 独立初案",
    critique: "阶段 2：Gemini 交叉互审与分工",
    merge: "阶段 3：根据你的融合意见生成最终锁定分工",
    execute_subtask: `阶段 4：执行 Gemini 独占子任务 ${first.task_id || ""}`,
    review_peer: "阶段 5：Gemini 独立审查 Codex 产出",
  };
  const chatCmd = `使用 aisys-driver 继续 AIsys 协作会话 ${s.id}`;

  bar.classList.remove("hidden");
  bar.innerHTML = `
    <div class="inline-row" style="justify-content:space-between;">
      <strong>✦ 当前待 Gemini (Antigravity) 执行：${esc(actionLabels[first.action] || first.action)}</strong>
      <span class="pill pill-gemini">路线 1 · 零额外费用</span>
    </div>
    <div class="cmd-box">
      <span class="muted">在 Antigravity 对话框发送：</span>
      <code style="flex:1;">${esc(chatCmd)}</code>
      <button class="btn btn-gemini btn-sm" id="btn-copy-agy-cmd">复制指令</button>
    </div>
    <details>
      <summary style="cursor:pointer;font-size:12px;color:var(--text-muted);">可选兜底：查看提示词或手动粘贴 Gemini JSON 提交</summary>
      <div style="margin-top:8px;display:flex;flex-direction:column;gap:8px;">
        <pre class="pre-box">${esc(first.prompt || "")}</pre>
        <textarea id="manual-gemini-json" class="input mono" rows="5" placeholder='粘贴符合 schema 的 JSON 对象后点击提交…'></textarea>
        <div><button class="btn btn-secondary btn-sm" id="btn-manual-gemini-submit">提交 JSON</button></div>
      </div>
    </details>
  `;

  bar.querySelector("#btn-copy-agy-cmd").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(chatCmd);
      toast("已复制 Antigravity 接力指令", "ok");
    } catch {
      toast(chatCmd, "info");
    }
  });

  bar.querySelector("#btn-manual-gemini-submit").addEventListener("click", async () => {
    const raw = bar.querySelector("#manual-gemini-json").value.trim();
    if (!raw) return;
    try {
      const payload = JSON.parse(raw);
      if (first.action === "execute_subtask" && !payload.task_id) payload.task_id = first.task_id;
      await api("POST", `/api/sessions/${s.id}/gemini`, { action: first.action, payload });
      toast("已提交 Gemini 步骤结果", "ok");
      await loadSession(s.id);
    } catch (err) {
      toast(`提交失败：${err.message}`, "error");
    }
  });
}

function renderSession() {
  const s = state.session;
  if (!s) return;

  document.getElementById("s-mode-badge").textContent = s.mode === "learn" ? "📚 学习与研究" : "💻 开发任务";
  document.getElementById("s-mode-badge").className = `pill ${s.mode === "learn" ? "pill-gemini" : "pill-info"}`;
  document.getElementById("s-stage-badge").textContent = STAGE_LABELS[s.stage] || s.stage;
  document.getElementById("s-stage-badge").className = `pill pill-${s.stage === "completed" ? "ok" : s.stage === "failed" || s.stage === "cancelled" ? "danger" : "running"}`;
  document.getElementById("s-id-label").textContent = `会话 ${s.id.slice(0, 8)}`;
  document.getElementById("s-task-title").textContent = s.task;

  const curStep = STAGE_STEP[s.stage] || 0;
  document.querySelectorAll("#stepper li").forEach((li) => {
    const num = Number(li.dataset.step);
    li.classList.toggle("done", curStep > num);
    li.classList.toggle("active", curStep === num);
  });

  const errBanner = document.getElementById("s-error-banner");
  if (s.error) {
    errBanner.textContent = `⚠ ${s.error}`;
    errBanner.classList.remove("hidden");
  } else {
    errBanner.classList.add("hidden");
  }

  renderGeminiActionBar(s);

  const stageEl = document.getElementById("stage-content");
  if (["proposing", "critiquing", "deciding", "merging"].includes(s.stage)) {
    const gValid = s.refined?.gemini?.division_check?.valid;
    const cValid = s.refined?.codex?.division_check?.valid;
    const decisionCard =
      s.stage === "deciding"
        ? `
        <div class="card decision-card">
          <div>
            <h3 style="margin:0 0 4px;">③ 由你拍板最终方案</h3>
            <div class="muted small">已确认无重叠的分工表将立即锁定执行权限；如需结合双方优点或微调任务归属，请在下方填写一句融合意见。</div>
          </div>
          <div class="decision-actions">
            <button class="btn btn-gemini btn-lg" id="btn-choose-gemini">✦ 采用 Gemini 方案${gValid ? "" : "（先自动消解分工冲突）"}</button>
            <button class="btn btn-codex btn-lg" id="btn-choose-codex">✦ 采用 Codex 方案${cValid ? "" : "（先自动消解分工冲突）"}</button>
          </div>
          <div class="fusion-box">
            <div class="inline-row">
              <strong>融合微调（可选）：</strong>
              <select id="fusion-base" class="input input-sm">
                <option value="gemini">以 Gemini 方案为主干</option>
                <option value="codex">以 Codex 方案为主干</option>
              </select>
            </div>
            <textarea id="fusion-notes" class="input" rows="2" placeholder="例如：采用 Gemini 的整体架构，但吸收 Codex 的边界校验设计，并把 T2 交给 Codex 负责…"></textarea>
            <div>
              <button class="btn btn-primary" id="btn-choose-merge">✦ 提交融合微调并锁定分工</button>
            </div>
          </div>
        </div>`
        : s.stage === "merging"
        ? `<div class="banner banner-warning">正在按你的融合微调意见生成最终无重叠分工表：<em>${esc(s.decision?.fusion_notes || "")}</em></div>`
        : "";

    stageEl.innerHTML = `
      <div class="grid-2">
        ${renderModelPlanColumn("gemini", s)}
        ${renderModelPlanColumn("codex", s)}
      </div>
      ${decisionCard}
    `;

    stageEl.querySelector("#btn-choose-gemini")?.addEventListener("click", () => submitDecision("gemini", "gemini", ""));
    stageEl.querySelector("#btn-choose-codex")?.addEventListener("click", () => submitDecision("codex", "codex", ""));
    stageEl.querySelector("#btn-choose-merge")?.addEventListener("click", () => {
      const notes = stageEl.querySelector("#fusion-notes").value.trim();
      const base = stageEl.querySelector("#fusion-base").value;
      if (!notes) {
        toast("请填写融合微调意见，或直接点击上方采用某一方方案", "warning");
        return;
      }
      submitDecision("merge", base, notes);
    });
    return;
  }

  // Stage 4, Stage 5 & Completed View
  const plan = s.locked_plan;
  const tasksHtml = (plan?.subtasks || [])
    .map((st) => {
      const ex = s.executions?.[st.id] || { status: "pending" };
      const stLabels = {
        pending: "等待执行",
        running: "正在执行…",
        completed: "已完成",
        needs_fix: "待返工修复",
        failed: "执行失败",
      };
      return `
        <div class="task-item owner-${esc(st.owner)}">
          <div class="inline-row" style="justify-content:space-between;">
            <div class="inline-row">
              <strong class="mono">${esc(st.id)}</strong>
              <strong>${esc(st.title)}</strong>
              <span class="pill pill-${esc(st.owner)}">${esc(st.owner)}</span>
            </div>
            <span class="pill pill-${esc(ex.status)}">${esc(stLabels[ex.status] || ex.status)}</span>
          </div>
          <div class="small muted">${esc(st.objective)}</div>
          <div class="inline-row small mono">
            <span>独占锁：${st.owned_paths.map((p) => `<code>🔒 ${esc(p)}</code>`).join(" ")}</span>
            ${st.depends_on?.length ? `<span>· 依赖：${esc(st.depends_on.join(", "))}</span>` : ""}
          </div>
          ${ex.summary ? `<div class="prose-box small">${esc(ex.summary)}</div>` : ""}
          ${ex.review_feedback ? `<div class="banner banner-warning"><strong>交叉审查修复意见：</strong>${esc(ex.review_feedback)}</div>` : ""}
          ${ex.error ? `<div class="banner banner-error">${esc(ex.error)}</div>` : ""}
        </div>`;
    })
    .join("");

  const renderReviewCard = (reviewer, target) => {
    const rev = s.reviews?.[reviewer];
    const title = reviewer === "gemini" ? "Gemini 审查 Codex 产出" : "Codex 审查 Gemini 产出";
    if (!rev) {
      return `
        <div class="card model-card ${reviewer}">
          <div class="model-head">
            <span class="model-name">${title}</span>
            <span class="pill pill-muted">${s.stage === "reviewing" ? "等待审查…" : "待执行完成后开始"}</span>
          </div>
        </div>`;
    }
    return `
      <div class="card model-card ${reviewer}">
        <div class="model-head">
          <span class="model-name">${title}（第 ${rev.round} 轮）</span>
          <span class="pill pill-${esc(rev.verdict)}">${rev.verdict === "pass" ? "✓ 通过" : "✕ 要求返工"}</span>
        </div>
        <div class="prose-box">${esc(rev.summary)}</div>
        ${rev.findings?.length ? `<ul class="small" style="margin:0;padding-left:18px;">${rev.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      </div>`;
  };

  stageEl.innerHTML = `
    <div class="card">
      <div class="inline-row" style="justify-content:space-between;margin-bottom:10px;">
        <div>
          <h3 style="margin:0;">④ 互斥分工协同执行（方案来源：${esc(plan?.source || "—")}）</h3>
          <div class="muted small">${esc(plan?.summary || "")}</div>
        </div>
        <span class="pill pill-ok">✓ 写入路径硬锁生效</span>
      </div>
      <div class="task-list">${tasksHtml}</div>
    </div>

    <div class="grid-2">
      ${renderReviewCard("gemini", "codex")}
      ${renderReviewCard("codex", "gemini")}
    </div>

    <div class="card" id="deliverable-card">
      <div class="inline-row" style="justify-content:space-between;margin-bottom:10px;">
        <h3 style="margin:0;">${s.mode === "learn" ? "📚 结构化 Markdown 学习文档" : "💻 隔离工作区代码变更 Diff"}</h3>
        <div class="inline-row" id="deliverable-actions"></div>
      </div>
      <div id="deliverable-body" class="muted">正在加载成果预览…</div>
    </div>
  `;

  loadDeliverable(s);
}

async function submitDecision(choice, baseModel, fusionNotes) {
  try {
    await api("POST", `/api/sessions/${state.activeId}/decide`, {
      choice,
      base_model: baseModel,
      fusion_notes: fusionNotes,
    });
    toast(choice === "merge" ? "已提交融合微调指令" : `已拍板采用 ${choice} 方案，进入协同执行`, "ok");
    await loadSession(state.activeId);
  } catch (err) {
    toast(`拍板失败：${err.message}`, "error");
  }
}

async function loadDeliverable(s) {
  const body = document.getElementById("deliverable-body");
  const actions = document.getElementById("deliverable-actions");
  if (!body || !actions) return;

  if (s.mode === "learn") {
    try {
      const guide = await api("GET", `/api/sessions/${s.id}/study-guide`);
      actions.innerHTML = `
        <span class="mono small muted">${esc(guide.path)}</span>
        <button class="btn btn-secondary btn-sm" id="btn-copy-md">复制 Markdown</button>
        <a class="btn btn-primary btn-sm" style="text-decoration:none;" href="/api/sessions/${encodeURIComponent(s.id)}/study-guide?download=1">⬇ 下载 .md</a>
      `;
      body.innerHTML = `<div class="study-reader">${esc(guide.markdown)}</div>`;
      actions.querySelector("#btn-copy-md").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(guide.markdown);
          toast("已复制完整 Markdown 学习文档", "ok");
        } catch {
          toast("复制失败，请使用下载按钮", "warning");
        }
      });
    } catch (err) {
      body.innerHTML = `<div class="banner banner-error">${esc(err.message)}</div>`;
    }
    return;
  }

  try {
    const d = await api("GET", `/api/sessions/${s.id}/diff`);
    actions.innerHTML = `<span class="mono small muted">${esc(d.workspace)}</span>`;
    const diffHtml = (d.diff || "(尚未产生代码文件变更)")
      .split("\n")
      .map((line) => {
        const cls = line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : line.startsWith("@@") ? "diff-hunk" : "";
        return `<span class="${cls}">${esc(line)}</span>`;
      })
      .join("\n");
    body.innerHTML = `
      <div class="small muted" style="margin-bottom:8px;">变更文件：${d.changed_paths.length ? d.changed_paths.map((p) => `<code>${esc(p)}</code>`).join(", ") : "(无)"}</div>
      <pre class="pre-box mono">${diffHtml}</pre>
    `;
  } catch (err) {
    body.innerHTML = `<div class="banner banner-error">${esc(err.message)}</div>`;
  }
}

/* ---------- Stop Service ---------- */
async function openStopServiceModal() {
  let health;
  try {
    health = await api("GET", "/api/health");
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  const active = health.active_runs || 0;
  openModal({
    title: "停止 AIsys 本地服务",
    bodyHtml: `
      <p style="margin:0;">将停止本地 Web 服务（PID <code>${esc(health.pid)}</code>，端口 <code>${esc(health.port)}</code>）。所有已完成与进行中的会话数据均保留在本地 <code>data/sessions/</code>，下次双击 <code>AIsys.command</code> 即可恢复。</p>
      ${active > 0 ? `<div class="banner banner-warning">当前仍有 ${active} 个正在运行的 Codex 步骤，确认要停止服务吗？</div>` : '<div class="banner banner-ok">当前无活跃运行中的模型步骤，可安全停止。</div>'}
    `,
    footerHtml: `<button class="btn btn-secondary" id="st-cancel">暂不停止</button><button class="btn btn-danger" id="st-ok">⏻ 确认停止</button>`,
    onMount: (dlg) => {
      dlg.querySelector("#st-cancel").addEventListener("click", closeModal);
      dlg.querySelector("#st-ok").addEventListener("click", async () => {
        try {
          await api("POST", "/api/server/shutdown", { confirm_active: true });
          closeModal();
          toast("AIsys 服务已停止", "warning");
        } catch (err) {
          toast(err.message, "error");
        }
      });
    },
  });
}

/* ---------- Wiring ---------- */
function bindEvents() {
  document.getElementById("btn-theme").addEventListener("click", toggleTheme);
  document.getElementById("btn-stop-service").addEventListener("click", openStopServiceModal);
  document.getElementById("btn-new-session").addEventListener("click", showComposer);
  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
  });

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.mode = btn.dataset.mode;
      document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.getElementById("project-row").classList.toggle("hidden", state.mode === "learn");
    });
  });

  document.getElementById("session-select").addEventListener("change", (e) => {
    if (!e.target.value) showComposer();
    else openSession(e.target.value);
  });

  document.getElementById("btn-validate-project").addEventListener("click", async () => {
    const val = document.getElementById("project-input").value.trim();
    const hint = document.getElementById("project-hint");
    if (!val) {
      hint.textContent = "已留空：使用空白隔离工作区。";
      return;
    }
    try {
      const res = await api("POST", "/api/validate-project", { path: val });
      hint.textContent = `✓ ${res.branch} @ ${res.head.slice(0, 8)}`;
      hint.style.color = "var(--codex)";
    } catch (err) {
      hint.textContent = `✕ ${err.message}`;
      hint.style.color = "var(--danger)";
    }
  });

  document.getElementById("btn-create-session").addEventListener("click", async () => {
    const task = document.getElementById("task-input").value.trim();
    if (!task) {
      toast("请先输入任务或学习目标", "warning");
      return;
    }
    const btn = document.getElementById("btn-create-session");
    btn.disabled = true;
    try {
      const created = await api("POST", "/api/sessions", {
        task,
        mode: state.mode,
        project: state.mode === "dev" ? document.getElementById("project-input").value.trim() || null : null,
      });
      document.getElementById("task-input").value = "";
      toast("已启动双模型分析：Codex 正在独立生成初案", "ok");
      await loadSessions(false);
      await openSession(created.id);
    } catch (err) {
      toast(`启动失败：${err.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-trigger-codex").addEventListener("click", async () => {
    if (!state.activeId) return;
    try {
      await api("POST", `/api/sessions/${state.activeId}/run-codex`);
      toast("已触发 Codex 执行就绪步骤", "info");
      await loadSession(state.activeId);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  document.getElementById("btn-cancel-session").addEventListener("click", async () => {
    if (!state.activeId || !confirm("确认取消当前协作会话？")) return;
    try {
      await api("POST", `/api/sessions/${state.activeId}/cancel`);
      toast("会话已取消", "warning");
      await loadSession(state.activeId);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

initTheme();
bindEvents();
connectSSE();
loadBackends();
loadSessions(true);
