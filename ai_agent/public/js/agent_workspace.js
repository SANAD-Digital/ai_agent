// AI Agent Workspace
frappe.provide("ai_agent");

ai_agent.WorkspacePage = class WorkspacePage {
    constructor() {
        this.agent = null;
        this.company = null;
        this.current_run = null;
        this.run_poll_interval = null;
        this.init();
    }

    init() {
        this.render();
        this.bind_events();
        this.load_agents();
    }

    render() {
        this.page = frappe.ui.make_page({
            title: __("Agent Workspace"),
            single_column: false,
        });

        this.page.main.addClass("agent-workspace");

        // Header bar
        this.page.main.append(`
            <div class="header-bar">
                <div class="agent-selector">
                    <label>${__("Agent")}</label>
                    <div class="agent-dropdown"></div>
                </div>
                <div class="company-selector">
                    <label>${__("Company")}</label>
                    <div class="company-dropdown"></div>
                </div>
                <div class="connection-status">
                    <span class="indicator-pill green">${__("Connected")}</span>
                </div>
                <button class="btn btn-primary btn-new-run">${__("New Run")}</button>
            </div>
        `);

        // Agent dropdown
        this.agent_dropdown = frappe.ui.form.make_control({
            parent: this.page.main.find(".agent-dropdown"),
            df: {
                fieldtype: "Link",
                options: "AI Agent",
                fieldname: "agent",
                label: __("Select Agent"),
                only_select: true,
                onchange: () => {
                    this.agent = this.agent_dropdown.get_value();
                    this.load_skills();
                },
            },
            render_input: true,
        });

        // Company dropdown
        this.company_dropdown = frappe.ui.form.make_control({
            parent: this.page.main.find(".company-dropdown"),
            df: {
                fieldtype: "Link",
                options: "Company",
                fieldname: "company",
                label: __("Company"),
                only_select: true,
                onchange: () => {
                    this.company = this.company_dropdown.get_value();
                },
            },
            render_input: true,
        });

        // Composer area
        this.page.main.append(`
            <div class="composer-area">
                <textarea class="prompt-input" placeholder="${__("Describe your task...")}"></textarea>
                <div class="composer-actions">
                    <button class="btn btn-default btn-cancel-run" style="display:none">${__("Cancel")}</button>
                    <button class="btn btn-primary btn-submit-run">${__("Run Analysis")}</button>
                </div>
            </div>
        `);

        // Skills bar
        this.page.main.append('<div class="skills-bar"></div>');

        // Run status
        this.page.main.append('<div class="run-status" style="display:none"></div>');

        // Timeline
        this.page.main.append('<div class="timeline" style="display:none"></div>');

        // Approval card area
        this.page.main.append('<div class="approval-area"></div>');

        // Artifacts tabs
        this.page.main.append(`
            <div class="artifacts-tabs" style="display:none">
                <div class="tab-header">
                    <button class="tab-btn active" data-tab="result">${__("Result")}</button>
                    <button class="tab-btn" data-tab="tables">${__("Tables")}</button>
                    <button class="tab-btn" data-tab="charts">${__("Charts")}</button>
                    <button class="tab-btn" data-tab="documents">${__("Documents")}</button>
                </div>
                <div class="tab-content">
                    <div class="tab-pane active" data-tab="result">
                        <div class="result-text"></div>
                    </div>
                    <div class="tab-pane" data-tab="tables">
                        <div class="result-table"></div>
                    </div>
                    <div class="tab-pane" data-tab="charts">
                        <div class="chart-container"></div>
                    </div>
                    <div class="tab-pane" data-tab="documents">
                        <div class="doc-links"></div>
                    </div>
                </div>
            </div>
        `);
    }

    bind_events() {
        const me = this;

        this.page.main.find(".btn-submit-run").on("click", () => me.submit_run());
        this.page.main.find(".btn-cancel-run").on("click", () => me.cancel_run());
        this.page.main.find(".btn-new-run").on("click", () => me.reset_composer());

        this.page.main.find(".tab-btn").on("click", function () {
            me.page.main.find(".tab-btn").removeClass("active");
            me.page.main.find(".tab-pane").removeClass("active");
            $(this).addClass("active");
            me.page.main.find(`.tab-pane[data-tab="${$(this).data("tab")}"]`).addClass("active");
        });

        // Realtime events
        frappe.realtime.on("ai_agent:run_updated", (data) => {
            if (me.current_run && data.run_id === me.current_run) {
                me.refresh_run_status();
            }
        });

        frappe.realtime.on("ai_agent:step_added", (data) => {
            if (me.current_run && data.run_id === me.current_run) {
                me.load_steps();
            }
        });

        frappe.realtime.on("ai_agent:approval_required", (data) => {
            if (me.current_run && data.run_id === me.current_run) {
                me.show_approval_card(data);
            }
        });

        frappe.realtime.on("ai_agent:run_completed", (data) => {
            if (me.current_run && data.run_id === me.current_run) {
                me.on_run_completed();
            }
        });

        frappe.realtime.on("ai_agent:run_failed", (data) => {
            if (me.current_run && data.run_id === me.current_run) {
                me.on_run_failed();
            }
        });
    }

    async load_agents() {
        const result = await frappe.xcall("ai_agent.api.list_available_agents");
        if (result && result.agents) {
            this.agents = result.agents;
        }
    }

    async load_skills() {
        if (!this.agent) return;
        const result = await frappe.xcall("ai_agent.api.list_available_skills", {
            agent: this.agent,
            company: this.company,
        });
        if (result && result.skills) {
            this.render_skills(result.skills);
        }
    }

    render_skills(skills) {
        const bar = this.page.main.find(".skills-bar");
        bar.empty();
        skills.forEach((skill) => {
            bar.append(`<span class="skill-tag" data-skill="${skill.name}" title="${skill.description || ""}">${skill.title || skill.name}</span>`);
        });
    }

    async submit_run() {
        const prompt = this.page.main.find(".prompt-input").val().trim();
        if (!prompt) {
            frappe.msgprint(__("Please enter a task description."));
            return;
        }
        if (!this.agent) {
            frappe.msgprint(__("Please select an agent."));
            return;
        }

        frappe.dom.freeze(__("Submitting run..."));

        try {
            const result = await frappe.xcall("ai_agent.api.create_run", {
                agent: this.agent,
                prompt: prompt,
                company: this.company,
            });

            if (result && result.run_name) {
                this.current_run = result.run_name;
                this.show_run_status();
                this.page.main.find(".btn-cancel-run").show();
                this.page.main.find(".btn-submit-run").prop("disabled", true);
                this.start_polling();
            }
        } catch (e) {
            let msg = (e && e.message) || __("Failed to create run.");
            frappe.msgprint({ message: msg, indicator: "red" });
        } finally {
            frappe.dom.unfreeze();
        }
    }

    async cancel_run() {
        if (!this.current_run) return;
        await frappe.xcall("ai_agent.api.cancel_run", { run_name: this.current_run });
        this.stop_polling();
    }

    reset_composer() {
        this.current_run = null;
        this.page.main.find(".prompt-input").val("");
        this.page.main.find(".btn-cancel-run").hide();
        this.page.main.find(".btn-submit-run").prop("disabled", false);
        this.page.main.find(".run-status").hide();
        this.page.main.find(".timeline").hide();
        this.page.main.find(".artifacts-tabs").hide();
        this.page.main.find(".approval-area").empty();
        this.stop_polling();
    }

    show_run_status() {
        this.page.main.find(".run-status").show();
        this.refresh_run_status();
    }

    async refresh_run_status() {
        if (!this.current_run) return;
        const result = await frappe.xcall("ai_agent.api.get_run", { run_name: this.current_run });
        if (!result) return;

        const status_class = result.status.toLowerCase().replace(/\s+/g, "-");
        this.page.main.find(".run-status").html(`
            <div style="display:flex; align-items:center; justify-content:space-between;">
                <div>
                    <span class="status-badge ${status_class}">${__(result.status)}</span>
                    <span style="margin-left:var(--padding-sm); color:var(--text-muted);">
                        ${__("Steps")}: ${result.current_step || 0} | ${__("Tool Calls")}: ${result.tool_call_count || 0}
                    </span>
                </div>
                <span style="color:var(--text-muted);">${result.run_id || result.name}</span>
            </div>
        `);

        if (result.plan_summary) {
            this.page.main.find(".run-status").append(`
                <div style="margin-top:var(--padding-sm); padding:var(--padding-sm); background:var(--bg-light-gray); border-radius:var(--border-radius-sm);">
                    <strong>${__("Plan")}:</strong> ${frappe.utils.escape_html(result.plan_summary)}
                </div>
            `);
        }

        if (result.status === "Completed" || result.status === "Failed") {
            this.stop_polling();
            this.load_steps();
            this.load_artifacts();
        }

        if (result.status === "Waiting Approval") {
            this.load_pending_approvals();
        }
    }

    async load_steps() {
        if (!this.current_run) return;
        const result = await frappe.xcall("ai_agent.api.get_run_steps", { run_name: this.current_run });
        if (!result || !result.steps) return;

        const timeline = this.page.main.find(".timeline");
        timeline.show();
        timeline.html(`<h5>${__("Activity Timeline")}</h5>`);

        result.steps.forEach((step) => {
            const icon_color = step.status === "Completed" ? "green" : step.status === "Failed" ? "red" : "blue";
            const summary = step.summary ? frappe.utils.escape_html(step.summary).substring(0, 1200) : "";
            timeline.append(`
                <div class="step">
                    <div class="step-icon" style="background:var(--${icon_color}-100); color:var(--${icon_color}-600);">
                        ${step.step_type === "Plan" ? "P" : step.step_type === "Tool" ? "T" : step.step_type === "Final" ? "F" : "S"}
                    </div>
                    <div>
                        <div><strong>${frappe.utils.escape_html(step.title || step.step_type)}</strong></div>
                        ${summary ? `<div style="color:var(--text-muted); font-size:var(--text-sm); white-space:pre-wrap; line-height:1.45; max-width:100%;">${summary}</div>` : ""}
                        ${step.duration_ms ? `<div style="color:var(--text-muted); font-size:var(--text-xs);">${step.duration_ms}ms</div>` : ""}
                    </div>
                </div>
            `);
        });
    }

    async load_artifacts() {
        if (!this.current_run) return;
        const run = await frappe.xcall("ai_agent.api.get_run", { run_name: this.current_run });
        if (run && run.final_answer) {
            this.page.main.find(".result-text").html(frappe.utils.markdown(run.final_answer));
            this.page.main.find(".artifacts-tabs").show();
        }

        if (run && run.error_summary) {
            this.page.main.find(".result-text").html(`<div class="text-danger">${frappe.utils.escape_html(run.error_summary)}</div>`);
            this.page.main.find(".artifacts-tabs").show();
        }
    }

    show_approval_card(data) {
        const area = this.page.main.find(".approval-area");
        const card_id = `approval-${Date.now()}`;
        area.append(`
            <div class="approval-card" id="${card_id}">
                <h5>${__("Approval Required")}</h5>
                <p><strong>${frappe.utils.escape_html(data.action_summary || "")}</strong></p>
                <div class="approval-actions">
                    <button class="btn btn-primary btn-approve" data-approval="${data.approval_id}">${__("Approve")}</button>
                    <button class="btn btn-danger btn-reject" data-approval="${data.approval_id}">${__("Reject")}</button>
                </div>
            </div>
        `);

        area.find(`#${card_id} .btn-approve`).on("click", async function () {
            const approval_id = $(this).data("approval");
            await frappe.xcall("ai_agent.api.approve_action", { approval_name: approval_id });
            $(`#${card_id}`).remove();
        });

        area.find(`#${card_id} .btn-reject`).on("click", async function () {
            const approval_id = $(this).data("approval");
            await frappe.xcall("ai_agent.api.reject_action", { approval_name: approval_id });
            $(`#${card_id}`).remove();
        });
    }

    async load_pending_approvals() {
        if (!this.current_run) return;
        const result = await frappe.xcall("ai_agent.api.get_pending_approvals", { run_name: this.current_run });
        if (result && result.approvals) {
            result.approvals.forEach((a) => {
                this.show_approval_card({ approval_id: a.name, action_summary: a.action_summary });
            });
        }
    }

    on_run_completed() {
        this.page.main.find(".btn-cancel-run").hide();
        this.page.main.find(".btn-submit-run").prop("disabled", false);
        this.load_steps();
        this.load_artifacts();
    }

    on_run_failed() {
        this.page.main.find(".btn-cancel-run").hide();
        this.page.main.find(".btn-submit-run").prop("disabled", false);
        this.refresh_run_status();
    }

    start_polling() {
        this.stop_polling();
        this.run_poll_interval = setInterval(() => this.refresh_run_status(), 3000);
    }

    stop_polling() {
        if (this.run_poll_interval) {
            clearInterval(this.run_poll_interval);
            this.run_poll_interval = null;
        }
    }
};

frappe.pages["agent-workspace"].on_page_load = function (wrapper) {
    frappe.agent_workspace = new ai_agent.WorkspacePage();
};
