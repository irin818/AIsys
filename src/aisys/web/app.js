import { WorkflowGraphCanvas } from "/static/graph.js";

const CSRF_TOKEN =
  document.querySelector('meta[name="aisys-csrf"]')?.getAttribute("content") || "";

const STATE_LABELS = {
  callable: "可调用",
  running: "正在执行",
  authenticated: "已认证",
  installed: "已安装 / 未认证",
  restricted: "受限",
  unavailable: "不可用",
  unverified: "未验证",
  ready: "待执行",
  waiting: "等待审批 / 交接",
  paused: "已暂停",
  recoverable: "可恢复",
  stopping: "正在停止",
  complete: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

const state = {
  view: "dashboard",
  dashboard: null,
  runs: [],
  runFilter: "all",
  selectedRunId: null,
  selectedRun: null,
  runTab: "steps",
  runDiffSpace: "main",
  workflows: [],
  activeWfId: null,
  activeWfSource: null,
  activeWfRevision: null,
  activeWfData: null,
  activeWfYaml: "",
  wfMode: "visual",
  wfInspectorTab: "node",
  selectedNodeId: null,
  agents: [],
  agentTemplates: [],
  agentsRevision: null,
  selectedAgentId: null,
  logs: [],
  serverConfig: null,
  serverConfigRevision: null,
  eventSource: null,
};

let builderGraph = null;
let runGraph = null;

function esc(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function fmtDuration(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  if (sec < 60) return `${Number(sec).toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

function pillClass(status) {
  return `pill pill-${esc(status || "muted")}`;
}

function toast(message, kind = "info") {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 4200);
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
    const msg = typeof data === "object" && data?.detail ? data.detail : String(data || resp.statusText);
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

/* ---------- Modal Helpers ---------- */
function openModal({ title, bodyHtml, footerHtml, onMount }) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  document.getElementById("modal-footer").innerHTML = footerHtml || "";
  document.getElementById("modal-backdrop").classList.remove("hidden");
  if (onMount) onMount(document.getElementById("modal-dialog"));
}

function closeModal() {
  document.getElementById("modal-backdrop").classList.add("hidden");
}

/* ---------- Navigation & Theme ---------- */
function initTheme() {
  const saved = localStorage.getItem("aisys-theme") || "dark";
  document.documentElement.dataset.theme = saved;
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("aisys-theme", next);
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${view}`);
  });
  if (view === "dashboard") loadDashboard();
  if (view === "tasks") loadRuns();
  if (view === "workflows") loadWorkflows();
  if (view === "agents") loadAgents();
  if (view === "logs") loadLogs();
  if (view === "settings") loadSettings();
}

/* ---------- Real-time SSE ---------- */
function connectSSE() {
  if (state.eventSource) state.eventSource.close();
  const pill = document.getElementById("sse-status");
  const label = document.getElementById("sse-label");
  const es = new EventSource("/api/events");
  state.eventSource = es;

  es.addEventListener("connected", () => {
    pill.className = "sse-pill connected";
    label.textContent = "实时状态同步中 (SSE)";
  });

  const handleChange = () => {
    if (state.view === "dashboard") loadDashboard(false);
    if (state.view === "tasks") {
      loadRuns(false);
      if (state.selectedRunId) loadRunDetail(state.selectedRunId, false);
    }
    if (state.view === "logs") loadLogs();
    refreshNavBadge();
  };

  es.addEventListener("runs_changed", handleChange);
  es.addEventListener("run_update", handleChange);
  es.addEventListener("config_changed", () => {
    if (state.view === "workflows") loadWorkflows(false);
    if (state.view === "agents") loadAgents();
    if (state.view === "dashboard") loadDashboard(false);
  });
  es.addEventListener("server_shutdown", () => {
    pill.className = "sse-pill disconnected";
    label.textContent = "AIsys 服务正在停止…";
    toast("AIsys 本地服务已收到关闭指令", "warning");
  });
  es.onerror = () => {
    pill.className = "sse-pill disconnected";
    label.textContent = "连接中断，等待重连…";
  };
}

async function refreshNavBadge() {
  try {
    const res = await api("GET", "/api/runs?status=all");
    const count = (res.runs || []).filter((r) =>
      ["running", "stopping", "ready", "waiting"].includes(r.status)
    ).length;
    const badge = document.getElementById("nav-active-badge");
    badge.textContent = String(count);
    badge.classList.toggle("hidden", count === 0);
  } catch {
    // ignore transient poll errors
  }
}

/* ---------- Dashboard ---------- */
async function loadDashboard(forceBackend = false) {
  try {
    const data = await api("GET", `/api/dashboard?refresh=${forceBackend ? "true" : "false"}`);
    state.dashboard = data;
    renderDashboard();
  } catch (err) {
    toast(`加载总览失败：${err.message}`, "error");
  }
}

function renderBackendCard(name, info) {
  const title = name === "codex" ? "OpenAI Codex CLI" : "Google Antigravity / Gemini";
  const stLabel = STATE_LABELS[info.state] || info.state;
  const caps = [
    `<span class="pill ${info.installed ? "pill-ok" : "pill-muted"}">已安装：${info.installed ? "是" : "否"}</span>`,
    `<span class="pill ${info.authenticated ? "pill-ok" : "pill-muted"}">已认证：${info.authenticated ? "是" : "否"}</span>`,
    `<span class="pill ${info.callable ? "pill-ok" : "pill-warning"}">直接调用：${info.callable ? "可用" : "不可直接调用"}</span>`,
    info.running ? '<span class="pill pill-running">正在执行 / 等待交接</span>' : "",
  ].join("");
  return `
    <div class="card backend-card">
      <div class="backend-top">
        <div class="backend-title">${title}</div>
        <span class="${pillClass(info.state)}">${esc(stLabel)}</span>
      </div>
      <div class="cap-row">${caps}</div>
      <div class="muted-text">${esc(info.detail)}</div>
      ${info.executable ? `<div class="muted-text mono" style="font-size:11.5px;">CLI: ${esc(info.executable)}</div>` : ""}
    </div>
  `;
}

function renderDashboard() {
  const d = state.dashboard;
  if (!d) return;
  document.getElementById("sidebar-server-meta").textContent =
    `PID ${d.server.pid} · 并发上限 ${d.server.max_concurrent_runs} · 运行 ${fmtDuration(d.server.uptime_seconds)}`;

  document.getElementById("dashboard-backends").innerHTML =
    renderBackendCard("codex", d.backends.codex) + renderBackendCard("google", d.backends.google);

  const c = d.counts;
  const statItems = [
    ["正在执行", c.running, "var(--accent)"],
    ["待执行队列", c.queued, "var(--warning)"],
    ["待审批 / 交接", c.waiting, "var(--purple)"],
    ["已完成", c.complete, "var(--success)"],
    ["执行失败", c.failed, "var(--danger)"],
    ["已取消", c.cancelled, "var(--text-muted)"],
  ];
  document.getElementById("dashboard-stats").innerHTML = statItems
    .map(
      ([label, val, color]) => `
      <div class="stat-card">
        <div class="stat-label">${label}</div>
        <div class="stat-value" style="color:${color}">${val}</div>
      </div>`
    )
    .join("");

  const activeBox = document.getElementById("dashboard-active-runs");
  if (!d.active_runs.length) {
    activeBox.innerHTML = '<div class="empty-state">当前没有运行中或待审批的任务</div>';
  } else {
    activeBox.innerHTML = d.active_runs
      .map(
        (r) => `
        <div class="list-item" data-open-run="${esc(r.id)}">
          <div class="list-item-top">
            <span class="list-item-title">${esc(r.workflow)}</span>
            <span class="${pillClass(r.status)}">${esc(STATE_LABELS[r.status] || r.status)}</span>
          </div>
          <div class="list-item-sub">
            <span>节点: <code>${esc(r.node)}</code></span>
            <span>${esc(r.task.slice(0, 72))}</span>
          </div>
        </div>`
      )
      .join("");
  }

  document.getElementById("dashboard-workflows").innerHTML = (d.workflows || [])
    .map(
      (wf) => `
      <div class="list-item" data-open-wf="${esc(wf.id)}">
        <div class="list-item-top">
          <span class="list-item-title">${esc(wf.name)} <span class="muted-text mono">(${esc(wf.id)})</span></span>
          <div class="inline-row">
            ${wf.backends.map((b) => `<span class="${pillClass(b)}">${esc(b)}</span>`).join("")}
            <button class="btn btn-primary btn-xs" data-launch-wf="${esc(wf.id)}">启动</button>
          </div>
        </div>
        <div class="list-item-sub">
          <span>${wf.node_count} 节点</span>
          <span>${wf.source === "preset" ? "内置预设" : wf.source === "preset_override" ? "已定制预设" : "自定义"}</span>
          ${wf.has_google_handoff ? '<span class="pill pill-purple">含 Google 交接</span>' : ""}
        </div>
      </div>`
    )
    .join("");

  const term = [...(d.recent_completed || []), ...(d.recent_failed || [])]
    .sort((a, b) => (b.created || 0) - (a.created || 0))
    .slice(0, 8);
  document.getElementById("dashboard-recent-terminal").innerHTML = term.length
    ? term
        .map(
          (r) => `
          <div class="list-item" data-open-run="${esc(r.id)}">
            <div class="list-item-top">
              <span class="list-item-title">${esc(r.workflow)} · <span class="mono muted-text">${esc(r.id.slice(0, 8))}</span></span>
              <span class="${pillClass(r.status)}">${esc(STATE_LABELS[r.status] || r.status)}</span>
            </div>
            <div class="list-item-sub">
              <span>${esc(r.task.slice(0, 72))}</span>
              <span>${fmtTime(r.created)}</span>
            </div>
          </div>`
        )
        .join("")
    : '<div class="empty-state">暂无已完成或失败的历史任务</div>';

  document.getElementById("dashboard-logs").innerHTML = (d.recent_logs || []).length
    ? d.recent_logs
        .slice(0, 10)
        .map(
          (l) => `
          <div class="log-entry">
            <div class="log-entry-head">
              <span class="${pillClass(l.level)}">${esc(l.category)}</span>
              <span class="log-time">${fmtTime(l.ts)}</span>
              <span>${esc(l.message)}</span>
            </div>
          </div>`
        )
        .join("")
    : '<div class="empty-state">暂无运行日志</div>';

  activeBox.querySelectorAll("[data-open-run]").forEach((el) => {
    el.addEventListener("click", () => openRunInTaskCenter(el.dataset.openRun));
  });
  document.querySelectorAll("#dashboard-recent-terminal [data-open-run]").forEach((el) => {
    el.addEventListener("click", () => openRunInTaskCenter(el.dataset.openRun));
  });
  document.querySelectorAll("#dashboard-workflows [data-launch-wf]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openNewTaskModal(btn.dataset.launchWf);
    });
  });
  document.querySelectorAll("#dashboard-workflows [data-open-wf]").forEach((el) => {
    el.addEventListener("click", () => {
      switchView("workflows");
      selectWorkflow(el.dataset.openWf);
    });
  });
}

