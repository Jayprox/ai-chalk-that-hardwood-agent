/* Tiny, safe markdown renderer for chat answers.
 * Escapes ALL input first, then adds a fixed set of tags, so model output can never inject markup.
 * Links are limited to http(s). Supports: headings, bold, italic, inline/fenced code, lists, tables,
 * blockquotes, horizontal rules, paragraphs (single newlines become <br>). Nested lists are flattened.
 */
(function (root) {
  "use strict";

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(raw) {
    var s = esc(raw);
    var codes = [];
    s = s.replace(/`([^`\n]+)`/g, function (_, c) {
      codes.push(c);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return "<code>" + codes[Number(i)] + "</code>";
    });
    return s;
  }

  function isTableSep(line) {
    return line.indexOf("|") >= 0 && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(line);
  }

  function splitRow(line) {
    var t = line.trim();
    if (t.charAt(0) === "|") t = t.slice(1);
    if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
    return t.split("|").map(function (c) { return c.trim(); });
  }

  var UL = /^\s*[-*+]\s+(.*)$/;
  var OL = /^\s*(\d+)[.)]\s+(.*)$/;
  var HR = /^\s*([-*_])(\s*\1){2,}\s*$/;

  function startsBlock(l) {
    return /^(#{1,6}\s|```|\s*>|\s*\|)/.test(l) || UL.test(l) || OL.test(l) || HR.test(l);
  }

  function render(src) {
    var lines = String(src).replace(/\r\n?/g, "\n").split("\n");
    var n = lines.length;
    var out = [];
    var i = 0;
    var m;

    while (i < n) {
      var line = lines[i];

      if (/^\s*$/.test(line)) { i++; continue; }

      if (/^```/.test(line)) {
        var code = [];
        i++;
        while (i < n && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++; // closing fence; at end of input while streaming this just runs past the end
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
        continue;
      }

      m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (m) {
        var lvl = Math.min(m[1].length + 2, 6);
        out.push("<h" + lvl + ">" + inline(m[2]) + "</h" + lvl + ">");
        i++;
        continue;
      }

      if (HR.test(line)) { out.push("<hr>"); i++; continue; }

      if (line.indexOf("|") >= 0 && i + 1 < n && isTableSep(lines[i + 1])) {
        var head = splitRow(line);
        i += 2;
        var rows = [];
        while (i < n && lines[i].indexOf("|") >= 0 && !/^\s*$/.test(lines[i])) rows.push(splitRow(lines[i++]));
        var html = '<div class="table-wrap"><table><thead><tr>' +
          head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>" +
          rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") +
          "</tbody></table></div>";
        out.push(html);
        continue;
      }

      if (/^\s*>/.test(line)) {
        var quote = [];
        while (i < n && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote><p>" + quote.map(inline).join("<br>") + "</p></blockquote>");
        continue;
      }

      if (UL.test(line)) {
        var ul = [];
        while (i < n && (m = lines[i].match(UL))) {
          ul.push(m[1]);
          i++;
          if (i + 1 < n && /^\s*$/.test(lines[i]) && UL.test(lines[i + 1])) i++; // loose list
        }
        out.push("<ul>" + ul.map(function (t) { return "<li>" + inline(t) + "</li>"; }).join("") + "</ul>");
        continue;
      }

      if (OL.test(line)) {
        var ol = [];
        var start = Number(line.match(OL)[1]);
        while (i < n && (m = lines[i].match(OL))) {
          ol.push(m[2]);
          i++;
          if (i + 1 < n && /^\s*$/.test(lines[i]) && OL.test(lines[i + 1])) i++;
        }
        out.push("<ol" + (start !== 1 ? ' start="' + start + '"' : "") + ">" +
          ol.map(function (t) { return "<li>" + inline(t) + "</li>"; }).join("") + "</ol>");
        continue;
      }

      var para = [line];
      i++;
      while (i < n && !/^\s*$/.test(lines[i]) && !startsBlock(lines[i])) para.push(lines[i++]);
      out.push("<p>" + para.map(inline).join("<br>") + "</p>");
    }
    return out.join("\n");
  }

  root.renderMarkdown = render;
  if (typeof module !== "undefined" && module.exports) module.exports = { renderMarkdown: render };
})(typeof window !== "undefined" ? window : globalThis);
