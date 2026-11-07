"use strict";

// The server needs this Mac's key on every request for data: `harness-watch open` stores it in
// this origin's storage (no other port or site can read it) and every request here sends it.
const PAGE_KEY = (() => { try { return localStorage.getItem("harness-watch-key") || ""; } catch { return ""; } })();
const plainFetch = window.fetch.bind(window);
window.fetch = (url, opts = {}) => plainFetch(url, {...opts, headers: {...opts.headers, "X-Harness-Watch-Key": PAGE_KEY}})
  .then(resp => { if (resp.status === 401) needKey(); return resp; });

function needKey() {
  // The key is kept per address: hand it to this one (localhost:8787 and 127.0.0.1:8787 are two).
  document.body.innerHTML = '<p class="need-key">This page needs this Mac\'s key. In a terminal, run ' +
    `<code>harness-watch open --host ${esc(location.host)}</code> (once per browser).</p>`;
}

const tbody = document.querySelector("#sessions tbody");
const empty = document.getElementById("empty");
const summary = document.getElementById("summary");
const conn = document.getElementById("conn");
const detail = document.getElementById("detail");
const overlay = document.getElementById("overlay");
const historyBody = document.querySelector("#history tbody");
const pager = document.getElementById("pager");
const indexing = document.getElementById("indexing");
const tabs = document.getElementById("tabs");
const search = document.getElementById("search");
const searchCount = document.getElementById("search-count");
const usageBox = document.getElementById("usage");
let sessions = [];
let usage = [];  // plan limits per account, as /usage shows them
// URL hash: "#<id>" (running view), "#all" or "#all/<id>" (all sessions view).
const hash = decodeURIComponent(location.hash.slice(1));
let view = hash === "all" || hash.startsWith("all/") ? "all" : "live";
let selectedId = (view === "all" ? hash.slice(4) : hash) || null;
let history_ = {page: 1, size: 50, total: null, all: null, q: "", items: [], indexing: null, loadedAt: 0};
let historySeq = 0;  // only the newest response is shown while typing
let closed = {key: null, data: null, loading: null};  // detail of a session that isn't running
const openDirs = new Map();  // tree node key -> open, as the user last toggled it
let activity = {key: null, data: null, loading: null};  // trees, sub-agents, shells of the selected session
const outputs = new Map();  // "<session id>|<task id>" -> output tail of an opened background shell
let lastDetailHtml = "";
let openSections = new Set();  // detail sections the user opened (all start folded), kept across sessions and reloads
try { openSections = new Set(JSON.parse(localStorage.getItem("openSections") || "[]")); } catch (_) {}

function secOpen(name) {
  return openSections.has(name) ? "open" : "";
}


function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function tilde(path) {
  return String(path ?? "").replace(/^\/Users\/[^/]+/, "~");
}

function ago(ms) {
  if (!ms) return "";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d`;
}

function tokens(n) {
  if (n == null) return "–";
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

function usd(n) {
  if (n == null) return "–";
  if (n >= 100) return `$${n.toFixed(0)}`;
  return n > 0 && n < 0.01 ? "<$0.01" : `$${n.toFixed(2)}`;
}

function when(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    ...(sameYear ? {} : {year: "numeric"})});
}

function duration(fromIso, toIso) {
  if (!fromIso || !toIso) return "–";
  const ms = Date.parse(toIso) - Date.parse(fromIso);
  return ms >= 0 ? ago(Date.now() - ms) : "–";
}

function shortModel(m) {
  return m ? m.replace(/^claude-/, "") : "–";
}

function titleOf(s) {
  return s.stats?.title || s.iterm?.name || s.name || "";
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

function statusCell(s) {
  const cls = s.attention ? "idle" : s.status === "busy" ? "busy" : "other";
  const label = s.attention ? s.status.toUpperCase() : s.status;
  const since = s.attention ? `${s.waiting_for ? esc(s.waiting_for) : "waiting"} ${ago(s.status_since_ms)}` : `for ${ago(s.status_since_ms)}`;
  const limit = s.limit ? `<span class="pill limit" title="${esc(s.limit.text)}${s.account ? ` (account ${esc(s.account)}; every session on it is blocked)` : ""}">${esc(s.limit.kind)} limit</span>` +
    `<span class="since limit">${s.limit.resets ? `resets ${clock(s.limit.resets)}` : "reset time unknown"}</span>` : "";
  return limit + `<span class="pill ${cls}">${esc(label)}</span><span class="since">${since}</span>`;
}

// Time since Claude's last response (main session or a sub-agent); the exact time on hover.
function replyCell(st) {
  if (!st?.last_response_at) return `<span class="muted">–</span>`;
  return `<span class="num" title="${esc(when(st.last_response_at))}">${ago(Date.parse(st.last_response_at))}</span>`;
}

function dirCell(s) {
  const cwd = s.stats?.cwd || s.start_cwd;
  const moved = cwd !== s.start_cwd;
  const hops = (s.stats?.cwd_history?.length || 1) - 1;
  return `<span class="path ${moved ? "moved" : ""}">${esc(tilde(cwd))}</span>` +
    (moved ? `<div class="sub">started in <span class="path">${esc(tilde(s.start_cwd))}</span></div>` : "") +
    (hops > 0 ? `<div class="sub">${hops} dir change${hops > 1 ? "s" : ""}</div>` : "");
}

function contextCell(st) {
  if (!st || st.context_pct == null) return `<span class="muted">–</span>`;
  const pct = Math.min(100, st.context_pct);
  const level = pct >= 80 ? "high" : pct >= 50 ? "mid" : "";
  return `<div class="meter ${level}"><span style="width:${pct}%"></span></div>` +
    `<div class="sub num">${tokens(st.context_tokens)} · ${st.context_pct}%</div>`;
}

function terminalText(s) {
  if (s.in_iterm) return `iTerm2 · window ${s.iterm.window_id}, tab ${s.iterm.tab_index}`;
  return s.host_app ? `${s.host_app} (not iTerm2)` : s.tty ? `${s.tty} (not iTerm2)` : "no terminal";
}

function itermTip(s) {
  const where = s.host_app ? `in ${s.host_app}` : s.tty ? `on ${s.tty}` : "without a terminal";
  return `Not running in iTerm2 (runs ${where}), so the dashboard can't focus its tab.`;
}

function itermWarning(s) {
  if (s.in_iterm) return "";
  const tip = itermTip(s);
  return ` <span class="warn-icon" title="${esc(tip)}" aria-label="${esc(tip)}">⚠</span>`;
}

function renderHeader() {
  const waiting = sessions.filter(s => s.attention && !s.limit).length;
  const limited = new Set(sessions.filter(s => s.limit).map(s => s.account));
  summary.innerHTML = `${sessions.length} running · ${waiting ? `<b>${waiting} waiting for you</b>` : "0 waiting for you"}` +
    (limited.size ? ` · <b class="limit">${[...limited].map(esc).join(", ")} at limit</b>` : "");
  for (const b of tabs.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.view === view);
    const n = b.dataset.view === "live" ? sessions.length : history_.all;
    b.innerHTML = `${b.dataset.view === "live" ? "Running" : "All sessions"}${n != null ? `<span class="count">${n}</span>` : ""}`;
  }
  document.getElementById("live-view").hidden = view !== "live";
  document.getElementById("all-view").hidden = view !== "all";
  renderUsage();
}

// "resets in 4 h 40 min (22:50)"; the weekday too when it isn't today.
function resetText(iso) {
  const at = new Date(iso), ms = at - Date.now();
  if (!(ms > 0)) return "resetting…";
  const mins = Math.ceil(ms / 60e3), d = Math.floor(mins / 1440), h = Math.floor(mins % 1440 / 60), m = mins % 60;
  const left = d ? `${d} d${h ? ` ${h} h` : ""}` : h ? `${h} h${m ? ` ${m} min` : ""}` : `${m} min`;
  const today = at.toDateString() === new Date().toDateString();
  const exact = at.toLocaleString([], {...(today ? {} : {weekday: "short"}), hour: "2-digit", minute: "2-digit"});
  return `resets in ${left} (${exact})`;
}

const STALE_MS = 10 * 60e3;  // the status line only reports while a session on the account runs

