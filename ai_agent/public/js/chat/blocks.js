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
    const { h, icon, md, __, fmtDuration, prettyJson, toolLabel, escape, formLink, fmtTokens } = AIChat;

    const RENDER_THROTTLE_MS = 50;

    /* ------------------------------------------------------------------ */
    /* shared pieces                                                       */
    /* ------------------------------------------------------------------ */

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
                label.textContent = __("Thinking…");
            } else {
                const ms = b.duration_ms || durationFromTs(b);
                label.textContent = ms ? __("Thought for {0}", [fmtDuration(ms)]) : __("Thoughts");
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

    /* Claude-style step rows: "tool label  argument summary  ›", Request / Response panels */
    const ARG_KEYS = ["doctype", "parent_doctype", "document_name", "name", "report_name", "report", "metric", "group", "action", "file", "skill", "plugin", "query", "q", "txt", "search", "url", "title"];

    function humanKey(key) {
        const text = String(key).replace(/^_+/, "").replace(/[_.]+/g, " ").trim();
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    function isScalar(v) {
        return v !== null && v !== undefined && v !== "" && typeof v !== "object";
    }

    /** "Journal Entry", or "Doctype: Journal Entry Account, Parent doctype: Journal Entry" (like Claude). */
    function argsSummary(data) {
        if (!data || typeof data !== "object") return "";
        const keys = Object.keys(data).filter((k) => !k.startsWith("_") && isScalar(data[k]));
        const ordered = ARG_KEYS.filter((k) => keys.includes(k)).concat(keys.filter((k) => !ARG_KEYS.includes(k)));
        const picked = ordered.slice(0, 2);
        if (!picked.length) return "";
        const text = picked.length === 1 ? String(data[picked[0]]) : picked.map((k) => `${humanKey(k)}: ${data[k]}`).join(", ");
        return text.length > 90 ? text.slice(0, 89) + "…" : text;
    }

    /** Flat key/value rows, one level deep ("Payload item group name"), lists counted. */
    function kvRows(obj, prefix, out) {
        out = out || [];
        for (const key of Object.keys(obj || {})) {
            if (key.startsWith("_") || out.length >= 40) continue;
            const v = obj[key];
            const label = prefix ? `${prefix} ${humanKey(key).toLowerCase()}` : humanKey(key);
            if (isScalar(v)) out.push([label, String(v)]);
            else if (Array.isArray(v)) out.push([label, v.length && v.every(isScalar) && v.join(", ").length < 120 ? v.join(", ") : __("{0} items", [v.length])]);
            else if (v && typeof v === "object" && !prefix) kvRows(v, humanKey(key), out);
            else if (v && typeof v === "object") out.push([label, JSON.stringify(v).slice(0, 120)]);
        }
        return out;
    }

    function kvGrid(obj) {
        const rows = kvRows(obj);
        if (!rows.length) return null;
        return h("div.ac-kv-grid", rows.map(([k, v]) => [h("div.ac-kv-key", { text: k }), h("div.ac-kv-val", { text: v })]).flat());
    }

    function stepPanel(title) {
        const body = h("div.ac-step-panel-body");
        const el = h("div.ac-step-panel", h("div.ac-step-panel-title", { text: title }), body);
        return { el, body };
    }

    function renderTool(block, ctx) {
        const name = block.name || "";
        const title = h("span.ac-tool-title", { text: toolLabel(name) });
        const argsEl = h("span.ac-step-args");
        const failEl = h("span.ac-step-failed", { text: __("Failed") });
        const request = stepPanel(__("Request"));
        const response = stepPanel(__("Response"));
        const outputSec = response.el;
        outputSec.hidden = true;
        const body = h("div.ac-step-body", request.el, response.el);
        const root = collapsible([title, argsEl, failEl], body, {
            class: "ac-block ac-tool",
            open: false,
        });
        let resultBlock = null;
        let lastInput = null;
        let lastOutputKey = null;
        // The MCP App view of the result (same view ChatGPT/Claude render): inside the tool's own
        // collapsed details, so every step of the work stays folded until the user opens it.
        const viewSlot = h("div.ac-tool-view");
        response.el.insertBefore(viewSlot, response.body);
        const card = h("div.ac-tool-wrap", root);
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
            outputSec.hidden = false;
            response.body.hidden = true; // the view is the output; the raw rows would repeat it
        }

        function paintInput(b) {
            const key = b.data && Object.keys(b.data).length ? JSON.stringify(b.data) : b._json || "";
            if (key === lastInput) return;
            lastInput = key;
            const target = request.body;
            AIChat.clear(target);
            argsEl.textContent = argsSummary(b.data);
            if (b.data && Object.keys(b.data).length) target.appendChild(kvGrid(b.data) || jsonPre(b.data));
            else if (b.status === "streaming") target.appendChild(h("div.ac-muted", { text: __("Preparing input…") }));
            else target.appendChild(h("div.ac-muted", { text: "—" }));
        }

        function paintOutput(r) {
            const key = JSON.stringify([r.data, r.text, r.is_error]);
            if (key === lastOutputKey) return;
            lastOutputKey = key;
            outputSec.hidden = false;
            response.body.hidden = !!view;
            const target = outputSec.lastChild;
            AIChat.clear(target);
            let data = r.data;
            if (data && data.view) {
                data = Object.assign({}, data);
                delete data.view;
            }
            if (r.is_error || (data && data.error)) {
                target.appendChild(h("div.ac-tool-error", icon("alert"), h("span", { text: (data && data.error) || r.text || __("Tool failed") })));
                return;
            }
            if (data && Array.isArray(data.rows)) {
                target.appendChild(dataTable(data.rows, data.columns, { limit: 10 }));
                const total = data.row_count || data.total || data.rows.length;
                if (total > 10 || data.is_truncated) {
                    target.appendChild(h("div.ac-muted.ac-small", { text: __("Showing {0} of {1} rows", [Math.min(10, data.rows.length), total]) }));
                }
                return;
            }
            if (data && typeof data === "object" && Object.keys(data).length) {
                target.appendChild(kvGrid(data) || jsonPre(data));
                return;
            }
            if (r.text) {
                target.appendChild(h("pre.ac-json.ac-tool-text", { text: String(r.text).slice(0, 4000) }));
                return;
            }
            target.appendChild(h("div.ac-muted", { text: __("No output") }));
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
            if (b.name && b.name !== name) title.textContent = toolLabel(b.name);
            root.dataset.status = effectiveStatus(b); // CSS: "Failed" tag, running pulse
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
        return {
            el: card,
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
        Pending: () => __("Awaiting decision"),
        Approved: () => __("Approved"),
        Rejected: () => __("Rejected"),
        Expired: () => __("Expired"),
        Executed: () => __("Executed"),
        Failed: () => __("Execution failed"),
    };

    const ACTION_TYPES = {
        Create: () => __("Create"),
        "Bulk Create": () => __("Create multiple"),
        Update: () => __("Edit"),
        Submit: () => __("Submit"),
        Cancel: () => __("Cancel"),
        Workflow: () => __("Workflow"),
        "Approved Method": () => __("Approved action"),
    };

    const AUTO_TYPES = new Set(["Create", "Bulk Create", "Update"]);

    function payloadSummary(payload) {
        if (!payload || typeof payload !== "object") return null;
        const rows = [];
        for (const key of Object.keys(payload)) {
            if (rows.length >= 8) break;
            if (key.startsWith("_") || key === "doctype") continue;
            const v = payload[key];
            let text;
            if (v === null || v === undefined || v === "") continue;
            if (Array.isArray(v)) text = __("{0} lines", [v.length]);
            else if (typeof v === "object") text = __("Object");
            else text = String(v);
            rows.push(h("tr", h("th", { text: key }), h("td", { text: text })));
        }
        if (!rows.length) return null;
        return h("div.ac-table-wrap", h("table.ac-table.ac-kv", h("tbody", rows)));
    }

    function renderApproval(block, ctx) {
        const badge = h("span.ac-badge");
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

        const btnApprove = h("button.ac-btn.ac-btn-primary", { type: "button" }, icon("check"), h("span", { text: __("Approve") }));
        // drafts and edits can switch the conversation to auto mode (policy.AUTO_APPROVE_TYPES)
        const btnAlways = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("check-check"), h("span", { text: __("Always approve in this conversation") }));
        const btnReject = h("button.ac-btn.ac-btn-danger-ghost", { type: "button" }, icon("x"), h("span", { text: __("Reject") }));
        const btnDetails = h("button.ac-btn.ac-btn-ghost", { type: "button" }, h("span", { text: __("Details") }), icon("chevron", "ac-chevron"));
        // Claude's permission card: details on the far side, then Deny · Always allow · Allow once
        const actions = h("div.ac-approval-actions", btnDetails, btnReject, btnAlways, btnApprove);

        const reasonInput = h("textarea.ac-textarea", { rows: 2, placeholder: __("Reason for rejecting (optional)") });
        const btnConfirmReject = h("button.ac-btn.ac-btn-danger", { type: "button", text: __("Confirm rejection") });
        const btnCancelReject = h("button.ac-btn.ac-btn-ghost", { type: "button", text: __("Cancel") });
        reasonBox.append(reasonInput, h("div.ac-row", btnConfirmReject, btnCancelReject));

        const kicker = h("span.ac-approval-kicker");
        const meta = h("div.ac-approval-meta");
        const root = h(
            "div.ac-block.ac-approval",
            h("div.ac-approval-head", h("span.ac-approval-icon", icon("shield")), h("div.ac-approval-heading", kicker, summary, meta), badge),
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
            [btnApprove, btnAlways, btnReject, btnConfirmReject].forEach((x) => (x.disabled = v));
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
                    if (r && r.session && ctx && ctx.store) ctx.store.upsertSession(r.session); // now in auto mode
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
        btnAlways.addEventListener("click", () => decide("approve_always"));
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
            const st = s.status || "Pending";
            kicker.textContent = st === "Pending" ? __("Musaed needs your approval") : __("Approval request");
            meta.textContent = [action, s.doctype_name, s.document_name].filter(Boolean).join(" · ");
            badge.textContent = s.auto && st === "Executed" ? __("Executed automatically") : APPROVAL_STATUS[st] ? APPROVAL_STATUS[st]() : st;
            badge.dataset.status = st;
            root.dataset.status = st;

            summary.textContent = s.action_summary || [__("Approval request"), action, s.doctype_name].filter(Boolean).join(" · ");
            badge.hidden = st === "Pending";
            AIChat.clear(impact);
            if (s.impact_summary) {
                impact.append(icon("info"), h("span", { text: s.impact_summary }));
                impact.hidden = false;
            } else impact.hidden = true;

            AIChat.clear(kv);
            const table = payloadSummary(s.payload); // the request's fields live under "Details", like Claude's parameters
            if (s.document_name) {
                kv.appendChild(h("div.ac-small.ac-muted", h("span", { text: __("Document: ") }), h("a", { href: formLink(s.doctype_name, s.document_name), target: "_blank", rel: "noopener", text: s.document_name })));
            }
            if (s.expires_at && st === "Pending") {
                kv.appendChild(h("div.ac-small.ac-muted", icon("clock"), " ", h("span", { text: __("Expires {0}", [AIChat.relTime(s.expires_at) || s.expires_at]) })));
            }

            AIChat.clear(details);
            if (table) details.appendChild(table);
            details.appendChild(h("div.ac-tool-section-title", { text: __("Full payload") }));
            details.appendChild(jsonPre(s.payload || {}));

            AIChat.clear(outcome);
            const pending = st === "Pending";
            actions.hidden = !pending;
            const canAlways = pending && AUTO_TYPES.has(s.action_type) && !!(AIChat.store && AIChat.store.state.autoApproval);
            btnAlways.hidden = !canAlways;
            btnApprove.lastChild.textContent = canAlways ? __("Approve once") : __("Approve");
            btnDetails.hidden = false;
            if (!pending) {
                const who = s.auto ? __("Auto-approval is on in this conversation") : s.decided_by ? __("By {0}", [s.decided_by]) : "";
                const when = s.decided_at ? AIChat.relTime(s.decided_at) : "";
                const line = [who, when].filter(Boolean).join(" · ");
                if (line) outcome.appendChild(h("div.ac-small.ac-muted", { text: line }));
                if (s.reason) outcome.appendChild(h("div.ac-small", h("strong", { text: __("Reason: ") }), s.reason));
                const created = s.result && s.result.document_name;
                if (created) {
                    outcome.appendChild(
                        h(
                            "a.ac-doc-link",
                            { href: formLink(s.doctype_name, created), target: "_blank", rel: "noopener" },
                            icon("external"),
                            h("span", { text: __("Open {0}", [created]) })
                        )
                    );
                } else if (s.result && typeof s.result === "object" && Object.keys(s.result).length) {
                    outcome.appendChild(jsonPre(s.result));
                }
                // the details toggle stays; move it into the outcome row
                outcome.appendChild(h("div.ac-row", btnDetails));
            } else {
                actions.insertBefore(btnDetails, actions.firstChild); // the far side, before Deny · Always · Allow once
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

    const FILE_LABEL = { excel: "Excel", csv: "CSV", pdf: "PDF", statement: "PDF", image: __("Image") };

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
                            holder.appendChild(h("div.ac-muted", { text: __("The chart could not be drawn") }));
                        }
                    } else {
                        holder.appendChild(jsonPre(s.chart));
                    }
                });
                if (s.rows && s.rows.length) bodyEl.appendChild(h("details.ac-details", h("summary", { text: __("Data") }), dataTable(s.rows, currentColumns(), { sticky: true })));
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
                const dl = h("a.ac-btn.ac-btn-primary.ac-file-download", { href: s.file_url, download: s.file_name || "", target: "_blank", rel: "noopener" }, icon("download"), h("span", { text: __("Download {0}", [FILE_LABEL[s.format] || ""]).trim() }));
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
                    const btn = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("expand"), h("span", { text: __("Show all ({0})", [fmtTokens(spec.row_count || 0)]) }));
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
                    const btn = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("expand"), h("span", { text: expanded ? __("Collapse") : __("Expand") }));
                    btn.addEventListener("click", () => {
                        expanded = !expanded;
                        const wrap = bodyEl.querySelector(".ac-table-wrap");
                        if (wrap) wrap.classList.toggle("is-expanded", expanded);
                        btn.lastChild.textContent = expanded ? __("Collapse") : __("Expand");
                    });
                    footer.appendChild(btn);
                }
                if (rows.length) {
                    const csv = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("download"), h("span", { text: __("Export CSV") }));
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
            titleEl.textContent = spec.title || __("View");
            const parts = [];
            if (spec.artifact_type === "File" && spec.file_size) parts.push(fileSize(spec.file_size));
            if (spec.row_count) parts.push(__("{0} rows", [spec.row_count.toLocaleString()]));
            if (spec.is_truncated) parts.push(__("Preview of the first {0}", [(spec.rows || []).length]));
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
            label.textContent = d.plugin ? __("Plugin: {0}", [d.title || b.name || ""]) : __("Skill loaded: {0}", [d.title || b.name || ""]);
            AIChat.clear(root.firstChild);
            root.firstChild.append(icon(d.plugin ? "puzzle" : "sparkles"), label);
            root.firstChild.title = d.description || "";
        }
        update(block);
        return { el: root, kind: "skill", update };
    }

    function renderMemory(block) {
        const label = h("span.ac-chip-label");
        const chip = h("button.ac-chip.ac-chip-memory", { type: "button", title: __("Open memory") }, icon("brain"), label);
        chip.addEventListener("click", () => AIChat.store.emit("memory:open"));
        const root = h("div.ac-block.ac-chip-row", chip);
        function update(b) {
            const d = b.data || {};
            const note = d.memory || b.text || "";
            label.textContent = __("Saved to memory: {0}", [String(note).slice(0, 80)]);
            chip.title = String(note) + "\n" + __("Click to open memory");
        }
        update(block);
        return { el: root, kind: "memory", update };
    }

    function renderError(block, ctx) {
        const text = h("span.ac-error-text");
        const retry = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("refresh"), h("span", { text: __("Try again") }));
        retry.addEventListener("click", () => ctx && ctx.onRetry && ctx.onRetry());
        retry.hidden = !(ctx && ctx.onRetry);
        const root = h("div.ac-block.ac-error", icon("alert"), text, retry);
        function update(b) {
            text.textContent = b.text || (b.data && b.data.error) || __("Something went wrong");
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
    /* question (ui__ask) and suggestions (ui__suggest)                    */
    /* ------------------------------------------------------------------ */

    /** The next user message after this reply, if any (the question's answer). */
    function answerAfter(ctx) {
        const store = ctx && ctx.store;
        const msg = ctx && ctx.message;
        if (!store || !msg) return null;
        const i = store.state.order.indexOf(msg.name);
        if (i < 0) return null;
        for (const name of store.state.order.slice(i + 1)) {
            const m = store.getMessage(name);
            if (m && m.role === "user") return m;
        }
        return null;
    }

    function renderQuestion(block, ctx) {
        const title = h("div.ac-question-title");
        const list = h("div.ac-question-options", { role: "group" });
        const other = h("input.ac-question-other-input", { type: "text", placeholder: __("Something else…"), aria: { label: __("Something else…") } });
        const btnSend = h("button.ac-btn.ac-btn-primary.ac-btn-sm", { type: "button" }, h("span", { text: __("Send") }), icon("send"));
        const otherRow = h("label.ac-question-option.ac-question-other", h("span.ac-question-mark"), other);
        const footer = h("div.ac-question-foot", btnSend);
        const answered = h("div.ac-question-answered");
        const root = h("div.ac-block.ac-question", h("div.ac-question-head", icon("help"), title), list, otherRow, footer, answered);
        let data = {};
        let picked = new Set();
        let sent = null;

        function answerText() {
            const typed = other.value.trim();
            const labels = data.options.filter((o, i) => picked.has(i)).map((o) => o.label);
            return [...labels, typed].filter(Boolean).join("، ");
        }
        function send(text) {
            if (!text || sent || !ctx || !ctx.onSend) return;
            sent = text;
            paint();
            Promise.resolve(ctx.onSend(text)).catch(() => {
                sent = null;
                paint();
            });
        }
        function paint() {
            const after = answerAfter(ctx);
            const done = sent || (after && after.text);
            root.classList.toggle("is-answered", !!done);
            Array.from(list.children).forEach((el, i) => {
                el.disabled = !!done;
                el.setAttribute("aria-checked", String(picked.has(i) || (!!done && done.split("، ").includes(data.options[i].label))));
            });
            other.disabled = !!done;
            otherRow.hidden = !!done;
            footer.hidden = !!done || (!data.multiple && !other.value.trim());
            btnSend.disabled = !answerText();
            // answered, like Claude: the question in grey, the answer under it in dark text
            list.hidden = !!done;
            answered.hidden = !done;
            answered.textContent = done || "";
        }
        function update(b) {
            data = Object.assign({ options: [] }, b.data || {});
            title.textContent = data.question || "";
            AIChat.clear(list);
            list.setAttribute("aria-label", data.question || "");
            data.options.forEach((o, i) => {
                const opt = h(
                    "button.ac-question-option",
                    { type: "button", role: data.multiple ? "checkbox" : "radio" },
                    h("span.ac-question-mark"),
                    h("span.ac-question-text", h("span.ac-question-label", { text: o.label }), o.description ? h("span.ac-question-desc", { text: o.description }) : null)
                );
                opt.addEventListener("click", () => {
                    if (!data.multiple) return send(o.label); // one choice: picking it is the answer
                    picked.has(i) ? picked.delete(i) : picked.add(i);
                    paint();
                });
                list.appendChild(opt);
            });
            paint();
        }
        other.addEventListener("input", paint);
        other.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.isComposing) {
                e.preventDefault();
                send(answerText());
            }
        });
        btnSend.addEventListener("click", () => send(answerText()));
        const off = ctx && ctx.store ? ctx.store.on("messages", paint) : null;
        update(block);
        return { el: root, kind: "question", update, destroy: () => off && off() };
    }

    function renderSuggestions(block, ctx) {
        const row = h("div.ac-block.ac-suggestions-row", { role: "group", aria: { label: __("Suggestions") } });
        function isLatest() {
            const order = (ctx && ctx.store && ctx.store.state.order) || [];
            return !!(ctx && ctx.message) && order[order.length - 1] === ctx.message.name;
        }
        function paint() {
            row.hidden = !isLatest(); // only the newest reply offers follow-ups
        }
        function update(b) {
            AIChat.clear(row);
            ((b.data && b.data.items) || []).forEach((text) => {
                const chip = h("button.ac-followup", { type: "button" }, h("span", { text }), icon("arrow-up-left", "ac-followup-arrow"));
                chip.addEventListener("click", () => ctx && ctx.onSend && ctx.onSend(text));
                row.appendChild(chip);
            });
            paint();
        }
        const off = ctx && ctx.store ? ctx.store.on("messages", paint) : null;
        update(block);
        return { el: row, kind: "suggestions", update, destroy: () => off && off() };
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
        question: renderQuestion,
        suggestions: renderSuggestions,
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
