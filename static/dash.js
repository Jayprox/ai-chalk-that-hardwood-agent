(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const params = new URLSearchParams(location.search);
  const dateQ = params.get("date") ? "date=" + encodeURIComponent(params.get("date")) : "";
  const STATE = { upcoming: "Upcoming", active: "In window", tipped: "Tipped", final: "Final" };
  let filters = { game: "all", market: "all" };
  let lastRouteKey = "";

  async function api(path) {
    const sep = path.includes("?") ? "&" : "?";
    const res = await fetch("/api/dash" + path + (dateQ ? sep + dateQ : ""), { cache: "no-store" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function route() {
    const parts = (location.hash || "#/games").replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    if (parts[0] === "m" && parts[1]) return { name: "market", market: parts[1] };
    if (parts[0] === "game" && parts[1]) return { name: "game", id: parts[1] };
    if (parts[0] === "prop" && parts.length >= 4) return { name: "prop", pid: parts[1], gid: parts[2], market: parts[3] };
    return { name: "games" };
  }

  // ---- small formatters
  const sideCls = (s) => (s === "OVER" ? "over" : s === "UNDER" ? "under" : "");
  const rank = (p) => (p.pct == null ? "—" : "p" + p.pct.toFixed(1));
  const tilt = (p) => (p.tilt == null ? "—" : p.tilt.toFixed(1));
  const moveText = (p) => {
    if (p.open_line == null) return "";
    if (!p.line_move) return "open " + p.open_line + " · no move";
    return "open " + p.open_line + " · " + (p.line_move > 0 ? "up " : "down ") + Math.abs(p.line_move);
  };
  const prices = (p) => (p.over_price || "—") + " / " + (p.under_price || "—");

  function resultLabel(p) {
    if (p.void) return '<span class="label void">' + esc((p.result || "void").toUpperCase()) + " · void</span>";
    if (p.result === "over" || p.result === "under") {
      const hit = p.side && p.result === p.side.toLowerCase();
      return '<span class="label ' + (hit ? "hit" : "miss") + '">' + p.result.toUpperCase() + (p.actual != null ? " · " + esc(p.actual) + " actual" : "") + "</span>";
    }
    return "";
  }
  function cardLabel(p) {
    if (p.state === "final" || p.void) return resultLabel(p);
    if (p.strong) return '<span class="label strong" title="Top 10% of its market by tilt and the simulation leans the same way. Unvalidated.">HIGH LEAN · unvalidated</span>';
    if (p.top10) {
      const why = { disagree: "sim disagrees", none: "no sim", pending: "sim pending" }[p.sim] || "";
      return '<span class="label muted-label" title="Top 10% of its market by tilt, but not marked high lean: ' + why + '.">TOP 10% · ' + why + "</span>";
    }
    return "";
  }
  function simCell(p) {
    if (p.sim === "agree" || p.sim === "disagree") return esc(p.sim_side) + (p.sim === "agree" ? " ✓" : " ✗");
    return p.sim === "none" ? "no sim" : "—";
  }
  function simFoot(p) {
    return "Sim: " + (p.sim === "agree" || p.sim === "disagree" ? esc(p.sim_side) + " (" + (p.sim === "agree" ? "agrees" : "disagrees") + ")" : p.sim === "none" ? "not enough games" : "not yet");
  }
  function pmfChart(sim) {
    const pmf = sim.pmf, line = sim.line;
    let hi = pmf.length - 1;
    while (hi > 0 && pmf[hi] < 0.0005) hi--;
    let lo = 0;
    while (lo < hi && pmf[lo] < 0.0005) lo++;
    const n = hi - lo + 1, W = 520, H = 130, bw = W / n, top = Math.max(...pmf.slice(lo, hi + 1)) || 1;
    let bars = "";
    for (let v = lo; v <= hi; v++) {
      const h = (pmf[v] / top) * (H - 8);
      const over = v > line, push = Number.isInteger(line) && v === line;
      bars += '<rect x="' + ((v - lo) * bw + 0.5).toFixed(1) + '" y="' + (H - h).toFixed(1) + '" width="' + Math.max(bw - 1, 1).toFixed(1) + '" height="' + h.toFixed(1) + '" fill="' + (push ? "#8d97a1" : over ? "var(--over)" : "var(--under)") + '"></rect>';
    }
    const lx = ((line + 0.5 - lo) * bw).toFixed(1);
    const mark = line + 0.5 >= lo && line + 0.5 <= hi + 1 ? '<line x1="' + lx + '" x2="' + lx + '" y1="0" y2="' + H + '" stroke="#e8ebee" stroke-dasharray="4 3"></line>' : "";
    return '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" height="' + H + '" role="img" aria-label="Simulated distribution against the line">' + bars + mark + "</svg>" +
      '<div class="axis"><span>' + lo + "</span><span>line " + esc(line) + "</span><span>" + hi + "</span></div>";
  }

  // ---- charts
  function spark(values, color) {
    if (!values || values.length < 2) return '<svg width="110" height="36" viewBox="0 0 110 36" aria-hidden="true"><line x1="0" y1="18" x2="110" y2="18" stroke="#2c333b" stroke-dasharray="3 3"></line></svg>';
    const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
    const pts = values.map((v, i) => (i / (values.length - 1)) * 110 + "," + (32 - ((v - lo) / span) * 28)).join(" ");
    return '<svg width="110" height="36" viewBox="0 0 110 36" aria-hidden="true"><polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline></svg>';
  }
  function series(history, key, color, step, label) {
    const rows = history.filter((h) => h[key] != null);
    if (!rows.length) return '<div class="faint">No history yet.</div>';
    const times = rows.map((h) => Date.parse(h.t));
    const t0 = Math.min(...times), t1 = Math.max(...times);
    const ys = rows.map((h) => h[key]);
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const H = 110, W = 520;
    const X = (i) => (t1 === t0 ? (i === 0 ? 0 : W) : ((times[i] - t0) / (t1 - t0)) * W);
    const Y = (v) => (hi === lo ? H / 2 : H - 10 - ((v - lo) / span) * (H - 20));
    let pts = [];
    rows.forEach((h, i) => {
      if (step && i > 0) pts.push(X(i) + "," + Y(ys[i - 1]));
      pts.push(X(i) + "," + Y(h[key]));
    });
    if (step || rows.length === 1) pts.push(W + "," + Y(ys[ys.length - 1]));
    if (rows.length === 1 && !step) pts.unshift("0," + Y(ys[0]));
    return '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" height="' + H + '" role="img" aria-label="' + esc(label) + '">' +
      '<polyline points="' + pts.join(" ") + '" fill="none" stroke="' + color + '" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"></polyline></svg>' +
      '<div class="axis"><span>' + esc(rows[0].at || "") + "</span><span>" + (lo === hi ? "steady at " + lo : "low " + lo + " · high " + hi) + "</span><span>" + esc(rows[rows.length - 1].at || "") + "</span></div>";
  }

  // ---- pages
  function propHref(p) { return "#/prop/" + [p.player_id, p.game_id, p.market].map(encodeURIComponent).join("/"); }
  function propCard(p) {
    const cls = "card" + (p.strong ? " strong" : "") + (p.state === "final" || p.void ? " dim" : "");
    return '<a class="' + cls + '" href="' + propHref(p) + '">' +
      '<div class="between"><div><div class="name">' + esc(p.name) + '</div><div class="sub">' + esc(p.team) + " vs " + esc(p.opponent) + " · " + esc(p.tip || "") + "</div></div>" + cardLabel(p) + "</div>" +
      '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><span class="big">' + esc(p.line) + '</span><span class="sub">' + esc(moveText(p)) + "</span></div>" +
      '<div style="font-size:13px;color:var(--soft)">Over ' + esc(p.over_price || "—") + " · Under " + esc(p.under_price || "—") + "</div>" +
      '<div class="sig"><div><div class="k">Signal strength</div><div class="sig-val ' + sideCls(p.side) + '">' + rank(p) + (p.side ? " · " + p.side : "") + '</div><div class="sub">tilt ' + tilt(p) + " pts off 50/50</div></div>" +
      spark(p.spark, p.side === "OVER" ? "var(--over)" : "var(--under)") + "</div>" +
      '<div class="foot"><span>' + simFoot(p) + '</span><span>Out: not checked</span></div></a>';
  }

  function viewGames(o) {
    if (!o.games.length) return empty(o);
    const cards = o.games.map((g) => {
      const top = g.top.map((t) => '<div><span>' + esc(t.name) + " · " + esc(t.market_label) + " " + esc((t.side || "").toLowerCase()) + " " + esc(t.line) + '</span><span class="green" style="font-weight:600">p' + (t.pct == null ? "—" : t.pct.toFixed(1)) + "</span></div>").join("") || '<div class="faint">No props yet.</div>';
      return '<a class="card' + (g.strong && g.state !== "final" ? " strong" : "") + (g.state === "final" ? " dim" : "") + '" href="#/game/' + encodeURIComponent(g.game_id) + '">' +
        '<div class="between"><div><div class="name" style="font-size:18px">' + esc(g.away) + " @ " + esc(g.home) + '</div><div class="sub">' + esc(g.tip) + '</div></div><span class="chip-state st-' + esc(g.state) + '">' + esc(STATE[g.state] || g.state) + "</span></div>" +
        '<div class="row"><span>' + g.props + " props</span><span>" + esc(g.refresh || (g.updated ? "updated " + g.updated : "")) + "</span></div>" +
        '<div class="top3"><div class="k" style="display:block">Top by rank</div>' + top + "</div>" +
        (g.strong ? '<div class="green" style="font-size:12px;font-weight:600">' + g.strong + " high-lean " + (g.strong === 1 ? "prop" : "props") + "</div>" : "") + "</a>";
    }).join("");
    return '<div class="row"><span>' + o.games.length + " games · " + o.props_total + " props tracked · green border = high-lean prop (top 10% of its market by tilt and the sim agrees; unvalidated, not a value signal)</span><span>Availability: not checked yet</span></div>" +
      '<div class="grid">' + cards + "</div>";
  }

  function viewMarket(m) {
    const chips = [{ id: "all", label: "All games" }].concat(m.games.map((g) => ({ id: g.game_id, label: g.label })))
      .map((c) => '<button class="chip' + (filters.game === c.id ? " on" : "") + '" data-game="' + esc(c.id) + '">' + esc(c.label) + "</button>").join("");
    const props = m.props.filter((p) => filters.game === "all" || p.game_id === filters.game);
    return '<div class="chips">' + chips + "</div>" +
      '<div class="row"><span>Sorted by within-market rank (percentile of market tilt). Rank is not a probability of hitting.</span><span>' + props.length + " " + esc(m.label) + " props · " + m.strong + " high lean</span></div>" +
      (props.length ? '<div class="grid">' + props.map(propCard).join("") + "</div>" : '<div class="empty">No ' + esc(m.label) + " props for this date yet.</div>");
  }

  function viewGame(d) {
    const g = d.game;
    const chips = [{ key: "all", label: "All markets" }].concat(d.markets).map((c) => '<button class="chip' + (filters.market === c.key ? " on" : "") + '" data-market="' + esc(c.key) + '">' + esc(c.label) + "</button>").join("");
    const props = d.props.filter((p) => filters.market === "all" || p.market === filters.market);
    const rows = props.map((p) => '<tr class="' + (p.state === "final" || p.void ? "dim" : "") + '"><td style="font-weight:600"><a href="' + propHref(p) + '" class="' + (p.strong ? "green" : "") + '">' + esc(p.name) + "</a></td><td>" + esc(p.market_label) + '</td><td class="' + sideCls(p.side) + '" style="font-weight:600">' + esc(p.side || "—") + "</td><td>" + esc(p.line) + '</td><td class="muted">' + esc(moveText(p)) + "</td><td>" + esc(prices(p)) + '</td><td style="font-weight:700" class="' + (p.strong ? "green" : "") + '">' + rank(p) + "</td><td>" + tilt(p) + ' pts</td><td class="faint">' + simCell(p) + '</td><td>' + (p.void || p.state === "final" ? resultLabel(p) : "") + "</td></tr>").join("");
    const score = g.home_score != null && g.away_score != null ? " · " + esc(g.away) + " " + g.away_score + ", " + esc(g.home) + " " + g.home_score : "";
    return '<a class="back" href="#/games">← All games</a>' +
      '<div class="hero' + (d.strong && g.state !== "final" ? " strong" : "") + '"><div><h2>' + esc(g.away) + " @ " + esc(g.home) + '</h2><div class="muted">' + esc(g.tip) + (g.props_open_at ? " · props open " + esc(g.props_open_at) : "") + (g.props_close_at ? " · closing line " + esc(g.props_close_at) : "") + score + '</div></div><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><span class="chip-state st-' + esc(g.state) + '">' + esc(STATE[g.state] || g.state) + '</span><span class="muted">' + d.props.length + " props · " + d.strong + " high lean</span></div></div>" +
      '<div class="two"><div class="box"><span class="k">' + esc(g.away) + ' availability</span><span style="color:var(--soft)">Not checked yet. The availability researcher arrives in a later milestone.</span></div><div class="box"><span class="k">' + esc(g.home) + ' availability</span><span style="color:var(--soft)">Not checked yet. The availability researcher arrives in a later milestone.</span></div></div>' +
      '<div class="chips">' + chips + "</div>" +
      '<div class="tablewrap"><table><thead><tr><th>Player</th><th>Market</th><th>Side</th><th>Line</th><th>Move</th><th>Prices (O / U)</th><th>Rank</th><th>Tilt</th><th>Sim</th><th>Result</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
      '<div class="faint" style="font-size:12px">Rank is each prop\'s percentile within its own market across the whole board, not within this game.</div>';
  }

  // ---- last-10 bar chart (display only; fetched from Hardwood on demand). Drawing and caching live in recent_chart.js.
  const RC = window.RecentChart;
  const recentCache = RC.makeCache();
  const recentInflight = {};
  let recentSeq = 0;
  function recentBlock(p) {
    const c = recentCache.get(RC.keyOf(p));
    return '<div class="chart"><div class="between"><span class="k">Last 10 games vs the line</span><span class="sub" id="l10sub">' + (c ? esc(RC.sub(c)) : "") + "</span></div>" +
      '<div id="l10body" data-key="' + esc(RC.keyOf(p)) + '">' + (c ? RC.chart(c) : '<div class="faint">Loading…</div>') + "</div>" +
      '<div class="faint" style="font-size:12px">Current season, games played before this date, from Hardwood. DEF = the opponent&#39;s defensive rank against this player&#39;s position for this stat (1 = fewest allowed, 30 = most), as ranked today, not on the night of the game. Minutes are what he actually played that night, shown as context; they were not known before tip-off. Display only.</div></div>';
  }
  async function loadRecent(p) {
    const key = RC.keyOf(p), mine = ++recentSeq;
    if (recentCache.get(key)) return;
    let html, sub = "";
    try {
      const url = "/recent/" + [p.player_id, p.game_id, p.market].map(encodeURIComponent).join("/");
      const r = await (recentInflight[key] || (recentInflight[key] = api(url).finally(() => { delete recentInflight[key]; })));
      // If the line moved while we were fetching, the answer is for the new line: file it under that line's key, never the old one.
      recentCache.set(RC.keyOf(Object.assign({}, p, { line: r.line })), r);
      html = RC.chart(r); sub = RC.sub(r);
    } catch (e) { html = '<div class="faint">Last-10 chart unavailable: ' + esc(e.message) + "</div>"; }
    const body = $("l10body");
    // A newer view of the page (another prop, or a newer line) started after this request: drop this late answer.
    if (mine !== recentSeq || !body || body.dataset.key !== key) return;
    body.innerHTML = html; if ($("l10sub")) $("l10sub").textContent = sub;
  }

  const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 > 10 && n % 100 < 14) ? 0 : (n % 10 < 4 ? n % 10 : 0)]);
  function defTile(p, ex) {
    const m = ex.matchup;
    if (!m || m.rank == null || !m.of) return ["Defense", "—", "no ranking for this matchup"];
    const vs = typeof m.vs_avg === "number" ? " (" + (m.vs_avg > 0 ? "+" : "") + m.vs_avg + " vs avg)" : "";
    const pos = m.position_label || m.position || "this position";
    return ["Defense vs " + esc(pos), ordinal(m.rank) + " of " + m.of,
      esc(m.opponent || p.opponent) + " allows " + (m.allowed != null ? esc(m.allowed) : "—") + vs + " · 1st = toughest"];
  }

  function viewProp(d) {
    const p = d.prop, h = d.history, ex = d.extra || {};
    const pov = p.p_over == null ? "—" : (100 * p.p_over).toFixed(1) + "%";
    const l10 = ex.last10 && ex.last10.over != null ? ex.last10.over + " over · " + ex.last10.under + " under" : "—";
    const stats = [["Line", esc(p.line), p.open_line != null ? "open " + esc(p.open_line) + (p.snapshot_kind === "close" ? " · closing line captured" : "") : ""],
      ["Prices", esc(prices(p)), "over / under"], ["Market P(over)", pov, "price with the margin removed"], ["Last 10 vs line", esc(l10), "from Hardwood"], defTile(p, ex), ["Sim P(over)", d.sim && d.sim.p_over != null ? (100 * d.sim.p_over).toFixed(0) + "%" : "—", d.sim ? "shrunk toward 50/50 (raw " + (100 * d.sim.p_over_raw).toFixed(0) + "%); read the direction" : "no simulation yet"]]
      .map((s) => '<div class="stat"><span class="k">' + s[0] + "</span><b>" + s[1] + '</b><span class="sub">' + s[2] + "</span></div>").join("");
    const notes = d.notes.map((n) => "<h3>" + esc(n.title) + "</h3><p>" + esc(n.text) + "</p>").join("");
    return '<a class="back" href="#/m/' + encodeURIComponent(p.market) + '">← ' + esc(p.market_label) + "</a>" +
      '<div class="hero' + (p.strong ? " strong" : "") + '"><div><h2>' + esc(p.name) + '</h2><div class="muted">' + esc(p.team) + " vs " + esc(p.opponent) + " · " + esc(p.tip || "") + " · " + esc(p.market_label) + '</div></div><div style="display:flex;align-items:center;gap:16px"><span class="big" style="font-size:36px">' + esc(p.line) + "</span>" + cardLabel(p) + "</div></div>" +
      '<div class="cols"><div class="left"><div class="stats">' + stats + "</div>" + recentBlock(p) +
      '<div class="chart"><div class="between"><span class="k">Line over time (changes only)</span><span class="sub">' + esc(moveText(p)) + "</span></div>" + series(h, "line", "var(--over)", true, "Line history") + "</div>" +
      '<div class="chart"><div class="between"><span class="k">Market tilt over time (our own snapshots)</span><span class="sub">' + esc(p.side || "") + " · " + rank(p) + " · tilt " + tilt(p) + " pts</span></div>" + series(h, "tilt", p.side === "OVER" ? "var(--over)" : "var(--under)", false, "Tilt history") + "</div>" +
      (d.sim ? '<div class="chart"><div class="between"><span class="k">Simulated distribution (raw, before shrinking)</span><span class="sub">mean ' + d.sim.mean.toFixed(1) + " ± " + d.sim.sd.toFixed(1) + " · " + d.sim.n_prior + " earlier games</span></div>" + pmfChart(d.sim) + "</div>" : "") + "</div>" +
      '<div class="right"><div class="notes"><div class="between"><span class="k">Notes</span><span class="sub">from stored data; the written research arrives in a later milestone</span></div>' + notes + "</div></div></div>";
  }

  function empty(o) {
    return '<div class="empty">No data for ' + esc(o.requested || o.date) + ' yet.<br>Run <code>python -m app.collector</code> to pull the board.</div>';
  }

  // ---- shell
  function renderTabs(o, active) {
    $("tabs").innerHTML = o.tabs.map((t) => '<a class="tab' + (t.key === active ? " on" : "") + '" href="#/' + (t.key === "games" ? "games" : "m/" + t.key) + '">' + esc(t.label) + (t.strong ? '<span class="badge">' + t.strong + "</span>" : "") + "</a>").join("");
  }
  function renderStatus(o) {
    $("date").textContent = o.date;
    const r = o.last_run, dot = $("dot");
    const sc = o.scheduler || {};
    if (!r) { $("refresh").textContent = "No collections yet"; dot.className = "dot idle"; }
    else {
      let t = (r.ok ? "Last collected " : "Last collection failed ") + (r.at || "");
      if (sc.settled) t += " · all games final";
      else if (sc.stale) t += " · overdue, the scheduler may be stopped";
      else if (o.next_run) t += " · next about " + o.next_run;
      else if (sc.due_now) t += " · refreshing now";
      $("refresh").textContent = t;
      dot.className = "dot" + (!r.ok ? " bad" : sc.stale ? " idle" : "");
    }
    const n = $("notice");
    if (o.fallback) { n.hidden = false; n.textContent = "No data for " + o.requested + " yet. Showing " + o.date + "."; } else { n.hidden = true; }
  }

  let loadSeq = 0;
  async function load() {
    const seq = ++loadSeq;
    const r = route();
    const key = r.name + (r.market || "") + (r.id || "") + (r.pid || "") + (r.gid || "");
    if (key !== lastRouteKey) { filters = { game: "all", market: "all" }; lastRouteKey = key; }
    try {
      const o = await api("/overview");
      renderStatus(o);
      let html = "", active = "games", recentFor = null;
      if (r.name === "games") html = viewGames(o);
      else if (r.name === "market") { active = r.market; html = viewMarket(await api("/market/" + encodeURIComponent(r.market))); }
      else if (r.name === "game") html = viewGame(await api("/game/" + encodeURIComponent(r.id)));
      else { active = r.market; const d = await api("/prop/" + [r.pid, r.gid, r.market].map(encodeURIComponent).join("/")); recentFor = d.prop; html = viewProp(d); }
      if (seq !== loadSeq) return;                   // a newer load started while this one was waiting: drop this one
      renderTabs(o, active);
      $("app").innerHTML = html;
      if (recentFor && $("l10body")) loadRecent(recentFor);
    } catch (e) {
      $("app").innerHTML = '<div class="empty">Could not load this view: ' + esc(e.message) + "</div>";
    }
  }

  $("app").addEventListener("click", (ev) => {
    const b = ev.target.closest("button.chip");
    if (!b) return;
    if (b.dataset.game) filters.game = b.dataset.game;
    if (b.dataset.market) filters.market = b.dataset.market;
    load();
  });
  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); load(); });
  load();
  setInterval(load, 60000);
})();