// One block per account: a bar per limit, filled by the share used.
function renderUsage() {
  usageBox.hidden = !usage.length;
  usageBox.innerHTML = usage.map(a => `
    <div class="usage-account">
      <div class="usage-head">
        <span class="account ${a.account === "claude" ? "" : "work"}">${esc(a.account)}</span>
        <span class="muted">${esc([a.email, a.plan].filter(Boolean).join(" · "))}</span>
        ${a.error ? `<span class="usage-error">${esc(a.error)}${a.updated_at ? ` · as of ${clock(a.updated_at)}` : ""}</span>`
          : a.updated_at && Date.now() - Date.parse(a.updated_at) > STALE_MS ? `<span class="muted">as of ${when(a.updated_at)}</span>` : ""}
      </div>
      ${a.limits.map(l => `
      <div class="usage-row ${esc(l.severity)}">
        <span class="usage-label">${esc(l.label)}</span>
        <span class="bar" role="progressbar" aria-valuenow="${l.percent}" aria-valuemin="0" aria-valuemax="100"><span style="width:${Math.min(100, Math.max(0, l.percent))}%"></span></span>
        <span class="num usage-pct">${Math.round(l.percent)}% used</span>
        <span class="muted usage-reset">${l.resets_at ? resetText(l.resets_at) : ""}</span>
      </div>`).join("")}
    </div>`).join("");
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// What runs besides the conversation itself: sub-agents, background shells, foreground commands.
function runningLine(s) {
  const c = s.stats?.task_counts || {};
  const commands = (s.processes || []).filter(p => p.tool && !p.shell_id).length;
  const parts = [
    c.agents_running && plural(c.agents_running, "sub-agent"),
    c.shells_running && plural(c.shells_running, "background shell"),
    commands && plural(commands, "command"),
  ].filter(Boolean);
  return parts.length ? `<div class="sub running">▶ ${parts.join(" · ")} running</div>` : "";
}

const TARGET_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
  <circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2.5"/><path d="M8 0v3M8 13v3M0 8h3M13 8h3"/></svg>`;

function focusButton(s) {
  const tip = s.in_iterm ? "Focus in iTerm2" : itermTip(s);
  return `<button class="focus-btn" data-focus="${s.pid}" title="${esc(tip)}" aria-label="${esc(tip)}" ${s.in_iterm ? "" : "disabled"}>${TARGET_ICON}</button>`;
}

function renderTable() {
  renderHeader();
  empty.hidden = sessions.length > 0;
  tbody.innerHTML = sessions.map(s => {
    const st = s.stats;
    const title = titleOf(s);
    return `
    <tr class="${s.limit ? "limited" : s.attention ? "idle" : ""} ${s.session_id === selectedId ? "selected" : ""}" data-pid="${s.pid}" data-id="${esc(s.session_id)}">
      <td class="col-focus">${focusButton(s)}</td>
      <td>${statusCell(s)}</td>
      <td class="col-reply">${replyCell(st)}</td>
      <td class="col-session">
        <div class="name">${esc(title)}${itermWarning(s)}</div>
        ${st?.last_prompt ? `<div class="prompt" title="${esc(st.last_prompt)}">“${esc(st.last_prompt)}”</div>` : ""}
        ${runningLine(s)}
      </td>
      <td>${dirCell(s)}</td>
      <td class="col-account">${s.account ? `<span class="account ${s.account === "claude" ? "" : "work"}">${esc(s.account)}</span>` : `<span class="muted">–</span>`}</td>
      <td class="col-context">${contextCell(st)}</td>
      <td class="col-num num">${st ? tokens(st.tokens_total) : "–"}</td>
      <td class="col-num muted num">${ago(s.started_at_ms)}</td>
      <td>${esc(shortModel(st?.model))}</td>
    </tr>`;
  }).join("");
}

// Tree of paths. entries: [path, count, isDir]; a dir entry was searched or listed, not read.
function buildTree(entries) {
  const root = {path: "", kids: new Map()};
  for (const [path, n, isDir] of entries) {
    let node = root;
    for (const part of path.split("/").filter(Boolean)) {
      if (!node.kids.has(part)) node.kids.set(part, {name: part, path: `${node.path}/${part}`, kids: new Map()});
      node = node.kids.get(part);
    }
    if (isDir) node.listed = (node.listed || 0) + n;
    else node.file = (node.file || 0) + n;
  }
  return root;
}

function fileCount(node) {
  let n = node.file ? 1 : 0;
  for (const kid of node.kids.values()) n += fileCount(kid);
  return n;
}

function treeHtml(node, tree, depth = 0) {
  const kids = [...node.kids.values()].sort((a, b) =>
    (!!a.file - !!b.file) || a.name.localeCompare(b.name));
  return kids.map(kid => {
    if (kid.file) {
      return `<li class="file"><span class="path">${esc(kid.name)}</span>${kid.file > 1 ? ` <span class="muted">×${kid.file}</span>` : ""}</li>`;
    }
    // Fold chains of single-child folders into one row: ~/workspace/projects/x/
    let cur = kid;
    let label = kid.name;
    while (!cur.listed && cur.kids.size === 1) {
      const only = cur.kids.values().next().value;
      if (only.file) break;
      cur = only;
      label += `/${only.name}`;
    }
    if (depth === 0) label = tilde(cur.path);
    const listed = cur.listed ? ` <span class="tag">listed</span>` : "";
    if (!cur.kids.size) return `<li class="dir"><span class="path">${esc(label)}/</span>${listed}</li>`;
    const key = `${selectedId}|${tree}|${cur.path}`;
    const open = openDirs.has(key) ? openDirs.get(key) : depth === 0;
    const n = fileCount(cur);
    return `<li class="dir"><details data-key="${esc(key)}" ${open ? "open" : ""}>` +
      `<summary><span class="path">${esc(label)}/</span>${n ? ` <span class="muted">${n}</span>` : ""}${listed}</summary>` +
      `<ul class="tree">${treeHtml(cur, tree, depth + 1)}</ul></details></li>`;
  }).join("");
}

function treeSection(title, tree, entries, emptyText) {
  const files = entries.filter(([, , isDir]) => !isDir).length;
  const dirs = entries.length - files;
  const counts = [files && `${files} file${files > 1 ? "s" : ""}`, dirs && `${dirs} dir${dirs > 1 ? "s" : ""}`].filter(Boolean).join(" · ");
  return `<section><details class="sec" data-section="${tree}" ${secOpen(tree)}>
      <summary><h3>${title} <span class="muted">${counts}</span></h3></summary>
      ${entries.length ? `<ul class="tree root">${treeHtml(buildTree(entries), tree)}</ul>` : `<p class="muted">${emptyText}</p>`}
    </details></section>`;
}

function branchesSection(startCwd, st, act) {
  const all = Object.entries(act.repos || {});
  const inRepo = root => startCwd === root || startCwd?.startsWith(`${root}/`);
  const repos = all.filter(([, r]) => !r.removed)
    .sort(([a], [b]) => (inRepo(b) - inRepo(a)) || a.localeCompare(b));
  const gone = all.filter(([, r]) => r.removed).sort(([a], [b]) => a.localeCompare(b));
  const body = repos.length
    ? `<ul class="repos">${repos.map(([root, {branches}]) => `
        <li><span class="path">${esc(tilde(root))}</span>${inRepo(root) ? ` <span class="tag">start repo</span>` : ""}
          <div class="branches">${branches.length ? branches.map(b => `<span class="path branch">${esc(b)}</span>`).join(" ") : `<span class="muted">branch unknown</span>`}</div></li>`).join("")}</ul>`
    : `<p class="muted">No edits in a git repo${st.live === false ? "" : " yet"}${st.git_branch ? `; ${st.live === false ? "was" : "working"} on <span class="path">${esc(st.git_branch)}</span>` : ""}.</p>`;
  // Edits in folders that are gone (removed worktrees, cleaned-up temp dirs): repo and branch unknowable.
  const key = `${selectedId}|gone`;
  const goneHtml = gone.length ? `
    <details class="gone" data-key="${esc(key)}" ${openDirs.get(key) ? "open" : ""}>
      <summary class="muted">${gone.length} folder${gone.length > 1 ? "s" : ""} with edits no longer exist${gone.length > 1 ? "" : "s"} (e.g. removed worktrees)</summary>
      <ul class="repos">${gone.map(([root, {branches}]) =>
        `<li><span class="path">${esc(tilde(root))}</span>${branches.map(b => ` <span class="path branch">${esc(b)}</span>`).join("")}</li>`).join("")}</ul>
    </details>` : "";
  return `<section><details class="sec" data-section="branches" ${secOpen("branches")}><summary><h3>Branches with edits <span class="muted">incl. sub-agents</span></h3></summary>${body}${goneHtml}</details></section>`;
}

// The detail is fetched for the selected session only, and again only when it changes.
function selectedActivity(s) {
  const key = `${s.pid}|${s.stats?.detail_version}`;
  if (activity.key !== key && activity.loading !== key) {
    activity.loading = key;
    fetch(`/api/sessions/${s.pid}/detail`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (activity.loading !== key) return;  // superseded by a newer fetch
        activity = {key, data, loading: null, pid: s.pid};
        renderDetail();
      })
      .catch(() => { if (activity.loading === key) activity.loading = null; });
  }
  return activity.pid === s.pid ? activity.data : null;
}

function tokensSection(st) {
  const t = st.tokens || {};
  return `
    <section><details class="sec" data-section="tokens" ${secOpen("tokens")}>
      <summary><h3>Tokens</h3></summary>
      <dl class="num">
        <dt>Context ${st.live === false ? "at end" : "now"}</dt><dd>${tokens(st.context_tokens)}${st.context_window ? ` of ${tokens(st.context_window)} (${st.context_pct}%)` : ""}</dd>
        <dt>Requests</dt><dd>${st.requests ?? "–"}</dd>
      </dl>
      <table class="cost">
        <thead><tr><th></th><th class="col-num">Tokens</th><th class="col-num">Cost</th></tr></thead>
        <tbody>
          ${[["Input (uncached)", "input_tokens", "input"],
             ["Cache read", "cache_read_input_tokens", "cache_read"],
             ["Cache write", "cache_creation_input_tokens", "cache_write"],
             ["Output", "output_tokens", "output"]].map(([label, tk, ck]) =>
            `<tr><td>${label}</td><td class="col-num num">${tokens(t[tk])}</td><td class="col-num num">${usd(st.cost_parts?.[ck])}</td></tr>`).join("")}
          <tr class="total"><td>Total</td><td class="col-num num">${tokens(st.tokens_total)}</td><td class="col-num num">${usd(st.cost_usd)}</td></tr>
        </tbody>
      </table>
      ${st.main && st.main.tokens_total !== st.tokens_total ? `
      <table class="cost split">
        <tbody>
          <tr><td>Main session</td><td class="col-num num">${tokens(st.main.tokens_total)}</td><td class="col-num num">${usd(st.main.cost_usd)}</td></tr>
          <tr><td>Sub-agents</td><td class="col-num num">${tokens(st.tokens_total - st.main.tokens_total)}</td><td class="col-num num">${usd(st.cost_usd - st.main.cost_usd)}</td></tr>
        </tbody>
      </table>` : ""}
      <p class="footnote">Includes sub-agents. Context is the main session's. API list-price estimate; subscription sessions aren't billed per token.${st.cost_complete === false ? " Some models have no known price and are left out." : ""}</p>
    </details></section>`;
}

