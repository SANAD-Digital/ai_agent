/**
 * core.js — the `window.AIChat` namespace and small, dependency-free helpers.
 *
 * Responsibility: DOM construction (`h`), safe markdown (`md`), formatting
 * (`fmtTokens`, `fmtDuration`, `relTime`), timing helpers (`debounce`,
 * `throttle`), inline SVG icons (`icon`) and the tool wire-name → human label
 * map (`toolLabel`). No state, no network, no rendering of chat objects.
 */
(function () {
    "use strict";

    const AIChat = (window.AIChat = window.AIChat || {});
    const __ = window.__ || ((s) => s);

    /* ------------------------------------------------------------------ */
    /* DOM                                                                 */
    /* ------------------------------------------------------------------ */

    /**
     * h("div.a.b", {class, text, html, onclick, dataset, aria, ...attrs}, ...children)
     * `html` is only for already-sanitised markup (output of `md`).
     */
    function h(tag, attrs, ...children) {
        if (attrs instanceof Node || typeof attrs === "string" || Array.isArray(attrs)) {
            children.unshift(attrs);
            attrs = null;
        }
        const parts = String(tag).split(".");
        const el = document.createElement(parts[0] || "div");
        if (parts.length > 1) el.className = parts.slice(1).join(" ");
        if (attrs) {
            for (const key of Object.keys(attrs)) {
                const val = attrs[key];
                if (val === null || val === undefined || val === false) continue;
                if (key === "class" || key === "className") {
                    el.className = el.className ? el.className + " " + val : val;
                } else if (key === "text") {
                    el.textContent = val;
                } else if (key === "html") {
                    el.innerHTML = val;
                } else if (key === "dataset") {
                    for (const k of Object.keys(val)) el.dataset[k] = val[k];
                } else if (key === "aria") {
                    for (const k of Object.keys(val)) el.setAttribute("aria-" + k, val[k]);
                } else if (key.startsWith("on") && typeof val === "function") {
                    el.addEventListener(key.slice(2).toLowerCase(), val);
                } else if (key === "style" && typeof val === "object") {
                    for (const k of Object.keys(val)) el.style.setProperty(k, val[k]);
                } else if (typeof val === "boolean") {
                    if (val) el.setAttribute(key, "");
                } else {
                    el.setAttribute(key, val);
                }
            }
        }
        append(el, children);
        return el;
    }

    function append(el, children) {
        for (const child of children) {
            if (child === null || child === undefined || child === false) continue;
            if (Array.isArray(child)) {
                append(el, child);
            } else if (child instanceof Node) {
                el.appendChild(child);
            } else {
                el.appendChild(document.createTextNode(String(child)));
            }
        }
    }

    function clear(el) {
        while (el.firstChild) el.removeChild(el.firstChild);
        return el;
    }

    function escape(text) {
        return String(text === null || text === undefined ? "" : text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    /* ------------------------------------------------------------------ */
    /* Markdown                                                            */
    /* ------------------------------------------------------------------ */

    const BLOCKED_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "FORM", "META", "LINK", "BASE"]);
    const URL_ATTRS = ["href", "src", "xlink:href", "action", "formaction"];

    function sanitiseHtml(html) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_ELEMENT);
        const doomed = [];
        let node = walker.nextNode();
        while (node) {
            if (BLOCKED_TAGS.has(node.tagName.toUpperCase())) {
                doomed.push(node);
            } else {
                for (const attr of Array.from(node.attributes)) {
                    const name = attr.name.toLowerCase();
                    const value = String(attr.value || "").replace(/[\u0000-\u001f\s]+/g, "").toLowerCase();
                    if (name.startsWith("on")) {
                        node.removeAttribute(attr.name);
                    } else if (URL_ATTRS.includes(name) && (value.startsWith("javascript:") || value.startsWith("vbscript:") || value.startsWith("data:text/html"))) {
                        node.removeAttribute(attr.name);
                    }
                }
                if (node.tagName === "A") {
                    node.setAttribute("target", "_blank");
                    node.setAttribute("rel", "noopener noreferrer");
                }
                if (node.tagName === "TABLE") {
                    node.classList.add("ac-md-table");
                }
            }
            node = walker.nextNode();
        }
        doomed.forEach((n) => n.parentNode && n.parentNode.removeChild(n));
        return tpl.innerHTML;
    }

    /** Markdown → sanitised HTML (string). */
    function md(text) {
        const src = text === null || text === undefined ? "" : String(text);
        if (!src.trim()) return "";
        let html;
        try {
            html = window.frappe && typeof frappe.markdown === "function" ? frappe.markdown(src) : "<p>" + escape(src).replace(/\n/g, "<br>") + "</p>";
        } catch (e) {
            html = "<p>" + escape(src).replace(/\n/g, "<br>") + "</p>";
        }
        return sanitiseHtml(html);
    }

    /* ------------------------------------------------------------------ */
    /* Formatting                                                          */
    /* ------------------------------------------------------------------ */

    function fmtTokens(n) {
        n = Number(n) || 0;
        if (n < 1000) return String(n);
        if (n < 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
        if (n < 1000000) return Math.round(n / 1000) + "k";
        return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    }

    function fmtDuration(ms) {
        ms = Number(ms) || 0;
        if (ms < 1000) return __("{0} ث", [(ms / 1000).toFixed(1)]);
        const s = ms / 1000;
        if (s < 10) return __("{0} ث", [s.toFixed(1).replace(/\.0$/, "")]);
        if (s < 60) return __("{0} ث", [Math.round(s)]);
        const m = Math.floor(s / 60);
        const rest = Math.round(s - m * 60);
        return __("{0} د {1} ث", [m, rest]);
    }

    function fmtCost(v) {
        v = Number(v) || 0;
        if (!v) return "";
        return "$" + (v < 0.01 ? v.toFixed(4) : v.toFixed(2));
    }

    /** Parse a server timestamp ("YYYY-MM-DD HH:MM:SS[.ffffff]") as a Date. */
    function parseTs(ts) {
        if (!ts) return null;
        if (ts instanceof Date) return ts;
        const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/);
        if (!m) {
            const d = new Date(ts);
            return isNaN(d) ? null : d;
        }
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    }

    function relTime(ts) {
        const d = parseTs(ts);
        if (!d) return "";
        const diff = Math.max(0, (Date.now() - d.getTime()) / 1000);
        if (diff < 45) return __("الآن");
        if (diff < 3600) return __("قبل {0} د", [Math.round(diff / 60)]);
        if (diff < 86400) return __("قبل {0} س", [Math.round(diff / 3600)]);
        if (diff < 7 * 86400) return __("قبل {0} ي", [Math.round(diff / 86400)]);
        try {
            return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
        } catch (e) {
            return d.toDateString();
        }
    }

    function fmtTime(ts) {
        const d = parseTs(ts);
        if (!d) return "";
        try {
            return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        } catch (e) {
            return "";
        }
    }

    function fmtBytes(n) {
        n = Number(n) || 0;
        if (n < 1024) return n + " B";
        if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
        return (n / (1024 * 1024)).toFixed(1) + " MB";
    }

    /* ------------------------------------------------------------------ */
    /* Timing                                                              */
    /* ------------------------------------------------------------------ */

    function debounce(fn, wait) {
        let t = null;
        const wrapped = function (...args) {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), wait);
        };
        wrapped.cancel = () => clearTimeout(t);
        return wrapped;
    }

    /** Leading + trailing throttle: the last call always lands. */
    function throttle(fn, wait) {
        let last = 0;
        let timer = null;
        let pendingArgs = null;
        const wrapped = function (...args) {
            const now = Date.now();
            const remaining = wait - (now - last);
            pendingArgs = args;
            if (remaining <= 0) {
                clearTimeout(timer);
                timer = null;
                last = now;
                fn.apply(this, pendingArgs);
                pendingArgs = null;
            } else if (!timer) {
                timer = setTimeout(() => {
                    last = Date.now();
                    timer = null;
                    if (pendingArgs) fn.apply(this, pendingArgs);
                    pendingArgs = null;
                }, remaining);
            }
        };
        wrapped.flush = function () {
            if (timer) {
                clearTimeout(timer);
                timer = null;
                if (pendingArgs) fn.apply(this, pendingArgs);
                pendingArgs = null;
            }
        };
        wrapped.cancel = () => {
            clearTimeout(timer);
            timer = null;
            pendingArgs = null;
        };
        return wrapped;
    }

    /* ------------------------------------------------------------------ */
    /* Icons (lucide-style, 24×24, stroke = currentColor)                  */
    /* ------------------------------------------------------------------ */

    const ICONS = {
        send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
        mail: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
        printer: '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect width="12" height="8" x="6" y="14"/>',
        image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
        puzzle: '<path d="M15.39 4.39a1 1 0 0 0 1.68-.47 2.5 2.5 0 1 1 3.01 3.01 1 1 0 0 0-.47 1.68l1.68 1.68a2.41 2.41 0 0 1 0 3.42l-1.68 1.68a1 1 0 0 1-1.68-.47 2.5 2.5 0 1 0-3.01 3.01 1 1 0 0 1 .47 1.68l-1.68 1.68a2.41 2.41 0 0 1-3.42 0l-1.68-1.68a1 1 0 0 0-1.68.47 2.5 2.5 0 1 1-3.01-3.01 1 1 0 0 0 .47-1.68l-1.68-1.68a2.41 2.41 0 0 1 0-3.42l1.68-1.68a1 1 0 0 1 1.68.47 2.5 2.5 0 1 0 3.01-3.01 1 1 0 0 1-.47-1.68l1.68-1.68a2.41 2.41 0 0 1 3.42 0z"/>',
        stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>',
        plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
        search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
        pin: '<line x1="12" x2="12" y1="17" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>',
        edit: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/>',
        archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
        trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
        copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
        refresh: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
        "thumbs-up": '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
        "thumbs-down": '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
        chevron: '<path d="m6 9 6 6 6-6"/>',
        "chevron-right": '<path d="m9 18 6-6-6-6"/>',
        wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
        brain: '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
        paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
        sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
        menu: '<path d="M4 12h16"/><path d="M4 6h16"/><path d="M4 18h16"/>',
        "arrow-down": '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
        file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
        table: '<path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/>',
        chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
        link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
        settings: '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
        database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
        book: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
        more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
        download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
        panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
        bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
        external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
        expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
        shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
        clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
        loader: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
        user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
        zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
        code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
        list: '<line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/>',
        info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
        "message-square": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    };

    /** Returns an inline <svg> element for `name` (falls back to a dot). */
    function icon(name, cls) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("width", "1em");
        svg.setAttribute("height", "1em");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        svg.setAttribute("class", "ac-icon ac-icon-" + name + (cls ? " " + cls : ""));
        svg.innerHTML = ICONS[name] || '<circle cx="12" cy="12" r="3"/>';
        return svg;
    }

    /* ------------------------------------------------------------------ */
    /* Tool names                                                          */
    /* ------------------------------------------------------------------ */

    const TOOL_LABELS = [
        [/^data__query$/, () => __("استعلام بيانات"), "database"],
        [/^data__get_document$/, () => __("قراءة مستند"), "file"],
        [/^data__get_child_records$/, () => __("قراءة بنود مستند"), "list"],
        [/^data__count$/, () => __("عدّ السجلات"), "database"],
        [/^reports__run$/, () => __("تشغيل تقرير"), "table"],
        [/^reports__(search|describe)$/, () => __("البحث في التقارير"), "search"],
        [/^analysis__/, () => __("تحليل"), "chart"],
        [/^erp__/, () => __("بيانات ERP"), "database"],
        [/^diagnose__/, () => __("تشخيص مستند"), "search"],
        [/^setup__check$/, () => __("فحص الإعداد"), "settings"],
        [/^knowledge__search$/, () => __("البحث في الدليل"), "book"],
        [/^skills__(list|read_reference)$/, () => __("المهارات"), "book"],
        [/^tools__enable$/, () => __("تفعيل أدوات إضافية"), "tool"],
        [/^attachments__list$/, () => __("مرفقات مستند"), "paperclip"],
        [/^metrics__query$/, () => __("مؤشر"), "chart"],
        [/^schema__/, () => __("استكشاف المخطط"), "list"],
        [/^skills__load$/, () => __("تحميل مهارة"), "book"],
        [/^actions__prepare_document$/, () => __("تجهيز مستند"), "file"],
        [/^actions__validate_document$/, () => __("التحقق من مستند"), "check"],
        [/^actions__request_/, () => __("طلب اعتماد"), "shield"],
        [/^artifacts__/, () => __("إنشاء عرض"), "chart"],
        [/^memory__/, () => __("الذاكرة"), "brain"],
        [/^files__read$/, () => __("قراءة ملف مرفق"), "paperclip"],
    ];

    /** Wire name ("data__query") → human label; unknown names show dotted. */
    function toolLabel(wireName) {
        const name = String(wireName || "");
        for (const [re, label] of TOOL_LABELS) {
            if (re.test(name)) return label();
        }
        return name.replace(/__/g, ".");
    }

    function toolIcon(wireName) {
        const name = String(wireName || "");
        for (const [re, , ic] of TOOL_LABELS) {
            if (re.test(name)) return ic;
        }
        return "wrench";
    }

    function toolDotted(wireName) {
        return String(wireName || "").replace(/__/g, ".");
    }

    /* ------------------------------------------------------------------ */
    /* Misc                                                                */
    /* ------------------------------------------------------------------ */

    function copyText(text) {
        const value = String(text || "");
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(value).catch(() => legacyCopy(value));
        }
        return Promise.resolve(legacyCopy(value));
    }

    function legacyCopy(value) {
        const ta = document.createElement("textarea");
        ta.value = value;
        ta.setAttribute("readonly", "");
        ta.className = "ac-visually-hidden";
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand("copy");
        } catch (e) {
            /* ignore */
        }
        document.body.removeChild(ta);
    }

    function toast(message, indicator) {
        if (window.frappe && typeof frappe.show_alert === "function") {
            frappe.show_alert({ message, indicator: indicator || "green" }, 3);
        }
    }

    function errorMessage(err) {
        if (!err) return __("حدث خطأ غير متوقع");
        if (typeof err === "string") return stripHtml(err);
        if (err.message) return stripHtml(err.message);
        if (err._server_messages) {
            try {
                const arr = JSON.parse(err._server_messages);
                const first = JSON.parse(arr[0]);
                return stripHtml(first.message || arr[0]);
            } catch (e) {
                return stripHtml(err._server_messages);
            }
        }
        if (err.exc) return stripHtml(String(err.exc).split("\n").pop());
        return __("حدث خطأ غير متوقع");
    }

    function stripHtml(s) {
        const div = document.createElement("div");
        div.innerHTML = String(s);
        return div.textContent || "";
    }

    function prettyJson(value) {
        try {
            if (typeof value === "string") {
                try {
                    return JSON.stringify(JSON.parse(value), null, 2);
                } catch (e) {
                    return value;
                }
            }
            return JSON.stringify(value, null, 2);
        } catch (e) {
            return String(value);
        }
    }

    function formLink(doctype, name) {
        if (!doctype || !name) return "#";
        const slug = window.frappe && frappe.router && frappe.router.slug ? frappe.router.slug(doctype) : String(doctype).toLowerCase().replace(/ /g, "-");
        return "/app/" + slug + "/" + encodeURIComponent(name);
    }

    function uid() {
        return "ac" + Math.random().toString(36).slice(2, 9);
    }

    function isRtl() {
        return (document.documentElement.getAttribute("dir") || document.body.getAttribute("dir") || "").toLowerCase() === "rtl";
    }

    /** A phone or tablet: the on-screen keyboard opens whenever an input has focus. */
    function isTouch() {
        return window.matchMedia("(hover: none) and (pointer: coarse)").matches;
    }

    /** "rtl" / "ltr" from the first letter of `text` (digits and symbols skipped); null when it has no letter. */
    function textDir(text) {
        const m = /\p{L}/u.exec(text || "");
        if (!m) return null;
        return /[\p{Script=Arabic}\p{Script=Hebrew}\p{Script=Syriac}\p{Script=Thaana}\p{Script=Nko}]/u.test(m[0]) ? "rtl" : "ltr";
    }

    Object.assign(AIChat, {
        __,
        h,
        append,
        clear,
        escape,
        md,
        sanitiseHtml,
        fmtTokens,
        fmtDuration,
        fmtCost,
        fmtBytes,
        fmtTime,
        parseTs,
        relTime,
        debounce,
        throttle,
        icon,
        toolLabel,
        toolIcon,
        toolDotted,
        copyText,
        toast,
        errorMessage,
        prettyJson,
        formLink,
        uid,
        isRtl,
        isTouch,
        textDir,
    });
})();
