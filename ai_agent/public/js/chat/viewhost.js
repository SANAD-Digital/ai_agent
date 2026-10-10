/**
 * viewhost.js — the Desk chat as an MCP Apps host.
 *
 * Responsibility: render a tool result with the very same MCP App view that
 * ChatGPT / Claude load (ai_agent/mcp/ui), inside a sandboxed iframe, and
 * speak the MCP Apps protocol (spec 2026-01-26) to it over postMessage:
 *   ui/initialize → host capabilities + context
 *   ui/notifications/initialized → we send ui/notifications/tool-result
 *   tools/call (paging, sorting, search, drill-down, view__load) → chat.api.view_call
 *   ui/open-link, ui/download-file, ui/notifications/size-changed, ping
 * One view, one presentation layer, two hosts.
 *
 * The frame is as tall as its content and never scrolls on its own (the
 * conversation is the only scroller), and the view receives the chat's
 * palette as the standard MCP Apps style variables, so the card it draws is
 * the chat's own surface rather than a foreign page in a box.
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const PROTOCOL = "2026-01-26";
    const MAX_HEIGHT = 6000;          // a guard against a runaway view, not a layout decision
    const COMPACT_HEIGHT = 264;       // the preview of a tall view inside the floating support panel
    const COMPACT_SLACK = 72;         // a view only a little taller than the preview is shown whole

    let htmlPromise = null;
    function loadHtml() {
        if (!htmlPromise) {
            htmlPromise = frappe.xcall("ai_agent.chat.api.view_html").then((r) => r.html);
            htmlPromise.catch(() => (htmlPromise = null));
        }
        return htmlPromise;
    }

    const frames = new Map();   // contentWindow -> frame controller

    window.addEventListener("message", (event) => {
        const ctl = frames.get(event.source);
        if (!ctl) return;
        const m = event.data;
        if (!m || m.jsonrpc !== "2.0") return;
        ctl.handle(m);
    });

    function theme() {
        return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    }

    /** The chat's tokens under the names the MCP Apps standard gives a host's palette. */
    const STYLE_MAP = {
        "--color-background-primary": "--ac-surface",
        "--color-background-secondary": "--ac-surface-2",
        "--color-background-tertiary": "--ac-surface-3",
        "--color-background-inverse": "--ac-accent",
        "--color-background-success": "--ac-success-soft",
        "--color-background-warning": "--ac-warning-soft",
        "--color-background-danger": "--ac-danger-soft",
        "--color-text-primary": "--ac-text",
        "--color-text-secondary": "--ac-text-2",
        "--color-text-tertiary": "--ac-text-3",
        "--color-text-inverse": "--ac-accent-text",
        "--color-text-success": "--ac-success",
        "--color-text-warning": "--ac-warning",
        "--color-text-danger": "--ac-danger",
        "--color-border-primary": "--ac-border",
        "--color-ring-primary": "--ac-accent",
        "--font-sans": "--ac-font",
    };
    function styles() {
        const cs = getComputedStyle(document.documentElement);
        const variables = {};
        for (const key of Object.keys(STYLE_MAP)) {
            const value = cs.getPropertyValue(STYLE_MAP[key]).trim();
            if (value) variables[key] = value;
        }
        return { variables };
    }

    function locale() {
        return (frappe.boot && frappe.boot.lang) || document.documentElement.lang || "ar";
    }

    /**
     * mount(container, toolResult, {session}) → {el, update(toolResult), destroy()}
     * toolResult is the MCP tool-result object the server stored on the block.
     */
    function mount(toolResult, opts) {
        const wrap = AIChat.h("div.ac-view");
        const frame = document.createElement("iframe");
        frame.className = "ac-view-frame";
        frame.setAttribute("sandbox", "allow-scripts allow-popups allow-downloads allow-popups-to-escape-sandbox");
        frame.setAttribute("title", AIChat.__("Show result"));
        frame.setAttribute("loading", "lazy");
        frame.setAttribute("scrolling", "no");
        frame.style.height = "120px";
        wrap.appendChild(frame);

        let current = toolResult;
        let initialized = false;
        // In a small host (the floating support panel) a tall view would push the
        // answer out of sight: it starts as a short preview with an expand toggle.
        let wanted = 120;
        let expanded = false;
        const more = AIChat.h("button.ac-view-more", { type: "button", hidden: true });
        wrap.appendChild(more);
        // A question the user has to answer (a choice) is never cut to a preview.
        const kindOf = (r) => (((r && r.structuredContent) || {}).presentation || {}).type;
        function applyHeight() {
            const tall = !!wrap.closest(".ac-support") && kindOf(current) !== "choice" && wanted > COMPACT_HEIGHT + COMPACT_SLACK;
            const clipped = tall && !expanded;
            frame.style.height = (clipped ? COMPACT_HEIGHT : Math.min(MAX_HEIGHT, wanted)) + "px";
            wrap.classList.toggle("is-clipped", clipped);
            more.hidden = !tall;
            more.textContent = expanded ? AIChat.__("Show less") : AIChat.__("Full view");
            more.setAttribute("aria-expanded", String(expanded));
        }
        more.addEventListener("click", () => {
            expanded = !expanded;
            applyHeight();
        });

        function post(msg) {
            try {
                frame.contentWindow && frame.contentWindow.postMessage(msg, "*");
            } catch (e) {
                /* frame gone */
            }
        }
        function reply(id, result) {
            post({ jsonrpc: "2.0", id, result });
        }
        function fail(id, message, code) {
            post({ jsonrpc: "2.0", id, error: { code: code || -32000, message: String(message || "error") } });
        }
        function sendResult() {
            if (initialized && current) post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: current });
        }

        const ctl = {
            handle(m) {
                if (m.id != null && !m.method) return; // responses to host requests: none pending
                switch (m.method) {
                    case "ui/initialize":
                        reply(m.id, {
                            protocolVersion: PROTOCOL,
                            hostInfo: { name: "ai-agent-desk", version: "1.0" },
                            hostCapabilities: { serverTools: {}, openLinks: {}, downloadFile: {} },
                            hostContext: {
                                theme: theme(),
                                styles: styles(),
                                locale: locale(),
                                displayMode: "inline",
                                availableDisplayModes: ["inline"],
                                platform: "web",
                            },
                        });
                        return;
                    case "ui/notifications/initialized":
                        initialized = true;
                        sendResult();
                        return;
                    case "ui/notifications/size-changed": {
                        wanted = Math.max(60, Math.ceil((m.params && m.params.height) || 0));
                        applyHeight();
                        return;
                    }
                    case "tools/call": {
                        const p = m.params || {};
                        frappe
                            .xcall("ai_agent.chat.api.view_call", { session: opts.session, name: p.name, arguments: JSON.stringify(p.arguments || {}) })
                            .then((res) => reply(m.id, res))
                            .catch((e) => fail(m.id, AIChat.errorMessage ? AIChat.errorMessage(e) : e && e.message));
                        return;
                    }
                    case "ui/open-link": {
                        const url = m.params && m.params.url;
                        openLink(url);
                        reply(m.id, {});
                        return;
                    }
                    case "ui/download-file": {
                        try {
                            const item = ((m.params && m.params.contents) || [])[0];
                            const res = item && item.resource;
                            if (res) {
                                const name = String(res.uri || "file").split("/").pop();
                                const blob = new Blob([res.text || ""], { type: res.mimeType || "text/plain" });
                                const a = document.createElement("a");
                                a.href = URL.createObjectURL(blob);
                                a.download = name;
                                document.body.appendChild(a);
                                a.click();
                                setTimeout(() => {
                                    URL.revokeObjectURL(a.href);
                                    a.remove();
                                }, 500);
                            }
                            reply(m.id, {});
                        } catch (e) {
                            reply(m.id, { isError: true });
                        }
                        return;
                    }
                    case "ui/message": {
                        // The view asks the assistant something on the user's behalf: put it in the composer.
                        const text = (((m.params && m.params.content) || [])[0] || {}).text;
                        if (text && AIChat.store) AIChat.store.emit("composer:prefill", text);
                        reply(m.id, {});
                        return;
                    }
                    case "ui/update-model-context":
                        reply(m.id, {});
                        return;
                    case "ping":
                        reply(m.id, {});
                        return;
                    default:
                        if (m.id != null) fail(m.id, "Method not found: " + m.method, -32601);
                }
            },
        };

        loadHtml()
            .then((html) => {
                frame.srcdoc = html;
                // contentWindow exists once the element is in a document; register on load too.
                const register = () => frame.contentWindow && frames.set(frame.contentWindow, ctl);
                register();
                frame.addEventListener("load", register);
            })
            .catch(() => {
                wrap.replaceChildren(AIChat.h("div.ac-muted.ac-small", { text: AIChat.__("The view could not load") }));
            });

        // theme follows Desk
        const obs = new MutationObserver(() => {
            if (initialized) post({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: theme(), styles: styles() } });
        });
        obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

        return {
            el: wrap,
            update(next) {
                if (!next || JSON.stringify(next) === JSON.stringify(current)) return;
                current = next;
                sendResult();
            },
            destroy() {
                obs.disconnect();
                if (frame.contentWindow) frames.delete(frame.contentWindow);
            },
        };
    }

    function openLink(url) {
        if (!url) return;
        try {
            const u = new URL(url, window.location.origin);
            if (u.origin === window.location.origin && /^\/(app|desk)\//.test(u.pathname)) {
                frappe.set_route(u.pathname.replace(/^\/(app|desk)\//, "").split("/"));
                return;
            }
        } catch (e) {
            /* fall through */
        }
        window.open(url, "_blank", "noopener");
    }

    AIChat.viewHost = { mount, loadHtml };
})();
