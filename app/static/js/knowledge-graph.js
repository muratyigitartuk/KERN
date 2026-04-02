/**
 * knowledge-graph.js — minimal offline canvas force-directed graph renderer
 * No external dependencies. Works with KERN's offline-first constraint.
 */

const NODE_COLORS = {
  person: "#6366f1",    // indigo
  company: "#22c55e",   // green
  document: "#94a3b8",  // gray
  date: "#f59e0b",      // amber
  amount: "#06b6d4",    // cyan
  default: "#8b5cf6",   // violet
};

export class KnowledgeGraph {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.nodes = [];
    this.links = [];
    this._animating = false;
    this._drag = null;
    this._transform = { x: 0, y: 0, scale: 1 };
    this._bindEvents();
  }

  load(data) {
    const w = this.canvas.width, h = this.canvas.height;
    this.nodes = (data.nodes || []).map((n, i) => ({
      ...n,
      x: w / 2 + (Math.random() - 0.5) * w * 0.6,
      y: h / 2 + (Math.random() - 0.5) * h * 0.6,
      vx: 0,
      vy: 0,
    }));
    const idIndex = Object.fromEntries(this.nodes.map((n, i) => [n.id, i]));
    this.links = (data.links || []).flatMap((l) => {
      const si = idIndex[l.source], ti = idIndex[l.target];
      if (si === undefined || ti === undefined) return [];
      return [{ source: si, target: ti, relationship: l.relationship || "" }];
    });
    this._startSim();
  }

  _startSim() {
    if (this._animating) return;
    this._animating = true;
    let ticks = 0;
    const step = () => {
      this._tick();
      this._draw();
      ticks++;
      if (ticks < 300 || this._drag) {
        requestAnimationFrame(step);
      } else {
        this._animating = false;
      }
    };
    requestAnimationFrame(step);
  }

  _tick() {
    const nodes = this.nodes, links = this.links;
    const alpha = 0.08, repulsion = 3000, spring = 0.04, springLen = 80, damping = 0.85;
    // repulsion
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        const d2 = dx * dx + dy * dy + 1;
        const f = repulsion / d2;
        nodes[i].vx += f * dx;
        nodes[i].vy += f * dy;
        nodes[j].vx -= f * dx;
        nodes[j].vy -= f * dy;
      }
    }
    // spring
    for (const l of links) {
      const s = nodes[l.source], t = nodes[l.target];
      const dx = t.x - s.x, dy = t.y - s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = spring * (d - springLen);
      s.vx += f * dx / d;
      s.vy += f * dy / d;
      t.vx -= f * dx / d;
      t.vy -= f * dy / d;
    }
    // gravity toward center
    const cx = this.canvas.width / 2, cy = this.canvas.height / 2;
    for (const n of nodes) {
      n.vx += (cx - n.x) * 0.005;
      n.vy += (cy - n.y) * 0.005;
      n.vx *= damping;
      n.vy *= damping;
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
    }
  }

  _draw() {
    const ctx = this.ctx, w = this.canvas.width, h = this.canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(this._transform.x, this._transform.y);
    ctx.scale(this._transform.scale, this._transform.scale);

    // links
    ctx.lineWidth = 1;
    for (const l of this.links) {
      const s = this.nodes[l.source], t = this.nodes[l.target];
      ctx.beginPath();
      ctx.strokeStyle = "rgba(148,163,184,0.35)";
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      ctx.stroke();
    }

    // nodes
    for (const n of this.nodes) {
      const color = NODE_COLORS[n.type] || NODE_COLORS.default;
      ctx.beginPath();
      ctx.arc(n.x, n.y, 7, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.4)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      // label
      ctx.fillStyle = "rgba(226,232,240,0.9)";
      ctx.font = "10px monospace";
      ctx.fillText(n.name.slice(0, 18), n.x + 9, n.y + 4);
    }
    ctx.restore();
  }

  _bindEvents() {
    const canvas = this.canvas;
    let panning = false, panStart = { x: 0, y: 0 };

    canvas.addEventListener("mousedown", (e) => {
      const pos = this._canvasPos(e);
      const node = this._hitTest(pos);
      if (node) {
        this._drag = node;
        this._startSim();
      } else {
        panning = true;
        panStart = { x: e.clientX - this._transform.x, y: e.clientY - this._transform.y };
      }
    });

    canvas.addEventListener("mousemove", (e) => {
      if (this._drag) {
        const pos = this._canvasPos(e);
        this._drag.x = pos.x;
        this._drag.y = pos.y;
        this._drag.vx = 0;
        this._drag.vy = 0;
      } else if (panning) {
        this._transform.x = e.clientX - panStart.x;
        this._transform.y = e.clientY - panStart.y;
        this._draw();
      }
    });

    canvas.addEventListener("mouseup", () => {
      this._drag = null;
      panning = false;
    });

    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.1 : 0.9;
      this._transform.scale = Math.max(0.2, Math.min(4, this._transform.scale * factor));
      this._draw();
    }, { passive: false });
  }

  _canvasPos(e) {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left - this._transform.x) / this._transform.scale,
      y: (e.clientY - rect.top - this._transform.y) / this._transform.scale,
    };
  }

  _hitTest(pos) {
    for (const n of this.nodes) {
      const dx = n.x - pos.x, dy = n.y - pos.y;
      if (dx * dx + dy * dy < 100) return n;
    }
    return null;
  }
}
