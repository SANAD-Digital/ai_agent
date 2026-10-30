// ai_agent_chat.bundle.js — the chat client (public/js/chat/*), loaded on demand by the agent-workspace page and by the
// support bubble (chat_bubble.js). One hashed file instead of twelve: a build always reaches browsers despite
// the year-long /assets cache, and the modules run once, in this order (each extends window.AIChat).
import "./chat/core.js";
import "./chat/store.js";
import "./chat/transport.js";
import "./chat/viewhost.js";
import "./chat/blocks.js";
import "./chat/message.js";
import "./chat/composer.js";
import "./chat/sidebar.js";
import "./chat/memory.js";
import "./chat/list.js";
import "./chat/bubble.js";
import "./chat/app.js";
