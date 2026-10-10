/**
 * memory.js — the memory side sheet.
 *
 * Responsibility: `AIChat.memoryPanel.create(ctx)` → `{ el, backdrop, open,
 * close, toggle, refresh }`. A 380px sheet on the inline-end edge (a drawer
 * on mobile) that lists the user's memories from `list_memories`, grouped by
 * type (تفضيلات / سياق / تعيينات), with per-item delete (inline confirm →
 * `delete_memory`) and a header action "تذكّر هذه المحادثة الآن" that calls
 * `remember_now` for the current session. Opened from the sidebar footer,
 * Ctrl/Cmd+M, and the memory block chip (store topic "memory:open").
 *
 * ctx: { api }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, relTime } = AIChat;
    const store = AIChat.store;

    const TYPE_ORDER = ["Preference", "Context", "Mapping"];
    const TYPE_LABEL = {
        Preference: () => __("Preferences"),
        Context: () => __("Context"),
        Mapping: () => __("Mappings"),
    };
    const SCOPE_LABEL = {
        User: () => __("User"),
        Agent: () => __("Agent"),
        Company: () => __("Company"),
    };

    function create(ctx) {
        const api = (ctx && ctx.api) || AIChat.api;
        const list = h("div.ac-sheet-body");
        const status = h("div.ac-sheet-status");
        status.hidden = true;
        const btnClose = h("button.ac-icon-btn", { type: "button", title: __("Close"), aria: { label: __("Close") } }, icon("x"));
        const btnRemember = h("button.ac-btn.ac-btn-primary.ac-btn-remember", { type: "button" }, icon("sparkles"), h("span", { text: __("Remember this conversation now") }));
        const el = h(
            "aside.ac-sheet",
            { role: "dialog", aria: { label: __("Memory"), modal: "false" } },
            h("div.ac-sheet-head", h("div.ac-sheet-title", icon("brain"), h("span", { text: __("Memory") })), btnClose),
            h("div.ac-sheet-actions", btnRemember, status),
            list
        );
        const backdrop = h("div.ac-sheet-backdrop");

        let openState = false;
        let loaded = false;
        let busy = false;
        let confirming = null;
        let memories = [];
        let enabled = true;

        btnClose.addEventListener("click", close);
        backdrop.addEventListener("click", close);
        btnRemember.addEventListener("click", rememberNow);
        store.on("session", paintRememberState);
        store.on("memory:open", open);
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && openState && !e.defaultPrevented) {
                e.preventDefault();
                close();
            }
        });

        function paintRememberState() {
            btnRemember.disabled = busy || !store.state.current || !enabled;
            btnRemember.title = store.state.current ? "" : __("Open a conversation first");
        }

        function showStatus(text, kind) {
            status.textContent = text;
            status.dataset.kind = kind || "info";
            status.hidden = !text;
        }

        function rememberNow() {
            const s = store.state.current;
            if (!s || busy) return;
            busy = true;
            paintRememberState();
            const label = btnRemember.lastChild;
            const old = label.textContent;
            label.textContent = __("Extracting…");
            btnRemember.classList.add("is-busy");
            btnRemember.firstChild.replaceWith(icon("loader", "ac-spin"));
            showStatus("");
            api.remember_now({ session: s.name })
                .then((r) => {
                    const n = (r && r.stored) || 0;
                    showStatus(n ? __("Saved {0}", [n]) : __("Nothing new worth saving"), n ? "success" : "info");
                    return refresh();
                })
                .catch((e) => showStatus(AIChat.errorMessage(e), "error"))
                .finally(() => {
                    busy = false;
                    label.textContent = old;
                    btnRemember.classList.remove("is-busy");
                    btnRemember.firstChild.replaceWith(icon("sparkles"));
                    paintRememberState();
                });
        }

        function refresh() {
            list.classList.add("is-loading");
            const agent = store.state.current ? store.state.current.agent : store.state.composer.agent;
            return api
                .list_memories({ agent: agent || null })
                .then((r) => {
                    enabled = !(r && r.enabled === false);
                    memories = (r && r.memories) || [];
                    loaded = true;
                    paint();
                })
                .catch((e) => {
                    AIChat.clear(list);
                    list.appendChild(h("div.ac-error", icon("alert"), h("span.ac-error-text", { text: AIChat.errorMessage(e) })));
                })
                .finally(() => {
                    list.classList.remove("is-loading");
                    paintRememberState();
                });
        }

        function row(m) {
            const btnDel = h("button.ac-icon-btn.ac-memory-delete", { type: "button", title: __("Delete"), aria: { label: __("Delete") } }, icon("trash"));
            const meta = [m.agent, m.entity, m.scope && SCOPE_LABEL[m.scope] ? SCOPE_LABEL[m.scope]() : m.scope, relTime(m.modified)].filter(Boolean);
            const node = h(
                "div.ac-memory",
                { dataset: { name: m.name } },
                h("div.ac-memory-main", h("div.ac-memory-content", { text: m.content || "" }), h("div.ac-memory-meta", meta.map((t) => h("span", { text: t })))),
                btnDel
            );
            btnDel.addEventListener("click", () => confirmDelete(node, m));
            if (confirming === m.name) confirmDelete(node, m);
            return node;
        }

        function confirmDelete(node, m) {
            if (node.querySelector(".ac-confirm")) return;
            confirming = m.name;
            const yes = h("button.ac-btn.ac-btn-danger.ac-btn-xs", { type: "button", text: __("Delete") });
            const no = h("button.ac-btn.ac-btn-ghost.ac-btn-xs", { type: "button", text: __("Cancel") });
            const box = h("div.ac-confirm", h("span", { text: __("Delete this memory?") }), yes, no);
            node.appendChild(box);
            node.classList.add("is-confirming");
            const done = () => {
                confirming = null;
                box.remove();
                node.classList.remove("is-confirming");
            };
            no.addEventListener("click", done);
            yes.addEventListener("click", () => {
                yes.disabled = true;
                api.delete_memory({ memory: m.name })
                    .then(() => {
                        memories = memories.filter((x) => x.name !== m.name);
                        done();
                        paint();
                    })
                    .catch((e) => {
                        yes.disabled = false;
                        AIChat.toast(AIChat.errorMessage(e), "red");
                    });
            });
        }

        function paint() {
            AIChat.clear(list);
            if (!enabled) {
                list.appendChild(h("div.ac-sheet-note", icon("info"), h("span", { text: __("Memory is turned off in the settings; nothing new will be remembered.") })));
            }
            if (!memories.length) {
                list.appendChild(h("div.ac-sheet-empty", h("div.ac-empty-mark", icon("brain")), h("p", { text: __("Nothing remembered yet — it is learned automatically after each conversation") })));
                return;
            }
            const groups = new Map();
            memories.forEach((m) => {
                const t = TYPE_ORDER.includes(m.type) ? m.type : "Context";
                if (!groups.has(t)) groups.set(t, []);
                groups.get(t).push(m);
            });
            TYPE_ORDER.forEach((t) => {
                const items = groups.get(t);
                if (!items || !items.length) return;
                list.appendChild(
                    h(
                        "div.ac-memory-group",
                        h("div.ac-session-group-title", h("span", { text: TYPE_LABEL[t]() }), h("span.ac-count", { text: String(items.length) })),
                        items.map(row)
                    )
                );
            });
        }

        function open() {
            openState = true;
            el.classList.add("is-open");
            backdrop.classList.add("is-visible");
            el.setAttribute("aria-hidden", "false");
            if (!loaded) refresh();
            paintRememberState();
            requestAnimationFrame(() => btnClose.focus({ preventScroll: true }));
        }

        function close() {
            if (!openState) return;
            openState = false;
            el.classList.remove("is-open");
            backdrop.classList.remove("is-visible");
            el.setAttribute("aria-hidden", "true");
        }

        function toggle() {
            if (openState) close();
            else open();
        }

        el.setAttribute("aria-hidden", "true");
        paint();
        return { el, backdrop, open, close, toggle, refresh, isOpen: () => openState };
    }

    AIChat.memoryPanel = { create };
})();
