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
    return m || "حدث خطأ";
  }
  function cellText(c) { return c && c.d != null ? String(c.d) : "—"; }
  function badge(c) { return h("span", { class: "badge " + (c.tone || "neutral"), text: cellText(c) }); }
  function fmtN(n) { return Number(n).toLocaleString("en-US"); }

  /* Numbers (and a trailing currency code) keep left-to-right order inside Arabic sentences: "210,000 YER" never flips. */
  function rich(text) {
    var s = String(text == null ? "" : text), out = [], re = /(\d[\d,\.]*(?:\s?[A-Za-z]{2,5})?)/g, last = 0, m;
    // A value with no Arabic in it (an id, "1 USD = 530 YER") is one left-to-right run, never reordered piece by piece.
    if (s && !/[\u0600-\u06FF]/.test(s)) return [h("span", { class: "num", text: s })];
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
      var rows = [["السبب المحتمل", it.cause], ["الأثر", it.impact], ["الإجراء المقترح", it.action]].filter(function (r) { return r[1]; });
      return h("article", { class: "finding " + (it.level || "info") },
        h("div", { class: "finding-head" }, icon(it.level === "info" ? "bulb" : "alert", 18), h("b", { text: it.title })),
        h("p", null, rich(it.observation)),
        rows.length ? h("dl", { class: "finding-dl" }, rows.map(function (r) { return h("div", null, h("dt", { text: r[0] }), h("dd", null, rich(r[1]))); })) : null,
        it.documents && it.documents.length ? h("div", { class: "finding-docs" }, it.documents.map(function (d) {
          return d.doctype && d.name ? h("button", { class: "chip", type: "button", onclick: function () { drill("data__get_document", { doctype: d.doctype, name: d.name }); } }, rich(d.label))
            : h("span", { class: "chip static" }, rich(d.label));
        })) : null,
        it.drill && it.drill.arguments ? h("div", { class: "ag-actions" }, h("button", { class: "btn soft sm", type: "button", onclick: function () { drill(it.drill.tool, it.drill.arguments); } }, icon("search", 15), it.drill.label || "عرض التفاصيل")) : null);
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
      if (k.filter && onFilter) return h("button", { class: cls, role: "listitem", type: "button", title: "عرض السجلات", onclick: function () { onFilter(k); } }, inner);
      return h("div", { class: cls, role: "listitem" }, inner);
    }));
  }
  /* Empty and error are different states: empty is a valid answer, error is a failed call. */
  function EmptyState(empty, action) {
    return h("div", { class: "empty", role: "status" },
      h("b", { text: (empty && empty.title) || "لا توجد نتائج" }),
      h("div", { text: (empty && empty.hint) || "" }),
      action ? h("div", { class: "ag-actions", style: "justify-content:center;margin-top:12px" }, h("button", { class: "btn sm", type: "button", onclick: action.run, text: action.label })) : null);
  }
  function Empty(empty) { return EmptyState(empty, null); }
  /* An error is written for the person: what happened, why, what to do. `view` comes from the server
     (presentation type "error"); a bare message is used for failures inside the view itself. */
  function ErrorState(message, retry, view) {
    var text = friendly({ message: message });
    var v = view || { title: /permission|not accessible|صلاحية|غير مسموح/i.test(text) ? "لا تملك صلاحية لهذا الإجراء" : "تعذّر تنفيذ الطلب", reason: text, suggestion: null,
                      retry: !/permission|not accessible|صلاحية|غير مسموح/i.test(text), kind: "failure" };
    var again = retry && v.retry !== false ? retry : null;
    if (!again && v.retry && Host.has("message")) again = function () { Host.followUp("أعد المحاولة"); };
    return h("div", { class: "errorbox kind-" + (v.kind || "failure"), role: "alert" },
      h("div", { class: "errorbox-head" }, icon(v.kind === "permission" ? "circleAlert" : "alert", 16), h("b", { text: v.title })),
      h("div", null, rich(v.reason)),
      v.suggestion ? h("div", { class: "errorbox-next" }, rich(v.suggestion)) : null,
      again ? h("div", { class: "ag-actions", style: "margin-top:8px" }, h("button", { class: "btn sm", type: "button", onclick: again }, icon("refresh", 15), "إعادة المحاولة")) : null);
  }
  /* A titled block inside a card; `collapsed` sections open on demand ("عرض المزيد"). */
  function Section(title, glyph, content, collapsed) {
    if (!collapsed) return h("section", { class: "ag-block" + (content && content.childNodes && content.childNodes.length > 3 || (content && content.tagName !== "DL") ? " wide" : "") },
      h("h2", { class: "ag-block-title" }, glyph ? icon(glyph, 16) : null, title), content);
    var body = h("div", { class: "ag-block-body", hidden: true }, content);
    var btn = h("button", { class: "ag-more", type: "button", "aria-expanded": "false" }, icon("down", 15), "عرض المزيد: " + title);
    btn.addEventListener("click", function () {
      var open = body.hasAttribute("hidden");
      if (open) body.removeAttribute("hidden"); else body.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", String(open));
      btn.lastChild.textContent = (open ? "إخفاء: " : "عرض المزيد: ") + title;
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
    return h("details", { class: "dev" }, h("summary", { text: "التفاصيل التقنية" }), h("pre", { text: JSON.stringify(obj, null, 2) }));
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
    var btn = h("button", { class: "btn ghost sm icon", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": "المزيد" }, icon("more", 16));
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
    var backBtn = opts && opts.back ? h("button", { class: "ag-back", type: "button", onclick: back }, icon("right", 16), "رجوع") : null;
    var hasHead = p.title || p.subtitle || (headerActions && headerActions.length) || p.status;
    return h("div", null, backBtn, hasHead ? h("div", { class: "ag-head" },
      h("div", { class: "ag-title" }, p.icon ? h("span", { class: "ag-ico" }, icon(p.icon, 17)) : null,
        h("div", null, h("h1", null, titled(p.title || "")), p.subtitle ? h("div", { class: "ag-sub" }, titled(p.subtitle)) : null)),
      p.status ? h("span", { class: "badge lg " + p.status.tone, text: p.status.d }) : null,
      ActionRow(headerActions, {})) : null);
  }
  function Card(children, cls) { return h("section", { class: "ag-card" + (cls ? " " + cls : "") }, children); }