function openRunInTaskCenter(runId) {
  state.selectedRunId = runId;
  switchView("tasks");
  loadRunDetail(runId);
}

/* ---------- New Task Modal ---------- */
async function openNewTaskModal(preselectWorkflowId = null) {
  const [wfRes, cfgRes, backends] = await Promise.all([
    api("GET", "/api/workflows"),
    api("GET", "/api/config"),
    api("GET", "/api/backends"),
  ]);
  const workflows = wfRes.workflows || [];
  const defaultWf = preselectWorkflowId || workflows[0]?.id || "dual_auto";
  const defaultProj = cfgRes.config?.default_project || "";

  openModal({
    title: "新建工作流任务",
    bodyHtml: `
      <form id="new-task-form" class="form-stack">
        <label class="field">
          <span>选择工作流</span>
          <select id="nt-workflow" class="input">
            ${workflows
              .map(
                (w) =>
                  `<option value="${esc(w.id)}" ${w.id === defaultWf ? "selected" : ""}>${esc(w.name)} (${esc(w.id)})</option>`
              )
              .join("")}
          </select>
        </label>
        <div id="nt-wf-preview" class="banner banner-ok"></div>
        <label class="field">
          <span>任务描述（Objective）</span>
          <textarea id="nt-task" class="input" rows="4" required placeholder="例如：在 solution.py 中实现 fibonacci(n)，并对负数抛出 ValueError。"></textarea>
        </label>
        <label class="field">
          <span>目标 Git 项目目录（可选；省略则创建空白隔离 Git 工作区）</span>
          <div class="inline-row">
            <input type="text" id="nt-project" class="input" value="${esc(defaultProj)}" placeholder="/Users/.../my-git-project" />
            <button type="button" class="btn btn-secondary btn-sm" id="btn-nt-validate-proj">验证目录</button>
          </div>
          <small id="nt-proj-status" class="muted-text">AIsys 仅读取已提交 HEAD 构建独立副本，不直接修改原仓库。</small>
        </label>
        <label class="field">
          <span>任务分类（用于条件路由 category）</span>
          <select id="nt-category" class="input">
            <option value="code">code（代码开发）</option>
            <option value="general">general（通用）</option>
            <option value="analysis">analysis（仅分析）</option>
            <option value="high_risk">high_risk（高风险需审批）</option>
          </select>
        </label>
      </form>
    `,
    footerHtml: `
      <button class="btn btn-secondary" id="nt-cancel">取消</button>
      <button class="btn btn-primary" id="nt-submit">▶ 启动任务</button>
    `,
    onMount: (dialog) => {
      const wfSelect = dialog.querySelector("#nt-workflow");
      const previewBox = dialog.querySelector("#nt-wf-preview");
      const updatePreview = async () => {
        try {
          const detail = await api("GET", `/api/workflows/${wfSelect.value}`);
          const agents = Object.entries(detail.workflow.agents || {});
          const agentHtml = agents.length
            ? agents
                .map(
                  ([aid, a]) =>
                    `<span class="${pillClass(a.backend)}">${esc(aid)}: ${esc(a.role)} (${esc(a.backend)}${a.backend === "google" ? "/" + esc(a.on_unavailable) : ""})</span>`
                )
                .join(" ")
            : '<span class="muted-text">无模型角色（仅本地节点）</span>';
          const warnings = detail.graph?.warnings || [];
          const hasGoogle = agents.some(([, a]) => a.backend === "google");
          const codexWarn =
            agents.some(([, a]) => a.backend === "codex") && !backends.codex.callable
              ? `<div>⚠ Codex 当前状态：${esc(STATE_LABELS[backends.codex.state] || backends.codex.state)}（${esc(backends.codex.detail)}）</div>`
              : "";
          const googleNote = hasGoogle
            ? "<div>ℹ 含 Google 角色：到达 Google 节点时会暂停并等待在官方 Antigravity 客户端交换或于本工作台提交人工交接结果。</div>"
            : "";
          previewBox.className = `banner ${warnings.length || codexWarn ? "banner-warning" : "banner-ok"}`;
          previewBox.innerHTML = `<div><strong>Agent 分工：</strong>${agentHtml}</div>${codexWarn}${googleNote}${warnings.map((w) => `<div>⚠ ${esc(w)}</div>`).join("")}`;
        } catch (err) {
          previewBox.className = "banner banner-error";
          previewBox.textContent = err.message;
        }
      };
      wfSelect.addEventListener("change", updatePreview);
      updatePreview();

      dialog.querySelector("#btn-nt-validate-proj").addEventListener("click", async () => {
        const val = dialog.querySelector("#nt-project").value.trim();
        const hint = dialog.querySelector("#nt-proj-status");
        if (!val) {
          hint.textContent = "已留空：将创建全新空白隔离 Git 工作区。";
          return;
        }
        try {
          const res = await api("POST", "/api/config/validate-project", { path: val });
          hint.textContent = `✓ 已验证 Git 仓库 (${res.branch} @ ${res.head.slice(0, 8)}): ${res.path}`;
          hint.style.color = "var(--success)";
        } catch (err) {
          hint.textContent = `✕ ${err.message}`;
          hint.style.color = "var(--danger)";
        }
      });

      dialog.querySelector("#nt-cancel").addEventListener("click", closeModal);
      dialog.querySelector("#nt-submit").addEventListener("click", async () => {
        const task = dialog.querySelector("#nt-task").value.trim();
        if (!task) {
          toast("请填写任务描述", "warning");
          return;
        }
        const btn = dialog.querySelector("#nt-submit");
        btn.disabled = true;
        try {
          const run = await api("POST", "/api/runs", {
            workflow_id: wfSelect.value,
            task,
            category: dialog.querySelector("#nt-category").value,
            project: dialog.querySelector("#nt-project").value.trim() || null,
          });
          closeModal();
          toast(`任务已创建 (${run.id.slice(0, 8)})`, "ok");
          openRunInTaskCenter(run.id);
        } catch (err) {
          toast(`启动失败：${err.message}`, "error");
          btn.disabled = false;
        }
      });
    },
  });
}

/* ---------- Task Center ---------- */
async function loadRuns(autoSelect = true) {
  try {
    const res = await api("GET", `/api/runs?status=${encodeURIComponent(state.runFilter)}`);
    state.runs = res.runs || [];
    renderRunsList();
    if (autoSelect && !state.selectedRunId && state.runs.length) {
      state.selectedRunId = state.runs[0].id;
      loadRunDetail(state.selectedRunId);
    }
  } catch (err) {
    toast(`加载任务列表失败：${err.message}`, "error");
  }
}

function renderRunsList() {
  const box = document.getElementById("runs-list");
  if (!state.runs.length) {
    box.innerHTML = '<div class="empty-state">当前筛选条件下暂无任务</div>';
    return;
  }
  box.innerHTML = state.runs
    .map(
      (r) => `
      <div class="list-item ${r.id === state.selectedRunId ? "selected" : ""}" data-run-id="${esc(r.id)}">
        <div class="list-item-top">
          <span class="list-item-title">${esc(r.workflow)}</span>
          <span class="${pillClass(r.status)}">${esc(STATE_LABELS[r.status] || r.status)}</span>
        </div>
        <div class="list-item-sub">
          <span class="mono">${esc(r.id.slice(0, 8))}</span>
          <span>节点: <code>${esc(r.node)}</code></span>
          <span>${fmtDuration(r.elapsed_seconds)}</span>
        </div>
        <div class="list-item-sub">${esc(r.task.slice(0, 80))}</div>
      </div>`
    )
    .join("");

  box.querySelectorAll("[data-run-id]").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedRunId = el.dataset.runId;
      renderRunsList();
      loadRunDetail(state.selectedRunId);
    });
  });
}

async function loadRunDetail(runId, showErrors = true) {
  try {
    const detail = await api("GET", `/api/runs/${runId}`);
    state.selectedRun = detail;
    renderRunDetail();
  } catch (err) {
    if (showErrors) toast(`读取任务详情失败：${err.message}`, "error");
  }
}

function buildLiveNodeStatus(run) {
  const nodes = {};
  for (const [name, res] of Object.entries(run.results || {})) {
    nodes[name] = res.status === "ok" ? "ok" : "failed";
  }
  for (const intr of run.interrupts || []) {
    nodes[intr.node] = "waiting";
  }
  if (run.active && run.node && run.node !== "end") {
    nodes[run.node] = "running";
  }
  return { nodes };
}

