frappe.pages["purchase-invoice-ai"].on_page_load = function (wrapper) {
    frappe.require("/assets/ai_agent/css/purchase_invoice_ai.css");
    $(wrapper).addClass("purchase-invoice-ai-page");
    frappe.ui.make_app_page({
        parent: wrapper,
        title: __("تحليل المرفقات للقيود"),
        single_column: true,
    });

    const page = wrapper.page;
    page.main.addClass("purchase-invoice-ai-main");
    page.main.html(`
        <div class="pi-ai-shell" dir="auto">
            <main class="pi-ai-workspace">
                <aside class="pi-files-pane">
                    <div class="pi-pane-head">
                        <b>${__("الملفات والصفحات")}</b>
                        <button class="btn btn-xs btn-default" data-action="open_upload">${__("إضافة ملفات")}</button>
                    </div>
                    <div class="pi-file-list" data-file-list></div>
                </aside>

                <section class="pi-results-pane">
                    <nav class="pi-stage-tabs" data-stepper>
                        <button class="active" data-step-nav="files">
                            <b>1</b><span>${__("الملفات")}</span>
                        </button>
                        <button data-step-nav="extract">
                            <b>2</b><span>${__("قراءة OCR والمراجعة")}</span>
                        </button>
                    </nav>
                    <div class="pi-stage-chat" data-stage-chat></div>
                </section>

                <section class="pi-preview-pane">
                    <div class="pi-pane-head">
                        <b>${__("المعاينة")}</b>
                        <span data-preview-title>${__("لا يوجد ملف محدد")}</span>
                    </div>
                    <div class="pi-preview" data-preview>
                        <div class="pi-empty">${__("اختر صفحة للمعاينة")}</div>
                    </div>
                </section>
            </main>

            <div class="pi-upload-modal hidden" data-upload-modal>
                <div class="pi-upload-dialog">
                    <header>
                        <div>
                            <b>${__("إضافة ملفات")}</b>
                            <span>${__("ارفع الملفات ثم جهّزها كشجرة صفحات للمراجعة")}</span>
                        </div>
                        <button class="btn btn-xs btn-default" data-action="close_upload">×</button>
                    </header>
                    <div class="pi-upload-config">
                        <div id="pi-company-control"></div>
                        <div class="pi-field">
                            <label>${__("طريقة التجميع")}</label>
                            <select class="form-control" data-field="grouping_mode">
                                <option value="auto">${__("يحددها النظام تلقائيًا")}</option>
                                <option value="single">${__("كل الملفات ضمن قيد واحد")}</option>
                                <option value="separate">${__("كل ملف/مستند قيد مستقل")}</option>
                            </select>
                        </div>
                        <div class="pi-field">
                            <label>${__("تعليمات إضافية")}</label>
                            <textarea class="form-control" data-field="instructions" rows="3" placeholder="${__("مثال: اعتبر الأسعار شاملة الضريبة، واستخدم المستودع الرئيسي")}"></textarea>
                        </div>
                    </div>
                    <div class="pi-dropzone" data-dropzone>
                        <div>${__("اسحب الملفات هنا أو الصق من الحافظة")}</div>
                        <small data-pi-limits></small>
                    </div>
                    <div class="pi-upload-file-list" data-upload-file-list></div>
                    <div class="pi-upload-actions">
                        <label class="btn btn-default btn-sm">
                            ${__("اختيار ملفات")}
                            <input type="file" data-field="files" multiple hidden>
                        </label>
                        <button class="btn btn-primary btn-sm" data-action="extract">${__("تجهيز الصفحات")}</button>
                    </div>
                </div>
            </div>
        </div>
    `);

    let boot = null;
    let companyControl = null;
    let files = [];
    let currentRun = null;
    let extractionRun = null;
    let analysisStage = "new";
    let activeStep = "files";
    let poll = null;
    let analysis = null;
    let lastRun = {};
    let lastSteps = [];
    let discussionMessages = [];
    let extractionMessage = "";
    let activeFileIndex = -1;
    let sessionName = "";
    let pageManifest = [];
    let selectedPages = new Set();
    let activePageId = "";
    let activeFileId = "";
    let pagePreviewCache = {};
    let pagePreviewLoading = new Set();
    let collapsedFileIds = new Set();
    let allowOpenWithMissingAccounts = false;
    let uiContract = {};
    let lastActiveStep = null;
    let workspaceVersion = 0;

    init();

    function init() {
        bind_events();
        page.set_secondary_action(__("جلسة جديدة"), function () {
            if (files.length || pageManifest.length || sessionName) {
                frappe.confirm(__("هل أنت تأكد من بدء جلسة جديدة وتصفية الملفات والصفحات الحالية؟"), function () {
                    reset_workspace();
                });
            } else {
                reset_workspace();
            }
        }, "fa fa-plus");
        frappe.xcall("ai_agent.purchase_invoice_ai.api.bootstrap").then((data) => {
            boot = data || {};
            uiContract = boot.ui_contract || {};
            apply_ui_contract();
            page.main.find("[data-pi-limits]").text(
                `${__("Allowed")}: ${(boot.allowed_extensions || []).join(", ")} · ${boot.max_file_mb || 15}MB`
            );
            companyControl = frappe.ui.form.make_control({
                parent: page.main.find("#pi-company-control"),
                df: {
                    fieldtype: "Link",
                    fieldname: "company",
                    label: __("الشركة"),
                    options: "Company",
                    reqd: 1,
                },
                render_input: true,
            });
            if ((boot.companies || []).length === 1) {
                companyControl.set_value(boot.companies[0].name);
            }
            const routeRun = route_run_name();
            const routeSession = (frappe.route_options?.session || frappe.route_options?.session_name || "").trim();
            const savedSession = routeSession || localStorage.getItem("financial_attachment_session");
            if (routeRun) load_run(routeRun);
            else if (savedSession) load_session(savedSession);
            if (frappe.route_options) {
                delete frappe.route_options.run;
                delete frappe.route_options.run_name;
                delete frappe.route_options.session;
                delete frappe.route_options.session_name;
            }
        });
        render_all();
    }

    wrapper.on_page_show = function () {
        const routeRun = route_run_name();
        if (routeRun && routeRun !== currentRun) {
            load_run(routeRun);
            if (frappe.route_options) {
                delete frappe.route_options.run;
                delete frappe.route_options.run_name;
            }
            return;
        }
        const routeSession = (frappe.route_options?.session || frappe.route_options?.session_name || "").trim();
        if (routeSession) {
            load_session(routeSession);
            delete frappe.route_options.session;
            delete frappe.route_options.session_name;
        }
    };

    function route_run_name() {
        const fromOptions = String(frappe.route_options?.run || frappe.route_options?.run_name || "").trim();
        if (fromOptions) return fromOptions;
        const route = frappe.get_route ? frappe.get_route() : [];
        const candidate = String(route?.[1] || "").trim();
        return /^AI-RUN-\d+/i.test(candidate) ? candidate : "";
    }

    function apply_ui_contract() {
        const stages = contract_stages();
        const actions = uiContract.actions || {};
        page.set_title(contract_text("page_title", __("تحليل المرفقات")));
        page.main.find("[data-action='new']").text(actions.new || __("عملية جديدة"));
        page.main.find("[data-action='extract']").text(actions.extract || __("استخراج البيانات"));
        page.main.find("[data-action='reextract']").text(actions.reextract || __("إعادة الاستخراج"));
        page.main.find("[data-step-nav]").each(function () {
            const stage = stages.find((row) => row.key === $(this).data("step-nav")) || {};
            $(this).find("span").text(stage.label || $(this).find("span").text());
        });
    }

    function bind_events() {
        page.main.on("click", "[data-action='new']", reset_workspace);
        page.main.on("click", "[data-action='open_upload']", open_upload_modal);
        page.main.on("click", "[data-action='close_upload']", close_upload_modal);
        page.main.on("click", "[data-action='extract'], [data-action='reextract']", start_extraction);
        page.main.on("click", "[data-action='read_selected_pages']", read_selected_pages);
        page.main.on("click", "[data-action='cancel_current_run']", cancel_current_run);
        page.main.on("click", "[data-action='open_agent_workspace']", open_agent_workspace);
        page.main.on("click", "[data-action='reprocess_failed_pages']", reprocess_failed_pages);
        page.main.on("change", "[data-field='files']", (event) => add_files(Array.from(event.target.files || [])));
        page.main.on("click", ".pi-file-row", function () {
            activeFileIndex = Number($(this).data("index"));
            activePageId = "";
            render_files();
            render_preview();
        });
        page.main.on("click", "[data-pending-file-index]", function () {
            activeFileIndex = Number($(this).data("pending-file-index"));
            render_files();
            render_preview();
        });
        page.main.on("click", ".pi-page-file-title", function (event) {
            event.stopPropagation();
            activeFileId = String($(this).data("file-id") || "");
            activePageId = "";
            render_analysis();
            render_preview();
        });
        page.main.on("click", ".pi-page-node, .pi-extraction-page-btn", function (event) {
            if ($(event.target).is("input")) return;
            activePageId = String($(this).data("page-id") || "");
            activeFileId = "";

            const treeScroll = page.main.find(".pi-page-tree").scrollTop() || 0;
            const listScroll = page.main.find(".pi-extraction-page-list").scrollTop() || 0;

            page.main.find(".pi-page-node, .pi-extraction-page-btn").removeClass("active");
            page.main.find(`[data-page-id="${activePageId}"]`).addClass("active");

            render_preview();

            if (activeStep === "extract") {
                page.main.find(".pi-current-page-indicator").html(
                    `<b>${escape_html(current_active_page_label())}</b> (${current_page_index_label()})`
                );
                page.main.find("[data-action='prev_page']").prop("disabled", is_first_page());
                page.main.find("[data-action='next_page']").prop("disabled", is_last_page());
            }

            if (treeScroll) page.main.find(".pi-page-tree").scrollTop(treeScroll);
            if (listScroll) page.main.find(".pi-extraction-page-list").scrollTop(listScroll);
        });
        page.main.on("click", "[data-action='prev_page']", function () {
            const index = pageManifest.findIndex((row) => row.page_id === activePageId);
            if (index > 0) {
                activePageId = pageManifest[index - 1].page_id;
                activeFileId = "";

                page.main.find(".pi-extraction-page-btn").removeClass("active");
                page.main.find(`[data-page-id="${activePageId}"]`).addClass("active");

                render_preview();
                page.main.find(".pi-current-page-indicator").html(
                    `<b>${escape_html(current_active_page_label())}</b> (${current_page_index_label()})`
                );
                page.main.find("[data-action='prev_page']").prop("disabled", is_first_page());
                page.main.find("[data-action='next_page']").prop("disabled", is_last_page());

                const activeBtn = page.main.find(`.pi-extraction-page-btn[data-page-id="${activePageId}"]`);
                if (activeBtn.length) {
                    activeBtn[0].scrollIntoView({ block: "nearest", behavior: "smooth" });
                }
            }
        });
        page.main.on("click", "[data-action='next_page']", function () {
            const index = pageManifest.findIndex((row) => row.page_id === activePageId);
            if (index >= 0 && index < pageManifest.length - 1) {
                activePageId = pageManifest[index + 1].page_id;
                activeFileId = "";

                page.main.find(".pi-extraction-page-btn").removeClass("active");
                page.main.find(`[data-page-id="${activePageId}"]`).addClass("active");

                render_preview();
                page.main.find(".pi-current-page-indicator").html(
                    `<b>${escape_html(current_active_page_label())}</b> (${current_page_index_label()})`
                );
                page.main.find("[data-action='prev_page']").prop("disabled", is_first_page());
                page.main.find("[data-action='next_page']").prop("disabled", is_last_page());

                const activeBtn = page.main.find(`.pi-extraction-page-btn[data-page-id="${activePageId}"]`);
                if (activeBtn.length) {
                    activeBtn[0].scrollIntoView({ block: "nearest", behavior: "smooth" });
                }
            }
        });
        page.main.on("click", ".pi-page-file-toggle", function (event) {
            event.stopPropagation();
            const fileId = String($(this).data("file-id") || "");
            if (!fileId) return;
            if (collapsedFileIds.has(fileId)) collapsedFileIds.delete(fileId);
            else collapsedFileIds.add(fileId);
            const parent = $(this).closest(".pi-page-file");
            parent.toggleClass("collapsed", collapsedFileIds.has(fileId));
            $(this).find("i").attr("class", `fa ${collapsedFileIds.has(fileId) ? "fa-chevron-left" : "fa-chevron-down"}`);
        });
        page.main.on("change", ".pi-page-select", function (event) {
            event.stopPropagation();
            const pageId = String($(this).data("page-id") || "");
            if (!pageId) return;
            if (this.checked) selectedPages.add(pageId);
            else selectedPages.delete(pageId);
            const row = pageManifest.find((item) => item.page_id === pageId);
            if (row) row.selected = this.checked ? 1 : 0;
            persist_selected_pages();

            page.main.find(".pi-selected-badge b").first().text(selectedPages.size);
            const readBtn = page.main.find("[data-action='read_selected_pages']");
            readBtn.prop("disabled", !selectedPages.size);
            if (selectedPages.size) {
                readBtn.text(`${__("قراءة الصفحات المختارة")} (${selectedPages.size})`);
            } else {
                readBtn.text(__("قراءة الصفحات المختارة"));
            }
        });
        page.main.on("click", "[data-page-bulk]", function () {
            const mode = $(this).data("page-bulk");
            const treeScroll = page.main.find(".pi-page-tree").scrollTop() || 0;
            selectedPages = new Set(mode === "all" ? pageManifest.filter((row) => row.can_read).map((row) => row.page_id) : []);
            pageManifest.forEach((row) => {
                row.selected = selectedPages.has(row.page_id) ? 1 : 0;
            });
            persist_selected_pages();

            page.main.find(".pi-page-select").prop("checked", mode === "all");
            page.main.find(".pi-selected-badge b").first().text(selectedPages.size);
            const readBtn = page.main.find("[data-action='read_selected_pages']");
            readBtn.prop("disabled", !selectedPages.size);
            if (selectedPages.size) {
                readBtn.text(`${__("قراءة الصفحات المختارة")} (${selectedPages.size})`);
            } else {
                readBtn.text(__("قراءة الصفحات المختارة"));
            }

            if (treeScroll) page.main.find(".pi-page-tree").scrollTop(treeScroll);
        });
        page.main.on("click", ".pi-file-remove", function (event) {
            event.stopPropagation();
            files.splice(Number($(this).data("index")), 1);
            if (activeFileIndex >= files.length) activeFileIndex = files.length - 1;
            render_all();
        });
        page.main.on("click", ".pi-tabs button", function () {
            activate_tab($(this).data("tab"));
        });
        page.main.on("click", "[data-step-nav]", function () {
            activeStep = $(this).data("step-nav");
            render_analysis();
        });
        page.main.find("[data-dropzone]").on("dragover", (event) => {
            event.preventDefault();
            page.main.find("[data-dropzone]").addClass("dragging");
        });
        page.main.find("[data-dropzone]").on("dragleave drop", (event) => {
            event.preventDefault();
            page.main.find("[data-dropzone]").removeClass("dragging");
        });
        page.main.find("[data-dropzone]").on("drop", (event) => {
            add_files(Array.from(event.originalEvent.dataTransfer.files || []));
        });
        $(document).on("paste.purchase-invoice-ai", (event) => {
            const pasted = Array.from(event.originalEvent.clipboardData?.files || []);
            if (pasted.length) add_files(pasted);
        });
        frappe.realtime.on("ai_agent:run_updated", (data) => {
            if (currentRun && data.run_id === currentRun) refresh_run();
        });
        frappe.realtime.on("ai_agent:step_added", (data) => {
            if (currentRun && data.run_id === currentRun) refresh_run();
        });
        frappe.realtime.on("ai_agent:page_reading_updated", (data) => {
            apply_realtime_page_reading_update(data || {});
        });
    }

    function add_files(newFiles) {
        allowOpenWithMissingAccounts = false;
        files.push(...newFiles.map((file, index) => ({
            id: `${Date.now()}-${index}-${file.name}`,
            file,
            name: file.name,
            size: file.size,
            type: file.type || extension_of(file.name),
            upload_status: __("Ready"),
            analysis_status: __("New"),
            role: __("غير محدد"),
            group: "GROUP-001",
        })));
        if (activeFileIndex < 0 && files.length) activeFileIndex = 0;
        render_all();
    }

    function open_upload_modal() {
        page.main.find("[data-upload-modal]").removeClass("hidden");
        render_upload_files();
    }

    function close_upload_modal() {
        page.main.find("[data-upload-modal]").addClass("hidden");
    }

    function next_workspace_version() {
        workspaceVersion += 1;
        return workspaceVersion;
    }

    function is_current_workspace(version) {
        return version === workspaceVersion;
    }

    function load_session(name) {
        const requestVersion = next_workspace_version();
        frappe.xcall("ai_agent.purchase_invoice_ai.api.get_session", { session_name: name }).then((result) => {
            if (!is_current_workspace(requestVersion)) return;
            apply_session(result);
            render_all();
        }).catch(() => {
            if (!is_current_workspace(requestVersion)) return;
            localStorage.removeItem("financial_attachment_session");
        });
    }

    function load_run(name) {
        const requestVersion = next_workspace_version();
        set_busy(true, __("Loading"));
        frappe.xcall("ai_agent.purchase_invoice_ai.api.get_analysis", { run_name: name }).then((result) => {
            if (!is_current_workspace(requestVersion)) return;
            const run = result.run || {};
            currentRun = run.name || name;
            extractionRun = run.parent_run || currentRun;
            lastRun = run;
            lastSteps = result.steps || [];
            analysis = normalize_analysis(result.analysis);
            apply_session_pages(result.session || {});
            const stage = analysis_workflow_stage();
            analysisStage = stage === "page_preparation"
                ? "pages_ready"
                : (analysis_has_data(analysis) ? "extracted" : "extraction_failed");
            activeStep = "extract";
            update_status(run.status || __("Loaded"));
            set_busy(false, run.status || __("Loaded"));
            render_all();
            render_log(lastSteps, run);
            if (run.status === "Running") start_polling();
        }).catch((error) => {
            if (!is_current_workspace(requestVersion)) return;
            set_busy(false, __("Failed"));
            frappe.msgprint(error_message(error, __("تعذر تحميل عملية التحليل.")));
        });
    }

    function apply_session(result) {
        const session = result.session || {};
        sessionName = session.name || "";
        if (sessionName) localStorage.setItem("financial_attachment_session", sessionName);
        currentRun = result.run_name || session.source_run || currentRun;
        extractionRun = currentRun;
        analysis = normalize_analysis(result.analysis);
        pageManifest = (session.pages || []).map((row) => ({
            ...row,
            page_id: row.page_id,
            file_id: row.file_id,
            file_name: row.file_name,
            page: row.page,
            page_type: row.page_type,
            status: row.status,
            can_read: row.can_read,
            selected: row.selected,
            text_preview: row.text_preview || "",
            metadata: row.metadata || {},
            warnings: row.warnings || [],
            analysis_status: row.analysis_status || "",
            analysis_run: row.analysis_run || "",
            analysis: row.analysis || {},
            preview_data_url: row.preview_data_url || "",
        }));
        collapsedFileIds = new Set();
        selectedPages = new Set(pageManifest.filter((row) => row.selected).map((row) => row.page_id));
        activePageId = session.current_page_id || activePageId || pageManifest[0]?.page_id || "";
        files = (session.files || []).map((row) => ({
            id: row.file_id,
            saved_file: { name: row.file_id, file_name: row.file_name, file_size: row.file_size },
            name: row.file_name,
            size: row.file_size,
            type: row.extension,
            upload_status: __("Uploaded"),
            analysis_status: session.status || __("Pages Ready"),
            role: __("مرفق"),
            group: "GROUP-001",
        }));
        analysisStage = session.status === "Pages Ready" ? "pages_ready" : session.status === "Reading" ? "extracting" : "extracted";
        extractionMessage = session.status === "Pages Ready"
            ? __("تم تحميل جلسة محفوظة. اختر الصفحات التي تريد قراءتها.")
            : __("تم تحميل جلسة محفوظة.");
        if (companyControl && session.company) companyControl.set_value(session.company);
        if (session.status === "Reading" && currentRun) start_polling();
    }

    function apply_session_pages(session) {
        if (!session || !session.name) return;
        sessionName = session.name || sessionName;
        if (sessionName) localStorage.setItem("financial_attachment_session", sessionName);
        if (!extractionRun && session.source_run) extractionRun = session.source_run;
        const incoming = (session.pages || []).map((row) => ({
            ...row,
            page_id: row.page_id,
            file_id: row.file_id,
            file_name: row.file_name,
            page: row.page,
            page_type: row.page_type,
            status: row.status,
            can_read: row.can_read,
            selected: row.selected,
            text_preview: row.text_preview || "",
            metadata: row.metadata || {},
            warnings: row.warnings || [],
            analysis_status: row.analysis_status || "",
            analysis_run: row.analysis_run || "",
            analysis: row.analysis || {},
            preview_data_url: row.preview_data_url || "",
        }));
        if (incoming.length) {
            const existingById = new Map(pageManifest.map((row) => [row.page_id, row]));
            pageManifest = incoming.map((row) => ({
                ...(existingById.get(row.page_id) || {}),
                ...row,
            }));
            selectedPages = new Set(pageManifest.filter((row) => row.selected).map((row) => row.page_id));
            activePageId = activePageId || session.current_page_id || pageManifest[0]?.page_id || "";
        }
        if (!files.length && (session.files || []).length) {
            files = (session.files || []).map((row) => ({
                id: row.file_id,
                saved_file: { name: row.file_id, file_name: row.file_name, file_size: row.file_size },
                name: row.file_name,
                size: row.file_size,
                type: row.extension,
                upload_status: __("Uploaded"),
                analysis_status: session.status || __("Pages Ready"),
                role: __("مرفق"),
                group: "GROUP-001",
            }));
        }
    }

    function persist_selected_pages() {
        if (!sessionName) return;
        const pages = pageManifest
            .filter((row) => selectedPages.has(row.page_id))
            .map((row) => ({ page_id: row.page_id }));
        frappe.xcall("ai_agent.purchase_invoice_ai.api.set_selected_pages", {
            session_name: sessionName,
            pages: JSON.stringify(pages),
        }).catch(() => { });
    }

    async function start_extraction() {
        if (!companyControl?.get_value()) {
            frappe.msgprint(__("اختر الشركة أولًا."));
            return;
        }
        const uploadFiles = files.filter((row) => row.file);
        if (!uploadFiles.length) {
            frappe.msgprint(__("أضف مرفقًا واحدًا على الأقل."));
            return;
        }
        const requestVersion = next_workspace_version();
        stop_polling();
        currentRun = null;
        extractionRun = null;
        sessionName = "";
        pageManifest = [];
        selectedPages = new Set();
        activePageId = "";
        activeFileId = "";
        pagePreviewCache = {};
        pagePreviewLoading = new Set();
        collapsedFileIds = new Set();
        localStorage.removeItem("financial_attachment_session");
        set_busy(true, __("Preparing"));
        analysisStage = "extracting";
        activeStep = "extract";
        extractionMessage = __("أجهّز الملفات ثم أفككها إلى صفحات للمعاينة والاختيار.");
        files = files.map((row) => row.file ? ({
            ...row,
            upload_status: __("Uploading"),
            analysis_status: __("Waiting"),
        }) : row);
        render_all();
        try {
            const encoded = await Promise.all(uploadFiles.map(file_to_payload));
            if (!is_current_workspace(requestVersion)) return;
            const result = await frappe.xcall("ai_agent.purchase_invoice_ai.api.start_extraction", {
                company: companyControl.get_value(),
                files: JSON.stringify(encoded),
                grouping_mode: page.main.find("[data-field='grouping_mode']").val(),
                user_instructions: page.main.find("[data-field='instructions']").val(),
                defaults: JSON.stringify({}),
            });
            if (!is_current_workspace(requestVersion)) return;
            if (!result || !result.run_name) {
                throw new Error(__("لم يرجع السيرفر رقم عملية الاستخراج."));
            }
            currentRun = result.run_name;
            extractionRun = result.run_name;
            sessionName = result.session_name || "";
            if (sessionName) localStorage.setItem("financial_attachment_session", sessionName);
            if (result.status === "pages_ready") {
                pageManifest = result.pages || [];
                collapsedFileIds = new Set();
                selectedPages = new Set(pageManifest.filter((row) => row.can_read).map((row) => row.page_id));
                activePageId = pageManifest[0]?.page_id || "";
                analysis = normalize_analysis(result.analysis);
                analysisStage = "pages_ready";
                activeStep = "extract";
                extractionMessage = result.message || __("تم تجهيز الصفحات للمعاينة والاختيار.");
                let savedIndex = 0;
                files = files.map((row) => {
                    if (!row.file) return row;
                    const saved = (result.files || [])[savedIndex++];
                    return {
                        ...row,
                        file: null,
                        upload_status: __("Uploaded"),
                        saved_file: saved,
                        analysis_status: __("Pages Ready"),
                    };
                });
                page.main.find("[data-action='reextract']").prop("disabled", false);
                set_busy(false, __("Pages Ready"));
                close_upload_modal();
                render_all();
                return;
            }
            if (result.reused) {
                frappe.show_alert({
                    message: result.message || __("تم استخدام تحليل محفوظ لنفس الملفات والإعدادات."),
                    indicator: "green",
                });
            }
            const hasData = analysis_has_data(result.analysis);
            analysisStage = hasData ? "extracted" : "extraction_failed";
            extractionMessage = result.message || "";
            activeStep = "extract";
            files = files.map((row, index) => ({
                ...row,
                upload_status: __("Uploaded"),
                saved_file: (result.files || [])[index],
                analysis_status: hasData ? (result.reused ? __("Reused") : __("Extracted")) : __("No data"),
            }));
            analysis = normalize_analysis(result.analysis);
            apply_file_classification();
            render_analysis();
            render_log([], {
                status: result.reused ? __("Reused") : __("Extracted"),
                tool_call_count: 0,
                input_tokens: 0,
                output_tokens: 0,
            });
            page.main.find("[data-action='reextract']").prop("disabled", false);
            set_busy(false, hasData ? (result.reused ? __("Reused") : __("Extracted")) : __("No data"));
            render_all();
        } catch (error) {
            if (!is_current_workspace(requestVersion)) return;
            analysisStage = "extraction_failed";
            extractionMessage = error_message(error, __("تعذر استخراج البيانات."));
            files = files.map((row) => ({
                ...(row || {}),
                upload_status: row?.saved_file ? __("Uploaded") : __("Ready"),
                analysis_status: __("Failed"),
            }));
            set_busy(false, __("Failed"));
            render_all();
            frappe.msgprint(extractionMessage);
        }
    }

    function open_agent_workspace() {
        if (!sessionName) {
            frappe.msgprint(__("أكمل تجهيز الصفحات وقراءة OCR أولًا."));
            return;
        }
        const readablePages = pageManifest
            .filter((row) => selectedPages.has(row.page_id) && String(row.status || "").toLowerCase() === "read")
            .length;
        if (!readablePages) {
            frappe.msgprint(__("لا توجد صفحات OCR مقروءة لإرسالها إلى المعالجة."));
            return;
        }
        const selectedCompany = companyControl?.get_value() || "";
        const userInstructions = page.main.find("[data-field='instructions']").val() || "";
        const prompt = [
            "FINANCIAL_ATTACHMENT_TO_JOURNAL_ENTRY_WORKSPACE",
            `المهمة المحددة: اقرأ جلسة المرفقات المالية ${sessionName} واستخرج منها مقترحات قيود يومية قابلة للمراجعة.`,
            "لا تسأل عن المهمة؛ هذه هي المهمة المطلوبة لهذه الجلسة.",
            `الشركة: ${selectedCompany}`,
            `عدد الصفحات المقروءة الجاهزة: ${readablePages}`,
            `تعليمات المستخدم الأصلية: ${userInstructions || "لا يوجد"}`,
            "",
            `ابدأ باستدعاء financial_attachments.get_session_ocr_text مع session_name = ${sessionName}.`,
            "اعتبر OCR الراجع من الأداة مصدر الحقيقة، ولا تعيد تشغيل OCR ولا تطلب رفع الملفات طالما الجلسة جاهزة.",
            "بعد قراءة OCR، استخرج كل مستند مالي مستقل من كل الصفحات المقروءة، ولا تسقط أي صفحة أو مستند بدون تحذير واضح.",
            "استخدم financial_attachments.list_leaf_accounts عند الحاجة لاختيار حسابات ERP صحيحة، واستخدم حسابات leaf فقط وبالاسم الكامل كما يرجعها النظام.",
            "نتائج الأدوات والمهارات المساعدة خطوات وسيطة فقط، وليست نتيجة نهائية.",
            "",
            "المخرج النهائي يجب أن يكون JSON صالح فقط بدون Markdown أو شرح.",
            "استخدم عقد واجهة Purchase Invoice AI الحالي:",
            "- المفاتيح العليا: invoice_groups, questions, warnings, errors.",
            "- كل invoice_groups[] يمثل مستندًا ماليًا مستقلًا ويحتوي: invoice_group_id, document_meta, files, journal_entry_lines, questions, warnings, errors.",
            "- كل journal_entry_lines[] يحتوي: line_no, account, account_match_method, account_confidence, account_suggestions, party_type, party, supplier_action, debit, credit, description, custom_cash_supplier, custom_bill_no, custom_bill_date, custom_tax_id, custom_tax_amount, user_remark, cost_center, source_file, source_page, line_meta.",
            "يجب أن تكون سطور كل مجموعة متوازنة: مجموع debit يساوي مجموع credit. إذا كان الحساب غير معروف اترك account فارغًا مع warning، لكن لا تختر حسابًا عشوائيًا.",
        ].join("\n");
        frappe.set_route("agent-workspace", {
            agent: boot?.agent || "Purchase Invoice AI",
            company: selectedCompany,
            prompt,
            auto_submit: 1,
        });
    }

    function read_selected_pages() {
        if (!extractionRun || !pageManifest.length) {
            frappe.msgprint(__("جهّز الصفحات أولًا."));
            return;
        }
        const selected = pageManifest
            .filter((row) => selectedPages.has(row.page_id) && row.can_read)
            .map((row) => ({ file_id: row.file_id, page: row.page }));
        if (!selected.length) {
            frappe.msgprint(__("اختر صفحة واحدة على الأقل للقراءة."));
            return;
        }
        set_busy(true, __("Reading"));
        analysisStage = "extracting";
        activeStep = "extract";
        extractionMessage = __("أقرأ كل صفحة مختارة كعملية مستقلة، وستظهر النتائج تدريجيًا.");
        currentRun = extractionRun;
        const requestVersion = workspaceVersion;
        mark_selected_pages_reading();
        focus_first_reading_page();
        render_all();
        frappe.xcall("ai_agent.purchase_invoice_ai.api.read_selected_pages", {
            run_name: extractionRun,
            pages: JSON.stringify(selected),
        }).then((result) => {
            if (!is_current_workspace(requestVersion)) return;
            sessionName = result.session_name || sessionName;
            if (sessionName) localStorage.setItem("financial_attachment_session", sessionName);
            extractionMessage = result.message || __("بدأت قراءة كل صفحة مختارة كعملية مستقلة.");
            focus_first_reading_page();
            start_polling();
            render_all();
        }).catch((error) => {
            if (!is_current_workspace(requestVersion)) return;
            analysisStage = "pages_ready";
            set_busy(false, __("Failed"));
            frappe.msgprint(error_message(error, __("تعذر بدء قراءة الصفحات المختارة.")));
            render_all();
        });
    }

    function cancel_current_run() {
        const runName = currentRun || extractionRun;
        if (!runName) return;
        set_busy(true, __("Stopping"));
        frappe.xcall("ai_agent.purchase_invoice_ai.api.cancel_analysis", {
            run_name: runName,
        }).then(() => {
            extractionMessage = __("تم إرسال طلب إيقاف العملية. سيتم إيقاف الصفحة الحالية ثم تجاوز بقية الصفحات.");
            analysisStage = activeStep === "extract" ? "pages_ready" : analysisStage;
            set_busy(false, __("Cancelled"));
            refresh_run();
        }).catch((error) => {
            set_busy(false, __("Failed"));
            frappe.msgprint(error_message(error, __("تعذر إيقاف العملية.")));
        });
    }

    function mark_selected_pages_reading() {
        pageManifest = pageManifest.map((row) => {
            if (!selectedPages.has(row.page_id) || !row.can_read) return row;
            const status = String(row.status || "").toLowerCase();
            if (["read", "failed"].includes(status)) return row;
            return { ...row, status: "Selected", selected: 1 };
        });
    }

    function focus_first_reading_page() {
        const reading = pageManifest.find((row) => ["reading", "selected"].includes(String(row.status || "").toLowerCase()));
        if (reading) activePageId = reading.page_id;
    }

    function start_polling() {
        stop_polling();
        refresh_run();
        poll = setInterval(refresh_run, 1500);
    }

    function stop_polling() {
        if (poll) clearInterval(poll);
        poll = null;
    }

    function refresh_run() {
        if (!currentRun) return;
        const requestedRun = currentRun;
        const requestedSession = sessionName;
        frappe.xcall("ai_agent.purchase_invoice_ai.api.get_analysis", { run_name: requestedRun }).then((result) => {
            if (requestedRun !== currentRun) return;
            if (sessionName && ["extracting", "matching"].includes(analysisStage)) {
                frappe.xcall("ai_agent.purchase_invoice_ai.api.get_session", { session_name: requestedSession }).then((sessionData) => {
                    if (requestedRun !== currentRun || requestedSession !== sessionName) return;
                    update_session_progress_in_place(sessionData);
                });
            }
            const run = result.run || {};
            lastRun = run;
            update_status(run.status || __("Running"));
            apply_session_pages(result.session || {});
            render_log(result.steps || [], run);
            if (result.analysis) {
                analysis = normalize_analysis(result.analysis);
                sync_page_manifest_from_analysis();
                apply_file_classification();
                update_tree_page_statuses();
                if (analysisStage === "matching") {
                    render_analysis();
                }
            }
            if (["Completed", "Failed", "Cancelled", "Timed Out"].includes(run.status)) {
                stop_polling();
                set_busy(false, run.status);
                if (run.status === "Completed") {
                    if (currentRun === extractionRun || analysisStage === "extracting") {
                        const stage = analysis_workflow_stage();
                        const hasData = analysis_has_data(analysis);
                        if (stage === "page_preparation") {
                            analysisStage = "pages_ready";
                            extractionMessage = __("تم تجهيز الصفحات. اختر الصفحات المطلوبة للقراءة فقط.");
                        } else {
                            analysisStage = hasData ? "extracted" : "extraction_failed";
                            extractionMessage = hasData ? __("تم استخراج البيانات من الصفحات المختارة.") : (run.error_summary || extractionMessage);
                        }
                        activeStep = "extract";
                    } else {
                        if (analysis_has_rows(analysis)) {
                            analysisStage = "analyzed";
                            activeStep = "review";
                        } else {
                            analysisStage = "extracted";
                            activeStep = "process";
                            push_agent_chat_message([
                                `### ${__("المعالجة لم ترجع سطور قيد")}`,
                                "",
                                __("تمت قراءة المرفقات، لكن رد الوكيل لم يحتوي على سطور قيد قابلة للمراجعة."),
                                "",
                                __("أعد إرسالها للوكيل للمعالجة، أو اكتب توجيهًا أوضح في المحادثة مثل الحسابات المطلوبة أو طريقة التجميع."),
                            ].join("\n"), "process");
                        }
                    }
                    render_analysis();
                } else if (currentRun === extractionRun || analysisStage === "extracting") {
                    analysisStage = "extraction_failed";
                    extractionMessage = run.error_summary || __("تعذر استخراج البيانات من المرفقات.");
                    activeStep = "extract";
                    render_analysis();
                } else {
                    analysisStage = analysis_has_data(analysis) ? "extracted" : "extraction_failed";
                    activeStep = "process";
                    push_agent_chat_message([
                        `### ${__("توقفت المعالجة")}`,
                        "",
                        run.error_summary || __("توقف Run التحليل قبل إرجاع نتيجة مكتملة."),
                        "",
                        __("يمكنك إعادة التحليل من OCR المحفوظ بدون الرجوع إلى رفع الملفات أو قراءة الصفحات."),
                    ].join("\n"), "process");
                    render_analysis();
                }
            }
        });
    }

    function update_session_progress_in_place(result) {
        const session = result.session || {};
        const backendPages = session.pages || [];
        if (!backendPages.length) return;

        for (const row of backendPages) {
            const existing = pageManifest.find((p) => p.page_id === row.page_id);
            if (existing) {
                existing.status = row.status;
                if (row.text_preview) existing.text_preview = row.text_preview;
                if (row.extraction) existing.extraction = row.extraction;
                if (row.warnings?.length) existing.warnings = row.warnings;
                existing.analysis_status = row.analysis_status || "";
                existing.analysis_run = row.analysis_run || "";
                existing.analysis = row.analysis || {};
            }
        }
        update_tree_page_statuses();
        render_preview();
        if (activeStep === "extract" || activeStep === "process") {
            render_analysis();
        }
    }

    function apply_realtime_page_reading_update(update) {
        // OCR retries can change the run while keeping the same page session.
        // The session is therefore the authoritative identity for page updates.
        const matchesActiveSession = Boolean(
            sessionName && update.session_name && sessionName === update.session_name
        );
        const matchesActiveRun = Boolean(
            update.run_id && [currentRun, extractionRun].includes(update.run_id)
        );
        if (!matchesActiveSession && !matchesActiveRun) return;
        if (sessionName && update.session_name && !matchesActiveSession) return;

        if (!sessionName && update.session_name) {
            sessionName = update.session_name;
            localStorage.setItem("financial_attachment_session", sessionName);
        }

        const incoming = update.page;
        if (incoming?.page_id) {
            const index = pageManifest.findIndex((row) => row.page_id === incoming.page_id);
            if (index >= 0) {
                pageManifest[index] = {
                    ...pageManifest[index],
                    ...incoming,
                    text_preview: incoming.text_preview || "",
                    metadata: incoming.metadata || {},
                    warnings: incoming.warnings || [],
                    analysis: incoming.analysis || {},
                };
            }
        }

        const progress = update.progress || {};
        const completed = Number(progress.completed || 0);
        const total = Number(progress.total || 0);
        if (total) {
            extractionMessage = completed >= total
                ? __("اكتملت قراءة الصفحات المختارة.")
                : __("تمت قراءة {0} من {1} صفحة.", [completed, total]);
        }

        update_tree_page_statuses();
        render_preview();
        if (activeStep === "extract" || activeStep === "process") render_analysis();

        if (["Completed", "Failed", "Cancelled", "Timed Out"].includes(update.run_status)) {
            refresh_run();
        }
    }

    function update_tree_page_statuses() {
        for (const row of pageManifest) {
            const statusClass = String(row.status || "").toLowerCase();
            const node = page.main.find(`.pi-page-node[data-page-id="${row.page_id}"]`);
            if (node.length) {
                node.attr("class", `pi-page-node ${row.page_id === activePageId ? "active" : ""} ${statusClass} ${is_image_page(row) ? "image-page" : ""}`);
                node.find(".pi-page-state")
                    .attr("class", `pi-page-state ${statusClass}`)
                    .text(page_status_label(row.status));
            }
        }
        const prog = page.main.find(".pi-page-progress");
        if (prog.length) {
            prog.replaceWith(page_reading_progress_html());
        }
    }

    function normalize_analysis(value) {
        const groupKeys = contract_array("group_collection_keys", ["document_groups", "documents", "invoice_groups", "groups"]);
        if (!value) return { document_groups: [] };
        if (Array.isArray(value)) return { document_groups: value };
        for (const key of groupKeys) {
            if (Array.isArray(value[key])) {
                return { ...value, document_groups: value[key] };
            }
        }
        return { ...value, document_groups: [value] };
    }

    function analysis_has_data(value) {
        const normalized = normalize_analysis(value);
        if ((normalized.workflow || {}).stage === "page_preparation") return false;
        return (normalized.document_groups || []).some((group) => primary_rows(group).length || pages_with_reading(group).length || object_has_visible_data(group));
    }

    function analysis_has_rows(value) {
        const normalized = normalize_analysis(value);
        return (normalized.document_groups || []).some((group) => primary_rows(group).length);
    }

    function analysis_workflow_stage() {
        return String((analysis || {}).workflow?.stage || "");
    }

    function sync_page_manifest_from_analysis() {
        if (sessionName) return;
        if (!analysis || analysis_workflow_stage() !== "page_preparation") return;
        const pages = [];
        for (const group of analysis_groups()) {
            for (const file of group.files || []) {
                for (const pageInfo of file.pages || []) {
                    pages.push({
                        file_id: file.file_id,
                        file_name: file.file_name,
                        page_id: pageInfo.page_id || `${file.file_id}::p${pageInfo.page || 1}`,
                        page: pageInfo.page || 1,
                        page_type: pageInfo.page_type || "pdf_page",
                        status: pageInfo.status || "ready",
                        can_read: pageInfo.can_reprocess ? 1 : 0,
                        preview_data_url: pageInfo.preview_data_url || "",
                        note: (pageInfo.warnings || []).join("، "),
                    });
                }
            }
        }
        if (pages.length) {
            pageManifest = pages;
            if (!selectedPages.size) {
                selectedPages = new Set(pages.filter((row) => row.can_read).map((row) => row.page_id));
            }
            activePageId = activePageId || pages[0]?.page_id || "";
        }
    }

    function apply_file_classification() {
        const classified = {};
        for (const group of analysis_groups()) {
            for (const file of group.files || []) {
                classified[file.file_name || file.file_id] = { group, file };
            }
        }
        files = files.map((row) => {
            const item = classified[row.name] || classified[row.saved_file?.name] || {};
            return {
                ...row,
                analysis_status: __("Analyzed"),
                role: item.file?.role || item.file?.document_type || row.role,
                group: item.group?.document_group_id || item.group?.invoice_group_id || row.group,
            };
        });
        render_files();
    }

    function analysis_groups() {
        return normalize_analysis(analysis).document_groups || [];
    }

    function primary_rows(group) {
        if (!group || typeof group !== "object") return [];
        let firstArray = null;
        for (const key of contract_array("primary_collection_keys", ["journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"])) {
            if (Array.isArray(group[key])) {
                if (!firstArray) firstArray = group[key];
                if (group[key].length) return group[key];
            }
        }
        return firstArray || [];
    }

    function primary_rows_path() {
        const group = first_group();
        if (!group) return "document_groups.0.lines";
        for (const key of contract_array("primary_collection_keys", ["journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"])) {
            if (Array.isArray(group[key])) return `document_groups.0.${key}`;
        }
        return "document_groups.0.lines";
    }

    function object_has_visible_data(value) {
        if (!value || typeof value !== "object") return false;
        return Object.entries(value).some(([key, current]) => (
            !["files", "warnings", "errors", "workflow"].includes(key) &&
            !is_empty_value(current) &&
            (typeof current !== "object" || object_has_visible_data(current))
        ));
    }

    function render_all() {
        render_files();
        render_preview();
        render_analysis();
        update_counts();
        update_page_header_actions();
    }

    function update_page_header_actions() {
        if (!page) return;
        page.clear_primary_action();
        if (activeStep === "extract") {
            const hasReadPages = pageManifest.some((row) => String(row.status || "").toLowerCase() === "read");
            page.set_primary_action(__("المعالجة في Agent Workspace"), open_agent_workspace, "fa fa-external-link");
            page.main.find(".page-actions .btn-primary").prop("disabled", !hasReadPages || !sessionName);
        } else if (activeStep === "files") {
            page.set_primary_action(__("إضافة ملفات"), function () {
                open_upload_modal();
            }, "fa fa-upload");
        }
    }

    function render_files() {
        const list = page.main.find("[data-file-list]");
        if (!pageManifest.length) {
            if (files.length) {
                list.html(pending_files_tree_html());
                render_upload_files();
                update_counts();
                return;
            }
            list.html(`
                <div class="pi-sidebar-empty">
                    <b>${__("لا توجد صفحات")}</b>
                    <span>${__("اضغط إضافة ملفات لتجهيز شجرة الصفحات")}</span>
                </div>
            `);
            render_upload_files();
            update_counts();
            return;
        }
        list.html(`
            <div class="pi-sidebar-toolbar">
                <button class="btn btn-xs btn-default" data-page-bulk="all">${__("تحديد الكل")}</button>
                <button class="btn btn-xs btn-default" data-page-bulk="none">${__("إلغاء")}</button>
                <span>${selectedPages.size}</span>
            </div>
            ${page_reading_progress_html()}
            <div class="pi-page-tree persistent">
                ${page_tree_html()}
                ${pending_files_tree_body_html()}
            </div>
        `);
        render_upload_files();
        update_counts();
    }

    function render_upload_files() {
        const list = page.main.find("[data-upload-file-list]");
        if (!list.length) return;
        const pending = pending_upload_files();
        if (!pending.length) {
            list.html(`<div class="pi-empty small">${__("لم يتم اختيار ملفات بعد")}</div>`);
            return;
        }
        list.html(pending.map(({ row, index }) => `
            <div class="pi-upload-file-row ${index === activeFileIndex ? "active" : ""}" data-index="${index}">
                <div>
                    <b>${escape_html(row.name)}</b>
                    <small>${escape_html(row.type || extension_of(row.name))} · ${format_size(row.size)}</small>
                    <small>${escape_html(row.upload_status)} · ${escape_html(row.analysis_status)}</small>
                </div>
                <button class="btn btn-xs btn-default pi-file-remove" data-index="${index}">×</button>
            </div>
        `).join(""));
    }

    function get_preview_title_text() {
        const activePage = pageManifest.find((row) => row.page_id === activePageId);
        if (activePage) {
            return `${activePage.file_name || ""} - ${__("صفحة")} ${activePage.page || 1}`;
        }
        if (activeFileId) {
            const pages = pageManifest.filter((row) => row.file_id === activeFileId);
            const fileName = pages[0]?.file_name || activeFileId;
            return `${fileName} - ${__("المستند كاملاً")} (${pages.length} ${__("صفحات")})`;
        }
        const row = files[activeFileIndex];
        return row?.name || __("لا يوجد ملف محدد");
    }

    function get_preview_body_html() {
        const activePage = pageManifest.find((row) => row.page_id === activePageId);
        if (activePage) {
            const cached = pagePreviewCache[activePage.page_id] || activePage.preview_data_url || "";
            if (cached) {
                return `
                    <div class="pi-preview-media-wrapper">
                        <img class="pi-preview-image" src="${cached}" alt="${escape_attr(activePage.file_name || "")}">
                    </div>
                `;
            } else {
                load_page_preview(activePage.page_id);
                return `
                    <div class="pi-preview-empty-state">
                        <div class="pi-empty-icon"><i class="fa fa-spinner fa-spin"></i></div>
                        <b>${__("جاري تحميل معاينة الصفحة...")}</b>
                    </div>
                `;
            }
        }

        if (activeFileId) {
            const pages = pageManifest.filter((row) => row.file_id === activeFileId);
            const pageOne = pages[0] || {};
            const fileName = pageOne.file_name || activeFileId;
            let fileUrl = pageOne.file_url || "";

            const fileItem = files.find((item) => item.name === fileName || item.file_id === activeFileId || item.row?.file);
            if (fileItem?.row?.file) {
                fileUrl = URL.createObjectURL(fileItem.row.file);
            } else if (fileItem?.file) {
                fileUrl = URL.createObjectURL(fileItem.file);
            }

            const ext = extension_of(fileName).toLowerCase();

            if (fileUrl) {
                if (ext === ".pdf" || fileUrl.toLowerCase().includes(".pdf")) {
                    return `<iframe class="pi-preview-iframe" src="${fileUrl}" title="${escape_attr(fileName)}"></iframe>`;
                }
                if ([".png", ".jpg", ".jpeg", ".webp"].includes(ext) || fileUrl.match(/\.(png|jpg|jpeg|webp)/i)) {
                    return `
                        <div class="pi-preview-media-wrapper">
                            <img class="pi-preview-image" src="${fileUrl}" alt="${escape_attr(fileName)}">
                        </div>
                    `;
                }
                return `<iframe class="pi-preview-iframe" src="${fileUrl}" title="${escape_attr(fileName)}"></iframe>`;
            }

            if (pages.length) {
                pages.forEach((p) => {
                    if (!pagePreviewCache[p.page_id] && !p.preview_data_url) {
                        load_page_preview(p.page_id);
                    }
                });

                return `
                    <div class="pi-full-file-preview-list">
                        ${pages.map((row) => {
                    const cached = pagePreviewCache[row.page_id] || row.preview_data_url || "";
                    return `
                                <div class="pi-file-page-card">
                                    <div class="pi-file-page-card-head">
                                        <span><i class="fa fa-file-text-o"></i> ${escape_html(page_display_name(row))}</span>
                                        <span class="pi-page-state ${String(row.status || "").toLowerCase()}">${escape_html(page_status_label(row.status))}</span>
                                    </div>
                                    <div class="pi-file-page-card-body">
                                        ${cached ? `<img src="${cached}" alt="${escape_attr(page_display_name(row))}">` : `<div class="pi-preview-page-loading"><i class="fa fa-spinner fa-spin"></i> ${__("جاري تحميل المعاينة...")}</div>`}
                                    </div>
                                </div>
                            `;
                }).join("")}
                    </div>
                `;
            }
        }

        const row = files[activeFileIndex];
        if (!row) {
            return `
                <div class="pi-preview-empty-state">
                    <div class="pi-empty-icon"><i class="fa fa-file-text-o"></i></div>
                    <b>${__("لا يوجد ملف أو صفحة محددة")}</b>
                    <p>${__("اختر صفحة أو اضغط اسم الملف على اليمين لمعاينته كاملًا.")}</p>
                </div>
            `;
        }
        if (!row.file) {
            return `
                <div class="pi-file-preview-card">
                    <i class="fa fa-file-pdf-o pi-preview-file-icon"></i>
                    <b>${escape_html(row.name || __("مرفق محفوظ"))}</b>
                    <span>${__("هذا الملف محفوظ ضمن الجلسة. اختر صفحة من الشجرة لعرض معاينتها.")}</span>
                </div>
            `;
        }
        const url = URL.createObjectURL(row.file);
        const ext = extension_of(row.name);
        if ([".png", ".jpg", ".jpeg", ".webp"].includes(ext)) {
            return `
                <div class="pi-preview-media-wrapper">
                    <img class="pi-preview-image" src="${url}" alt="${escape_attr(row.name)}">
                </div>
            `;
        } else if (ext === ".pdf") {
            return `<iframe class="pi-preview-iframe" src="${url}" title="${escape_attr(row.name)}"></iframe>`;
        } else {
            return `
                <div class="pi-file-preview-card">
                    <i class="fa fa-file-o pi-preview-file-icon"></i>
                    <b>${escape_html(row.name)}</b>
                    <span>${escape_html(ext || row.type)} · ${format_size(row.size)}</span>
                </div>
            `;
        }
    }

    function render_preview() {
        page.main.find("[data-preview-title]").text(get_preview_title_text());
        page.main.find("[data-preview]").html(get_preview_body_html());

        const inlinePreview = page.main.find(".pi-inline-preview");
        if (inlinePreview.length) {
            inlinePreview.html(active_page_preview_html());
        }

        const pageReader = page.main.find(".pi-page-reader.standalone");
        if (pageReader.length) {
            pageReader.html(active_page_reading_html());
        }

        if (activePageId) {
            load_page_preview(activePageId);
        }
    }

    function load_page_preview(pageId) {
        if (!sessionName || pagePreviewLoading.has(pageId) || pagePreviewCache[pageId]) return;
        pagePreviewLoading.add(pageId);
        frappe.xcall("ai_agent.purchase_invoice_ai.api.get_page_preview", {
            session_name: sessionName,
            page_id: pageId,
        }).then((result) => {
            if (result.preview_data_url) pagePreviewCache[pageId] = result.preview_data_url;
            const pageRow = pageManifest.find((row) => row.page_id === pageId);
            if (pageRow && result.text_preview) pageRow.text_preview = result.text_preview;
            pagePreviewLoading.delete(pageId);
            if (activePageId === pageId) render_preview();
        }).catch(() => {
            pagePreviewLoading.delete(pageId);
        });
    }

    function render_analysis() {
        render_stage_tabs();
        render_stage_chat();
    }

    function render_stage_tabs() {
        page.main.find("[data-step-nav]").each(function () {
            const step = $(this).data("step-nav");
            $(this)
                .toggleClass("active", step === activeStep)
                .toggleClass("done", step_is_done(step));
        });
    }

    function step_is_done(step) {
        if (step === "files") return files.length || pageManifest.length;
        if (step === "extract") return analysisStage === "extracted";
        return false;
    }

    function render_stage_chat() {
        const panel = page.main.find("[data-stage-chat]");
        const prevScroll = panel.scrollTop();
        const stepChanged = (activeStep !== lastActiveStep);
        lastActiveStep = activeStep;

        let html = "";
        html = activeStep === "files" ? files_step_chat() : extraction_step_chat();
        panel.html(html);

        if (stepChanged) {
            panel.scrollTop(0);
        } else {
            panel.scrollTop(prevScroll);
        }
    }

    function files_step_chat() {
        return `
            <section class="pi-step-panel pi-files-panel">
                <header class="pi-step-panel-head">
                    <div>
                        <h3>${__("الملفات والصفحات")}</h3>
                        <p>${files.length || pageManifest.length
                ? __("راجع الملفات والصفحات، وحدد الصفحات التي تريد قراءتها.")
                : __("أضف المرفقات أولًا. بعد الرفع ستظهر الملفات والصفحات هنا.")}</p>
                    </div>
                    <div class="pi-step-panel-actions">
                        ${action_button("open_upload", __("إضافة ملفات"), "primary", false)}
                        ${action_button("extract", __("تجهيز الصفحات"), "default", !pending_upload_files().length)}
                        ${pageManifest.length ? action_button("read_selected_pages", __("قراءة المحدد"), "default", !selectedPages.size) : ""}
                    </div>
                </header>
                ${files_tab_tree_html()}
            </section>
        `;
    }

    function extraction_step_chat() {
        const group = first_group();
        if (analysisStage === "extracting") {
            return chat_thread([
                user_message(`${__("رفعت")} ${files.length} ${__("ملف/ملفات")}`),
                ai_message(
                    markdown_block([
                        `### ${__("جاري استخراج البيانات")}`,
                        "",
                        extractionMessage || __("أقرأ المرفقات في الخلفية. يمكنك الانتظار هنا، وستظهر البيانات بمجرد اكتمال الاستخراج."),
                        "",
                        `- **${__("الحالة")}**: ${escape_html(lastRun?.status || (currentRun ? __("Queued") : __("Uploading")))}`,
                        `- **${__("الملفات")}**: ${files.length}`,
                        pageManifest.length ? `- **${__("الصفحات المختارة")}**: ${selectedPages.size}` : "",
                        currentRun ? `- **${__("Run")}**: ${currentRun}` : `- **${__("Run")}**: ${__("سيظهر بعد رفع الملفات")}`,
                    ].filter(Boolean).join("\n")) +
                    extraction_workspace_html() +
                    chat_actions([
                        action_button("cancel_current_run", __("إيقاف الاستخراج"), "default", !currentRun),
                    ])
                ),
            ]);
        }
        if (analysisStage === "extraction_failed") {
            return chat_thread([
                user_message(`${__("رفعت")} ${files.length} ${__("ملف/ملفات")}`),
                ai_message(
                    markdown_block([
                        `### ${__("لم يتم استخراج بيانات قابلة للعرض")}`,
                        "",
                        extractionMessage || __("لم أستطع استخراج سطور قيد واضحة من المرفقات."),
                        "",
                        `- **${__("الملفات")}**: ${files.length}`,
                        `- **${__("السطور المستخرجة")}**: 0`,
                        no_data_diagnostics_markdown(),
                        "",
                        __("يمكنك إعادة الاستخراج بعد رفع ملف أوضح أو إضافة تعليمات أكثر تحديدًا."),
                    ].join("\n")) +
                    chat_actions([
                        action_button("reprocess_failed_pages", __("إعادة قراءة الصفحات غير المقروءة"), "primary", !pages_needing_reprocess().length),
                        action_button("reextract", __("إعادة تجهيز الصفحات"), "default", false),
                        action_button("new", __("عملية جديدة"), "default", false),
                    ])
                ),
            ]);
        }
        if (!group) {
            return chat_thread([
                user_message(files.length ? `${__("رفعت")} ${files.length} ${__("ملف/ملفات")}` : __("لم يتم رفع مرفقات بعد")),
                ai_message(
                    markdown_block([
                        `### ${__("الخطوة 1: رفع الملفات واستخراج البيانات")}`,
                        "",
                        files.length
                            ? __("الملفات جاهزة. اضغط زر الاستخراج لقراءة المرفقات وعرض البيانات الأولية هنا.")
                            : __("أضف ملفات الفواتير أو السندات أولًا، ثم اضغط استخراج البيانات."),
                    ].join("\n")) +
                    chat_actions([
                        action_button("extract", __("استخراج البيانات"), "primary", !files.length),
                        action_button("new", __("عملية جديدة"), "default", false),
                    ])
                ),
            ]);
        }
        if (analysisStage === "pages_ready") {
            return extraction_panel_html([
                `### ${__("الصفحات جاهزة للقراءة")}`,
                "",
                extractionMessage || __("اختر الصفحات التي تريد قراءتها ثم ابدأ القراءة."),
                "",
                `- **${__("عدد الصفحات")}**: ${pageManifest.length}`,
                `- **${__("المحدد للقراءة")}**: ${selectedPages.size}`,
            ].join("\n"), [
                action_button("read_selected_pages", __("قراءة الصفحات المختارة"), "primary", !selectedPages.size),
                action_button("reextract", __("إعادة تجهيز الصفحات"), "default", false),
            ]);
        }
        return extraction_panel_html("", [
            action_button("open_agent_workspace", __("المعالجة في Agent Workspace"), "primary", !pageManifest.some((row) => String(row.status || "").toLowerCase() === "read")),
            action_button("reextract", __("إعادة تجهيز الصفحات"), "default", false),
            action_button("reprocess_failed_pages", __("إعادة قراءة الصفحات غير المقروءة"), "default", !pages_needing_reprocess().length),
            action_button("cancel_current_run", __("إيقاف الاستخراج"), "default", !currentRun || !["extracting"].includes(analysisStage)),
        ], pageManifest.length ? extraction_workspace_html() : markdown_block(extracted_markdown(group)));
    }

    function extraction_panel_html(markdown, actions = [], body = "") {
        return `
            <section class="pi-step-panel pi-extract-panel">
                <header class="pi-step-panel-head">
                    <div>
                        <h3>${__("الاستخراج")}</h3>
                        <p>${__("راجع النص المقروء من الصفحة المحددة، ثم افتح Agent Workspace لمعالجة الجلسة.")}</p>
                    </div>
                    <div class="pi-step-panel-actions">${actions.join("")}</div>
                </header>
                ${markdown ? markdown_block(markdown) : ""}
                ${body || extraction_workspace_html()}
            </section>
        `;
    }

    async function reprocess_failed_pages() {
        if (!extractionRun || !analysis) {
            frappe.msgprint(__("استخرج البيانات أولًا."));
            return;
        }
        const pages = pages_needing_reprocess();
        if (!pages.length) {
            frappe.show_alert({ message: __("لا توجد صفحات تحتاج إعادة قراءة."), indicator: "green" });
            return;
        }
        set_busy(true, __("Re-reading"));
        extractionMessage = __("أعيد قراءة الصفحات التي لم ترجع نصًا واضحًا فقط.");
        render_analysis();
        try {
            let latest = null;
            for (const item of pages) {
                const result = await frappe.xcall("ai_agent.purchase_invoice_ai.api.reprocess_page", {
                    run_name: extractionRun,
                    file_id: item.file_id,
                    page: item.page,
                });
                latest = result;
            }
            if (latest?.analysis) {
                analysis = normalize_analysis(latest.analysis);
                analysisStage = analysis_has_data(analysis) ? "extracted" : "extraction_failed";
                extractionMessage = latest.message || __("تم تحديث قراءة الصفحات.");
                apply_file_classification();
            }
            set_busy(false, analysisStage === "extracted" ? __("Extracted") : __("No data"));
            render_all();
        } catch (error) {
            set_busy(false, __("Failed"));
            extractionMessage = error_message(error, __("تعذرت إعادة قراءة الصفحات."));
            render_analysis();
            frappe.msgprint(extractionMessage);
        }
    }

    function pages_needing_reprocess() {
        const pages = [];
        for (const group of analysis_groups()) {
            for (const file of group.files || []) {
                for (const pageInfo of file.pages || []) {
                    const text = strip_text_fence(pageInfo.text_preview || "");
                    const status = String(pageInfo.status || "").toLowerCase();
                    if ((pageInfo.can_reprocess || status === "failed" || !text) && (status === "failed" || !text)) {
                        pages.push({
                            file_id: file.file_id || file.file_name,
                            file_name: file.file_name,
                            page: pageInfo.page || 1,
                        });
                    }
                }
            }
        }
        return pages;
    }

    function process_step_chat() {
        const group = first_group();
        if (!group) {
            const details = [
                analysis_progress_markdown(),
                page_analysis_progress_markdown(),
                analysis_errors_markdown(),
                run_log_markdown(),
            ].filter(Boolean).join("\n\n");
            return chat_thread([
                ai_message(
                    markdown_block([
                        `### ${__("المعالجة والمطابقة")}`,
                        "",
                        details || __("لا توجد نتيجة معالجة قابلة للعرض لهذه العملية."),
                    ].join("\n")) +
                    process_pages_workspace_html() +
                    chat_actions([
                        action_button("reanalyze_saved_ocr", __("إعادة التحليل من OCR المحفوظ"), "primary", !currentRun && !extractionRun),
                    ])
                ),
            ]);
        }
        if (analysisStage === "matching") {
            if (analysis_has_rows(analysis)) {
                return chat_thread([
                    user_message(__("ابدأ المطابقة والتحقق بناءً على البيانات المستخرجة.")),
                    ai_message(
                        markdown_block([
                            `### ${__("جاري المعالجة")}`,
                            "",
                            analysis_progress_markdown(),
                            page_analysis_progress_markdown(),
                            "",
                            processed_markdown(group),
                        ].filter(Boolean).join("\n")) +
                        process_pages_workspace_html() +
                        chat_actions([
                            action_button("reanalyze_saved_ocr", __("إعادة التحليل من OCR المحفوظ"), "default", true),
                        ])
                    ),
                    ai_message(markdown_block(run_log_markdown())),
                ]);
            }
            return chat_thread([
                user_message(__("ابدأ المطابقة والتحقق بناءً على البيانات المستخرجة.")),
                ai_message(
                    markdown_block(`### ${__("جاري المعالجة")}\n\n${page_analysis_progress_markdown() || __("أرسل الصفحات للوكيل صفحة صفحة، وستظهر نتيجة كل صفحة هنا عند اكتمالها.")}`) +
                    process_pages_workspace_html()
                ),
                ai_message(markdown_block(run_log_markdown())),
            ]);
        }
        if (analysisStage === "analyzed") {
            return chat_thread([
                user_message(__("تمت مراجعة الاستخراج، اعرض نتيجة المعالجة.")),
                ai_message(
                    markdown_block(processed_markdown(group)) +
                    process_pages_workspace_html() +
                    chat_actions([
                        action_button("create", __("الانتقال للإنشاء"), "success", false, "review"),
                        action_button("reanalyze_saved_ocr", __("إعادة التحليل من OCR المحفوظ"), "default", !currentRun && !extractionRun),
                    ])
                ),
            ]);
        }
        return chat_thread([
            ai_message(
                markdown_block(`### ${__("الخطوة 2: المعالجة والمطابقة")}\n\n${__("البيانات المستخرجة جاهزة. لن أعيد قراءة المرفقات؛ سأستخدم البيانات المعروضة في الخطوة الأولى فقط.")}`) +
                process_pages_workspace_html() +
                chat_actions([
                    action_button("analyze", __("بدء المعالجة والمطابقة"), "primary", false),
                    action_button("reanalyze_saved_ocr", __("إعادة التحليل من OCR المحفوظ"), "default", !currentRun && !extractionRun),
                ])
            ),
        ]);
    }

    function review_step_chat() {
        const group = first_group();
        if (!group) {
            return chat_thread([
                ai_message(markdown_block(`### ${__("الخطوة 3: الاستعراض النهائي والإنشاء")}\n\n${__("لا توجد بيانات بعد. ابدأ بالاستخراج أولًا.")}`)),
            ]);
        }
        return `
            <div class="pi-step-action-bar">
                <div class="pi-action-bar-info">
                    <b><i class="fa fa-check-circle text-success" style="font-size:1.1em; margin-inline-end: 4px;"></i> ${__("جاهز لإنشاء مسودة القيد")}</b>
                    <span>${__("تم إعداد السطور والمورد النقدي والحسابات المقترحة بنجاح.")}</span>
                </div>
                <div class="pi-action-bar-buttons">
                    <button class="btn btn-success btn-md" data-action="create" ${!primary_rows(group).length ? "disabled" : ""}>
                        <i class="fa fa-plus-circle"></i> <b>${__("فتح قيد يومية للمراجعة")}</b>
                    </button>
                    <button class="btn btn-default btn-md" data-action="reanalyze_saved_ocr" ${!currentRun && !extractionRun ? "disabled" : ""}>
                        <i class="fa fa-refresh"></i> ${__("إعادة المعالجة")}
                    </button>
                </div>
            </div>
        ` + chat_thread([
            ai_message(
                markdown_block(final_review_markdown(group)) +
                chat_actions([
                    action_button("create", __("فتح قيد يومية للمراجعة"), "success", !primary_rows(group).length),
                    action_button("reanalyze_saved_ocr", __("إعادة التحليل من OCR المحفوظ"), "default", !currentRun && !extractionRun),
                ])
            ),
        ]);
    }

    function chat_thread(messages) {
        return `<div class="pi-chat-thread">${messages.join("")}</div>`;
    }

    function discussion_thread() {
        return discussionMessages
            .filter((message) => message.step === activeStep)
            .map((message) => message.role === "user" ? user_message(message.content) : ai_message(markdown_block(message.content)));
    }

    async function add_discussion_message() {
        const input = page.main.find("[data-chat-input]");
        const text = String(input.val() || "").trim();
        if (!text) return;
        discussionMessages.push({ step: activeStep, role: "user", content: text });
        input.val("");
        const pending = { step: activeStep, role: "ai", content: pending_discussion_message(text) };
        discussionMessages.push(pending);
        render_analysis();
        try {
            const result = await ask_agent_discussion(text);
            pending.content = result.reply_markdown || __("تمت مراجعة طلبك.");
            await refresh_analysis_after_discussion();
            apply_agent_decision(result.decision || {});
            if (!String(result?.decision?.type || "").trim() && analysis_has_rows(analysis)) {
                analysisStage = "analyzed";
                activeStep = "review";
            }
        } catch (error) {
            pending.content = [
                `### ${__("تعذر رد الوكيل")}`,
                "",
                error_message(error, __("لم أستطع الحصول على رد الوكيل الآن. حاول مرة أخرى.")),
            ].join("\n");
        }
        render_analysis();
    }

    async function refresh_analysis_after_discussion() {
        const candidates = [currentRun, extractionRun].filter(Boolean);
        for (const runName of [...new Set(candidates)]) {
            try {
                const result = await frappe.xcall("ai_agent.purchase_invoice_ai.api.get_analysis", { run_name: runName });
                if (result?.analysis) {
                    const next = normalize_analysis(result.analysis);
                    if (analysis_has_rows(next) || !analysis_has_rows(analysis)) {
                        analysis = next;
                        lastRun = result.run || lastRun;
                        lastSteps = result.steps || lastSteps;
                        if (analysis_has_rows(analysis)) return;
                    }
                }
            } catch (error) {
                // Keep the chat response visible even if a refresh attempt fails.
            }
        }
    }

    function pending_discussion_message(text) {
        const value = String(text || "");
        if (/(انش|إنش|افتح|فتح).*(قيد|القيد)/i.test(value)) {
            return `### ${__("جاري تجهيز القيد")}\n\n${__("أراجع البيانات الحالية وأجهز قيد اليومية للمراجعة.")}`;
        }
        if (/(عالج|حلل|طابق|مطابق|المعالجة|المطابقة)/i.test(value)) {
            return `### ${__("جاري معالجة البيانات")}\n\n${__("أراجع البيانات المستخرجة وأطابقها مع الحسابات المناسبة.")}`;
        }
        return `### ${__("جاري تنفيذ طلبك")}\n\n${__("أراجع الملاحظة والبيانات الحالية، ثم أعرض لك النتيجة المناسبة.")}`;
    }

    async function ask_agent_discussion(text) {
        const started = await frappe.xcall("ai_agent.purchase_invoice_ai.api.start_discussion", {
            company: companyControl?.get_value() || "",
            message: text,
            active_step: activeStep,
            analysis: JSON.stringify(analysis || {}),
            source_run_name: extractionRun || "",
            current_run_name: currentRun || "",
        });
        return poll_discussion_run(started.run_name);
    }

    async function poll_discussion_run(runName) {
        for (let attempt = 0; attempt < 45; attempt++) {
            await sleep(attempt < 2 ? 700 : 1400);
            const result = await frappe.xcall("ai_agent.purchase_invoice_ai.api.get_discussion", { run_name: runName });
            const status = result?.run?.status;
            if (status === "Completed") return result;
            if (["Failed", "Cancelled", "Timed Out"].includes(status)) {
                throw new Error(result?.run?.error_summary || __("تعذر إكمال رد الوكيل."));
            }
        }
        throw new Error(__("استغرق رد الوكيل وقتًا أطول من المتوقع."));
    }

    function apply_agent_decision(decision) {
        const type = String(decision.type || "").toLowerCase();
        if (Object.prototype.hasOwnProperty.call(decision, "allow_open_with_missing_accounts")) {
            allowOpenWithMissingAccounts = Boolean(decision.allow_open_with_missing_accounts);
        }
        if (type === "suggest_accounts") {
            discussionMessages.push({ step: activeStep, role: "ai", content: selected_accounts_markdown(first_group()) });
            render_analysis();
        }
    }

    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function selected_accounts_markdown(group) {
        const rows = primary_rows(group);
        if (!rows.length) {
            return `### ${__("الحسابات المختارة")}\n\n${__("لا توجد سطور قيد بعد.")}`;
        }
        const lines = [
            `### ${__("الحسابات المختارة")}`,
            "",
            `- **${__("عدد السطور")}**: ${rows.length}`,
            "",
            `| # | ${__("البيان")} | ${__("الحساب")} |`,
            "|---|---|---|",
        ];
        rows.forEach((row, index) => {
            const account = journal_line_value(row, "account", group);
            lines.push(`| ${index + 1} | ${markdown_cell(journal_line_value(row, "user_remark", group))} | ${markdown_cell(account || "")} |`);
        });
        const questions = accounting_questions_markdown(group);
        return [lines.join("\n"), questions].filter(Boolean).join("\n\n");
    }

    function push_agent_chat_message(content, step = activeStep) {
        activeStep = step || activeStep;
        discussionMessages.push({ step: activeStep, role: "ai", content });
        render_analysis();
    }

    function user_message(content) {
        return `
            <div class="pi-chat-message user">
                <div class="pi-chat-avatar">Y</div>
                <div class="pi-chat-card">
                    <div class="pi-chat-name">${__("المستخدم")}</div>
                    <div class="pi-chat-content">${escape_html(content)}</div>
                </div>
            </div>
        `;
    }

    function ai_message(content) {
        return `
            <div class="pi-chat-message ai">
                <div class="pi-chat-avatar">AI</div>
                <div class="pi-chat-card">
                    <div class="pi-chat-name">${escape_html(contract_text("assistant_label", __("AI")))}</div>
                    <div class="pi-chat-content">${content}</div>
                </div>
            </div>
        `;
    }

    function chat_actions(actions) {
        return `<div class="pi-chat-actions">${actions.join("")}</div>`;
    }

    function action_button(action, label, type = "default", disabled = false, step = null) {
        const cls = type === "primary" ? "btn-primary" : type === "success" ? "btn-success" : "btn-default";
        const attr = step ? `data-step-nav="${escape_attr(step)}"` : `data-action="${escape_attr(action)}"`;
        return `<button class="btn btn-sm ${cls}" ${attr} ${disabled ? "disabled" : ""}>${escape_html(label)}</button>`;
    }

    function markdown_block(markdown) {
        const lines = String(markdown || "").split("\n");
        const html = [];
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (!line.trim()) continue;
            if (line.startsWith("# ")) {
                html.push(`<h3>${escape_html(line.slice(2))}</h3>`);
                continue;
            }
            if (line.startsWith("### ")) {
                html.push(`<h3>${escape_html(line.slice(4))}</h3>`);
                continue;
            }
            if (line.startsWith("```")) {
                const codeLines = [];
                i++;
                while (i < lines.length && !lines[i].startsWith("```")) {
                    codeLines.push(lines[i]);
                    i++;
                }
                html.push(`<pre class="pi-text-preview">${escape_html(codeLines.join("\n"))}</pre>`);
                continue;
            }
            if (line.startsWith("## ")) {
                html.push(`<h3>${escape_html(line.slice(3))}</h3>`);
                continue;
            }
            if (line.startsWith("#### ")) {
                html.push(`<h3>${escape_html(line.slice(5))}</h3>`);
                continue;
            }
            if (/^\s*---+\s*$/.test(line)) {
                html.push("<hr>");
                continue;
            }
            if (line.startsWith("- ")) {
                const items = [];
                while (i < lines.length && lines[i].startsWith("- ")) {
                    items.push(`<li>${inline_markdown(lines[i].slice(2))}</li>`);
                    i++;
                }
                i--;
                html.push(`<ul>${items.join("")}</ul>`);
                continue;
            }
            if (is_markdown_table_start(lines, i)) {
                const headers = split_markdown_row(line);
                i += 2;
                const rows = [];
                while (i < lines.length && is_markdown_table_row(lines[i])) {
                    rows.push(split_markdown_row(lines[i]));
                    i++;
                }
                i--;
                html.push(`
                    <div class="pi-md-table-wrap">
                        <table class="pi-md-table">
                            <thead><tr>${headers.map((cell) => `<th>${inline_markdown(cell)}</th>`).join("")}</tr></thead>
                            <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inline_markdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
                        </table>
                    </div>
                `);
                continue;
            }
            html.push(`<p>${inline_markdown(line)}</p>`);
        }
        return `<div class="pi-markdown">${html.join("")}</div>`;
    }

    function inline_markdown(value) {
        return escape_html(value).replace(/\*\*(.*?)\*\*/g, "<b>$1</b>");
    }

    function split_markdown_row(line) {
        return String(line || "").replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
    }

    function is_markdown_table_start(lines, index) {
        return is_markdown_table_row(lines[index]) && /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(lines[index + 1] || "");
    }

    function is_markdown_table_row(line) {
        const value = String(line || "").trim();
        return value.includes("|") && !value.startsWith("```");
    }

    function extracted_markdown(group) {
        const rows = primary_rows(group);
        return [
            `### ${__("نتيجة الاستخراج الأولي")}`,
            "",
            `- **${__("الحالة")}**: ${extractionMessage || __("تم استخراج بيانات مبدئية من المرفقات فقط، بدون مقارنة مع النظام.")}`,
            `- **${__("القراءة")}**: ${__("تمت قراءة المرفقات وسيتم إرسالها للوكيل لترتيبها وتحليلها.")}`,
            rows.length ? `- **${__("ملاحظات بنيوية")}**: ${rows.length}` : "",
            "",
            extracted_facts_markdown(group),
            "",
            extracted_pages_markdown(group),
        ].join("\n");
    }

    function extracted_facts_markdown(group) {
        const facts = [];
        add_dynamic_facts(facts, group || {}, "");
        if (!facts.length) return "";
        return facts.map((row) => `- **${row.label}**: ${row.value}`).join("\n");
    }

    function add_dynamic_facts(facts, object, prefix) {
        for (const [key, value] of Object.entries(object || {})) {
            if (is_hidden_display_key(key) || ["files", "warnings", "errors"].includes(key) || Array.isArray(value) || is_empty_value(value) || is_zero_value(value)) continue;
            const label = [prefix, field_label(key)].filter(Boolean).join(" / ");
            if (value && typeof value === "object") {
                add_dynamic_facts(facts, value, label);
                continue;
            }
            facts.push({ label, value: display_value(value) });
            if (facts.length >= 24) return;
        }
    }

    function processed_markdown(group) {
        const errors = group.errors || [];
        const warnings = group.warnings || [];
        const rows = primary_rows(group);
        const summary = client_readable_summary(group);
        return [
            `### ${__("نتيجة المعالجة والمطابقة")}`,
            "",
            summary ? summary : "",
            summary ? "" : "",
            `- **${contract_text("primary_collection_label", __("السطور"))}**: ${rows.length}`,
            `- **${__("الأخطاء")}**: ${errors.length}`,
            `- **${__("التحذيرات")}**: ${warnings.length}`,
            "",
            extracted_rows_markdown(group),
            "",
            accounting_questions_markdown(group),
            "",
            validation_markdown(errors, warnings),
        ].join("\n");
    }

    function analysis_progress_markdown() {
        const progress = analysis?.workflow?.progress || {};
        const total = Number(progress.total_chunks || 0);
        const done = Number(progress.completed_chunks || 0);
        const failed = Number(progress.failed_chunks || 0);
        if (!total) return "";
        const percent = Math.round(Number(progress.percent || 0));
        return [
            `- **${__("تقدم الدفعات")}**: ${done}/${total} (${percent}%)`,
            failed ? `- **${__("دفعات فشلت")}**: ${failed}` : "",
            `- **${__("النتائج الحالية")}**: ${primary_rows(first_group()).length} ${__("سطر/سطور")}`,
        ].filter(Boolean).join("\n");
    }

    function page_analysis_progress_markdown() {
        if (!pageManifest.length) return "";
        const stats = page_analysis_stats();
        if (!stats.total) return "";
        return [
            `- **${__("تقدم الصفحات")}**: ${stats.done}/${stats.total} (${stats.percent}%)`,
            stats.running ? `- **${__("قيد المعالجة الآن")}**: ${stats.running}` : "",
            stats.failed ? `- **${__("صفحات فشلت")}**: ${stats.failed}` : "",
        ].filter(Boolean).join("\n");
    }

    function page_analysis_progress_bar_html() {
        const stats = page_analysis_stats();
        if (!stats.total) return "";
        return `
            <div class="pi-page-progress pi-analysis-progress">
                <div class="pi-progress-info">
                    <span class="pi-progress-count"><b>${stats.done}</b> ${__("من")} <b>${stats.total}</b></span>
                    ${stats.running ? `<small class="pi-progress-reading"><i class="fa fa-spinner fa-spin"></i> ${stats.running} ${__("قيد المعالجة")}</small>` : ""}
                    ${stats.failed ? `<small class="pi-progress-failed">${stats.failed} ${__("فشلت")}</small>` : ""}
                </div>
                <progress max="100" value="${stats.percent}"></progress>
            </div>
        `;
    }

    function page_analysis_stats() {
        const pages = pageManifest.filter((row) => String(row.status || "").toLowerCase() === "read" || row.text_preview);
        const total = pages.length;
        const completed = pages.filter((row) => String(row.analysis_status || "").toLowerCase() === "completed").length;
        const failed = pages.filter((row) => String(row.analysis_status || "").toLowerCase() === "failed").length;
        const running = pages.filter((row) => String(row.analysis_status || "").toLowerCase() === "running").length;
        const done = completed + failed;
        return {
            total,
            completed,
            failed,
            running,
            done,
            percent: total ? Math.round((done / total) * 100) : 0,
        };
    }

    function final_review_markdown(group) {
        const rows = primary_rows(group);
        const summary = client_readable_summary(group);
        return [
            `### ${__("الاستعراض النهائي")}`,
            "",
            summary ? summary : "",
            summary ? "" : "",
            `- **${contract_text("primary_collection_label", __("السطور"))}**: ${rows.length}`,
            "",
            extracted_facts_markdown(group),
            "",
            extracted_rows_markdown(group),
            "",
            accounting_questions_markdown(group),
            "",
            analysisStage === "analyzed"
                ? __("إذا كانت الحسابات والموردون مؤكدة يمكنك فتح قيد يومية للمراجعة قبل الحفظ.")
                : __("هذه الخطوة تتفعل بعد اكتمال المعالجة والمطابقة."),
        ].join("\n");
    }

    function accounting_questions_markdown(group) {
        const rows = primary_rows(group);
        const questions = []
            .concat(analysis?.questions || [])
            .concat(group?.questions || [])
            .concat(rows.flatMap((row) => row.questions || []))
            .filter(Boolean);
        if (!questions.length) return "";
        return [
            `### ${__("أسئلة تحتاج مراجعة")}`,
            "",
            ...questions.map((item) => `- ${item}`),
        ].join("\n");
    }

    function analysis_errors_markdown() {
        const errors = []
            .concat(analysis?.errors || [])
            .concat(first_group()?.errors || [])
            .filter(Boolean)
            .slice(0, 5);
        const warnings = []
            .concat(analysis?.warnings || [])
            .concat(first_group()?.warnings || [])
            .filter(Boolean)
            .slice(0, 5);
        if (!errors.length && !warnings.length) return "";
        return [
            errors.length ? `### ${__("الأخطاء")}` : "",
            ...errors.map((item) => `- ${markdown_cell(item)}`),
            warnings.length ? `### ${__("التنبيهات")}` : "",
            ...warnings.map((item) => `- ${markdown_cell(item)}`),
        ].filter(Boolean).join("\n");
    }

    function client_readable_summary(group) {
        return String(
            group?.client_readable_summary ||
            analysis?.client_readable_summary ||
            group?.summary ||
            analysis?.summary ||
            ""
        ).trim();
    }

    function agent_final_answer() {
        return String(lastRun?.final_answer || "").trim();
    }

    function items_markdown(rows, limit) {
        if (!rows.length) return __("لم يتم استخراج سطور قيد بعد.");
        const header = `| # | ${__("البيان")} | ${__("الصفحة")} | ${__("الكمية")} | ${__("الوحدة")} | ${__("السعر")} | ${__("مدين")} |`;
        const divider = "|---|---|---:|---:|---|---:|---:|";
        const body = rows.slice(0, limit).map((row, index) => (
            `| ${index + 1} | ${row.extracted_item_name || row.description || ""} | ${row.source_page || ""} | ${format_number(row.qty)} | ${row.uom || ""} | ${format_number(row.rate)} | ${format_number(row.line_total || row.net_amount)} |`
        ));
        if (rows.length > limit) {
            body.push(`|  | ${__("تم إخفاء بقية السطور من العرض المختصر")} |  |  |  |  | ${rows.length - limit}+ |`);
        }
        return [header, divider, ...body].join("\n");
    }

    function extracted_rows_markdown(group, limit = null) {
        const rows = primary_rows(group);
        if (!rows.length) return __("لم يتم استخراج سطور قابلة للعرض بعد.");

        const columns = extracted_row_columns(group, rows);
        const header = `| # | ${columns.map((column) => markdown_cell(column.label)).join(" | ")} |`;
        const divider = `|---|${columns.map(() => "---").join("|")}|`;
        const displayRows = limit ? rows.slice(0, limit) : rows;
        const body = displayRows.map((row, index) => (
            `| ${index + 1} | ${columns.map((column) => markdown_cell(column.value(row, group))).join(" | ")} |`
        ));
        if (limit && rows.length > limit) {
            body.push(`|  | ${markdown_cell(__("تم إخفاء بقية السطور من العرض المختصر"))} | ${columns.slice(1).map(() => "").join(" | ")} |`);
        }
        return [header, divider, ...body].join("\n");
    }

    function extracted_pages_markdown(group) {
        const sections = [];
        for (const { file, page } of pages_with_reading(group)) {
            const text = strip_text_fence(page.text_preview || "");
            const facts = Object.entries(page.metadata || {})
                .filter(([, value]) => !is_empty_value(value))
                .map(([key, value]) => `- **${field_label(key)}**: ${display_value(value)}`);
            const notes = (page.warnings || []).filter(Boolean).map((note) => `- **${__("ملاحظة")}**: ${note}`);
            sections.push([
                `### ${file.file_name || __("مرفق")} - ${__("صفحة")} ${page.page || 1}`,
                "",
                ...facts,
                ...notes,
                page.status ? `- **${__("حالة الصفحة")}**: ${page.status}` : "",
                "",
                text || __("لم يرجع OCR نصًا واضحًا"),
            ].filter((line) => line !== "").join("\n"));
        }
        if (!sections.length) return __("لم يرجع الاستخراج أي نص أو ملاحظة قابلة للعرض.");
        return [`### ${__("قراءة المرفقات")}`, "", ...sections].join("\n");
    }

    function pages_with_reading(group) {
        const rows = [];
        for (const file of group?.files || []) {
            for (const page of file.pages || []) {
                const text = strip_text_fence(page.text_preview || "");
                const status = String(page.status || "").toLowerCase();
                if (status === "ready" && !text) continue;
                if (text || status === "read" || status === "failed") {
                    rows.push({ file, page });
                }
            }
        }
        return rows;
    }

    function pages_workspace_html() {
        if (!pageManifest.length) return `<div class="pi-empty small">${__("لا توجد صفحات جاهزة")}</div>`;
        return `
            <div class="pi-page-reader standalone">
                ${active_page_reading_html()}
            </div>
        `;
    }

    function process_pages_workspace_html() {
        if (!pageManifest.length) return "";
        return `
            <div class="pi-process-pages-block">
                ${page_analysis_progress_bar_html()}
                ${extraction_workspace_html("process")}
            </div>
        `;
    }

    function files_tab_tree_html() {
        return `
            <div class="pi-tab-workspace pi-files-split-workspace">
                <div class="pi-files-list-pane">
                    ${pageManifest.length ? `
                        <div class="pi-sidebar-toolbar">
                            <div class="pi-toolbar-group">
                                <button class="btn btn-xs btn-default" data-page-bulk="all"><i class="fa fa-check-square-o"></i> ${__("تحديد الكل")}</button>
                                <button class="btn btn-xs btn-default" data-page-bulk="none"><i class="fa fa-square-o"></i> ${__("إلغاء التحديد")}</button>
                            </div>
                            <span class="pi-selected-badge">${__("المحدد")}: <b>${selectedPages.size}</b> / ${pageManifest.length}</span>
                        </div>
                        ${page_reading_progress_html()}
                        <div class="pi-page-tree persistent">
                            ${page_tree_html()}
                            ${pending_files_tree_body_html()}
                        </div>
                    ` : (pending_upload_files().length ? `
                        <div class="pi-sidebar-toolbar">
                            <span class="pi-selected-badge">${__("الملفات الجاهزة")}: <b>${pending_upload_files().length}</b></span>
                        </div>
                        <div class="pi-page-tree persistent">
                            ${pending_files_tree_body_html()}
                        </div>
                    ` : `
                        <div class="pi-empty-files-cta">
                            <i class="fa fa-folder-open-o"></i>
                            <p>${__("لا توجد ملفات مرفوعة بعد.")}</p>
                            <button class="btn btn-sm btn-primary" data-action="open_upload">${__("إضافة ملفات")}</button>
                        </div>
                    `)}
                </div>

                <div class="pi-files-preview-pane">
                    <header class="pi-files-preview-head">
                        <div class="pi-preview-head-title">
                            <i class="fa fa-file-text-o"></i>
                            <b data-preview-title>${escape_html(get_preview_title_text())}</b>
                        </div>
                    </header>
                    <div class="pi-files-preview-body" data-preview>
                        ${get_preview_body_html()}
                    </div>
                </div>
            </div>
        `;
    }

    function current_active_page_label() {
        const row = pageManifest.find((item) => item.page_id === activePageId);
        if (!row) return pageManifest[0] ? `${pageManifest[0].file_name || ""} - ${page_display_name(pageManifest[0])}` : __("اختر صفحة");
        return `${row.file_name || ""} - ${page_display_name(row)}`;
    }

    function current_page_index_label() {
        let index = pageManifest.findIndex((item) => item.page_id === activePageId);
        if (index === -1 && pageManifest.length) index = 0;
        if (index === -1) return "";
        return `${index + 1} ${__("من")} ${pageManifest.length}`;
    }

    function is_first_page() {
        let index = pageManifest.findIndex((item) => item.page_id === activePageId);
        if (index === -1 && pageManifest.length) index = 0;
        return index <= 0;
    }

    function is_last_page() {
        let index = pageManifest.findIndex((item) => item.page_id === activePageId);
        if (index === -1 && pageManifest.length) index = 0;
        return index === -1 || index >= pageManifest.length - 1;
    }

    function extraction_workspace_html(mode = "extract") {
        if (!pageManifest.length) return active_page_reading_html();
        if (!activePageId && pageManifest.length) {
            activePageId = pageManifest[0].page_id;
        }
        const isProcess = mode === "process";
        return `
            <div class="pi-tab-workspace pi-extraction-workspace">
                <div class="pi-extraction-sidebar">
                    <div class="pi-sidebar-title">
                        <div>
                            <i class="fa fa-files-o"></i>
                            <span>${isProcess ? __("صفحات المعالجة") : __("صفحات الاستخراج")}</span>
                        </div>
                        <span class="pi-sidebar-count">${pageManifest.length}</span>
                    </div>
                    <div class="pi-extraction-page-list">
                        ${pageManifest.map((row) => `
                            <button class="pi-extraction-page-btn ${row.page_id === activePageId ? "active" : ""}" type="button" data-page-id="${escape_attr(row.page_id)}">
                                <div class="pi-page-btn-main">
                                    <i class="fa fa-file-text-o pi-page-icon"></i>
                                    <div class="pi-page-btn-info">
                                        <span class="pi-page-num">${escape_html(page_display_name(row))}</span>
                                        <span class="pi-page-file-sub">${escape_html(row.file_name || "")}${isProcess && row.analysis_status ? ` · ${escape_html(page_analysis_status_label(row.analysis_status))}` : ""}</span>
                                    </div>
                                </div>
                                <span class="${isProcess ? "pi-page-analysis-state" : "pi-page-state"} ${escape_attr(String((isProcess ? row.analysis_status : row.status) || "").toLowerCase())}">
                                    ${escape_html(isProcess ? page_analysis_status_label(row.analysis_status) || __("لم تبدأ") : page_status_label(row.status))}
                                </span>
                            </button>
                        `).join("")}
                    </div>
                </div>

                <div class="pi-extraction-main-content">
                    <div class="pi-extraction-header-toolbar">
                        <button class="btn btn-xs btn-default" data-action="prev_page" ${is_first_page() ? "disabled" : ""}>
                            <i class="fa fa-chevron-right"></i> ${__("الصفحة السابقة")}
                        </button>
                        <span class="pi-current-page-indicator">
                            <b>${escape_html(current_active_page_label())}</b> (${current_page_index_label()})
                        </span>
                        <button class="btn btn-xs btn-default" data-action="next_page" ${is_last_page() ? "disabled" : ""}>
                            ${__("الصفحة التالية")} <i class="fa fa-chevron-left"></i>
                        </button>
                    </div>
                    <div class="pi-extraction-body-split">
                        <div class="pi-inline-preview">
                            ${active_page_preview_html()}
                        </div>
                        <div class="pi-page-reader standalone">
                            ${active_page_reading_html()}
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    function active_page_preview_html() {
        const row = pageManifest.find((item) => item.page_id === activePageId);
        if (!row) return `<div class="pi-empty small">${__("اختر صفحة للمعاينة")}</div>`;
        const cached = pagePreviewCache[row.page_id] || row.preview_data_url || "";
        if (!cached) {
            load_page_preview(row.page_id);
            return `<div class="pi-empty small">${__("جاري تحميل معاينة الصفحة")}</div>`;
        }
        return `
            <div class="pi-inline-preview-head">
                <b>${escape_html(row.file_name || "")}</b>
                <span>${escape_html(page_display_name(row))}</span>
            </div>
            <img src="${cached}" alt="${escape_attr(row.file_name || "")}">
        `;
    }

    function page_tree_html() {
        const filesMap = new Map();
        for (const row of pageManifest) {
            if (!filesMap.has(row.file_id)) filesMap.set(row.file_id, { file_name: row.file_name, pages: [] });
            filesMap.get(row.file_id).pages.push(row);
        }
        return Array.from(filesMap.entries()).map(([fileId, file]) => `
            <section class="pi-page-file ${collapsedFileIds.has(fileId) ? "collapsed" : ""} ${fileId === activeFileId ? "active" : ""}">
                <header class="pi-page-file-head">
                    <button class="pi-page-file-toggle" type="button" data-file-id="${escape_attr(fileId)}">
                        <i class="fa ${collapsedFileIds.has(fileId) ? "fa-chevron-left" : "fa-chevron-down"}"></i>
                    </button>
                    <div class="pi-page-file-title" data-file-id="${escape_attr(fileId)}">
                        <i class="fa fa-file-text-o"></i>
                        <b>${escape_html(file.file_name || fileId)}</b>
                    </div>
                    <span>${escape_html(file_pages_label(file.pages))}</span>
                </header>
                <div class="pi-page-file-children">
                    ${file.pages.map((row) => page_node(row)).join("")}
                </div>
            </section>
        `).join("");
    }

    function page_reading_progress_html() {
        if (!pageManifest.length) return "";
        const selected = pageManifest.filter((row) => selectedPages.has(row.page_id));
        const basis = selected.length ? selected : pageManifest;
        const read = basis.filter((row) => String(row.status || "").toLowerCase() === "read").length;
        const failed = basis.filter((row) => String(row.status || "").toLowerCase() === "failed").length;
        const reading = basis.filter((row) => String(row.status || "").toLowerCase() === "reading").length;
        const total = basis.length;
        const percent = total ? Math.round(((read + failed) / total) * 100) : 0;
        return `
            <div class="pi-page-progress">
                <div class="pi-progress-info">
                    <span class="pi-progress-count"><b>${read + failed}</b> ${__("من")} <b>${total}</b></span>
                    ${reading ? `<small class="pi-progress-reading"><i class="fa fa-spinner fa-spin"></i> ${reading} ${__("قيد القراءة")}</small>` : ""}
                </div>
                <progress max="100" value="${percent}"></progress>
            </div>
        `;
    }

    function pending_files_tree_html() {
        return `
            <div class="pi-sidebar-toolbar">
                <span>${pending_upload_files().length}</span>
            </div>
            <div class="pi-page-tree persistent">
                ${pending_files_tree_body_html()}
            </div>
        `;
    }

    function pending_files_tree_body_html() {
        const pending = pending_upload_files();
        if (!pending.length) return "";
        return pending.map(({ row, index }) => `
            <section class="pi-page-file pending">
                <header>
                    <button class="pi-page-file-toggle ghost" type="button" disabled>
                        <span>${is_image_file(row) ? "IMG" : file_extension_label(row)}</span>
                        <b>${escape_html(row.name || __("مرفق"))}</b>
                    </button>
                    <span>${escape_html(row.analysis_status || row.upload_status || __("جاهز"))}</span>
                </header>
                <div class="pi-page-file-children">
                    <button class="pi-page-node ${index === activeFileIndex ? "active" : ""} ${is_image_file(row) ? "image-page" : ""}" type="button" data-pending-file-index="${index}">
                        ${is_image_file(row) ? `<span class="pi-page-thumb">${pending_file_thumb(row)}</span>` : ""}
                        <span class="pi-page-node-main">
                            <b>${escape_html(row.name || __("مرفق"))}</b>
                            <small>${escape_html(row.type || extension_of(row.name || ""))} · ${format_size(row.size)}</small>
                        </span>
                        <span class="pi-page-state">${escape_html(row.upload_status || __("Ready"))}</span>
                    </button>
                </div>
            </section>
        `).join("");
    }

    function pending_upload_files() {
        return files
            .map((row, index) => ({ row, index }))
            .filter((item) => item.row?.file);
    }

    function page_status_label(status) {
        const s = String(status || "").toLowerCase();
        if (s === "read") return __("مقروءة");
        if (s === "reading") return __("قيد القراءة");
        if (s === "selected") return __("في الانتظار");
        if (s === "queued") return __("في الانتظار");
        if (s === "failed") return __("فشلت");
        return __("جاهزة");
    }

    function page_node(row) {
        const checked = selectedPages.has(row.page_id) ? "checked" : "";
        const active = row.page_id === activePageId ? "active" : "";
        const disabled = row.can_read ? "" : "disabled";
        const image = is_image_page(row);
        const statusClass = String(row.status || "").toLowerCase();
        return `
            <button class="pi-page-node ${active} ${statusClass} ${image ? "image-page" : ""}" type="button" data-page-id="${escape_attr(row.page_id)}">
                <span class="pi-page-node-check">
                    <input class="pi-page-select" type="checkbox" data-page-id="${escape_attr(row.page_id)}" ${checked} ${disabled}>
                </span>
                ${image ? `<span class="pi-page-thumb">${row.preview_data_url ? `<img src="${row.preview_data_url}" alt="">` : "IMG"}</span>` : ""}
                <span class="pi-page-node-main">
                    <b>${escape_html(page_display_name(row))}</b>
                    <small>${escape_html(page_type_label(row.page_type))}${row.analysis_status ? ` · ${escape_html(page_analysis_status_label(row.analysis_status))}` : ""}</small>
                </span>
                <span class="pi-page-state ${escape_attr(statusClass)}">
                    ${escape_html(page_status_label(row.status))}
                </span>
                ${row.analysis_status ? `<span class="pi-page-analysis-state ${escape_attr(String(row.analysis_status || "").toLowerCase())}">${escape_html(page_analysis_status_label(row.analysis_status))}</span>` : ""}
            </button>
        `;
    }

    function active_page_reading_html() {
        const row = pageManifest.find((item) => item.page_id === activePageId);
        if (!row) return `<div class="pi-empty small">${__("اختر صفحة من الشجرة")}</div>`;
        const text = strip_text_fence(row.text_preview || "");
        const warnings = (row.warnings || []).filter(Boolean);
        const analysisHtml = active_page_analysis_html(row);
        return `
            <div class="pi-active-page-head">
                <div>
                    <b>${escape_html(row.file_name || "")}</b>
                    <span>${escape_html(page_display_name(row))} · ${escape_html(page_status_label(row.status))}${row.analysis_status ? ` · ${escape_html(page_analysis_status_label(row.analysis_status))}` : ""}</span>
                </div>
                <button class="btn btn-xs btn-default" data-action="read_selected_pages">${__("قراءة المحدد")}</button>
            </div>
            ${warnings.length ? `<div class="pi-page-warnings">${warnings.map((item) => `<div>${escape_html(item)}</div>`).join("")}</div>` : ""}
            <section class="pi-page-output-section">
                <h4>${__("قراءة OCR")}</h4>
                ${text ? markdown_block(text) : `<div class="pi-empty small">${__("لم تُقرأ هذه الصفحة بعد")}</div>`}
            </section>
            <section class="pi-page-output-section">
                <h4>${__("ناتج معالجة الصفحة")}</h4>
                ${analysisHtml}
            </section>
        `;
    }

    function active_page_analysis_html(row) {
        const status = String(row.analysis_status || "").trim();
        const payload = row.analysis || {};
        if (!status && is_empty_analysis_payload(payload)) {
            return `<div class="pi-empty small">${__("لم تبدأ معالجة هذه الصفحة بعد")}</div>`;
        }
        if (["pending", "running"].includes(status.toLowerCase())) {
            return `
                <div class="pi-page-analysis-working">
                    <i class="fa fa-spinner fa-spin"></i>
                    <span>${escape_html(page_analysis_status_label(status))}</span>
                </div>
            `;
        }
        return `
            <div class="pi-page-analysis-summary">
                <span class="pi-page-analysis-state ${escape_attr(status.toLowerCase())}">${escape_html(page_analysis_status_label(status))}</span>
                ${row.analysis_run ? `<small>${__("Run")}: ${escape_html(row.analysis_run)}</small>` : ""}
            </div>
            ${page_analysis_payload_html(payload)}
        `;
    }

    function page_analysis_payload_html(payload) {
        if (is_empty_analysis_payload(payload)) {
            return `<div class="pi-empty small">${__("لا يوجد JSON محفوظ لهذه الصفحة")}</div>`;
        }
        const groups = normalize_analysis(payload).document_groups || [];
        const lines = [];
        groups.forEach((group, groupIndex) => {
            const rows = primary_rows(group);
            lines.push(`### ${__("مجموعة")} ${group.document_group_id || group.id || groupIndex + 1}`);
            lines.push(`- **${__("السطور")}**: ${rows.length}`);
            const summary = client_readable_summary(group);
            if (summary) lines.push(`- **${__("ملخص")}**: ${summary}`);
            const errors = (group.errors || []).concat(payload.errors || []).filter(Boolean).slice(0, 5);
            const warnings = (group.warnings || []).concat(payload.warnings || []).filter(Boolean).slice(0, 5);
            warnings.forEach((item) => lines.push(`- **${__("تنبيه")}**: ${markdown_cell(item)}`));
            errors.forEach((item) => lines.push(`- **${__("خطأ")}**: ${markdown_cell(item)}`));
            if (rows.length) {
                lines.push("");
                lines.push(extracted_rows_markdown(group, 12));
            }
        });
        if (lines.length) return markdown_block(lines.join("\n"));
        return `<pre class="pi-text-preview">${escape_html(JSON.stringify(payload, null, 2))}</pre>`;
    }

    function is_empty_analysis_payload(payload) {
        return !payload || typeof payload !== "object" || !Object.keys(payload).length;
    }

    function page_analysis_status_label(status) {
        const s = String(status || "").toLowerCase();
        if (s === "pending") return __("بانتظار المعالجة");
        if (s === "running") return __("قيد المعالجة");
        if (s === "completed") return __("تمت المعالجة");
        if (s === "failed") return __("فشلت المعالجة");
        if (s === "skipped") return __("تم التجاوز");
        return status || "";
    }

    function page_type_label(value) {
        const labels = {
            pdf_page: __("صفحة PDF"),
            image: __("صورة"),
            text_document: __("مستند نصي"),
            unsupported_attachment: __("ملف غير مدعوم"),
            pdf_page_limit: __("خارج الحد"),
        };
        return labels[value] || value || "";
    }

    function is_image_page(row) {
        return String(row?.page_type || "").toLowerCase() === "image";
    }

    function is_text_document_page(row) {
        return String(row?.page_type || "").toLowerCase() === "text_document";
    }

    function page_display_name(row) {
        if (is_image_page(row)) return row.file_name || __("صورة");
        if (is_text_document_page(row)) return row.file_name || __("مستند نصي");
        return `${__("صفحة")} ${row?.page || 1}`;
    }

    function file_pages_label(pages) {
        const imageCount = (pages || []).filter((row) => is_image_page(row)).length;
        if (imageCount && imageCount === (pages || []).length) {
            return imageCount === 1 ? __("صورة واحدة") : `${imageCount} ${__("صور")}`;
        }
        const textCount = (pages || []).filter((row) => is_text_document_page(row)).length;
        if (textCount && textCount === (pages || []).length) {
            return textCount === 1 ? __("مستند نصي واحد") : `${textCount} ${__("مستندات نصية")}`;
        }
        return `${(pages || []).length} ${__("صفحة")}`;
    }

    function is_image_file(row) {
        const value = String(row?.type || row?.name || "").toLowerCase();
        return [".png", ".jpg", ".jpeg", ".webp", "image/png", "image/jpeg", "image/webp"].some((item) => value.includes(item));
    }

    function file_extension_label(row) {
        const ext = extension_of(row?.name || row?.type || "");
        return (ext || __("ملف")).replace(".", "").toUpperCase();
    }

    function pending_file_thumb(row) {
        if (!is_image_file(row) || !row.file) return "IMG";
        if (!row.preview_url) row.preview_url = URL.createObjectURL(row.file);
        return `<img src="${escape_attr(row.preview_url)}" alt="">`;
    }

    function extracted_row_columns(group, rows) {
        const columns = [
            { label: __("الحساب"), value: (row) => account_review_value(row, group) },
            { label: __("المورد / الطرف"), value: (row) => journal_line_value(row, "custom_cash_supplier", group) },
            { label: __("رقم المستند"), value: (row) => journal_line_value(row, "custom_bill_no", group) },
            { label: __("تاريخ المستند"), value: (row) => journal_line_value(row, "custom_bill_date", group) },
            { label: __("الرقم الضريبي"), value: (row) => journal_line_value(row, "custom_tax_id", group) },
            { label: __("الضريبة"), value: (row) => journal_line_value(row, "custom_tax_amount", group) },
            { label: __("مدين"), value: (row) => journal_line_value(row, "debit", group) },
            { label: __("دائن"), value: (row) => journal_line_value(row, "credit", group) },
            { label: __("مركز التكلفة"), value: (row) => journal_line_value(row, "cost_center", group) },
            { label: __("البيان"), value: (row) => journal_line_value(row, "user_remark", group) },
        ];
        return columns;
    }

    function account_review_value(row, group) {
        const account = journal_line_value(row, "account", group);
        if (account) return account;
        return "";
    }

    function is_valid_supplier_string(val) {
        if (!val || typeof val === "object") return false;
        const str = String(val).trim();
        if (!str) return false;
        const clean = str.replace(/[\/\s\$\,\.]/g, "");
        if (!clean || /^\d+$/.test(clean)) return false;
        return true;
    }

    function journal_line_value(row, field, group = null) {
        const aliases = {
            account: ["account", "expense_account", "debit_account", "credit_account"],
            custom_cash_supplier: ["custom_cash_supplier", "cash_supplier", "supplier", "supplier_name", "vendor", "vendor_name", "seller", "seller_name", "merchant", "store", "store_name", "biller", "biller_name", "issued_by", "company_name"],
            custom_bill_no: ["custom_bill_no", "bill_no", "invoice_no", "invoice_number", "Invoice Number", "voucher_no", "voucher_number", "document_number", "receipt_no", "receipt_number", "reference_no"],
            custom_bill_date: ["custom_bill_date", "bill_date", "invoice_date", "invoice_number_date", "Invoice Date", "voucher_date", "document_date", "posting_date", "date"],
            custom_tax_id: ["custom_tax_id", "tax_id", "vat_number", "tax_identifier", "trn", "vat_no"],
            custom_mobile_no: ["custom_mobile_no", "mobile_no", "mobile", "phone", "phone_no", "cell_number", "contact_no"],
            custom_tax_amount: ["custom_tax_amount", "tax_amount", "vat_amount"],
            debit: ["line_total", "net_amount", "amount", "total_amount", "document_amount", "debit_in_account_currency", "debit"],
            credit: ["credit_in_account_currency", "credit"],
            cost_center: ["cost_center"],
            user_remark: ["user_remark", "description", "extracted_item_name", "item_name", "remarks", "purpose"],
        };
        const value = row_value_deep(row, aliases[field] || [field]);
        if (field === "custom_bill_no") {
            const fallback = group ? group_level_value(group, aliases.custom_bill_no) : "";
            return best_document_number(value, fallback);
        }
        if (field === "custom_cash_supplier") {
            if (is_valid_supplier_string(value)) return value;
            const groupSupplier = group ? group_supplier_name(group) : "";
            if (is_valid_supplier_string(groupSupplier)) return groupSupplier;
            return "";
        }
        if (!is_empty_value(value)) return value;
        if (!group) return "";
        if (field === "custom_bill_date") return group_level_value(group, aliases.custom_bill_date);
        if (field === "custom_tax_id") return row_value_deep(group.supplier || group.party || {}, aliases.custom_tax_id);
        return "";
    }

    function best_document_number(rowValue, fallbackValue) {
        const rowText = String(rowValue || "").trim();
        const fallbackText = String(fallbackValue || "").trim();
        if (is_plausible_document_number(rowText)) return rowText;
        if (is_plausible_document_number(fallbackText)) return fallbackText;
        return "";
    }

    function is_plausible_document_number(value) {
        const text = String(value || "").trim();
        return /\d/.test(text) && text.replace(/\s+/g, "").length >= 3;
    }

    function row_value_deep(row, keys) {
        const containers = [row, row?.journal_entry_line, row?.accounting_line, row?.line, row?.document, row?.invoice, row?.source];
        for (const container of containers) {
            if (!container || typeof container !== "object") continue;
            for (const key of keys) {
                const direct = get_object_value(container, key);
                if (!is_empty_value(direct)) return direct;
            }
        }
        return "";
    }

    function group_level_value(group, keys) {
        const containers = [
            group,
            group?.invoice,
            group?.document,
            group?.metadata,
            ...(group?.files || []),
            ...(group?.files || []).flatMap((file) => file?.pages || []),
        ];
        for (const container of containers) {
            const value = row_value_deep(container, keys);
            if (!is_empty_value(value)) return value;
        }
        const textValue = group_text_value(group, keys);
        if (!is_empty_value(textValue)) return textValue;
        return "";
    }

    function group_text_value(group, keys) {
        const text = [
            group?.client_readable_summary,
            group?.summary,
            group?.invoice?.summary,
            group?.document?.summary,
            group?.invoice?.remarks,
            group?.document?.remarks,
        ].filter(Boolean).join("\n");
        if (!text) return "";
        const normalizedKeys = keys.map(normalize_key);
        if (normalizedKeys.some((key) => ["custombillno", "billno", "invoiceno", "invoicenumber", "documentnumber", "voucherno", "receiptno", "referenceno"].includes(key))) {
            return extract_labeled_value(text, [
                "Invoice Number", "رقم الفاتورة", "رقم فاتورة المورد", "Bill No", "Document Number", "Voucher No", "Receipt No",
            ]);
        }
        if (normalizedKeys.some((key) => ["custombilldate", "billdate", "invoicedate", "documentdate", "voucherdate"].includes(key))) {
            return extract_labeled_value(text, [
                "Invoice Date", "تاريخ الفاتورة", "تاريخ المستند", "Bill Date", "Document Date", "Voucher Date",
            ]);
        }
        return "";
    }

    function extract_labeled_value(text, labels) {
        for (const label of labels) {
            const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            const match = String(text).match(new RegExp(`${escaped}(?:\\s*\\([^)]*\\))?\\s*[:：-]\\s*([^\\n|،,]+)`, "i"));
            if (match && match[1]) return match[1].trim();
        }
        return "";
    }

    function get_object_value(container, key) {
        if (!container || typeof container !== "object") return "";
        if (!is_empty_value(container[key])) return container[key];
        const normalizedKey = normalize_key(key);
        for (const [candidate, value] of Object.entries(container)) {
            if (normalize_key(candidate) === normalizedKey && !is_empty_value(value)) return value;
        }
        return "";
    }

    function normalize_key(key) {
        return String(key || "").toLowerCase().replace(/[^a-z0-9\u0600-\u06ff]+/g, "");
    }

    function group_supplier_name(group) {
        const supplier = group?.supplier || {};
        return supplier.extracted_name || supplier.supplier_name || supplier.matched_supplier || "";
    }

    function row_value(row, keys, fallback = "") {
        for (const key of keys) {
            if (!is_empty_value(row?.[key])) return row[key];
        }
        return fallback || "";
    }

    function is_empty_value(value) {
        return value === undefined || value === null || value === "" || (typeof value === "number" && !Number.isFinite(value));
    }

    function is_zero_value(value) {
        return value === 0 || value === "0" || value === "0.0" || value === "0.00";
    }

    function is_hidden_display_key(key) {
        return contract_array("hidden_display_keys", []).includes(key);
    }

    function display_value(value) {
        if (typeof value === "number") return format_number(value);
        return String(value || "");
    }

    function markdown_cell(value) {
        return display_value(value).replace(/\|/g, "/").replace(/\n/g, " ").trim();
    }

    function field_label(key) {
        const configured = Array.isArray(uiContract.line_display) ? uiContract.line_display : [];
        const match = configured.find((row) => row && row.key === key);
        const labels = {
            tax_identifier: __("الرقم الضريبي"),
            document_date: __("التاريخ"),
            document_number: __("رقم المستند"),
            document_amount: __("المبلغ"),
            custom_tax_id: __("الرقم الضريبي"),
            custom_bill_date: __("التاريخ"),
            custom_bill_no: __("رقم المستند"),
        };
        return match?.label || labels[key] || String(key || "").replace(/^custom_/, "").replace(/_/g, " ");
    }

    function strip_text_fence(value) {
        return String(value || "")
            .replace(/^```(?:text)?\s*/i, "")
            .replace(/\s*```$/i, "")
            .trim();
    }

    function contract_text(key, fallback) {
        const value = uiContract?.[key];
        return is_empty_value(value) ? fallback : value;
    }

    function contract_array(key, fallback) {
        const value = uiContract?.[key];
        return Array.isArray(value) && value.length ? value : fallback;
    }

    function contract_stages() {
        return contract_array("stages", [
            { key: "upload", label: __("الرفع والاستخراج") },
            { key: "process", label: __("المعالجة") },
            { key: "review", label: __("الاستعراض والإنشاء") },
        ]);
    }

    function validation_markdown(errors, warnings) {
        const parts = [];
        if (errors.length) {
            parts.push(`### ${__("الأخطاء")}`);
            parts.push(...errors.map((row) => `- ${row}`));
        }
        if (warnings.length) {
            parts.push(`### ${__("التحذيرات")}`);
            parts.push(...warnings.map((row) => `- ${row}`));
        }
        if (!parts.length) return __("لا توجد أخطاء أو تحذيرات مانعة.");
        return parts.join("\n");
    }

    function run_log_markdown() {
        if (!lastSteps.length) return __("لم تصل خطوات تنفيذ بعد.");
        return [
            `### ${__("سجل العمل")}`,
            "",
            ...lastSteps.slice(-8).map((step) => `- **${step.sequence}. ${step.title || step.step_type}**: ${step.status || ""}`),
        ].join("\n");
    }

    function no_data_diagnostics_markdown() {
        const rows = page_diagnostic_rows(first_group(), 20);
        if (!rows.length) return "";
        return [
            "",
            `| ${__("الملف")} | ${__("الصفحة")} | ${__("السطور")} | ${__("النص المقروء")} | ${__("ملاحظة")} |`,
            "|---|---:|---:|---|---|",
            ...rows,
        ].join("\n");
    }

    function page_diagnostic_rows(group, limit) {
        const rows = [];
        for (const file of group?.files || []) {
            for (const page of file.pages || []) {
                const preview = shorten((page.text_preview || "").replace(/\s+/g, " "), 180);
                const metadata = Object.entries(page.metadata || {})
                    .filter(([, value]) => !is_empty_value(value))
                    .map(([key, value]) => `${field_label(key)}=${display_value(value)}`)
                    .join("، ");
                const note = [(page.warnings || []).join("، "), metadata].filter(Boolean).join("، ");
                rows.push(`| ${markdown_cell(file.file_name || "")} | ${markdown_cell(page.page || "")} | ${markdown_cell(page.extracted_lines_count || page.row_count || (page.rows || []).length || 0)} | ${markdown_cell(preview || __("لم يرجع OCR نصًا واضحًا"))} | ${markdown_cell(note)} |`);
                if (rows.length >= limit) return rows;
            }
        }
        return rows;
    }

    function shorten(value, maxLength) {
        const text = String(value || "").trim();
        if (text.length <= maxLength) return text;
        return `${text.slice(0, maxLength).trim()}...`;
    }

    function render_working_chat() {
        const group = first_group();
        const rows = group?.items || [];
        const totals = group?.totals || {};
        const stageLabel = {
            new: __("جاهز لاستقبال المرفقات"),
            extracted: __("تم استخراج البيانات"),
            matching: __("جاري التحليل والمطابقة"),
            analyzed: __("اكتمل التحليل والمطابقة"),
        }[analysisStage] || __("قيد العمل");
        const assistantText = analysisStage === "extracted"
            ? __("استخرجت البيانات من المرفقات فقط. راجعها وعدّلها، ثم اضغط تحليل ومطابقة.")
            : analysisStage === "analyzed"
                ? __("تمت المطابقة مع النظام. راجع التحقق ثم أنشئ مسودة القيد عند الجاهزية.")
                : analysisStage === "matching"
                    ? __("أستخدم البيانات التي راجعتها للمطابقة والتحقق مع النظام.")
                    : __("أضف المرفقات ثم اضغط استخراج البيانات لعرض القراءة الأولية.");
        page.main.find("[data-working-chat]").html(`
            <div class="pi-work-head">
                <span>${escape_html(stageLabel)}</span>
                <b>${rows.length} ${__("سطر")}</b>
            </div>
            <div class="pi-chat-row user">
                <div class="pi-avatar">Y</div>
                <div class="pi-bubble">
                    <b>${__("المستخدم")}</b>
                    <span>${files.length ? __("تم رفع المرفقات للمراجعة") : __("بانتظار رفع المرفقات")}</span>
                </div>
            </div>
            <div class="pi-chat-row ai">
                <div class="pi-avatar">AI</div>
                <div class="pi-bubble">
                    <b>${__("Journal Entry AI")}</b>
                    <span>${escape_html(assistantText)}</span>
                    ${rows.length ? `
                        <div class="pi-chat-metrics">
                            <span>${__("ملفات")}: <b>${(group.files || []).length || files.length}</b></span>
                            <span>${__("سطور")}: <b>${rows.length}</b></span>
                            <span>${__("قبل الضريبة")}: <b>${format_number(totals.subtotal)}</b></span>
                            <span>${__("الضريبة")}: <b>${format_number(totals.tax_amount)}</b></span>
                            <span>${__("الإجمالي")}: <b>${format_number(totals.grand_total)}</b></span>
                        </div>
                    ` : ""}
                </div>
            </div>
        `);
    }

    function render_header() {
        const panel = page.main.find("[data-panel='header']");
        const group = first_group();
        if (!group) {
            panel.html(`<div class="pi-empty">${__("ستظهر البيانات المستخرجة بعد رفع المرفقات والضغط على استخراج البيانات")}</div>`);
            return;
        }
        const supplier = group.supplier || {};
        const invoice = group.invoice || {};
        panel.html(`
            ${review_summary(group)}
            <div class="pi-header-grid">
                ${field("invoice_groups.0.supplier.extracted_name", __("المورد المستخرج"), supplier.extracted_name)}
                ${field("invoice_groups.0.supplier.tax_id", __("الرقم الضريبي"), supplier.tax_id)}
                ${field("invoice_groups.0.invoice.bill_no", __("رقم فاتورة المورد"), group_level_value(group, ["bill_no", "invoice_no", "invoice_number", "Invoice Number", "document_number", "voucher_no", "receipt_no"]))}
                ${field("invoice_groups.0.invoice.bill_date", __("تاريخ الفاتورة"), invoice.bill_date)}
                ${field("invoice_groups.0.invoice.posting_date", __("تاريخ القيد"), invoice.posting_date)}
                ${field("invoice_groups.0.invoice.due_date", __("تاريخ الاستحقاق"), invoice.due_date)}
                ${field("invoice_groups.0.invoice.currency", __("العملة"), invoice.currency)}
                ${field("invoice_groups.0.invoice.purchase_order", __("أمر الشراء"), invoice.purchase_order)}
                ${analysisStage !== "extracted" ? field("invoice_groups.0.supplier.matched_supplier", __("المورد المطابق"), supplier.matched_supplier) : ""}
                ${field("invoice_groups.0.invoice.remarks", __("ملاحظات"), invoice.remarks)}
            </div>
            <div class="pi-total-bar" data-total-bar></div>
        `);
    }

    function review_summary(group) {
        const rows = group.items || [];
        const totals = group.totals || {};
        const filesCount = (group.files || []).length || files.length;
        const stageText = analysisStage === "extracted"
            ? __("بيانات مستخرجة من المرفقات فقط")
            : __("بيانات محللة ومطابقة مع النظام");
        const hint = analysisStage === "extracted"
            ? __("راجع سطور القيد والطرف والحساب والمدين والدائن. بعد التصحيح اضغط تحليل ومطابقة.")
            : __("راجع نتائج المطابقة والتحقق قبل إنشاء مسودة القيد.");
        return `
            <section class="pi-review-summary">
                <div>
                    <b>${escape_html(stageText)}</b>
                    <span>${escape_html(hint)}</span>
                </div>
                <div class="pi-review-metrics">
                    <span>${__("الملفات")}: <b>${filesCount}</b></span>
                    <span>${__("السطور")}: <b>${rows.length}</b></span>
                    <span>${__("قبل الضريبة")}: <b>${format_number(totals.subtotal)}</b></span>
                    <span>${__("الضريبة")}: <b>${format_number(totals.tax_amount)}</b></span>
                    <span>${__("الإجمالي")}: <b>${format_number(totals.grand_total)}</b></span>
                </div>
            </section>
        `;
    }

    function field(path, label, value) {
        return `
            <label class="pi-review-field">
                <span>${escape_html(label)}</span>
                <input class="form-control" data-analysis-path="${escape_attr(path)}" value="${escape_attr(value || "")}">
            </label>
        `;
    }

    function render_items(filter = "all") {
        const panel = page.main.find("[data-panel='items']");
        const group = first_group();
        const rows = group?.items || [];
        if (!group) {
            panel.html(`<div class="pi-empty">${__("ستظهر سطور القيد المستخرجة بعد رفع المرفقات والضغط على استخراج البيانات")}</div>`);
            return;
        }
        const visible = rows.filter((row) => {
            if (filter === "unmatched") return !row.item_code && !row.matched_item_code;
            if (filter === "errors") return String(row.validation_status || "").toLowerCase() === "error";
            return true;
        });
        panel.html(`
            ${items_review_note(rows)}
            <div class="pi-table-tools">
                <button class="btn btn-xs btn-default" data-filter-items="all">${__("الكل")} (${rows.length})</button>
                ${analysisStage !== "extracted" ? `<button class="btn btn-xs btn-default" data-filter-items="unmatched">${__("غير المطابقة")}</button>` : ""}
                ${analysisStage !== "extracted" ? `<button class="btn btn-xs btn-default" data-filter-items="errors">${__("أخطاء")}</button>` : ""}
            </div>
            <div class="pi-items-wrap">
                <table class="pi-items-table">
                    <thead>
                        <tr>
                            ${item_headers().map((h) => `<th>${escape_html(h)}</th>`).join("")}
                        </tr>
                    </thead>
                    <tbody>
                        ${visible.slice(0, 500).map((row) => item_row(row, rows.indexOf(row))).join("")}
                    </tbody>
                </table>
            </div>
            ${visible.length > 500 ? `<div class="pi-note">${__("يعرض أول 500 صف للحفاظ على الأداء.")}</div>` : ""}
        `);
    }

    function items_review_note(rows) {
        if (analysisStage !== "extracted") return "";
        return `
            <div class="pi-review-summary compact">
                <div>
                    <b>${__("مراجعة سطور القيد المستخرجة")}</b>
                    <span>${__("هذه البيانات من المرفقات فقط. عدّل الطرف أو الحساب أو البيان أو المدين والدائن قبل إرسالها للمطابقة مع النظام.")}</span>
                </div>
                <div class="pi-review-metrics">
                    <span>${__("عدد السطور")}: <b>${rows.length}</b></span>
                </div>
            </div>
        `;
    }

    function item_headers() {
        const headers = [
            "#",
            __("المورد/الطرف"),
            __("رقم الفاتورة/السند"),
            __("تاريخ الفاتورة/السند"),
            __("الرقم الضريبي"),
            __("الحساب"),
            __("البيان"),
            __("الكمية"),
            __("الوحدة"),
            __("السعر"),
            __("الضريبة"),
            __("مدين"),
            __("دائن"),
            __("المرفق"),
        ];
        if (analysisStage !== "extracted") {
            headers.splice(6, 0, __("المرجع"));
            headers.push(__("المطابقة"));
        }
        return headers;
    }

    function item_row(row, index) {
        const base = `${primary_rows_path()}.${index}`;
        const group = first_group() || {};
        if (analysisStage === "extracted") {
            return `
                <tr>
                    <td>${index + 1}</td>
                    <td><input data-analysis-path="${base}.custom_cash_supplier" value="${escape_attr(journal_line_value(row, "custom_cash_supplier", group))}"></td>
                    <td><input data-analysis-path="${base}.custom_bill_no" value="${escape_attr(journal_line_value(row, "custom_bill_no", group))}"></td>
                    <td><input data-analysis-path="${base}.custom_bill_date" value="${escape_attr(journal_line_value(row, "custom_bill_date", group))}"></td>
                    <td><input data-analysis-path="${base}.custom_tax_id" value="${escape_attr(row_value(row, ["custom_tax_id", "tax_id", "vat_number"], group.supplier?.tax_id))}"></td>
                    <td><input data-analysis-path="${base}.account" value="${escape_attr(row.account || row.expense_account || "")}"></td>
                    <td><input data-analysis-path="${base}.extracted_item_name" value="${escape_attr(row.extracted_item_name || row.description || row.user_remark || "")}"></td>
                    <td><input type="number" data-analysis-path="${base}.qty" value="${escape_attr(row.qty || 0)}"></td>
                    <td><input data-analysis-path="${base}.uom" value="${escape_attr(row.uom || "")}"></td>
                    <td><input type="number" data-analysis-path="${base}.rate" value="${escape_attr(row.rate || 0)}"></td>
                    <td><input type="number" data-analysis-path="${base}.custom_tax_amount" value="${escape_attr(row.custom_tax_amount || row.tax_amount || 0)}"></td>
                    <td><input type="number" data-analysis-path="${base}.debit" value="${escape_attr(row.debit || row.line_total || row.net_amount || (Number(row.qty || 0) * Number(row.rate || 0)))}"></td>
                    <td><input type="number" data-analysis-path="${base}.credit" value="${escape_attr(row.credit || 0)}"></td>
                    <td>${escape_html(row.source_file || "")}</td>
                </tr>
            `;
        }
        return `
            <tr>
                <td>${index + 1}</td>
                <td><input data-analysis-path="${base}.custom_cash_supplier" value="${escape_attr(journal_line_value(row, "custom_cash_supplier", group))}"></td>
                <td><input data-analysis-path="${base}.custom_bill_no" value="${escape_attr(journal_line_value(row, "custom_bill_no", group))}"></td>
                <td><input data-analysis-path="${base}.custom_bill_date" value="${escape_attr(journal_line_value(row, "custom_bill_date", group))}"></td>
                <td><input data-analysis-path="${base}.custom_tax_id" value="${escape_attr(row_value(row, ["custom_tax_id", "tax_id", "vat_number"], group.supplier?.tax_id))}"></td>
                <td><input data-analysis-path="${base}.account" value="${escape_attr(row.account || row.expense_account || "")}"></td>
                <td><input data-analysis-path="${base}.extracted_item_name" value="${escape_attr(row.extracted_item_name || row.description || row.user_remark || "")}"></td>
                <td><input data-analysis-path="${base}.item_code" value="${escape_attr(row.item_code || row.matched_item_code || "")}"></td>
                <td><input type="number" data-analysis-path="${base}.qty" value="${escape_attr(row.qty || 0)}"></td>
                <td><input data-analysis-path="${base}.uom" value="${escape_attr(row.uom || "")}"></td>
                <td><input type="number" data-analysis-path="${base}.rate" value="${escape_attr(row.rate || 0)}"></td>
                <td><input type="number" data-analysis-path="${base}.custom_tax_amount" value="${escape_attr(row.custom_tax_amount || row.tax_amount || 0)}"></td>
                <td><input type="number" data-analysis-path="${base}.debit" value="${escape_attr(row.debit || row.line_total || row.net_amount || (Number(row.qty || 0) * Number(row.rate || 0)))}"></td>
                <td><input type="number" data-analysis-path="${base}.credit" value="${escape_attr(row.credit || 0)}"></td>
                <td>${escape_html(row.source_file || "")}</td>
                <td>${escape_html(row.match_status || row.match_method || "")}</td>
            </tr>
        `;
    }

    function render_validation() {
        const panel = page.main.find("[data-panel='validation']");
        const group = first_group();
        if (!group) {
            panel.html(`<div class="pi-empty">${__("لا توجد نتائج تحقق بعد")}</div>`);
            return;
        }
        if (analysisStage === "extracted") {
            panel.html(`
                <div class="pi-stage-card">
                    <b>${__("لم تبدأ المطابقة بعد")}</b>
                    <span>${__("البيانات المعروضة حاليًا هي سطور قيد مستخرجة من المرفقات فقط. بعد مراجعتها اضغط تحليل ومطابقة لفحص الأطراف والحسابات مع النظام.")}</span>
                </div>
            `);
            return;
        }
        const errors = group.errors || [];
        const warnings = group.warnings || [];
        panel.html(`
            <div class="pi-validation-grid">
                <section>
                    <h4>${__("الأخطاء")}</h4>
                    ${errors.length ? errors.map((e) => `<div class="pi-issue error">${escape_html(String(e))}</div>`).join("") : `<div class="pi-ok">${__("لا توجد أخطاء مانعة")}</div>`}
                </section>
                <section>
                    <h4>${__("التحذيرات")}</h4>
                    ${warnings.length ? warnings.map((w) => `<div class="pi-issue warning">${escape_html(String(w))}</div>`).join("") : `<div class="pi-ok">${__("لا توجد تحذيرات")}</div>`}
                </section>
            </div>
        `);
    }

    function render_totals() {
        const group = first_group();
        if (!group) return;
        const totals = group.totals || {};
        page.main.find("[data-total-bar]").html(`
            <span>${__("Subtotal")}: <b>${format_number(totals.subtotal)}</b></span>
            <span>${__("Tax")}: <b>${format_number(totals.tax_amount)}</b></span>
            <span>${__("Grand Total")}: <b>${format_number(totals.grand_total)}</b></span>
        `);
    }

    function render_log(steps, run) {
        lastSteps = steps || [];
        lastRun = run || {};
        const panel = page.main.find("[data-panel='log']");
        if (!panel.length) {
            render_analysis();
            return;
        }
        panel.html(`
            <div class="pi-run-summary">
                <span>${__("Run")}: <b>${escape_html(currentRun || "")}</b></span>
                <span>${__("Status")}: <b>${escape_html(run.status || "")}</b></span>
                <span>${__("Tools")}: <b>${run.tool_call_count || 0}</b></span>
                <span>${__("Tokens")}: <b>${(run.input_tokens || 0) + (run.output_tokens || 0)}</b></span>
            </div>
            <div class="pi-step-list">
                ${(steps || []).map((step) => `
                    <div class="pi-step">
                        <b>${escape_html(step.sequence)}. ${escape_html(step.title || step.step_type)}</b>
                        <span>${escape_html(step.status || "")}</span>
                        ${step.summary ? `<small>${escape_html(step.summary)}</small>` : ""}
                    </div>
                `).join("")}
            </div>
        `);
    }

    async function create_drafts() {
        if (!analysis) return;
        set_busy(true, __("Preparing Journal Entry"));
        try {
            const result = await frappe.xcall("ai_agent.purchase_invoice_ai.api.prepare_journal_entry_analysis", {
                company: companyControl?.get_value() || "",
                analysis: JSON.stringify(analysis),
            });
            if (result?.analysis) {
                analysis = normalize_analysis(result.analysis);
                render_all();
            }
        } catch (error) {
            set_busy(false, __("Failed"));
            push_agent_chat_message([
                `### ${__("تعذر تجهيز القيد")}`,
                "",
                error_message(error, __("تعذر تجهيز الموردين والحسابات قبل فتح القيد.")),
            ].join("\n"), "process");
            return;
        }
        set_busy(false, __("Ready"));
        const group = first_group();
        const rows = primary_rows(group);
        if (!rows.length) {
            push_agent_chat_message([
                `### ${__("لا يمكن فتح القيد الآن")}`,
                "",
                __("لا توجد سطور قابلة للترحيل إلى قيد يومية."),
                "",
                __("ارجع إلى مرحلة الاستخراج أو المعالجة حتى تظهر السطور أولًا."),
            ].join("\n"), "process");
            return;
        }
        const readiness = journal_entry_readiness(group);
        if (!readiness.ready) {
            push_agent_chat_message([
                `### ${__("سيتم فتح القيد مع بيانات تحتاج مراجعة")}`,
                "",
                ...readiness.messages.map((item) => `- ${item}`),
                "",
                __("فتحت لك القيد كمسودة حتى تكمل الحقول الناقصة وتراجعها قبل الحفظ."),
            ].join("\n"), "process");
        }
        frappe.model.with_doctype("Journal Entry", () => {
            frappe.model.with_doctype("Journal Entry Account", () => {
                const doc = frappe.model.get_new_doc("Journal Entry");
                doc.company = companyControl?.get_value() || doc.company;
                doc.posting_date = first_value_from_rows(rows, ["posting_date", "document_date", "custom_bill_date", "bill_date", "invoice_date", "voucher_date"]) || frappe.datetime.get_today();
                doc.user_remark = journal_entry_remark(rows);
                rows.forEach((row) => add_journal_entry_account(doc, row, group));
                frappe.set_route("Form", "Journal Entry", doc.name);
                setTimeout(() => {
                    if (cur_frm?.doc?.name === doc.name) {
                        cur_frm.refresh_field("accounts");
                        cur_frm.refresh_fields();
                    }
                }, 400);
            });
        });
    }

    function journal_entry_readiness(group) {
        const missingAccount = [];
        const needsSupplier = [];
        primary_rows(group).forEach((row, index) => {
            const lineNo = index + 1;
            if (!allowOpenWithMissingAccounts && !journal_line_value(row, "account", group)) missingAccount.push(lineNo);
            const supplierAction = String(row.supplier_action || "").toLowerCase();
            if (supplierAction === "needs_user_choice") needsSupplier.push(lineNo);
        });
        const messages = [];
        if (missingAccount.length) {
            messages.push(`${__("الحساب غير محدد في")} ${missingAccount.length} ${__("سطر/سطور")}. ${__("شغّل المعالجة والمطابقة أو اختر الحسابات من جدول المراجعة قبل فتح القيد.")}`);
            messages.push(`${__("أول السطور المتأثرة")}: ${missingAccount.slice(0, 12).join(", ")}${missingAccount.length > 12 ? "..." : ""}`);
        }
        if (needsSupplier.length) {
            messages.push(`${__("المورد يحتاج تأكيد في")} ${needsSupplier.length} ${__("سطر/سطور")}.`);
            messages.push(`${__("أول السطور المتأثرة")}: ${needsSupplier.slice(0, 12).join(", ")}${needsSupplier.length > 12 ? "..." : ""}`);
        }
        return { ready: !messages.length, messages };
    }

    function add_journal_entry_account(doc, source, group) {
        const row = frappe.model.add_child(doc, "Journal Entry Account", "accounts");
        row.account = journal_line_value(source, "account", group);
        row.debit_in_account_currency = flt(journal_line_value(source, "debit", group));
        row.credit_in_account_currency = flt(journal_line_value(source, "credit", group));
        row.debit = row.debit_in_account_currency;
        row.credit = row.credit_in_account_currency;
        row.user_remark = journal_line_value(source, "user_remark", group);
        set_child_if_field(row, "party_type", source.party_type);
        set_child_if_field(row, "party", source.party || source.supplier || source.customer || source.payee);
        set_child_if_field(row, "cost_center", journal_line_value(source, "cost_center", group));
        set_child_if_field(row, "reference_type", source.reference_type);
        set_child_if_field(row, "reference_name", source.reference_name);
        set_child_if_field(row, "custom_cash_supplier", journal_line_value(source, "custom_cash_supplier", group));
        set_child_if_field(row, "custom_bill_no", journal_line_value(source, "custom_bill_no", group));
        set_child_if_field(row, "custom_bill_date", journal_line_value(source, "custom_bill_date", group));
        set_child_if_field(row, "custom_tax_id", journal_line_value(source, "custom_tax_id", group));
        set_child_if_field(row, "custom_tax_amount", journal_line_value(source, "custom_tax_amount", group));
    }

    function set_child_if_field(row, fieldname, value) {
        if (is_empty_value(value)) return;
        row[fieldname] = value;
    }

    function first_value_from_rows(rows, keys) {
        for (const row of rows || []) {
            const value = row_value(row, keys);
            if (!is_empty_value(value)) return value;
        }
        return "";
    }

    function journal_entry_remark(rows) {
        const count = rows.length;
        const docs = rows
            .map((row) => journal_line_value(row, "custom_bill_no", first_group()))
            .filter(Boolean)
            .slice(0, 8)
            .join(", ");
        return [
            __("تم تجهيز هذا القيد من تحليل المرفقات للمراجعة قبل الحفظ."),
            currentRun ? `${__("Run")}: ${currentRun}` : "",
            count ? `${__("عدد السطور")}: ${count}` : "",
            docs ? `${__("المستندات")}: ${docs}` : "",
        ].filter(Boolean).join("\n");
    }

    function reset_workspace() {
        next_workspace_version();
        stop_polling();
        files = [];
        analysis = null;
        currentRun = null;
        extractionRun = null;
        analysisStage = "new";
        extractionMessage = "";
        activeStep = "files";
        lastRun = {};
        lastSteps = [];
        discussionMessages = [];
        activeFileIndex = -1;
        sessionName = "";
        pageManifest = [];
        selectedPages = new Set();
        activePageId = "";
        activeFileId = "";
        pagePreviewCache = {};
        pagePreviewLoading = new Set();
        collapsedFileIds = new Set();
        allowOpenWithMissingAccounts = false;
        localStorage.removeItem("financial_attachment_session");
        page.main.find("[data-action='reextract'], [data-action='open_agent_workspace']").prop("disabled", true);
        update_status(__("New"));
        render_all();
        render_log([], {});
        frappe.show_alert({ message: __("تم بدء جلسة جديدة بنجاح."), indicator: "green" });
    }

    function first_group() {
        const groups = analysis_groups();
        if (!groups.length) return null;
        if (groups.length === 1) return groups[0];
        const merged = {
            document_group_id: "ALL",
            files: [],
            warnings: [],
            errors: [],
        };
        const rowKeys = ["journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"];
        for (const group of groups) {
            merged.files.push(...(group.files || []));
            merged.warnings.push(...(group.warnings || []));
            merged.errors.push(...(group.errors || []));
            if (group.client_readable_summary) {
                merged.client_readable_summary = [merged.client_readable_summary, group.client_readable_summary].filter(Boolean).join("\n\n");
            }
            for (const key of rowKeys) {
                if (Array.isArray(group[key])) {
                    if (!Array.isArray(merged[key])) merged[key] = [];
                    merged[key].push(...group[key]);
                }
            }
        }
        return merged;
    }

    function set_analysis_value(path, value) {
        if (!analysis) return;
        const parts = String(path || "").split(".");
        let target = analysis;
        for (let i = 0; i < parts.length - 1; i++) {
            const key = parts[i];
            const nextKey = parts[i + 1];
            if (Array.isArray(target)) target = target[Number(key)];
            else {
                if (!target[key]) target[key] = /^\d+$/.test(nextKey) ? [] : {};
                target = target[key];
            }
            if (!target) return;
        }
        const last = parts[parts.length - 1];
        if (Array.isArray(target)) target[Number(last)] = cast_value(value);
        else target[last] = cast_value(value);
    }

    function cast_value(value) {
        const text = String(value ?? "");
        if (text !== "" && !Number.isNaN(Number(text)) && /^-?\d+(\.\d+)?$/.test(text)) return Number(text);
        return value;
    }

    function update_counts() {
        page.main.find("[data-pi-file-count]").text(files.length);
        const groups = analysis_groups();
        if (groups.length) {
            page.main.find("[data-pi-group-count]").text(groups.length);
            return;
        }
        const fileGroups = new Set(files.map((row) => row.group || "GROUP-001"));
        page.main.find("[data-pi-group-count]").text(files.length ? fileGroups.size : 0);
    }

    function update_status(status) {
        page.main.find("[data-pi-status]").text(__(status || "New"));
    }

    function set_busy(isBusy, status) {
        update_status(status);
        page.main.find("[data-action='extract'], [data-action='reextract']").prop("disabled", isBusy);
        page.main.find("[data-action='read_selected_pages']").prop("disabled", isBusy || !selectedPages.size);
        page.main.find("[data-action='open_agent_workspace']").prop("disabled", isBusy || !sessionName);
    }

    function activate_tab(tab) {
        page.main.find(".pi-tabs button").removeClass("active");
        page.main.find(`.pi-tabs button[data-tab='${tab}']`).addClass("active");
        page.main.find(".pi-tab-panel").removeClass("active");
        page.main.find(`[data-panel='${tab}']`).addClass("active");
    }

    function file_to_payload(row) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve({ filename: row.name, content_base64: reader.result });
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(row.file);
        });
    }

    function extension_of(name) {
        const index = String(name || "").lastIndexOf(".");
        return index >= 0 ? String(name).slice(index).toLowerCase() : "";
    }

    function format_size(bytes) {
        const value = Number(bytes || 0);
        if (value > 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
        if (value > 1024) return `${(value / 1024).toFixed(1)} KB`;
        return `${value} B`;
    }

    function format_number(value) {
        return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    function flt(value) {
        const normalized = String(value ?? "").replace(/,/g, "").trim();
        const number = Number(normalized);
        return Number.isFinite(number) ? number : 0;
    }

    function error_message(error, fallback) {
        if (!error) return fallback;
        if (typeof error === "string") return error;
        if (error.message) return error.message;
        if (error.exc) return error.exc;
        const serverMessages = error._server_messages || error.responseJSON?._server_messages;
        if (serverMessages) {
            try {
                const messages = JSON.parse(serverMessages)
                    .map((item) => JSON.parse(item).message)
                    .filter(Boolean);
                return messages.join("<br>") || fallback;
            } catch (e) {
                return fallback;
            }
        }
        return fallback;
    }

    function escape_html(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function escape_attr(value) {
        return escape_html(value);
    }
};
