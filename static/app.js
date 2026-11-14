/* Chat UI: sends the conversation to /api/chat and renders the SSE event stream. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var log = $("log"), input = $("input"), sendBtn = $("send"), banner = $("banner"), empty = $("empty");
  var statusEl = $("status");
  var state = { history: [], busy: false, abort: null };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function nearBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  }
  function scrollDown(force) {
    if (force || nearBottom()) log.scrollTop = log.scrollHeight;
  }

  function fmtMs(ms) { return ms == null ? "" : ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms"; }
  function fmtTok(n) { return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }
  function domain(url) { try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return url; } }
  function isHttp(url) { return /^https?:\/\//i.test(url); }

  // ---------------------------------------------------------------- messages

  function addUser(text) {
    var m = el("div", "msg user");
    m.appendChild(el("div", "bubble", text));
    log.appendChild(m);
    scrollDown(true);
  }

  function addAssistant() {
    var root = el("div", "msg assistant");
    var tools = el("details", "tools");
    tools.hidden = true;
    var summary = el("summary", "", "Tool activity");
    var list = el("ul", "tool-list");
    tools.appendChild(summary);
    tools.appendChild(list);
    var body = el("div", "body md");
    var sources = el("details", "sources");
    sources.hidden = true;
    var notices = el("div", "notices");
    var meta = el("div", "meta");
    [tools, body, notices, sources, meta].forEach(function (c) { root.appendChild(c); });
    log.appendChild(root);
    return {
      root: root, tools: tools, toolSummary: summary, toolList: list, body: body, sources: sources,
      notices: notices, meta: meta, text: "", raf: 0, pending: {}, toolCount: 0, failed: 0,
      ok: true, error: null, done: null
    };
  }

  function renderBody(a) {
    a.raf = 0;
    a.body.innerHTML = window.renderMarkdown(a.text);
    scrollDown(false);
  }

  function addToolRow(a, name) {
    var li = el("li", "tool running");
    li.appendChild(el("span", "icon", "…"));
    var label = el("span", "label", name);
    li.appendChild(label);
    li.appendChild(el("span", "ms", ""));
    li.appendChild(el("span", "detail", ""));
    a.toolList.appendChild(li);
    a.tools.hidden = false;
    a.tools.open = true;
    (a.pending[name] = a.pending[name] || []).push(li);
    return li;
  }

  function updateToolSummary(a) {
    a.toolSummary.textContent = "Tool activity (" + a.toolCount + (a.failed ? ", " + a.failed + " failed" : "") + ")";
  }

  function finishToolRow(a, ev) {
    var queue = a.pending[ev.name];
    var li = queue && queue.length ? queue.shift() : addToolRow(a, ev.name);
    if (queue && !queue.length) delete a.pending[ev.name];
    li.className = "tool " + (ev.ok ? "ok" : "fail");
    li.querySelector(".icon").textContent = ev.ok ? "✓" : "✗";
    li.querySelector(".label").textContent = ev.label || ev.name;
    li.querySelector(".ms").textContent = fmtMs(ev.ms);
    li.querySelector(".detail").textContent = ev.detail || "";
    a.toolCount++;
    if (!ev.ok) a.failed++;
    updateToolSummary(a);
    scrollDown(false);
  }

  function renderSources(a, sources) {
    var cited = sources.filter(function (s) { return s.cited && isHttp(s.url); });
    var other = sources.filter(function (s) { return !s.cited && isHttp(s.url); });
    if (!cited.length && !other.length) return;
    a.sources.hidden = false;
    a.sources.appendChild(el("summary", "", "Sources (" + (cited.length || other.length) + ")"));

    function list(items) {
      var ul = el("ul", "source-list");
      items.forEach(function (s) {
        var li = el("li");
        var link = el("a", "", s.title || s.url);
        link.href = s.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        li.appendChild(link);
        li.appendChild(el("span", "src-meta", " — " + domain(s.url) + (s.age ? ", " + s.age : "")));
        ul.appendChild(li);
      });
      return ul;
    }
    if (cited.length) a.sources.appendChild(list(cited));
    if (other.length) {
      if (cited.length) a.sources.appendChild(el("div", "also", "Also found"));
      a.sources.appendChild(list(other));
    }
    if (!cited.length) a.sources.open = true;
  }

  function handle(a, ev) {
    switch (ev.type) {
      case "text":
        a.text += ev.delta;
        if (!a.raf) a.raf = requestAnimationFrame(function () { renderBody(a); });
        break;
      case "tool_start":
        addToolRow(a, ev.name);
        scrollDown(false);
        break;
      case "tool_result":
        finishToolRow(a, ev);
        break;
      case "notice":
        a.notices.appendChild(el("div", "notice", ev.message));
        break;
      case "check":
        (ev.notes || []).forEach(function (n) { a.notices.appendChild(el("div", "notice", "\u26a0 " + n)); });
        break;
      case "sources":
        renderSources(a, ev.sources || []);
        break;
      case "error":
        a.ok = false;
        a.error = ev.message;
        a.notices.appendChild(el("div", "notice error", ev.message));
        break;
      case "done":
        a.done = ev;
        break;
    }
  }

  function finalize(a) {
    if (a.raf) { cancelAnimationFrame(a.raf); a.raf = 0; }
    a.body.innerHTML = window.renderMarkdown(a.text);
    // Tools that never reported a result (e.g. the connection dropped) should not spin forever.
    Object.keys(a.pending).forEach(function (k) {
      a.pending[k].forEach(function (li) { li.className = "tool fail"; li.querySelector(".icon").textContent = "✗"; });
    });
    if (!a.failed) a.tools.open = false;
    if (a.done) {
      var d = a.done, bits = [fmtMs(d.elapsed_ms)];
      if (a.toolCount) bits.push(a.toolCount + " tool call" + (a.toolCount === 1 ? "" : "s"));
      if (d.searches) bits.push(d.searches + " web search" + (d.searches === 1 ? "" : "es"));
      bits.push(fmtTok(d.input_tokens) + " in" + (d.cache_read_tokens ? " (" + fmtTok(d.cache_read_tokens) + " cached)" : "") +
                " / " + fmtTok(d.output_tokens) + " out tokens" +
                (d.est_cost_usd != null ? " \u00b7 ~$" + d.est_cost_usd.toFixed(2) : ""));
      a.meta.textContent = bits.join(" · ");
    }
    scrollDown(false);
  }

  // ---------------------------------------------------------------- sending

  function setBusy(busy) {
    state.busy = busy;
    sendBtn.textContent = busy ? "Stop" : "Send";
    sendBtn.classList.toggle("stop", busy);
    input.disabled = false;
  }

  async function readSSE(res, onEvent) {
    var reader = res.body.getReader();
    var dec = new TextDecoder();
    var buf = "";
    for (;;) {
      var r = await reader.read();
      if (r.done) break;
      buf += dec.decode(r.value, { stream: true });
      var i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        var chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        chunk.split("\n").forEach(function (line) {
          if (line.indexOf("data: ") !== 0) return;
          try { onEvent(JSON.parse(line.slice(6))); } catch (e) { console.error("bad event", e); }
        });
      }
    }
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || state.busy) return;
    if (empty) empty.hidden = true;
    addUser(text);
    state.history.push({ role: "user", content: text });
    var a = addAssistant();
    var ctrl = new AbortController();
    state.abort = ctrl;
    setBusy(true);
    var aborted = false;

    try {
      var res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: state.history }),
        signal: ctrl.signal
      });
      if (!res.ok) {
        var detail = res.status + " " + res.statusText;
        try {
          var j = await res.json();
          if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        } catch (e) { /* keep status text */ }
        throw new Error(detail);
      }
      await readSSE(res, function (ev) { handle(a, ev); });
    } catch (e) {
      if (e && e.name === "AbortError") {
        aborted = true;
        a.notices.appendChild(el("div", "notice", "Stopped."));
      } else {
        a.ok = false;
        a.error = (e && e.message) || "Request failed";
        a.notices.appendChild(el("div", "notice error", a.error));
      }
    } finally {
      finalize(a);
      // Keep the history alternating user/assistant: a failed turn drops its question.
      if (a.text && (a.ok || aborted)) state.history.push({ role: "assistant", content: a.text });
      else state.history.pop();
      state.abort = null;
      setBusy(false);
      input.focus();
    }
  }

  // ---------------------------------------------------------------- wiring

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  $("composer").addEventListener("submit", function (e) {
    e.preventDefault();
    if (state.busy) { if (state.abort) state.abort.abort(); return; }
    var text = input.value;
    input.value = "";
    autosize();
    send(text);
  });

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  input.addEventListener("input", autosize);

  document.querySelectorAll("[data-prompt]").forEach(function (b) {
    b.addEventListener("click", function () { send(b.getAttribute("data-prompt")); });
  });

  $("new-chat").addEventListener("click", function () {
    if (state.abort) state.abort.abort();
    state.history = [];
    Array.prototype.slice.call(log.querySelectorAll(".msg")).forEach(function (n) { n.remove(); });
    if (empty) empty.hidden = false;
    input.focus();
  });

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
    statusEl.textContent = h.model + (h.web_search_max_uses ? " · web search ≤" + h.web_search_max_uses + "/question" : " · web search off");
    var missing = [];
    if (!h.anthropic_key_set) missing.push("ANTHROPIC_API_KEY");
    if (!h.hardwood_key_set) missing.push("HARDWOOD_API_KEY");
    if (missing.length) {
      banner.textContent = "Missing " + missing.join(" and ") + " in .env. Add it and restart the server.";
      banner.hidden = false;
    }
  }).catch(function () {
    banner.textContent = "Cannot reach the server.";
    banner.hidden = false;
  });

  input.focus();
})();