function renderInterruptCard(run, item) {
  const isApproval = item.kind === "approval";
  const writePaths = item.write_paths || [];
  const taskEnv = (run.exchange?.pending_tasks || []).find((t) => t.interrupt_id === item.id);
  const requiresVerdict = taskEnv?.permissions?.requires_verdict;

  if (isApproval) {
    return `
      <div class="interrupt-card" data-interrupt-id="${esc(item.id)}">
        <div class="list-item-top">
          <strong>⚠ 人工审批门控：节点 <code>${esc(item.node)}</code></strong>
          <span class="pill pill-waiting">等待确认</span>
        </div>
        <pre class="pre-box">${esc(item.prompt || "请核查最新测试结果与代码变更后决定是否继续。")}</pre>
        <div class="inline-row">
          <button class="btn btn-success btn-sm" data-approve-id="${esc(item.id)}">✓ 批准并继续</button>
          <button class="btn btn-danger btn-sm" data-reject-id="${esc(item.id)}">✕ 驳回终止</button>
        </div>
      </div>
    `;
  }

  return `
    <div class="interrupt-card" data-interrupt-id="${esc(item.id)}">
      <div class="list-item-top">
        <strong>⇄ Google 角色交接：<code>${esc(item.node)}</code> (${esc(item.role || "Google")})</strong>
        <span class="pill pill-purple">等待 Antigravity / 人工提交</span>
      </div>
      <div class="muted-text">可在官方 Antigravity 会话内执行 <code>aisys exchange pending ${esc(run.id)}</code> 自动提交，或在下方直接输入经你核阅的结果：</div>
      <details>
        <summary style="cursor:pointer;font-weight:600;font-size:12.5px;">查看完整节点提示词（可复制至官方 Google 客户端）</summary>
        <pre class="pre-box" style="margin-top:6px;">${esc(item.prompt || "")}</pre>
      </details>
      <div class="form-stack">
        <label class="field">
          <span>结果摘要（Summary）</span>
          <textarea class="input" rows="3" data-handoff-result placeholder="输入规划方案、分析结论或审查总结…"></textarea>
        </label>
        <div class="grid-2">
          <label class="field">
            <span>审查结论 Verdict ${requiresVerdict ? "（本节点要求 pass / fail）" : ""}</span>
            <select class="input" data-handoff-verdict>
              <option value="none" ${!requiresVerdict ? "selected" : ""}>none（非审查节点）</option>
              <option value="pass">pass（审查通过）</option>
              <option value="fail">fail（打回返工）</option>
            </select>
          </label>
          <label class="field">
            <span>审查反馈 review_feedback（打回返工时将自动传给下游 Codex）</span>
            <input type="text" class="input" data-handoff-feedback placeholder="具体改进项与验证要求（可选）" />
          </label>
        </div>
        ${writePaths
          .map(
            (wp) => `
          <label class="field">
            <span>允许写入文件完整内容：<code>${esc(wp)}</code>（留空则不修改）</span>
            <textarea class="input mono" rows="4" data-handoff-file="${esc(wp)}" placeholder="输入完整 UTF-8 文件内容…"></textarea>
          </label>`
          )
          .join("")}
        <div class="inline-row">
          <button class="btn btn-primary btn-sm" data-handoff-submit="${esc(item.id)}">提交结果并推进工作流</button>
          <button class="btn btn-danger-ghost btn-sm" data-handoff-reject="${esc(item.id)}">拒绝该交接</button>
        </div>
      </div>
    </div>
  `;
}