const STATE_LABEL = {running: "running", done: "done", failed: "failed", killed: "stopped", stopped: "stopped",
  interrupted: "interrupted", ended: "ended"};

function statePill(state) {
  return `<span class="pill state ${esc(state)}">${esc(STATE_LABEL[state] || state)}</span>`;
}

function since(iso) {
  return iso ? ago(Date.parse(iso)) : "–";
}

// Running ones and the newest few; the rest behind a "N more" toggle.
function capped(items, rowHtml, key, keep = 8) {
  const shown = items.filter((x, i) => x.state === "running" || i < keep);
  const rest = items.filter(x => !shown.includes(x));
  const moreKey = `${selectedId}|${key}|more`;
  return {shown: shown.map(rowHtml).join(""), more: rest.length ? `
    <details class="more" data-key="${esc(moreKey)}" ${openDirs.get(moreKey) ? "open" : ""}>
      <summary class="muted">${rest.length} older</summary>${rest.map(rowHtml).join("")}</details>` : ""};
}

function byRecency(items, field) {
  return [...items].sort((a, b) => (b.state === "running") - (a.state === "running") ||
    String(b[field] || "").localeCompare(String(a[field] || "")));
}

function subagentsSection(agents, live) {
  if (!agents?.length) return "";
  const running = agents.filter(a => a.state === "running").length;
  const row = a => `
    <div class="task">
      <div class="task-head">
        ${statePill(a.state)}
        <span class="task-name">${a.depth > 1 ? "↳ " : ""}${esc(a.name || a.type || a.id)}</span>
        <span class="num task-figures">${tokens(a.tokens_total)} · ${usd(a.cost_usd)}</span>
      </div>
      <div class="sub">${esc(a.description || "")}</div>
      <div class="sub num">${a.state === "running" ? `running ${duration(a.started, new Date().toISOString())} · last step ${since(a.last_activity)} ago`
        : `${duration(a.started, a.last_activity)} · ended ${since(a.last_activity)} ago`}${a.type && a.name ? ` · ${esc(a.type)}` : ""}${a.background ? "" : " · foreground"}</div>
    </div>`;
  const {shown, more} = capped(byRecency(agents, "last_activity"), row, "agents");
  return `<section><details class="sec" data-section="agents" ${secOpen("agents")}>
    <summary><h3>Sub-agents <span class="muted">${live && running ? `${running} running · ` : ""}${agents.length} total</span></h3></summary>
    ${shown}${more}
  </details></section>`;
}

// The output tail of an opened shell: fetched on open, then every second while it runs.
function shellOutput(sid, sh) {
  const key = `${sid}|${sh.id}`;
  const o = outputs.get(key);
  // Refetched every second while it runs, and once more when it stops (the last lines).
  const stale = !o || !o.loading && ((sh.state === "running" && Date.now() - o.at > 1000) || (o.state && o.state !== sh.state));
  if (stale) {
    outputs.set(key, {...(o || {}), loading: true, at: o?.at || 0});
    fetch(`/api/output?session_id=${encodeURIComponent(sid)}&task_id=${encodeURIComponent(sh.id)}`)
      .then(r => r.ok ? r.json() : r.json().then(b => ({error: b.detail || `HTTP ${r.status}`})))
      .then(data => { outputs.set(key, {...data, at: Date.now(), loading: false}); renderDetail(); })
      .catch(() => outputs.set(key, {...(outputs.get(key) || {}), loading: false, at: Date.now()}));
  }
  if (!o || o.text == null && !o.error) return `<p class="muted">Loading output…</p>`;
  if (o.error) return `<p class="muted">${esc(o.error)}.</p>`;
  if (!o.exists) return `<p class="muted">The output file is gone (temp files are cleared on restart).</p>`;
  if (!o.text.trim()) return `<p class="muted">No output${sh.state === "running" ? " yet" : ""}.</p>`;
  return `${o.truncated ? `<p class="footnote">Last ${tokens(o.text.length)} characters of ${tokens(o.size)} bytes.</p>` : ""}` +
    `<pre class="output" data-out="${esc(key)}">${esc(o.text)}</pre>`;
}

