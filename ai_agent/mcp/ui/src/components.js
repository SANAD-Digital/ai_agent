  /* ---------- what to draw: the presentation schema is the only input ---------- */
  function presentationOf(result) {
    var meta = result && result._meta && result._meta["ai_agent/presentation"];
    if (meta) return meta;
    var env = result && result.structuredContent;
    return env && env.presentation ? env.presentation : null;
  }
  /* structuredContent carries a lean presentation (no rows, no action arguments) plus `ref`; the full one comes from
     `_meta` when the host hands it over, otherwise the view fetches it itself with the app-only tool view__load. */
  function needsLoad(result) {
    var meta = result && result._meta && result._meta["ai_agent/presentation"];
    var p = result && result.structuredContent && result.structuredContent.presentation;
    return !meta && !!p && !!p.ref && !p.full;
  }
  function textOf(result) {
    return ((result && result.content) || []).filter(function (x) { return x.type === "text"; }).map(function (x) { return x.text; }).join("\n");
  }
  function friendly(e) {
    var m = String((e && e.message) || e || "");
    try { var j = JSON.parse(m); if (j.error) return j.error; } catch (x) {}
    return m || T("Something went wrong");
  }
  /* Reading direction of the view (the server builds it rtl for Arabic, ltr otherwise; the host may change it).
     Direction-dependent icons (back, forward, a closed disclosure) follow it. */
  function isRtl() { return document.documentElement.getAttribute("dir") === "rtl"; }
  function cellText(c) { return c && c.d != null ? String(c.d) : "—"; }
  function badge(c) { return h("span", { class: "badge " + (c.tone || "neutral"), text: cellText(c) }); }
  function fmtN(n) { return Number(n).toLocaleString("en-US"); }

  /* Numbers (and a trailing currency code) keep left-to-right order inside Arabic sentences: "210,000 YER" never flips. */
  function rich(text) {
    var s = String(text == null ? "" : text), out = [], re = /(\d[\d,\.]*(?:\s?[A-Za-z]{2,5})?)/g, last = 0, m;
    // A value with no Arabic in it (an id, "1 USD = 530 YER") is one left-to-right run, never reordered piece by piece.
    // A long Latin sentence (an English warning) keeps its order too but must still wrap.
    if (s && !/[\u0600-\u06FF]/.test(s)) return [h("span", { class: s.length > 40 && /\s/.test(s) ? "ltr" : "num", text: s })];
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      out.push(h("span", { class: "num", text: m[1] }));
      last = m.index + m[1].length;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  }

  /* A title mixes Arabic with document numbers and Latin names. A number such as ACC-SINV-2026-00042 must stay on one
     line (a break after "ACC-" strands the hyphen on the wrong side); a Latin phrase keeps its order but may wrap. */
  function titled(text) {
    var s = String(text == null ? "" : text), out = [], re = /[A-Za-z0-9][A-Za-z0-9._\/#&\- ]*[A-Za-z0-9]|[A-Za-z0-9]/g, last = 0, m;
    if (!/[\u0600-\u06FF]/.test(s)) return [s];
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      out.push(h("span", { class: m[0].indexOf(" ") < 0 ? "num" : "ltr", text: m[0] }));
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  }

  /* ---------- shared components ---------- */
  function Notes(list, cls) {
    if (!list || !list.length) return null;
    var glyph = { info: "info", warning: "alert", critical: "alert" };
    return h("div", { class: "ag-notes" + (cls ? " " + cls : "") }, list.map(function (w) {
      return h("div", { class: "note " + w.level, role: w.level === "critical" ? "alert" : "status" }, icon(glyph[w.level] || "info", 16), h("span", null, rich(w.text)));
    }));
  }
  /* Findings computed on the server. A plain sentence is a note; a structured finding is a card with its evidence,
     its likely cause, what it affects, what to do, and a button that opens the records behind it. */
  function Insights(list) {
    if (!list || !list.length) return null;
    return h("div", { class: "ag-notes" }, list.map(function (it) {
      if (typeof it === "string") return h("div", { class: "note insight", role: "status" }, icon("bulb", 16), h("span", null, rich(it)));
      var rows = [[T("Likely cause"), it.cause], [T("Impact"), it.impact], [T("Suggested action"), it.action]].filter(function (r) { return r[1]; });
      return h("article", { class: "finding " + (it.level || "info") },
        h("div", { class: "finding-head" }, icon(it.level === "info" ? "bulb" : "alert", 18), h("b", { text: it.title })),
        h("p", null, rich(it.observation)),
        rows.length ? h("dl", { class: "finding-dl" }, rows.map(function (r) { return h("div", null, h("dt", { text: r[0] }), h("dd", null, rich(r[1]))); })) : null,
        it.documents && it.documents.length ? h("div", { class: "finding-docs" }, it.documents.map(function (d) {
          return d.doctype && d.name ? h("button", { class: "chip", type: "button", onclick: function () { drill("data__get_document", { doctype: d.doctype, name: d.name }); } }, rich(d.label))
            : h("span", { class: "chip static" }, rich(d.label));
        })) : null,
        it.drill && it.drill.arguments ? h("div", { class: "ag-actions" }, h("button", { class: "btn soft sm", type: "button", onclick: function () { drill(it.drill.tool, it.drill.arguments); } }, icon("search", 15), it.drill.label || T("Show details"))) : null);
    }));
  }
  function Kpis(list, onFilter) {
    if (!list || !list.length) return null;
    return h("div", { class: "ag-kpis", role: "list" }, list.map(function (k) {
      var unit = /^(.*\S)\s+([A-Za-z؀-ۿ]{2,5})$/.exec(k.d || "");
      var value = unit && k.type === "currency" ? [h("span", { class: "num", title: k.exact || k.d, text: unit[1] }), h("small", { text: unit[2] })] : [h("span", { class: "num", title: k.exact || k.d, text: k.d })];
      var inner = [h("div", { class: "l", text: k.label }),
        k.icon ? h("span", { class: "ic" }, icon(k.icon, 16)) : null,
        h("div", { class: "v" }, value, k.delta ? h("span", { class: "delta num " + k.delta.tone, text: (k.delta.dir === "up" ? "▲ " : k.delta.dir === "down" ? "▼ " : "") + k.delta.d }) : null),
        k.hint ? h("div", { class: "h" }, rich(k.hint)) : null];
      var cls = "kpi " + (k.accent || k.tone || "neutral");
      if (k.filter && onFilter) return h("button", { class: cls, role: "listitem", type: "button", title: T("Show records"), onclick: function () { onFilter(k); } }, inner);
      return h("div", { class: cls, role: "listitem" }, inner);
    }));
  }
  /* Empty and error are different states: empty is a valid answer, error is a failed call. */
  function EmptyState(empty, action) {
    return h("div", { class: "empty", role: "status" },
      h("b", { text: (empty && empty.title) || T("No results") }),
      h("div", { text: (empty && empty.hint) || "" }),
      action ? h("div", { class: "ag-actions", style: "justify-content:center;margin-top:12px" }, h("button", { class: "btn sm", type: "button", onclick: action.run, text: action.label })) : null);
  }
  function Empty(empty) { return EmptyState(empty, null); }
  /* An error is written for the person: what happened, why, what to do. `view` comes from the server
     (presentation type "error"); a bare message is used for failures inside the view itself. */
  function ErrorState(message, retry, view) {
    var text = friendly({ message: message });
    var v = view || { title: /permission|not accessible|صلاحية|غير مسموح/i.test(text) ? T("You don't have permission for this action") : T("The request could not be completed"), reason: text, suggestion: null,
                      retry: !/permission|not accessible|صلاحية|غير مسموح/i.test(text), kind: "failure" };
    var again = retry && v.retry !== false ? retry : null;
    if (!again && v.retry && Host.has("message")) again = function () { Host.followUp(T("Try again")); };
    return h("div", { class: "errorbox kind-" + (v.kind || "failure"), role: "alert" },
      h("div", { class: "errorbox-head" }, icon(v.kind === "permission" ? "circleAlert" : "alert", 16), h("b", { text: v.title })),
      h("div", null, rich(v.reason)),
      v.suggestion ? h("div", { class: "errorbox-next" }, rich(v.suggestion)) : null,
      again ? h("div", { class: "ag-actions", style: "margin-top:8px" }, h("button", { class: "btn sm", type: "button", onclick: again }, icon("refresh", 15), T("Retry"))) : null);
  }
  /* A titled block inside a card; `collapsed` sections open on demand ("عرض المزيد"). */
  function Section(title, glyph, content, collapsed) {
    if (!collapsed) return h("section", { class: "ag-block" + (content && content.childNodes && content.childNodes.length > 3 || (content && content.tagName !== "DL") ? " wide" : "") },
      h("h2", { class: "ag-block-title" }, glyph ? icon(glyph, 16) : null, title), content);
    var body = h("div", { class: "ag-block-body", hidden: true }, content);
    var btn = h("button", { class: "ag-more", type: "button", "aria-expanded": "false" }, icon("down", 15), T("Show more: {0}", [title]));
    btn.addEventListener("click", function () {
      var open = body.hasAttribute("hidden");
      if (open) body.removeAttribute("hidden"); else body.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", String(open));
      btn.lastChild.textContent = T(open ? "Hide: {0}" : "Show more: {0}", [title]);
      setTimeout(reportSize, 0);
    });
    return h("section", { class: "ag-block" }, btn, body);
  }
  function FieldGrid(fields) {
    return h("dl", { class: "dl" }, fields.map(function (f) {
      return h("div", null, h("dt", { text: f.label }), h("dd", null, f.type === "status" ? badge({ d: f.d, tone: f.tone }) :
        h("span", { class: /currency|number|percentage|quantity/.test(f.type || "") ? "num" : "" }, f.type === "text" || !f.type ? rich(f.d) : f.d), f.hint ? h("div", { class: "ag-sub" }, rich(f.hint)) : null));
    }));
  }
  function Developer(obj) {
    if (!obj || !Object.keys(obj).length) return null;
    return h("details", { class: "dev" }, h("summary", { text: T("Technical details") }), h("pre", { text: JSON.stringify(obj, null, 2) }));
  }

  /* ---------- actions ---------- */
  function actionButton(a, ctx) {
    var cls = "btn " + (a.style || "");
    return h("button", { class: cls, type: "button", onclick: function () { runAction(a, ctx); } }, a.icon ? icon(a.icon, 16) : null, a.label);
  }
  function ActionRow(actions, ctx, cls) {
    var main = (actions || []).filter(function (a) { return !a.overflow; });
    var more = (actions || []).filter(function (a) { return a.overflow; });
    if (!main.length && !more.length) return null;
    return h("div", { class: cls || "ag-actions" }, main.map(function (a) { return actionButton(a, ctx); }), more.length ? Overflow(more, ctx) : null);
  }
  function Overflow(items, ctx) {
    var wrap = h("span", { class: "menu" });
    var btn = h("button", { class: "btn ghost sm icon", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": T("More") }, icon("more", 16));
    var pop = null;
    function close() { if (pop) { pop.remove(); pop = null; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("click", outside, true); } }
    function outside(e) { if (!wrap.contains(e.target)) close(); }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (pop) return close();
      pop = h("div", { class: "pop", role: "menu" }, items.map(function (a) { return h("button", { role: "menuitem", type: "button", text: a.label, onclick: function () { close(); if (a.run) a.run(); else runAction(a, ctx); } }); }));
      wrap.appendChild(pop); btn.setAttribute("aria-expanded", "true");
      document.addEventListener("click", outside, true);
      if (pop.firstChild) pop.firstChild.focus();
    });
    wrap.addEventListener("keydown", function (e) { if (e.key === "Escape") { close(); btn.focus(); } });
    wrap.appendChild(btn);
    return wrap;
  }
  function runAction(a, ctx) {
    if (a.kind === "link") return openLink(a.href);
    if (a.kind === "copy") return copyText((a.arguments && a.arguments.text) || "");
    if (a.kind === "drill") return drill(a.tool, a.arguments);
    if (a.kind === "message") { if (!Host.followUp((a.arguments && a.arguments.text) || a.label)) copyText((a.arguments && a.arguments.text) || a.label); return; }
    if (a.kind === "export" && ctx && ctx.export) return ctx.export();
    if (a.kind === "edit" && ctx && ctx.edit) return ctx.edit();
    if (a.kind === "choose" && ctx && ctx.choose) return ctx.choose();
    if (a.kind === "tool" && ctx && ctx.tool) return ctx.tool(a);
    if (a.kind === "tool") return drill(a.tool, a.arguments);
  }

  /* ---------- header: icon + title on the start side, primary actions on the end ---------- */
  function HeadBlock(p, opts, headerActions) {
    var backBtn = opts && opts.back ? h("button", { class: "ag-back", type: "button", onclick: back }, icon(isRtl() ? "right" : "left", 16), T("Back")) : null;
    var hasHead = p.title || p.subtitle || (headerActions && headerActions.length) || p.status;
    return h("div", null, backBtn, hasHead ? h("div", { class: "ag-head" },
      h("div", { class: "ag-title" }, p.icon ? h("span", { class: "ag-ico" }, icon(p.icon, 17)) : null,
        h("div", null, h("h1", null, titled(p.title || "")), p.subtitle ? h("div", { class: "ag-sub" }, titled(p.subtitle)) : null)),
      p.status ? h("span", { class: "badge lg " + p.status.tone, text: p.status.d }) : null,
      ActionRow(headerActions, {})) : null);
  }
  function Card(children, cls) { return h("section", { class: "ag-card" + (cls ? " " + cls : "") }, children); }

  /* ---------- code: the full source with line numbers, light token colouring and an optional unified diff ----------
     Built from text nodes only (never innerHTML), so code shown for approval cannot run inside the view. */
  var CODE_KEYWORDS = {
    python: "and as assert break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield",
    javascript: "async await break case catch class const continue default delete do else export extends false finally for function if import in instanceof let new null return super switch this throw true try typeof undefined var void while yield",
    sql: "select from where and or not in is null like between group by order having limit offset join left right inner outer on as union all distinct case when then else end with asc desc count sum avg min max ifnull coalesce exists",
    json: "true false null"
  };
  var CODE_RES = {};
  function codeRe(lang) {
    if (CODE_RES[lang]) return CODE_RES[lang];
    var com = lang === "python" ? "#.*$" : lang === "sql" ? "--.*$" : lang === "javascript" ? "\\/\\/.*$" : "(?!)";
    var str = lang === "javascript" ? "`(?:\\\\.|[^`\\\\])*`|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'" : "\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'";
    var kw = (CODE_KEYWORDS[lang] || "").split(" ").join("|");
    var src = "(" + com + ")|(" + str + ")|(%\\(\\w+\\)s)|(\\b\\d+(?:\\.\\d+)?\\b)" + (kw ? "|(\\b(?:" + kw + ")\\b)" : "");
    return (CODE_RES[lang] = new RegExp(src, lang === "sql" ? "gi" : "g"));
  }
  function codeLine(text, lang) {
    var out = [], re = codeRe(lang), last = 0, m, cls = ["c-com", "c-str", "c-var", "c-num", "c-kw"];
    re.lastIndex = 0;
    while ((m = re.exec(text))) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) out.push(text.slice(last, m.index));
      for (var g = 1; g <= 5; g++) if (m[g] !== undefined) { out.push(h("span", { class: cls[g - 1], text: m[0] })); break; }
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  /* code: {language, text, lines, diff, title}; marks: {line: level} from the checks. */
  function CodeBlock(code, marks, labels) {
    var L = labels || {};
    var st = { view: code.diff ? "diff" : "full" };
    var body = h("div", { class: "cz-code-body", tabindex: "0", role: "region", "aria-label": code.title || "" });
    var tabs = code.diff ? h("div", { class: "cz-tabs", role: "tablist" },
      [["diff", L.diff_view || T("Changes")], ["full", L.full_view || T("Full code")]].map(function (x) {
        return h("button", { class: "cz-tab", type: "button", role: "tab", "data-view": x[0], onclick: function () { show(x[0]); } }, x[1]);
      })) : null;
    var copy = h("button", { class: "btn ghost sm", type: "button", onclick: function () { copyText(code.text); flash(L.copied || T("Copied")); } }, icon("copy", 14), L.copy || T("Copy code"));
    var box = h("div", { class: "cz-code", dir: "ltr" },
      h("div", { class: "cz-code-head" }, h("span", { class: "cz-lang", text: code.language }), h("span", { class: "cz-lines num", text: String(code.lines || "") }), tabs, h("span", { class: "grow" }), copy),
      body);
    function full() {
      return h("ol", { class: "cz-src" }, String(code.text).split("\n").map(function (line, i) {
        var n = i + 1, lvl = marks && marks[n];
        return h("li", { class: "cz-line" + (lvl ? " mark-" + lvl : ""), "data-line": n }, h("span", { class: "cz-ln", "aria-hidden": "true", text: String(n) }), h("span", { class: "cz-tx" }, codeLine(line, code.language)));
      }));
    }
    function diff() {
      return h("ol", { class: "cz-src cz-diff" }, String(code.diff).split("\n").map(function (line) {
        var k = line.slice(0, 3) === "+++" || line.slice(0, 3) === "---" ? "meta" : line[0] === "@" ? "hunk" : line[0] === "+" ? "add" : line[0] === "-" ? "del" : "ctx";
        return h("li", { class: "cz-line d-" + k }, h("span", { class: "cz-ln", "aria-hidden": "true", text: k === "add" ? "+" : k === "del" ? "−" : "" }),
          h("span", { class: "cz-tx" }, k === "add" || k === "del" || k === "ctx" ? codeLine(line.slice(1), code.language) : line));
      }));
    }
    function show(view) {
      st.view = view; clear(body); body.appendChild(view === "diff" ? diff() : full());
      if (tabs) Array.prototype.forEach.call(tabs.children, function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-view") === view)); });
      setTimeout(reportSize, 0);
    }
    show(st.view);
    box.goTo = function (n) {
      if (st.view !== "full") show("full");
      var el = body.querySelector('[data-line="' + n + '"]');
      if (!el) return;
      body.scrollTop = el.offsetTop - body.clientHeight / 3;
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    };
    return box;
  }
  /* Analyzer findings, most serious first; a finding with a line jumps to it in the code. */
  function CheckList(checks, onLine, labels) {
    var L = labels || {};
    if (!checks || !checks.length) return h("div", { class: "msg", text: L.no_checks || T("No findings.") });
    var glyph = { ok: "checkCircle", info: "info", warning: "alert", critical: "circleAlert" };
    return h("ul", { class: "cz-checks" }, checks.map(function (c) {
      return h("li", { class: "cz-check lvl-" + c.level, role: c.level === "critical" ? "alert" : null },
        icon(glyph[c.level] || "info", 16), h("span", { class: "grow" }, rich(c.message)),
        c.line && onLine ? h("button", { class: "cz-lineref", type: "button", onclick: function () { onLine(c.line); } }, h("span", { class: "num", text: (L.line || T("Line {0}")).replace("{0}", c.line) })) : null);
    }));
  }