function renderRunDetail() {
  const panel = document.getElementById("run-detail-panel");
  const r = state.selectedRun;
  if (!r) {
    panel.innerHTML = '<div class="empty-state">请选择一个任务查看详情</div>';
    return;
  }

  const isTerminal = ["complete", "failed", "cancelled"].includes(r.status);
  const canResume = ["paused", "recoverable", "ready"].includes(r.status) && !r.active;
  const interruptsHtml = (r.interrupts || [])
    .filter((i) => i.kind !== "pause")
    .map((item) => renderInterruptCard(r, item))
    .join("");

  panel.innerHTML = `
    <div class="card-header">
      <div>
        <h2>${esc(r.workflow)} <span class="${pillClass(r.status)}">${esc(STATE_LABELS[r.status] || r.status)}</span></h2>
        <div class="muted-text mono" style="font-size:12px;">
          Run: ${esc(r.id)} · 当前节点: ${esc(r.node)} · 耗时: ${fmtDuration(r.elapsed_seconds)}
          ${r.source_commit ? ` · 快照: ${esc(r.source_commit.slice(0, 8))}` : ""}
        </div>
      </div>
      <div class="header-actions">
        ${!isTerminal ? `<button class="btn btn-secondary btn-sm" id="btn-run-pause">⏸ 请求暂停</button>` : ""}
        ${canResume ? `<button class="btn btn-primary btn-sm" id="btn-run-resume">▶ 继续 / 恢复</button>` : ""}
        ${!isTerminal ? `<button class="btn btn-danger-ghost btn-sm" id="btn-run-cancel">✕ 取消任务</button>` : ""}
        ${r.active ? `<button class="btn btn-danger btn-sm" id="btn-run-force-cancel">⏹ 终止当前进程</button>` : ""}
      </div>
    </div>

    ${r.reason ? `<div class="banner banner-warning">${esc(r.reason)}</div>` : ""}
    <div class="muted-text" style="margin:8px 0 12px;"><strong>任务目标：</strong>${esc(r.task)}</div>

    ${interruptsHtml}

    <div class="graph-canvas compact" id="run-live-graph"></div>

    <div class="tabs-bar" style="margin-top:14px;">
      <button class="tab-btn ${state.runTab === "steps" ? "active" : ""}" data-run-tab="steps">执行步骤与输出</button>
      <button class="tab-btn ${state.runTab === "diff" ? "active" : ""}" data-run-tab="diff">代码变更 Diff</button>
      <button class="tab-btn ${state.runTab === "report" ? "active" : ""}" data-run-tab="report">运行报告</button>
      <button class="tab-btn ${state.runTab === "raw" ? "active" : ""}" data-run-tab="raw">路由与原始日志</button>
    </div>
    <div id="run-tab-body"></div>
  `;

  if (runGraph) runGraph.destroy();
  runGraph = new WorkflowGraphCanvas(panel.querySelector("#run-live-graph"), { interactive: false });
  runGraph.setGraph(r.graph, { resetLayout: true, liveStatus: buildLiveNodeStatus(r) });

  panel.querySelector("#btn-run-pause")?.addEventListener("click", async () => {
    try {
      await api("POST", `/api/runs/${r.id}/pause`);
      toast("已请求在下一节点边界暂停", "info");
      loadRunDetail(r.id);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  panel.querySelector("#btn-run-resume")?.addEventListener("click", async () => {
    try {
      await api("POST", `/api/runs/${r.id}/resume`);
      toast("已触发继续/恢复执行", "ok");
      loadRunDetail(r.id);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  panel.querySelector("#btn-run-cancel")?.addEventListener("click", async () => {
    try {
      await api("POST", `/api/runs/${r.id}/cancel`, { terminate_active: false });
      toast("已请求在安全边界取消该任务", "warning");
      loadRunDetail(r.id);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  panel.querySelector("#btn-run-force-cancel")?.addEventListener("click", async () => {
    if (!confirm("确认向当前运行中的任务子进程发送 SIGTERM 并取消该工作流？不会影响其他工作流或 AIsys 服务。")) return;
    try {
      await api("POST", `/api/runs/${r.id}/cancel`, { terminate_active: true });
      toast("已发送受控中断信号并取消任务", "warning");
      loadRunDetail(r.id);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  panel.querySelectorAll("[data-approve-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("POST", `/api/runs/${r.id}/interrupts/${btn.dataset.approveId}`, {
          accept: true,
          status: "completed",
          result: "Approved via AIsys Web Workbench",
          human_confirmed: true,
        });
        toast("已批准，工作流继续执行", "ok");
        loadRunDetail(r.id);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  panel.querySelectorAll("[data-reject-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("POST", `/api/runs/${r.id}/interrupts/${btn.dataset.rejectId}`, {
          accept: false,
          status: "rejected",
          error: "Rejected by user in Web Workbench",
          human_confirmed: true,
        });
        toast("已驳回审批", "warning");
        loadRunDetail(r.id);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  panel.querySelectorAll("[data-handoff-submit]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".interrupt-card");
      const result = card.querySelector("[data-handoff-result]").value.trim();
      const verdict = card.querySelector("[data-handoff-verdict]").value;
      const feedback = card.querySelector("[data-handoff-feedback]").value.trim();
      const files = [];
      card.querySelectorAll("[data-handoff-file]").forEach((ta) => {
        if (ta.value.length > 0) {
          files.push({ path: ta.dataset.handoffFile, content: ta.value });
        }
      });
      if (!result && !feedback) {
        toast("请填写结果摘要或审查反馈", "warning");
        return;
      }
      btn.disabled = true;
      try {
        await api("POST", `/api/runs/${r.id}/interrupts/${btn.dataset.handoffSubmit}`, {
          accept: true,
          status: "completed",
          result,
          verdict,
          review_feedback: feedback,
          files,
        });
        toast("交接结果已提交，后端正在推进下一节点", "ok");
        loadRunDetail(r.id);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
      }
    });
  });

  panel.querySelectorAll("[data-handoff-reject]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api("POST", `/api/runs/${r.id}/interrupts/${btn.dataset.handoffReject}`, {
          accept: false,
          status: "rejected",
          error: "Handoff rejected in Web Workbench",
        });
        toast("已拒绝该交接节点", "warning");
        loadRunDetail(r.id);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  panel.querySelectorAll("[data-run-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.runTab = tab.dataset.runTab;
      renderRunDetail();
    });
  });

  renderRunTabBody();
}

async function renderRunTabBody() {
  const box = document.getElementById("run-tab-body");
  const r = state.selectedRun;
  if (!box || !r) return;

  if (state.runTab === "steps") {
    const events = r.events || [];
    const results = Object.entries(r.results || {});
    if (!events.length && !results.length) {
      box.innerHTML = '<div class="empty-state">尚未产生执行记录</div>';
      return;
    }
    const items = events.length
      ? events.map(
          (ev) => `
          <div class="log-entry">
            <div class="log-entry-head">
              <strong>${esc(ev.node)}</strong>
              <span class="${pillClass(ev.backend)}">${esc(ev.agent_role)} · ${esc(ev.backend)}</span>
              <span class="${pillClass(ev.in_progress ? "running" : ev.status)}">${ev.in_progress ? "正在执行" : esc(ev.status)}</span>
              ${ev.verdict && ev.verdict !== "none" ? `<span class="${pillClass(ev.verdict)}">verdict: ${esc(ev.verdict)}</span>` : ""}
              <span class="log-time">第 ${ev.visit} 轮 / 尝试 ${ev.attempt} · ${fmtDuration(ev.duration)} · ${fmtTime(ev.started)}</span>
            </div>
            ${ev.usage && Object.keys(ev.usage).length ? `<div class="muted-text mono" style="font-size:11.5px;margin-top:4px;">Tokens: ${esc(JSON.stringify(ev.usage))}</div>` : ""}
            ${ev.artifacts?.length ? `<div class="muted-text" style="margin-top:4px;">文件变更: ${ev.artifacts.map((a) => `<code>${esc(a)}</code>`).join(", ")}</div>` : ""}
            ${ev.output ? `<pre class="pre-box" style="margin-top:6px;">${esc(ev.output)}</pre>` : ""}
            ${ev.review_feedback ? `<div class="banner banner-warning" style="margin-top:6px;"><strong>审查意见：</strong>${esc(ev.review_feedback)}</div>` : ""}
            ${ev.errors?.length ? `<div class="banner banner-error" style="margin-top:6px;">${esc(ev.errors.join("\n"))}</div>` : ""}
          </div>`
        )
      : results.map(
          ([name, res]) => `
          <div class="log-entry">
            <div class="log-entry-head">
              <strong>${esc(name)}</strong>
              <span class="${pillClass(res.status)}">${esc(res.status)}</span>
            </div>
            <pre class="pre-box" style="margin-top:6px;">${esc(res.output || "")}</pre>
          </div>`
        );
    box.innerHTML = `<div class="list-stack">${items.join("")}</div>`;
    return;
  }

  if (state.runTab === "diff") {
    box.innerHTML = '<div class="muted-text">正在加载隔离工作区 Diff…</div>';
    try {
      const spaces = Object.keys(r.exchange?.workspaces || { main: "" });
      const space = spaces.includes(state.runDiffSpace) ? state.runDiffSpace : spaces[0] || "main";
      state.runDiffSpace = space;
      const d = await api("GET", `/api/runs/${r.id}/diff?space=${encodeURIComponent(space)}`);
      const diffHtml = (d.diff || "(无已追踪改动)")
        .split("\n")
        .map((line) => {
          const cls = line.startsWith("+")
            ? "diff-line-add"
            : line.startsWith("-")
            ? "diff-line-del"
            : line.startsWith("@@")
            ? "diff-line-hunk"
            : "";
          return `<span class="${cls}">${esc(line)}</span>`;
        })
        .join("\n");
      box.innerHTML = `
        <div class="inline-row" style="margin-bottom:8px;">
          <span class="muted-label">工作区：</span>
          <select id="diff-space-select" class="input input-sm">
            ${d.spaces.map((sp) => `<option value="${esc(sp)}" ${sp === d.space ? "selected" : ""}>${esc(sp)}</option>`).join("")}
          </select>
          <span class="muted-text mono" style="font-size:11.5px;">${esc(d.workspace)}</span>
        </div>
        <div class="muted-text" style="margin-bottom:8px;">变更文件：${d.changed_paths.length ? d.changed_paths.map((p) => `<code>${esc(p)}</code>`).join(", ") : "(无)"}</div>
        <pre class="pre-box mono">${diffHtml}</pre>
      `;
      box.querySelector("#diff-space-select")?.addEventListener("change", (e) => {
        state.runDiffSpace = e.target.value;
        renderRunTabBody();
      });
    } catch (err) {
      box.innerHTML = `<div class="banner banner-error">${esc(err.message)}</div>`;
    }
    return;
  }

  if (state.runTab === "report") {
    try {
      const rep = await api("GET", `/api/runs/${r.id}/report`);
      box.innerHTML = `<pre class="pre-box">${esc(rep.report)}</pre>`;
    } catch (err) {
      box.innerHTML = `<div class="banner banner-error">${esc(err.message)}</div>`;
    }
    return;
  }

  if (state.runTab === "raw") {
    const traceHtml = (r.trace || [])
      .map(
        (t) =>
          `<li><code>${esc(t.node)}</code> → <code>${esc(t.next)}</code> (${esc(t.status)} / ${esc(t.verdict)} · 规则 ${t.rule ?? "default"})</li>`
      )
      .join("");
    box.innerHTML = `
      <div class="form-stack">
        <div><strong>路由轨迹：</strong><ul style="margin:6px 0;padding-left:18px;">${traceHtml || "<li>暂无</li>"}</ul></div>
        <div><strong>后台驱动日志（driver.log）：</strong><pre class="pre-box mono">${esc(r.driver_log || "(空)")}</pre></div>
      </div>
    `;
  }
}

/* ---------- Workflow Builder ---------- */
async function loadWorkflows(selectFirst = true) {
  try {
    const [wfRes, agRes] = await Promise.all([api("GET", "/api/workflows"), api("GET", "/api/agents")]);
    state.workflows = wfRes.workflows || [];
    state.agents = agRes.agents || [];
    renderWorkflowList();
    if (selectFirst && (!state.activeWfId || !state.workflows.some((w) => w.id === state.activeWfId))) {
      if (state.workflows.length) await selectWorkflow(state.workflows[0].id);
    }
  } catch (err) {
    toast(`加载工作流失败：${err.message}`, "error");
  }
}

function renderWorkflowList() {
  const box = document.getElementById("wf-list");
  box.innerHTML = state.workflows
    .map(
      (wf) => `
      <div class="list-item ${wf.id === state.activeWfId ? "selected" : ""}" data-wf-id="${esc(wf.id)}">
        <div class="list-item-top">
          <span class="list-item-title">${esc(wf.name)}</span>
        </div>
        <div class="list-item-sub">
          <span class="mono">${esc(wf.id)}</span>
          <span>${wf.node_count} 节点</span>
          <span>${wf.source === "preset" ? "预设" : wf.source === "preset_override" ? "已覆盖" : "自定义"}</span>
        </div>
      </div>`
    )
    .join("");
  box.querySelectorAll("[data-wf-id]").forEach((el) => {
    el.addEventListener("click", () => selectWorkflow(el.dataset.wfId));
  });
}

async function selectWorkflow(wfId) {
  try {
    const item = await api("GET", `/api/workflows/${wfId}`);
    state.activeWfId = item.id;
    state.activeWfSource = item.source;
    state.activeWfRevision = item.revision;
    state.activeWfData = item.workflow;
    state.activeWfYaml = item.yaml;
    state.selectedNodeId = item.workflow.start;
    renderWorkflowList();
    document.getElementById("wf-name-input").value = item.workflow.name;
    document.getElementById("wf-source-badge").textContent =
      item.source === "preset" ? "内置预设" : item.source === "preset_override" ? "已定制预设" : "自定义工作流";
    document.getElementById("wf-yaml-editor").value = item.yaml;
    renderWorkflowCanvas(item.graph, true);
    renderWorkflowValidation({ valid: true, errors: [], warnings: item.graph.warnings || [] });
    renderInspector();
  } catch (err) {
    toast(`加载工作流失败：${err.message}`, "error");
  }
}

function buildLocalGraphFromData(wf) {
  const nodes = [];
  const edges = [];
  const nodeEntries = Object.entries(wf.nodes || {});
  for (const [id, n] of nodeEntries) {
    const agent = n.agent ? wf.agents?.[n.agent] : null;
    nodes.push({
      id,
      kind: n.kind,
      is_start: wf.start === id,
      agent: n.agent || null,
      agent_role: agent?.role || null,
      backend: agent?.backend || (n.kind === "check" ? "local" : null),
      space: n.space || "main",
      max_visits: n.max_visits || 1,
    });
    if (n.kind === "parallel") {
      for (const b of n.branches || []) {
        edges.push({ source: id, target: b, kind: "parallel", label: "parallel" });
      }
    }
    const isBranch = nodeEntries.some(([, other]) => (other.branches || []).includes(id));
    if (n.next && n.next !== "end") {
      edges.push({ source: id, target: n.next, kind: "next", label: "next" });
    } else if (!isBranch && (!n.next || n.next === "end")) {
      edges.push({ source: id, target: "end", kind: "next", label: "finish" });
    }
    for (const r of n.routes || []) {
      const cond = Object.entries(r.when || {})
        .filter(([, v]) => v != null && v !== "")
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      edges.push({ source: id, target: r.to, kind: "route", label: cond || "route" });
    }
  }
  return { nodes, edges };
}

function renderWorkflowCanvas(graphObj = null, resetLayout = false) {
  const container = document.getElementById("wf-graph-canvas");
  if (!builderGraph) {
    builderGraph = new WorkflowGraphCanvas(container, {
      interactive: true,
      onSelect: (nodeId) => {
        state.selectedNodeId = nodeId;
        state.wfInspectorTab = "node";
        document.querySelectorAll("[data-inspector]").forEach((b) =>
          b.classList.toggle("active", b.dataset.inspector === "node")
        );
        renderInspector();
      },
      onConnect: (sourceId, targetId) => {
        if (!state.activeWfData?.nodes?.[sourceId]) return;
        state.activeWfData.nodes[sourceId].next = targetId;
        toast(`已连接 ${sourceId} → ${targetId}`, "ok");
        renderWorkflowCanvas(null, false);
        renderInspector();
        validateCurrentWorkflow();
      },
    });
  }
  const graph = graphObj || buildLocalGraphFromData(state.activeWfData);
  builderGraph.setGraph(graph, { selectedId: state.selectedNodeId, resetLayout });
}

function renderWorkflowValidation(res) {
  const badge = document.getElementById("wf-valid-badge");
  const banner = document.getElementById("wf-validation-banner");
  if (!res.valid) {
    badge.className = "pill pill-failed";
    badge.textContent = "校验失败";
    banner.className = "banner banner-error";
    banner.innerHTML = res.errors.map((e) => `<div>✕ ${esc(e)}</div>`).join("");
    banner.classList.remove("hidden");
  } else if (res.warnings?.length) {
    badge.className = "pill pill-warning";
    badge.textContent = `有效（${res.warnings.length} 提示）`;
    banner.className = "banner banner-warning";
    banner.innerHTML = res.warnings.map((w) => `<div>⚠ ${esc(w)}</div>`).join("");
    banner.classList.remove("hidden");
  } else {
    badge.className = "pill pill-ok";
    badge.textContent = "校验通过";
    banner.classList.add("hidden");
  }
}

async function validateCurrentWorkflow() {
  if (!state.activeWfData && state.wfMode === "visual") return null;
  const payload =
    state.wfMode === "yaml"
      ? { yaml: document.getElementById("wf-yaml-editor").value }
      : { workflow: state.activeWfData };
  const res = await api("POST", "/api/workflows/validate", payload);
  renderWorkflowValidation(res);
  if (res.valid) {
    state.activeWfData = res.workflow;
    state.activeWfYaml = res.yaml;
    if (state.wfMode === "visual") {
      document.getElementById("wf-yaml-editor").value = res.yaml;
      renderWorkflowCanvas(res.graph, false);
    }
  }
  return res;
}

function addNodeToWorkflow(kind) {
  const wf = state.activeWfData;
  if (!wf) return;
  let idx = Object.keys(wf.nodes).length + 1;
  let id = `${kind}_${idx}`;
  while (wf.nodes[id]) id = `${kind}_${++idx}`;

  const node = {
    kind,
    agent: null,
    instruction: "",
    space: "main",
    files: [],
    shared_files: [],
    inputs: [],
    argv: [],
    branches: [],
    timeout: 120,
    retries: 0,
    max_visits: 1,
    next: "end",
    routes: [],
  };
  if (kind === "agent") {
    let agentKey = Object.keys(wf.agents)[0];
    if (!agentKey) {
      agentKey = "codex_agent";
      wf.agents[agentKey] = {
        role: "Executor",
        backend: "codex",
        instructions: "Execute the assigned task cleanly.",
        write_paths: [],
        reuse_session: false,
        on_unavailable: "stop",
      };
    }
    node.agent = agentKey;
  } else if (kind === "check") {
    node.argv = ["python", "-c", "print('check ok')"];
    node.timeout = 30;
  } else if (kind === "approval") {
    node.instruction = "请核查最新输出并决定是否批准继续。";
  }

  wf.nodes[id] = node;
  state.selectedNodeId = id;
  renderWorkflowCanvas(null, true);
  renderInspector();
  validateCurrentWorkflow();
}

function renderInspector() {
  const box = document.getElementById("wf-inspector-content");
  const wf = state.activeWfData;
  if (!wf) {
    box.innerHTML = '<div class="empty-state">请先选择一个工作流</div>';
    return;
  }

  if (state.wfInspectorTab === "node") {
    const id = state.selectedNodeId;
    const node = id ? wf.nodes[id] : null;
    if (!node) {
      box.innerHTML = '<div class="empty-state">点击左侧画布中的节点进行配置</div>';
      return;
    }
    const allNodeIds = Object.keys(wf.nodes);
    const destOptions = ["end", ...allNodeIds]
      .map((nid) => `<option value="${esc(nid)}" ${node.next === nid ? "selected" : ""}>${esc(nid)}</option>`)
      .join("");
    const agentOptions = Object.entries(wf.agents || {})
      .map(
        ([aid, a]) =>
          `<option value="${esc(aid)}" ${node.agent === aid ? "selected" : ""}>${esc(aid)} (${esc(a.role)} / ${esc(a.backend)})</option>`
      )
      .join("");

    const routesHtml = (node.routes || [])
      .map(
        (r, i) => `
        <div class="route-row" data-route-idx="${i}">
          <div class="inline-row">
            <select class="input input-sm" data-r-field="status">
              <option value="">任意 status</option>
              ${["ok", "failed", "timeout", "unavailable", "interrupted", "rejected"]
                .map((s) => `<option value="${s}" ${r.when?.status === s ? "selected" : ""}>status=${s}</option>`)
                .join("")}
            </select>
            <select class="input input-sm" data-r-field="verdict">
              <option value="">任意 verdict</option>
              ${["pass", "fail", "none"]
                .map((v) => `<option value="${v}" ${r.when?.verdict === v ? "selected" : ""}>verdict=${v}</option>`)
                .join("")}
            </select>
          </div>
          <div class="inline-row">
            <input type="text" class="input input-sm" data-r-field="category" placeholder="category (可选)" value="${esc(r.when?.category || "")}" />
            <select class="input input-sm" data-r-field="to">
              ${["end", ...allNodeIds]
                .map((nid) => `<option value="${esc(nid)}" ${r.to === nid ? "selected" : ""}>→ ${esc(nid)}</option>`)
                .join("")}
            </select>
            <button type="button" class="btn btn-danger-ghost btn-xs" data-r-del="${i}">删</button>
          </div>
        </div>`
      )
      .join("");

    box.innerHTML = `
      <div class="inline-row" style="justify-content:space-between;">
        <strong>节点：<code>${esc(id)}</code></strong>
        <div class="inline-row">
          ${wf.start !== id ? `<button class="btn btn-ghost btn-xs" id="btn-set-start">设为起点</button>` : '<span class="pill pill-info">起点</span>'}
          <button class="btn btn-danger-ghost btn-xs" id="btn-del-node">删除节点</button>
        </div>
      </div>

      <label class="field">
        <span>节点类型</span>
        <select id="ni-kind" class="input">
          ${["agent", "check", "approval", "route", "parallel", "report"]
            .map((k) => `<option value="${k}" ${node.kind === k ? "selected" : ""}>${k}</option>`)
            .join("")}
        </select>
      </label>

      ${
        node.kind === "agent"
          ? `<label class="field"><span>绑定 Agent</span><select id="ni-agent" class="input">${agentOptions}</select></label>`
          : ""
      }

      ${
        node.kind === "check"
          ? `<label class="field"><span>执行命令 argv（每行一个参数）</span><textarea id="ni-argv" class="input mono" rows="4">${esc((node.argv || []).join("\n"))}</textarea></label>`
          : ""
      }

      ${
        node.kind === "parallel"
          ? `<label class="field"><span>并行分支节点 ID（逗号分隔，至少 2 个）</span><input id="ni-branches" class="input mono" value="${esc((node.branches || []).join(", "))}" /></label>`
          : ""
      }

      <label class="field">
        <span>节点指令（Instruction）</span>
        <textarea id="ni-instruction" class="input" rows="2">${esc(node.instruction || "")}</textarea>
      </label>

      <div class="grid-2">
        <label class="field"><span>工作区 space</span><input id="ni-space" class="input mono" value="${esc(node.space || "main")}" /></label>
        <label class="field"><span>默认下一节点 next</span><select id="ni-next" class="input">${destOptions}</select></label>
      </div>

      <div class="grid-2">
        <label class="field"><span>最大访问轮数 max_visits</span><input id="ni-max-visits" type="number" min="1" max="30" class="input" value="${node.max_visits || 1}" /></label>
        <label class="field"><span>超时（秒）timeout</span><input id="ni-timeout" type="number" min="1" max="3600" class="input" value="${node.timeout || 120}" /></label>
      </div>

      <div class="grid-2">
        <label class="field"><span>重试次数 retries</span><input id="ni-retries" type="number" min="0" max="5" class="input" value="${node.retries || 0}" /></label>
        <label class="field"><span>输入节点 inputs（逗号分隔）</span><input id="ni-inputs" class="input mono" value="${esc((node.inputs || []).join(", "))}" /></label>
      </div>

      <label class="field">
        <span>上下文文件 files（逗号分隔相对路径）</span>
        <input id="ni-files" class="input mono" value="${esc((node.files || []).join(", "))}" placeholder="solution.py" />
      </label>

      <div class="divider"></div>
      <div class="inline-row" style="justify-content:space-between;">
        <span class="muted-label">条件路由 routes</span>
        <button type="button" class="btn btn-secondary btn-xs" id="btn-add-route">+ 添加条件路由</button>
      </div>
      <div id="ni-routes-list" class="form-stack">${routesHtml || '<div class="muted-text">无条件路由，将始终沿 next 推进</div>'}</div>
    `;

    const syncNode = () => {
      const nextKind = box.querySelector("#ni-kind").value;
      node.kind = nextKind;
      if (nextKind === "agent") {
        node.agent = box.querySelector("#ni-agent")?.value || Object.keys(wf.agents)[0] || null;
        node.argv = [];
        node.branches = [];
      } else if (nextKind === "check") {
        node.agent = null;
        node.branches = [];
        const rawArgv = box.querySelector("#ni-argv")?.value ?? "python\n-c\nprint('ok')";
        node.argv = rawArgv.split("\n").map((s) => s.trim()).filter(Boolean);
      } else if (nextKind === "parallel") {
        node.agent = null;
        node.argv = [];
        const rawBr = box.querySelector("#ni-branches")?.value ?? "";
        node.branches = rawBr.split(",").map((s) => s.trim()).filter(Boolean);
      } else {
        node.agent = null;
        node.argv = [];
        node.branches = [];
      }
      node.instruction = box.querySelector("#ni-instruction").value;
      node.space = box.querySelector("#ni-space").value.trim() || "main";
      node.next = box.querySelector("#ni-next").value;
      node.max_visits = Math.max(1, parseInt(box.querySelector("#ni-max-visits").value, 10) || 1);
      node.timeout = Math.max(1, parseInt(box.querySelector("#ni-timeout").value, 10) || 120);
      node.retries = Math.max(0, parseInt(box.querySelector("#ni-retries").value, 10) || 0);
      node.inputs = box.querySelector("#ni-inputs").value.split(",").map((s) => s.trim()).filter(Boolean);
      node.files = box.querySelector("#ni-files").value.split(",").map((s) => s.trim()).filter(Boolean);

      const newRoutes = [];
      box.querySelectorAll(".route-row").forEach((row) => {
        const when = {};
        const st = row.querySelector('[data-r-field="status"]').value;
        const vd = row.querySelector('[data-r-field="verdict"]').value;
        const cat = row.querySelector('[data-r-field="category"]').value.trim();
        if (st) when.status = st;
        if (vd) when.verdict = vd;
        if (cat) when.category = cat;
        const to = row.querySelector('[data-r-field="to"]').value;
        newRoutes.push({ when, to });
      });
      node.routes = newRoutes;
      renderWorkflowCanvas(null, false);
      validateCurrentWorkflow();
    };

    box.querySelector("#ni-kind").addEventListener("change", () => {
      syncNode();
      renderInspector();
    });
    box.querySelectorAll("input, textarea, select").forEach((el) => {
      if (el.id === "ni-kind") return;
      el.addEventListener("change", syncNode);
    });
    box.querySelector("#btn-set-start")?.addEventListener("click", () => {
      wf.start = id;
      renderWorkflowCanvas(null, true);
      renderInspector();
      validateCurrentWorkflow();
    });
    box.querySelector("#btn-del-node")?.addEventListener("click", () => {
      if (Object.keys(wf.nodes).length <= 1) {
        toast("工作流至少需要保留 1 个节点", "warning");
        return;
      }
      delete wf.nodes[id];
      if (wf.start === id) wf.start = Object.keys(wf.nodes)[0];
      for (const other of Object.values(wf.nodes)) {
        if (other.next === id) other.next = "end";
        other.routes = (other.routes || []).filter((r) => r.to !== id);
        other.inputs = (other.inputs || []).filter((i) => i !== id);
        other.branches = (other.branches || []).filter((b) => b !== id);
      }
      state.selectedNodeId = wf.start;
      renderWorkflowCanvas(null, true);
      renderInspector();
      validateCurrentWorkflow();
    });
    box.querySelector("#btn-add-route")?.addEventListener("click", () => {
      node.routes = node.routes || [];
      node.routes.push({ when: { status: "failed" }, to: wf.start });
      renderInspector();
      renderWorkflowCanvas(null, false);
      validateCurrentWorkflow();
    });
    box.querySelectorAll("[data-r-del]").forEach((btn) => {
      btn.addEventListener("click", () => {
        node.routes.splice(Number(btn.dataset.rDel), 1);
        renderInspector();
        renderWorkflowCanvas(null, false);
        validateCurrentWorkflow();
      });
    });
    return;
  }

  if (state.wfInspectorTab === "agents") {
    const libraryOpts = state.agents
      .map((a) => `<option value="${esc(a.id)}">${esc(a.name)} (${esc(a.role)} / ${esc(a.backend)})</option>`)
      .join("");
    const cards = Object.entries(wf.agents || {})
      .map(
        ([aid, a]) => `
        <div class="route-row" data-wf-agent="${esc(aid)}">
          <div class="inline-row" style="justify-content:space-between;">
            <strong><code>${esc(aid)}</code></strong>
            <button type="button" class="btn btn-danger-ghost btn-xs" data-del-wf-agent="${esc(aid)}">移除</button>
          </div>
          <div class="grid-2">
            <label class="field"><span>职责 Role</span><input class="input input-sm" data-wa="role" value="${esc(a.role)}" /></label>
            <label class="field"><span>后端 Backend</span>
              <select class="input input-sm" data-wa="backend">
                <option value="codex" ${a.backend === "codex" ? "selected" : ""}>codex</option>
                <option value="google" ${a.backend === "google" ? "selected" : ""}>google</option>
              </select>
            </label>
          </div>
          <label class="field"><span>Prompt 指令</span><textarea class="input" rows="2" data-wa="instructions">${esc(a.instructions)}</textarea></label>
          <div class="grid-2">
            <label class="field"><span>可写路径（逗号分隔）</span><input class="input input-sm mono" data-wa="write_paths" value="${esc((a.write_paths || []).join(", "))}" /></label>
            <label class="field"><span>不可用策略</span>
              <select class="input input-sm" data-wa="on_unavailable">
                <option value="stop" ${a.on_unavailable === "stop" ? "selected" : ""}>stop</option>
                <option value="handoff" ${a.on_unavailable === "handoff" ? "selected" : ""}>handoff（交接）</option>
              </select>
            </label>
          </div>
        </div>`
      )
      .join("");

    box.innerHTML = `
      <div class="inline-row">
        <select id="wa-import-select" class="input input-sm" style="flex:1;">${libraryOpts}</select>
        <button class="btn btn-secondary btn-xs" id="btn-wa-import">从 Agent 库导入</button>
      </div>
      <div class="form-stack">${cards || '<div class="muted-text">当前工作流未定义 Agent</div>'}</div>
    `;

    box.querySelector("#btn-wa-import")?.addEventListener("click", () => {
      const selId = box.querySelector("#wa-import-select").value;
      const prof = state.agents.find((a) => a.id === selId);
      if (!prof) return;
      wf.agents[prof.id] = {
        role: prof.role,
        backend: prof.backend,
        instructions: prof.instructions,
        write_paths: [...(prof.write_paths || [])],
        reuse_session: Boolean(prof.reuse_session),
        on_unavailable: prof.on_unavailable,
      };
      if (prof.model) wf.agents[prof.id].model = prof.model;
      toast(`已导入 Agent '${prof.id}' 到当前工作流`, "ok");
      renderInspector();
      validateCurrentWorkflow();
    });

    box.querySelectorAll("[data-wf-agent]").forEach((card) => {
      const aid = card.dataset.wfAgent;
      const syncAgent = () => {
        const a = wf.agents[aid];
        a.role = card.querySelector('[data-wa="role"]').value.trim() || "Agent";
        a.backend = card.querySelector('[data-wa="backend"]').value;
        a.instructions = card.querySelector('[data-wa="instructions"]').value;
        a.write_paths = card.querySelector('[data-wa="write_paths"]').value.split(",").map((s) => s.trim()).filter(Boolean);
        a.on_unavailable = card.querySelector('[data-wa="on_unavailable"]').value;
        if (a.backend === "google") a.reuse_session = false;
        renderWorkflowCanvas(null, false);
        validateCurrentWorkflow();
      };
      card.querySelectorAll("input, textarea, select").forEach((el) => el.addEventListener("change", syncAgent));
    });

    box.querySelectorAll("[data-del-wf-agent]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const aid = btn.dataset.delWfAgent;
        delete wf.agents[aid];
        renderInspector();
        validateCurrentWorkflow();
      });
    });
    return;
  }

  // Budget & Notes
  const b = wf.budget || {};
  box.innerHTML = `
    <label class="field"><span>长期项目说明 project_notes</span><textarea id="wb-notes" class="input" rows="4">${esc(wf.project_notes || "")}</textarea></label>
    <div class="grid-2">
      <label class="field"><span>总步数上限 steps</span><input id="wb-steps" type="number" min="1" max="120" class="input" value="${b.steps ?? 24}" /></label>
      <label class="field"><span>模型调用上限 model_calls</span><input id="wb-calls" type="number" min="0" max="100" class="input" value="${b.model_calls ?? 8}" /></label>
      <label class="field"><span>最大并行度 parallelism</span><input id="wb-par" type="number" min="1" max="8" class="input" value="${b.parallelism ?? 2}" /></label>
      <label class="field"><span>总耗时上限（秒）</span><input id="wb-wall" type="number" min="1" class="input" value="${b.wall_seconds ?? 86400}" /></label>
    </div>
    <label class="field"><span>上下文字符预算 context_chars</span><input id="wb-ctx" type="number" min="500" max="65536" class="input" value="${b.context_chars ?? 16000}" /></label>
  `;
  const syncBudget = () => {
    wf.project_notes = box.querySelector("#wb-notes").value;
    wf.budget = {
      steps: parseInt(box.querySelector("#wb-steps").value, 10) || 24,
      model_calls: parseInt(box.querySelector("#wb-calls").value, 10) || 0,
      parallelism: parseInt(box.querySelector("#wb-par").value, 10) || 2,
      wall_seconds: parseInt(box.querySelector("#wb-wall").value, 10) || 86400,
      context_chars: parseInt(box.querySelector("#wb-ctx").value, 10) || 16000,
    };
    validateCurrentWorkflow();
  };
  box.querySelectorAll("input, textarea").forEach((el) => el.addEventListener("change", syncBudget));
}

