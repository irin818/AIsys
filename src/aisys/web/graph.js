/**
 * Interactive SVG + DOM Workflow Graph Renderer & Editor.
 * Supports layered auto-layout, drag-to-move, pan/zoom, drag-to-connect,
 * and live execution status highlighting with zero external dependencies.
 */

const NODE_W = 210;
const NODE_H = 82;
const END_W = 96;
const END_H = 38;
const COL_GAP = 86;
const ROW_GAP = 42;

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export class WorkflowGraphCanvas {
  constructor(container, options = {}) {
    this.container = container;
    this.interactive = options.interactive !== false;
    this.onSelect = options.onSelect || (() => {});
    this.onConnect = options.onConnect || (() => {});
    this.positions = new Map();
    this.selectedId = null;
    this.graph = null;
    this.liveStatus = null;
    this.scale = 1;
    this.tx = 28;
    this.ty = 28;
    this._draggingNode = null;
    this._panning = null;
    this._connecting = null;
    this._boundMove = (e) => this._onPointerMove(e);
    this._boundUp = (e) => this._onPointerUp(e);
    window.addEventListener("pointermove", this._boundMove);
    window.addEventListener("pointerup", this._boundUp);
    this.container.addEventListener("pointerdown", (e) => this._onBackgroundDown(e));
  }

  destroy() {
    window.removeEventListener("pointermove", this._boundMove);
    window.removeEventListener("pointerup", this._boundUp);
  }

  setGraph(graph, { selectedId = null, resetLayout = false, liveStatus = null } = {}) {
    this.graph = graph;
    this.selectedId = selectedId;
    this.liveStatus = liveStatus;
    if (resetLayout || !this._positionsMatch(graph)) {
      this.autoLayout();
    }
    this.render();
  }

  _positionsMatch(graph) {
    if (!graph) return false;
    for (const node of graph.nodes) {
      if (!this.positions.has(node.id)) return false;
    }
    return true;
  }

  autoLayout() {
    if (!this.graph) return;
    const nodes = this.graph.nodes || [];
    const edges = this.graph.edges || [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const adj = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) {
      if (adj.has(e.source) && byId.has(e.target)) {
        adj.get(e.source).push(e.target);
      }
    }

    const startNode = nodes.find((n) => n.is_start) || nodes[0];
    const depth = new Map();
    if (startNode) {
      const queue = [[startNode.id, 0]];
      depth.set(startNode.id, 0);
      while (queue.length) {
        const [cur, d] = queue.shift();
        for (const nxt of adj.get(cur) || []) {
          if (!depth.has(nxt)) {
            depth.set(nxt, d + 1);
            queue.push([nxt, d + 1]);
          }
        }
      }
    }
    let maxD = 0;
    for (const v of depth.values()) maxD = Math.max(maxD, v);
    for (const n of nodes) {
      if (!depth.has(n.id)) depth.set(n.id, ++maxD);
    }

    const cols = new Map();
    for (const n of nodes) {
      const c = depth.get(n.id) ?? 0;
      if (!cols.has(c)) cols.set(c, []);
      cols.get(c).push(n.id);
    }

    this.positions.clear();
    let maxCol = 0;
    for (const [col, ids] of cols.entries()) {
      maxCol = Math.max(maxCol, col);
      ids.forEach((id, row) => {
        this.positions.set(id, {
          x: col * (NODE_W + COL_GAP),
          y: row * (NODE_H + ROW_GAP),
        });
      });
    }
    this.positions.set("end", {
      x: (maxCol + 1) * (NODE_W + COL_GAP),
      y: 22,
    });
    this.fit();
  }

  zoom(delta) {
    this.scale = Math.max(0.45, Math.min(1.6, +(this.scale + delta).toFixed(2)));
    this._applyTransform();
  }

  fit() {
    if (!this.positions.size) return;
    let maxX = 0;
    let maxY = 0;
    for (const [id, p] of this.positions.entries()) {
      const w = id === "end" ? END_W : NODE_W;
      const h = id === "end" ? END_H : NODE_H;
      maxX = Math.max(maxX, p.x + w);
      maxY = Math.max(maxY, p.y + h);
    }
    const cw = Math.max(320, this.container.clientWidth || 720);
    const ch = Math.max(240, this.container.clientHeight || 480);
    const pad = 44;
    const scaleX = (cw - pad * 2) / Math.max(1, maxX);
    const scaleY = (ch - pad * 2) / Math.max(1, maxY);
    this.scale = Math.max(0.5, Math.min(1.0, Math.min(scaleX, scaleY)));
    this.tx = pad;
    this.ty = Math.max(pad, (ch - maxY * this.scale) / 2);
    this._applyTransform();
  }

  _applyTransform() {
    const vp = this.container.querySelector(".graph-viewport");
    if (vp) {
      vp.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
    }
  }

  render() {
    if (!this.graph) {
      this.container.innerHTML = '<div class="empty-state">暂无可显示的工作流图</div>';
      return;
    }

    const nodes = this.graph.nodes || [];
    const edges = this.graph.edges || [];
    const hasEndEdge = edges.some((e) => e.target === "end");

    const svgEdges = [];
    for (const e of edges) {
      const sp = this.positions.get(e.source);
      const tp = this.positions.get(e.target);
      if (!sp || !tp) continue;
      const sx = sp.x + NODE_W;
      const sy = sp.y + NODE_H / 2;
      const tx = tp.x;
      const ty = tp.y + (e.target === "end" ? END_H / 2 : NODE_H / 2);
      const isBack = tx <= sp.x + 12;

      let d;
      let lx;
      let ly;
      if (isBack) {
        const arcY = Math.max(sp.y, tp.y) + NODE_H + 46;
        d = `M ${sp.x + NODE_W / 2} ${sp.y + NODE_H} C ${sp.x + NODE_W / 2} ${arcY}, ${tp.x + NODE_W / 2} ${arcY}, ${tp.x + NODE_W / 2} ${tp.y + NODE_H}`;
        lx = (sp.x + tp.x + NODE_W) / 2;
        ly = arcY - 8;
      } else {
        const dx = Math.max(40, (tx - sx) * 0.5);
        d = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
        lx = (sx + tx) / 2;
        ly = (sy + ty) / 2 - 8;
      }

      const color =
        e.kind === "route"
          ? "#fbbf24"
          : e.kind === "parallel"
          ? "#2dd4bf"
          : "#38bdf8";
      const dash = e.kind === "route" ? 'stroke-dasharray="6 4"' : "";
      const marker =
        e.kind === "route"
          ? "url(#arrow-route)"
          : e.kind === "parallel"
          ? "url(#arrow-parallel)"
          : "url(#arrow-next)";

      svgEdges.push(`
        <path d="${d}" fill="none" stroke="${color}" stroke-width="2" ${dash} marker-end="${marker}" opacity="0.88" />
        <g transform="translate(${lx}, ${ly})">
          <rect x="-44" y="-10" width="88" height="18" rx="9" fill="var(--bg-card)" stroke="${color}" stroke-width="1" opacity="0.95" />
          <text x="0" y="3" text-anchor="middle" font-size="10" font-weight="600" fill="${color}">${escapeHtml(e.label.slice(0, 16))}</text>
        </g>
      `);
    }

    const nodeCards = nodes.map((n) => {
      const pos = this.positions.get(n.id) || { x: 0, y: 0 };
      const selected = n.id === this.selectedId ? "selected" : "";
      const backendCls = n.backend ? `backend-${n.backend}` : "";
      let liveCls = "";
      let liveBadge = "";
      if (this.liveStatus) {
        const st = this.liveStatus.nodes?.[n.id];
        if (st === "running") {
          liveCls = "live-running";
          liveBadge = '<span class="pill pill-running">运行中</span>';
        } else if (st === "waiting") {
          liveCls = "live-waiting";
          liveBadge = '<span class="pill pill-waiting">等待中</span>';
        } else if (st === "ok") {
          liveCls = "live-ok";
          liveBadge = '<span class="pill pill-ok">完成</span>';
        } else if (st === "failed") {
          liveCls = "live-failed";
          liveBadge = '<span class="pill pill-failed">失败</span>';
        }
      }
      const startBadge = n.is_start ? '<span class="pill pill-info">START</span>' : "";
      const roleTag = n.agent_role
        ? `<span>${escapeHtml(n.agent_role)} · ${escapeHtml(n.backend)}</span>`
        : `<span>${escapeHtml(n.kind)}</span>`;
      const visitsTag = n.max_visits > 1 ? `<span>≤${n.max_visits}轮</span>` : "";
      const port = this.interactive
        ? `<div class="graph-port" data-port-source="${escapeHtml(n.id)}" title="拖向另一节点连接 next"></div>`
        : "";
      return `
        <div class="graph-node kind-${escapeHtml(n.kind)} ${backendCls} ${selected} ${liveCls}"
             data-node-id="${escapeHtml(n.id)}"
             style="left:${pos.x}px; top:${pos.y}px;">
          <div class="graph-node-header">
            <span class="graph-node-id">${escapeHtml(n.id)}</span>
            ${liveBadge || startBadge}
          </div>
          <div class="graph-node-meta">
            ${roleTag}
            ${visitsTag}
            ${n.space && n.space !== "main" ? `<span>space:${escapeHtml(n.space)}</span>` : ""}
          </div>
          ${port}
        </div>
      `;
    });

    const endPos = this.positions.get("end") || { x: 640, y: 22 };
    const endHtml = hasEndEdge
      ? `<div class="graph-end-node" data-node-id="end" style="left:${endPos.x}px; top:${endPos.y}px;">END</div>`
      : "";

    this.container.innerHTML = `
      <div class="graph-viewport" style="transform:translate(${this.tx}px, ${this.ty}px) scale(${this.scale});">
        <svg class="graph-svg" width="4000" height="3000">
          <defs>
            <marker id="arrow-next" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
            </marker>
            <marker id="arrow-route" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#fbbf24" />
            </marker>
            <marker id="arrow-parallel" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#2dd4bf" />
            </marker>
          </defs>
          ${svgEdges.join("")}
          <path id="graph-preview-edge" d="" fill="none" stroke="#38bdf8" stroke-width="2" stroke-dasharray="4 4" />
        </svg>
        ${nodeCards.join("")}
        ${endHtml}
      </div>
    `;

    this.container.querySelectorAll(".graph-node").forEach((el) => {
      el.addEventListener("pointerdown", (e) => this._onNodeDown(e, el.dataset.nodeId));
    });
    this.container.querySelectorAll(".graph-port").forEach((el) => {
      el.addEventListener("pointerdown", (e) => this._onPortDown(e, el.dataset.portSource));
    });
  }

  _clientToCanvas(clientX, clientY) {
    const rect = this.container.getBoundingClientRect();
    return {
      x: (clientX - rect.left - this.tx) / this.scale,
      y: (clientY - rect.top - this.ty) / this.scale,
    };
  }

  _onBackgroundDown(e) {
    if (e.target.closest(".graph-node") || e.target.closest(".graph-port")) return;
    this._panning = { startX: e.clientX, startY: e.clientY, origTx: this.tx, origTy: this.ty };
  }

  _onNodeDown(e, nodeId) {
    if (e.target.closest(".graph-port")) return;
    e.stopPropagation();
    this.selectedId = nodeId;
    this.onSelect(nodeId);
    const pos = this.positions.get(nodeId) || { x: 0, y: 0 };
    if (this.interactive) {
      this._draggingNode = {
        nodeId,
        startX: e.clientX,
        startY: e.clientY,
        origX: pos.x,
        origY: pos.y,
        moved: false,
      };
    }
    this.render();
  }

  _onPortDown(e, sourceId) {
    if (!this.interactive) return;
    e.stopPropagation();
    const sp = this.positions.get(sourceId);
    if (!sp) return;
    this._connecting = {
      sourceId,
      sx: sp.x + NODE_W,
      sy: sp.y + NODE_H / 2,
    };
  }

  _onPointerMove(e) {
    if (this._panning) {
      this.tx = this._panning.origTx + (e.clientX - this._panning.startX);
      this.ty = this._panning.origTy + (e.clientY - this._panning.startY);
      this._applyTransform();
      return;
    }
    if (this._draggingNode) {
      const dx = (e.clientX - this._draggingNode.startX) / this.scale;
      const dy = (e.clientY - this._draggingNode.startY) / this.scale;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) this._draggingNode.moved = true;
      this.positions.set(this._draggingNode.nodeId, {
        x: Math.round(this._draggingNode.origX + dx),
        y: Math.round(this._draggingNode.origY + dy),
      });
      this.render();
      return;
    }
    if (this._connecting) {
      const pt = this._clientToCanvas(e.clientX, e.clientY);
      const preview = this.container.querySelector("#graph-preview-edge");
      if (preview) {
        const { sx, sy } = this._connecting;
        const dx = Math.max(30, Math.abs(pt.x - sx) * 0.45);
        preview.setAttribute("d", `M ${sx} ${sy} C ${sx + dx} ${sy}, ${pt.x - dx} ${pt.y}, ${pt.x} ${pt.y}`);
      }
    }
  }

  _onPointerUp(e) {
    if (this._panning) {
      this._panning = null;
      return;
    }
    if (this._draggingNode) {
      this._draggingNode = null;
      return;
    }
    if (this._connecting) {
      const sourceId = this._connecting.sourceId;
      this._connecting = null;
      const preview = this.container.querySelector("#graph-preview-edge");
      if (preview) preview.setAttribute("d", "");
      const targetEl = document.elementFromPoint(e.clientX, e.clientY)?.closest("[data-node-id]");
      const targetId = targetEl?.dataset?.nodeId;
      if (targetId && targetId !== sourceId) {
        this.onConnect(sourceId, targetId);
      }
    }
  }
}