function shellsSection(shells, sid, live) {
  if (!shells?.length) return "";
  const running = shells.filter(sh => sh.state === "running").length;
  const row = sh => {
    const key = `${selectedId}|shell|${sh.id}`;
    const open = !!openDirs.get(key);
    const outcome = sh.state === "running"
      ? (sh.elapsed_s != null ? `for ${ago(Date.now() - sh.elapsed_s * 1000)}` : `since ${since(sh.started)} ago`)
      : sh.exit_code != null ? `exit ${sh.exit_code}` : "";
    return `
    <details class="task shell" data-key="${esc(key)}" ${open ? "open" : ""}>
      <summary class="task-head">
        ${statePill(sh.state)}
        <span class="task-name">${esc(sh.description || sh.command || sh.id)}</span>
        <span class="num task-figures">${outcome}</span>
      </summary>
      <code class="cmd">${esc(sh.command || "(command not recorded)")}</code>
      <div class="sub num">${sh.kind === "monitor" ? "monitor · " : ""}started ${when(sh.started)}${sh.ended ? ` · ended ${when(sh.ended)}` : ""}${sh.pid ? ` · pid ${sh.pid}` : ""}</div>
      ${!open ? "" : sh.kind === "monitor" && !sh.output_file
        ? `<p class="muted">A monitor's events go to the session as notifications; it has no output file.</p>`
        : shellOutput(sid, sh)}
    </details>`;
  };
  const {shown, more} = capped(byRecency(shells, "started"), row, "shells");
  return `<section><details class="sec" data-section="shells" ${secOpen("shells")}>
    <summary><h3>Background shells <span class="muted">${live && running ? `${running} running · ` : ""}${shells.length} total</span></h3></summary>
    ${shown}${more}
  </details></section>`;
}

// Child processes of a running session: Bash commands in progress, and others such as MCP servers.
function processesSection(s) {
  const procs = s.processes || [];
  const commands = procs.filter(p => p.tool && !p.shell_id);
  const others = procs.filter(p => !p.tool);
  if (!commands.length && !others.length) return "";
  const key = `${selectedId}|procs`;
  return `<section><details class="sec" data-section="running" ${secOpen("running")}>
    <summary><h3>Running now</h3></summary>
    ${commands.length ? commands.map(p => `
      <div class="task">
        <code class="cmd">${esc(p.command)}</code>
        <div class="sub num">for ${ago(Date.now() - p.elapsed_s * 1000)} · pid ${p.pid}</div>
      </div>`).join("") : `<p class="muted">No command running.</p>`}
    ${others.length ? `
      <details class="more" data-key="${esc(key)}" ${openDirs.get(key) ? "open" : ""}>
        <summary class="muted">${plural(others.length, "other child process")} (MCP servers, …)</summary>
        ${others.map(p => `<div class="sub path">${esc(p.command)} <span class="muted">pid ${p.pid}</span></div>`).join("")}
      </details>` : ""}
  </details></section>`;
}

// Tool-use explorer: every tool call of the session and its sub-agents, from the server's index.
const CATEGORY_LABEL = {web: "Web", commands: "Commands", files: "Files", mcp: "MCP", agents: "Sub-agent launches", other: "Other"};
const EX_PAGE = 100;
let explorer = newExplorer(null);

function newExplorer(sid) {
  return {sid, mode: "grouped", cat: "", q: "", agent: "", summary: null, sumAt: 0, sumLoading: false,
          lists: new Map()};  // "<category>|<group>" or "timeline" -> {items, total, at, loading}
}

function exUrl(path, params) {
  const qs = Object.entries(params).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  return `/api/tools/${encodeURIComponent(explorer.sid)}/${path}${qs ? `?${qs}` : ""}`;
}

function exLoadSummary() {
  const ex = explorer;
  ex.sumLoading = true;
  fetch(exUrl("summary", {q: ex.q, agent: ex.agent})).then(r => r.ok ? r.json() : null).then(data => {
    if (ex !== explorer) return;
    Object.assign(ex, {summary: data, sumAt: Date.now(), sumLoading: false});
    renderDetail();
  }).catch(() => { ex.sumLoading = false; });
}

// A list of calls: the timeline, or one group's calls. `more`: append the next page.
function exLoadList(key, more = false) {
  const ex = explorer;
  const list = ex.lists.get(key) || {items: [], total: null, at: 0};
  const [category, group] = key === "timeline" ? [ex.cat, null] : key.split("|");
  const offset = more ? list.items.length : 0;
  const limit = more ? EX_PAGE : Math.max(EX_PAGE, list.items.length);  // a refresh keeps what was shown
  ex.lists.set(key, {...list, loading: true});
  fetch(exUrl("events", {category, group, q: ex.q, agent: ex.agent, offset, limit})).then(r => r.ok ? r.json() : null).then(data => {
    if (ex !== explorer || !data) return;
    ex.lists.set(key, {items: more ? [...list.items, ...data.items] : data.items, total: data.total, at: Date.now(), loading: false});
    renderDetail();
  }).catch(() => ex.lists.set(key, {...list, loading: false}));
}

function exResetLists() {
  explorer.lists = new Map();
  explorer.summary = null;
  explorer.sumAt = 0;
}

// Fetches what the open parts show; a running session's every 3 s.
function exEnsure(live) {
  const ex = explorer;
  const stale = at => !at || (live && Date.now() - at > 3000);
  if (!ex.sumLoading && stale(ex.sumAt)) exLoadSummary();
  const prefix = `${ex.sid}|exg|`;  // open groups: "<sid>|exg|<category>|<group>"
  const keys = ex.mode === "timeline" ? ["timeline"]
    : [...openDirs].filter(([k, open]) => open && k.startsWith(prefix)).map(([k]) => k.slice(prefix.length));
  for (const key of keys) {
    const list = ex.lists.get(key);
    if (!list?.loading && stale(list?.at)) exLoadList(key);
  }
}

function statusBadge(e, live) {
  if (e.status === "ok") return e.exit_code ? `<span class="ev-status error">exit ${e.exit_code}</span>` : "";
  if (e.status === "pending") return `<span class="ev-status ${live ? "running" : ""}">${live ? "running" : "no result"}</span>`;
  const label = e.status === "error" && e.exit_code != null ? `exit ${e.exit_code}` : e.status;
  return `<span class="ev-status ${esc(e.status)}">${esc(label)}</span>`;
}

function prettyJson(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) { return text || ""; }
}

function eventRow(e, live, agents) {
  const key = `${explorer.sid}|exe|${e.id}`;
  const open = !!openDirs.get(key);
  const who = e.agent_id ? (agents[e.agent_id] || "sub-agent") : "";
  const time = e.ts ? new Date(e.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "";
  return `
    <details class="event" data-key="${esc(key)}" ${open ? "open" : ""}>
      <summary>
        <span class="ev-time num" title="${esc(when(e.ts))}">${esc(time)}</span>
        <span class="ev-tool">${esc(e.tool.startsWith("mcp__") ? e.grp : e.tool)}</span>
        <span class="ev-summary">${highlight(e.summary || "", explorer.q)}</span>
        ${who ? `<span class="ev-agent" title="Called by a sub-agent">${esc(who)}</span>` : ""}
        ${statusBadge(e, live)}
      </summary>
      ${open ? `
      <div class="ev-body">
        <div class="sub">${esc(e.tool)} · ${esc(when(e.ts))}${e.cwd ? ` · in <span class="path">${esc(tilde(e.cwd))}</span>` : ""}${e.agent_ref ? ` · launched ${esc(agents[e.agent_ref] || e.agent_ref)}` : ""}</div>
        <pre class="output">${esc(prettyJson(e.detail))}</pre>
        ${e.preview ? `<div class="sub">Result${e.preview.length >= 600 ? " (start)" : ""}</div><pre class="output">${esc(e.preview)}</pre>` : ""}
      </div>` : ""}
    </details>`;
}

function eventList(key, live, agents) {
  const list = explorer.lists.get(key);
  if (!list || list.total == null) return `<p class="muted">Loading…</p>`;
  if (!list.items.length) return `<p class="muted">No calls.</p>`;
  const more = list.total - list.items.length;
  return list.items.map(e => eventRow(e, live, agents)).join("") +
    (more > 0 ? `<button class="more-btn" data-ex-more="${esc(key)}" ${list.loading ? "disabled" : ""}>Show ${Math.min(more, EX_PAGE)} more (${more} left)</button>` : "");
}

function groupedBody(live, agents) {
  const sum = explorer.summary;
  const cats = CATEGORIES_ORDER.filter(c => (!explorer.cat || c === explorer.cat) && sum.categories[c]);
  if (!cats.length) return `<p class="muted">No calls${explorer.q ? " match" : ""}.</p>`;
  return cats.map(cat => {
    const groups = sum.groups.filter(g => g.category === cat);
    const row = g => {
      const key = `${cat}|${g.group}`;
      const dkey = `${explorer.sid}|exg|${key}`;
      const open = !!openDirs.get(dkey);
      return `
        <details class="ex-group" data-key="${esc(dkey)}" ${open ? "open" : ""}>
          <summary>
            <span class="ex-group-name">${highlight(g.group, explorer.q)}</span>
            <span class="num muted">${g.count}</span>
            ${g.errors ? `<span class="ev-status error">${g.errors} failed</span>` : ""}
            <span class="muted ex-last">${g.last_ts ? `${since(g.last_ts)} ago` : ""}</span>
          </summary>
          ${open ? `<div class="ex-events">${eventList(key, live, agents)}</div>` : ""}
        </details>`;
    };
    const top = groups.slice(0, 12);
    const rest = groups.slice(12);
    const moreKey = `${explorer.sid}|exg-more|${cat}`;
    return `<div class="ex-cat">
      <h4>${CATEGORY_LABEL[cat]} <span class="muted num">${sum.categories[cat]}</span></h4>
      ${top.map(row).join("")}
      ${rest.length ? `<details class="more" data-key="${esc(moreKey)}" ${openDirs.get(moreKey) ? "open" : ""}>
        <summary class="muted">${rest.length} more</summary>${rest.map(row).join("")}</details>` : ""}
    </div>`;
  }).join("");
}

const CATEGORIES_ORDER = ["commands", "files", "web", "mcp", "agents", "other"];

function toolsSection(sid, live, agentList) {
  if (explorer.sid !== sid) explorer = newExplorer(sid);
  const open = !!secOpen("tools");
  if (open) exEnsure(live);
  const sum = explorer.summary;
  const agents = Object.fromEntries((agentList || []).map(a => [a.id, a.name || a.description || a.type]));
  const chips = sum ? [["", "All", sum.total], ...CATEGORIES_ORDER.filter(c => sum.categories[c]).map(c => [c, CATEGORY_LABEL[c], sum.categories[c]])]
    .map(([c, label, n]) => `<button class="chip ${explorer.cat === c ? "active" : ""}" data-ex-cat="${c}">${label} <span class="num">${n}</span></button>`).join("") : "";
  const only = explorer.agent ? `<div class="chips"><button class="chip active" data-ex-agent="" title="Show every agent's calls">Only ${esc(explorer.agent === "main" ? "main session" : agents[explorer.agent] || explorer.agent)} ×</button></div>` : "";
  const body = !sum ? `<p class="muted">Loading…</p>`
    : explorer.mode === "timeline" ? eventList("timeline", live, agents) : groupedBody(live, agents);
  return `<section><details class="sec" data-section="tools" ${secOpen("tools")}>
    <summary><h3>Tool use <span class="muted">${sum ? `${sum.total}${explorer.q ? " matching" : ""} calls` : ""} ${explorer.agent ? "of one agent" : "incl. sub-agents"}</span></h3></summary>
    ${open ? `<div class="explorer">
      <div class="ex-bar">
        <div class="seg">
          <button class="${explorer.mode === "grouped" ? "active" : ""}" data-ex-mode="grouped">Grouped</button>
          <button class="${explorer.mode === "timeline" ? "active" : ""}" data-ex-mode="timeline">Timeline</button>
        </div>
        <input type="search" id="ex-q" placeholder="Filter: command, file, URL, tool…" value="${esc(explorer.q)}" autocomplete="off" spellcheck="false">
      </div>
      ${only}
      <div class="chips">${chips}</div>
      ${body}
    </div>` : ""}
  </details></section>`;
}

// Flow: a lane per agent on a shared time axis, its active spans as bars, and every message
// between two agents as a line from sender to receiver. Quiet stretches when nothing ran are cut
// out of the axis. Click a line: the messages between that pair; click a lane: its tool calls.
// The time axis zooms (pinch, ⌘/Ctrl + scroll, buttons, or a drag across a stretch); the lane
// names stay put while the diagram scrolls sideways.
const FLOW_LABEL = 210, FLOW_PAD = 8, FLOW_ZOOM_MAX = 256;
const FLOW_KIND = {prompt: "task", message: "message", idle: "idle notice", handback: "final report",
  notification: "finished", result: "result"};
let flowState = newFlow(null);

function newFlow(sid) {
  return {sid, data: null, at: 0, loading: false, pair: null, between: null, zoom: 1, sc: null};
}

function flowLoad() {
  const fl = flowState;
  fl.loading = true;
  fetch(`/api/flow/${encodeURIComponent(fl.sid)}`).then(r => r.ok ? r.json() : null).then(data => {
    if (fl !== flowState) return;
    Object.assign(fl, {data, at: Date.now(), loading: false});
    renderDetail();
  }).catch(() => { fl.loading = false; });
}

function flowLoadPair() {
  const fl = flowState;
  const [a, b] = fl.pair;
  fl.between = {...(fl.between || {}), loading: true};
  fetch(`/api/flow/${encodeURIComponent(fl.sid)}/between?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)
    .then(r => r.ok ? r.json() : null).then(data => {
      if (fl !== flowState || fl.pair?.[0] !== a || fl.pair?.[1] !== b) return;
      fl.between = {items: data?.items || [], at: Date.now(), loading: false};
      renderDetail();
    }).catch(() => { fl.between = null; });
}

