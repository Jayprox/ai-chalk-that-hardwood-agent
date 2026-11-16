// Run with: node tests/js/recent_chart_check.js   (pytest runs it through tests/test_recent_chart_js.py when node is installed)
const assert = require("assert");
const RC = require("../../static/recent_chart.js");

const games = (vals) => vals.map((v, i) => ({ minutes: 30 + i, date: "2026-11-" + String(i + 1).padStart(2, "0"), opponent: "SAC", venue: i % 2 ? "away" : "home", value: v, def_rank: 5 }));
// What the server returns for the same ten games at two different lines (the server does the over/under scoring).
const score = (vals, line) => {
  const gs = games(vals).map((g) => ({ ...g, result: g.value === line ? "push" : g.value > line ? "over" : "under" }));
  const c = (k) => gs.filter((g) => g.result === k).length;
  return { line, games: gs, over: c("over"), under: c("under"), push: c("push") };
};
const vals = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18];
const prop = { player_id: "p", game_id: "g", market: "pts", line: 13.5 };

// 1. The line is part of the cache key.
assert.notStrictEqual(RC.keyOf(prop), RC.keyOf({ ...prop, line: 15.5 }));

// 2. A moved line misses the old cache entry; the same line hits; entries expire.
let t = 1000;
const cache = RC.makeCache(() => t);
cache.set(RC.keyOf(prop), score(vals, 13.5));
assert.ok(cache.get(RC.keyOf(prop)), "same line is a hit");
assert.strictEqual(cache.get(RC.keyOf({ ...prop, line: 15.5 })), null, "moved line must miss");
t += RC.TTL_MS - 1;
assert.ok(cache.get(RC.keyOf(prop)), "still fresh just before the lifetime ends");
t += 2;
assert.strictEqual(cache.get(RC.keyOf(prop)), null, "expired after the lifetime");
assert.strictEqual(cache.size(), 0, "expired entry is removed");

// 3. Revisiting after the line moved redraws the threshold, the bar classes and the counts.
const before = score(vals, 13.5), after = score(vals, 15.5);
const html1 = RC.chart(before), html2 = RC.chart(after);
const lineY = (h) => /class="threshold"[^>]* y1="([\d.]+)"/.exec(h)[1];
const classes = (h) => [...h.matchAll(/class="bar (\w+)"/g)].map((m) => m[1]);
assert.notStrictEqual(lineY(html1), lineY(html2), "dashed threshold moves");
assert.ok(html1.includes("line 13.5") && html2.includes("line 15.5"));
assert.deepStrictEqual(classes(html1), ["under", "under", "under", "under", "under", "over", "over", "over", "over", "over"]);
assert.deepStrictEqual(classes(html2), ["under", "under", "under", "under", "under", "under", "under", "over", "over", "over"]);
assert.strictEqual(RC.sub(before), "5 over · 5 under in 10 games");
assert.strictEqual(RC.sub(after), "3 over · 7 under in 10 games");

// 4. A push is drawn grey and counted separately; an empty history draws a message, not a broken chart.
const pushed = score(vals, 14);
assert.ok(RC.chart(pushed).includes("push") && RC.sub(pushed).includes("1 push"));
assert.ok(RC.chart({ line: 10, games: [], over: 0, under: 0, push: 0 }).includes("No earlier games"));
// 5. Actual minutes are drawn under each bar (and a missing value shows a dash, not a zero).
assert.ok(html1.includes("30 min") && html1.includes("39 min"));
assert.ok(RC.chart({ line: 10, over: 0, under: 1, push: 0, games: [{ date: "2026-11-01", opponent: "SAC", venue: "home", value: 5, result: "under", minutes: null }] }).includes("— min"));
console.log("recent_chart_check: ok");
