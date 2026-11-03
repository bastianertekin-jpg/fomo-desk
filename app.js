/* app.js — Plan B client. Opens as {URL}#d={DATA_PATH}&t={APP_TOKEN}; polls ./d/{DATA_PATH}/*.json every 15s.
   No SSE. All FOMO links come pre-built from the JSON (server links.py) — never built here. */
(function () {
  "use strict";
  const POLL_MS = 15000, LS_D = "desk.d", LS_T = "desk.t", SALT = "fomo-desk-gate:";
  const $ = s => document.querySelector(s);
  let D = null, T = null, state = null, chart = null, range = 86400, openMint = null;

  // ---- credentials from hash -> localStorage, then strip hash from the URL bar
  const hp = new URLSearchParams(location.hash.replace(/^#/, ""));
  if (hp.get("d")) localStorage.setItem(LS_D, hp.get("d"));
  if (hp.get("t")) localStorage.setItem(LS_T, hp.get("t"));
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  D = localStorage.getItem(LS_D); T = localStorage.getItem(LS_T);
  if (!D || !/^[A-Za-z0-9_-]{16,128}$/.test(D)) return lock();

  const base = "d/" + encodeURIComponent(D) + "/";
  const get = p => fetch(base + p + "?_=" + Date.now(), { cache: "no-store", credentials: "omit" })
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

  function lock() { $("#app").classList.add("hidden"); $("#locked").classList.remove("hidden"); }
  async function sha256(s) {
    const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
    return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join("");
  }
  async function gateOk(st) { // soft gate: only enforced when server publishes a gate hash
    if (!st.gate) return true;
    if (!T || !crypto.subtle) return false;
    return (await sha256(SALT + T)) === st.gate;
  }

  // ---- formatting
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sol = (v, d = 4) => v == null ? "—" : (+v).toFixed(d) + " SOL";
  const usd = v => v == null ? "" : "$" + (+v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const pct = v => v == null ? "" : (v >= 0 ? "+" : "") + (+v).toFixed(2) + "%";
  const cls = v => v == null ? "" : v >= 0 ? "up" : "down";
  const ago = ts => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d"; };
  const short = m => m ? m.slice(0, 4) + "…" + m.slice(-4) : "";
  const safeUrl = u => /^https:\/\/fomo\.family\//.test(u || "") ? u : null;

  // ---- render
  function renderHome(st) {
    const b = st.balance || {};
    $("#equity").textContent = sol(b.equity_sol, 3);
    $("#equity-usd").textContent = usd(b.equity_usd);
    $("#pnl").innerHTML = `<span class="${cls(b.pnl_sol)}">${b.pnl_sol >= 0 ? "+" : ""}${(+b.pnl_sol || 0).toFixed(4)} SOL (${pct(b.pnl_pct)})</span> <span class="muted small">all time · today ${(+b.today_sol || 0).toFixed(4)}</span>`;
    const pos = st.positions || [];
    $("#positions").innerHTML = pos.length ? pos.map(p => `
      <button class="row chev" data-mint="${esc(p.mint)}"><div class="main"><div class="t">${esc(p.symbol || short(p.mint))}</div>
      <div class="s">${sol(p.value_sol)} · opened ${ago(p.opened_ts)} ago</div></div>
      <div class="v ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</div></button>`).join("") : `<div class="empty">No open positions</div>`;
    const coins = st.coins || [];
    $("#coins").innerHTML = coins.length ? coins.slice(0, 15).map(c => `
      <button class="row chev" data-mint="${esc(c.mint)}"><div class="main"><div class="t">${esc(c.symbol || short(c.mint))}</div>
      <div class="s">${esc(c.status)} · ${esc(c.source || "")} · ${ago(c.first_seen)} ago</div></div></button>`).join("") : `<div class="empty">Scanner warming up…</div>`;
  }
  function renderAgents(st) {
    const a = st.agents || [];
    $("#agents").innerHTML = a.length ? a.map(h => {
      const status = h.age_s > 180 ? "dead" : h.status;
      return `<div class="row"><span class="dot ${esc(status)}"></span><div class="main"><div class="t">${esc(h.agent)}</div>
      <div class="s">${esc(h.detail || "")}</div></div><div class="v muted small">${esc(status)}<br>${ago(h.ts)} ago</div></div>`;
    }).join("") : `<div class="empty">No heartbeats yet</div>`;
  }
  function renderActivity(items) {
    $("#activity").innerHTML = items.length ? items.map(i => `
      <${i.mint ? `button class="row" data-mint="${esc(i.mint)}"` : `div class="row"`}><div class="main">
      <div class="t">${esc(i.msg)}</div><div class="s">${esc(i.agent)} · ${esc(i.kind)}${i.mint ? " · " + short(i.mint) : ""}</div></div>
      <div class="v muted small">${ago(i.ts)}</div></${i.mint ? "button" : "div"}>`).join("") : `<div class="empty">Nothing yet</div>`;
  }
  function renderAccount(st) {
    const a = st.account || {};
    const link = (label, u) => safeUrl(u) ? `<a class="row chev" href="${esc(u)}" target="_blank" rel="noopener noreferrer"><div class="main"><div class="t">${label}</div></div></a>` : "";
    $("#account").innerHTML = `
      <div class="row"><div class="main"><div class="t">FOMO handle</div></div><div class="v muted">${esc(a.fomo_handle ? "@" + a.fomo_handle : "not set")}</div></div>
      ${link("Open my FOMO profile", a.profile_url)}
      ${link("FOMO sign-up link", a.signup_url)}
      <div class="row"><div class="main"><div class="t">Mode</div></div><div class="v"><span class="badge paper">PAPER</span></div></div>
      <div class="row"><div class="main"><div class="t">Analyst</div></div><div class="v muted">${esc(a.analyst_mode || "")}</div></div>
      <div class="row"><div class="main"><div class="t">Notifications</div><div class="s">${esc(a.webpush_note || "")}</div></div><div class="v muted">Telegram only</div></div>
      <div class="row"><div class="main"><div class="t">Hosting</div></div><div class="v muted">Plan B · 15 s polling</div></div>`;
  }
  function renderChart() {
    if (!chart) return;
    const now = Date.now() / 1000;
    const pts = chart.equity.filter(p => !range || p[0] >= now - range).map(p => [p[0], p[1]]);
    DeskChart.drawLine($("#equity-chart"), pts, { base: state && state.balance ? state.balance.start_sol : null });
  }

  async function openCoin(mint) {
    openMint = mint; $("#sheet").classList.remove("hidden");
    $("#sheet-title").textContent = short(mint); $("#sheet-body").innerHTML = `<div class="empty">Loading…</div>`;
    try {
      const c = await get("coin/" + encodeURIComponent(mint) + ".json");
      if (openMint !== mint) return;
      const co = c.coin || {}, au = c.audit || {}, an = c.analysis || {}, f = au.facts || {};
      $("#sheet-title").textContent = co.symbol || short(mint);
      DeskChart.drawLine($("#coin-chart"), (c.series || []).map(s => [s[0], s[2] != null ? s[2] : s[1]]));
      const r = (k, v) => `<div class="row"><div class="main"><div class="t">${k}</div></div><div class="v muted">${esc(v)}</div></div>`;
      $("#sheet-body").innerHTML = r("Status", co.status || "—") + r("Liquidity", f.liquidity_usd != null ? usd(f.liquidity_usd) : "—")
        + r("Market cap", f.market_cap != null ? usd(f.market_cap) : "—")
        + r("Audit", au.passed == null ? "—" : au.passed ? "passed" : "rejected: " + (au.reasons || []).slice(0, 2).join("; "))
        + r("Analyst", an.decision ? `${an.decision} (${an.mode}, ${(+an.confidence).toFixed(2)})` : "—")
        + r("Mint", short(mint));
      const u = safeUrl(c.fomo_url); const a = $("#sheet-fomo");
      if (u) { a.href = u; a.classList.remove("hidden"); } else a.classList.add("hidden");
    } catch (e) { $("#sheet-body").innerHTML = `<div class="empty">No detail published for this coin yet</div>`; $("#sheet-fomo").classList.add("hidden"); }
  }

  // ---- poll
  let gated = false;
  async function tick() {
    try {
      const [st, ch, ac] = await Promise.all([get("state.json"), get("chart.json"), get("activity.json")]);
      if (!gated) { if (!(await gateOk(st))) return lock(); gated = true; $("#app").classList.remove("hidden"); }
      state = st; chart = ch;
      $("#paused").classList.toggle("hidden", !st.paused);
      const age = Date.now() / 1000 - st.generated_at;
      $("#stale").classList.toggle("hidden", age < 90);
      $("#updated").textContent = "updated " + ago(st.generated_at) + " ago";
      renderHome(st); renderAgents(st); renderActivity(ac.items || []); renderAccount(st); renderChart();
    } catch (e) {
      if (!gated) return lock();
      $("#stale").classList.remove("hidden");
    }
  }

  // ---- ui wiring
  document.querySelectorAll(".tabbar button").forEach(b => b.onclick = () => {
    document.querySelectorAll(".tabbar button").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
    window.scrollTo(0, 0); if (b.dataset.tab === "home") renderChart();
  });
  document.querySelectorAll("#range button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#range button").forEach(x => x.classList.toggle("on", x === b));
    range = +b.dataset.r; renderChart();
  });
  document.addEventListener("click", e => { const m = e.target.closest("[data-mint]"); if (m) openCoin(m.dataset.mint); });
  $("#sheet-close").onclick = () => { $("#sheet").classList.add("hidden"); openMint = null; };
  $("#sheet").onclick = e => { if (e.target.id === "sheet") $("#sheet-close").onclick(); };
  $("#signout").onclick = () => { localStorage.removeItem(LS_D); localStorage.removeItem(LS_T); location.reload(); };
  window.addEventListener("resize", renderChart);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

  tick(); setInterval(() => { if (!document.hidden) tick(); }, POLL_MS);
})();