async function saveCurrentWorkflow() {
  if (!state.activeWfId) return;
  try {
    const payload =
      state.wfMode === "yaml"
        ? { yaml: document.getElementById("wf-yaml-editor").value, expected_revision: state.activeWfRevision }
        : { workflow: state.activeWfData, expected_revision: state.activeWfRevision };
    const saved = await api("PUT", `/api/workflows/${state.activeWfId}`, payload);
    state.activeWfRevision = saved.revision;
    state.activeWfSource = saved.source;
    state.activeWfData = saved.workflow;
    state.activeWfYaml = saved.yaml;
    document.getElementById("wf-yaml-editor").value = saved.yaml;
    toast(`工作流 '${saved.id}' 已保存并持久化到 YAML`, "ok");
    await loadWorkflows(false);
    renderWorkflowCanvas(saved.graph, false);
    renderWorkflowValidation({ valid: true, errors: [], warnings: saved.graph.warnings || [] });
  } catch (err) {
    if (err.status === 409) {
      toast("检测到磁盘配置已被其他页面或进程修改，请刷新重新加载后再保存。", "warning");
    } else {
      toast(`保存失败：${err.message}`, "error");
    }
  }
}

/* ---------- Agent Library ---------- */
async function loadAgents() {
  try {
    const res = await api("GET", "/api/agents");
    state.agents = res.agents || [];
    state.agentTemplates = res.templates || [];
    state.agentsRevision = res.revision;
    renderAgentTemplates();
    renderAgentsList();
    if (state.selectedAgentId) {
      const found = state.agents.find((a) => a.id === state.selectedAgentId);
      renderAgentEditor(found || null, false);
    }
  } catch (err) {
    toast(`加载 Agent 列表失败：${err.message}`, "error");
  }
}