function laneName(l) {
  return l.id === "main" ? "main session" : l.name || l.type || l.id;
}

// Time -> x. The longest quiet stretches (10+ min with nothing running, at most MAX_BREAKS of
// them) are cut down to a fixed-width break; everything else keeps its real length.
function flowScale(lanes, width) {
  const GAP = 10 * 60e3, MAX_BREAKS = 20, BREAK = 14;
  const spans = lanes.flatMap(l => l.segments.map(([a, b]) => [Date.parse(a), Date.parse(b)])).filter(([a]) => a);
  if (!spans.length) return null;
  spans.sort((x, y) => x[0] - y[0]);
  const busy = [];
  for (const [a, b] of spans) {
    const last = busy[busy.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b); else busy.push([a, b]);
  }
  const gaps = busy.slice(1).map((r, i) => r[0] - busy[i][1]);
  const cutoff = Math.max(GAP, [...gaps].sort((x, y) => y - x)[MAX_BREAKS] ?? 0);
  const runs = [[...busy[0]]];
  busy.slice(1).forEach((r, i) => {
    if (gaps[i] > cutoff) runs.push([...r]); else runs[runs.length - 1][1] = r[1];
  });
  const MIN = Math.max(20e3, runs.reduce((n, [a, b]) => n + b - a, 0) / 100);  // a short burst stays visible
  const total = runs.reduce((n, [a, b]) => n + Math.max(b - a, MIN), 0);
  const k = Math.max(0, width - BREAK * (runs.length - 1)) / total;
  let x = 0;
  const parts = runs.map(([a, b]) => { const p = {a, b, x}; x += Math.max(b - a, MIN) * k + BREAK; return p; });
  const at = t => {
    let i = parts.length - 1;
    while (i > 0 && t < parts[i].a) i--;
    const p = parts[i];
    return p.x + Math.min(Math.max(t - p.a, 0), Math.max(p.b - p.a, MIN)) * k;
  };
  const time = x => {  // x -> time, the inverse of at (a break maps to the end of the stretch before it)
    let i = parts.length - 1;
    while (i > 0 && x < parts[i].x) i--;
    const p = parts[i];
    return p.a + Math.min(Math.max(x - p.x, 0) / k, Math.max(p.b - p.a, MIN));
  };
  return {at, time, parts, k, BREAK};
}

function hhmm(t, withDay, withSeconds) {
  const d = new Date(t);
  const time = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", ...(withSeconds ? {second: "2-digit"} : {})});
  return withDay ? `${d.toLocaleDateString([], {month: "short", day: "numeric"})} ${time}` : time;
}

function gapText(ms) {
  const m = Math.round(ms / 60e3);
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
}

const TICK_STEPS = [1, 5, 15, 30, 60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400].map(s => s * 1e3);

function flowSvg(fl, liveAgents) {
  const live = Object.fromEntries((liveAgents || []).map(a => [a.id, a]));
  const lanes = fl.data.lanes.map(l => {
    const state = live[l.id]?.state || l.state;
    const segments = l.segments.map(s => [...s]);
    if (state === "running" && segments.length) segments[segments.length - 1][1] = new Date().toISOString();
    return {...l, state, segments};
  });
  const LABEL = FLOW_LABEL, PAD = FLOW_PAD, LANE = 22, TOP = 24;
  const view = Math.max(600, (detail.clientWidth || 900) - 64) - LABEL;  // the plot's visible width
  const sc = flowScale(lanes, (view - 2 * PAD) * fl.zoom);
  fl.sc = sc;
  if (!sc) return `<p class="muted">No activity recorded.</p>`;
  const width = PAD * 2 + sc.at(sc.parts[sc.parts.length - 1].b);
  const X = t => PAD + sc.at(Date.parse(t));
  const row = Object.fromEntries(lanes.map((l, i) => [l.id, i]));
  const Y = id => TOP + row[id] * LANE + LANE / 2;
  const height = TOP + lanes.length * LANE + 6;
  const pairKey = fl.pair ? fl.pair.join("|") : "";
  const multiDay = new Date(sc.parts[0].a).toDateString() !== new Date(sc.parts[sc.parts.length - 1].b).toDateString();

  // Ticks at round local times (every 1 s … 1 day, whatever leaves ~100 px between labels),
  // plus one at the start of every stretch; the date shows when it changes.
  const step = TICK_STEPS.find(s => s * sc.k >= 100) ?? TICK_STEPS[TICK_STEPS.length - 1];
  let lastLabel = -1e9, lastDay = "", axis = "";
  sc.parts.forEach((p, i) => {
    const x = PAD + p.x;
    if (i > 0) {
      const gap = p.a - sc.parts[i - 1].b;
      axis += `<g class="fl-break"><rect x="${x - sc.BREAK}" y="${TOP - 4}" width="${sc.BREAK}" height="${height}"></rect>` +
        `<line x1="${x - sc.BREAK / 2}" x2="${x - sc.BREAK / 2}" y1="${TOP - 4}" y2="${height}"></line>` +
        `<title>${esc(gapText(gap))} with nothing running (cut out)</title></g>`;
    }
    const tz = new Date(p.a).getTimezoneOffset() * 60e3;
    const ticks = [p.a];
    for (let t = Math.ceil((p.a - tz) / step) * step + tz; t <= p.b; t += step) ticks.push(t);
    for (const t of ticks) {
      const tx = x + (t - p.a) * sc.k;
      if (tx - lastLabel < 80) continue;
      const day = new Date(t).toDateString();
      const edge = tx < 50;  // the first label starts at the edge rather than being cut in half
      axis += `<text class="fl-tick${edge ? " edge" : ""}" x="${edge ? Math.max(tx - 4, 2) : tx}" y="${TOP - 10}">${esc(hhmm(t, multiDay && day !== lastDay, step < 60e3))}</text>`;
      lastLabel = tx;
      lastDay = day;
    }
  });

  let labels = "", rows = "";
  lanes.forEach((l, i) => {
    const y = TOP + i * LANE;
    const name = laneName(l);
    const label = name.length > 26 - l.depth ? name.slice(0, 25 - l.depth) + "…" : name;
    const tip = [name, l.type && l.type !== name ? l.type : "", l.description || "", l.state || "",
      l.external ? "" : "Click: show only its tool calls"].filter(Boolean).join("\n");
    const attrs = `class="fl-lane${explorer.agent === l.id ? " sel" : ""}" ${l.external ? "" : `data-lane="${esc(l.id)}"`}`;
    labels += `<g ${attrs}>
      <rect class="fl-row" x="0" y="${y}" width="${LABEL}" height="${LANE}"></rect>
      <text class="fl-label${l.id === "main" ? " main" : ""}" x="${6 + l.depth * 10}" y="${y + LANE / 2 + 4}">${l.depth > 1 ? "↳ " : ""}${esc(label)}</text>
      <title>${esc(tip)}</title>
    </g>`;
    rows += `<g ${attrs}>
      <rect class="fl-row" x="0" y="${y}" width="${width}" height="${LANE}"></rect>
      ${l.segments.map(([a, b]) => {
        const x1 = X(a), x2 = Math.max(X(b), x1 + 3);
        return `<rect class="fl-bar ${esc(l.external ? "external" : l.id === "main" ? "main" : l.state || "done")}" x="${x1}" y="${y + 6}" width="${x2 - x1}" height="${LANE - 12}" rx="2"></rect>`;
      }).join("")}
      <title>${esc(tip)}</title>
    </g>`;
  });

  const lines = fl.data.messages.filter(m => m.from in row && m.to in row).map(m => {
    const x = X(m.ts), y1 = Y(m.from), y2 = Y(m.to);
    const key = [m.from, m.to].sort().join("|");
    const tip = `${laneName(lanes[row[m.from]])} → ${laneName(lanes[row[m.to]])} · ${FLOW_KIND[m.kind] || m.kind} · ${when(m.ts)}` +
      `${m.title ? `\n${m.title}` : ""}${m.preview ? `\n${m.preview}` : ""}\nClick: every message between these two`;
    return `<g class="fl-msg ${esc(m.kind)}${pairKey === key ? " sel" : ""}" data-pair="${esc(key)}">
      <line class="fl-hit" x1="${x}" x2="${x}" y1="${y1}" y2="${y2}"></line>
      <line class="fl-line" x1="${x}" x2="${x}" y1="${y1}" y2="${y2}"></line>
      <circle class="fl-dot" cx="${x}" cy="${y2}" r="3"></circle>
      <title>${esc(tip)}</title>
    </g>`;
  }).join("");

  const z = fl.zoom < 10 ? fl.zoom.toFixed(1).replace(/\.0$/, "") : Math.round(fl.zoom);
  return `<div class="fl-tools">
      <span class="seg"><button data-flow-zoom="out" title="Zoom out" ${fl.zoom <= 1 ? "disabled" : ""}>−</button><button data-flow-zoom="fit" title="Whole session">Fit</button><button data-flow-zoom="in" title="Zoom in" ${fl.zoom >= FLOW_ZOOM_MAX ? "disabled" : ""}>+</button></span>
      <span class="muted num">×${z}</span>
      <span class="sub">Zoom: pinch or ⌘/Ctrl + scroll, or drag across a stretch. Scroll sideways (Shift + scroll) to move along.</span>
    </div>
    <div class="flow-wrap" data-keep-scroll="flow"><div class="fl-grid">
      <svg class="fl-labels" width="${LABEL}" height="${height}">${labels}</svg>
      <svg class="flow fl-plot${pairKey ? " has-sel" : ""}" width="${width}" height="${height}">${axis}${rows}${lines}<rect class="fl-brush" x="0" y="0" width="0" height="${height}"></rect></svg>
    </div></div>`;
}

