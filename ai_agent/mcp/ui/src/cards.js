  /* ---------- KPI cards, document view, error view ---------- */
  function Cards(p, opts) {
    return h("div", { class: "ag-stack" }, Card([HeadBlock(p, opts, p.actions), Kpis(p.kpis), Notes(p.warnings), Insights(p.insights), Developer(p.developer)]));
  }
  function Document(p, opts) {
    var open = (p.sections || []).filter(function (s) { return !s.collapsed; });
    var more = (p.sections || []).filter(function (s) { return s.collapsed; });
    var tables = (p.tables || []).map(function (t) {
      return Section(t.title, "layers", Table(Object.assign({}, t, { title: "", subtitle: null, embedded: true }), {}));
    });
    return h("div", { class: "ag-stack" }, Card([HeadBlock(p, opts, p.actions), Kpis(p.kpis), Notes(p.warnings),
      h("div", { class: "ag-blocks" }, open.map(function (s) { return Section(s.title, s.icon, FieldGrid(s.fields)); })),
      tables, more.map(function (s) { return Section(s.title, null, FieldGrid(s.fields), true); }),
      Insights(p.insights), Developer(p.developer)]));
  }
  function ErrorView(p, opts) {
    return h("div", { class: "ag-stack" }, Card([ErrorState(p.reason, null, p), Developer(p.developer)]));
  }