function renderAgentTemplates() {
  const row = document.getElementById("agent-templates-row");
  row.innerHTML = state.agentTemplates
    .map(
      (t) =>
        `<button class="chip" data-agent-tpl="${esc(t.id)}">+ ${esc(t.name)} (${esc(t.backend)})</button>`
    )
    .join("");
  row.querySelectorAll("[data-agent-tpl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tpl = state.agentTemplates.find((t) => t.id === btn.dataset.agentTpl);
      if (!tpl) return;
      let newId = `${tpl.id}_custom`;
      let i = 2;
      while (state.agents.some((a) => a.id === newId)) newId = `${tpl.id}_${i++}`;
      renderAgentEditor({ ...tpl, id: newId, name: `${tpl.name} (自定义)` }, true);
    });
  });
}

function renderAgentsList() {
  const box = document.getElementById("agents-list");
  if (!state.agents.length) {
    box.innerHTML = '<div class="empty-state">暂无 Agent 配置</div>';
    return;
  }
  box.innerHTML = state.agents
    .map(
      (a) => `
      <div class="list-item ${a.id === state.selectedAgentId ? "selected" : ""}" data-agent-id="${esc(a.id)}">
        <div class="list-item-top">
          <span class="list-item-title">${esc(a.name)}</span>
          <span class="${pillClass(a.backend)}">${esc(a.backend)}</span>
        </div>
        <div class="list-item-sub">
          <span class="mono">${esc(a.id)}</span>
          <span>职责: ${esc(a.role)}</span>
          <span>${a.write_paths?.length ? `可写: ${esc(a.write_paths.join(", "))}` : "只读"}</span>
        </div>
      </div>`
    )
    .join("");
  box.querySelectorAll("[data-agent-id]").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedAgentId = el.dataset.agentId;
      renderAgentsList();
      const found = state.agents.find((a) => a.id === state.selectedAgentId);
      renderAgentEditor(found, false);
    });
  });
}

