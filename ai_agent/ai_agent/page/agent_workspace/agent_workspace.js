frappe.provide("ai_agent");

frappe.pages["agent-workspace"].on_page_load = function (wrapper) {
    frappe.require("/assets/ai_agent/css/agent_workspace.css");
    $(wrapper).addClass("agent-workspace-page");
    inject_layout_fix();
    frappe.ui.make_app_page({
        parent: wrapper,
        title: __("Agent Workspace"),
        single_column: true,
    });

    const page = wrapper.page;
    page.main.addClass("agent-workspace-main");
    page.main.empty();
    page.main.html(`
        <div class="agent-chat-shell">
            <aside class="agent-chat-sidebar">
                <div class="chat-sidebar-head">
                    <div>
                        <div class="chat-kicker">${__("AI Agent")}</div>
                        <h3>${__("Chats")}</h3>
                    </div>
                    <button class="btn btn-primary btn-sm" id="btn-new-chat">${__("New")}</button>
                </div>
                <div class="chat-agent-controls">
                    <div id="agent-select"></div>
                    <div id="company-select"></div>
                </div>
                <div id="skills-bar" class="chat-skills"></div>
                <div class="chat-sessions-title">${__("Recent Sessions")}</div>
                <div id="chat-sessions" class="chat-sessions"></div>
            </aside>

            <section class="agent-chat-main">
                <header class="chat-topbar">
                    <div>
                        <div class="chat-kicker">${__("ERP assistant")}</div>
                        <h2 id="chat-title">${__("New chat")}</h2>
                    </div>
                    <div id="run-status" class="chat-run-status"></div>
                </header>

                <div id="chat-messages" class="chat-messages">
                    <div class="chat-empty-state">
                        <h3>${__("Ask an ERP question")}</h3>
                        <p>${__("Simple questions answer instantly. Deeper analysis keeps a readable work log.")}</p>
                    </div>
                </div>

                <footer class="chat-composer">
                    <div class="composer-input-wrap">
                        <textarea id="prompt-input" class="form-control" rows="1" placeholder="${__("Ask about sales, invoices, stock, customers...")}"></textarea>
                        <div id="attached-files" class="attached-files"></div>
                        <input id="file-input" type="file" multiple hidden
                            accept=".csv,.json,.txt,.md,.log,.pdf,.xlsx,.xls,.png,.jpg,.jpeg,.webp">
                    </div>
                    <div class="composer-actions">
                        <button class="btn btn-default btn-sm voice-btn" id="btn-voice" title="${__("Dictate with voice")}" aria-label="${__("Dictate with voice")}">🎙️</button>
                        <button class="btn btn-default btn-sm" id="btn-attach" title="${__("Attach files")}">📎</button>
                        <button class="btn btn-default btn-sm" id="btn-cancel" style="display:none">${__("Cancel")}</button>
                        <button class="btn btn-primary btn-sm" id="btn-send">${__("Send")}</button>
                    </div>
                </footer>
            </section>
        </div>
    `);

    setup_layout_sizing();

    let currentRoot = null;
    let currentRun = null;
    let pollInterval = null;
    let selectedAgent = null;
    let selectedCompany = null;
    let agentControl = null;
    let companyControl = null;
    let selectedFiles = [];
    let speechRecognition = null;
    let isListening = false;
    let voiceBasePrompt = "";
    let printLetterHeads = null;

    setup_controls();
    bind_events();
    load_sessions();

    function setup_layout_sizing() {
        const resizeEvent = "resize.ai_agent_workspace";
        $(window).off(resizeEvent).on(resizeEvent, frappe.utils.debounce(update_layout_height, 80));
        update_layout_height();
        requestAnimationFrame(update_layout_height);
        setTimeout(update_layout_height, 300);
    }

    function update_layout_height() {
        const main = page.main.get(0);
        if (!main) return;
        const top = main.getBoundingClientRect().top;
        const available = Math.max(540, window.innerHeight - top - 10);
        main.style.height = `${available}px`;
    }

    function setup_controls() {
        frappe.xcall("ai_agent.api.list_available_agents").then((result) => {
            const agents = result?.agents || [];
            const options = agents.map((agent) => agent.name).join("\n");
            selectedAgent = agents[0]?.name || null;
            agentControl = frappe.ui.form.make_control({
                parent: page.main.find("#agent-select"),
                df: {
                    fieldtype: "Select",
                    label: __("Agent"),
                    options,
                    fieldname: "agent",
                    onchange: () => {
                        selectedAgent = agentControl.get_value();
                        load_skills();
                    },
                },
                render_input: true,
            });
            if (selectedAgent) {
                agentControl.set_value(selectedAgent);
                load_skills();
            }
        });

        companyControl = frappe.ui.form.make_control({
            parent: page.main.find("#company-select"),
            df: {
                fieldtype: "Link",
                options: "Company",
                label: __("Company"),
                fieldname: "company",
                onchange: () => {
                    selectedCompany = companyControl.get_value();
                },
            },
            render_input: true,
        });
    }

    function bind_events() {
        page.main.find("#btn-new-chat").on("click", new_chat);
        page.main.find("#btn-send").on("click", submit_message);
        page.main.find("#btn-voice").on("click", toggle_voice_input);
        page.main.find("#btn-attach").on("click", () => page.main.find("#file-input").trigger("click"));
        page.main.find("#file-input").on("change", handle_file_select);
        page.main.find("#btn-cancel").on("click", cancel_run);
        page.main.find("#prompt-input").on("input", autosize_prompt);
        page.main.find("#prompt-input").on("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit_message();
            }
        });

        page.main.on("click", ".chat-session", function () {
            select_session($(this).data("root"));
        });
        page.main.on("click", ".btn-approve-approval", function () {
            approve_approval($(this).data("approval"));
        });
        page.main.on("click", ".btn-reject-approval", function () {
            reject_approval($(this).data("approval"));
        });
        page.main.on("click", ".btn-remove-file", function () {
            remove_selected_file(Number($(this).data("index")));
        });
        page.main.on("click", ".btn-chat-suggestion", function () {
            submit_suggested_message($(this).data("prompt"));
        });
        page.main.on("click", ".btn-print-message", function () {
            open_print_dialog($(this).data("run"), $(this).data("title"));
        });
        page.main.on("click", ".btn-load-artifact", function () {
            load_full_artifact($(this).data("artifact"));
        });
        page.main.on("click", ".btn-export-artifact", function () {
            export_artifact_csv($(this).data("artifact"));
        });

        frappe.realtime.on("ai_agent:run_updated", (data) => {
            if (currentRun && data.run_id === currentRun) refresh_current_run();
        });
        frappe.realtime.on("ai_agent:step_added", (data) => {
            if (currentRun && data.run_id === currentRun) load_steps_for_run(currentRun);
        });
        frappe.realtime.on("ai_agent:run_completed", (data) => {
            if (currentRun && data.run_id === currentRun) finish_current_run();
        });
        frappe.realtime.on("ai_agent:run_failed", (data) => {
            if (currentRun && data.run_id === currentRun) finish_current_run();
        });
        frappe.realtime.on("ai_agent:approval_required", (data) => {
            if (data?.run_id) load_approvals_for_run(data.run_id);
        });
    }

    function load_skills() {
        if (!selectedAgent) return;
        frappe.xcall("ai_agent.api.list_available_skills", {
            agent: selectedAgent,
            company: selectedCompany,
        }).then((result) => {
            const skillsBar = page.main.find("#skills-bar").empty();
            (result?.skills || []).slice(0, 8).forEach((skill) => {
                skillsBar.append(`
                    <span class="chat-skill" title="${escape_html(skill.description || "")}">
                        ${escape_html(skill.title || skill.name)}
                    </span>
                `);
            });
        });
    }

    function load_sessions() {
        frappe.xcall("ai_agent.api.list_chat_sessions", { limit: 40 }).then((result) => {
            const sessions = result?.sessions || [];
            render_sessions(sessions);
        });
    }

    function render_sessions(sessions) {
        const list = page.main.find("#chat-sessions").empty();
        if (!sessions.length) {
            list.html(`<div class="chat-session-empty">${__("No saved chats yet.")}</div>`);
            return;
        }
        sessions.forEach((session) => {
            const active = currentRoot === session.root_run ? "active" : "";
            list.append(`
                <button class="chat-session ${active}" data-root="${escape_attr(session.root_run)}">
                    <span class="session-title">${escape_html(session.title || session.root_run)}</span>
                    <span class="session-preview">${escape_html(session.preview || "")}</span>
                    <span class="session-meta">${escape_html(session.status || "")} · ${session.message_count || 1}</span>
                </button>
            `);
        });
    }

    function select_session(rootRun) {
        if (!rootRun) return Promise.resolve();
        stop_polling();
        currentRoot = rootRun;
        page.main.find("#chat-messages").html(`
            <div class="chat-empty-state">
                <h3>${__("Loading chat")}</h3>
                <p>${__("Opening saved messages and work log...")}</p>
            </div>
        `);
        page.main.find(".chat-session").removeClass("active");
        page.main.find(".chat-session").filter(function () {
            return $(this).data("root") === rootRun;
        }).addClass("active");
        return frappe.xcall("ai_agent.api.get_chat_messages", { root_run: rootRun }).then((result) => {
            currentRun = result?.latest_run || rootRun;
            render_messages(result?.messages || []);
            update_title_from_messages(result?.messages || []);
            start_polling_if_needed();
        }).catch((error) => {
            page.main.find("#chat-messages").html(`
                <div class="chat-empty-state error-state">
                    <h3>${__("Could not load chat")}</h3>
                    <p>${escape_html(error?.message || __("Please refresh and try again."))}</p>
                </div>
            `);
        });
    }

    function render_messages(messages) {
        const panel = page.main.find("#chat-messages").empty();
        update_layout_height();
        if (!messages.length) {
            panel.html(`
                <div class="chat-empty-state">
                    <h3>${__("Ask an ERP question")}</h3>
                    <p>${__("Simple questions answer instantly. Deeper analysis keeps a readable work log.")}</p>
                </div>
            `);
            return;
        }

        messages.forEach((message) => {
            if (message.role === "user") {
                panel.append(render_user_message(message));
            } else {
                panel.append(render_assistant_message(message));
                load_steps_for_run(message.run_name);
                load_approvals_for_run(message.run_name);
            }
        });
        scroll_to_bottom(true);
        update_layout_height();
    }

    function render_user_message(message) {
        return `
            <article class="chat-message user-message" data-run="${escape_attr(message.run_name)}">
                <div class="message-avatar">${__("You").charAt(0)}</div>
                <div class="message-bubble">
                    <div class="message-meta">${__("You")}</div>
                    <div class="message-content plain">${escape_html(message.content || "")}</div>
                    ${render_file_chips(message.attachments || [])}
                </div>
            </article>
        `;
    }

    function render_file_chips(files) {
        if (!files.length) return "";
        return `
            <div class="message-files">
                ${files.map((file) => `
                    <span class="file-chip">
                        <span>📎</span>
                        <span>${escape_html(file.file_name || file.name)}</span>
                        ${file.file_size ? `<span class="file-size">${format_file_size(file.file_size)}</span>` : ""}
                    </span>
                `).join("")}
            </div>
        `;
    }

    function render_assistant_message(message) {
        const statusClass = (message.status || "").toLowerCase().replace(/\s+/g, "-");
        const content = message.content
            ? safe_markdown(message.content)
            : `<p class="muted">${__("Working on it...")}</p>`;
        const reportClass = is_printable_message(message) ? "report-message" : "";
        return `
            <article class="chat-message assistant-message ${message.is_error ? "error" : ""}" data-run="${escape_attr(message.run_name)}">
                <div class="message-avatar">AI</div>
                <div class="message-bubble">
                    <div class="message-meta">
                        <span>${escape_html(message.agent || __("Assistant"))}</span>
                        <span class="status-pill ${statusClass}">${escape_html(message.status || "")}</span>
                        <span>${__("Steps")}: ${message.current_step || 0}</span>
                        <span>${__("Tools")}: ${message.tool_call_count || 0}</span>
                        ${render_token_usage(message.token_usage)}
                        ${render_print_button(message)}
                    </div>
                    <div class="message-content markdown-body ${reportClass}">${content}</div>
                    ${render_chat_suggestions(message)}
                    ${render_artifacts(message.artifacts || [])}
                    <div class="approval-list" data-run-approvals="${escape_attr(message.run_name)}"></div>
                    <details class="work-log">
                        <summary>
                            <span class="work-log-summary-main">
                                <span class="work-log-caret">▸</span>
                                <span>${__("Work log")}</span>
                            </span>
                            <span class="work-log-summary-meta" data-run-step-meta="${escape_attr(message.run_name)}">
                                ${__("Loading")}
                            </span>
                        </summary>
                        <div class="work-log-body" data-run-steps="${escape_attr(message.run_name)}">
                            <div class="work-step muted">${__("Loading steps...")}</div>
                        </div>
                    </details>
                </div>
            </article>
        `;
    }

    function render_print_button(message) {
        if (!is_printable_message(message)) return "";
        const title = truncate(extract_print_title(message) || __("Report"), 90);
        return `
            <button class="btn btn-xs btn-default btn-print-message"
                data-run="${escape_attr(message.run_name)}"
                data-title="${escape_attr(title)}"
                title="${__("Print")}">
                🖨️ ${__("Print")}
            </button>
        `;
    }

    function is_printable_message(message) {
        if (!message || message.is_error || message.status !== "Completed" || !message.content) return false;
        if (message.printable) return true;
        if ((message.artifacts || []).length) return true;
        if (/\|[^\n]+\|/.test(message.content || "")) return true;

        const source = normalize_arabic(`${message.user_prompt || ""} ${message.content || ""}`);
        return [
            "تقرير", "تحليل", "بحث", "بيانات", "كشف", "قائمه", "جدول",
            "مبيعات", "مشتريات", "عملاء", "موردين", "مخزون", "رصيد",
            "report", "analysis", "search", "data", "table",
        ].some((marker) => source.includes(marker));
    }

    function render_chat_suggestions(message) {
        const suggestions = get_chat_suggestions(message);
        if (!suggestions.length) return "";
        return `
            <div class="chat-suggestions">
                ${suggestions.map((suggestion) => `
                    <button class="btn btn-sm btn-primary btn-chat-suggestion"
                        data-prompt="${escape_attr(suggestion.prompt)}">
                        ${escape_html(suggestion.label)}
                    </button>
                `).join("")}
            </div>
        `;
    }

    function get_chat_suggestions(message) {
        const content = message?.content || "";
        const suggestions = [];
        const nextActions = message?.next_actions || [];
        if (nextActions.length) {
            return nextActions
                .filter((action) => action?.label && action?.prompt)
                .map((action) => ({
                    label: action.label,
                    prompt: action.prompt,
                }));
        }
        if (should_suppress_generic_continue(message)) {
            return suggestions;
        }
        const textualNextAction = build_textual_next_action(message);
        if (textualNextAction) {
            return [textualNextAction];
        }
        const canContinue = (
            content.includes("اكتب **اكمل**")
            || content.includes("اكتب اكمل")
            || (content.includes("تم تنفيذ الموافقة") && content.includes("الخطوة التالية"))
            || (content.includes("طلب مركب") && content.includes("الخطوة التالية"))
        );
        if (canContinue && ["Completed", "Failed"].includes(message.status || "")) {
            suggestions.push({
                label: build_continue_action_label(message),
                prompt: "اكمل",
            });
        }
        return suggestions;
    }

    function should_suppress_generic_continue(message) {
        const content = normalize_arabic(message?.content || "");
        const executedInvoiceAction = (
            content.includes("تم تنفيذ الموافقه")
            && (
                content.includes("الاجراء: **create**")
                || content.includes("الاجراء: **submit**")
            )
            && content.includes("الرقم/الاسم")
        );
        const isInvoice = (
            content.includes("المستند: **purchase invoice**")
            || content.includes("المستند:purchase invoice")
            || content.includes("المستند: **sales invoice**")
            || content.includes("المستند:sales invoice")
        );
        const executedPaymentAction = (
            content.includes("تم تنفيذ الموافقه")
            && (
                content.includes("الاجراء: **create**")
                || content.includes("الاجراء: **submit**")
            )
            && (
                content.includes("المستند: **payment entry**")
                || content.includes("المستند:payment entry")
            )
        );
        return (executedInvoiceAction && isInvoice) || executedPaymentAction;
    }

    function build_textual_next_action(message) {
        const content = message?.content || "";
        const normalized = normalize_arabic(content);
        const documentName = extract_last_document_name(content);
        const suggestedLabel = extract_suggested_action_label(content);
        if (!documentName || !normalized.includes("الخطوه التاليه المقترحه")) {
            return null;
        }
        if (normalized.includes("اعتماد item") || normalized.includes("اعتمد item")) {
            return null;
        }
        if (normalized.includes("سجل دفع الفاتوره")) {
            return {
                label: suggestedLabel || __("سجل دفع الفاتورة"),
                prompt: `سجل دفع فاتورة الشراء ${documentName}`,
            };
        }
        if (normalized.includes("سجل قبض الفاتوره")) {
            return {
                label: suggestedLabel || __("سجل قبض الفاتورة"),
                prompt: `سجل قبض فاتورة البيع ${documentName}`,
            };
        }
        if (normalized.includes("اعتمد فاتوره الشراء")) {
            return {
                label: suggestedLabel || __("اعتمد فاتورة الشراء"),
                prompt: `اعتمد فاتورة الشراء ${documentName}`,
            };
        }
        if (normalized.includes("اعتمد فاتوره البيع")) {
            return {
                label: suggestedLabel || __("اعتمد فاتورة البيع"),
                prompt: `اعتمد فاتورة البيع ${documentName}`,
            };
        }
        if (normalized.includes("اعتمد سند الدفع")) {
            return {
                label: suggestedLabel || __("اعتمد سند الدفع"),
                prompt: `اعتمد سند الدفع ${documentName}`,
            };
        }
        return null;
    }

    function extract_suggested_action_label(content) {
        const text = String(content || "");
        const lineMatch = text.match(/الخطوة التالية المقترحة:\s*([^\n]+)/);
        if (lineMatch?.[1]) {
            return lineMatch[1].replace(/\*\*/g, "").trim();
        }
        const boldMatch = text.match(/الخطوة التالية المقترحة:[\s\S]*?\*\*([^*]+)\*\*/);
        if (boldMatch?.[1]) {
            return boldMatch[1].trim();
        }
        const plainMatch = text.match(/الخطوة التالية المقترحة:\s*هل تريد تنفيذ\s+(.+?)؟/);
        return plainMatch?.[1]?.trim() || "";
    }

    function extract_last_document_name(content) {
        const matches = Array.from(String(content || "").matchAll(/`([^`]+)`/g));
        if (!matches.length) return "";
        return matches[matches.length - 1][1] || "";
    }

    function build_continue_action_label(message) {
        const prompt = normalize_arabic(message?.user_prompt || "");
        const content = normalize_arabic(message?.content || "");
        const source = `${prompt} ${content}`;

        if (content.includes("المستند: **item**") || content.includes("المستند:item")) {
            if (source.includes("فاتوره شراء") || source.includes("فاتوره مشتريات")) return __("أكمل إنشاء فاتورة الشراء");
            if (source.includes("فاتوره بيع")) return __("أكمل إنشاء فاتورة البيع");
            return __("أكمل تجهيز الصنف التالي");
        }
        if (content.includes("المستند: **supplier**") || content.includes("المستند:supplier")) {
            return source.includes("فاتوره") ? __("أكمل إنشاء فاتورة المورد") : __("أكمل بعد إضافة المورد");
        }
        if (content.includes("المستند: **customer**") || content.includes("المستند:customer")) {
            return source.includes("فاتوره") ? __("أكمل إنشاء فاتورة العميل") : __("أكمل بعد إضافة العميل");
        }
        if (source.includes("فاتوره شراء")) return __("أكمل إنشاء فاتورة الشراء");
        if (source.includes("فاتوره بيع")) return __("أكمل إنشاء فاتورة البيع");
        if (source.includes("سند قبض")) return __("أكمل إنشاء سند القبض");
        if (source.includes("سند دفع")) return __("أكمل إنشاء سند الدفع");
        if (source.includes("امر شراء") || source.includes("طلب شراء")) return __("أكمل إنشاء طلب الشراء");
        if (source.includes("امر بيع") || source.includes("طلب بيع")) return __("أكمل إنشاء طلب البيع");
        if (source.includes("عميل") && source.includes("فاتوره")) return __("أكمل إنشاء فاتورة العميل");
        if (source.includes("مورد") && source.includes("فاتوره")) return __("أكمل إنشاء فاتورة المورد");
        if (source.includes("صنف") || source.includes("اصناف")) return __("أكمل تجهيز الأصناف");

        const summary = truncate((message?.user_prompt || "").replace(/\s+/g, " ").trim(), 42);
        return summary ? __("أكمل تنفيذ: {0}", [summary]) : __("أكمل تنفيذ الطلب");
    }

    function normalize_arabic(value) {
        return String(value || "")
            .toLowerCase()
            .replace(/[أإآ]/g, "ا")
            .replace(/ة/g, "ه")
            .replace(/ى/g, "ي")
            .replace(/\s+/g, " ")
            .trim();
    }

    function render_artifacts(artifacts) {
        if (!artifacts.length) return "";
        return `
            <div class="artifact-list">
                ${artifacts.map(render_artifact).join("")}
            </div>
        `;
    }

    function render_artifact(artifact) {
        const columns = artifact.columns || [];
        const rows = artifact.rows || [];
        const hasMore = Boolean(artifact.has_more || artifact.is_truncated);
        const table = columns.length
            ? `
                <div class="artifact-table-wrap">
                    <table class="artifact-table">
                        <thead>
                            <tr>${columns.map((column) => `<th>${escape_html(column)}</th>`).join("")}</tr>
                        </thead>
                        <tbody>
                            ${rows.map((row) => `
                                <tr>
                                    ${columns.map((column) => `<td>${escape_html(format_cell(row?.[column]))}</td>`).join("")}
                                </tr>
                            `).join("")}
                        </tbody>
                    </table>
                </div>
            `
            : "";
        return `
            <section class="artifact-card" data-artifact="${escape_attr(artifact.name)}">
                <div class="artifact-head">
                    <div>
                        <div class="artifact-title">${escape_html(artifact.title || __("Generated report"))}</div>
                        <div class="artifact-meta">
                            ${escape_html(artifact.artifact_type || "")}
                            ${artifact.row_count ? ` · ${artifact.row_count} ${__("rows")}` : ""}
                            ${hasMore ? ` · ${__("preview")}: ${artifact.preview_row_count || rows.length}` : ""}
                            ${artifact.is_truncated ? ` · ${__("truncated")}` : ""}
                        </div>
                    </div>
                    <div class="artifact-actions">
                        ${hasMore ? `<button class="btn btn-xs btn-default btn-load-artifact" data-artifact="${escape_attr(artifact.name)}">${__("عرض كل البيانات")}</button>` : ""}
                        <button class="btn btn-xs btn-default btn-export-artifact" data-artifact="${escape_attr(artifact.name)}">${__("تصدير CSV")}</button>
                    </div>
                </div>
                ${table}
            </section>
        `;
    }

    function load_full_artifact(artifactName) {
        if (!artifactName) return Promise.resolve(null);
        const card = page.main.find(`.artifact-card[data-artifact="${css_escape(artifactName)}"]`);
        card.addClass("loading");
        return frappe.xcall("ai_agent.api.get_artifact_content", { artifact_name: artifactName })
            .then((artifact) => {
                const html = render_artifact_table(artifact.columns || [], artifact.rows || []);
                const target = page.main.find(`.artifact-card[data-artifact="${css_escape(artifactName)}"]`);
                target.find(".artifact-table-wrap").replaceWith(html);
                target.find(".btn-load-artifact").remove();
                target.find(".artifact-meta").text(`${artifact.artifact_type || ""} · ${artifact.row_count || 0} ${__("rows")}${artifact.is_truncated ? ` · ${__("truncated")}` : ""}`);
                target.data("full-loaded", true);
                return artifact;
            })
            .finally(() => card.removeClass("loading"));
    }

    function render_artifact_table(columns, rows) {
        return `
            <div class="artifact-table-wrap">
                <table class="artifact-table">
                    <thead>
                        <tr>${columns.map((column) => `<th>${escape_html(column)}</th>`).join("")}</tr>
                    </thead>
                    <tbody>
                        ${(rows || []).map((row) => `
                            <tr>
                                ${columns.map((column) => `<td>${escape_html(format_cell(row?.[column]))}</td>`).join("")}
                            </tr>
                        `).join("")}
                    </tbody>
                </table>
            </div>
        `;
    }

    function export_artifact_csv(artifactName) {
        if (!artifactName) return;
        frappe.xcall("ai_agent.api.get_artifact_content", { artifact_name: artifactName }).then((artifact) => {
            const columns = artifact.columns || [];
            const rows = artifact.rows || [];
            const csv = [
                columns.map(csv_cell).join(","),
                ...rows.map((row) => columns.map((column) => csv_cell(row?.[column])).join(",")),
            ].join("\n");
            const blob = new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" });
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = `${(artifact.title || "artifact").replace(/[\\/:*?"<>|]+/g, "-")}.csv`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(link.href);
        });
    }

    function csv_cell(value) {
        const text = format_cell(value).replace(/"/g, '""');
        return `"${text}"`;
    }

    async function open_print_dialog(runName, title) {
        const letterHeads = await load_print_letter_heads();
        const noneLabel = __("بدون ترويسة");
        const savedLetterHead = localStorage.getItem("ai_agent_print_letter_head") || "";
        const defaultLetterHead = letterHeads.find((item) => item.name === savedLetterHead)?.name
            || letterHeads.find((item) => item.is_default)?.name
            || noneLabel;

        const dialog = new frappe.ui.Dialog({
            title: __("خيارات الطباعة"),
            fields: [
                {
                    fieldtype: "Select",
                    fieldname: "letter_head",
                    label: __("الترويسة"),
                    options: [noneLabel, ...letterHeads.map((item) => item.name)].join("\n"),
                    default: defaultLetterHead,
                },
                {
                    fieldtype: "Check",
                    fieldname: "include_footer",
                    label: __("إظهار ذيل الترويسة"),
                    default: 1,
                },
            ],
            primary_action_label: __("Print"),
            primary_action(values) {
                const selectedName = values.letter_head === noneLabel ? "" : values.letter_head;
                localStorage.setItem("ai_agent_print_letter_head", selectedName);
                dialog.hide();
                print_message(runName, title, {
                    letterHead: letterHeads.find((item) => item.name === selectedName),
                    includeFooter: Boolean(values.include_footer),
                });
            },
        });
        dialog.show();
    }

    function load_print_letter_heads() {
        if (printLetterHeads) return Promise.resolve(printLetterHeads);
        return frappe.xcall("ai_agent.api.get_print_letter_heads").then((result) => {
            printLetterHeads = result?.letter_heads || [];
            return printLetterHeads;
        });
    }

    async function print_message(runName, title, options = {}) {
        const article = page.main.find(`.assistant-message[data-run="${css_escape(runName)}"]`).get(0);
        if (!article) return;

        await expand_artifacts_for_print(article);

        const content = article.querySelector(".message-content")?.cloneNode(true);
        const artifacts = article.querySelector(".artifact-list")?.cloneNode(true);
        if (!content && !artifacts) return;
        const cleanContent = content ? clean_print_content(content) : null;
        const cleanTitle = escape_html(title || extract_title_from_print_content(cleanContent) || __("Report"));

        const printWindow = window.open("", "_blank", "width=1100,height=800");
        if (!printWindow) {
            frappe.msgprint(__("Please allow popups to print this result."));
            return;
        }

        const direction = document.documentElement.dir || "rtl";
        const letterHeadHtml = prepare_print_html_fragment(options.letterHead?.content || "");
        const letterFooterHtml = options.includeFooter
            ? prepare_print_html_fragment(options.letterHead?.footer || "")
            : "";
        printWindow.document.open();
        printWindow.document.write(`
            <!doctype html>
            <html dir="${escape_attr(direction)}">
            <head>
                <meta charset="utf-8">
                <base href="${escape_attr(window.location.origin)}/">
                <title>${cleanTitle}</title>
                <style>
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Tahoma, Arial, sans-serif;
                        color: #111827;
                        margin: 28px;
                        line-height: 1.65;
                    }
                    h1 {
                        font-size: 20px;
                        margin: 0 0 18px;
                        padding-bottom: 12px;
                        border-bottom: 1px solid #e5e7eb;
                    }
                    .message-content > h1:first-child,
                    .message-content > h2:first-child,
                    .message-content > h3:first-child {
                        text-align: center;
                    }
                    .message-content > h1:first-child + p,
                    .message-content > h1:first-child + p + p,
                    .message-content > h1:first-child + p + p + p,
                    .message-content > h2:first-child + p,
                    .message-content > h2:first-child + p + p,
                    .message-content > h2:first-child + p + p + p {
                        text-align: center;
                    }
                    table {
                        width: 100%;
                        border-collapse: collapse;
                        margin: 12px 0;
                        font-size: 12px;
                    }
                    th, td {
                        border: 1px solid #d1d5db;
                        padding: 8px 10px;
                        vertical-align: top;
                    }
                    th {
                        background: #f3f4f6;
                        font-weight: 700;
                    }
                    .artifact-card {
                        border: 1px solid #e5e7eb;
                        border-radius: 8px;
                        margin-top: 14px;
                        overflow: hidden;
                    }
                    .artifact-head {
                        padding: 10px 12px;
                        background: #f9fafb;
                        border-bottom: 1px solid #e5e7eb;
                    }
                    .artifact-title { font-weight: 700; }
                    .artifact-meta { color: #6b7280; font-size: 12px; }
                    .artifact-actions { display: none; }
                    .artifact-table-wrap {
                        max-height: none;
                        overflow: visible;
                    }
                    code {
                        background: #f3f4f6;
                        border-radius: 4px;
                        padding: 1px 4px;
                    }
                    .print-letter-head {
                        margin-bottom: 18px;
                    }
                    .print-letter-head img,
                    .print-letter-footer img {
                        max-width: 100%;
                        height: auto;
                    }
                    .print-letter-footer {
                        margin-top: 24px;
                        padding-top: 12px;
                        border-top: 1px solid #e5e7eb;
                    }
                    @page { margin: 14mm; }
                </style>
            </head>
            <body>
                ${letterHeadHtml ? `<div class="print-letter-head">${letterHeadHtml}</div>` : ""}
                ${cleanContent ? cleanContent.outerHTML : ""}
                ${artifacts ? artifacts.outerHTML : ""}
                ${letterFooterHtml ? `<div class="print-letter-footer">${letterFooterHtml}</div>` : ""}
                <script>
                    function waitForImages() {
                        const images = Array.from(document.images || []);
                        if (!images.length) return Promise.resolve();
                        return Promise.all(images.map((img) => {
                            if (img.complete) return Promise.resolve();
                            return new Promise((resolve) => {
                                img.onload = resolve;
                                img.onerror = resolve;
                                setTimeout(resolve, 2500);
                            });
                        }));
                    }
                    window.onload = async () => {
                        await waitForImages();
                        window.focus();
                        window.print();
                    };
                <\/script>
            </body>
            </html>
        `);
        printWindow.document.close();
    }

    async function expand_artifacts_for_print(article) {
        const buttons = Array.from(article.querySelectorAll(".btn-load-artifact"));
        if (!buttons.length) return;
        await Promise.all(buttons.map((button) => load_full_artifact(button.dataset.artifact)));
    }

    function prepare_print_html_fragment(html) {
        if (!html) return "";
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html;

        wrapper.querySelectorAll("[src]").forEach((element) => {
            const source = element.getAttribute("src");
            if (!source || /^(?:data:|blob:|https?:|\/\/)/i.test(source)) return;
            element.setAttribute("src", new URL(source, window.location.origin).href);
        });

        wrapper.querySelectorAll("img").forEach((image) => {
            const style = (image.getAttribute("style") || "")
                .replace(/(?:^|;)\s*(?:height|width)\s*:\s*px\s*;?/gi, ";")
                .trim();
            image.setAttribute("style", style);
            image.setAttribute("loading", "eager");
        });

        return wrapper.innerHTML;
    }

    function clean_print_content(content) {
        const clone = content.cloneNode(true);
        remove_print_chatter(clone);
        return clone;
    }

    function remove_print_chatter(container) {
        const firstDataNode = find_first_print_data_node(container);
        if (!firstDataNode) return;

        let node = container.firstChild;
        while (node && node !== firstDataNode) {
            const next = node.nextSibling;
            node.remove();
            node = next;
        }
    }

    function find_first_print_data_node(container) {
        const children = Array.from(container.children || []);
        return children.find(is_print_report_start_node) || container.querySelector("table");
    }

    function is_print_report_start_node(node) {
        const tag = node.tagName;
        const text = normalize_arabic(node.textContent || "");
        if (!text && tag !== "TABLE") return false;
        if (tag === "HR") return false;
        if (is_print_chatter(text)) return false;
        if (tag === "TABLE") return true;
        if (/^H[1-6]$/.test(tag) && is_data_heading(text)) return true;
        if (node.querySelector?.("h1, h2, h3, h4, h5, h6") && is_data_heading(text)) return true;
        if (is_report_filter_text(text)) return true;
        if (["P", "DIV", "SECTION"].includes(tag) && text.length < 180 && is_data_heading(text)) return true;
        return false;
    }

    function is_print_chatter(text) {
        return [
            "ساقدم", "سوف اقدم", "الان لدي", "دعوني", "لدي جميع البيانات",
            "تم تجهيز", "here is", "i will", "now i have",
        ].some((marker) => text.startsWith(marker));
    }

    function is_report_filter_text(text) {
        return [
            "الفتره", "الشركه", "الشركات", "الشركتان", "العمله",
            "period", "company", "currency", "filters",
        ].some((marker) => text.includes(marker));
    }

    function is_data_heading(text) {
        return [
            "كشف", "تقرير", "تحليل", "اجماليات", "الاجماليات",
            "جدول", "بيانات", "ملخص", "نتائج", "ارصده", "الرصيد",
            "report", "analysis", "summary", "results",
        ].some((marker) => text.includes(marker));
    }

    function extract_title_from_print_content(content) {
        if (!content) return "";
        const heading = content.querySelector("h1, h2, h3");
        return (heading?.textContent || "").trim();
    }

    function extract_print_title(message) {
        const temp = document.createElement("div");
        temp.innerHTML = safe_markdown(message.content || "");
        const cleaned = clean_print_content(temp);
        return extract_title_from_print_content(cleaned) || message.print_title || "";
    }

    function load_approvals_for_run(runName) {
        if (!runName) return;
        frappe.xcall("ai_agent.api.get_pending_approvals", { run_name: runName }).then((result) => {
            const target = page.main.find("[data-run-approvals]").filter(function () {
                return $(this).data("run-approvals") === runName;
            });
            if (!target.length) return;
            const approvals = result?.approvals || [];
            target.toggle(Boolean(approvals.length));
            target.html(approvals.map(render_approval).join(""));
        });
    }

    function render_approval(approval) {
        return `
            <div class="approval-card-inline">
                <div>
                    <div class="approval-title">${escape_html(approval.action_summary || __("Approval required"))}</div>
                    <div class="approval-meta">
                        ${escape_html(approval.action_type || "")}
                        ${approval.doctype_name ? ` · ${escape_html(approval.doctype_name)}` : ""}
                        ${approval.document_name ? ` · ${escape_html(approval.document_name)}` : ""}
                    </div>
                </div>
                <div class="approval-actions">
                    <button class="btn btn-xs btn-default btn-reject-approval" data-approval="${escape_attr(approval.name)}">${__("Reject")}</button>
                    <button class="btn btn-xs btn-primary btn-approve-approval" data-approval="${escape_attr(approval.name)}">${__("Approve & Execute")}</button>
                </div>
            </div>
        `;
    }

    function approve_approval(approvalName) {
        if (!approvalName) return;
        frappe.confirm(__("Approve and execute this action?"), () => {
            frappe.xcall("ai_agent.api.approve_action", { approval_name: approvalName }).then((result) => {
                const documentName = result?.result?.document_name;
                frappe.show_alert({
                    message: documentName ? __("Executed: {0}", [documentName]) : __("Action executed."),
                    indicator: "green",
                });
                select_session(currentRoot || currentRun);
                load_sessions();
            }).catch((error) => {
                frappe.msgprint({ message: error?.message || __("Approval failed."), indicator: "red" });
            });
        });
    }

    function reject_approval(approvalName) {
        if (!approvalName) return;
        frappe.prompt([{
            fieldtype: "Small Text",
            fieldname: "reason",
            label: __("Reason"),
        }], (values) => {
            frappe.xcall("ai_agent.api.reject_action", {
                approval_name: approvalName,
                reason: values.reason,
            }).then(() => {
                frappe.show_alert({ message: __("Approval rejected."), indicator: "orange" });
                select_session(currentRoot || currentRun);
                load_sessions();
            }).catch((error) => {
                frappe.msgprint({ message: error?.message || __("Reject failed."), indicator: "red" });
            });
        }, __("Reject Approval"), __("Reject"));
    }

    function load_steps_for_run(runName) {
        if (!runName) return;
        const shouldStick = should_stick_to_bottom();
        frappe.xcall("ai_agent.api.get_run_steps", { run_name: runName }).then((result) => {
            const target = page.main.find("[data-run-steps]").filter(function () {
                return $(this).data("run-steps") === runName;
            });
            if (!target.length) return;
            const steps = result?.steps || [];
            update_work_log_meta(runName, steps);
            if (!steps.length) {
                target.html(`<div class="work-step muted">${__("No steps recorded yet.")}</div>`);
                if (shouldStick) scroll_to_bottom(true);
                return;
            }
            target.html(steps.map(render_step).join(""));
            if (shouldStick) scroll_to_bottom(true);
        });
    }

    function update_work_log_meta(runName, steps) {
        const meta = page.main.find("[data-run-step-meta]").filter(function () {
            return $(this).data("run-step-meta") === runName;
        });
        if (!meta.length) return;
        const completed = (steps || []).filter((step) => step.status === "Completed").length;
        const failed = (steps || []).filter((step) => step.status === "Failed").length;
        const tools = (steps || []).filter((step) => step.step_type === "Tool").length;
        const label = failed
            ? `${steps.length} ${__("steps")} · ${failed} ${__("failed")}`
            : `${steps.length} ${__("steps")} · ${tools} ${__("tools")}`;
        meta.text(label);
        meta.toggleClass("has-error", Boolean(failed));
        meta.toggleClass("is-complete", Boolean(steps.length && completed === steps.length));
    }

    function render_step(step) {
        const statusClass = (step.status || "").toLowerCase();
        const summary = step.summary ? safe_markdown(step.summary) : "";
        return `
            <div class="work-step ${statusClass}">
                <div class="work-step-head">
                    <span class="work-step-icon">${step.step_type === "Plan" ? "P" : step.step_type === "Tool" ? "T" : step.step_type === "Final" ? "F" : "S"}</span>
                    <strong>${escape_html(step.title || step.step_type)}</strong>
                    <span>${escape_html(step.status || "")}</span>
                    ${step.duration_ms ? `<span>${step.duration_ms}ms</span>` : ""}
                </div>
                ${summary ? `<div class="work-step-summary markdown-body">${summary}</div>` : ""}
            </div>
        `;
    }

    async function submit_message() {
        const promptInput = page.main.find("#prompt-input");
        const prompt = promptInput.val().trim();
        if (!prompt && !selectedFiles.length) return;
        if (!selectedAgent) {
            frappe.msgprint(__("Select an agent first."));
            return;
        }
        const finalPrompt = prompt || __("Analyze the attached files.");
        submit_prompt(finalPrompt, selectedFiles, { clearComposer: true });
    }

    async function submit_prompt(finalPrompt, files, options = {}) {
        set_busy(true);
        append_pending_user_message(finalPrompt, files);
        let attachments = [];
        try {
            attachments = await read_selected_files(files);
        } catch (error) {
            set_busy(false);
            frappe.msgprint({ message: error?.message || __("Could not read attached files."), indicator: "red" });
            return;
        }
        frappe.xcall("ai_agent.api.create_run", {
            agent: selectedAgent,
            prompt: finalPrompt,
            company: selectedCompany,
            parent_run: currentRun,
            attachments,
        }).then((result) => {
            if (!result?.run_name) return;
            currentRun = result.run_name;
            currentRoot = currentRoot || result.run_name;
            if (options.clearComposer) {
                page.main.find("#prompt-input").val("");
                selectedFiles = [];
                render_selected_files();
            }
            autosize_prompt();
            const status = result.status || "Queued";
            const initialExtra = String(status).toLowerCase() === "queued"
                ? format_run_status_extra({
                    status: "Queued",
                    current_step: 0,
                    tool_call_count: 0,
                    execution_status: result.execution_status,
                })
                : result.run_name;
            update_status(status, initialExtra);
            load_sessions();
            select_session(currentRoot).then(() => {
                scroll_to_bottom(true);
                if (["completed", "already_exists"].includes(String(result.status || "").toLowerCase())) {
                    refresh_current_run();
                } else {
                    start_polling();
                }
            });
        }).catch((error) => {
            set_busy(false);
            frappe.msgprint({ message: error?.message || __("Failed to create run."), indicator: "red" });
            select_session(currentRoot);
        });
    }

    function submit_suggested_message(prompt) {
        const value = String(prompt || "").trim();
        if (!value || page.main.find("#btn-send").prop("disabled")) return;
        submit_prompt(value, [], { clearComposer: false });
    }

    function append_pending_user_message(prompt, files) {
        const panel = page.main.find("#chat-messages");
        panel.find(".chat-empty-state").remove();
        panel.append(render_user_message({
            content: prompt,
            run_name: `draft-${Date.now()}`,
            attachments: (files || []).map((file) => ({
                file_name: file.name,
                file_size: file.size,
            })),
        }));
        scroll_to_bottom(true);
    }

    function handle_file_select(event) {
        const files = Array.from(event.target.files || []);
        const allowedExtensions = [".csv", ".json", ".txt", ".md", ".log", ".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp"];
        for (const file of files) {
            const extension = `.${(file.name.split(".").pop() || "").toLowerCase()}`;
            if (!allowedExtensions.includes(extension)) {
                frappe.show_alert({ message: __("Unsupported file type: {0}", [file.name]), indicator: "red" });
                continue;
            }
            if (file.size > 5 * 1024 * 1024) {
                frappe.show_alert({ message: __("File is too large: {0}", [file.name]), indicator: "red" });
                continue;
            }
            if (selectedFiles.length >= 5) {
                frappe.show_alert({ message: __("You can attach up to 5 files."), indicator: "orange" });
                break;
            }
            selectedFiles.push(file);
        }
        page.main.find("#file-input").val("");
        render_selected_files();
        scroll_to_bottom(true);
    }

    function render_selected_files() {
        const target = page.main.find("#attached-files");
        target.toggle(Boolean(selectedFiles.length));
        target.html(selectedFiles.map((file, index) => `
            <span class="attached-file-pill">
                <span>📎 ${escape_html(file.name)}</span>
                <span class="file-size">${format_file_size(file.size)}</span>
                <button type="button" class="btn-remove-file" data-index="${index}" aria-label="${__("Remove")}">×</button>
            </span>
        `).join(""));
    }

    function remove_selected_file(index) {
        selectedFiles.splice(index, 1);
        render_selected_files();
    }

    function read_selected_files(files = selectedFiles) {
        return Promise.all((files || []).map((file) => new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const result = String(reader.result || "");
                resolve({
                    filename: file.name,
                    size: file.size,
                    content_type: file.type,
                    content_base64: result.split(",", 2)[1] || "",
                });
            };
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(file);
        })));
    }

    function refresh_current_run() {
        if (!currentRun) return;
        frappe.xcall("ai_agent.api.get_run", { run_name: currentRun }).then((run) => {
            if (!run) return;
            update_status(run.status, format_run_status_extra(run));
            load_steps_for_run(currentRun);
            if (["Completed", "Failed", "Cancelled", "Timed Out"].includes(run.status)) {
                finish_current_run();
            }
        });
    }

    function finish_current_run() {
        stop_polling();
        set_busy(false);
        page.main.find("#btn-cancel").hide();
        select_session(currentRoot || currentRun).then(() => scroll_to_bottom(true));
        load_sessions();
    }

    function start_polling_if_needed() {
        if (!currentRun) return;
        frappe.xcall("ai_agent.api.get_run", { run_name: currentRun }).then((run) => {
            if (run) {
                update_status(run.status, format_run_status_extra(run));
            }
            if (run && !["Completed", "Failed", "Cancelled", "Timed Out"].includes(run.status)) {
                set_busy(true);
                start_polling();
            } else {
                set_busy(false);
            }
        });
    }

    function start_polling() {
        stop_polling();
        pollInterval = setInterval(refresh_current_run, 2000);
    }

    function stop_polling() {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    }

    function cancel_run() {
        if (!currentRun) return;
        frappe.xcall("ai_agent.api.cancel_run", { run_name: currentRun }).then(() => {
            update_status("Cancelling", currentRun);
            stop_polling();
            set_busy(false);
        });
    }

    function new_chat() {
        stop_polling();
        currentRoot = null;
        currentRun = null;
        set_busy(false);
        page.main.find("#chat-title").text(__("New chat"));
        page.main.find("#run-status").empty();
        page.main.find("#prompt-input").val("");
        selectedFiles = [];
        render_selected_files();
        page.main.find("#chat-messages").html(`
            <div class="chat-empty-state">
                <h3>${__("Ask an ERP question")}</h3>
                <p>${__("Simple questions answer instantly. Deeper analysis keeps a readable work log.")}</p>
            </div>
        `);
        page.main.find(".chat-session").removeClass("active");
    }

    function update_status(status, extra) {
        if (!status && !extra) {
            page.main.find("#run-status").empty();
            return;
        }
        page.main.find("#run-status").html(`
            <span class="status-pill ${(status || "").toLowerCase().replace(/\s+/g, "-")}">${escape_html(status || "")}</span>
            <span>${escape_html(extra || "")}</span>
        `);
    }

    function format_run_status_extra(run) {
        const usage = run?.token_usage || {};
        const parts = [
            `${__("Steps")}: ${run?.current_step || 0}`,
            `${__("Tools")}: ${run?.tool_call_count || 0}`,
        ];
        if (run?.status === "Queued") {
            const execution = run.execution_status || {};
            parts.unshift(execution.message || __("يعمل الآن في الخلفية داخل التطبيق"));
        }
        if (usage.total_tokens || usage.remaining_context_tokens) {
            parts.push(`${__("Tokens")}: ${format_tokens(usage.total_tokens)} used`);
            parts.push(`${format_tokens(usage.remaining_context_tokens)} remaining est.`);
        }
        return parts.join(" · ");
    }

    function render_token_usage(usage) {
        if (!usage || (!usage.total_tokens && !usage.remaining_context_tokens)) return "";
        const title = [
            `${__("Input")}: ${format_tokens(usage.input_tokens)}`,
            `${__("Output")}: ${format_tokens(usage.output_tokens)}`,
            `${__("Context")}: ${format_tokens(usage.context_window_tokens)}`,
            usage.is_estimated ? __("Remaining is estimated") : "",
        ].filter(Boolean).join(" · ");
        return `
            <span class="token-usage-pill" title="${escape_attr(title)}">
                ${__("Tokens")}: ${format_tokens(usage.total_tokens)}
                <span class="token-remaining">· ${format_tokens(usage.remaining_context_tokens)} ${__("left")}</span>
            </span>
        `;
    }

    function update_title_from_messages(messages) {
        const firstUser = messages.find((message) => message.role === "user");
        page.main.find("#chat-title").text(firstUser ? truncate(firstUser.content, 70) : __("Chat"));
    }

    function set_busy(isBusy) {
        page.main.find("#btn-send").prop("disabled", isBusy);
        page.main.find("#btn-cancel").toggle(Boolean(isBusy));
    }

    function toggle_voice_input() {
        if (isListening) {
            stop_voice_input();
            return;
        }
        start_voice_input();
    }

    function start_voice_input() {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            frappe.show_alert({
                message: __("Voice input is not supported in this browser. Use Chrome/Edge over HTTPS."),
                indicator: "orange",
            });
            return;
        }

        speechRecognition = new SpeechRecognition();
        speechRecognition.lang = get_voice_language();
        speechRecognition.continuous = false;
        speechRecognition.interimResults = true;

        let finalTranscript = "";
        voiceBasePrompt = page.main.find("#prompt-input").val() || "";
        set_voice_listening(true);

        speechRecognition.onresult = (event) => {
            let interimTranscript = "";
            for (let index = event.resultIndex; index < event.results.length; index++) {
                const transcript = event.results[index][0]?.transcript || "";
                if (event.results[index].isFinal) {
                    finalTranscript += transcript;
                } else {
                    interimTranscript += transcript;
                }
            }
            update_prompt_from_voice(finalTranscript || interimTranscript);
        };

        speechRecognition.onerror = (event) => {
            set_voice_listening(false);
            const message = event?.error === "not-allowed"
                ? __("Microphone permission was denied.")
                : __("Voice input stopped: {0}", [event?.error || __("unknown error")]);
            frappe.show_alert({ message, indicator: "orange" });
        };

        speechRecognition.onend = () => {
            set_voice_listening(false);
        };

        try {
            speechRecognition.start();
        } catch (error) {
            set_voice_listening(false);
        }
    }

    function stop_voice_input() {
        if (speechRecognition) {
            speechRecognition.stop();
        }
        set_voice_listening(false);
    }

    function set_voice_listening(value) {
        isListening = Boolean(value);
        const button = page.main.find("#btn-voice");
        button.toggleClass("listening", isListening);
        button.attr("title", isListening ? __("Stop voice input") : __("Dictate with voice"));
        button.attr("aria-label", isListening ? __("Stop voice input") : __("Dictate with voice"));
        button.text(isListening ? "⏹️" : "🎙️");
        if (!isListening) {
            voiceBasePrompt = "";
        }
    }

    function update_prompt_from_voice(transcript) {
        const input = page.main.find("#prompt-input");
        const cleanTranscript = (transcript || "").trim();
        if (!cleanTranscript) return;

        const separator = voiceBasePrompt && !/\s$/.test(voiceBasePrompt) ? " " : "";
        input.val(`${voiceBasePrompt}${separator}${cleanTranscript}`);
        autosize_prompt();
        input.trigger("focus");
    }

    function get_voice_language() {
        const language = (frappe.boot?.lang || document.documentElement.lang || "").toLowerCase();
        if (language.startsWith("ar")) return "ar-SA";
        if (language.startsWith("en")) return "en-US";
        return "ar-SA";
    }

    function autosize_prompt() {
        const el = page.main.find("#prompt-input").get(0);
        if (!el) return;
        el.style.height = "auto";
        el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
        update_layout_height();
    }

    function scroll_to_bottom(force) {
        const panel = page.main.find("#chat-messages").get(0);
        if (!panel) return;
        const distanceFromBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight;
        if (force || distanceFromBottom < 180) {
            requestAnimationFrame(() => {
                panel.scrollTop = panel.scrollHeight;
            });
        }
    }

    function should_stick_to_bottom() {
        const panel = page.main.find("#chat-messages").get(0);
        if (!panel) return true;
        const distanceFromBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight;
        return distanceFromBottom < 260;
    }

    function truncate(text, length) {
        text = text || "";
        return text.length <= length ? text : `${text.slice(0, length - 1).trim()}…`;
    }

    function safe_markdown(value) {
        try {
            if (frappe.markdown) {
                return frappe.markdown(value || "");
            }
            if (frappe.utils && frappe.utils.markdown) {
                return frappe.utils.markdown(value || "");
            }
            return `<div class="markdown-fallback">${escape_html(value || "")}</div>`;
        } catch (error) {
            return `<div class="markdown-fallback">${escape_html(value || "")}</div>`;
        }
    }

    function escape_html(value) {
        return frappe.utils.escape_html(value || "");
    }

    function escape_attr(value) {
        return escape_html(value).replace(/"/g, "&quot;");
    }

    function css_escape(value) {
        if (window.CSS && typeof window.CSS.escape === "function") {
            return window.CSS.escape(String(value || ""));
        }
        return String(value || "").replace(/["\\]/g, "\\$&");
    }

    function format_cell(value) {
        if (value === null || value === undefined) return "";
        if (typeof value === "number") return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
        return String(value);
    }

    function format_file_size(size) {
        const value = Number(size || 0);
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
        return `${(value / 1024 / 1024).toFixed(1)} MB`;
    }

    function format_tokens(value) {
        const number = Number(value || 0);
        if (number >= 1000000) return `${(number / 1000000).toFixed(1)}M`;
        if (number >= 1000) return `${(number / 1000).toFixed(1)}K`;
        return number.toLocaleString();
    }

};

frappe.pages["agent-workspace"].on_page_show = function () {
    inject_layout_fix();
    setTimeout(() => $(window).trigger("resize.ai_agent_workspace"), 50);
};

function inject_layout_fix() {
    $("#ai-agent-layout-fix").remove();
    $("head").append(`
        <style id="ai-agent-layout-fix">
            .agent-workspace-main {
                min-height: 540px !important;
                overflow: hidden !important;
                padding: 0 !important;
            }
            .agent-chat-shell {
                height: 100% !important;
                min-height: 0 !important;
                overflow: hidden !important;
            }
            .agent-chat-main {
                height: 100% !important;
                min-height: 0 !important;
                display: flex !important;
                flex-direction: column !important;
                overflow: hidden !important;
            }
            .chat-messages {
                flex: 1 1 auto !important;
                min-height: 0 !important;
                overflow-y: auto !important;
                padding: 22px !important;
            }
            .chat-composer {
                flex: 0 0 auto !important;
                position: relative !important;
                top: auto !important;
                right: auto !important;
                bottom: auto !important;
                left: auto !important;
                z-index: 5 !important;
                border-radius: 0 !important;
                border-width: 1px 0 0 !important;
            }
        </style>
    `);
}
