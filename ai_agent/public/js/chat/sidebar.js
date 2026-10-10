/**
 * sidebar.js — the sessions sidebar.
 *
 * Responsibility: `AIChat.sidebar.create(ctx)` → `{ el, backdrop, refresh,
 * setOpen, toggle }`. Renders "محادثة جديدة", a debounced search (server
 * `search` param), the session list grouped into مثبّت / اليوم / أمس /
 * آخر 7 أيام / أقدم, a hover menu per row (rename inline, pin, archive,
 * delete with an inline confirm) and the collapse button. Under 992px the
 * same element becomes a drawer with a backdrop (CSS), driven by
 * `store.state.ui.sidebarOpen`.
 *
 * ctx: { onSelect(name), onNew(), onMemory(), api }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, debounce, relTime, parseTs } = AIChat;
    const store = AIChat.store;

    function groupOf(session) {
        if (session.pinned) return "pinned";
        const d = parseTs(session.last_message_on || session.modified || session.creation);
        if (!d) return "older";
        const now = new Date();
        const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
        const t = d.getTime();
        if (t >= startToday) return "today";
        if (t >= startToday - 86400000) return "yesterday";
        if (t >= startToday - 6 * 86400000) return "week";
        return "older";
    }

    const GROUP_ORDER = ["pinned", "today", "yesterday", "week", "older"];
    const GROUP_LABEL = {
        pinned: () => __("Pinned"),
        today: () => __("Today"),
        yesterday: () => __("Yesterday"),
        week: () => __("Last 7 days"),
        older: () => __("Older"),
    };

    function create(ctx) {
        const api = ctx.api || AIChat.api;
        const list = h("div.ac-sessions", { role: "list" });
        const search = h("input.ac-search-input", { type: "search", placeholder: __("Search conversations"), aria: { label: __("Search") } });
        const btnNew = h("button.ac-btn.ac-btn-new", { type: "button" }, icon("plus"), h("span", { text: __("New Conversation") }));
        const btnCollapse = h("button.ac-icon-btn.ac-sidebar-collapse", { type: "button", title: __("Collapse sidebar"), aria: { label: __("Collapse sidebar") } }, icon("panel"));
        const btnArchived = h("button.ac-sidebar-foot-btn", { type: "button" }, icon("archive"), h("span", { text: __("Show archived") }));
        const btnMemory = h("button.ac-sidebar-foot-btn.ac-sidebar-memory", { type: "button", title: __("Memory (Ctrl+M)") }, icon("brain"), h("span", { text: __("Memory") }));
        const empty = h("div.ac-sidebar-empty", { text: __("No conversations yet") });

        const el = h(
            "aside.ac-sidebar",
            { aria: { label: __("Conversations") } },
            h("div.ac-sidebar-head", h("div.ac-brand", icon("sparkles"), h("span", { text: __("Assistant") })), btnCollapse),
            h("div.ac-sidebar-new", btnNew),
            h("div.ac-search", icon("search"), search),
            list,
            h("div.ac-sidebar-foot", btnArchived, btnMemory)
        );
        const backdrop = h("div.ac-backdrop");

        let includeArchived = false;
        let query = "";
        let openMenu = null;
        let editing = null;
        let confirming = null;

        btnNew.addEventListener("click", () => {
            ctx.onNew();
            closeDrawerOnMobile();
        });
        btnCollapse.addEventListener("click", () => setOpen(false));
        btnMemory.addEventListener("click", () => {
            if (ctx.onMemory) ctx.onMemory();
            closeDrawerOnMobile();
        });
        backdrop.addEventListener("click", () => setOpen(false));
        btnArchived.addEventListener("click", () => {
            includeArchived = !includeArchived;
            btnArchived.lastChild.textContent = includeArchived ? __("Hide archived") : __("Show archived");
            btnArchived.classList.toggle("is-active", includeArchived);
            fetch();
        });

        const runSearch = debounce(() => fetch(), 300);
        search.addEventListener("input", () => {
            query = search.value.trim();
            runSearch();
        });
        search.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                search.value = "";
                query = "";
                fetch();
            }
        });

        function fetch() {
            list.classList.add("is-loading");
            return api
                .list_sessions({ limit: 50, offset: 0, include_archived: includeArchived ? 1 : 0, search: query })
                .then((r) => store.setSessions((r && r.sessions) || [], r && r.has_more))
                .catch((e) => AIChat.toast(AIChat.errorMessage(e), "red"))
                .finally(() => list.classList.remove("is-loading"));
        }

        function closeDrawerOnMobile() {
            if (window.matchMedia("(max-width: 991.98px)").matches) setOpen(false);
        }

        function setOpen(open) {
            store.setSidebarOpen(open);
        }

        function toggle() {
            setOpen(!store.state.ui.sidebarOpen);
        }

        /* ---- rows ---- */

        function row(session) {
            const current = store.state.current && store.state.current.name === session.name;
            const title = h("span.ac-session-title", { text: session.title || __("Untitled conversation") });
            const preview = h("span.ac-session-preview", { text: session.preview || session.agent || "" });
            const time = h("span.ac-session-time", { text: relTime(session.last_message_on) });
            const btnMenu = h("button.ac-icon-btn.ac-session-menu-btn", { type: "button", title: __("Options"), aria: { label: __("Options"), haspopup: "menu" } }, icon("more"));
            const main = h("button.ac-session-main", { type: "button" }, h("span.ac-session-line", title, time), preview);
            const node = h(
                "div.ac-session",
                { role: "listitem", class: [current ? "is-current" : "", session.pinned ? "is-pinned" : "", session.status === "Archived" ? "is-archived" : ""].join(" "), dataset: { name: session.name } },
                session.pinned ? h("span.ac-session-pin", icon("pin")) : null,
                main,
                btnMenu
            );
            main.addEventListener("click", () => {
                ctx.onSelect(session.name);
                closeDrawerOnMobile();
            });
            btnMenu.addEventListener("click", (e) => {
                e.stopPropagation();
                toggleMenu(node, session);
            });
            if (editing === session.name) startRename(node, session, true);
            if (confirming === session.name) showConfirm(node, session, true);
            return node;
        }

        function closeMenu() {
            if (openMenu) {
                openMenu.remove();
                openMenu = null;
            }
        }

        function toggleMenu(node, session) {
            if (openMenu && openMenu.parentNode === node) return closeMenu();
            closeMenu();
            const items = [
                ["edit", __("Rename"), () => startRename(node, session)],
                ["pin", session.pinned ? __("Unpin") : __("Pin"), () => pin(session)],
                ["archive", session.status === "Archived" ? __("Unarchive") : __("Archive"), () => archive(session)],
                ["trash", __("Delete"), () => showConfirm(node, session), "is-danger"],
            ];
            const menu = h(
                "div.ac-menu",
                { role: "menu" },
                items.map(([ic, label, fn, cls]) => {
                    const b = h("button.ac-menu-item", { type: "button", role: "menuitem", class: cls }, icon(ic), h("span", { text: label }));
                    b.addEventListener("click", (e) => {
                        e.stopPropagation();
                        closeMenu();
                        fn();
                    });
                    return b;
                })
            );
            node.appendChild(menu);
            openMenu = menu;
        }

        function startRename(node, session, silent) {
            editing = session.name;
            const main = node.querySelector(".ac-session-main");
            const input = h("input.ac-rename-input", { type: "text", value: session.title || "", aria: { label: __("Conversation title") } });
            const form = h("div.ac-rename", input);
            main.hidden = true;
            node.insertBefore(form, main);
            const finish = (save) => {
                if (editing !== session.name) return;
                editing = null;
                const title = input.value.trim();
                form.remove();
                main.hidden = false;
                if (save && title && title !== session.title) {
                    api.rename_session({ session: session.name, title })
                        .then((r) => store.upsertSession((r && r.session) || { name: session.name, title }))
                        .catch((e) => AIChat.toast(AIChat.errorMessage(e), "red"));
                }
            };
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    finish(true);
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    finish(false);
                }
            });
            input.addEventListener("blur", () => finish(true));
            if (!silent) {
                input.focus();
                input.select();
            } else requestAnimationFrame(() => input.focus());
        }

        function showConfirm(node, session, silent) {
            confirming = session.name;
            const yes = h("button.ac-btn.ac-btn-danger.ac-btn-xs", { type: "button", text: __("Delete") });
            const no = h("button.ac-btn.ac-btn-ghost.ac-btn-xs", { type: "button", text: __("Cancel") });
            const box = h("div.ac-confirm", h("span", { text: __("Delete this conversation?") }), yes, no);
            node.appendChild(box);
            node.classList.add("is-confirming");
            const done = () => {
                confirming = null;
                box.remove();
                node.classList.remove("is-confirming");
            };
            no.addEventListener("click", (e) => {
                e.stopPropagation();
                done();
            });
            yes.addEventListener("click", (e) => {
                e.stopPropagation();
                yes.disabled = true;
                api.delete_session({ session: session.name })
                    .then(() => {
                        done();
                        store.removeSession(session.name);
                    })
                    .catch((err) => {
                        yes.disabled = false;
                        AIChat.toast(AIChat.errorMessage(err), "red");
                    });
            });
            void silent;
        }

        function pin(session) {
            const pinned = session.pinned ? 0 : 1;
            api.pin_session({ session: session.name, pinned })
                .then((r) => store.upsertSession((r && r.session) || { name: session.name, pinned }))
                .catch((e) => AIChat.toast(AIChat.errorMessage(e), "red"));
        }

        function archive(session) {
            const archived = session.status === "Archived" ? 0 : 1;
            api.archive_session({ session: session.name, archived })
                .then((r) => {
                    if (archived && !includeArchived) store.removeSession(session.name);
                    else store.upsertSession((r && r.session) || { name: session.name, status: archived ? "Archived" : "Active" });
                })
                .catch((e) => AIChat.toast(AIChat.errorMessage(e), "red"));
        }

        /* ---- list ---- */

        function paint() {
            closeMenu();
            AIChat.clear(list);
            const sessions = store.state.sessions.filter((s) => includeArchived || s.status !== "Archived");
            if (!sessions.length) {
                list.appendChild(empty);
                empty.textContent = query ? __("No results") : __("No conversations yet");
                return;
            }
            const groups = new Map();
            sessions.forEach((s) => {
                const g = groupOf(s);
                if (!groups.has(g)) groups.set(g, []);
                groups.get(g).push(s);
            });
            GROUP_ORDER.forEach((g) => {
                const items = groups.get(g);
                if (!items || !items.length) return;
                items.sort((a, b) => {
                    const ta = parseTs(a.last_message_on) || 0;
                    const tb = parseTs(b.last_message_on) || 0;
                    return tb - ta;
                });
                list.appendChild(h("div.ac-session-group", h("div.ac-session-group-title", { text: GROUP_LABEL[g]() }), items.map(row)));
            });
        }

        store.on("sessions", paint);
        store.on("session", () => {
            Array.from(list.querySelectorAll(".ac-session")).forEach((n) => {
                n.classList.toggle("is-current", !!(store.state.current && n.dataset.name === store.state.current.name));
            });
        });
        store.on("sidebar", (open) => {
            el.classList.toggle("is-open", open);
            backdrop.classList.toggle("is-visible", open);
        });
        document.addEventListener("click", (e) => {
            if (openMenu && !openMenu.contains(e.target)) closeMenu();
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && openMenu) closeMenu();
        });

        paint();
        return { el, backdrop, refresh: fetch, setOpen, toggle, focusSearch: () => search.focus() };
    }

    AIChat.sidebar = { create, groupOf };
})();
