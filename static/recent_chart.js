// Last-10 bar chart helpers for the player page: cache key, short-lived cache, and SVG drawing. Display only.
// Kept apart from dash.js so the logic can be checked under node (tests/js/recent_chart_check.js); it has no DOM access.
(function (root) {
  "use strict";
  const TTL_MS = 5 * 60 * 1000; // a cached chart is reused for five minutes, then fetched again
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // The line is part of the key: when the prop's line moves, the old chart no longer matches the page and must not be reused.
  const keyOf = (p) => [p.player_id, p.game_id, p.market, p.line].join("|");

  function makeCache(now) {
    const clock = now || Date.now, m = new Map();
    return {
      get(k) { const e = m.get(k); if (!e) return null; if (clock() - e.at > TTL_MS) { m.delete(k); return null; } return e.val; },
      set(k, v) { m.set(k, { at: clock(), val: v }); },
      size() { return m.size; },
    };
  }

  function sub(r) {
    return r.games.length ? r.over + " over · " + r.under + " under" + (r.push ? " · " + r.push + " push" : "") + " in " + r.games.length + " games" : "";
  }

  function chart(r) {
    if (!r.games.length) return '<div class="faint">No earlier games this season to show.</div>';
    const n = r.games.length, W = 520, H = 198, padT = 18, padB = 60, plotH = H - padT - padB, bw = W / n;
    const top = Math.max(r.line, ...r.games.map((g) => g.value)) * 1.12 || 1;
    const Y = (v) => padT + plotH - (v / top) * plotH;
    let bars = "";
    r.games.forEach((g, i) => {
      const x = i * bw, y = Y(g.value), h = Math.max(plotH + padT - y, 1);
      const col = g.result === "over" ? "var(--over)" : g.result === "under" ? "var(--under)" : "#8d97a1";
      const dt = String(g.date || "").slice(5).replace("-", "/");
      bars += '<rect class="bar ' + esc(g.result) + '" x="' + (x + 4).toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + (bw - 8).toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2" fill="' + col + '"></rect>' +
        '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (y - 4).toFixed(1) + '" text-anchor="middle" font-size="11" fill="#e8ebee">' + esc(g.value) + "</text>" +
        '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 44) + '" text-anchor="middle" font-size="10" fill="#7d8896">' + esc(dt) + "</text>" +
        '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 32) + '" text-anchor="middle" font-size="10" fill="#7d8896">' + esc((g.venue === "away" ? "@" : "vs ") + (g.opponent || "")) + "</text>" +
        '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 20) + '" text-anchor="middle" font-size="10" fill="#e8ebee">' + (g.def_rank != null ? "DEF " + esc(g.def_rank) : "DEF —") + "</text>" +
        '<text class="mins" x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 7) + '" text-anchor="middle" font-size="10" fill="#7d8896">' + (typeof g.minutes === "number" ? esc(g.minutes) + " min" : "— min") + "</text>";
    });
    const ly = Y(r.line).toFixed(1);
    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Last ' + n + ' games against the line">' + bars +
      '<line class="threshold" x1="0" x2="' + W + '" y1="' + ly + '" y2="' + ly + '" stroke="#e8ebee" stroke-dasharray="4 3"></line>' +
      '<text x="' + (W - 2) + '" y="' + (Number(ly) - 4) + '" text-anchor="end" font-size="11" fill="#e8ebee">line ' + esc(r.line) + "</text></svg>" +
      '<div class="axis"><span>oldest</span><span>blue = over, orange = under, grey = push</span><span>most recent</span></div>';
  }

  const api = { TTL_MS, keyOf, makeCache, sub, chart, esc };
  root.RecentChart = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
