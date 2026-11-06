/* chart.js — canvas helpers (no deps), devicePixelRatio aware.
   DeskChart.line(canvas, points[[ts,v]], {base, marks, color, animate})  Home LIVE line + gradient fill + dashed start + marks
   DeskChart.candles(canvas, candles[{ts,o,h,l,c,volume}], {exitLine, exitLabel})  Coin 1m OHLC + volume + orange dashed exit line
   DeskChart.ring(canvas, score|null, greenFrom)  Analyst score ring 0-100 */
(function () {
  "use strict";
  const C = { green: "#00d35a", red: "#ff453a", orange: "#ff9f0a", yellow: "#ffd60a", track: "#2c2c2e",
    sec: "rgba(235,235,245,0.6)", ter: "rgba(235,235,245,0.3)", sep: "rgba(84,84,88,0.55)" };
  const TEXT = '-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif';
  const DISPLAY = '"SF Pro Display",-apple-system,BlinkMacSystemFont,system-ui,sans-serif';
  const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setup(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 300, h = cv.clientHeight || 200;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    return { c, w, h };
  }
  function hexA(hex, a) { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`; }
  function anim(cv, key, ms, draw) { // rAF tween 0..1; instant when reduced motion
    if (cv[key]) cancelAnimationFrame(cv[key]);
    if (reduced() || ms <= 0) return draw(1);
    const t0 = performance.now();
    const step = now => { const t = Math.min(1, (now - t0) / ms); draw(1 - Math.pow(1 - t, 3)); if (t < 1) cv[key] = requestAnimationFrame(step); };
    cv[key] = requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- line
  function line(cv, pts, opt) {
    opt = opt || {};
    pts = (pts || []).filter(p => p && p[1] != null && isFinite(p[1]));
    const prev = cv._prev || [];
    const appended = prev.length && pts.length === prev.length + 1 && pts[0][0] === prev[0][0];
    const newMarks = (opt.marks || []).filter(m => !(cv._marks || []).some(o => o.ts === m.ts && o.label === m.label));
    cv._prev = pts; cv._marks = opt.marks || [];
    const go = (t, ma) => drawLine(cv, pts, opt, appended && t < 1 ? { from: prev[prev.length - 1], t } : null, newMarks, ma);
    if (opt.animate && appended) anim(cv, "_raf", 250, t => go(t, t));
    else if (opt.animate && newMarks.length) anim(cv, "_raf", 200, t => go(1, t));
    else go(1, 1);
  }
  function drawLine(cv, pts, opt, slide, newMarks, markAlpha) {
    const { c, w, h } = setup(cv);
    const col = opt.color || C.green, padX = 4, bot = 12, LH = 20, GAP = 4;
    let top = 32;
    const base = opt.base;
    if (pts.length < 2) {
      if (base != null) { c.strokeStyle = C.ter; c.setLineDash([4, 5]); c.lineWidth = 1; c.beginPath(); c.moveTo(0, h / 2); c.lineTo(w, h / 2); c.stroke(); c.setLineDash([]); }
      return;
    }
    const xs = pts.map(p => p[0]); let ys = pts.map(p => p[1]);
    if (base != null) ys = ys.concat([base]);
    const x0 = xs[0], x1 = xs[xs.length - 1];
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (y1 - y0 < Math.abs(y1) * 0.002 || y1 === y0) { const m = Math.abs(y0) * 0.01 || 1; y0 -= m; y1 += m; }
    const X = x => padX + (x - x0) / ((x1 - x0) || 1) * (w - 2 * padX);
    // marks layout first (x only): a later mark that would overlap moves up a row; rows are fixed bands, so no overlap
    c.font = `600 12px ${TEXT}`;
    const marks = (opt.marks || []).filter(m => m.ts >= x0 && m.ts <= x1).sort((a, b) => a.ts - b.ts);
    const rows = [];
    const lay = marks.map(m => {
      const mx = X(m.ts), tw = c.measureText(m.label).width + 14;
      const lx = Math.min(Math.max(0, mx - tw / 2), w - tw); let r = 0;
      while (rows[r] != null && rows[r] + GAP > lx) r++;
      rows[r] = lx + tw; return { m, mx, tw, lx, r };
    });
    if (rows.length) top = 16 + rows.length * (LH + GAP) + 8;
    const Y = y => top + (1 - (y - y0) / (y1 - y0)) * (h - top - bot);
    const P = pts.map(p => [X(p[0]), Y(p[1])]);
    if (slide) { const a = [X(slide.from[0]), Y(slide.from[1])], b = P[P.length - 1]; P[P.length - 1] = [a[0] + (b[0] - a[0]) * slide.t, a[1] + (b[1] - a[1]) * slide.t]; }
    // dashed start line
    if (base != null) { c.strokeStyle = C.ter; c.setLineDash([4, 5]); c.lineWidth = 1; c.beginPath(); c.moveTo(0, Y(base)); c.lineTo(w, Y(base)); c.stroke(); c.setLineDash([]); }
    // soft gradient fill under the line
    const g = c.createLinearGradient(0, top, 0, h); g.addColorStop(0, hexA(col, 0.22)); g.addColorStop(1, hexA(col, 0));
    c.beginPath(); P.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1]));
    c.lineTo(P[P.length - 1][0], h); c.lineTo(P[0][0], h); c.closePath(); c.fillStyle = g; c.fill();
    c.beginPath(); P.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1]));
    c.lineWidth = 2; c.strokeStyle = col; c.lineJoin = "round"; c.lineCap = "round"; c.stroke();
    const L = P[P.length - 1]; c.beginPath(); c.arc(L[0], L[1], 4, 0, 7); c.fillStyle = col; c.fill();
    lay.forEach(({ m, mx, tw, lx, r }) => {
      const my = Y(yAt(pts, m.ts, m.y));
      const ly = top - 8 - LH - r * (LH + GAP);
      c.globalAlpha = newMarks.includes(m) ? markAlpha : 1;
      c.strokeStyle = hexA(m.color || "#8e8e93", 0.5); c.lineWidth = 1;
      c.beginPath(); c.moveTo(mx, ly + LH); c.lineTo(mx, my - 5); c.stroke();
      c.beginPath(); c.arc(mx, my, 3.5, 0, 7); c.fillStyle = m.color || C.sec; c.fill();
      roundRect(c, lx, ly, tw, LH, 10); c.fillStyle = m.color || C.sec; c.fill();
      c.fillStyle = "#000"; c.textBaseline = "middle"; c.textAlign = "center"; c.fillText(m.label, lx + tw / 2, ly + LH / 2 + 0.5);
      c.globalAlpha = 1;
    });
  }
  function yAt(pts, ts, fallback) {
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i][0] <= ts) return pts[i][1];
    return fallback != null ? fallback : pts[0][1];
  }
  function roundRect(c, x, y, w, h, r) {
    c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
  }

  // ---------------------------------------------------------------- candles
  function candles(cv, cs, opt) {
    opt = opt || {};
    const { c, w, h } = setup(cv);
    cs = (cs || []).filter(k => k && isFinite(k.o) && isFinite(k.c));
    if (!cs.length) {
      c.fillStyle = C.sec; c.font = `15px ${TEXT}`; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("No price data yet", w / 2, h / 2); return;
    }
    cs = cs.slice(-Math.max(20, Math.floor(w / 6)));
    const volH = h * 0.2, pTop = 10, pBot = h - volH - 10, padR = 4;
    let lo = Math.min(...cs.map(k => k.l)), hi = Math.max(...cs.map(k => k.h));
    if (opt.exitLine != null && isFinite(opt.exitLine)) { lo = Math.min(lo, opt.exitLine); hi = Math.max(hi, opt.exitLine); }
    if (hi === lo) { hi *= 1.01; lo *= 0.99; if (hi === lo) { hi = 1; lo = 0; } }
    const Y = v => pTop + (1 - (v - lo) / (hi - lo)) * (pBot - pTop);
    const slot = (w - padR) / cs.length, bw = Math.max(1, Math.min(8, slot * 0.7));
    const vmax = Math.max(...cs.map(k => k.volume || 0)) || 0;
    cs.forEach((k, i) => {
      const x = i * slot + slot / 2, up = k.c >= k.o, col = up ? C.green : C.red;
      c.strokeStyle = col; c.fillStyle = col; c.lineWidth = 1;
      c.beginPath(); c.moveTo(x, Y(k.h)); c.lineTo(x, Y(k.l)); c.stroke();
      const yo = Y(k.o), yc = Y(k.c); c.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
      if (vmax > 0 && k.volume > 0) { const vh = (k.volume / vmax) * (volH - 4); c.globalAlpha = 0.45; c.fillRect(x - bw / 2, h - vh, bw, vh); c.globalAlpha = 1; }
    });
    if (opt.exitLine != null && isFinite(opt.exitLine)) {
      const y = Y(opt.exitLine);
      c.strokeStyle = C.orange; c.setLineDash([5, 4]); c.lineWidth = 1.5; c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke(); c.setLineDash([]);
      c.font = `600 12px ${TEXT}`; c.fillStyle = C.orange; c.textAlign = "left"; c.textBaseline = "bottom";
      c.fillText(opt.exitLabel || "Risk trailing stop", 4, y - 3);
    }
  }

  // ---------------------------------------------------------------- score ring
  function ring(cv, score, greenFrom) {
    const { c, w, h } = setup(cv);
    const s = Math.min(w, h), lw = s * 0.09, r = s / 2 - lw / 2 - 1, cx = w / 2, cy = h / 2;
    c.lineWidth = lw; c.lineCap = "round";
    c.strokeStyle = C.track; c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.stroke();
    const has = score != null && isFinite(score);
    const draw = t => {
      setup(cv); c.lineWidth = lw; c.lineCap = "round";
      c.strokeStyle = C.track; c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.stroke();
      if (has) {
        const v = Math.max(0, Math.min(100, score)) * t;
        c.strokeStyle = score >= (greenFrom || 70) ? C.green : C.yellow;
        c.beginPath(); c.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * v / 100); c.stroke();
      }
      c.fillStyle = has ? "#fff" : C.ter; c.textAlign = "center"; c.textBaseline = "middle";
      c.font = `700 ${Math.round(s * 0.3)}px ${DISPLAY}`;
      c.fillText(has ? String(Math.round(score)) : "—", cx, cy);
    };
    if (cv._score === score) return draw(1);
    cv._score = score; anim(cv, "_raf", has ? 600 : 0, draw);
  }

  window.DeskChart = { line, candles, ring, reduced };
})();