function renderAgentEditor(agent, isNew = false) {
  const panel = document.getElementById("agent-editor-panel");
  if (!agent) {
    panel.innerHTML = '<div class="empty-state">请选择左侧 Agent 或点击“创建 Agent”</div>';
    return;
  }
  panel.innerHTML = `
    <div class="card-header">
      <h2>${isNew ? "创建新 Agent" : `编辑 Agent：${esc(agent.name)}`}</h2>
      ${!isNew ? `<button class="btn btn-danger-ghost btn-sm" id="btn-agent-delete">删除 Agent</button>` : ""}
    </div>
    <form id="agent-form" class="form-stack">
      <div class="grid-2">
        <label class="field">
          <span>Agent ID（小写字母/数字/下划线）</span>
          <input id="ag-id" class="input mono" value="${esc(agent.id || "")}" ${isNew ? "" : "readonly"} required />
        </label>
        <label class="field">
          <span>显示名称</span>
          <input id="ag-name" class="input" value="${esc(agent.name || "")}" required />
        </label>
      </div>
      <div class="grid-2">
        <label class="field">
          <span>逻辑职责 Role</span>
          <input id="ag-role" class="input" value="${esc(agent.role || "")}" required />
        </label>
        <label class="field">
          <span>模型后端 Backend</span>
          <select id="ag-backend" class="input">
            <option value="codex" ${agent.backend === "codex" ? "selected" : ""}>codex (ChatGPT CLI)</option>
            <option value="google" ${agent.backend === "google" ? "selected" : ""}>google (Antigravity / 交接)</option>
          </select>
        </label>
      </div>
      <div class="grid-2">
        <label class="field">
          <span>模型名覆盖（可选）</span>
          <input id="ag-model" class="input mono" value="${esc(agent.model || "")}" placeholder="默认由 CLI 账号决定" />
        </label>
        <label class="field">
          <span>不可用策略</span>
          <select id="ag-unavail" class="input">
            <option value="stop" ${agent.on_unavailable === "stop" ? "selected" : ""}>stop</option>
            <option value="handoff" ${agent.on_unavailable === "handoff" ? "selected" : ""}>handoff（推荐用于 Google）</option>
          </select>
        </label>
      </div>
      <label class="field">
        <span>系统提示词 Instructions</span>
        <textarea id="ag-instructions" class="input" rows="4" required>${esc(agent.instructions || "")}</textarea>
      </label>
      <label class="field">
        <span>允许写入的相对路径（逗号分隔；留空为只读角色）</span>
        <input id="ag-write-paths" class="input mono" value="${esc((agent.write_paths || []).join(", "))}" placeholder="solution.py, tests/test_solution.py" />
      </label>
      <div class="grid-2">
        <label class="field"><span>默认工作区 space</span><input id="ag-space" class="input mono" value="${esc(agent.default_space || "main")}" /></label>
        <label class="field"><span>默认超时（秒）</span><input id="ag-timeout" type="number" min="1" max="3600" class="input" value="${agent.default_timeout ?? 120}" /></label>
      </div>
      <div class="grid-2">
        <label class="field"><span>默认重试次数</span><input id="ag-retries" type="number" min="0" max="5" class="input" value="${agent.default_retries ?? 0}" /></label>
        <label class="field">
          <span>会话续接（仅 Codex 支持）</span>
          <select id="ag-reuse" class="input">
            <option value="false" ${!agent.reuse_session ? "selected" : ""}>临时会话（默认）</option>
            <option value="true" ${agent.reuse_session ? "selected" : ""}>同运行内续接会话</option>
          </select>
        </label>
      </div>
      <label class="field">
        <span>角色说明</span>
        <input id="ag-desc" class="input" value="${esc(agent.description || "")}" />
      </label>
      <div class="form-actions">
        <button type="submit" class="btn btn-primary">${isNew ? "创建并保存" : "保存修改"}</button>
      </div>
    </form>
  `;

  panel.querySelector("#agent-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const backend = panel.querySelector("#ag-backend").value;
    const profile = {
      id: panel.querySelector("#ag-id").value.trim(),
      name: panel.querySelector("#ag-name").value.trim(),
      role: panel.querySelector("#ag-role").value.trim(),
      backend,
      instructions: panel.querySelector("#ag-instructions").value.trim(),
      model: panel.querySelector("#ag-model").value.trim() || null,
      write_paths: panel.querySelector("#ag-write-paths").value.split(",").map((s) => s.trim()).filter(Boolean),
      reuse_session: backend === "codex" && panel.querySelector("#ag-reuse").value === "true",
      on_unavailable: panel.querySelector("#ag-unavail").value,
      default_space: panel.querySelector("#ag-space").value.trim() || "main",
      default_timeout: parseInt(panel.querySelector("#ag-timeout").value, 10) || 120,
      default_retries: parseInt(panel.querySelector("#ag-retries").value, 10) || 0,
      template: agent.template || "",
      description: panel.querySelector("#ag-desc").value.trim(),
    };
    try {
      if (isNew) {
        await api("POST", "/api/agents", { profile, expected_revision: state.agentsRevision });
        toast(`已创建 Agent '${profile.id}'`, "ok");
      } else {
        await api("PUT", `/api/agents/${profile.id}`, { profile, expected_revision: state.agentsRevision });
        toast(`已更新 Agent '${profile.id}'`, "ok");
      }
      state.selectedAgentId = profile.id;
      await loadAgents();
    } catch (err) {
      toast(`保存失败：${err.message}`, "error");
    }
  });

  panel.querySelector("#btn-agent-delete")?.addEventListener("click", async () => {
    if (!confirm(`确定删除 Agent '${agent.id}'？`)) return;
    try {
      await api("DELETE", `/api/agents/${agent.id}?expected_revision=${encodeURIComponent(state.agentsRevision || "")}`);
      toast(`已删除 Agent '${agent.id}'`, "info");
      state.selectedAgentId = null;
      await loadAgents();
      renderAgentEditor(null, false);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

/* ---------- Logs ---------- */
async function loadLogs() {
  const cat = document.getElementById("log-category").value;
  const lvl = document.getElementById("log-level").value;
  const rid = document.getElementById("log-run-id").value.trim();
  const q = document.getElementById("log-search").value.trim();
  const params = new URLSearchParams({ category: cat, level: lvl, limit: "200" });
  if (rid) params.set("run_id", rid);
  if (q) params.set("q", q);
  try {
    const res = await api("GET", `/api/logs?${params.toString()}`);
    state.logs = res.logs || [];
    const box = document.getElementById("logs-table");
    if (!state.logs.length) {
      box.innerHTML = '<div class="empty-state">当前筛选条件下暂无日志</div>';
      return;
    }
    box.innerHTML = state.logs
      .map(
        (l) => `
        <div class="log-entry">
          <div class="log-entry-head">
            <span class="${pillClass(l.level)}">${esc(l.level)}</span>
            <span class="pill pill-muted">${esc(l.category)}</span>
            <span class="log-time">${fmtTime(l.ts)}</span>
            ${l.run_id ? `<button class="btn btn-link mono" data-log-run="${esc(l.run_id)}">${esc(l.run_id.slice(0, 8))}</button>` : ""}
            <span>${esc(l.message)}</span>
          </div>
          ${l.detail && Object.keys(l.detail).length ? `<pre class="pre-box mono" style="margin-top:6px;max-height:140px;">${esc(JSON.stringify(l.detail, null, 2))}</pre>` : ""}
        </div>`
      )
      .join("");
    box.querySelectorAll("[data-log-run]").forEach((btn) => {
      btn.addEventListener("click", () => openRunInTaskCenter(btn.dataset.logRun));
    });
  } catch (err) {
    toast(`加载日志失败：${err.message}`, "error");
  }
}

/* ---------- Settings & Service Shutdown ---------- */
async function loadSettings() {
  try {
    const [cfgRes, health] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/health")]);
    state.serverConfig = cfgRes.config;
    state.serverConfigRevision = cfgRes.revision;
    document.getElementById("cfg-host").value = cfgRes.config.host;
    document.getElementById("cfg-port").value = cfgRes.config.port;
    document.getElementById("cfg-concurrency").value = cfgRes.config.max_concurrent_runs;
    document.getElementById("cfg-default-project").value = cfgRes.config.default_project || "";
    document.getElementById("settings-runtime-info").innerHTML = `
      <div class="kv-row"><span>服务状态</span><span class="pill pill-ok">运行中</span></div>
      <div class="kv-row"><span>版本</span><span class="mono">${esc(health.version)}</span></div>
      <div class="kv-row"><span>进程 PID</span><span class="mono">${esc(health.pid)}</span></div>
      <div class="kv-row"><span>监听地址</span><span class="mono">http://${esc(health.host)}:${esc(health.port)}</span></div>
      <div class="kv-row"><span>数据目录</span><span class="mono">${esc(health.data_dir)}</span></div>
      <div class="kv-row"><span>活跃 / 排队任务数</span><strong>${esc(health.active_runs)}</strong></div>
      <div class="kv-row"><span>已运行时长</span><span>${fmtDuration(health.uptime_seconds)}</span></div>
    `;
  } catch (err) {
    toast(`读取系统设置失败：${err.message}`, "error");
  }
}

async function openStopServiceModal() {
  let health;
  try {
    health = await api("GET", "/api/health");
  } catch (err) {
    toast(`无法读取服务状态：${err.message}`, "error");
    return;
  }
  const activeCount = health.active_runs || 0;
  openModal({
    title: "停止整个 AIsys 本地服务",
    bodyHtml: `
      <p>此操作将停止 AIsys Web 后端服务（PID <code>${esc(health.pid)}</code>，端口 <code>${esc(health.port)}</code>）。若仅需停止单个工作流，请在“任务中心”中对该任务点击“取消任务”。</p>
      ${
        activeCount > 0
          ? `<div class="banner banner-warning">
               <strong>注意：当前仍有 ${activeCount} 个运行中或排队中的任务！</strong><br/>
               默认仅关闭 Web 控制台，已启动的独立任务子进程会保留到检查点；勾选下方选项可同时向活跃任务发送取消请求。
             </div>
             <label class="inline-row" style="margin-top:6px;">
               <input type="checkbox" id="chk-cancel-active" />
               <span>同时取消并终止当前运行中的任务</span>
             </label>`
          : '<div class="banner banner-ok">当前没有活跃运行中的任务，可安全停止服务。</div>'
      }
    `,
    footerHtml: `
      <button class="btn btn-secondary" id="stop-svc-cancel">暂不停止</button>
      <button class="btn btn-danger" id="stop-svc-confirm">⏻ 确认停止 AIsys 服务</button>
    `,
    onMount: (dialog) => {
      dialog.querySelector("#stop-svc-cancel").addEventListener("click", closeModal);
      dialog.querySelector("#stop-svc-confirm").addEventListener("click", async () => {
        const cancelActive = Boolean(dialog.querySelector("#chk-cancel-active")?.checked);
        try {
          await api("POST", "/api/server/shutdown", {
            confirm_active: true,
            cancel_active: cancelActive,
          });
          closeModal();
          toast("AIsys 本地服务已停止，可随时再次双击 AIsys.command 启动。", "warning");
        } catch (err) {
          toast(err.message, "error");
        }
      });
    },
  });
}

/* ---------- Event Wiring ---------- */
function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  document.querySelectorAll("[data-jump]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.jump));
  });
  document.getElementById("btn-theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("btn-stop-service").addEventListener("click", openStopServiceModal);
  document.getElementById("btn-settings-stop-service").addEventListener("click", openStopServiceModal);
  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
  });

  ["btn-quick-new-task", "btn-dash-new-task", "btn-tasks-new-task"].forEach((id) => {
    document.getElementById(id).addEventListener("click", () => openNewTaskModal());
  });
  document.getElementById("btn-refresh-dashboard").addEventListener("click", () => loadDashboard(true));
  document.getElementById("btn-refresh-runs").addEventListener("click", () => {
    loadRuns();
    if (state.selectedRunId) loadRunDetail(state.selectedRunId);
  });

  document.querySelectorAll("#run-filter-tabs .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.runFilter = chip.dataset.filter;
      document.querySelectorAll("#run-filter-tabs .chip").forEach((c) => c.classList.toggle("active", c === chip));
      loadRuns(false);
    });
  });

  // Workflow builder controls
  document.getElementById("wf-name-input").addEventListener("change", (e) => {
    if (state.activeWfData) {
      state.activeWfData.name = e.target.value.trim() || "Untitled Workflow";
      validateCurrentWorkflow();
    }
  });
  document.getElementById("btn-mode-visual").addEventListener("click", async () => {
    if (state.wfMode === "visual") return;
    const res = await validateCurrentWorkflow();
    if (res && !res.valid) {
      toast("请先修复 YAML 校验错误再切回可视化画布", "warning");
      return;
    }
    state.wfMode = "visual";
    document.getElementById("btn-mode-visual").classList.add("active");
    document.getElementById("btn-mode-yaml").classList.remove("active");
    document.getElementById("wf-visual-pane").classList.remove("hidden");
    document.getElementById("wf-yaml-pane").classList.add("hidden");
    renderWorkflowCanvas(res?.graph || null, true);
    renderInspector();
  });
  document.getElementById("btn-mode-yaml").addEventListener("click", async () => {
    if (state.wfMode === "yaml") return;
    await validateCurrentWorkflow();
    state.wfMode = "yaml";
    document.getElementById("btn-mode-yaml").classList.add("active");
    document.getElementById("btn-mode-visual").classList.remove("active");
    document.getElementById("wf-visual-pane").classList.add("hidden");
    document.getElementById("wf-yaml-pane").classList.remove("hidden");
  });
  document.getElementById("btn-wf-validate").addEventListener("click", validateCurrentWorkflow);
  document.getElementById("btn-wf-save").addEventListener("click", saveCurrentWorkflow);
  document.getElementById("btn-wf-launch").addEventListener("click", () => openNewTaskModal(state.activeWfId));
  document.getElementById("btn-wf-export").addEventListener("click", () => {
    if (!state.activeWfId) return;
    window.location.href = `/api/workflows/${encodeURIComponent(state.activeWfId)}/export`;
  });
  document.getElementById("btn-wf-delete").addEventListener("click", async () => {
    if (!state.activeWfId) return;
    const label = state.activeWfSource === "preset_override" ? "重置为内置预设" : "删除自定义工作流";
    if (!confirm(`确认对 '${state.activeWfId}' 执行：${label}？`)) return;
    try {
      const res = await api("DELETE", `/api/workflows/${state.activeWfId}`);
      toast(res.action === "reset_override" ? "已恢复为内置默认预设" : `已删除 '${state.activeWfId}'`, "info");
      if (res.action === "deleted") state.activeWfId = null;
      await loadWorkflows(true);
      if (res.action === "reset_override") await selectWorkflow(res.id);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  document.getElementById("btn-wf-duplicate").addEventListener("click", () => {
    if (!state.activeWfId) return;
    openModal({
      title: `复制工作流：${state.activeWfId}`,
      bodyHtml: `
        <label class="field"><span>新工作流 ID（小写字母/数字/下划线）</span><input id="dup-id" class="input mono" value="${esc(state.activeWfId)}_custom" /></label>
        <label class="field"><span>新名称</span><input id="dup-name" class="input" value="${esc(state.activeWfData?.name || "")} (Custom)" /></label>
      `,
      footerHtml: `<button class="btn btn-secondary" id="dup-cancel">取消</button><button class="btn btn-primary" id="dup-ok">创建副本</button>`,
      onMount: (d) => {
        d.querySelector("#dup-cancel").addEventListener("click", closeModal);
        d.querySelector("#dup-ok").addEventListener("click", async () => {
          try {
            const created = await api("POST", `/api/workflows/${state.activeWfId}/duplicate`, {
              new_id: d.querySelector("#dup-id").value.trim(),
              new_name: d.querySelector("#dup-name").value.trim(),
            });
            closeModal();
            toast(`已创建工作流 '${created.id}'`, "ok");
            await loadWorkflows(false);
            await selectWorkflow(created.id);
          } catch (err) {
            toast(err.message, "error");
          }
        });
      },
    });
  });
  document.getElementById("btn-wf-new").addEventListener("click", () => {
    openModal({
      title: "新建空白工作流",
      bodyHtml: `
        <label class="field"><span>工作流 ID</span><input id="new-wf-id" class="input mono" placeholder="my_workflow" /></label>
        <label class="field"><span>名称</span><input id="new-wf-name" class="input" placeholder="My Custom Workflow" /></label>
      `,
      footerHtml: `<button class="btn btn-secondary" id="nwf-cancel">取消</button><button class="btn btn-primary" id="nwf-ok">创建</button>`,
      onMount: (d) => {
        d.querySelector("#nwf-cancel").addEventListener("click", closeModal);
        d.querySelector("#nwf-ok").addEventListener("click", async () => {
          const id = d.querySelector("#new-wf-id").value.trim();
          const name = d.querySelector("#new-wf-name").value.trim() || id;
          try {
            const created = await api("PUT", `/api/workflows/${id}`, {
              workflow: { version: 1, name, start: "report", agents: {}, nodes: { report: { kind: "report" } } },
            });
            closeModal();
            toast(`已创建 '${created.id}'`, "ok");
            await loadWorkflows(false);
            await selectWorkflow(created.id);
          } catch (err) {
            toast(err.message, "error");
          }
        });
      },
    });
  });
  document.getElementById("btn-wf-import").addEventListener("click", () => {
    openModal({
      title: "导入 YAML 工作流",
      bodyHtml: `
        <label class="field"><span>保存为工作流 ID</span><input id="imp-id" class="input mono" placeholder="imported_flow" /></label>
        <label class="field"><span>YAML 内容</span><textarea id="imp-yaml" class="input mono" rows="10" placeholder="version: 1\nname: ..."></textarea></label>
      `,
      footerHtml: `<button class="btn btn-secondary" id="imp-cancel">取消</button><button class="btn btn-primary" id="imp-ok">导入并校验</button>`,
      onMount: (d) => {
        d.querySelector("#imp-cancel").addEventListener("click", closeModal);
        d.querySelector("#imp-ok").addEventListener("click", async () => {
          try {
            const created = await api("POST", "/api/workflows/import", {
              workflow_id: d.querySelector("#imp-id").value.trim(),
              yaml: d.querySelector("#imp-yaml").value,
            });
            closeModal();
            toast(`已导入 '${created.id}'`, "ok");
            await loadWorkflows(false);
            await selectWorkflow(created.id);
          } catch (err) {
            toast(err.message, "error");
          }
        });
      },
    });
  });

  document.querySelectorAll("[data-add-node]").forEach((btn) => {
    btn.addEventListener("click", () => addNodeToWorkflow(btn.dataset.addNode));
  });
  document.getElementById("btn-graph-layout").addEventListener("click", () => {
    if (builderGraph) {
      builderGraph.autoLayout();
      builderGraph.render();
    }
  });
  document.getElementById("btn-graph-zoom-in").addEventListener("click", () => builderGraph?.zoom(0.15));
  document.getElementById("btn-graph-zoom-out").addEventListener("click", () => builderGraph?.zoom(-0.15));
  document.getElementById("btn-graph-zoom-fit").addEventListener("click", () => builderGraph?.fit());

  document.querySelectorAll("[data-inspector]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.wfInspectorTab = btn.dataset.inspector;
      document.querySelectorAll("[data-inspector]").forEach((b) => b.classList.toggle("active", b === btn));
      renderInspector();
    });
  });

  // Agent page
  document.getElementById("btn-new-agent").addEventListener("click", () => {
    state.selectedAgentId = null;
    renderAgentsList();
    renderAgentEditor(
      {
        id: "custom_agent",
        name: "Custom Agent",
        role: "Specialist",
        backend: "codex",
        instructions: "Follow the configured task instructions carefully.",
        write_paths: [],
        reuse_session: false,
        on_unavailable: "stop",
        default_space: "main",
        default_timeout: 120,
        default_retries: 0,
        description: "",
      },
      true
    );
  });

  // Logs page
  document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);
  document.getElementById("btn-apply-log-filter").addEventListener("click", loadLogs);

  // Settings page
  document.getElementById("btn-cfg-validate-project").addEventListener("click", async () => {
    const path = document.getElementById("cfg-default-project").value.trim();
    const hint = document.getElementById("cfg-project-hint");
    if (!path) {
      hint.textContent = "已清空默认项目目录。";
      return;
    }
    try {
      const res = await api("POST", "/api/config/validate-project", { path });
      hint.textContent = `✓ 有效 Git 仓库 (${res.branch} @ ${res.head.slice(0, 8)})`;
      hint.style.color = "var(--success)";
    } catch (err) {
      hint.textContent = `✕ ${err.message}`;
      hint.style.color = "var(--danger)";
    }
  });

  document.getElementById("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const saved = await api("PUT", "/api/config", {
        host: "127.0.0.1",
        port: parseInt(document.getElementById("cfg-port").value, 10) || 8765,
        max_concurrent_runs: parseInt(document.getElementById("cfg-concurrency").value, 10) || 2,
        default_project: document.getElementById("cfg-default-project").value.trim(),
        expected_revision: state.serverConfigRevision,
      });
      state.serverConfigRevision = saved.revision;
      toast("服务设置已保存", "ok");
      loadSettings();
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

/* ---------- Boot ---------- */
initTheme();
bindEvents();
connectSSE();
loadDashboard(false);
refreshNavBadge();
