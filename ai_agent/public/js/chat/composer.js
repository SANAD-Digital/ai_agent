/**
 * composer.js — the prompt editor at the bottom of the chat.
 *
 * Responsibility: `AIChat.composer.create(ctx)` → `{ el, focus, getText,
 * setText, setAgent, setStreaming(msg|null), setSession(session), submit,
 * closePopovers }` and `AIChat.composer.createContextMeter()` (shared with the
 * header). Handles autosize (max 40vh), Enter / Shift+Enter with the IME
 * guard, the `/` skill palette, the `@` document search popover, paste/drop
 * uploads with progress chips, agent + model pickers, the thinking toggle,
 * the send/stop button, Esc to stop, ↑ to edit the last user message.
 *
 * ctx: { boot, onSend(payload) → Promise (alias `send`), onStop(), onEditLast(), upload(file, onProgress),
 *        listSkills(agent), searchDocuments(txt),
 *        compact: bool      — the support bubble: 30vh max, no hint line, warning as an info icon
 *        pickers: false     — no agent / model / thinking controls
 *        palette: false     — no `/` skill palette (`@` needs `searchDocuments`)
 *        state: {}          — a private draft state (default: the shared store.state.composer)
 *        placeholder, warning (text), onContextRemoved(context) }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, fmtTokens, fmtBytes, debounce, escape } = AIChat;
    const store = AIChat.store;

    /* ------------------------------------------------------------------ */
    /* context meter                                                       */
    /* ------------------------------------------------------------------ */

    function createContextMeter() {
        const bar = h("span.ac-meter-bar", h("span.ac-meter-fill"));
        const label = h("span.ac-meter-label");
        const el = h("span.ac-meter", { title: __("Current context size out of the model's limit") }, bar, label);
        function update(session) {
            const used = session ? Number(session.context_tokens) || 0 : 0;
            const window_ = session ? Number(session.context_window) || 0 : 0;
            if (!window_) {
                el.hidden = true;
                return;
            }
            el.hidden = false;
            const pct = Math.min(100, Math.round((used / window_) * 100));
            bar.firstChild.style.width = pct + "%";
            el.dataset.level = pct >= 90 ? "danger" : pct >= 75 ? "warning" : "ok";
            label.textContent = fmtTokens(used) + " / " + fmtTokens(window_);
        }
        update(null);
        return { el, update };
    }

    /* ------------------------------------------------------------------ */
    /* popover primitive                                                   */
    /* ------------------------------------------------------------------ */

    const PLUGIN_ICONS = { Excel: "table", CSV: "file", PDF: "file", Image: "image", Chart: "chart", Other: "puzzle" };
    const NAMED_PLUGIN_ICONS = { print: "printer", email: "mail" };
    function pluginIcon(plugin) {
        return (plugin && NAMED_PLUGIN_ICONS[plugin.name]) || PLUGIN_ICONS[plugin && plugin.output] || "puzzle";
    }

    function createPopover(anchorRoot) {
        const el = h("div.ac-popover", { role: "listbox" });
        el.hidden = true;
        anchorRoot.appendChild(el);
        let items = [];
        let active = -1;
        let onPick = null;

        function open(list, opts) {
            items = list;
            onPick = opts.onPick;
            active = list.length ? 0 : -1;
            AIChat.clear(el);
            if (opts.title) el.appendChild(h("div.ac-popover-title", { text: opts.title }));
            if (!list.length) el.appendChild(h("div.ac-popover-empty", { text: opts.empty || __("No results") }));
            let section = null;
            list.forEach((item, i) => {
                if (item.section && item.section !== section) {
                    section = item.section;
                    el.appendChild(h("div.ac-popover-section", { text: section }));
                }
                const row = h(
                    "button.ac-popover-item",
                    { type: "button", role: "option", dataset: { index: i } },
                    item.icon ? h("span.ac-popover-icon", icon(item.icon)) : null,
                    h("span.ac-popover-text", h("span.ac-popover-label", { text: item.label }), item.sub ? h("span.ac-popover-sub", { text: item.sub }) : null),
                    item.selected ? icon("check", "ac-popover-check") : null
                );
                row.addEventListener("mousedown", (e) => e.preventDefault());
                row.addEventListener("click", () => pick(i));
                row.addEventListener("mousemove", () => setActive(i));
                el.appendChild(row);
            });
            el.dataset.kind = opts.kind || "";
            el.hidden = false;
            setActive(active);
        }

        function setActive(i) {
            active = i;
            Array.from(el.querySelectorAll(".ac-popover-item")).forEach((row, j) => {
                row.classList.toggle("is-active", j === i);
                if (j === i) row.scrollIntoView({ block: "nearest" });
            });
        }

        function move(delta) {
            if (!items.length) return;
            setActive((active + delta + items.length) % items.length);
        }

        function pick(i) {
            const item = items[i === undefined ? active : i];
            if (!item || !onPick) return close();
            onPick(item);
            close();
        }

        function close() {
            el.hidden = true;
            items = [];
            active = -1;
            onPick = null;
        }

        return { el, open, close, move, pick, isOpen: () => !el.hidden, kind: () => el.dataset.kind };
    }

    /* ------------------------------------------------------------------ */
    /* composer                                                            */
    /* ------------------------------------------------------------------ */

    function create(ctx) {
        const c = ctx.state || store.state.composer;
        const compact = !!ctx.compact;
        const pickers = ctx.pickers !== false;
        const palette = ctx.palette !== false;
        const onSend = ctx.send || ctx.onSend;
        // boot arrives after the composer is built, so always read it lazily
        const getBoot = () => ctx.boot || store.state.boot || {};
        const skillCache = new Map();

        const ta = h("textarea.ac-input", { rows: 1, dir: AIChat.isRtl() ? "rtl" : "ltr", placeholder: ctx.placeholder || (palette ? __("Type your message… (/ for skills and plugins, @ for documents)") : __("Type your message…")), aria: { label: __("Message") } });
        const chips = h("div.ac-composer-chips");
        chips.hidden = true;
        const fileInput = h("input.ac-visually-hidden", { type: "file", multiple: true, tabindex: -1, aria: { hidden: "true" } });

        const btnAttach = toolBtn("paperclip", __("Attach a file"), () => fileInput.click());
        const btnAgent = toolBtn("bot", __("Agent"), () => toggleAgentPicker(), true);
        const btnModel = toolBtn("zap", __("Model"), () => toggleModelPicker(), true);
        const btnThinking = toolBtn("brain", __("Thinking"), () => cycleThinking(), true);
        const btnSend = h("button.ac-send", { type: "button", aria: { label: __("Send") } }, icon("send"));
        const hint = h("div.ac-composer-hint");
        const warning = h("div.ac-composer-warning");
        warning.hidden = true;
        // compact: the third-party notice collapses to an info icon with a tooltip
        const info = h("span.ac-composer-info", { tabindex: 0, role: "note" }, icon("info"));
        info.hidden = true;
        const errorText = h("span");
        const errorRetry = h("button.ac-btn.ac-btn-ghost.ac-btn-xs", { type: "button" }, icon("refresh"), h("span", { text: __("Try again") }));
        const errorLine = h("div.ac-composer-error", { role: "alert" }, icon("alert"), errorText, errorRetry);
        errorLine.hidden = true;

        const tools = h("div.ac-composer-tools", btnAttach, pickers ? [btnAgent, btnModel, btnThinking] : null, compact ? info : null);
        const bar = h("div.ac-composer-bar", tools, h("div.ac-composer-right", btnSend));
        const box = h("div.ac-composer-box", chips, ta, bar);
        const dropHint = h("div.ac-drop-hint", icon("paperclip"), h("span", { text: __("Drop files here") }));
        const el = h("div.ac-composer", { class: compact ? "is-compact" : null }, box, dropHint, errorLine, compact ? null : hint, compact ? null : warning, fileInput);
        const popover = createPopover(el);

        let streamingMsg = null;
        let session = null;
        let submitting = false;
        let atQuery = null; // {start, end, query}

        /* ---- helpers ---- */

        function toolBtn(name, label, onClick, withText) {
            const b = h("button.ac-tool-btn", { type: "button", title: label, aria: { label } }, icon(name), withText ? h("span.ac-tool-btn-text") : null);
            b.addEventListener("click", (e) => {
                e.preventDefault();
                onClick(e);
            });
            return b;
        }

        function agentOf(name) {
            return (getBoot().agents || []).find((a) => a.name === name) || null;
        }

        function modelOf(provider, model) {
            const p = (getBoot().providers || []).find((x) => x.name === provider);
            if (!p) return null;
            return (p.models || []).find((m) => m.model_name === model) || null;
        }

        function defaults() {
            if (!c.agent) {
                const a = (getBoot().agents || []).find((x) => x.is_default) || (getBoot().agents || [])[0];
                if (a) c.agent = a.name;
            }
            if (!c.provider || !c.model) {
                const p = (getBoot().providers || []).find((x) => x.is_default) || (getBoot().providers || [])[0];
                if (p) {
                    c.provider = p.name;
                    const m = (p.models || []).find((x) => x.is_default) || (p.models || [])[0];
                    if (m) c.model = m.model_name;
                }
            }
            const opts = (getBoot().settings && getBoot().settings.thinking_options) || ["off", "adaptive", "on"];
            if (!opts.includes(c.thinking)) c.thinking = opts[0];
        }

        function paintPickers() {
            const a = agentOf(c.agent);
            btnAgent.lastChild.textContent = a ? a.agent_name || a.name : __("Agent");
            btnAgent.disabled = !!session; // the session owns its agent
            btnAgent.title = session ? __("The agent is fixed for this conversation") : __("Agent");
            const m = modelOf(c.provider, c.model);
            btnModel.lastChild.textContent = m ? m.label || m.model_name : c.model || __("Model");
            const supports = !!(m && m.supports_thinking);
            btnThinking.hidden = !supports;
            const labels = { off: __("No thinking"), adaptive: __("Adaptive thinking"), on: __("Thinking") };
            btnThinking.lastChild.textContent = labels[c.thinking] || c.thinking;
            btnThinking.dataset.state = c.thinking;
            const notice = ctx.warning !== undefined ? ctx.warning : getBoot().settings && getBoot().settings.third_party_warning;
            warning.hidden = !notice;
            warning.textContent = notice || "";
            info.hidden = !notice;
            info.title = notice || "";
            info.setAttribute("aria-label", notice || "");
        }

        function paintChips() {
            AIChat.clear(chips);
            if (c.skill) {
                chips.appendChild(
                    chip("sparkles", __("Skill: {0}", [c.skill.title || c.skill.name]), () => {
                        c.skill = null;
                        paintChips();
                    })
                );
            }
            if (c.plugin) {
                chips.appendChild(
                    chip(pluginIcon(c.plugin), __("Plugin: {0}", [c.plugin.title || c.plugin.name]), () => {
                        c.plugin = null;
                        paintChips();
                    })
                );
            }
            if (c.context) {
                const node = chip(c.context.label ? "file" : "link", c.context.label || c.context.title || c.context.name, () => {
                    const removed = c.context;
                    c.context = null;
                    paintChips();
                    if (ctx.onContextRemoved) ctx.onContextRemoved(removed);
                }, c.context.label ? null : c.context.doctype);
                node.classList.add("ac-chip-context");
                node.title = c.context.label || "";
                chips.appendChild(node);
            }
            c.files.forEach((f) => {
                const node = chip("file", f.name, () => removeFile(f), fmtBytes(f.size));
                node.classList.add("ac-chip-upload");
                node.dataset.id = f.id;
                if (f.error) {
                    node.classList.add("is-error");
                    node.title = f.error;
                } else if (!f.doc) {
                    const prog = h("span.ac-chip-progress", h("span.ac-chip-progress-fill"));
                    prog.firstChild.style.width = Math.round((f.progress || 0) * 100) + "%";
                    node.appendChild(prog);
                }
                chips.appendChild(node);
            });
            chips.hidden = !chips.childNodes.length;
            paintSend();
        }

        function chip(ic, label, onRemove, meta) {
            const x = h("button.ac-chip-remove", { type: "button", aria: { label: __("Remove") } }, icon("x"));
            x.addEventListener("click", (e) => {
                e.stopPropagation();
                onRemove();
            });
            return h("span.ac-chip", icon(ic), h("span.ac-chip-label", { text: label }), meta ? h("span.ac-chip-meta", { text: meta }) : null, x);
        }

        function uploading() {
            return c.files.some((f) => !f.doc && !f.error);
        }

        function canSend() {
            return !submitting && !uploading() && (ta.value.trim().length > 0 || c.files.some((f) => f.doc));
        }

        function paintSend() {
            const stop = !!streamingMsg;
            AIChat.clear(btnSend);
            btnSend.appendChild(icon(stop ? "stop" : "send"));
            btnSend.classList.toggle("is-stop", stop);
            btnSend.setAttribute("aria-label", stop ? __("Stop") : __("Send"));
            btnSend.title = stop ? __("Stop (Esc)") : __("Send (Enter)");
            btnSend.disabled = stop ? false : !canSend();
            hint.textContent = uploading() ? __("Uploading files…") : stop ? __("Esc to stop") : __("Enter to send · Shift+Enter for a new line");
        }

        /**
         * The first letter typed sets the direction (Arabic → RTL, Latin → LTR);
         * with no letter yet it follows the UI. dir="auto" can't do this: Chrome
         * makes an empty box LTR, which put the Arabic placeholder on the left.
         */
        function syncDir() {
            const dir = AIChat.textDir(ta.value) || (AIChat.isRtl() ? "rtl" : "ltr");
            if (ta.dir !== dir) ta.dir = dir;
        }

        // every value change (typing, paste, setText, clear, restore) ends here
        function autosize() {
            syncDir();
            ta.style.height = "auto";
            const max = Math.round(window.innerHeight * (compact ? 0.3 : 0.4));
            ta.style.height = Math.min(ta.scrollHeight, max) + "px";
            ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
        }

        /* ---- files ---- */

        function addFiles(list) {
            Array.from(list || []).forEach((file) => {
                if (!file || !file.name) return;
                const entry = { id: AIChat.uid(), name: file.name, size: file.size, progress: 0, doc: null, error: null };
                c.files.push(entry);
                paintChips();
                Promise.resolve(ctx.upload(file, (p) => {
                    entry.progress = p;
                    const fill = chips.querySelector(`[data-id="${entry.id}"] .ac-chip-progress-fill`);
                    if (fill) fill.style.width = Math.round(p * 100) + "%";
                }))
                    .then((doc) => {
                        entry.doc = doc;
                        entry.progress = 1;
                    })
                    .catch((e) => {
                        entry.error = AIChat.errorMessage(e);
                        AIChat.toast(entry.error, "red");
                    })
                    .finally(paintChips);
            });
        }

        function removeFile(f) {
            c.files = c.files.filter((x) => x !== f);
            paintChips();
        }

        fileInput.addEventListener("change", () => {
            addFiles(fileInput.files);
            fileInput.value = "";
        });
        ta.addEventListener("paste", (e) => {
            const files = e.clipboardData && e.clipboardData.files;
            if (files && files.length) {
                e.preventDefault();
                addFiles(files);
            }
        });
        let dragDepth = 0;
        el.addEventListener("dragenter", (e) => {
            if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
            e.preventDefault();
            dragDepth++;
            el.classList.add("is-dragover");
        });
        el.addEventListener("dragover", (e) => {
            if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault();
        });
        el.addEventListener("dragleave", () => {
            dragDepth = Math.max(0, dragDepth - 1);
            if (!dragDepth) el.classList.remove("is-dragover");
        });
        el.addEventListener("drop", (e) => {
            e.preventDefault();
            dragDepth = 0;
            el.classList.remove("is-dragover");
            if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
        });

        /* ---- pickers ---- */

        function toggleAgentPicker() {
            if (popover.isOpen() && popover.kind() === "agent") return popover.close();
            const list = (getBoot().agents || []).map((a) => ({
                label: a.agent_name || a.name,
                sub: [a.description, a.read_only ? __("Read only") : null].filter(Boolean).join(" · "),
                icon: "bot",
                selected: a.name === c.agent,
                value: a.name,
            }));
            popover.open(list, {
                kind: "agent",
                title: __("Agent"),
                onPick: (item) => {
                    c.agent = item.value;
                    skillCacheClear();
                    paintPickers();
                    focus();
                },
            });
        }

        function toggleModelPicker() {
            if (popover.isOpen() && popover.kind() === "model") return popover.close();
            const list = [];
            (getBoot().providers || []).forEach((p) => {
                (p.models || []).forEach((m) => {
                    list.push({
                        label: m.label || m.model_name,
                        sub: [p.label || p.name, m.supports_thinking ? __("Supports thinking") : null].filter(Boolean).join(" · "),
                        icon: "zap",
                        selected: p.name === c.provider && m.model_name === c.model,
                        value: { provider: p.name, model: m.model_name },
                    });
                });
            });
            popover.open(list, {
                kind: "model",
                title: __("Model"),
                onPick: (item) => {
                    c.provider = item.value.provider;
                    c.model = item.value.model;
                    paintPickers();
                    focus();
                },
            });
        }

        function cycleThinking() {
            const opts = (getBoot().settings && getBoot().settings.thinking_options) || ["off", "adaptive", "on"];
            const i = opts.indexOf(c.thinking);
            c.thinking = opts[(i + 1) % opts.length];
            paintPickers();
            AIChat.toast(btnThinking.lastChild.textContent, "blue");
        }

        function skillCacheClear() {
            skillCache.clear();
        }

        /** The / menu: skills of the current agent, then plugins, both filtered by the access rules server-side. */
        function loadSkills() {
            const agent = c.agent;
            if (skillCache.has(agent)) return Promise.resolve(skillCache.get(agent));
            return Promise.resolve(ctx.listSkills ? ctx.listSkills(agent) : { skills: [] })
                .then((r) => {
                    const menu = [
                        ...((r && r.skills) || []).map((s) => ({ kind: "skill", ...s })),
                        ...((r && r.plugins) || []).map((p) => ({ kind: "plugin", ...p })),
                    ];
                    skillCache.set(agent, menu);
                    return menu;
                })
                .catch(() => []);
        }

        /* ---- / and @ triggers ---- */

        function tokenAtCaret() {
            const pos = ta.selectionStart;
            const before = ta.value.slice(0, pos);
            const slash = palette && before.match(/^\/([^\s]*)$/);
            if (slash) return { kind: "skill", start: 0, end: pos, query: slash[1] };
            const at = ctx.searchDocuments && before.match(/(?:^|\s)@([^\s@]*)$/);
            if (at) return { kind: "doc", start: pos - at[1].length - 1, end: pos, query: at[1] };
            return null;
        }

        const searchDocs = debounce((token) => {
            if (!ctx.searchDocuments) return;
            const q = token.query;
            Promise.resolve(ctx.searchDocuments(q))
                .then((r) => {
                    const cur = tokenAtCaret();
                    if (!cur || cur.kind !== "doc" || cur.query !== q) return;
                    const list = ((r && r.results) || []).map((d) => ({ label: d.title || d.name, sub: d.doctype + " · " + d.name, icon: "file", value: d }));
                    popover.open(list, {
                        kind: "doc",
                        title: __("Insert a document"),
                        empty: q ? __("No results for {0}", [q]) : __("Type to search"),
                        onPick: (item) => {
                            const d = item.value;
                            replaceRange(cur.start, cur.end, "@" + (d.title || d.name) + " ");
                            c.context = { doctype: d.doctype, name: d.name, title: d.title || d.name };
                            paintChips();
                        },
                    });
                })
                .catch(() => popover.close());
        }, 250);

        function replaceRange(start, end, text) {
            const v = ta.value;
            ta.value = v.slice(0, start) + text + v.slice(end);
            const pos = start + text.length;
            ta.setSelectionRange(pos, pos);
            c.text = ta.value;
            autosize();
            paintSend();
        }

        function onInput() {
            c.text = ta.value;
            autosize();
            paintSend();
            atQuery = tokenAtCaret();
            if (!atQuery) {
                if (popover.isOpen() && (popover.kind() === "skill" || popover.kind() === "doc")) popover.close();
                return;
            }
            if (atQuery.kind === "skill") {
                const token = atQuery;
                loadSkills().then((menu) => {
                    const cur = tokenAtCaret();
                    if (!cur || cur.kind !== "skill") return;
                    const q = cur.query.toLowerCase();
                    const list = menu
                        .filter((s) => !q || (s.title || "").toLowerCase().includes(q) || (s.name || "").toLowerCase().includes(q))
                        .map((s) => ({
                            label: s.title || s.name,
                            sub: s.kind === "plugin" ? s.description || "" : "", // a skill's description is written for the model (English)
                            icon: s.kind === "plugin" ? pluginIcon(s) : "sparkles",
                            section: s.kind === "plugin" ? __("Plugins") : __("Skills"),
                            selected: (s.kind === "plugin" ? c.plugin : c.skill) && (s.kind === "plugin" ? c.plugin : c.skill).name === s.name,
                            value: s,
                        }));
                    popover.open(list, {
                        kind: "skill",
                        empty: menu.length ? __("No matching results") : __("No skills or plugins are allowed for you yet"),
                        onPick: (item) => {
                            if (item.value.kind === "plugin") c.plugin = item.value;
                            else c.skill = item.value;
                            replaceRange(cur.start, cur.end, "");
                            paintChips();
                        },
                    });
                });
                void token;
            } else if (atQuery.kind === "doc") {
                searchDocs(atQuery);
            }
        }

        /* ---- keys ---- */

        ta.addEventListener("input", onInput);
        ta.addEventListener("click", () => {
            if (!tokenAtCaret() && popover.isOpen()) popover.close();
        });
        ta.addEventListener("keydown", (e) => {
            if (popover.isOpen()) {
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    return popover.move(1);
                }
                if (e.key === "ArrowUp") {
                    e.preventDefault();
                    return popover.move(-1);
                }
                if (e.key === "Enter" || e.key === "Tab") {
                    e.preventDefault();
                    return popover.pick();
                }
                if (e.key === "Escape") {
                    e.preventDefault();
                    return popover.close();
                }
            }
            if (e.key === "Escape") {
                if (streamingMsg && ctx.onStop) {
                    e.preventDefault();
                    ctx.onStop(streamingMsg);
                }
                return;
            }
            if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) {
                if (e.isComposing || e.keyCode === 229) return;
                e.preventDefault();
                submit().catch(() => {});
                return;
            }
            if (e.key === "ArrowUp" && !ta.value && ctx.onEditLast) {
                e.preventDefault();
                ctx.onEditLast();
            }
        });
        ta.addEventListener("blur", () => setTimeout(() => {
            if (!el.contains(document.activeElement)) popover.close();
        }, 120));

        btnSend.addEventListener("click", () => {
            if (streamingMsg) {
                if (ctx.onStop) ctx.onStop(streamingMsg);
            } else submit().catch(() => {});
        });
        errorRetry.addEventListener("click", () => submit().catch(() => {}));

        document.addEventListener("click", (e) => {
            if (popover.isOpen() && !el.contains(e.target)) popover.close();
        });

        /* ---- submit ---- */

        function submit() {
            if (!canSend()) return Promise.resolve();
            const text = ta.value.trim();
            const payload = {
                text,
                files: c.files.filter((f) => f.doc).map((f) => f.doc.name),
                skill: c.skill ? c.skill.name : null,
                plugin: c.plugin ? c.plugin.name : null,
                agent: c.agent,
                company: c.company,
                provider: c.provider,
                model: c.model,
                thinking: !pickers || btnThinking.hidden ? null : c.thinking,
                context_doctype: c.context ? c.context.doctype : null,
                context_name: c.context ? c.context.name : null,
            };
            submitting = true;
            paintSend();
            const snapshot = { text: ta.value, files: c.files, skill: c.skill, plugin: c.plugin, context: c.context };
            ta.value = "";
            c.text = "";
            c.files = [];
            c.skill = null;
            c.plugin = null;
            if (!(c.context && c.context.sticky)) c.context = null; // a sticky context (open document) stays attached
            errorLine.hidden = true;
            popover.close();
            paintChips();
            autosize();
            return Promise.resolve(onSend(payload))
                .catch((e) => {
                    // restore the draft so nothing is lost
                    ta.value = snapshot.text;
                    c.text = snapshot.text;
                    c.files = snapshot.files;
                    c.skill = snapshot.skill;
                    c.plugin = snapshot.plugin;
                    c.context = snapshot.context;
                    paintChips();
                    autosize();
                    throw e;
                })
                .finally(() => {
                    submitting = false;
                    paintSend();
                    // on touch devices refocusing kept the keyboard over the reply
                    if (AIChat.isTouch()) ta.blur();
                    else focus();
                });
        }

        /* ---- public ---- */

        function focus() {
            // a phone opens its keyboard on every focus: only the user's own tap may do that
            if (AIChat.isTouch() && document.activeElement !== ta) return;
            try {
                ta.focus({ preventScroll: true });
            } catch (e) {
                ta.focus();
            }
        }

        function setText(text, opts) {
            ta.value = text || "";
            c.text = ta.value;
            autosize();
            paintSend();
            if (!opts || !opts.keepCaret) ta.setSelectionRange(ta.value.length, ta.value.length);
        }

        function setStreaming(msg) {
            streamingMsg = msg || null;
            paintSend();
        }

        function setSession(s) {
            session = s || null;
            if (session) {
                if (session.agent) c.agent = session.agent;
                if (session.provider) c.provider = session.provider;
                if (session.model) c.model = session.model;
                if (session.company !== undefined) c.company = session.company;
            }
            defaults();
            paintPickers();
        }

        function setAgent(name) {
            if (agentOf(name)) {
                c.agent = name;
                skillCacheClear();
            }
            paintPickers();
        }

        function setCompany(company) {
            c.company = company || null;
        }

        /** {doctype, name, label?, sticky?} or null; `sticky` survives a send (the open document). */
        function setContext(context) {
            c.context = context || null;
            paintChips();
        }

        /** Inline error under the box (send failures); cleared by the next submit. */
        function showError(text) {
            errorText.textContent = text || "";
            errorLine.hidden = !text;
        }

        defaults();
        paintPickers();
        paintChips();
        paintSend();
        requestAnimationFrame(autosize);

        return { el, ta, focus, getText: () => ta.value, setText, setAgent, setCompany, setStreaming, setSession, setContext, showError, submit, closePopovers: popover.close, addFiles };
    }

    /** A private draft state for a second composer (the bubble). */
    function newState() {
        return { text: "", agent: null, company: null, provider: null, model: null, thinking: "adaptive", skill: null, plugin: null, context: null, files: [] };
    }

    AIChat.composer = { create, createContextMeter, createPopover, newState };
})();