// Zooms the time axis by `factor`, keeping the moment under `clientX` (default: the middle) in place.
function flowZoom(factor, clientX) {
  const fl = flowState;
  const wrap = detail.querySelector(".flow-wrap");
  if (!fl.sc || !wrap) return;
  const zoom = Math.min(FLOW_ZOOM_MAX, Math.max(1, fl.zoom * factor));
  if (zoom === fl.zoom) return;
  const left = wrap.getBoundingClientRect().left + wrap.clientLeft + FLOW_LABEL;
  const off = clientX == null ? (wrap.clientWidth - FLOW_LABEL) / 2 : Math.max(0, clientX - left);
  const t = fl.sc.time(wrap.scrollLeft + off - FLOW_PAD);
  flowShow(zoom, t, off);
}

// Re-draws at `zoom` and scrolls so time `t` sits `off` px into the visible plot.
function flowShow(zoom, t, off) {
  flowState.zoom = zoom;
  renderDetail();
  const wrap = detail.querySelector(".flow-wrap");
  if (wrap && flowState.sc) wrap.scrollLeft = FLOW_PAD + flowState.sc.at(t) - off;
}

function flowPairPanel(fl, lanes) {
  if (!fl.pair) return "";
  const byId = Object.fromEntries(lanes.map(l => [l.id, laneName(l)]));
  const [a, b] = fl.pair;
  const items = fl.between?.items;
  const days = new Set((items || []).map(m => new Date(m.ts).toDateString())).size > 1;
  const body = !items ? `<p class="muted">Loading…</p>` : !items.length ? `<p class="muted">No messages.</p>` : items.map(m => {
    const key = `${fl.sid}|fm|${m.n}`;
    const open = !!openDirs.get(key);
    const first = (m.text || "").split("\n").find(Boolean) || "";
    return `<details class="event fl-item" data-key="${esc(key)}" ${open ? "open" : ""}>
      <summary>
        <span class="ev-time num" title="${esc(when(m.ts))}">${esc(days ? hhmm(Date.parse(m.ts), true)
          : new Date(m.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}))}</span>
        <span class="fl-dir">${esc(byId[m.from] || m.from)} → ${esc(byId[m.to] || m.to)}</span>
        <span class="ev-status fl-kind ${esc(m.kind)}">${esc(FLOW_KIND[m.kind] || m.kind)}</span>
        <span class="ev-summary">${esc(m.title || first)}</span>
      </summary>
      ${open ? `<pre class="output">${esc(m.text || "(empty)")}</pre>` : ""}
    </details>`;
  }).join("");
  return `<div class="fl-pair">
    <div class="fl-pair-head"><h4>${esc(byId[a] || a)} ↔ ${esc(byId[b] || b)} <span class="muted num">${items ? items.length : ""}</span></h4>
      <button class="chip" data-flow-clear title="Clear the selection">×</button></div>
    ${body}
  </div>`;
}

function flowSection(sid, live, agentList) {
  if (!agentList?.length) return "";
  if (flowState.sid !== sid) flowState = newFlow(sid);
  const fl = flowState;
  const open = !!secOpen("flow");
  if (open && !fl.loading && (!fl.at || (live && Date.now() - fl.at > 5000))) flowLoad();
  if (open && fl.pair && !fl.between?.loading && (!fl.between || (live && Date.now() - fl.between.at > 5000))) flowLoadPair();
  if (!open) fl.sc = null;
  const n = fl.data?.messages.length;
  return `<section><details class="sec" data-section="flow" ${secOpen("flow")}>
    <summary><h3>Flow <span class="muted">${fl.data ? `${fl.data.lanes.length - 1} agents · ${n} messages` : ""}</span></h3></summary>
    ${open ? (!fl.data ? `<p class="muted">Loading…</p>` : `
      <p class="sub fl-help">Bars: when each agent worked. Lines: messages, sender to receiver (dot). Click a line for the messages between two agents, a lane for its tool calls.</p>
      ${flowSvg(fl, agentList)}
      ${flowPairPanel(fl, fl.data.lanes)}`) : ""}
  </details></section>`;
}

function activitySections(startCwd, st, act) {
  const edited = Object.entries(act?.files_edited || {}).map(([p, n]) => [p, n, false]);
  const read = [
    ...Object.entries(act?.files_read || {}).map(([p, n]) => [p, n, false]),
    ...Object.entries(act?.dirs_listed || {}).map(([p, n]) => [p, n, true]),
  ];
  const none = act ? "None." : "Loading…";
  return `
    ${act ? branchesSection(startCwd, st, act) : ""}
    ${treeSection("Files edited", "edited", edited, none)}
    ${treeSection("Files read", "read", read, none)}
    ${st.last_prompt ? `<section><details class="sec" data-section="prompt" ${secOpen("prompt")}><summary><h3>Last prompt</h3></summary><p class="prompt-full">${esc(st.last_prompt)}</p></details></section>` : ""}`;
}

// The detail opens as a modal over the list; a click outside it, × or Esc closes it.

function setDetail(html) {
  if (overlay.hidden === !!html) {
    overlay.hidden = !html;
    document.body.classList.toggle("modal-open", !!html);
    if (html) detail.scrollTop = 0;
  }
  // Only touch the DOM on a real change, so scroll position and text selection survive updates.
  if (html !== lastDetailHtml) {
    // Output panes stay where the reader left them, or follow new lines when scrolled to the end.
    const panes = new Map([...detail.querySelectorAll("pre[data-out]")].map(pre =>
      [pre.dataset.out, pre.scrollHeight - pre.scrollTop - pre.clientHeight < 4 ? null : pre.scrollTop]));
    const scrolled = new Map([...detail.querySelectorAll("[data-keep-scroll]")].map(el =>
      [el.dataset.keepScroll, [el.scrollLeft, el.scrollTop]]));
    const active = detail.contains(document.activeElement) && document.activeElement.id ? document.activeElement : null;
    const caret = active && [active.selectionStart, active.selectionEnd];
    detail.innerHTML = html;
    lastDetailHtml = html;
    if (active) {  // typing in the explorer filter goes on while the panel updates
      const again = document.getElementById(active.id);
      again?.focus();
      try { again?.setSelectionRange(...caret); } catch (_) {}
    }
    for (const el of detail.querySelectorAll("[data-keep-scroll]")) {
      const at = scrolled.get(el.dataset.keepScroll);
      if (at) [el.scrollLeft, el.scrollTop] = at;
    }
    for (const pre of detail.querySelectorAll("pre[data-out]")) {
      const top = panes.get(pre.dataset.out);
      pre.scrollTop = top == null ? pre.scrollHeight : top;
    }
  }
}

function renderDetail() {
  if (!selectedId) return setDetail("");
  const s = sessions.find(x => x.session_id === selectedId);
  if (!s) return renderClosedDetail();
  const st = s.stats || {};
  const act = selectedActivity(s);
  setDetail(`
    <div class="detail-head">
      <div>
        <h2>${esc(titleOf(s))}</h2>
      </div>
      <button class="close" data-action="close" title="Close">×</button>
    </div>
    <div class="actions">
      <button class="with-icon" data-action="focus" ${s.in_iterm ? "" : `disabled title="${esc(itermTip(s))}"`}>${TARGET_ICON} Focus in iTerm2</button>
      ${forkButton(s.session_id, titleOf(s))}
    </div>

    <section><details class="sec" data-section="overview" ${secOpen("overview")}>
      <summary><h3>Overview</h3></summary>
      <dl>
        <dt>Status</dt><dd>${statusCell(s)}</dd>
        <dt>Model</dt><dd>${esc(st.model || "–")}</dd>
        <dt>Name</dt><dd class="path">${esc(s.name || "–")}</dd>
        <dt>Account</dt><dd class="path">${esc(s.account || "–")}</dd>
        <dt>Started</dt><dd>${ago(s.started_at_ms)} ago</dd>
        <dt>Last activity</dt><dd>${st.last_activity ? `${ago(Date.parse(st.last_activity))} ago` : "–"}</dd>
        <dt>Session id</dt><dd class="path">${esc(s.session_id)}</dd>
        <dt>Terminal</dt><dd>${esc(terminalText(s))}${itermWarning(s)}</dd>
        <dt>Process</dt><dd class="path">pid ${s.pid} · ${esc(s.tty || "no tty")} · v${esc(s.version || "?")}</dd>
      </dl>
    </details></section>

    ${processesSection(s)}
    ${subagentsSection(act?.subagents, true)}
    ${flowSection(s.session_id, true, act?.subagents)}
    ${shellsSection(act?.shells, s.session_id, true)}
    ${toolsSection(s.session_id, true, act?.subagents)}
    ${tokensSection(st)}
    ${activitySections(s.start_cwd, st, act)}
  `);
}

