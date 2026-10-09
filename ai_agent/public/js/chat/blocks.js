/**
 * blocks.js — one renderer per block kind.
 *
 * Responsibility: `AIChat.blocks.render(block, ctx)` returns
 * `{ el, kind, update(block), setResult?(block), destroy?() }` so message.js
 * can patch a block in place on every realtime delta instead of re-creating
 * DOM. Kinds: text, thinking, tool_use (+ tool_result folded into the same
 * card), approval, artifact, skill, memory, error, status.
 *
 * ctx: { message, onRetry(), api, store }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, md, __, fmtDuration, prettyJson, toolLabel, toolIcon, toolDotted, escape, formLink, fmtTokens } = AIChat;

    const RENDER_THROTTLE_MS = 50;

    /* ------------------------------------------------------------------ */
    /* shared pieces                                                       */
    /* ------------------------------------------------------------------ */

    function statusIcon(status) {
        const wrap = h("span.ac-status-icon", { dataset: { status } });
        if (status === "streaming" || status === "running" || status === "pending") {
            wrap.appendChild(icon("loader", "ac-spin"));
        } else if (status === "failed" || status === "denied") {
            wrap.appendChild(icon("x"));
        } else {
            wrap.appendChild(icon("check"));
        }
        return wrap;
    }

    function setStatusIcon(wrap, status) {
        if (wrap.dataset.status === status) return;
        wrap.dataset.status = status;
        AIChat.clear(wrap);
        wrap.appendChild(statusIcon(status).firstChild);
    }

    function jsonPre(value) {
        return h("pre.ac-json", { text: prettyJson(value) });
    }

    function cellText(v) {
        if (v === null || v === undefined) return "";
        if (typeof v === "object") return JSON.stringify(v);
        return String(v);
    }

    /** rows: [{...}], columns?: [string] → <table> */
    function dataTable(rows, columns, opts) {
        opts = opts || {};
        rows = Array.isArray(rows) ? rows : [];
        if (!columns || !columns.length) {
            const set = new Set();
            rows.slice(0, 50).forEach((r) => Object.keys(r || {}).forEach((k) => set.add(k)));
            columns = Array.from(set);
        }
        const limit = opts.limit || rows.length;
        const thead = h("thead", h("tr", columns.map((c) => h("th", { text: c }))));
        const tbody = h(
            "tbody",
            rows.slice(0, limit).map((r) =>
                h(
                    "tr",
                    columns.map((c) => {
                        const v = r ? r[c] : "";
                        const num = typeof v === "number";
                        return h("td", { class: num ? "is-num" : null, text: num ? v.toLocaleString() : cellText(v) });
                    })
                )
            )
        );
        const table = h("table.ac-table", thead, tbody);
        return h("div.ac-table-wrap", { class: opts.sticky ? "is-sticky" : null }, table);
    }

    function collapsible(headerChildren, bodyEl, opts) {
        opts = opts || {};
        const chevron = icon("chevron", "ac-chevron");
        const btn = h("button.ac-collapsible-head", { type: "button", aria: { expanded: opts.open ? "true" : "false" } }, headerChildren, chevron);
        const body = h("div.ac-collapsible-body", bodyEl);
        const root = h("div.ac-collapsible", { class: opts.class, dataset: { open: opts.open ? "1" : "0" } }, btn, body);
        body.hidden = !opts.open;
        btn.addEventListener("click", () => {
            const open = root.dataset.open !== "1";
            root.dataset.open = open ? "1" : "0";
            body.hidden = !open;
            btn.setAttribute("aria-expanded", open ? "true" : "false");
            if (opts.onToggle) opts.onToggle(open);
        });
        root.setOpen = (open) => {
            root.dataset.open = open ? "1" : "0";
            body.hidden = !open;
            btn.setAttribute("aria-expanded", open ? "true" : "false");
        };
        return root;
    }

    /* ------------------------------------------------------------------ */
    /* text                                                                */
    /* ------------------------------------------------------------------ */

    function renderText(block) {
        const el = h("div.ac-block.ac-text.ac-md");
        let lastText = null;
        const paint = AIChat.throttle((text) => {
            if (text === lastText) return;
            lastText = text;
            el.innerHTML = md(text);
        }, RENDER_THROTTLE_MS);
        function update(b) {
            el.classList.toggle("is-streaming", b.status === "streaming");
            // mid-stream a lone trailing "-" / "=" line would turn the paragraph above into a heading
            if (b.status === "streaming") paint((b.text || "").replace(/\n[ \t]*[-=*]+[ \t]*$/, "\n"));
            else {
                paint.cancel();
                if ((b.text || "") !== lastText) {
                    lastText = b.text || "";
                    el.innerHTML = md(lastText);
                }
            }
        }
        update(block);
        return { el, kind: "text", update, destroy: () => paint.cancel() };
    }

    /* ------------------------------------------------------------------ */
    /* thinking                                                            */
    /* ------------------------------------------------------------------ */

    /** Thinking is working notes, not an answer: plain text (Markdown turned snake_case names into
        italics), one paragraph per blank line, each taking its own direction (often English in an Arabic chat). */
    function setThinking(body, text) {
        AIChat.clear(body);
        String(text || "")
            .split(/\n\s*\n/)
            .filter((p) => p.trim())
            .forEach((p) => body.appendChild(h("p", { dir: "auto", text: p.trim() })));
    }

    /** Thinking inside a process group (message.js): plain muted text, the group is the collapsible. */
    function renderThinkingInline(block) {
        const body = h("div.ac-thinking-body.ac-md");
        const root = h("div.ac-thinking-inline", body);
        let lastText = null;
        const paint = AIChat.throttle((text) => {
            if (text === lastText) return;
            lastText = text;
            setThinking(body, text);
        }, RENDER_THROTTLE_MS);
        function update(b) {
            root.classList.toggle("is-streaming", b.status === "streaming");
            if (b.status === "streaming") paint(b.text || "");
            else {
                paint.cancel();
                if ((b.text || "") !== lastText) {
                    lastText = b.text || "";
                    setThinking(body, lastText);
                }
            }
        }
        update(block);
        return { el: root, kind: "thinking", update, destroy: () => paint.cancel() };
    }

    function renderThinking(block, ctx) {
        if (ctx && ctx.inGroup) return renderThinkingInline(block);
        const label = h("span.ac-thinking-label");
        const body = h("div.ac-thinking-body.ac-md");
        const root = collapsible([icon("brain"), label], body, { class: "ac-block ac-thinking", open: false });
        let lastText = null;
        const paint = AIChat.throttle((text) => {
            if (text === lastText) return;
            lastText = text;
            setThinking(body, text);
        }, RENDER_THROTTLE_MS);
        function update(b) {
            const streaming = b.status === "streaming";
            root.classList.toggle("is-streaming", streaming);
            if (streaming) {
                label.textContent = __("يفكّر…");
            } else {
                const ms = b.duration_ms || durationFromTs(b);
                label.textContent = ms ? __("فكّر لمدة {0}", [fmtDuration(ms)]) : __("أفكار");
            }
            if (streaming) paint(b.text || "");
            else {
                paint.cancel();
                if ((b.text || "") !== lastText) {
                    lastText = b.text || "";
                    setThinking(body, lastText);
                }
            }
        }
        update(block);
        return { el: root, kind: "thinking", update, destroy: () => paint.cancel() };
    }

    function durationFromTs(b) {
        const a = AIChat.parseTs(b.started_at);
        const z = AIChat.parseTs(b.completed_at);
        if (a && z) return Math.max(0, z - a);
        return 0;
    }

    /* ------------------------------------------------------------------ */
    /* tool card (tool_use + tool_result)                                  */
    /* ------------------------------------------------------------------ */

    function renderTool(block, ctx) {
        const name = block.name || "";
        const status = h("span.ac-status-icon");
        const duration = h("span.ac-tool-duration");
        const title = h("span.ac-tool-title", { text: toolLabel(name) });
        const wire = h("span.ac-tool-wire", { text: toolDotted(name) });
        const inputPre = h("div.ac-tool-section", h("div.ac-tool-section-title", { text: __("المدخلات") }), h("div.ac-tool-section-body"));
        const outputSec = h("div.ac-tool-section", h("div.ac-tool-section-title", { text: __("المخرجات") }), h("div.ac-tool-section-body"));
        outputSec.hidden = true;
        const body = h("div", inputPre, outputSec);
        const root = collapsible([h("span.ac-tool-icon", icon(toolIcon(name))), title, wire, duration, status], body, {
            class: "ac-block ac-tool",
            open: false,
        });
        let resultBlock = null;
        let lastInput = null;
        let lastOutputKey = null;
        // The MCP App view of the result (same view ChatGPT/Claude render), shown outside the collapsed details.
        const viewSlot = h("div.ac-tool-view");
        const card = h("div.ac-tool-wrap", root, viewSlot);
        let view = null;

        function paintView(r) {
            const toolResult = r && r.data && r.data.view;
            if (!toolResult || r.is_error || !AIChat.viewHost) return;
            const session = ctx && ctx.message && ctx.message.session;
            if (view) view.update(toolResult);
            else {
                view = AIChat.viewHost.mount(toolResult, { session });
                viewSlot.appendChild(view.el);
            }
        }

        function paintInput(b) {
            const key = b.data && Object.keys(b.data).length ? JSON.stringify(b.data) : b._json || "";
            if (key === lastInput) return;
            lastInput = key;
            const target = inputPre.lastChild;
            AIChat.clear(target);
            if (b.data && Object.keys(b.data).length) target.appendChild(jsonPre(b.data));
            else if (b.status === "streaming") target.appendChild(h("div.ac-muted", { text: __("جارٍ تجهيز المدخلات…") }));
            else target.appendChild(h("div.ac-muted", { text: "—" }));
        }

        function paintOutput(r) {
            const key = JSON.stringify([r.data, r.text, r.is_error]);
            if (key === lastOutputKey) return;
            lastOutputKey = key;
            outputSec.hidden = false;
            const target = outputSec.lastChild;
            AIChat.clear(target);
            let data = r.data;
            if (data && data.view) {
                data = Object.assign({}, data);
                delete data.view;
            }
            if (r.is_error || (data && data.error)) {
                target.appendChild(h("div.ac-tool-error", icon("alert"), h("span", { text: (data && data.error) || r.text || __("فشلت الأداة") })));
                return;
            }
            if (data && Array.isArray(data.rows)) {
                target.appendChild(dataTable(data.rows, data.columns, { limit: 10 }));
                const total = data.row_count || data.total || data.rows.length;
                if (total > 10 || data.is_truncated) {
                    target.appendChild(h("div.ac-muted.ac-small", { text: __("عرض {0} من {1} صف", [Math.min(10, data.rows.length), total]) }));
                }
                return;
            }
            if (data && typeof data === "object" && Object.keys(data).length) {
                target.appendChild(jsonPre(data));
                return;
            }
            if (r.text) {
                target.appendChild(h("pre.ac-json.ac-tool-text", { text: String(r.text).slice(0, 4000) }));
                return;
            }
            target.appendChild(h("div.ac-muted", { text: __("لا مخرجات") }));
        }

        function effectiveStatus(b) {
            if (resultBlock) return resultBlock.is_error || resultBlock.status === "failed" ? "failed" : "completed";
            if (b.status === "streaming") return "streaming";
            if (b.status === "failed" || b.status === "denied") return "failed";
            // tool_use done but no result yet → the tool is running
            if (b.status === "completed" && ctx && ctx.message && (ctx.message.status === "streaming" || ctx.message.status === "pending")) return "running";
            return "completed";
        }

        function update(b) {
            if (b.name && b.name !== name) {
                title.textContent = toolLabel(b.name);
                wire.textContent = toolDotted(b.name);
            }
            const st = effectiveStatus(b);
            setStatusIcon(status, st);
            root.dataset.status = st;
            const ms = (resultBlock && resultBlock.duration_ms) || b.duration_ms || 0;
            duration.textContent = ms ? fmtDuration(ms) : "";
            paintInput(b);
            if (resultBlock) {
                paintOutput(resultBlock);
                paintView(resultBlock);
            }
        }

        function setResult(r) {
            resultBlock = r;
            update(block);
        }

        update(block);
        const grouped = ctx && ctx.inGroup;
        if (grouped) card.removeChild(viewSlot); // the result view stays in the message flow, outside the collapsed group
        return {
            el: card,
            outside: grouped ? viewSlot : null,
            kind: "tool_use",
            update: (b) => {
                block = b;
                update(b);
            },
            setResult,
        };
    }

    /** A tool_result that arrived without its tool_use (defensive). */
    function renderToolResultOnly(block, ctx) {
        const r = renderTool(Object.assign({}, block, { kind: "tool_use", status: "completed", data: null }), ctx);
        r.setResult(block);
        r.kind = "tool_result";
        r.update = (b) => r.setResult(b);
        return r;
    }

    /* ------------------------------------------------------------------ */
    /* approval                                                            */
    /* ------------------------------------------------------------------ */

    const APPROVAL_STATUS = {
        Pending: () => __("بانتظار القرار"),
        Approved: () => __("تمت الموافقة"),
        Rejected: () => __("مرفوض"),
        Expired: () => __("منتهي"),
        Executed: () => __("نُفّذ"),
        Failed: () => __("فشل التنفيذ"),
    };

    const ACTION_TYPES = {
        Create: () => __("إنشاء"),
        "Bulk Create": () => __("إنشاء متعدد"),
        Update: () => __("تعديل"),
        Submit: () => __("اعتماد"),
        Cancel: () => __("إلغاء"),
        Workflow: () => __("سير عمل"),
        "Approved Method": () => __("إجراء معتمد"),
    };

    function payloadSummary(payload) {
        if (!payload || typeof payload !== "object") return null;
        const rows = [];
        for (const key of Object.keys(payload)) {
            if (rows.length >= 8) break;
            if (key.startsWith("_") || key === "doctype") continue;
            const v = payload[key];
            let text;
            if (v === null || v === undefined || v === "") continue;
            if (Array.isArray(v)) text = __("{0} سطر", [v.length]);
            else if (typeof v === "object") text = __("كائن");
            else text = String(v);
            rows.push(h("tr", h("th", { text: key }), h("td", { text: text })));
        }
        if (!rows.length) return null;
        return h("div.ac-table-wrap", h("table.ac-table.ac-kv", h("tbody", rows)));
    }

    function renderApproval(block, ctx) {
        const badge = h("span.ac-badge");
        const heading = h("span.ac-approval-title");
        const summary = h("div.ac-approval-summary");
        const impact = h("div.ac-approval-impact");
        const kv = h("div.ac-approval-kv");
        const details = h("div.ac-approval-details");
        details.hidden = true;
        const outcome = h("div.ac-approval-outcome");
        const errorLine = h("div.ac-inline-error");
        errorLine.hidden = true;
        const reasonBox = h("div.ac-approval-reason");
        reasonBox.hidden = true;

        const btnApprove = h("button.ac-btn.ac-btn-primary", { type: "button" }, icon("check"), h("span", { text: __("موافقة") }));
        const btnReject = h("button.ac-btn.ac-btn-danger-ghost", { type: "button" }, icon("x"), h("span", { text: __("رفض") }));
        const btnDetails = h("button.ac-btn.ac-btn-ghost", { type: "button" }, h("span", { text: __("التفاصيل") }), icon("chevron", "ac-chevron"));
        const actions = h("div.ac-approval-actions", btnApprove, btnReject, btnDetails);

        const reasonInput = h("textarea.ac-textarea", { rows: 2, placeholder: __("سبب الرفض (اختياري)") });
        const btnConfirmReject = h("button.ac-btn.ac-btn-danger", { type: "button", text: __("تأكيد الرفض") });
        const btnCancelReject = h("button.ac-btn.ac-btn-ghost", { type: "button", text: __("إلغاء") });
        reasonBox.append(reasonInput, h("div.ac-row", btnConfirmReject, btnCancelReject));

        const root = h(
            "div.ac-block.ac-approval",
            h("div.ac-approval-head", h("span.ac-approval-icon", icon("shield")), heading, badge),
            summary,
            impact,
            kv,
            details,
            outcome,
            errorLine,
            reasonBox,
            actions
        );

        let busy = false;
        let current = block;

        function snapshot(b) {
            return (b && b.data) || {};
        }

        function setBusy(v) {
            busy = v;
            [btnApprove, btnReject, btnConfirmReject].forEach((x) => (x.disabled = v));
            root.classList.toggle("is-busy", v);
        }

        function decide(decision, reason) {
            const s = snapshot(current);
            if (!s.approval || busy) return;
            setBusy(true);
            errorLine.hidden = true;
            AIChat.api
                .decide_approval({ approval: s.approval, decision, reason: reason || null })
                .then((r) => {
                    if (r && r.approval) {
                        current = Object.assign({}, current, { data: Object.assign({}, s, r.approval), status: "completed" });
                        // reflect locally right away; approval.decided will confirm
                        const storeMsg = ctx && ctx.message && ctx.store ? ctx.store.getMessage(ctx.message.name) : null;
                        if (storeMsg) {
                            const sb = storeMsg.blocks.find((b) => b.seq === block.seq);
                            if (sb) {
                                sb.data = current.data;
                                sb.status = "completed";
                            }
                            ctx.store.markResumed(storeMsg.name);
                        } else {
                            update(current);
                        }
                    }
                    reasonBox.hidden = true;
                })
                .catch((e) => {
                    errorLine.textContent = AIChat.errorMessage(e);
                    errorLine.hidden = false;
                })
                .finally(() => setBusy(false));
        }

        btnApprove.addEventListener("click", () => decide("approve"));
        btnReject.addEventListener("click", () => {
            reasonBox.hidden = !reasonBox.hidden;
            if (!reasonBox.hidden) reasonInput.focus();
        });
        btnCancelReject.addEventListener("click", () => (reasonBox.hidden = true));
        btnConfirmReject.addEventListener("click", () => decide("reject", reasonInput.value.trim()));
        btnDetails.addEventListener("click", () => {
            details.hidden = !details.hidden;
            btnDetails.classList.toggle("is-open", !details.hidden);
        });

        let lastKey = null;
        function update(b) {
            current = b;
            const s = snapshot(b);
            const key = JSON.stringify([s, b.status]);
            if (key === lastKey) return;
            lastKey = key;

            const action = ACTION_TYPES[s.action_type] ? ACTION_TYPES[s.action_type]() : s.action_type || "";
            heading.textContent = [__("طلب اعتماد"), action, s.doctype_name].filter(Boolean).join(" · ");
            const st = s.status || "Pending";
            badge.textContent = APPROVAL_STATUS[st] ? APPROVAL_STATUS[st]() : st;
            badge.dataset.status = st;
            root.dataset.status = st;

            summary.textContent = s.action_summary || "";
            summary.hidden = !s.action_summary;
            AIChat.clear(impact);
            if (s.impact_summary) {
                impact.append(icon("info"), h("span", { text: s.impact_summary }));
                impact.hidden = false;
            } else impact.hidden = true;

            AIChat.clear(kv);
            const table = payloadSummary(s.payload);
            if (table) kv.appendChild(table);
            if (s.document_name) {
                kv.appendChild(h("div.ac-small.ac-muted", h("span", { text: __("المستند: ") }), h("a", { href: formLink(s.doctype_name, s.document_name), target: "_blank", rel: "noopener", text: s.document_name })));
            }
            if (s.expires_at && st === "Pending") {
                kv.appendChild(h("div.ac-small.ac-muted", icon("clock"), " ", h("span", { text: __("ينتهي {0}", [AIChat.relTime(s.expires_at) || s.expires_at]) })));
            }

            AIChat.clear(details);
            details.appendChild(h("div.ac-tool-section-title", { text: __("الحمولة الكاملة") }));
            details.appendChild(jsonPre(s.payload || {}));

            AIChat.clear(outcome);
            const pending = st === "Pending";
            actions.hidden = !pending;
            btnDetails.hidden = false;
            if (!pending) {
                const who = s.decided_by ? __("بواسطة {0}", [s.decided_by]) : "";
                const when = s.decided_at ? AIChat.relTime(s.decided_at) : "";
                const line = [who, when].filter(Boolean).join(" · ");
                if (line) outcome.appendChild(h("div.ac-small.ac-muted", { text: line }));
                if (s.reason) outcome.appendChild(h("div.ac-small", h("strong", { text: __("السبب: ") }), s.reason));
                const created = s.result && s.result.document_name;
                if (created) {
                    outcome.appendChild(
                        h(
                            "a.ac-doc-link",
                            { href: formLink(s.doctype_name, created), target: "_blank", rel: "noopener" },
                            icon("external"),
                            h("span", { text: __("فتح {0}", [created]) })
                        )
                    );
                } else if (s.result && typeof s.result === "object" && Object.keys(s.result).length) {
                    outcome.appendChild(jsonPre(s.result));
                }
                // the details toggle stays; move it into the outcome row
                outcome.appendChild(h("div.ac-row", btnDetails));
            } else {
                actions.appendChild(btnDetails);
            }
        }

        update(block);
        return { el: root, kind: "approval", update };
    }

    /* ------------------------------------------------------------------ */
    /* artifact                                                            */
    /* ------------------------------------------------------------------ */

    const ARTIFACT_ICON = { Table: "table", Chart: "chart", "Document Links": "link", JSON: "code", Text: "file" };

    function toCsv(columns, rows) {
        const esc = (v) => {
            const s = cellText(v);
            return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
        };
        const lines = [columns.map(esc).join(",")];
        rows.forEach((r) => lines.push(columns.map((c) => esc(r ? r[c] : "")).join(",")));
        return "﻿" + lines.join("\n");
    }

    function download(filename, text, mime) {
        const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = h("a", { href: url, download: filename });
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function chartColors() {
        const css = getComputedStyle(document.documentElement);
        const accent = css.getPropertyValue("--ac-accent").trim() || "#c96442";
        return [accent, "#5b8def", "#3ec27a", "#e0a83a", "#a78bfa", "#f0605d", "#22b8cf"];
    }

    const FILE_LABEL = { excel: "Excel", csv: "CSV", pdf: "PDF", statement: "PDF", image: __("الصورة") };

    function fileSize(bytes) {
        if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
        return Math.max(1, Math.round(bytes / 1024)) + " KB";
    }

    function renderArtifact(block, ctx) {
        const titleEl = h("span.ac-artifact-title");
        const metaEl = h("span.ac-artifact-meta");
        const summaryEl = h("div.ac-artifact-summary");
        const bodyEl = h("div.ac-artifact-body");
        const footer = h("div.ac-artifact-footer");
        const iconWrap = h("span.ac-artifact-icon");
        const root = h("div.ac-block.ac-artifact", h("div.ac-artifact-head", iconWrap, titleEl, metaEl), summaryEl, bodyEl, footer);

        let spec = {};
        let full = null;
        let chart = null;
        let expanded = false;
        let offTheme = null;

        function currentColumns() {
            const s = full || spec;
            if (s.columns && s.columns.length) return s.columns;
            const rows = s.rows || [];
            const set = new Set();
            rows.slice(0, 50).forEach((r) => Object.keys(r || {}).forEach((k) => set.add(k)));
            return Array.from(set);
        }

        function paintBody() {
            const s = full || spec;
            AIChat.clear(bodyEl);
            if (chart) {
                chart = null;
            }
            const type = s.artifact_type || spec.artifact_type;
            if (type === "Chart" && s.chart) {
                const holder = h("div.ac-chart");
                bodyEl.appendChild(holder);
                requestAnimationFrame(() => {
                    if (!holder.isConnected) return;
                    if (window.frappe && frappe.Chart) {
                        try {
                            chart = new frappe.Chart(holder, {
                                data: { labels: s.chart.labels || [], datasets: s.chart.datasets || [] },
                                type: s.chart.type || "bar",
                                height: 260,
                                colors: chartColors(),
                                axisOptions: { xIsSeries: s.chart.type === "line" },
                                barOptions: { spaceRatio: 0.5 },
                            });
                        } catch (e) {
                            holder.appendChild(h("div.ac-muted", { text: __("تعذّر رسم المخطط") }));
                        }
                    } else {
                        holder.appendChild(jsonPre(s.chart));
                    }
                });
                if (s.rows && s.rows.length) bodyEl.appendChild(h("details.ac-details", h("summary", { text: __("البيانات") }), dataTable(s.rows, currentColumns(), { sticky: true })));
                return;
            }
            if (type === "Document Links") {
                const list = h(
                    "ul.ac-link-list",
                    (s.links || []).map((l) =>
                        h("li", h("a", { href: formLink(l.doctype, l.name), target: "_blank", rel: "noopener" }, icon("external"), h("span", { text: l.title || l.name }), h("span.ac-muted.ac-small", { text: " · " + l.doctype })))
                    )
                );
                bodyEl.appendChild(list);
                return;
            }
            if (type === "JSON") {
                bodyEl.appendChild(jsonPre(s.rows && s.rows.length ? s.rows : s.data || s));
                return;
            }
            if (type === "File") {
                // a plugin's output: an image previews inline, every file gets a download button
                if (s.image && s.file_url) bodyEl.appendChild(h("a.ac-file-preview", { href: s.file_url, target: "_blank", rel: "noopener" }, h("img", { src: s.file_url, alt: s.title || "", loading: "lazy" })));
                const dl = h("a.ac-btn.ac-btn-primary.ac-file-download", { href: s.file_url, download: s.file_name || "", target: "_blank", rel: "noopener" }, icon("download"), h("span", { text: __("تحميل {0}", [FILE_LABEL[s.format] || ""]).trim() }));
                bodyEl.appendChild(h("div.ac-file-row", h("span.ac-file-name", { text: s.file_name || "" }), dl));
                return;
            }
            if (type === "Text") {
                bodyEl.appendChild(h("div.ac-md", { html: md(s.text || block.text || "") }));
                return;
            }
            // Table (default)
            const rows = s.rows || [];
            const wrap = dataTable(rows, currentColumns(), { sticky: true });
            wrap.classList.toggle("is-expanded", expanded);
            bodyEl.appendChild(wrap);
        }

        function paintFooter() {
            AIChat.clear(footer);
            const s = full || spec;
            const rows = s.rows || [];
            const type = s.artifact_type || spec.artifact_type;
            if (type === "Table" || type === "Chart") {
                if (spec.is_truncated && !full && spec.artifact) {
                    const btn = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("expand"), h("span", { text: __("عرض الكل ({0})", [fmtTokens(spec.row_count || 0)]) }));
                    btn.addEventListener("click", () => {
                        btn.disabled = true;
                        AIChat.api
                            .get_artifact({ artifact: spec.artifact })
                            .then((r) => {
                                full = r && r.artifact ? r.artifact : r;
                                expanded = true;
                                paintBody();
                                paintFooter();
                            })
                            .catch((e) => {
                                btn.disabled = false;
                                AIChat.toast(AIChat.errorMessage(e), "red");
                            });
                    });
                    footer.appendChild(btn);
                } else if (rows.length > 8) {
                    const btn = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("expand"), h("span", { text: expanded ? __("تصغير") : __("توسيع") }));
                    btn.addEventListener("click", () => {
                        expanded = !expanded;
                        const wrap = bodyEl.querySelector(".ac-table-wrap");
                        if (wrap) wrap.classList.toggle("is-expanded", expanded);
                        btn.lastChild.textContent = expanded ? __("تصغير") : __("توسيع");
                    });
                    footer.appendChild(btn);
                }
                if (rows.length) {
                    const csv = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("download"), h("span", { text: __("تصدير CSV") }));
                    csv.addEventListener("click", () => download((s.title || "artifact").replace(/[^\w؀-ۿ -]+/g, "_") + ".csv", toCsv(currentColumns(), rows), "text/csv;charset=utf-8"));
                    footer.appendChild(csv);
                }
            }
            footer.hidden = !footer.childNodes.length;
        }

        let lastKey = null;
        function update(b) {
            spec = (b && b.data) || {};
            const key = JSON.stringify([spec, b.status]);
            if (key === lastKey) return;
            lastKey = key;
            AIChat.clear(iconWrap);
            iconWrap.appendChild(icon(spec.artifact_type === "File" ? ({ excel: "table", image: "image" }[spec.format] || "file") : ARTIFACT_ICON[spec.artifact_type] || "file"));
            titleEl.textContent = spec.title || __("عرض");
            const parts = [];
            if (spec.artifact_type === "File" && spec.file_size) parts.push(fileSize(spec.file_size));
            if (spec.row_count) parts.push(__("{0} صف", [spec.row_count.toLocaleString()]));
            if (spec.is_truncated) parts.push(__("معاينة أول {0}", [(spec.rows || []).length]));
            metaEl.textContent = parts.join(" · ");
            summaryEl.textContent = spec.summary || "";
            summaryEl.hidden = !spec.summary;
            paintBody();
            paintFooter();
        }

        offTheme = AIChat.store.on("theme", () => {
            if ((full || spec).artifact_type === "Chart") paintBody();
        });

        update(block);
        return { el: root, kind: "artifact", update, destroy: () => offTheme && offTheme() };
    }

    /* ------------------------------------------------------------------ */
    /* chips, error, status                                                */
    /* ------------------------------------------------------------------ */

    function renderSkill(block) {
        const label = h("span.ac-chip-label");
        const root = h("div.ac-block.ac-chip-row", h("span.ac-chip.ac-chip-skill", icon("sparkles"), label));
        function update(b) {
            const d = b.data || {};
            label.textContent = d.plugin ? __("الإضافة: {0}", [d.title || b.name || ""]) : __("تم تحميل المهارة: {0}", [d.title || b.name || ""]);
            AIChat.clear(root.firstChild);
            root.firstChild.append(icon(d.plugin ? "puzzle" : "sparkles"), label);
            root.firstChild.title = d.description || "";
        }
        update(block);
        return { el: root, kind: "skill", update };
    }

    function renderMemory(block) {
        const label = h("span.ac-chip-label");
        const chip = h("button.ac-chip.ac-chip-memory", { type: "button", title: __("فتح الذاكرة") }, icon("brain"), label);
        chip.addEventListener("click", () => AIChat.store.emit("memory:open"));
        const root = h("div.ac-block.ac-chip-row", chip);
        function update(b) {
            const d = b.data || {};
            const note = d.memory || b.text || "";
            label.textContent = __("تم الحفظ في الذاكرة: {0}", [String(note).slice(0, 80)]);
            chip.title = String(note) + "\n" + __("انقر لفتح الذاكرة");
        }
        update(block);
        return { el: root, kind: "memory", update };
    }

    function renderError(block, ctx) {
        const text = h("span.ac-error-text");
        const retry = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("refresh"), h("span", { text: __("إعادة المحاولة") }));
        retry.addEventListener("click", () => ctx && ctx.onRetry && ctx.onRetry());
        retry.hidden = !(ctx && ctx.onRetry);
        const root = h("div.ac-block.ac-error", icon("alert"), text, retry);
        function update(b) {
            text.textContent = b.text || (b.data && b.data.error) || __("حدث خطأ");
        }
        update(block);
        return { el: root, kind: "error", update };
    }

    function renderStatus(block) {
        const root = h("div.ac-block.ac-status-line");
        function update(b) {
            root.textContent = b.text || "";
        }
        update(block);
        return { el: root, kind: "status", update };
    }

    /* ------------------------------------------------------------------ */
    /* dispatch                                                            */
    /* ------------------------------------------------------------------ */

    const RENDERERS = {
        text: renderText,
        thinking: renderThinking,
        tool_use: renderTool,
        tool_result: renderToolResultOnly,
        approval: renderApproval,
        artifact: renderArtifact,
        skill: renderSkill,
        memory: renderMemory,
        error: renderError,
        status: renderStatus,
    };

    function render(block, ctx) {
        const fn = RENDERERS[block.kind] || renderText;
        const r = fn(block, ctx || {});
        r.kind = r.kind || block.kind;
        r.el.dataset.blockSeq = String(block.seq);
        return r;
    }

    AIChat.blocks = { render, dataTable, jsonPre, collapsible, toCsv, download, durationFromTs };
})();
