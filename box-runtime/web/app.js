/* app.js — STEP 8 iPhone app, Plan B client.
   Opens as {URL}#d={DATA_PATH}&t={APP_TOKEN}; d+t go to localStorage + same-origin cookies, then the hash is stripped.
   Cookies (SameSite=Lax, Secure) survive iOS Home Screen / standalone when Safari localStorage is partitioned.
   Boot reads hash → query → localStorage → cookie; re-mirrors into both stores; rewrites manifest start_url
   so a later Add to Home Screen opens with ?d=&t= even without the hash.
   Polls ./d/{DATA_PATH}/*.json every 15 s (no SSE, no POST, no web push registration).
   Soft gate: state.gate === sha256("fomo-desk-gate:" + t).
   Every FOMO link comes pre-built in the JSON (server links.py) — the app never builds one. */
(function () {
  "use strict";
  const POLL_MS = 15000, LS_D = "desk.d", LS_T = "desk.t", LS_SEEN = "desk.seen", SALT = "fomo-desk-gate:";
  const RANGES = ["LIVE", "1H", "4H", "1D", "1W", "ALL"];
  const AGENTS = ["scanner", "auditor", "analyst", "executor", "risk"];
  const PUSH_KINDS = { bought: 1, sold: 1, stopped: 1 };
  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];
  const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- credentials (hash -> localStorage + cookie, strip hash)
  // iOS standalone often launches manifest start_url without the private hash and may partition
  // localStorage from Safari; same-origin cookies are shared across Safari ↔ Home Screen.
  function cookiePath() {
    let p = location.pathname || "/";
    if (!p.endsWith("/")) p = p.replace(/\/[^/]*$/, "/") || "/";
    return p;
  }
  function readCookie(n) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function writeCred(n, v) {
    if (!v) return;
    try { localStorage.setItem(n, v); } catch (e) {}
    document.cookie = n + "=" + encodeURIComponent(v) + "; Path=" + cookiePath() + "; Max-Age=31536000; SameSite=Lax; Secure";
  }
  function clearCred(n) {
    try { localStorage.removeItem(n); } catch (e) {}
    document.cookie = n + "=; Path=" + cookiePath() + "; Max-Age=0; SameSite=Lax; Secure";
  }
  function refreshManifest(d, t) {
    const link = document.querySelector('link[rel="manifest"]');
    if (!link || !d) return;
    const q = new URLSearchParams();
    q.set("d", d);
    if (t) q.set("t", t);
    const apply = m => {
      m.start_url = "./?" + q.toString();
      m.scope = "./";
      const blob = new Blob([JSON.stringify(m)], { type: "application/json" });
      link.href = URL.createObjectURL(blob);
    };
    fetch("manifest.json", { cache: "no-store" }).then(r => r.ok ? r.json() : null).then(m => { if (m) apply(m); }).catch(() => {});
  }
  const hp = new URLSearchParams(location.hash.replace(/^#/, ""));
  const qp = new URLSearchParams(location.search);
  const dIn = hp.get("d") || qp.get("d") || "";
  const tIn = hp.get("t") || qp.get("t") || "";
  if (dIn) writeCred(LS_D, dIn);
  if (tIn) writeCred(LS_T, tIn);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  function readLS(n) { try { return localStorage.getItem(n) || ""; } catch (e) { return ""; } }
  let D = dIn || readLS(LS_D) || readCookie(LS_D) || "";
  let T = tIn || readLS(LS_T) || readCookie(LS_T) || "";
  if (D) writeCred(LS_D, D);
  if (T) writeCred(LS_T, T);
  if (D) refreshManifest(D, T);
  function lock() { $("#app").classList.add("hidden"); $("#locked").classList.remove("hidden"); }
  if (!D || !/^[A-Za-z0-9_-]{16,128}$/.test(D)) return lock();

  const base = "d/" + encodeURIComponent(D) + "/";
  const get = p => fetch(base + p + "?_=" + Date.now(), { cache: "no-store", credentials: "omit" })
    .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); });
  async function sha256(s) {
    const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
    return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join("");
  }
  async function gateOk(st) { // soft gate: only enforced when the server publishes a gate hash
    if (!st.gate) return true;
    if (!T || !(window.crypto && crypto.subtle)) return false;
    return (await sha256(SALT + T)) === st.gate;
  }

  // ---------------------------------------------------------------- formatting
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fin = v => v != null && v !== "" && isFinite(v);
  const usd = (v, d = 2) => fin(v) ? (v < 0 ? "-" : "") + "$" + Math.abs(+v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "n/a";
  const sUsd = v => fin(v) ? (v >= 0 ? "+" : "-") + usd(Math.abs(v)) : "n/a";
  const pct = (v, d = 2) => fin(v) ? (v >= 0 ? "+" : "") + (+v).toFixed(d) + "%" : "n/a";
  const compact = v => fin(v) ? "$" + Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v) : "n/a";
  const price = v => {
    if (!fin(v)) return "n/a";
    v = +v; if (v >= 1) return usd(v, v >= 1000 ? 0 : 2);
    const z = Math.max(0, Math.ceil(-Math.log10(v)) - 1); return "$" + v.toFixed(Math.min(12, z + 4));
  };
  const hhmm = ts => fin(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
  const cls = v => !fin(v) ? "" : v >= 0 ? "up" : "down";
  const short = m => m ? m.slice(0, 4) + "…" + m.slice(-4) : "";
  const httpsUrl = u => { try { const x = new URL(u); return x.protocol === "https:" ? x.href : null; } catch (e) { return null; } };
  const dur = s => { if (!fin(s)) return "n/a"; s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`; };

  // ---------------------------------------------------------------- store (last good data is kept on screen)
  const S = { state: null, chart: {}, activity: null, extra: [], nextPage: null, account: null, settings: null, meta: null,
    decisions: {}, coin: {}, audit: {}, lastOk: 0, failing: false, retryAt: 0, gated: false };
  let range = "LIVE", tab = "home", screen = null, screenMint = null, tickN = 0, busy = false;

  // ---------------------------------------------------------------- toast
  let toastT = null;
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 2600); }

  // ---------------------------------------------------------------- odometer (600ms ease-out per changed digit, 30ms stagger right to left)
  function odometer(el, text) {
    if (el.dataset.v === text) return;
    const old = el.dataset.v || "";
    el.dataset.v = text; el.setAttribute("aria-label", text);
    if (reduced() || !old || old.length !== text.length || !el.querySelector(".odo")) {
      el.innerHTML = `<span class="odo">${[...text].map(ch => /\d/.test(ch)
        ? `<span class="dg"><span class="col" style="transform:translateY(-${ch}0%)">${"0123456789".split("").map(d => `<span>${d}</span>`).join("")}</span></span>`
        : `<span class="ch">${esc(ch)}</span>`).join("")}</span>`;
      return;
    }
    const cols = [...el.querySelectorAll(".odo > *")];
    let k = 0;
    for (let i = text.length - 1; i >= 0; i--) {
      if (text[i] === old[i]) continue;
      const node = cols[i];
      if (!/\d/.test(text[i]) || !node.classList.contains("dg")) { node.textContent = text[i]; continue; }
      const col = node.firstChild;
      col.style.transition = `transform 600ms cubic-bezier(0.22,1,0.36,1) ${k * 30}ms`;
      col.style.transform = `translateY(-${text[i]}0%)`;
      k++;
    }
  }

  // ---------------------------------------------------------------- error row
  function renderOffline() {
    const age = S.state ? Date.now() / 1000 - S.state.generated_at : 0;
    const stale = S.state && age > 600;
    const show = S.failing || stale;
    const n = Math.max(0, Math.ceil(((S.failing ? S.retryAt : nextPollAt) - Date.now()) / 1000));
    const last = S.state ? hhmm(S.state.generated_at) : "never";
    $$(".offline").forEach(el => { el.classList.toggle("hidden", !show); if (show) el.textContent = `Desk offline · retrying in ${n}s · last update ${last}`; });
  }

  // ---------------------------------------------------------------- PAPER badge (STEP 11: every screen shows PAPER)
  function isPaper() { const st = S.state; return !!st && st.paper !== false && st.mode !== "LIVE"; }
  function paperFlag() { // Home + Coin carry their own PAPER pill; every other tab/screen gets the fixed one
    const own = (tab === "home" && !screen) || screen === "coin";
    $("#paper-flag").classList.toggle("hidden", !isPaper() || own);
  }

  // ---------------------------------------------------------------- HOME
  function renderHome() {
    const st = S.state; if (!st) return;
    const b = st.balance || {};
    const eq = fin(b.equity) ? b.equity : b.equity_usd;
    const balEl = $("#balance");
    if (fin(eq)) odometer(balEl, usd(eq)); else if (!balEl.dataset.v) balEl.textContent = "n/a";
    fitBalance(balEl);
    $("#paper-badge").classList.toggle("hidden", !isPaper());
    paperFlag();
    $("#today").innerHTML = `<span class="${cls(b.today_pct)}">${pct(b.today_pct)}</span><span class="lbl">today</span>`
      + (b.multiplier_text ? `<span class="pill mult">${esc(b.multiplier_text)}</span>` : "");
    const ac = st.account || {};
    $("#avatar-letter").textContent = (ac.fomo_handle || "G").replace(/^@/, "").charAt(0).toUpperCase() || "G";
    const running = fin(st.agents_running) ? st.agents_running : (st.agents || []).filter(a => a.running).length;
    $("#agents-h").textContent = `Agents · ${running} running`;
    const car = $("#carousel"), keep = car.scrollLeft;
    car.innerHTML = (st.agents || []).map(a => agentCard(a)).join("");
    car.scrollLeft = keep; drawRings(car);
    renderBell();
  }
  function fitBalance(el) {
    el.style.fontSize = "";
    const max = el.parentElement.clientWidth - 90; let fs = parseFloat(getComputedStyle(el).fontSize);
    while (el.scrollWidth > max && fs > 48) { fs -= 2; el.style.fontSize = fs + "px"; }
  }
  function renderChart(animate) {
    const ch = S.chart[range], cv = $("#home-chart"), em = $("#chart-empty");
    if (!ch) { if (!cv._prev) { em.textContent = ""; } return; }
    const pts = (ch.points || ch.equity || []).map(p => [p[0], p[1]]);
    DeskChart.line(cv, pts, { base: fin(ch.base) ? ch.base : ch.start_usd, marks: ch.marks || [], color: "#00d35a", animate: animate && !reduced() });
    const msg = ch.empty || (pts.length < 2 ? "Waiting for the first trade" : "");
    em.textContent = msg; em.classList.toggle("hidden", !msg);
  }
  function renderBell() {
    const seen = +localStorage.getItem(LS_SEEN) || 0;
    const items = (S.activity && S.activity.items) || [];
    const unread = items.some(i => PUSH_KINDS[i.kind] && i.ts > seen);
    $("#bell-dot").classList.toggle("hidden", !unread);
  }

  // ---------------------------------------------------------------- AGENT CARDS
  function dotCls(a) { return a.paused ? "pause" : a.running ? "run" : "down"; }
  function agentCard(a) {
    const col = a.color || "#fff";
    const head = `<div class="ah"><span class="dot ${dotCls(a)}"></span><span class="nm" style="color:${esc(col)}">${esc(a.name || a.agent)}</span></div>`;
    let big = esc(a.headline || ""), sub = "", body = "";
    if (a.agent === "scanner") {
      const feed = a.feed || [];
      body = feed.length ? `<ul class="feed">${feed.slice(0, 4).map(f => f.killed
        ? `<li><span class="k">${esc(f.coin)}</span><span class="why">${esc(f.reason || f.verdict)}</span></li>`
        : `<li><span>${esc(f.coin)}</span><span class="ok">→ auditor</span></li>`).join("")}</ul>`
        : `<div class="card-empty">${esc(a.empty || "Listening for graduations")}</div>`;
    } else if (a.agent === "auditor") {
      const s = a.strips || [];
      const cells = Array.from({ length: 8 }, (_, i) => `<i class="${s[i] === 1 || s[i] === true ? "p" : s[i] === 0 || s[i] === false ? "f" : ""}"></i>`).join("");
      const l = a.last;
      const line = !l ? `<div class="card-empty">${esc(a.detail || "No coin taken apart yet")}</div>`
        : s.length < 8 && !s.some(x => x === 0 || x === false) ? `<div class="sub">${esc(l.coin)} · taking apart…</div>`
        : `<div class="sub"><span style="color:#fff">${esc(l.coin)}</span> · <span class="${l.passed ? "up" : "down"}">${esc(l.result)}</span></div>`;
      body = `<div class="strips">${cells}</div>${line}`;
      sub = esc(a.sub || "");
    } else if (a.agent === "analyst") {
      const r = a.ring || {};
      body = `<canvas class="ring" data-score="${fin(r.score) ? r.score : ""}" data-green="${fin(r.green_from) ? r.green_from : 70}"></canvas>
        <div class="sub" style="text-align:center">${r.coin ? esc(r.coin) + " · " + (r.pass ? '<span class="up">pass</span>' : '<span class="down">reject</span>') : "Waiting for a coin"}</div>`;
      sub = "green from " + esc(fin(r.green_from) ? r.green_from : 70);
    } else if (a.agent === "executor") {
      const l = a.last;
      if (l) {
        const p = fin(l.progress) ? Math.max(0, Math.min(100, l.progress)) : null;
        const done = !!l.filled;
        const w = done ? 100 : p != null ? p : 0;
        const state = done ? `<span class="up">✓ Filled</span>` : p != null ? `Executing… ${Math.round(p)}%` : esc(l.status === "rejected" ? "Not filled" : "Executing…");
        body = `<div class="sub" style="color:#fff">${esc(l.coin)}</div><div class="sub">${esc(l.ticket || "")}</div>
          <div class="stackbar"><b style="width:${w}%"></b></div><div class="sub">${state}</div>`;
      } else body = `<div class="card-empty">${esc(a.detail || "Waiting for Analyst")}</div>`;
    } else if (a.agent === "risk") {
      sub = fin(a.guarding) ? `${a.guarding} open` : "";
      const pills = (a.pills || []).map(p => `<span class="pill mono" style="color:${esc(p.color)}">${esc(p.text)}</span>`).join("");
      body = (a.last_exit ? `<div class="sub" style="margin-bottom:10px">Last: ${esc(a.last_exit)}</div>` : "") + `<div class="pills">${pills}</div>`;
    }
    return `<div class="acard" data-agent="${esc(a.agent)}">${head}<div class="big">${big}</div>${sub ? `<div class="sub">${sub}</div>` : ""}<div class="body">${body}</div></div>`;
  }
  function drawRings(root) {
    root.querySelectorAll("canvas.ring").forEach(cv => DeskChart.ring(cv, cv.dataset.score === "" ? null : +cv.dataset.score, +cv.dataset.green));
  }

  // ---------------------------------------------------------------- AGENTS TAB
  function renderAgents() {
    const st = S.state; if (!st) return;
    const anyOpen = (st.positions || []).length > 0;
    $("#agents-list").innerHTML = (st.agents || []).map(a => {
      const locked = a.agent === "risk" && (a.locked || anyOpen);
      const dec = S.decisions[a.agent];
      const rows = !dec ? `<div class="row"><div class="m"><div class="sk sk-line"></div></div></div>`
        : !(dec.items || []).length ? `<div class="empty">No decisions yet</div>`
        : dec.items.slice(0, 20).map(d => {
          const u = httpsUrl(d.coin_url);
          const coin = u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(d.coin)}</a>` : esc(d.coin);
          const good = /pass|bought|guarding|trailed/.test(d.verdict || "");
          return `<div class="row dec"><div class="m"><div class="t">${coin} · <span class="${good ? "verdict-pass" : "verdict-reject"}">${esc(d.verdict)}</span></div>
            <div class="s">${esc(d.reason || "")}</div></div><div class="v s2">${esc(d.time || hhmm(d.ts))}</div></div>`;
        }).join("");
      return `<div class="agent-block" id="agent-${esc(a.agent)}">
        <div class="agent-head"><span class="dot ${dotCls(a)}"></span><span class="nm">${esc(a.name)}</span>
          <span class="cnt">${fin(a.today_count) ? a.today_count + " today" : ""}</span><span class="grow"></span>
          <button class="sw-btn" data-pause="${esc(a.agent)}" data-locked="${locked ? 1 : ""}" aria-label="${a.paused ? "Resume" : "Pause"} ${esc(a.name)}">
            <span class="sw ro ${a.paused ? "" : "on"}"></span></button></div>
        ${agentCard(a)}
        <div class="agent-note">${a.paused ? "Paused" : a.running ? "Running" : "Not running"}${locked ? " · Locked while a position is open" : ""} · ${esc(a.detail || "")}</div>
        <div class="group-h">Last 20 decisions</div>
        <div class="list">${rows}</div>
        <div class="list" style="margin-top:8px"><button class="row nav-row" data-settings="${esc(a.agent)}"><div class="m"><div class="t">Settings</div></div></button></div>
      </div>`;
    }).join("") + `<p class="foot">Plan B: pause and resume are shown read-only here. Send /pause or /resume to the Telegram bot; the next update shows it. Pause is desk-wide.</p><div style="height:24px"></div>`;
    drawRings($("#agents-list"));
  }

  // ---------------------------------------------------------------- ACTIVITY
  function actRow(i) {
    const right = i.right || (i.kind === "rejected" ? "Skipped" : fin(i.value) ? sUsd(i.value) : "");
    const rc = i.kind === "sold" ? cls(i.value) : i.kind === "stopped" ? "down" : i.kind === "rejected" ? "muted" : "";
    const opens = i.opens || (i.kind === "rejected" ? "audit" : "coin");
    const attrs = i.mint ? `data-open="${esc(opens)}" data-mint="${esc(i.mint)}"` : "";
    return `<button class="row" ${attrs}><div class="m"><div class="t">${esc(i.title || i.msg || "")}</div><div class="s">${esc(i.subtitle || "")}</div></div>
      <div class="v ${rc}">${esc(right)}</div></button>`;
  }
  function allActivity() {
    const first = (S.activity && S.activity.items) || [];
    const ids = new Set(first.map(i => i.id));
    return first.concat(S.extra.filter(i => !ids.has(i.id)));
  }
  function renderActivity() {
    if (!S.activity) return;
    const items = allActivity();
    const empty = `<div class="empty">${esc(S.activity.empty || "Nothing yet tonight")}</div>`;
    $("#activity-list").innerHTML = items.length ? items.map(actRow).join("") : empty;
    $("#home-activity").innerHTML = items.length ? items.slice(0, 5).map(actRow).join("") : empty;
    const next = S.extra.length ? S.nextPage : S.activity.next;
    $("#activity-more").classList.toggle("hidden", !next);
  }
  async function loadMore() {
    const next = S.extra.length ? S.nextPage : S.activity && S.activity.next;
    if (!next || !/^activity\/page_\d+\.json$/.test(next)) return;
    $("#load-more").textContent = "Loading…";
    try { const p = await get(next); S.extra = S.extra.concat(p.items || []); S.nextPage = p.next || null; renderActivity(); }
    catch (e) { toast("Couldn't load more · try again"); }
    $("#load-more").textContent = "Load more";
  }

  // ---------------------------------------------------------------- ACCOUNT
  // STEP 10: every setting, grouped per agent. Plan B: read-only (slider disabled + readonly exact number);
  // ranges / choices / bools come from the settings.json payload, never hard-coded here.
  function setRow(agent, key, val, set, prefix) {
    const ranges = set.ranges || {}, choices = set.choices || {}, bools = set.bools || [];
    const dotted = `${agent}.${prefix ? prefix + "." : ""}${key}`;
    const label = esc((prefix ? prefix + " · " : "") + key);
    const hint = `<div class="set-range"><span>${esc((set.range_text && set.range_text[dotted]) || "")}</span><span>set ${esc(dotted)} to …</span></div>`;
    if (typeof val === "boolean" || bools.includes(dotted)) {
      return `<div class="set-row" data-key="${esc(dotted)}"><div class="set-top"><span class="k">${label}</span><button class="sw-btn" data-ro="${esc(dotted)}" aria-disabled="true" aria-label="${label}"><span class="sw ro ${val ? "on" : ""}"></span></button></div>${hint}</div>`;
    }
    if (choices[dotted]) {
      return `<div class="set-row" data-key="${esc(dotted)}"><div class="set-top"><span class="k">${label}</span></div><div class="seg" style="margin:6px 0 0">${choices[dotted].map(c =>
        `<button data-ro="${esc(dotted)}" aria-disabled="true" class="${c === val ? "on" : ""}">${esc(c)}</button>`).join("")}</div>${hint}</div>`;
    }
    if (ranges[dotted] && fin(val)) {
      const [lo, hi] = ranges[dotted];
      const step = (hi - lo) <= 1 ? 0.01 : Number.isInteger(+val) && Number.isInteger(lo) ? 1 : 0.1;
      return `<div class="set-row" data-key="${esc(dotted)}"><div class="set-top"><span class="k">${label}</span><input type="number" inputmode="decimal" readonly min="${lo}" max="${hi}" step="${step}" value="${esc(val)}" data-ro="${esc(dotted)}" aria-label="${label} exact value"></div>
        <input type="range" min="${lo}" max="${hi}" step="${step}" value="${esc(val)}" disabled aria-label="${label}">
        <div class="set-range"><span>${lo}</span><span>${hi}</span></div></div>`;
    }
    return `<div class="set-row" data-key="${esc(dotted)}"><div class="set-top"><span class="k">${label}</span><span class="muted" style="font-size:15px;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(val == null ? "n/a" : val)}</span></div>${hint}</div>`;
  }
  function renderAccount() {
    const ac = S.account || (S.state && S.state.account); if (!ac) return;
    const mode = ac.mode || (S.state && S.state.mode) || "PAPER";
    const stack = ac.stack || {};
    const prof = httpsUrl(ac.profile_url), inv = httpsUrl((ac.invite && ac.invite.url) || ac.signup_url || ac.SIGNUP_URL);
    const tg = ac.telegram || {};
    const set = S.settings;
    const tc = (set && set.ticket_check) || ac.ticket_check;
    let settingsHtml = `<div class="list"><div class="row"><div class="m"><div class="sk sk-line"></div></div></div></div>`;
    if (set && set.settings) {
      settingsHtml = (set.groups || AGENTS.concat(["desk"])).filter(g => set.settings[g]).map(g => {
        const rows = [];
        Object.entries(set.settings[g]).forEach(([k, v]) => {
          if (v && typeof v === "object" && !Array.isArray(v)) Object.entries(v).forEach(([k2, v2]) => rows.push(setRow(g, k2, v2, set, k)));
          else rows.push(setRow(g, k, v, set));
        });
        return `<div class="group-h" id="set-${esc(g)}">${esc(g.charAt(0).toUpperCase() + g.slice(1))}</div><div class="list">${rows.join("")}</div>`;
      }).join("");
    }
    const standalone = navigator.standalone || (window.matchMedia && matchMedia("(display-mode: standalone)").matches);
    $("#account-body").innerHTML = `
      <div class="group-h">Mode</div>
      <div class="seg"><button data-mode="PAPER" class="${mode !== "LIVE" ? "on" : ""}">PAPER</button><button data-mode="LIVE" class="${mode === "LIVE" ? "on" : ""}">LIVE</button></div>
      <p class="foot">Read-only on Plan B. Going LIVE: send "live" to the Telegram bot, then type LIVE. Needs at least 1 full PAPER night in the db${ac.mode_switch && ac.mode_switch.live_gate ? (ac.mode_switch.live_gate.paper_night_done ? " (done)" : " (not yet)") : ""}. The desk never places orders: you buy and sell inside FOMO.</p>
      <div class="group-h">Stack</div>
      <div class="list">
        <div class="row"><div class="m"><div class="t">Paper stack at start</div></div><div class="v">${usd(stack.paper_start_stack)}</div></div>
        <div class="row"><div class="m"><div class="t">Cash</div></div><div class="v">${usd(stack.paper_cash_usd)}</div></div>
        <div class="row"><div class="m"><div class="t">Equity</div></div><div class="v">${usd(stack.equity_usd)}</div></div>
        ${tc ? `<div class="row"><div class="m"><div class="t">Ticket check</div><div class="s wrap">${esc(tc.text || ("OK · min stack " + usd(tc.min_stack_usd)))}</div></div><div class="v ${tc.status === "skip" ? "down" : tc.status === "bump" ? "s2" : "up"}">${esc(tc.status === "ok" ? "OK" : tc.below_min || "")}</div></div>` : ""}
      </div>
      <div class="group-h">FOMO</div>
      <div class="list">
        ${prof ? `<a class="row link" href="${esc(prof)}" target="_blank" rel="noopener noreferrer"><div class="m"><div class="t">Your FOMO profile</div><div class="s">${esc(ac.fomo_handle ? "@" + String(ac.fomo_handle).replace(/^@/, "") : "")}</div></div><div class="v"></div></a>`
          : `<div class="row"><div class="m"><div class="t">Your FOMO profile</div><div class="s">No FOMO handle set</div></div></div>`}
        ${inv ? `<button class="row link" id="invite" data-url="${esc(inv)}"><div class="m"><div class="t">Invite a friend</div></div><div class="v"></div></button>` : ""}
      </div>
      <div class="group-h">Notifications</div>
      <div class="list">
        <div class="row"><div class="m"><div class="t">Telegram</div></div><div class="v ${tg.connected ? "up" : "down"}">${tg.connected ? "Connected" : "Not connected"}</div></div>
        <button class="row" id="test-push"><div class="m"><div class="t" style="color:var(--blue)">Send test push</div><div class="s">Via the Telegram bot</div></div></button>
        <div class="row"><div class="m"><div class="t">Web push</div><div class="s wrap">${esc((ac.push && ac.push.note) || ac.webpush_note || "Plan B: no web push. Telegram only.")}</div></div><div class="v s2">Telegram only</div></div>
        ${standalone ? `<div class="row"><div class="m"><div class="t faint">Enable notifications</div><div class="s">Not available on Plan B (no endpoint to save a subscription)</div></div></div>` : ""}
      </div>
      <div class="group-h">Settings</div>
      <p class="foot" style="margin-top:0">${esc((S.meta && S.meta.post) || "Plan B: settings are read-only here.")}</p>
      ${settingsHtml}
      <div class="group-h">Hosting</div>
      <div class="list"><div class="row"><div class="m"><div class="t">Plan B · static</div><div class="s">Refreshes every 15 s · no SSE · no POST</div></div></div></div>
      <div class="list" style="margin-top:24px"><button class="row danger" id="signout">Forget this device</button></div>
      <div style="height:24px"></div>`;
  }

  // ---------------------------------------------------------------- COIN SCREEN
  function renderCoin() {
    const m = screenMint, el = $("#coin-body");
    const c = S.coin[m];
    if (c === undefined) {
      el.innerHTML = `<div class="coin-head"><div class="sk" style="height:28px;width:50%"></div><div class="sk" style="height:70px;width:70%;margin-top:12px"></div></div>
        <div class="sk" style="height:280px;margin:12px 16px"></div>`; return;
    }
    if (c === null) { el.innerHTML = `<div class="empty">No data published for ${esc(short(m))} yet</div>`; return; }
    const sh = c.audit && c.audit.shield;
    const pos = c.position, ex = c.exit;
    const st = c.stats || {};
    const u = httpsUrl((c.button && c.button.url) || c.coin_url);
    const btnText = (c.button && c.button.text) || (pos ? "Sell on FOMO" : "Buy on FOMO");
    const left = ex && fin(ex.timeout_remaining_s) ? ex.timeout_remaining_s - (Date.now() / 1000 - c.generated_at) : null;
    el.innerHTML = `
      <div class="coin-head">
        <div class="coin-name">${esc(c.name)}${sh ? `<span class="chip"><svg viewBox="0 0 24 24"><path d="M12 2l8 3v6c0 5-3.4 9.4-8 11-4.6-1.6-8-6-8-11V5z"/></svg>${esc(sh)}</span>` : ""}
          ${c.paper !== false ? `<span class="pill paper">PAPER</span>` : ""}</div>
        <div class="muted" style="font-size:15px;margin-top:2px">${esc(c.subtitle || "")}</div>
        <div class="price">${price(c.price)}</div>
        ${fin(c.since_entry_pct) ? `<div class="${cls(c.since_entry_pct)}" style="font-weight:600">${c.since_entry_pct >= 0 ? "▲" : "▼"} ${Math.abs(c.since_entry_pct).toFixed(2)}% since entry</div>` : ""}
      </div>
      <canvas id="coin-chart"></canvas>
      <div class="stats">
        <div class="stat"><div class="k">Market cap</div><div class="v">${compact(st.market_cap)}</div></div>
        <div class="stat"><div class="k">Liquidity</div><div class="v">${compact(st.liquidity_usd)}</div></div>
        <div class="stat"><div class="k">24h volume</div><div class="v">${compact(st.volume_h24)}</div></div>
        <div class="stat"><div class="k">Holders</div><div class="v">${fin(st.holders) ? (+st.holders).toLocaleString("en-US") : "n/a"}</div></div>
      </div>
      ${pos ? `<div class="card"><div class="ct">${esc(pos.label || "Your position")}</div>
        <div style="font-size:22px;font-weight:700;margin-top:4px" class="${cls(pos.pnl_usd)}">${sUsd(pos.pnl_usd)} <span style="font-size:15px">${pct(pos.pnl_pct)}</span></div></div>
        <div class="card"><div class="ct">Exit</div>
          <div class="row" style="padding:10px 0"><div class="m"><div class="t">Stop line</div></div><div class="v down">${price(ex && ex.stop_price)}${ex && fin(ex.stop_pct) ? ` · -${(+ex.stop_pct).toFixed(1)}%` : ""}</div></div>
          <div class="row" style="padding:10px 0"><div class="m"><div class="t">Trail line</div></div><div class="v up">${ex && ex.trail_active ? price(ex.trail_price) : ex && fin(ex.trail_arms_at_price) ? "arms at " + price(ex.trail_arms_at_price) : "n/a"}</div></div>
          <div class="row" style="padding:10px 0"><div class="m"><div class="t">Time left to timeout</div></div><div class="v">${dur(left)}</div></div>
        </div>` : ""}
      <a class="cta ${u ? "" : "disabled"}" ${u ? `href="${esc(u)}" target="_blank" rel="noopener noreferrer"` : ""}>${esc(btnText)}</a>
      <div style="height:16px"></div>`;
    DeskChart.candles($("#coin-chart"), c.candles || [], { exitLine: ex && ex.exit_line, exitLabel: (ex && ex.exit_line_label) || "Risk trailing stop" });
  }

  // ---------------------------------------------------------------- AUDITOR SCREEN (beam 350ms/layer, then 150ms to green/red)
  let beamKey = null, beamTimer = null, beamDone = { id: null, n: 0 };
  function renderAudit() {
    const m = screenMint, el = $("#audit-body"), a = S.audit[m];
    if (a === undefined) { el.innerHTML = `<div class="discs">${Array.from({ length: 8 }, () => `<div class="disc sk"></div>`).join("")}</div>`; return; }
    if (a === null || !(a.layers || []).length) { el.innerHTML = `<div class="empty">${esc((a && a.empty) || "No coin taken apart yet")}</div>`; return; }
    const L = a.layers;
    const firstFail = L.findIndex(x => x.pass === false);
    const firstWait = L.findIndex(x => x.pass === null || x.pass === undefined);
    // the beam stops on a layer the server hasn't answered yet (unless an earlier layer already failed)
    const stopAt = firstFail >= 0 && (firstWait < 0 || firstFail < firstWait) ? -1 : firstWait;
    const u = httpsUrl((a.button && a.button.url) || a.coin_url);
    const res = a.result || {};
    const enabled = a.enabled !== false;
    el.innerHTML = `
      <div style="padding:0 16px;text-align:center"><div class="coin-name" style="justify-content:center">${esc(a.coin || short(m))}</div></div>
      <div class="discs" id="discs">${L.map(x => `<div class="disc" data-n="${x.n}"></div>`).join("")}<div class="beam" id="beam"></div></div>
      <div class="result">${res.title ? `<div class="rt ${a.audit && a.audit.passed ? "up" : "down"}">${esc(res.title)}</div><div class="rs">${esc(res.subtitle || "")}</div>` : `<div class="rt muted">Taking apart…</div>`}</div>
      <div class="group-h">8 parts</div>
      <div class="list">${L.map(x => `<div class="row"><div class="m"><div class="t">${x.n}. ${esc(x.name)}</div></div>
        <div class="v ${x.pass === true ? "up" : x.pass === false ? "down" : "faint"}">${esc(x.value_text || x.value || "not checked")} ${x.pass === true ? "✓" : x.pass === false ? "✕" : ""}</div></div>`).join("")}</div>
      <div class="list" style="margin-top:16px"><button class="row" id="audit-toggle"><div class="m"><div class="t">Take apart before buying</div><div class="s wrap">Plan B: read-only · turning it off asks twice (Plan A)</div></div><span class="sw ro ${enabled ? "on" : ""}"></span></button></div>
      <a class="cta ${u ? "" : "disabled"}" ${u ? `href="${esc(u)}" target="_blank" rel="noopener noreferrer"` : ""}>${esc((a.button && a.button.text) || "Open on FOMO")}</a>
      <div style="height:16px"></div>`;
    const key = m + ":" + (a.audit && a.audit.ts) + ":" + L.map(x => x.status).join(",");
    const discs = [...el.querySelectorAll(".disc")], beam = $("#beam");
    const paint = i => { const x = L[i]; discs[i].className = "disc " + (x.pass === true ? "p" : x.pass === false ? "f" : ""); discs[i].textContent = x.pass === true ? "✓" : x.pass === false ? "✕" : ""; };
    const beamTo = i => { const d = discs[i]; beam.style.transform = `translateY(${d.offsetTop + d.offsetHeight / 2}px)`; };
    clearTimeout(beamTimer);
    const last = stopAt >= 0 ? stopAt : (firstFail >= 0 ? firstFail : L.length - 1);
    if (reduced() || beamKey === key) { // instant swap
      L.forEach((_, i) => { if (i <= last && i !== stopAt) paint(i); });
      if (stopAt >= 0) { beam.classList.add("on", "waiting"); beam.style.transition = "none"; beamTo(stopAt); }
      beamKey = key; return;
    }
    beamKey = key;
    const id = m + ":" + (a.audit && a.audit.ts);
    let i = beamDone.id === id ? Math.min(beamDone.n, last) : 0; // continue where the beam was waiting
    for (let j = 0; j < i; j++) paint(j);
    beam.style.transition = "none"; beam.classList.add("on");
    if (i > 0) beamTo(i - 1); else beam.style.transform = "translateY(0px)";
    const step = () => {
      if (i > last) { beam.classList.remove("on"); return; }
      beam.style.transition = "transform 350ms linear"; beamTo(i);
      if (i === stopAt) { beam.classList.add("waiting"); beamDone = { id, n: i }; return; } // wait on this layer for the server
      const k = i;
      beamTimer = setTimeout(() => { paint(k); i++; beamDone = { id, n: i }; beamTimer = setTimeout(step, 150); }, 350);
    };
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- search
  function searchItems() {
    const st = S.state || {}, map = new Map();
    (st.positions || []).forEach(p => map.set(p.mint, { mint: p.mint, title: p.symbol || p.name || short(p.mint), sub: "Open position" }));
    (st.coins || []).forEach(c => map.has(c.mint) || map.set(c.mint, { mint: c.mint, title: c.symbol || c.name || short(c.mint), sub: [c.name, c.status].filter(Boolean).join(" · "), name: c.name }));
    allActivity().forEach(i => i.mint && !map.has(i.mint) && map.set(i.mint, { mint: i.mint, title: i.title, sub: i.subtitle }));
    return [...map.values()];
  }
  function renderSearch() {
    const q = $("#search-input").value.trim().toLowerCase();
    const items = searchItems().filter(i => !q || [i.title, i.sub, i.name, i.mint].some(x => x && String(x).toLowerCase().includes(q)));
    $("#search-results").innerHTML = items.length ? items.slice(0, 40).map(i => `<button class="row" data-open="coin" data-mint="${esc(i.mint)}"><div class="m"><div class="t">${esc(i.title)}</div><div class="s">${esc(i.sub || short(i.mint))}</div></div><div class="v s2">${esc(short(i.mint))}</div></button>`).join("")
      : `<div class="empty">${q ? "No match" : "Nothing yet tonight"}</div>`;
  }

  // ---------------------------------------------------------------- navigation
  function setTab(t) {
    tab = t;
    $$("#tabbar button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.dataset.tab !== t));
    window.scrollTo(0, 0);
    if (t === "home") requestAnimationFrame(() => { renderChart(false); drawRings($("#carousel")); });
    if (t === "agents") { renderAgents(); loadDecisions(); }
    if (t === "activity") { localStorage.setItem(LS_SEEN, String(Date.now() / 1000)); renderBell(); }
    paperFlag();
  }
  function openScreen(kind, mint) {
    if (!mint || !/^[A-Za-z0-9]{20,64}$/.test(mint)) return;
    closeScreen(true);
    screen = kind; screenMint = mint;
    const el = kind === "audit" ? $("#screen-audit") : $("#screen-coin");
    el.scrollTop = 0; el.classList.add("open"); el.setAttribute("aria-hidden", "false");
    history.pushState({ screen: kind }, "", location.pathname + location.search);
    if (kind === "audit") { beamKey = null; beamDone = { id: null, n: 0 }; renderAudit(); } else renderCoin();
    paperFlag();
    loadScreen();
  }
  function closeScreen(silent) {
    $$(".screen").forEach(s => { s.classList.remove("open"); s.setAttribute("aria-hidden", "true"); });
    clearTimeout(beamTimer); screen = null; screenMint = null;
    paperFlag();
    if (!silent && history.state && history.state.screen) history.back();
  }
  async function loadScreen() {
    const kind = screen, m = screenMint; if (!kind || !m) return;
    try { const j = await get((kind === "audit" ? "audit/" : "coin/") + encodeURIComponent(m) + ".json"); (kind === "audit" ? S.audit : S.coin)[m] = j; }
    catch (e) { const bag = kind === "audit" ? S.audit : S.coin; if (String(e.message) === "404" && bag[m] === undefined) bag[m] = null; }
    if (screen === kind && screenMint === m) (kind === "audit" ? renderAudit : renderCoin)();
  }
  async function loadDecisions() {
    await Promise.all(AGENTS.map(a => get(`agents/${a}/decisions.json`).then(j => { S.decisions[a] = j; }).catch(() => {})));
    if (tab === "agents") renderAgents();
  }
  async function loadChart(r, animate) {
    try { S.chart[r] = await get(`chart/${r}.json`); }
    catch (e) { if (r === "LIVE") S.chart[r] = await get("chart.json"); else throw e; }
    if (r === range) renderChart(animate);
  }

  // ---------------------------------------------------------------- poll
  let nextPollAt = Date.now() + POLL_MS;
  async function tick() {
    if (busy) return; busy = true; tickN++;
    try {
      const st = await get("state.json");
      if (!S.gated) { if (!(await gateOk(st))) { busy = false; return lock(); } S.gated = true; }
      S.state = st;
      const jobs = [loadChart(range, tickN > 1),
        get("activity.json").then(a => { S.activity = a; }),
        get("account.json").then(a => { S.account = a; }).catch(() => {})];
      if (!S.settings || tickN % 4 === 1) jobs.push(get("settings.json").then(s => { S.settings = s; }).catch(() => {}),
        get("settings_meta.json").then(s => { S.meta = s; }).catch(() => {}));
      if (tab === "agents") jobs.push(loadDecisions());
      await Promise.all(jobs);
      S.failing = false; S.lastOk = Date.now();
      renderHome(); renderActivity(); if (tab === "agents") renderAgents(); renderAccount();
      if (!$("#search").classList.contains("hidden")) renderSearch();
      if (screen) loadScreen();
    } catch (e) {
      S.failing = true; S.retryAt = Date.now() + POLL_MS;
    }
    nextPollAt = Date.now() + POLL_MS;
    renderOffline(); busy = false;
  }

  // ---------------------------------------------------------------- wiring
  $("#tabbar").addEventListener("click", e => { const b = e.target.closest("button[data-tab]"); if (b) setTab(b.dataset.tab); });
  $("#avatar").onclick = () => setTab("account");
  $("#bot-title").onclick = () => setTab("agents");
  $("#see-all").onclick = () => setTab("activity");
  $("#bell").onclick = () => setTab("activity");
  $("#load-more").onclick = loadMore;
  $("#search-btn").onclick = () => { $("#search").classList.remove("hidden"); renderSearch(); $("#search-input").focus(); };
  $("#search-cancel").onclick = () => { $("#search").classList.add("hidden"); $("#search-input").value = ""; };
  $("#search-input").oninput = renderSearch;
  $("#ranges").addEventListener("click", e => {
    const b = e.target.closest("button[data-r]"); if (!b || !RANGES.includes(b.dataset.r)) return;
    range = b.dataset.r; $$("#ranges button").forEach(x => x.classList.toggle("on", x === b));
    const cv = $("#home-chart"); cv._prev = null; cv._marks = null;
    renderChart(false); loadChart(range, false).catch(() => { S.failing = true; S.retryAt = Date.now() + POLL_MS; renderOffline(); });
  });
  document.addEventListener("click", e => {
    const back = e.target.closest("[data-back]"); if (back) return closeScreen();
    const o = e.target.closest("[data-open][data-mint]");
    if (o && !e.target.closest("a")) { $("#search").classList.add("hidden"); return openScreen(o.dataset.open === "audit" ? "audit" : "coin", o.dataset.mint); }
    const card = e.target.closest(".acard[data-agent]");
    if (card && card.closest("#carousel")) { setTab("agents"); const t = $("#agent-" + card.dataset.agent); if (t) t.scrollIntoView(); return; }
    const p = e.target.closest("[data-pause]");
    if (p) return toast(p.dataset.locked ? "Risk is locked while a position is open" : "Plan B: send /pause or /resume to the Telegram bot");
    const s = e.target.closest("[data-settings]");
    if (s) { setTab("account"); requestAnimationFrame(() => { const t = $("#set-" + s.dataset.settings); if (t) t.scrollIntoView({ block: "start" }); }); return; }
    if (e.target.closest("[data-ro]") || e.target.closest("[data-mode]")) return toast("Plan B: read-only here. Settings change via Telegram.");
    if (e.target.closest("#audit-toggle")) return toast("Plan B: read-only. Turning it off asks twice (Plan A).");
    if (e.target.closest("#test-push")) return toast("Test pushes come from the Telegram bot");
    if (e.target.closest("#signout")) { clearCred(LS_D); clearCred(LS_T); location.reload(); return; }
    const inv = e.target.closest("#invite");
    if (inv) {
      const url = inv.dataset.url;
      if (navigator.share) navigator.share({ title: "FOMO", url }).catch(() => {});
      else if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => toast("Link copied"), () => toast(url));
      else toast(url);
    }
  });
  window.addEventListener("popstate", () => { if (screen) closeScreen(true); });
  let rz = null;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { renderChart(false); drawRings(document); if (S.state) fitBalance($("#balance")); if (screen === "coin") renderCoin(); }, 120); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  setInterval(renderOffline, 1000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

  tick();
  setInterval(() => { if (!document.hidden && Date.now() >= nextPollAt - 200) tick(); }, 1000);
})();