// A session that isn't running: its transcript as indexed, fetched once per index update.
function renderClosedDetail() {
  const item = history_.items.find(i => i.session_id === selectedId);
  const key = `${selectedId}|${item?.last_ts}|${item?.requests}`;
  if (closed.key !== key && closed.loading !== key) {
    closed.loading = key;
    fetch(`/api/history/${encodeURIComponent(selectedId)}`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (closed.loading !== key) return;
        closed = {key, data, loading: null};
        renderDetail();
      })
      .catch(() => { if (closed.loading === key) closed.loading = null; });
  }
  const d = closed.data?.session_id === selectedId ? closed.data : null;
  if (!d) {
    return setDetail(closed.loading ? `<p class="muted">Loading…</p>` : "");
  }
  const st = {...d.stats, live: false};
  const cwd = st.cwd && st.cwd !== d.start_cwd ? st.cwd : null;
  setDetail(`
    <div class="detail-head">
      <div><h2>${esc(st.title || st.last_prompt || d.session_id)}</h2></div>
      <button class="close" data-action="close" title="Close">×</button>
    </div>
    <div class="actions">${forkButton(d.session_id, st.title || "")}</div>

    <section><details class="sec" data-section="overview" ${secOpen("overview")}>
      <summary><h3>Overview</h3></summary>
      <dl>
        <dt>Status</dt><dd><span class="pill closed">closed</span></dd>
        <dt>Model</dt><dd>${esc(st.model || "–")}</dd>
        <dt>Started</dt><dd>${when(st.first_activity)}</dd>
        <dt>Last activity</dt><dd>${when(st.last_activity)} · ${st.last_activity ? `${ago(Date.parse(st.last_activity))} ago` : ""}</dd>
        <dt>Duration</dt><dd>${duration(st.first_activity, st.last_activity)}</dd>
        <dt>Directory</dt><dd class="path">${esc(tilde(d.start_cwd || "–"))}${cwd ? ` → ${esc(tilde(cwd))}` : ""}</dd>
        <dt>Session id</dt><dd class="path">${esc(d.session_id)}</dd>
        <dt>Transcript</dt><dd class="path">${esc(tilde(d.path))}</dd>
      </dl>
    </details></section>
    ${subagentsSection(d.activity.subagents, false)}
    ${flowSection(d.session_id, false, d.activity.subagents)}
    ${shellsSection(d.activity.shells, d.session_id, false)}
    ${toolsSection(d.session_id, false, d.activity.subagents)}
    ${tokensSection(st)}
    ${activitySections(d.start_cwd, st, d.activity)}
  `);
}

// Escapes text and wraps the search words in <mark>.
function highlight(text, q) {
  const terms = q.split(/\s+/).filter(Boolean).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return esc(text);
  const re = new RegExp(`(${terms.join("|")})`, "giu");
  return String(text ?? "").split(re).map((part, i) => i % 2 ? `<mark>${esc(part)}</mark>` : esc(part)).join("");
}

function renderHistory() {
  const h = history_;
  const q = h.q;
  searchCount.textContent = q && h.total != null ? `${h.total} matching` : "";
  const ix = h.indexing;
  indexing.hidden = !ix?.running;
  if (ix?.running) {
    const pct = ix.bytes_total ? Math.floor(100 * ix.bytes_done / ix.bytes_total) : 0;
    indexing.textContent = `Indexing transcripts: ${ix.files_done} of ${ix.files_total} files (${pct}%). ` +
      `Sessions appear as they are read; later starts only read what changed.`;
  }
  const running = new Set(sessions.map(s => s.session_id));
  historyBody.innerHTML = h.items.map(i => {
    const live = running.has(i.session_id);
    const title = i.title || i.last_prompt || i.session_id;
    const cwd = i.cwd && i.cwd !== i.start_cwd ? i.cwd : null;
    return `
    <tr data-id="${esc(i.session_id)}" class="${i.session_id === selectedId ? "selected" : ""}">
      <td class="when">${when(i.last_ts)}${live ? ` <span class="pill live">running</span>` : ""}</td>
      <td class="col-session">
        <div class="name">${highlight(title, q)}</div>
        ${i.title && i.last_prompt ? `<div class="prompt" title="${esc(i.last_prompt)}">“${esc(i.last_prompt)}”</div>` : ""}
      </td>
      <td><span class="path">${highlight(tilde(i.start_cwd || "–"), q)}</span>${cwd ? `<div class="sub">ended in <span class="path">${highlight(tilde(cwd), q)}</span></div>` : ""}</td>
      <td class="col-num muted num">${duration(i.first_ts, i.last_ts)}</td>
      <td class="col-num num">${tokens(i.tokens_total)}</td>
      <td>${esc(shortModel(i.model))}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">${
    q ? `No sessions match “${esc(q)}”.` : ix?.running ? "Indexing…" : "No transcripts on disk."}</td></tr>`;
  const from = h.total ? (h.page - 1) * h.size + 1 : 0;
  const to = Math.min(h.total || 0, h.page * h.size);
  pager.innerHTML = h.total > h.size ? `
    <button data-page="${h.page - 1}" ${h.page > 1 ? "" : "disabled"}>‹ Newer</button>
    <span class="num">${from}–${to} of ${h.total}</span>
    <button data-page="${h.page + 1}" ${to < h.total ? "" : "disabled"}>Older ›</button>` : "";
}

async function loadHistory(page = history_.page) {
  const seq = ++historySeq;
  try {
    const q = encodeURIComponent(history_.q);
    const resp = await fetch(`/api/history?page=${page}&size=${history_.size}&q=${q}`);
    if (!resp.ok || seq !== historySeq) return;
    const body = await resp.json();
    if (seq !== historySeq) return;
    history_ = {...history_, page: body.page, total: body.total, all: body.all, items: body.items,
                indexing: body.indexing, loadedAt: Date.now()};
    render();
  } catch (_) { /* server restarting: the next tick retries */ }
}

function render() {
  renderTable();
  if (view === "all") renderHistory();
  renderDetail();
}

let toastTimer;
function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast show ${isError ? "error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2500);
}

const FORK_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
  <circle cx="4" cy="3" r="1.6"/><circle cx="12" cy="3" r="1.6"/><circle cx="8" cy="13" r="1.6"/>
  <path d="M4 4.6v1.4a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2V4.6M8 8v3.4"/></svg>`;

// Fork asks for the new session's name first (prefilled), then opens the tab.
let forking = null;  // {sid, name} while the name field is open

function forkButton(sid, title) {
  if (forking?.sid === sid) {
    return `<span class="fork-form">
      <input id="fork-name" value="${esc(forking.name)}" placeholder="Name of the fork" autocomplete="off" spellcheck="false" title="Shown in the prompt box, the /resume picker and the tab title">
      <button data-action="fork-go" data-sid="${esc(sid)}">Fork</button>
      <button data-action="fork-cancel">Cancel</button>
    </span>`;
  }
  const tip = "New iTerm2 tab running claude --resume <id> --fork-session from the session's start directory. " +
    "The fork gets the whole conversation so far under a new id and name; this session is left as it is.";
  return `<button class="with-icon" data-action="fork" data-sid="${esc(sid)}" data-title="${esc(title || "")}" title="${esc(tip)}">${FORK_ICON} Fork</button>`;
}

function forkAsk(sid, title) {
  forking = {sid, name: title ? `${title} (fork)` : ""};
  renderDetail();
  const input = document.getElementById("fork-name");
  input?.focus();
  input?.select();
}

async function forkSession(btn) {
  const {sid, name} = forking;
  if (btn) btn.disabled = true;  // one click, one tab
  try {
    const resp = await fetch(`/api/fork/${encodeURIComponent(sid)}`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name})});
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) return toast(body.detail || body.error || `Fork failed (${resp.status})`, true);
    forking = null;
    renderDetail();
    toast(`Forked${name ? ` as “${name}”` : ""} in a new iTerm2 tab`);
  } catch (err) {
    toast(`Fork failed: ${err}`, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function focusSession(pid) {
  const s = sessions.find(x => x.pid === pid);
  try {
    const resp = await fetch(`/api/sessions/${pid}/focus`, {method: "POST"});
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      toast(body.detail || body.error || `Focus failed (${resp.status})`, true);
      return;
    }
    toast(`Focused ${s ? s.name : pid} in iTerm2`);
  } catch (err) {
    toast(`Focus failed: ${err}`, true);
  }
}

function select(id) {
  if (id !== selectedId) forking = null;
  selectedId = id;
  const h = view === "all" ? (id ? `#all/${encodeURIComponent(id)}` : "#all") : (id ? `#${encodeURIComponent(id)}` : "");
  history.replaceState(null, "", h || location.pathname);
  render();
}

function setView(v) {
  view = v;
  select(selectedId);
  if (v === "all") {
    loadHistory();
    search.focus();
  }
}

tabs.addEventListener("click", ev => {
  const b = ev.target.closest("button[data-view]");
  if (b) setView(b.dataset.view);
});

let searchTimer;
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    history_.q = search.value.trim();
    loadHistory(1);
  }, 120);
});
search.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && search.value) {
    search.value = "";
    search.dispatchEvent(new Event("input"));
  }
});
document.addEventListener("keydown", ev => {  // "/" jumps to the search box, as on GitHub
  if (ev.key === "/" && view === "all" && overlay.hidden && document.activeElement !== search) {
    ev.preventDefault();
    search.focus();
  }
});

