(function () {
    "use strict";

    var STORAGE_KEY = "ai_chatbot_conversation";
    var PREFS_KEY = "ai_chatbot_prefs";

    var state = {
        config: null,
        conversation: null,
        busy: false,
        loadedHistory: false,
        lang: "auto",
        pollTimer: null,
        activeStreamId: null
    };

    function boot() {
        ensureMarked();
        var attempts = 0;
        function tryInit() {
            if (window.frappe && frappe.call) {
                init();
                return;
            }
            attempts += 1;
            if (attempts > 50) {
                return;
            }
            setTimeout(tryInit, 200);
        }
        tryInit();
    }

    function init() {
        if (!window.frappe || !frappe.call || document.getElementById("ai-chatbot-fab")) {
            return;
        }
        if (!frappe.boot || !frappe.boot.user || !frappe.boot.user.name) {
            return;
        }
        state.conversation = getStoredConversation();
        state.lang = getStoredLang();

        frappe.call({
            method: "ai_agent.chatbot.api.get_config",
            args: {},
            callback: function (response) {
                var config = response && response.message ? response.message : null;
                if (!config || !config.enabled || !config.can_access) {
                    return;
                }
                state.config = config;
                if (config.conversation) {
                    state.conversation = config.conversation;
                }
                buildWidget(config);
            },
            error: function () {}
        });
    }

    function isRtl() {
        return (document.documentElement.getAttribute("dir") || "rtl").toLowerCase() === "rtl";
    }

    function detectLanguage() {
        var html = document.documentElement.getAttribute("lang") || "";
        if (html && html.toLowerCase().indexOf("en") === 0) {
            return "en";
        }
        return "ar";
    }

    function t(en, ar) {
        var lang = state.lang === "auto" ? detectLanguage() : state.lang;
        return lang === "en" ? en : ar;
    }

    function buildWidget(config) {
        var fab = document.createElement("button");
        fab.id = "ai-chatbot-fab";
        fab.className = "ai-chatbot-fab";
        fab.title = config.title || t("Support", "الدعم الفني");
        fab.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
        document.body.appendChild(fab);

        var panel = document.createElement("div");
        panel.id = "ai-chatbot-panel";
        panel.className = "ai-chatbot-panel";
        panel.innerHTML = buildPanelHTML(config);
        document.body.appendChild(panel);

        wirePanel(panel, fab, config);
    }

    function buildPanelHTML(config) {
        var title = config.title || "الدعم الفني";
        var welcome = config.welcome_message || "أهلًا! كيف يمكنني مساعدتك؟";
        return '<div class="ai-chatbot-header">' +
            '<div class="ai-chatbot-header-side-left">' +
            '<div class="ai-chatbot-avatar"></div>' +
            '<div class="ai-chatbot-header-text">' +
            '<div class="ai-chatbot-title"></div>' +
            '<div class="ai-chatbot-status"><span class="ai-chatbot-dot"></span><span class="ai-chatbot-status-text"></span></div>' +
            '</div></div>' +
            '<div class="ai-chatbot-header-side-right">' +
            '<button type="button" class="ai-chatbot-lang" title="Switch language">&#127760;</button>' +
            '<button type="button" class="ai-chatbot-close" title="' + t("Close", "إغلاق") + '">&times;</button>' +
            '</div></div>' +
            '<div class="ai-chatbot-body">' +
            '<div class="ai-chatbot-welcome"></div>' +
            '<div class="ai-chatbot-messages"></div>' +
            '</div>' +
            '<div class="ai-chatbot-composer">' +
            '<textarea class="ai-chatbot-input" rows="1" placeholder="' + t("Type a message…", "اكتب رسالتك هنا…") + '" dir="auto"></textarea>' +
            '<button type="button" class="ai-chatbot-send" title="' + t("Send", "إرسال") + '">' +
            '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/></svg>' +
            '</button></div>';
    }

    function wirePanel(panel, fab, config) {
        var title = config.title || "الدعم الفني";
        panel.querySelector(".ai-chatbot-title").textContent = title;
        panel.querySelector(".ai-chatbot-status-text").textContent = t("Online", "متصل");
        panel.querySelector(".ai-chatbot-welcome").textContent = config.welcome_message || "أهلًا! كيف يمكنني مساعدتك؟";

        var messagesEl = panel.querySelector(".ai-chatbot-messages");

        fab.addEventListener("click", function () {
            togglePanel(panel, fab, messagesEl);
        });
        panel.querySelector(".ai-chatbot-close").addEventListener("click", function () {
            closePanel(panel, fab);
        });
        panel.querySelector(".ai-chatbot-lang").addEventListener("click", function () {
            cycleLanguage(panel);
        });

        var input = panel.querySelector(".ai-chatbot-input");
        var send = panel.querySelector(".ai-chatbot-send");

        function onSend() {
            sendMessage(input, send, messagesEl);
        }
        send.addEventListener("click", onSend);
        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                onSend();
            }
        });
        input.addEventListener("input", function () {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 120) + "px";
        });

        if (state.conversation) {
            loadHistory(messagesEl);
        }
    }

    function togglePanel(panel, fab, messagesEl) {
        if (panel.classList.contains("ai-chatbot-open")) {
            closePanel(panel, fab);
        } else {
            openPanel(panel, fab, messagesEl);
        }
    }

    function openPanel(panel, fab, messagesEl) {
        panel.classList.add("ai-chatbot-open");
        fab.classList.add("ai-chatbot-fab-hidden");
        var input = panel.querySelector(".ai-chatbot-input");
        if (input) {
            input.focus();
        }
        scrollToBottom(messagesEl);
    }

    function closePanel(panel, fab) {
        panel.classList.remove("ai-chatbot-open");
        fab.classList.remove("ai-chatbot-fab-hidden");
        stopPolling();
    }

    function cycleLanguage(panel) {
        var order = ["auto", "ar", "en"];
        var idx = order.indexOf(state.lang);
        state.lang = order[(idx + 1) % order.length];
        storeLang(state.lang);
    }

    function loadHistory(messagesEl) {
        if (state.loadedHistory || !state.conversation) {
            return;
        }
        state.loadedHistory = true;
        frappe.call({
            method: "ai_agent.chatbot.api.get_messages",
            args: { conversation: state.conversation },
            callback: function (response) {
                var messages = response && response.message ? response.message : [];
                if (!messages.length) {
                    return;
                }
                messagesEl.innerHTML = "";
                messages.forEach(function (row) {
                    var role = String(row.role || "").toLowerCase();
                    if (role === "user") {
                        addBubble(messagesEl, "user", row.content || "");
                    } else if (role === "assistant") {
                        addBubble(messagesEl, "assistant", row.content || "");
                    }
                });
                scrollToBottom(messagesEl);
            },
            error: function () {}
        });
    }

    function sendMessage(input, send, messagesEl) {
        var message = (input.value || "").trim();
        if (!message || state.busy) {
            return;
        }
        addBubble(messagesEl, "user", message);
        input.value = "";
        input.style.height = "auto";
        setBusy(true, input, send, messagesEl);

        frappe.call({
            method: "ai_agent.chatbot.api.start_message",
            args: { message: message, conversation: state.conversation },
            callback: function (response) {
                var result = response && response.message ? response.message : null;
                if (result && result.stream_id) {
                    state.activeStreamId = result.stream_id;
                    if (result.conversation) {
                        state.conversation = result.conversation;
                        storeConversation(result.conversation);
                    }
                    removeTyping(messagesEl);
                    var bubble = startStreamingBubble(messagesEl);
                    startPolling(result.stream_id, bubble, input, send, messagesEl);
                } else {
                    finishWithFallback(messagesEl, input, send);
                }
            },
            error: function () {
                finishWithFallback(messagesEl, input, send);
            }
        });
    }

    function startStreamingBubble(messagesEl) {
        var wrapper = document.createElement("div");
        wrapper.className = "ai-chatbot-row ai-chatbot-assistant ai-chatbot-streaming";
        var bubble = document.createElement("div");
        bubble.className = "ai-chatbot-bubble";
        bubble.innerHTML = '<span class="ai-chatbot-cursor"></span>';
        wrapper.appendChild(bubble);
        messagesEl.appendChild(wrapper);
        scrollToBottom(messagesEl);
        return bubble;
    }

    function startPolling(streamId, bubble, input, send, messagesEl) {
        stopPolling();
        var cycles = 0;

        function tick() {
            frappe.call({
                method: "ai_agent.chatbot.api.get_stream_content",
                args: { stream_id: streamId },
                callback: function (response) {
                    var result = response && response.message ? response.message : null;
                    if (!result) {
                        return;
                    }
                    if (result.content) {
                        renderStreamed(bubble, result.content);
                        scrollToBottom(messagesEl);
                    }
                    cycles += 1;
                    if (result.status === "completed" || result.status === "failed") {
                        state.activeStreamId = null;
                        finalizeBubble(bubble);
                        scrollToBottom(messagesEl);
                        setBusy(false, input, send, messagesEl);
                        return;
                    }
                    if (cycles > 2400) {
                        state.activeStreamId = null;
                        setBusy(false, input, send, messagesEl);
                        return;
                    }
                    state.pollTimer = setTimeout(tick, 350);
                },
                error: function () {
                    cycles += 1;
                    if (cycles > 2400) {
                        state.activeStreamId = null;
                        setBusy(false, input, send, messagesEl);
                        return;
                    }
                    state.pollTimer = setTimeout(tick, 500);
                }
            });
        }
        state.pollTimer = setTimeout(tick, 300);
    }

    function renderStreamed(bubble, content) {
        bubble.setAttribute("data-ai-md", content);
        bubble.innerHTML = renderMarkdown(content);
        var cursor = document.createElement("span");
        cursor.className = "ai-chatbot-cursor";
        bubble.appendChild(cursor);
    }

    function stopPolling() {
        if (state.pollTimer) {
            clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function finalizeBubble(bubble) {
        if (!bubble) {
            return;
        }
        var cursor = bubble.querySelector(".ai-chatbot-cursor");
        if (cursor && cursor.parentNode) {
            cursor.parentNode.removeChild(cursor);
        }
        var row = bubble.closest(".ai-chatbot-streaming");
        if (row) {
            row.classList.remove("ai-chatbot-streaming");
        }
    }

    function finishWithFallback(messagesEl, input, send) {
        removeTyping(messagesEl);
        setBusy(false, input, send, messagesEl);
    }

    function setBusy(busy, input, send, messagesEl) {
        state.busy = busy;
        if (input) {
            input.disabled = busy;
        }
        if (send) {
            send.disabled = busy;
        }
        if (busy) {
            addTypingRow(messagesEl);
        }
    }

    function addTypingRow(messagesEl) {
        var wrapper = document.createElement("div");
        wrapper.className = "ai-chatbot-row ai-chatbot-assistant ai-chatbot-typing-row";
        var bubble = document.createElement("div");
        bubble.className = "ai-chatbot-bubble ai-chatbot-typing";
        bubble.innerHTML = '<span class="ai-typing-dot"></span><span class="ai-typing-dot"></span><span class="ai-typing-dot"></span>';
        wrapper.appendChild(bubble);
        messagesEl.appendChild(wrapper);
        scrollToBottom(messagesEl);
    }

    function removeTyping(container) {
        var rows = container.querySelectorAll(".ai-chatbot-typing-row");
        var last = null;
        for (var i = 0; i < rows.length; i++) {
            last = rows[i];
        }
        if (last && last.parentNode) {
            last.parentNode.removeChild(last);
        }
    }

    function addBubble(container, role, content) {
        var wrapper = document.createElement("div");
        wrapper.className = "ai-chatbot-row ai-chatbot-" + role;
        var bubble = document.createElement("div");
        bubble.className = "ai-chatbot-bubble";
        bubble.setAttribute("data-ai-md", content);
        bubble.innerHTML = renderMarkdown(content);
        wrapper.appendChild(bubble);
        container.appendChild(wrapper);
        scrollToBottom(container);
    }

    function renderMarkdown(text) {
        var source = String(text || "");

        // 1) Block-level code fences — protect them before anything else.
        var blocks = [];
        source = source.replace(/```([\s\S]*?)```/g, function (match, code) {
            blocks.push('<pre class="ai-code-block"><code>' + code + "</code></pre>");
            return "\u0000BLOCK" + (blocks.length - 1) + "\u0000";
        });

        // 2) GFM tables — handle natively so they always render even without `marked`.
        source = renderTables(source, blocks);

        // 3) Inline formatting.
        source = escapeHtml(source);
        source = source.replace(/`([^`]+)`/g, "<code>$1</code>");
        source = source.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        source = source.replace(/\*([^*]+)\*/g, "<em>$1</em>");
        source = source.replace(/^### (.*)$/gm, "<h4>$1</h4>");
        source = source.replace(/^## (.*)$/gm, "<h4>$1</h4>");
        source = source.replace(/^# (.*)$/gm, "<h4>$1</h4>");
        source = source.replace(/^\s*[-*]\s+(.*)$/gm, "<li>$1</li>");
        source = source.replace(/^\s*\d+[.)]\s+(.*)$/gm, "<li>$1</li>");
        source = source.replace(/((?:<li>.*<\/li>\s*)+)/g, "<ul>$1</ul>");
        source = source.replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");
        source = source.replace(/\n/g, "<br>");

        // 4) Restore code blocks.
        source = source.replace(/\u0000BLOCK(\d+)\u0000/g, function (m, idx) {
            return blocks[Number(idx)];
        });

        return source;
    }

    // Convert GitHub-Flavored Markdown tables into styled HTML (table HTML is
    // stashed in `blocks` so it survives the later escapeHtml pass).
    function renderTables(src, blocks) {
        var lines = String(src || "").split(/\r?\n/);
        var out = [];
        var i = 0;
        while (i < lines.length) {
            var line = lines[i];
            var isHeader = /^\s*\|/.test(line) && /\|/.test(line.slice(1));
            if (isHeader && i + 1 < lines.length) {
                var headerCells = splitRow(line);
                var sepLine = lines[i + 1];
                var isSep = /^\s*\|?[\s:|-]+\|?\s*$/.test(sepLine) && /-/.test(sepLine.replace(/[:\s|]/g, ""));
                if (isSep) {
                    var body = [];
                    var j = i + 2;
                    while (j < lines.length && /^\s*\|/.test(lines[j])) {
                        body.push(splitRow(lines[j]));
                        j += 1;
                    }
                    var idx = blocks.length;
                    blocks.push(buildTableHTML(headerCells, body));
                    out.push("\u0000BLOCK" + idx + "\u0000");
                    i = j;
                    continue;
                }
            }
            out.push(line);
            i += 1;
        }
        return out.join("\n");
    }

    function splitRow(line) {
        var raw = String(line || "").trim();
        raw = raw.replace(/^\|/, "").replace(/\|$/, "");
        return raw.split("|").map(function (cell) {
            return cell.trim();
        });
    }

    function buildTableHTML(headers, rows) {
        var html = '<div class="ai-md-table-wrap"><table class="ai-md-table">';
        html += "<thead><tr>";
        for (var h = 0; h < headers.length; h++) {
            html += "<th>" + escapeHtml(headers[h]) + "</th>";
        }
        html += "</tr></thead><tbody>";
        for (var r = 0; r < rows.length; r++) {
            var cells = rows[r];
            html += "<tr>";
            for (var c = 0; c < headers.length; c++) {
                html += "<td>" + escapeHtml(cells[c] !== undefined ? cells[c] : "") + "</td>";
            }
            html += "</tr>";
        }
        html += "</tbody></table></div>";
        return html;
    }

    function escapeHtml(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function scrollToBottom(container) {
        if (container) {
            container.scrollTop = container.scrollHeight;
        }
    }

    function getStoredConversation() {
        try {
            var key = STORAGE_KEY + "_" + (frappe.boot.user.name || "guest");
            return window.localStorage.getItem(key) || null;
        } catch (e) {
            return null;
        }
    }

    function storeConversation(value) {
        try {
            var key = STORAGE_KEY + "_" + (frappe.boot.user.name || "guest");
            window.localStorage.setItem(key, value);
        } catch (e) {}
    }

    function getStoredLang() {
        try {
            return window.localStorage.getItem(PREFS_KEY) || "auto";
        } catch (e) {
            return "auto";
        }
    }

    function storeLang(lang) {
        try {
            window.localStorage.setItem(PREFS_KEY, lang);
        } catch (e) {}
    }

    function ensureMarked() {
        if (window.marked || document.getElementById("ai-marked-script")) {
            reRenderExistingBubbles();
            return;
        }
        var srcs = [
            "/assets/frappe/js/lib/marked.js",
            "https://cdn.jsdelivr.net/npm/marked/marked.min.js"
        ];
        var loadNext = function (node) {
            var next = srcs.shift();
            if (!next) {
                return;
            }
            node.src = next;
        };
        var script = document.createElement("script");
        script.id = "ai-marked-script";
        script.async = true;
        script.onload = function () {
            reRenderExistingBubbles();
        };
        script.onerror = function () {
            loadNext(script);
        };
        loadNext(script);
        document.head.appendChild(script);
    }

    function reRenderExistingBubbles() {
        var messagesEl = document.querySelector(".ai-chatbot-messages");
        if (!messagesEl) {
            return;
        }
        // Re-render already-finalized assistant bubbles (no cursor marker).
        var bubbles = messagesEl.querySelectorAll(
            ".ai-chatbot-row:not(.ai-chatbot-streaming) .ai-chatbot-bubble"
        );
        for (var i = 0; i < bubbles.length; i++) {
            var raw = bubbles[i].getAttribute("data-ai-md");
            if (raw !== null) {
                bubbles[i].innerHTML = renderMarkdown(raw);
            }
        }
        scrollToBottom(messagesEl);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
