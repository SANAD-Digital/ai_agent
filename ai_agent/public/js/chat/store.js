/**
 * store.js — in-memory state for the chat client plus a tiny pub/sub.
 *
 * Responsibility: hold `boot`, the session list, the current session, the
 * messages of the current session (Map name → Message, plus `order`), the set
 * of streaming message names, composer defaults and UI flags. Every change goes
 * through a mutation function here so renderers only subscribe to topics:
 *   "session"          the current session changed (or its fields did)
 *   "sessions"         the session list changed
 *   "messages"         message order changed (added / removed / replaced)
 *   "message:<name>"   one message changed (delta, status, blocks…)
 *   "streaming"        the streaming set changed
 *   "theme"            light/dark switched
 * `applyEvent(payload)` implements the realtime contract, including the gap
 * rule (returns "gap" so transport can resync via get_message).
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;

    const state = {
        boot: null,
        sessions: [],
        current: null,
        messages: new Map(),
        order: [],
        streaming: new Set(),
        hasMore: false,
        composer: {
            text: "",
            agent: null,
            company: null,
            provider: null,
            model: null,
            thinking: "adaptive",
            skill: null,
            context: null,
            files: [],
        },
        ui: {
            sidebarOpen: true,
            theme: "light",
            loading: false,
        },
    };

    /* ------------------------------------------------------------------ */
    /* pub/sub                                                             */
    /* ------------------------------------------------------------------ */

    const subs = new Map();

    function on(topic, fn) {
        if (!subs.has(topic)) subs.set(topic, new Set());
        subs.get(topic).add(fn);
        return () => off(topic, fn);
    }

    function off(topic, fn) {
        const set = subs.get(topic);
        if (set) set.delete(fn);
    }

    function emit(topic, data) {
        const set = subs.get(topic);
        if (!set) return;
        for (const fn of Array.from(set)) {
            try {
                fn(data);
            } catch (e) {
                console.error("[AIChat] subscriber error on " + topic, e);
            }
        }
    }

    /* ------------------------------------------------------------------ */
    /* sessions                                                            */
    /* ------------------------------------------------------------------ */

    function setBoot(boot) {
        state.boot = boot;
        setSessions((boot && boot.sessions) || []);
    }

    function setSessions(list, hasMore) {
        state.sessions = (list || []).map((s) => Object.assign({}, s));
        if (hasMore !== undefined) state.sessionsHasMore = !!hasMore;
        emit("sessions", state.sessions);
    }

    function upsertSession(session) {
        if (!session || !session.name) return null;
        const idx = state.sessions.findIndex((s) => s.name === session.name);
        let merged;
        if (idx >= 0) {
            merged = Object.assign(state.sessions[idx], session);
        } else {
            merged = Object.assign({}, session);
            state.sessions.unshift(merged);
        }
        if (state.current && state.current.name === session.name) {
            Object.assign(state.current, session);
            emit("session", state.current);
        }
        emit("sessions", state.sessions);
        return merged;
    }

    function removeSession(name) {
        state.sessions = state.sessions.filter((s) => s.name !== name);
        emit("sessions", state.sessions);
        if (state.current && state.current.name === name) setCurrent(null);
    }

    function getSession(name) {
        return state.sessions.find((s) => s.name === name) || null;
    }

    /** Switch the current session; clears the messages when the name changes. */
    function setCurrent(session) {
        const prevName = state.current ? state.current.name : null;
        const nextName = session ? session.name : null;
        if (nextName && getSession(nextName)) {
            state.current = Object.assign(getSession(nextName), session);
        } else {
            state.current = session ? Object.assign({}, session) : null;
        }
        if (prevName !== nextName) {
            state.messages = new Map();
            state.order = [];
            state.streaming = new Set();
            state.hasMore = false;
            emit("messages", state.order);
            emit("streaming", state.streaming);
        }
        emit("session", state.current);
    }

    /* ------------------------------------------------------------------ */
    /* messages                                                            */
    /* ------------------------------------------------------------------ */

    const STREAMING_STATUSES = new Set(["pending", "streaming"]);

    function normalizeMessage(raw) {
        const msg = Object.assign({}, raw);
        msg.blocks = Array.isArray(msg.blocks) ? msg.blocks.map((b) => Object.assign({}, b)) : [];
        msg.blocks.sort((a, b) => (a.seq || 0) - (b.seq || 0));
        msg.usage = msg.usage || {};
        msg.attachments = msg.attachments || [];
        if (msg.event_seq === undefined) {
            msg.event_seq = raw.last_event_seq !== undefined ? raw.last_event_seq : null;
        }
        if (msg.role === "assistant") msg.text = joinText(msg);
        return msg;
    }

    function joinText(msg) {
        return msg.blocks
            .filter((b) => b.kind === "text" && b.text)
            .map((b) => b.text)
            .join("\n\n");
    }

    function syncStreaming(msg) {
        const was = state.streaming.has(msg.name);
        const now = msg.role === "assistant" && STREAMING_STATUSES.has(msg.status);
        if (now && !msg._started_local) msg._started_local = Date.now();
        if (was === now) return;
        if (now) state.streaming.add(msg.name);
        else state.streaming.delete(msg.name);
        emit("streaming", state.streaming);
    }

    function sortOrder() {
        state.order.sort((a, b) => {
            const ma = state.messages.get(a);
            const mb = state.messages.get(b);
            return (ma ? ma.seq : 0) - (mb ? mb.seq : 0);
        });
    }

    /** Insert or merge a message; emits "messages" when the order changes. */
    function upsertMessage(raw, opts) {
        if (!raw || !raw.name) return null;
        opts = opts || {};
        const existing = state.messages.get(raw.name);
        let msg;
        if (existing && !opts.replace) {
            const keep = { _started_local: existing._started_local, _resyncing: existing._resyncing, _buffer: existing._buffer };
            msg = Object.assign(existing, normalizeMessage(Object.assign({}, existing, raw)), keep);
        } else {
            msg = normalizeMessage(raw);
            if (existing) {
                msg._started_local = existing._started_local;
            }
            state.messages.set(msg.name, msg);
        }
        const isNew = !state.order.includes(msg.name);
        if (isNew) {
            state.order.push(msg.name);
            sortOrder();
        }
        syncStreaming(msg);
        if (isNew || opts.replace) emit("messages", state.order);
        emit("message:" + msg.name, msg);
        return msg;
    }

    /** Replace the whole list (initial load / session switch). */
    function setMessages(list, hasMore) {
        state.messages = new Map();
        state.order = [];
        state.streaming = new Set();
        (list || []).forEach((raw) => {
            const msg = normalizeMessage(raw);
            state.messages.set(msg.name, msg);
            state.order.push(msg.name);
            if (msg.role === "assistant" && STREAMING_STATUSES.has(msg.status)) {
                msg._started_local = msg._started_local || Date.now();
                state.streaming.add(msg.name);
            }
        });
        sortOrder();
        state.hasMore = !!hasMore;
        emit("messages", state.order);
        emit("streaming", state.streaming);
    }

    /** Prepend older messages (pagination). */
    function prependMessages(list, hasMore) {
        (list || []).forEach((raw) => {
            if (state.messages.has(raw.name)) return;
            const msg = normalizeMessage(raw);
            state.messages.set(msg.name, msg);
            state.order.push(msg.name);
        });
        sortOrder();
        state.hasMore = !!hasMore;
        emit("messages", state.order);
    }

    function removeMessage(name) {
        if (!state.messages.has(name)) return;
        state.messages.delete(name);
        state.order = state.order.filter((n) => n !== name);
        if (state.streaming.delete(name)) emit("streaming", state.streaming);
        emit("messages", state.order);
    }

    /** Drop every message with seq >= `seq` (after edit_message). */
    function removeMessagesFrom(seq) {
        const doomed = state.order.filter((n) => {
            const m = state.messages.get(n);
            return m && m.seq >= seq;
        });
        doomed.forEach((n) => {
            state.messages.delete(n);
            state.streaming.delete(n);
        });
        state.order = state.order.filter((n) => !doomed.includes(n));
        emit("streaming", state.streaming);
        emit("messages", state.order);
    }

    function getMessage(name) {
        return state.messages.get(name) || null;
    }

    function lastMessage(role) {
        for (let i = state.order.length - 1; i >= 0; i--) {
            const m = state.messages.get(state.order[i]);
            if (m && (!role || m.role === role)) return m;
        }
        return null;
    }

    function streamingMessage() {
        for (const name of state.streaming) {
            const m = state.messages.get(name);
            if (m) return m;
        }
        return null;
    }

    function awaitingMessage() {
        for (let i = state.order.length - 1; i >= 0; i--) {
            const m = state.messages.get(state.order[i]);
            if (m && m.status === "awaiting_approval") return m;
        }
        return null;
    }

    /* ------------------------------------------------------------------ */
    /* blocks                                                              */
    /* ------------------------------------------------------------------ */

    function findBlock(msg, seq) {
        return msg.blocks.find((b) => b.seq === seq) || null;
    }

    function upsertBlock(msg, seq, patch) {
        let block = findBlock(msg, seq);
        if (block) {
            Object.assign(block, patch);
        } else {
            block = Object.assign({ seq, status: "completed", text: "", data: null }, patch, { seq });
            msg.blocks.push(block);
            msg.blocks.sort((a, b) => (a.seq || 0) - (b.seq || 0));
        }
        return block;
    }

    function nowTs() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    /* ------------------------------------------------------------------ */
    /* realtime                                                            */
    /* ------------------------------------------------------------------ */

    /**
     * Apply one realtime payload. Returns one of:
     *   "applied" | "ignored" | "unknown" (message not loaded) |
     *   "gap" (transport must call get_message → finishResync) | "buffered"
     */
    function applyEvent(payload) {
        if (!payload || !payload.event) return "ignored";
        const event = payload.event;

        if (event.type === "session.updated") {
            const es = event.session;
            if (es) {
                // The sidebar lists workspace conversations only: a support-bubble
                // (or other surface) session updates the current header but is not added.
                const known = state.sessions.some((x) => x.name === es.name);
                if (known || !es.surface || es.surface === "Desk") upsertSession(es);
                else if (state.current && state.current.name === es.name) {
                    Object.assign(state.current, es);
                    emit("session", state.current);
                }
            }
            return "applied";
        }
        if (!state.current || state.current.name !== payload.session) {
            return "ignored";
        }
        const msg = state.messages.get(payload.message);
        if (!msg) return "unknown";

        const seq = Number(payload.seq);
        if (msg._resyncing) {
            msg._buffer.push(payload);
            return "buffered";
        }
        if (Number.isFinite(seq) && msg.event_seq !== null && msg.event_seq !== undefined) {
            if (seq <= msg.event_seq) return "ignored"; // duplicate / stale
            if (seq !== msg.event_seq + 1) {
                msg._resyncing = true;
                msg._buffer = [payload];
                return "gap";
            }
        }
        if (Number.isFinite(seq)) msg.event_seq = seq;
        applyEventBody(msg, event);
        emit("message:" + msg.name, msg);
        return "applied";
    }

    /** Called by transport with the freshly fetched message after a gap. */
    function finishResync(name, fetched) {
        const old = state.messages.get(name);
        const buffer = (old && old._buffer) || [];
        if (!fetched) {
            if (old) {
                old._resyncing = false;
                old._buffer = [];
                old.event_seq = null; // accept whatever comes next
            }
            return;
        }
        const msg = normalizeMessage(fetched);
        if (old) msg._started_local = old._started_local;
        state.messages.set(name, msg);
        if (!state.order.includes(name)) {
            state.order.push(name);
            sortOrder();
            emit("messages", state.order);
        }
        buffer.sort((a, b) => Number(a.seq) - Number(b.seq));
        for (const p of buffer) {
            const seq = Number(p.seq);
            if (msg.event_seq !== null && msg.event_seq !== undefined && seq <= msg.event_seq) continue;
            msg.event_seq = seq;
            applyEventBody(msg, p.event);
        }
        syncStreaming(msg);
        emit("message:" + name, msg);
    }

    function applyEventBody(msg, ev) {
        switch (ev.type) {
            case "message.started":
                msg.status = "streaming";
                if (ev.model) msg.model = ev.model;
                if (ev.provider) msg.provider = ev.provider;
                msg.error = null;
                if (!ev.resumed) msg._started_local = msg._started_local || Date.now();
                break;

            case "block.start": {
                const kind = ev.kind || ev.block_type || "text";
                const block = upsertBlock(msg, ev.block_seq, {
                    kind,
                    status: "streaming",
                    started_at: nowTs(),
                });
                if (block.text === undefined || block.text === null) block.text = "";
                if (ev.id) block.id = ev.id;
                if (ev.name || ev.tool_name) block.name = ev.name || ev.tool_name;
                if (msg.status === "pending") msg.status = "streaming";
                break;
            }

            case "block.delta": {
                const block = upsertBlock(msg, ev.block_seq, {});
                if (block.kind === "tool_use") {
                    block._json = (block._json || "") + (ev.delta || "");
                } else {
                    if (!block.kind) block.kind = "text";
                    block.text = (block.text || "") + (ev.delta || "");
                    if (block.status !== "streaming") block.status = "streaming";
                }
                if (block.kind === "text") msg.text = joinText(msg);
                break;
            }

            case "block.done": {
                const block = upsertBlock(msg, ev.block_seq, ev.block || {});
                if (!block.status || block.status === "streaming") block.status = "completed";
                if (!block.completed_at) block.completed_at = nowTs();
                delete block._json;
                if (block.kind === "text") msg.text = joinText(msg);
                break;
            }

            case "tool.result": {
                const block = upsertBlock(msg, ev.block_seq, Object.assign({ kind: "tool_result" }, ev.block || {}));
                if (ev.duration_ms !== undefined) block.duration_ms = ev.duration_ms;
                if (!block.status || block.status === "streaming") block.status = block.is_error ? "failed" : "completed";
                if (block.id) {
                    const use = msg.blocks.find((b) => b.kind === "tool_use" && b.id === block.id);
                    if (use) {
                        if (use.status === "streaming") use.status = block.is_error ? "failed" : "completed";
                        if (ev.duration_ms !== undefined && !use.duration_ms) use.duration_ms = ev.duration_ms;
                    }
                }
                break;
            }

            case "skill.loaded":
            case "memory.note":
            case "artifact.created": {
                const block = upsertBlock(msg, ev.block_seq, ev.block || {});
                if (!block.status || block.status === "streaming") block.status = "completed";
                break;
            }

            case "approval.requested": {
                const block = upsertBlock(msg, ev.block_seq, Object.assign({ kind: "approval" }, ev.block || {}));
                if (ev.approval && (!block.data || !block.data.approval)) {
                    block.data = Object.assign({}, block.data || {}, { approval: ev.approval });
                }
                msg.status = "awaiting_approval";
                break;
            }

            case "approval.decided": {
                const block = upsertBlock(msg, ev.block_seq, ev.block || {});
                if (ev.block && ev.block.data) block.data = ev.block.data;
                if (msg.status === "awaiting_approval") msg.status = "streaming";
                break;
            }

            case "usage":
                if (ev.usage) msg.usage = Object.assign({}, msg.usage || {}, ev.usage);
                if (ev.context_tokens !== undefined) {
                    msg.context_tokens = ev.context_tokens;
                    if (state.current) {
                        state.current.context_tokens = ev.context_tokens;
                        emit("session", state.current);
                    }
                }
                if (ev.model) msg.model = ev.model;
                break;

            case "compaction": {
                const maxSeq = msg.blocks.reduce((m, b) => Math.max(m, b.seq || 0), 0);
                upsertBlock(msg, maxSeq + 0.5, {
                    kind: "status",
                    status: "completed",
                    _local: true,
                    text: AIChat.__("تم ضغط السياق: {0} ← {1} رمز، حُذفت {2} رسالة", [
                        AIChat.fmtTokens(ev.before_tokens),
                        AIChat.fmtTokens(ev.after_tokens),
                        ev.dropped_messages || 0,
                    ]),
                });
                break;
            }

            case "message.done":
                msg.status = ev.status || "completed";
                if (ev.stop_reason) msg.stop_reason = ev.stop_reason;
                if (ev.usage) msg.usage = Object.assign({}, msg.usage || {}, ev.usage);
                if (ev.steps !== undefined) msg.steps = ev.steps;
                if (ev.tool_calls !== undefined) msg.tool_calls = ev.tool_calls;
                msg.completed_at = msg.completed_at || nowTs();
                if (!msg.duration_ms && msg._started_local) msg.duration_ms = Date.now() - msg._started_local;
                msg.blocks.forEach((b) => {
                    if (b.status === "streaming") b.status = "completed";
                });
                msg.text = joinText(msg);
                break;

            case "message.failed":
                msg.status = "failed";
                msg.error = ev.error || AIChat.__("فشل توليد الرد");
                msg.error_kind = ev.kind || null;
                msg.completed_at = msg.completed_at || nowTs();
                if (!msg.duration_ms && msg._started_local) msg.duration_ms = Date.now() - msg._started_local;
                msg.blocks.forEach((b) => {
                    if (b.status === "streaming") b.status = "failed";
                });
                break;

            case "message.cancelled":
                msg.status = "cancelled";
                if (ev.usage) msg.usage = Object.assign({}, msg.usage || {}, ev.usage);
                msg.completed_at = msg.completed_at || nowTs();
                if (!msg.duration_ms && msg._started_local) msg.duration_ms = Date.now() - msg._started_local;
                msg.blocks.forEach((b) => {
                    if (b.status === "streaming") b.status = "completed";
                });
                break;

            default:
                break;
        }
        syncStreaming(msg);
    }

    /**
     * The user decided an approval: the server resumes the same message in a
     * new job. Mark it live through syncStreaming so the fallback poller (and
     * the working indicator) follow it even when no realtime event arrives.
     */
    function markResumed(name) {
        const msg = state.messages.get(name);
        if (!msg) return;
        if (msg.status === "awaiting_approval") msg.status = "streaming";
        syncStreaming(msg);
        emit("message:" + name, msg);
    }

    /* ------------------------------------------------------------------ */
    /* ui                                                                  */
    /* ------------------------------------------------------------------ */

    function setTheme(theme) {
        const t = theme === "dark" ? "dark" : "light";
        if (state.ui.theme === t) return;
        state.ui.theme = t;
        emit("theme", t);
    }

    function setSidebarOpen(open) {
        state.ui.sidebarOpen = !!open;
        emit("sidebar", state.ui.sidebarOpen);
    }

    AIChat.store = {
        state,
        on,
        off,
        emit,
        setBoot,
        setSessions,
        upsertSession,
        removeSession,
        getSession,
        setCurrent,
        upsertMessage,
        setMessages,
        prependMessages,
        removeMessage,
        removeMessagesFrom,
        getMessage,
        lastMessage,
        streamingMessage,
        awaitingMessage,
        applyEvent,
        finishResync,
        markResumed,
        setTheme,
        setSidebarOpen,
    };
})();
