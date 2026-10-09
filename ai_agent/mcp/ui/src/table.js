  /* ---------- table view: data / financial / statement / gl / aging / stock / ranking / comparison ----------
     Renders only the server presentation schema: columns, rows{cells{v,d,tone}}, totals, tabs, filters, paging. */
  function Table(p, opts) {
    var stateKey = "table:" + (p.title || "");
    var firstTab = p.tabs && p.tabs.length ? p.tabs[0].key : null;
    var state = { sortKey: p.sort && (p.sort.key || p.sort[0]) || null, sortDir: p.sort && (p.sort.dir || p.sort.direction || p.sort[1]) || "asc", query: "", page: 0, selected: null,
                  density: Host.loadState(stateKey + ":density") || p.density || "compact", rows: p.rows || [], filters: {}, kpiFilter: null, tab: firstTab,
                  loading: false, showFilters: false, error: null, open: {}, fullscreen: false };
    var paging = p.paging || {};
    var server = !!(paging.token && paging.total > (paging.shown || 0));
    var body = h("div");
    var allCols = (p.columns || []).filter(function (c) { return !c.hidden; });   // hidden columns still carry cell data (tabs, filters)
    var cols = allCols.filter(function (c) { return !c.collapsed; });
    var hidden = allCols.filter(function (c) { return c.collapsed; });
    var dateFilter = (p.filters || []).filter(function (f) { return f.type === "date_range"; })[0] || null;
    var panelFilters = (p.filters || []).filter(function (f) { return f !== dateFilter; });
    var noun = paging.noun || "نتيجة";

    /* ----- client-side filtering and sorting over the loaded rows ----- */
    function activeFilters() { return Object.keys(state.filters).filter(function (k) { var v = state.filters[k]; return v && (typeof v === "string" || v.from || v.to); }); }
    function tabOf() { return (p.tabs || []).filter(function (t) { return t.key === state.tab; })[0]; }
    function matches(r) {
      var q = state.query.trim().toLowerCase();
      if (q && !server && !Object.keys(r.cells).some(function (k) { return String(r.cells[k].d).toLowerCase().indexOf(q) >= 0; })) return false;
      if (state.kpiFilter && !state.kpiFilter(r)) return false;
      var t = tabOf();
      if (t && t.in && !server) { var tv = r.cells[t.col] && r.cells[t.col].v; if (t.in.indexOf(String(tv)) < 0) return false; }
      if (server) return true;
      return activeFilters().every(function (key) {
        var f = (p.filters || []).filter(function (x) { return x.key === key; })[0] || {}, cell = r.cells[key] || {}, v = state.filters[key];
        if (f.type === "date_range") { var d = String(cell.v || "").slice(0, 10); return (!v.from || d >= v.from) && (!v.to || d <= v.to); }
        if (f.type === "text") return String(cell.d).toLowerCase().indexOf(String(v).toLowerCase()) >= 0;
        return String(cell.v) === String(v) || String(cell.d) === String(v);
      });
    }
    function visibleRows() {
      var list = state.rows.filter(matches);
      if (state.sortKey && p.variant !== "statement" && !server) {
        var key = state.sortKey, dir = state.sortDir === "desc" ? -1 : 1;
        list = list.slice().sort(function (a, b) {
          var x = a.cells[key] && a.cells[key].v, y = b.cells[key] && b.cells[key].v;
          if (x == null || x === "") return 1;
          if (y == null || y === "") return -1;
          if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
          return String(x).localeCompare(String(y), "ar") * dir;
        });
      }
      return list;
    }
    function serverFilters() {
      var out = [];
      activeFilters().forEach(function (key) {
        var f = (p.filters || []).filter(function (x) { return x.key === key; })[0] || {}, v = state.filters[key];
        if (f.type === "date_range") { if (v.from) out.push({ field: key, operator: ">=", value: v.from }); if (v.to) out.push({ field: key, operator: "<=", value: v.to }); }
        else if (f.type === "text") out.push({ field: key, operator: "like", value: "%" + v + "%" });
        else out.push({ field: key, operator: "=", value: v });
      });
      var t = tabOf(); if (t && t.in) out.push({ field: t.col, operator: "in", value: t.in });
      return out;
    }

    /* ----- server side: page, sort, search and filters go to the app-only tool, never through the model ----- */
    function serverLoad(patch) {
      state.loading = true; draw();
      var args = { token: paging.token, offset: patch.offset != null ? patch.offset : 0, limit: paging.page_size || 50 };
      var sk = patch.sortKey || state.sortKey;
      if (sk) { args.sort_key = sk; args.sort_dir = patch.sortDir || state.sortDir; }
      var q = patch.search != null ? patch.search : state.query;
      if (q) { args.search = q; args.search_key = paging.search_key; }
      var fl = serverFilters(); if (fl.length) args.filters = fl;
      callTool("view__page", args).then(function (res) {
        var np = presentationOf(res);
        if (res.isError || !np) throw new Error(textOf(res) || "تعذر التحميل");
        p = np; paging = np.paging || {}; state.rows = np.rows || []; state.loading = false; state.page = 0; state.error = null; draw();
      }).catch(function (e) { state.loading = false; state.error = friendly(e); draw(); });
    }
    function refilter() { state.page = 0; if (server) serverLoad({ offset: 0 }); else draw(); }

    /* ----- body: toolbar, filters, table or cards, pager, notes, actions ----- */
    // Every change redraws the body; keep the keyboard user where they were (matched by accessible name).
    function draw() {
      var ae = document.activeElement, label = ae && body.contains(ae) && ae.getAttribute ? ae.getAttribute("aria-label") : null;
      render();
      if (label) { var again = body.querySelector('[aria-label="' + label.replace(/"/g, '\\"') + '"]'); if (again && again !== document.activeElement) again.focus(); }
    }
    function render() {
      clear(body);
      var rows = visibleRows();
      add(body, Toolbar());
      add(body, FilterBar());
      if (state.error) body.appendChild(ErrorState(state.error, function () { state.error = null; refilter(); }));
      if (state.loading) { body.appendChild(h("div", { class: "skeleton", style: "width:90%" })); body.appendChild(h("div", { class: "skeleton", style: "width:70%" })); return; }
      if (!rows.length) { body.appendChild(EmptyState(p.empty, emptyAction())); add(body, footer()); return; }
      var start = server ? 0 : state.page * PAGE;
      var pageRows = server ? rows : rows.slice(start, start + PAGE);
      var hasActions = pageRows.some(function (r) { return r.actions && r.actions.some(function (a) { return a.overflow; }); });
      var span = cols.length + (hidden.length ? 1 : 0) + (hasActions ? 1 : 0);
      var table = h("table", { class: state.density === "compact" ? "compact" : "", role: "grid", "aria-label": p.title || "جدول", "aria-rowcount": rows.length + 1 },
        h("thead", null, h("tr", null, hidden.length ? h("th", { scope: "col", class: "expander", "aria-label": "تفاصيل" }) : null, cols.map(Th), hasActions ? h("th", { scope: "col", "aria-label": "إجراءات" }, "إجراءات") : null)),
        h("tbody", null, bodyRows(pageRows, hasActions, span, start)),
        p.totals ? h("tfoot", null, h("tr", null, hidden.length ? h("td") : null, cols.map(function (c, i) {
          var cell = p.totals.cells[c.key];
          return h("td", { class: c.align === "end" ? "end" : "" }, i === 0 ? p.totals.label : (cell ? h("span", { class: "num", text: cell.d }) : ""));
        }), hasActions ? h("td") : null)) : null);
      table.addEventListener("keydown", tableKeys);
      body.appendChild(h("div", { class: "tablewrap has-cards" }, table));
      body.appendChild(h("div", { class: "cards" }, pageRows.map(function (r) { return RowCard(r); })));
      if (!(p.embedded && rows.length <= PAGE)) body.appendChild(Pager(rows.length, start, pageRows.length));
      add(body, footer());
    }
    function footer() {
      var late = (p.warnings || []).filter(function (w) { return w.code !== "truncated" && w.code !== "mixed_currency"; });
      var acts = (p.actions || []).filter(function (a) { return a.placement !== "header"; });
      return h("div", null, Notes(late), Insights(p.chart ? [] : p.insights), acts.length ? ActionRow(acts, { export: function () { downloadCsv((p.title || "export") + ".csv", toCsv(p, visibleRows())); } }, "ag-actions ag-section") : null);
    }
    function bodyRows(pageRows, hasActions, span, start) {
      var out = [], gkey = p.group_by, last = null;
      pageRows.forEach(function (r, i) {
        if (gkey) {
          var g = r.cells[gkey] ? r.cells[gkey].d : "";
          if (g !== last) { last = g; out.push(GroupRow(g, pageRows.filter(function (x) { return (x.cells[gkey] || {}).d === g; }))); }
        }
        out.push(Tr(r, i, hasActions, start + i + 1));
        if (state.open[r.id || i] && hidden.length) out.push(DetailRow(r, span));
      });
      return out;
    }
    function GroupRow(label, rows) {
      var tr = h("tr", { class: "kind-group", "aria-label": label });
      var first = true;
      if (hidden.length) tr.appendChild(h("td"));
      cols.forEach(function (c) {
        var td = h("td", { class: c.align === "end" ? "end" : "" });
        if (first) { td.appendChild(document.createTextNode(label + " (" + fmtN(rows.length) + ")")); first = false; }
        else if (c.total) {
          var sum = rows.reduce(function (a, r) { return a + (Number((r.cells[c.key] || {}).v) || 0); }, 0);
          var unit = ((rows[0].cells[c.key] || {}).d || "").match(/[A-Za-z؀-ۿ]+$/);   // same currency suffix the cells show
          td.appendChild(h("span", { class: "num", text: fmtN(Math.round(sum * 100) / 100) + (unit ? " " + unit[0] : "") }));
        }
        tr.appendChild(td);
      });
      return tr;
    }
    function DetailRow(r, span) {
      return h("tr", { class: "detail" }, h("td", { colspan: span }, h("dl", { class: "dl" }, hidden.map(function (c) {
        return h("div", null, h("dt", { text: c.label }), h("dd", null, c.type === "status" || c.type === "badge" ? badge(r.cells[c.key]) : h("span", { class: c.align === "end" ? "num" : "", text: cellText(r.cells[c.key]) })));
      }))));
    }

    /* ----- toolbar: search, tabs, date range, filters, density, export ----- */
    function Toolbar() {
      var n = activeFilters().filter(function (k) { return !dateFilter || k !== dateFilter.key; }).length + (state.kpiFilter ? 1 : 0);
      var wantSearch = server || state.rows.length > 8 || !!state.query || !!(p.search_placeholder && !(p.tabs || []).length && p.variant !== "ranking" && !p.embedded);
      var input = h("input", { type: "search", placeholder: p.search_placeholder || (server ? "بحث (Enter)" : "بحث"), "aria-label": "بحث في الجدول" });
      input.value = state.query;
      input.addEventListener("input", function () {
        state.query = input.value; state.page = 0;
        if (server) return;
        var pos = input.selectionStart; draw(); var s = body.querySelector(".search input"); if (s) { s.focus(); try { s.setSelectionRange(pos, pos); } catch (e) {} }
      });
      input.addEventListener("keydown", function (e) { if (e.key === "Enter" && server) serverLoad({ search: input.value, offset: 0 }); });
      var tabs = p.tabs && p.tabs.length ? h("div", { class: "tabs", role: "tablist", "aria-label": "التصنيف" }, p.tabs.map(function (t) {
        return h("button", { class: "tab", type: "button", role: "tab", "aria-selected": String(state.tab === t.key), onclick: function () { state.tab = t.key; refilter(); }, text: t.label });
      })) : null;
      // View options live in one menu so the toolbar stays as lean as the reference: search, tabs, dates, filters.
      var options = [
        { label: state.density === "compact" ? "عرض مريح" : "عرض مضغوط", run: function () { state.density = state.density === "compact" ? "comfortable" : "compact"; Host.saveState(stateKey + ":density", state.density); draw(); } },
        { label: "تصدير CSV", run: function () { downloadCsv((p.title || "export") + ".csv", toCsv(p, visibleRows())); } }];
      if (Host.has("fullscreen")) options.unshift({ label: state.fullscreen ? "تصغير" : "ملء الشاشة", run: function () { state.fullscreen = !state.fullscreen; Host.fullscreen(state.fullscreen); draw(); } });
      var filterBtn = panelFilters.length ? h("button", { class: "btn" + (n ? " on" : ""), type: "button", "aria-expanded": String(state.showFilters), onclick: function () { state.showFilters = !state.showFilters; draw(); } }, icon("filter", 16), n ? "الفلاتر " + n : "الفلاتر") : null;
      var menu = p.rows && !p.embedded && (p.rows.length > 8 || server || p.tabs && p.tabs.length) ? Overflow(options, {}) : null;
      var hasA = !!(tabs || dateFilter), hasB = !!(wantSearch || filterBtn);
      var rowA = hasA ? h("div", { class: "ag-toolbar" }, tabs, h("span", { class: "spacer" }), dateFilter ? DateRange(dateFilter) : null, hasB ? null : menu) : null;
      var rowB = hasB || (!hasA && menu) ? h("div", { class: "ag-toolbar", role: "search" },
        wantSearch ? h("div", { class: "search" }, icon("search", 16), input) : null, h("span", { class: "spacer" }), filterBtn, menu) : null;
      return h("div", null, rowA, rowB);
    }
    function DateRange(f) {
      var v = state.filters[f.key] || {};
      var from = h("input", { type: "date", "aria-label": "من تاريخ" }), to = h("input", { type: "date", "aria-label": "إلى تاريخ" });
      from.value = v.from || ""; to.value = v.to || "";
      var apply = function () { state.filters[f.key] = { from: from.value, to: to.value }; refilter(); };
      from.addEventListener("change", apply); to.addEventListener("change", apply);
      return h("div", { class: "daterange", role: "group", "aria-label": "الفترة" }, h("span", { text: "من" }), from, h("span", { text: "إلى" }), to);
    }
    function FilterBar() {
      var active = activeFilters().filter(function (k) { return !dateFilter || k !== dateFilter.key; });
      if (!panelFilters.length && !state.kpiFilter) return null;
      var bar = h("div", { class: "filterbar" });
      active.forEach(function (key) {
        var f = panelFilters.filter(function (x) { return x.key === key; })[0] || { label: key }, v = state.filters[key];
        var text = typeof v === "string" ? optionLabel(f, v) : (v.from || "…") + " → " + (v.to || "…");
        bar.appendChild(h("button", { class: "chip on", type: "button", "aria-label": "إزالة الفلتر " + f.label, onclick: function () { delete state.filters[key]; refilter(); }, text: f.label + ": " + text + " ×" }));
      });
      if (state.kpiFilter) bar.appendChild(h("button", { class: "chip on", type: "button", onclick: function () { state.kpiFilter = null; refilter(); }, text: state.kpiLabel + " ×" }));
      if (state.showFilters) {
        var panel = h("div", { class: "filterpanel", role: "group", "aria-label": "الفلاتر" });
        panelFilters.forEach(function (f) { panel.appendChild(FilterControl(f)); });
        panel.appendChild(h("div", { class: "ag-actions" },
          h("button", { class: "btn primary", type: "button", onclick: function () { state.showFilters = false; refilter(); }, text: "تطبيق" }),
          h("button", { class: "btn", type: "button", onclick: function () { state.filters = {}; state.kpiFilter = null; state.showFilters = false; refilter(); }, text: "مسح الكل" })));
        bar.appendChild(panel);
      }
      return bar.childNodes.length ? bar : null;
    }
    function optionLabel(f, v) { var o = (f.options || []).filter(function (x) { return String(x.v) === String(v); })[0]; return o ? o.d : v; }
    function FilterControl(f) {
      var v = state.filters[f.key];
      if (f.type === "text") {
        var t = h("input", { type: "text" }); t.value = v || "";
        t.addEventListener("input", function () { state.filters[f.key] = t.value; });
        return h("label", { class: "fld" }, f.label, t);
      }
      var sel = h("select", null, h("option", { value: "", text: "الكل" }), (f.options || []).map(function (o) { return h("option", { value: o.v, text: o.d }); }));
      sel.value = v || "";
      sel.addEventListener("change", function () { if (sel.value) state.filters[f.key] = sel.value; else delete state.filters[f.key]; });
      return h("label", { class: "fld" }, f.label, sel);
    }
    function emptyAction() {
      if (activeFilters().length || state.kpiFilter || state.query || (state.tab && state.tab !== firstTab)) {
        return { label: "مسح الفلاتر", run: function () { state.filters = {}; state.kpiFilter = null; state.query = ""; state.tab = firstTab; refilter(); } };
      }
      var e = p.empty || {};
      if (e.followup && Host.has("message")) return { label: e.followup_label || "اسأل المساعد", run: function () { Host.followUp(e.followup); } };
      return null;
    }

    /* ----- header cells and rows ----- */
    function Th(c) {
      var sorted = state.sortKey === c.key;
      var th = h("th", { scope: "col", class: c.align === "end" ? "end" : "", title: c.tooltip || null, "aria-sort": sorted ? (state.sortDir === "desc" ? "descending" : "ascending") : "none" });
      if (c.sortable === false || p.variant === "statement" || c.key.charAt(0) === "_") { th.appendChild(document.createTextNode(c.label)); return th; }
      th.appendChild(h("button", { type: "button", onclick: function () {
        var dir = sorted && state.sortDir === "asc" ? "desc" : "asc";
        state.sortKey = c.key; state.sortDir = dir; state.page = 0;
        if (server) serverLoad({ sortKey: c.key, sortDir: dir, offset: 0 }); else draw();
      } }, c.label, h("span", { "aria-hidden": "true", style: "font-size:9px", text: sorted ? (state.sortDir === "desc" ? "▼" : "▲") : "" })));
      return th;
    }
    function primaryOpen(r) {
      var a = (r.actions || []).filter(function (x) { return x.kind === "drill"; })[0];
      if (a) return runAction(a, {});
      if (r.href) openLink(r.href);
    }
    function Td(c, r, position) {
      var cell = r.cells[c.key] || { d: "—" };
      if (c.key === "_index") cell = { v: position, d: String(position) };   // the number on screen, whatever the sort
      var tone = cell.tone && c.type !== "status" && c.type !== "badge" ? " tone-" + cell.tone : "";
      var td = h("td", { class: "t-" + c.type + (c.align === "end" ? " end" : "") + (c.priority === "primary" ? " primary" : "") + (c.key.charAt(0) === "_" ? " rank" : "") + tone });
      if (r.level && c.priority === "primary") td.style.paddingInlineStart = (14 + r.level * 16) + "px";
      if (c.type === "status" || c.type === "badge") td.appendChild(badge(cell));
      else if (c.type === "currency" || c.type === "number" || c.type === "percentage" || c.type === "quantity") {
        td.appendChild(h("span", { class: "num", title: cell.d, text: cellText(cell) }));
        if (c.bar) {
          var max = Math.max.apply(null, state.rows.map(function (x) { return Math.abs((x.cells[c.key] || {}).v || 0); })) || 1;
          td.appendChild(h("span", { class: "bar", "aria-hidden": "true" }, h("i", { style: "width:" + Math.round(Math.abs(cell.v || 0) / max * 100) + "%" })));
        }
      } else if ((c.priority === "primary" || c.type === "document_link") && (r.href || (r.actions || []).length)) td.appendChild(h("button", { class: "lnk" + (/[؀-ۿ]/.test(cellText(cell)) ? " plain" : ""), type: "button", text: cellText(cell), onclick: function (e) { e.stopPropagation(); primaryOpen(r); } }));
      else td.appendChild(document.createTextNode(cellText(cell)));
      return td;
    }
    function Tr(r, i, hasActions, position) {
      var key = r.id || i, open = !!state.open[key];
      var tr = h("tr", { tabindex: i === 0 ? "0" : "-1", class: r.kind ? "kind-" + r.kind : "", "aria-selected": String(r.id != null && state.selected === r.id) });
      if (hidden.length) tr.appendChild(h("td", { class: "expander" }, h("button", { class: "btn ghost sm icon", type: "button", "aria-expanded": String(open), "aria-label": "تفاصيل الصف",
        onclick: function (e) { e.stopPropagation(); state.open[key] = !open; draw(); } }, icon(open ? "down" : "left", 16))));
      cols.forEach(function (c) { tr.appendChild(Td(c, r, position)); });
      if (hasActions) {
        var td = h("td", { class: "end", style: "white-space:nowrap" });
        var more = (r.actions || []).filter(function (a) { return a.overflow; });
        if (more.length) td.appendChild(Overflow(more, {}));
        tr.appendChild(td);
      }
      tr.addEventListener("click", function () {
        state.selected = r.id;
        Array.prototype.forEach.call(tr.parentNode.children, function (x) { x.setAttribute("aria-selected", String(x === tr)); });
      });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target === tr) primaryOpen(r); });
      return tr;
    }
    function tableKeys(e) {
      var tr = e.target.closest && e.target.closest("tr");
      if (!tr || !tr.parentNode || tr.parentNode.tagName !== "TBODY") return;
      var next = e.key === "ArrowDown" ? tr.nextElementSibling : e.key === "ArrowUp" ? tr.previousElementSibling : null;
      while (next && (next.classList.contains("detail") || next.classList.contains("kind-group"))) next = e.key === "ArrowDown" ? next.nextElementSibling : next.previousElementSibling;
      if (next) { e.preventDefault(); tr.tabIndex = -1; next.tabIndex = 0; next.focus(); }
    }

    /* ----- mobile row card: identity, status, the two facts that matter, the rest behind details ----- */
    function RowCard(r) {
      var prim = allCols.filter(function (c) { return c.priority === "primary"; })[0] || allCols[0];
      var status = allCols.filter(function (c) { return c.type === "status"; })[0];
      var others = allCols.filter(function (c) { return c !== prim && c !== status && c.key.charAt(0) !== "_"; });
      var moneys = others.filter(function (c) { return c.type === "currency"; }).slice(0, 3);
      var text = others.filter(function (c) { return c.type === "text" || c.type === "badge"; })[0];
      var when = others.filter(function (c) { return c.type === "date" || c.type === "datetime"; })[0];
      var shown = [text, when].concat(moneys).filter(Boolean);
      // Opening / closing rows have no document number: their label sits in the first filled cell.
      var titleCell = r.cells[prim.key];
      if (!titleCell || !titleCell.d || titleCell.d === "—") { for (var ci = 0; ci < allCols.length; ci++) { var cc = r.cells[allCols[ci].key]; if (cc && cc.d && cc.d !== "—" && allCols[ci].type === "text") { titleCell = cc; shown = shown.filter(function (c) { return c.key !== allCols[ci].key; }); break; } } }
      var rest = others.filter(function (c) { return shown.indexOf(c) < 0; });
      function val(c) { var cell = r.cells[c.key]; return c.type === "badge" ? badge(cell) : h("span", { class: c.align === "end" ? "num" : "", text: cellText(cell) }); }
      function line(c) { return h("div", { class: "f" }, h("span", { text: c.label }), h("b", null, val(c))); }
      var card = h("div", { class: "rowcard" },
        h("div", { class: "t" }, h("span", { class: "num", text: cellText(titleCell) }), status ? badge(r.cells[status.key]) : null),
        shown.map(line),
        rest.length ? h("div", { class: "more" }, rest.map(line)) : null,
        h("div", { class: "ag-actions" },
          (r.actions || []).filter(function (a) { return !a.overflow; }).map(function (a) { return actionButton(a, {}); }),
          (r.actions || []).some(function (a) { return a.kind === "drill"; }) ? actionButton((r.actions || []).filter(function (a) { return a.kind === "drill"; })[0], {}) : null,
          rest.length ? h("button", { class: "btn ghost sm", type: "button", "aria-expanded": "false", text: "عرض التفاصيل", onclick: function (e) {
            var open = card.classList.toggle("open"); e.currentTarget.textContent = open ? "إخفاء التفاصيل" : "عرض التفاصيل"; e.currentTarget.setAttribute("aria-expanded", String(open)); } }) : null));
      return card;
    }

    /* ----- pager: numbered pages, never a silent truncation ----- */
    function pageButtons(current, total, go) {
      var nums = [], from = Math.max(1, current - 1), to = Math.min(total, from + 2);
      from = Math.max(1, to - 2);
      for (var i = from; i <= to; i++) nums.push(i);
      return h("div", { class: "pages", role: "navigation", "aria-label": "الصفحات" },
        h("button", { class: "pg", type: "button", disabled: current <= 1, onclick: function () { go(current - 1); }, text: "السابق" }),
        nums.map(function (n) { return h("button", { class: "pg", type: "button", "aria-current": n === current ? "page" : null, onclick: function () { go(n); }, text: String(n) }); }),
        h("button", { class: "pg", type: "button", disabled: current >= total, onclick: function () { go(current + 1); }, text: "التالي" }));
    }
    function Pager(count, start, pageCount) {
      var left, right = null;
      if (server) {
        var off = paging.offset || 0, size = paging.page_size || pageCount || 1, pages = Math.max(1, Math.ceil(paging.total / size));
        left = "إظهار " + fmtN(off + 1) + " - " + fmtN(off + pageCount) + " من " + fmtN(paging.total) + " " + noun;
        right = pageButtons(Math.floor(off / size) + 1, pages, function (n) { serverLoad({ offset: (n - 1) * size }); });
      } else {
        var pagesN = Math.max(1, Math.ceil(count / PAGE));
        left = count > PAGE ? "إظهار " + fmtN(start + 1) + " - " + fmtN(start + pageCount) + " من " + fmtN(count) + " " + noun
          : "إظهار " + (count ? "1 - " + fmtN(count) : "0") + " من " + fmtN(Math.max(count, paging.total || 0)) + " " + noun;
        if (count > PAGE) right = pageButtons(state.page + 1, pagesN, function (n) { state.page = n - 1; draw(); });
      }
      return h("div", { class: "pager" }, h("span", { class: "num", text: left }), right);
    }

    /* ----- assembly: one card, or header+KPIs then chart and table side by side ----- */
    var headerActions = (p.actions || []).filter(function (a) { return a.placement === "header"; });
    var infoNotes = (p.warnings || []).filter(function (w) { return w.code === "truncated" || w.code === "mixed_currency"; });
    var kpis = Kpis(p.kpis, function (k) {
      var f = k.filter;
      state.kpiLabel = k.label;
      state.kpiFilter = function (r) { var v = r.cells[f.col] && r.cells[f.col].v; return f.gt != null ? Number(v) > f.gt : true; };
      state.page = 0; draw();
    });
    draw();
    if (p.embedded) return body;                 // a table inside a document card: no card, header or KPI strip of its own
    var top = [HeadBlock(p, opts, headerActions), kpis, Notes(infoNotes)];
    if (p.chart) {
      var chart = Chart(p.chart);
      return h("div", { class: "ag-stack" },
        Card(top),
        h("div", { class: "ag-two" + (chart ? " has-chart" : "") }, chart ? Card(chart) : null, Card([p.table_title ? h("div", { class: "section-title big", text: p.table_title }) : null, body, Developer(p.developer)])),
        Insights(p.insights));
    }
    return h("div", { class: "ag-stack" }, Card([top, body, Developer(p.developer)]));
  }
  function toCsv(p, rows) {
    var cols = p.columns.filter(function (c) { return c.key.charAt(0) !== "_"; });
    var q = function (s) { s = String(s == null ? "" : s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    return [cols.map(function (c) { return q(c.label); }).join(",")].concat(rows.map(function (r) { return cols.map(function (c) { return q(cellText(r.cells[c.key])); }).join(","); })).join("\n");
  }
