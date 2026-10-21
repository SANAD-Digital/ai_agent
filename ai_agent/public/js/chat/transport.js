/**
 * transport.js — everything that talks to the server.
 *
 * Responsibility: `AIChat.api.<endpoint>(args)` wrappers over
 * `frappe.xcall("ai_agent.chat.api.<name>")` for every endpoint in the client
 * contract, private file upload with progress, the `ai_agent:chat` realtime
 * subscription (feeding `store.applyEvent`), gap resync via `get_message`,
 * and re-fetching streaming messages when the socket reconnects or the tab
 * becomes visible again.
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const store = AIChat.store;

    const ENDPOINTS = [
        "bootstrap",
        "list_sessions",
        "create_session",
        "get_session",
        "get_messages",
        "get_message",
        "send_message",
        "stop_message",
        "retry_message",
        "edit_message",
        "decide_approval",
        "set_approval_mode",
        "rename_session",
        "export_session",
        "archive_session",
        "pin_session",
        "delete_session",
        "list_skills",
        "search_documents",
        "feedback",
        "get_artifact",
        "list_memories",
        "delete_memory",
        "remember_now",
    ];

    const api = {};
    ENDPOINTS.forEach((name) => {
        api[name] = (args) => frappe.xcall("ai_agent.chat.api." + name, args || {});
    });

    /* ------------------------------------------------------------------ */
    /* upload                                                              */
    /* ------------------------------------------------------------------ */

    /**
     * Upload one private file (no attachment) and resolve with the File doc.
     * `onProgress(fraction)` is called with 0..1.
     */
    function upload(file, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.upload.addEventListener("progress", (e) => {
                if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
            });
            xhr.addEventListener("error", () => reject(new Error(AIChat.__("The file could not be uploaded"))));
            xhr.addEventListener("abort", () => reject(new Error(AIChat.__("Upload cancelled"))));
            xhr.onreadystatechange = () => {
                if (xhr.readyState !== XMLHttpRequest.DONE) return;
                let body = null;
                try {
                    body = JSON.parse(xhr.responseText);
                } catch (e) {
                    body = null;
                }
                if (xhr.status === 200 && body && body.message) {
                    if (onProgress) onProgress(1);
                    resolve(body.message);
                } else {
                    let msg = AIChat.__("The file could not be uploaded");
                    if (xhr.status === 413) msg = AIChat.__("The file is larger than allowed");
                    else if (body) msg = AIChat.errorMessage(body);
                    reject(new Error(msg));
                }
            };
            xhr.open("POST", "/api/method/upload_file", true);
            xhr.setRequestHeader("Accept", "application/json");
            xhr.setRequestHeader("X-Frappe-CSRF-Token", frappe.csrf_token);
            const form = new FormData();
            form.append("file", file, file.name);
            form.append("is_private", 1);
            form.append("folder", "Home/Attachments");
            xhr.send(form);
        });
    }

    /* ------------------------------------------------------------------ */
    /* realtime                                                            */
    /* ------------------------------------------------------------------ */

    const inflight = new Set();
    let subscribed = false;
    let everConnected = false;

    function resync(name) {
        if (inflight.has(name)) return Promise.resolve();
        inflight.add(name);
        return api
            .get_message({ message: name })
            .then((r) => {
                const fetched = r && r.message ? r.message : r;
                if (store.getMessage(name) && store.getMessage(name)._resyncing) {
                    store.finishResync(name, fetched);
                } else if (fetched && fetched.name) {
                    store.upsertMessage(fetched, { replace: true });
                }
            })
            .catch((e) => {
                console.warn("[AIChat] resync failed for " + name, e);
                store.finishResync(name, null);
            })
            .finally(() => inflight.delete(name));
    }

    /** Fetch a message we do not know yet (created elsewhere, e.g. another tab). */
    function adopt(payload) {
        const name = payload.message;
        if (!name || inflight.has(name)) return;
        inflight.add(name);
        api.get_message({ message: name })
            .then((r) => {
                const fetched = r && r.message ? r.message : r;
                if (fetched && fetched.name && store.state.current && store.state.current.name === fetched.session) {
                    store.upsertMessage(fetched, { replace: true });
                    // the user message that preceded it may be unknown too
                    if (fetched.seq > 1) {
                        const known = store.state.order.some((n) => {
                            const m = store.getMessage(n);
                            return m && m.seq === fetched.seq - 1;
                        });
                        if (!known) refreshTail();
                    }
                }
            })
            .catch(() => {})
            .finally(() => inflight.delete(name));
    }

    let refreshing = false;
    function refreshTail() {
        if (refreshing || !store.state.current) return;
        refreshing = true;
        const session = store.state.current.name;
        api.get_messages({ session, limit: 40 })
            .then((r) => {
                if (!store.state.current || store.state.current.name !== session) return;
                (r.messages || []).forEach((m) => {
                    const existing = store.getMessage(m.name);
                    if (!existing) store.upsertMessage(m);
                });
            })
            .catch(() => {})
            .finally(() => (refreshing = false));
    }

    /* ------------------------------------------------------------------ */
    /* fallback polling                                                    */
    /*                                                                     */
    /* Realtime is the primary channel. While a message is streaming and   */
    /* the socket is not connected (or has gone quiet), the message is     */
    /* re-fetched every 1.5 s: the server merges the live delta tail into  */
    /* the streaming blocks, so the text still grows without a socket.     */
    /* ------------------------------------------------------------------ */

    const POLL_MS = 1500;
    const QUIET_MS = 4000;
    let lastEventAt = 0;
    let pollTimer = null;

    function socketConnected() {
        const s = window.frappe && frappe.socketio && frappe.socketio.socket;
        return !!(s && s.connected);
    }

    function pollTick() {
        if (!store.state.streaming.size) {
            clearInterval(pollTimer);
            pollTimer = null;
            return;
        }
        const quiet = Date.now() - lastEventAt > QUIET_MS;
        if (!socketConnected() || quiet) {
            for (const name of Array.from(store.state.streaming)) resync(name);
        }
    }

    function ensurePolling() {
        if (pollTimer || !store.state.streaming.size) return;
        pollTimer = setInterval(pollTick, POLL_MS);
    }

    function onRealtime(payload) {
        if (!payload || typeof payload !== "object") return;
        lastEventAt = Date.now();
        const result = store.applyEvent(payload);
        if (result === "gap") {
            resync(payload.message);
        } else if (result === "unknown") {
            adopt(payload);
        }
    }

    function refetchStreaming() {
        for (const name of Array.from(store.state.streaming)) {
            const msg = store.getMessage(name);
            if (msg) {
                msg._resyncing = true;
                msg._buffer = [];
                resync(name);
            }
        }
    }

    function subscribe() {
        if (subscribed) return;
        subscribed = true;
        if (!window.frappe || !frappe.realtime) return;
        frappe.realtime.on("ai_agent:chat", onRealtime);
        store.on("streaming", () => {
            if (store.state.streaming.size) {
                lastEventAt = lastEventAt || Date.now();
                ensurePolling();
            }
        });

        const socket = frappe.socketio && frappe.socketio.socket;
        if (socket && typeof socket.on === "function") {
            socket.on("connect", () => {
                if (everConnected) refetchStreaming();
                everConnected = true;
            });
        }
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible" && store.state.streaming.size) refetchStreaming();
        });
    }

    AIChat.api = api;
    AIChat.transport = { upload, subscribe, resync, refetchStreaming, refreshTail, onRealtime, socketConnected };
})();
