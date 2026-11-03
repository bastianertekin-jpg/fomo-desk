/* chart.js — tiny canvas line chart (no deps). drawLine(canvas, [[x,y],...], {color}) */
(function () {
  function drawLine(cv, pts, opt) {
    opt = opt || {};
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 300, h = cv.height / (cv._dpr || 1) || 180;
    cv._dpr = dpr; cv.width = w * dpr; cv.height = h * dpr; cv.style.height = h + "px";
    const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    pts = (pts || []).filter(p => p[1] != null && isFinite(p[1]));
    if (pts.length < 2) {
      c.fillStyle = "#8e8e93"; c.font = "13px -apple-system,system-ui"; c.textAlign = "center";
      c.fillText(pts.length ? "Collecting data…" : "No data yet", w / 2, h / 2); return;
    }
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (y0 === y1) { y0 -= Math.abs(y0) * 0.01 || 1; y1 += Math.abs(y1) * 0.01 || 1; }
    const pad = 8, X = x => pad + (x - x0) / ((x1 - x0) || 1) * (w - 2 * pad), Y = y => h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad);
    const up = ys[ys.length - 1] >= ys[0];
    const col = opt.color || (up ? "#30d158" : "#ff453a");
    if (opt.base != null && opt.base >= y0 && opt.base <= y1) {
      c.strokeStyle = "rgba(142,142,147,.35)"; c.setLineDash([3, 4]); c.beginPath();
      c.moveTo(pad, Y(opt.base)); c.lineTo(w - pad, Y(opt.base)); c.stroke(); c.setLineDash([]);
    }
    const g = c.createLinearGradient(0, 0, 0, h); g.addColorStop(0, col + "55"); g.addColorStop(1, col + "00");
    c.beginPath(); pts.forEach((p, i) => i ? c.lineTo(X(p[0]), Y(p[1])) : c.moveTo(X(p[0]), Y(p[1])));
    c.lineWidth = 2; c.strokeStyle = col; c.lineJoin = "round"; c.stroke();
    c.lineTo(X(x1), h); c.lineTo(X(x0), h); c.closePath(); c.fillStyle = g; c.fill();
    const lx = X(xs[xs.length - 1]), ly = Y(ys[ys.length - 1]);
    c.beginPath(); c.arc(lx, ly, 3.5, 0, 7); c.fillStyle = col; c.fill();
  }
  window.DeskChart = { drawLine };
})();