pager.addEventListener("click", ev => {
  const b = ev.target.closest("button[data-page]");
  if (b && !b.disabled) loadHistory(Number(b.dataset.page));
});

historyBody.addEventListener("click", ev => {
  const row = ev.target.closest("tr[data-id]");
  if (row) select(row.dataset.id);
});

function rowSession(ev) {
  const row = ev.target.closest("tr[data-id]");
  return row ? sessions.find(x => x.session_id === row.dataset.id) : null;
}

tbody.addEventListener("click", ev => {
  const btn = ev.target.closest("button[data-focus]");
  if (btn) {  // the target button focuses; the rest of the row opens the detail
    if (!btn.disabled) focusSession(Number(btn.dataset.focus));
    return;
  }
  const s = rowSession(ev);
  if (s) select(s.session_id);
});

overlay.addEventListener("click", ev => {
  if (ev.target === overlay) select(null);  // outside the modal
});

document.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && !overlay.hidden && document.activeElement !== search) select(null);
});

detail.addEventListener("click", ev => {
  if (flowDragged) {  // the end of a drag-to-zoom, not a click on what it ended over
    flowDragged = false;
    return;
  }
  const zoomBtn = ev.target.closest("[data-flow-zoom]")?.dataset.flowZoom;
  if (zoomBtn) flowZoom(zoomBtn === "in" ? 2 : zoomBtn === "out" ? 0.5 : 1 / flowState.zoom);
  const summary = ev.target.closest("summary");
  const sec = summary?.parentElement.dataset.section;
  if (sec) {  // runs before the browser toggles it
    if (summary.parentElement.open) openSections.delete(sec); else openSections.add(sec);
    try { localStorage.setItem("openSections", JSON.stringify([...openSections])); } catch (_) {}
  }
  if (summary?.parentElement.dataset.key) {  // runs before the browser toggles it
    openDirs.set(summary.parentElement.dataset.key, !summary.parentElement.open);
    setTimeout(renderDetail);  // an opened shell shows its output
  }
  const mode = ev.target.closest("[data-ex-mode]")?.dataset.exMode;
  if (mode && mode !== explorer.mode) {
    explorer.mode = mode;
    renderDetail();
  }
  const cat = ev.target.closest("[data-ex-cat]")?.dataset.exCat;
  if (cat != null && cat !== explorer.cat) {
    explorer.cat = cat;
    explorer.lists.delete("timeline");
    renderDetail();
  }
  const agent = ev.target.closest("[data-ex-agent]")?.dataset.exAgent;
  if (agent != null) showAgentCalls(agent);
  const lane = ev.target.closest("[data-lane]")?.dataset.lane;
  const pair = ev.target.closest("[data-pair]")?.dataset.pair;
  if (pair) {
    flowState.pair = flowState.pair?.join("|") === pair ? null : pair.split("|");
    flowState.between = null;
    renderDetail();
  } else if (lane) {
    showAgentCalls(explorer.agent === lane ? "" : lane, true);
  }
  if (ev.target.closest("[data-flow-clear]")) {
    flowState.pair = null;
    renderDetail();
  }
  const more = ev.target.closest("[data-ex-more]")?.dataset.exMore;
  if (more) exLoadList(more, true);
  const action = ev.target.closest("[data-action]")?.dataset.action;
  const s = sessions.find(x => x.session_id === selectedId);
  if (action === "close") select(null);
  if (action === "focus" && s) focusSession(s.pid);
  if (action === "fork") { const b = ev.target.closest("[data-sid]"); forkAsk(b.dataset.sid, b.dataset.title); }
  if (action === "fork-go") forkSession(ev.target.closest("button"));
  if (action === "fork-cancel") { forking = null; renderDetail(); }
});

// Flow zoom: pinch / ⌘-Ctrl + scroll over the diagram (one redraw per frame), or drag across a
// stretch of it to fill the view with that stretch.
let flowWheel = null, flowDrag = null, flowDragged = false;

detail.addEventListener("wheel", ev => {
  if (!(ev.ctrlKey || ev.metaKey) || !ev.target.closest(".fl-plot")) return;
  ev.preventDefault();  // not the browser's page zoom
  const factor = Math.exp(-Math.max(-50, Math.min(50, ev.deltaY)) * 0.01);
  if (!flowWheel) requestAnimationFrame(() => { const w = flowWheel; flowWheel = null; flowZoom(w.factor, w.x); });
  flowWheel = {factor: (flowWheel?.factor || 1) * factor, x: ev.clientX};
}, {passive: false});

detail.addEventListener("mousedown", ev => {
  if (ev.button === 0 && ev.target.closest(".fl-plot")) flowDrag = {x0: ev.clientX, x1: ev.clientX};
});

function flowBrush() {  // the dragged stretch, in plot coordinates
  const plot = detail.querySelector(".fl-plot");
  if (!plot) return null;
  const left = plot.getBoundingClientRect().left;
  const [a, b] = [flowDrag.x0, flowDrag.x1].sort((x, y) => x - y);
  return {plot, a: a - left, b: b - left};
}

document.addEventListener("mousemove", ev => {
  if (!flowDrag) return;
  flowDrag.x1 = ev.clientX;
  if (Math.abs(flowDrag.x1 - flowDrag.x0) < 5) return;
  ev.preventDefault();  // no text selection while dragging
  const br = flowBrush();
  const rect = br?.plot.querySelector(".fl-brush");
  if (rect) { rect.setAttribute("x", br.a); rect.setAttribute("width", br.b - br.a); }
});

document.addEventListener("mouseup", () => {
  if (!flowDrag) return;
  const br = Math.abs(flowDrag.x1 - flowDrag.x0) >= 5 ? flowBrush() : null;
  flowDrag = null;
  const wrap = detail.querySelector(".flow-wrap");
  if (!br || !wrap || !flowState.sc) return;
  flowDragged = true;  // swallows the click that follows this mouseup
  setTimeout(() => { flowDragged = false; });
  const view = wrap.clientWidth - FLOW_LABEL;
  const t = flowState.sc.time(br.a - FLOW_PAD);
  const zoom = Math.min(FLOW_ZOOM_MAX, Math.max(1, flowState.zoom * view / (br.b - br.a)));
  flowShow(zoom, t, 0);
});

// The explorer limited to one agent's calls ("main": the main session; "": everyone's).
function showAgentCalls(agent, reveal = false) {
  explorer.agent = agent;
  exResetLists();
  if (reveal && agent && !openSections.has("tools")) {
    openSections.add("tools");
    try { localStorage.setItem("openSections", JSON.stringify([...openSections])); } catch (_) {}
  }
  renderDetail();
  if (reveal && agent) detail.querySelector('[data-section="tools"]')?.scrollIntoView({behavior: "smooth", block: "start"});
}

let exTimer;
detail.addEventListener("input", ev => {
  if (ev.target.id === "fork-name" && forking) forking.name = ev.target.value;  // survives the panel's updates
  if (ev.target.id !== "ex-q") return;
  clearTimeout(exTimer);
  exTimer = setTimeout(() => {
    explorer.q = ev.target.value.trim();
    exResetLists();
    renderDetail();
  }, 200);
});
detail.addEventListener("keydown", ev => {
  if (ev.target.id === "fork-name" && (ev.key === "Enter" || ev.key === "Escape")) {
    ev.stopPropagation();  // Escape cancels the fork instead of closing the modal
    if (ev.key === "Enter") forkSession(detail.querySelector('[data-action="fork-go"]'));
    else { forking = null; renderDetail(); }
  }
  if (ev.target.id === "ex-q" && ev.key === "Escape" && ev.target.value) {
    ev.stopPropagation();  // clears the filter instead of closing the modal
    ev.target.value = "";
    ev.target.dispatchEvent(new Event("input", {bubbles: true}));
  }
});

// The page reloads itself when its code changed on the server (a dashboard tab stays open for
// days), unless you are typing in it; then on the next update after that.
let pageVersion = null;

function connect() {
  if (!PAGE_KEY) return needKey();
  fetch("/api/sessions");  // a wrong key shows the same message (the stream can't tell why it fails)
  const es = new EventSource(`/api/stream?key=${encodeURIComponent(PAGE_KEY)}`);  // EventSource sends no headers
  es.onopen = () => { conn.className = "conn live"; };
  es.onmessage = ev => {
    const data = JSON.parse(ev.data);
    pageVersion ??= data.page;
    if (data.page && data.page !== pageVersion && !document.activeElement?.matches("input, textarea")) {
      location.reload();
      return;
    }
    sessions = data.sessions;
    usage = data.usage || [];
    render();
  };
  es.onerror = () => { conn.className = "conn down"; };
}

connect();
if (view === "all") loadHistory();
setInterval(() => {
  renderTable();  // keep idle timers ticking between updates
  renderUsage();  // and reset countdowns and "as of": a closed account's reading never changes
  renderDetail();  // and running times; refreshes open shell outputs
  // The all-sessions list: every second while indexing, else every 10 s.
  if (view === "all" && (history_.indexing?.running || Date.now() - history_.loadedAt > 10_000)) loadHistory();
}, 1000);
