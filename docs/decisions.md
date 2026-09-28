# سجل القرارات — ai_agent

كل قرار اتُّخذ أثناء التنفيذ دون تعليمات صريحة، مع سببه. الأحدث في الأسفل ضمن كل مرحلة.

---

## المرحلة A — ثغرات الصلاحيات (2026-09-28)

### A-01: فحص الصلاحية على مستوى الصف عبر `frappe.has_permission(doc=...)`
أدوات القراءة (`get_document`، `get_child_records`) تُحمِّل المستند ثم تفحص `frappe.has_permission(doctype, "read", doc=doc, user=run_user)`. هذا يطبق User Permissions وif_owner والمشاركة وhooks الصلاحيات معاً.
المستند غير الموجود والمستند المحجوب يعيدان **نفس** الخطأ (`Document not found or not accessible.`)، فلا يمكن استكشاف وجود مستندات خارج النطاق.

### A-02: permlevel يُحسب من `Meta.get_permlevel_access` بدل قراءة DocPerm يدوياً
الكود السابق كان يقرأ `DocPerm` فقط، فيتجاهل `Custom DocPerm`، ويفحص permlevel الجدول الفرعي على صلاحيات الجدول الفرعي نفسه (وهي فارغة دائماً). الآن يُمرَّر `parent_doctype` ويُقرأ permlevel من صلاحيات الأب، كما يفعل Frappe v16.

### A-03: `data.query` بالتجميع يقيَّد بـ subquery من `frappe.qb.get_query(user=...)`
المسار المجمَّع/المربوط كان يطبق User Permissions يدوياً ويستثني System Manager كلياً. الآن يُضاف شرط `name IN (frappe.qb.get_query(doctype, fields=["name"], ignore_permissions=False, user=...))`. بذلك يطبق Frappe نفسه كل القواعد، ولا يُستثنى أي دور (لـ System Manager أيضاً User Permissions في Frappe). المسار البسيط كان يستخدم `frappe.qb.get_query(user=...)` أصلاً، وهو محرك `get_list` نفسه في v16، فبقي كما هو.

### A-04: `data.count` عبر `frappe.get_list` مع `{"COUNT": "name"}`
v16 يرفض `"count(name) as c"` كنص ويشترط صيغة القاموس. العدد المُعاد دقيق وبلا حد أعلى: التجميع يتم في SQL فلا حاجة لجلب الأسماء.

### A-05: التحقق من حقول الكتابة داخل `ApprovalEngine` نفسه
كل مسارات الكتابة (tools/actions.py، ActionInterceptor، MCP) تمر عبر `ApprovalEngine.create_approval_request`، والتنفيذ عبر `execute_approved_action`. لذلك وُضع التحقق في الاثنين (`assert_action_allowed`):
- **عند الطلب**: صلاحيات صاحب التشغيل.
- **عند التنفيذ**: صلاحيات المعتمِد (هو من ينفذ فعلياً)، مع إعادة فحص الحقول والمستند.

ActionInterceptor كان ينشئ طلبات Update/Submit/Cancel/Workflow **بلا أي فحص صلاحية**. هذا مُغلق الآن.

### A-06: التحديث يرفض الحقول المحظورة ولا يحذفها بصمت
عند وجود حقل محظور يُرفض الطلب كاملاً برسالة تسمّي الحقول، فيفهم النموذج السبب ويعيد المحاولة بدونها. الحذف الصامت كان سيُنتج مستنداً مختلفاً عما وافق عليه المستخدم.
المحظور في التحديث: `docstatus`، `name`، `owner`، `creation`، `modified`، `modified_by`، `amended_from`، `idx`، `doctype`، حقول الأب، أي حقل `read_only` أو `hidden`، أي حقل خارج permlevel الكتابة للمستخدم، وأي حقل غير موجود في الـ DocType. تُطبَّق نفس القواعد على صفوف الجداول الفرعية (يُسمح فيها بـ `name` لتحديد الصف).

### A-07: الإنشاء أخف من التحديث في فحص الحقول
في الإنشاء يُرفض: `docstatus` ≠ 0 (مسودة فقط)، حقول التدقيق/دورة الحياة، الحقول خارج permlevel، و`payload.doctype` المختلف عن الـ DocType المعتمد. هذا الأخير كان ثغرة: الفحص يتم على `doctype_name` بينما يُنشأ المستند من `payload["doctype"]`.
أما حقول `read_only`/`fetch_from` فمسموحة في الإنشاء: Frappe يعيد حسابها عند الإدراج، والحمولات المنقولة من مستندات أخرى (ومنها حمولات فواتير الشراء الحالية) تحملها مشروعاً.

### A-08: `execute_run` يرفض التشغيل إن لم تكن الجلسة هي صاحب التشغيل
الأدوات تقرأ بجلسة Frappe (`get_list`، `has_permission`). أي مسار ينفذ تشغيلاً بجلسة مستخدم آخر (مثل مهمة خلفية بجلسة Administrator) كان سيقرأ بصلاحيات خاطئة، وكان هذا واقعاً فعلاً في WhatsApp. الحارس مركزي في `orchestrator.execute_run`: يرفض ويسجل الخطأ، ولا يغيّر المستخدم تلقائياً.

### A-09: سجل تدقيق المحادثات = `Access Log` القياسي
عند تفعيل `allow_admin_conversation_audit` (معطل افتراضياً) يُسجَّل كل اطلاع من System Manager على محادثة غيره في `Access Log`، بالقيم `method = ai_agent.conversation_audit` و`reference_document` و`filters.conversation_owner`. لم يُنشأ DocType جديد: Access Log جزء من Frappe ومحمي أصلاً. يُسجَّل الاطلاع مرة واحدة لكل طلب HTTP.
التدقيق **قراءة فقط**: إعادة التسمية والأرشفة والإلغاء ومتابعة المحادثة (`parent_run`) لصاحبها وحده حتى مع تفعيل الإعداد.

### A-10: hooks صلاحيات على DocTypes المحادثات
DocType `AI Run` يمنح دور `User` قراءة كل التشغيلات، و`Financial Attachment Session` مثله. أُضيف `has_permission` و`permission_query_conditions` لكل من: AI Run، AI Chat Session، AI Chatbot Conversation، AI Memory، AI Tool Call، AI Approval Request، Financial Attachment Session. أُزيلت دوال `has_permission` من الـ controllers لأنها كانت تتجاوز الفحص لكل System Manager، وتوقيعها لا يطابق v16 أصلاً.

### A-11: قائمة الموافقات المعلقة ليست محادثة
`get_pending_approvals` وhook الـ AI Approval Request يسمحان للمعتمدين (System Manager أو `approver_role`) برؤية الطلبات المعلقة لكل المستخدمين، لأنها قائمة عمل لا يمكن الاعتماد بدونها. محتوى المحادثة نفسه (AI Run) يبقى لصاحبه.

### A-12: حساب Administrator
الـ endpoints تطبق قاعدة "صاحب المحادثة فقط" على Administrator أيضاً، وأُزيل استثناؤه من الـ chatbot. لكن Frappe يمنح Administrator كل الصلاحيات في `has_permission` قبل أي hook، فلا يمكن حجب شاشة الـ Desk عنه من داخل التطبيق. **التوصية:** لا يُستخدم حساب Administrator للعمل اليومي.

### A-13: WhatsApp بلا مستخدم افتراضي
- أُزيل `DEFAULT_SUPPORT_USER = "Administrator"`.
- `support_user()` يعيد `None` إن كان المستخدم فارغاً أو معطلاً، أو كان Administrator/Guest، أو يحمل System Manager أو Accounts Manager.
- المعالجة تُرفض وتُسجَّل عندها.
- `AI WhatsApp Support Settings.validate` يمنع حفظ مستخدم غير آمن.
- `ensure_settings` (عند كل migrate) لم يعد يضع مستخدماً افتراضياً، ولا يُفشل الـ migrate إن كان الإعداد القديم غير آمن: وقت التشغيل يرفضه أصلاً.
- التنفيذ يتم بجلسة صاحب التشغيل (`set_user(run.user)`).

### A-14: ثغرات إضافية أُغلقت خارج جدول A (اكتُشفت أثناء المراجعة)
| الموضع | المشكلة |
|---|---|
| `api.create_run` | `parent_run` لأي مستخدم كان يسحب تاريخ محادثته إلى سياق النموذج. `company` لم تُفحص مقابل User Permissions. `idempotency_key` غير مقيد بالمستخدم. |
| `api.reject_action` | أي مستخدم كان يستطيع رفض أي طلب موافقة بالاسم. |
| `chatbot.get_stream_content` | بلا فحص ملكية، فمن يملك `stream_id` يقرأ الرد. |
| `gemini_ocr_settings.test_connection` | أي مستخدم كان يستطيع إطلاق طلبات Gemini مدفوعة بالمفتاح المخزن. صار لـ System Manager فقط. |
| `tools/reports.py` | `prepared_report_name` من النموذج كان يقرأ تقريراً محضَّراً لمستخدم آخر، أي بيانات محسوبة بصلاحياته. صار يُشترط أن يكون مالكه صاحب التشغيل. |
| `tools/financial_attachments.py` | استثناء System Manager من ملكية الجلسة. |
| `purchase_invoice_ai` | نفس استثناء System Manager في `_assert_run_access`/`_assert_session_access`. شركة `start_extraction`/`start_discussion` بلا فحص صف. `bootstrap` يعرض كل الشركات عبر `get_all`. |
| `approval_engine` | `approval_id = APR-<ثانية>` فريد: طلبان في نفس الثانية يفشلان. أُضيفت لاحقة عشوائية. |

### A-15: الاختبارات على acc.dev
- الموقع يعمل بتطبيق `site_quota` بحد 10 مستخدمين. الاختبارات تعطل `site_quota.quota.validate_users_limit` أثناء إنشاء مستخدمي الاختبار، ثم **تعطّل** هؤلاء المستخدمين في النهاية فلا يستهلكون الحصة.
- في v16 يحصل كل مستخدم نظام على دور `Desk User`، فلا يمكن إنشاء مستخدم بلا permlevel 1 على Project. اختبارات permlevel تحاكي غيابه بـ patch على `Meta.get_permlevel_access`.

### A-16: حسم الأسئلة المفتوحة للمرحلة A (بتفويض: "اعمل المناسب")
- **واتساب:** يبقى الرفض قائماً. أي موقع يعمل بمستخدم Administrator يتوقف عن الرد حتى يُختار مستخدم دعم مخصص منخفض الصلاحيات، ويُسجَّل السبب في Error Log بعنوان "WhatsApp AI Support Refused". الأمان مقدَّم على استمرار الرد.
- **المعتمِدون:** يرون قائمة الموافقات المعلقة فقط، لا المحادثة. ملخص الطلب وحمولته كافيان للقرار.
- **Administrator:** توصية فقط، لا تغيير في الكود (قيد من Frappe).
- **مستخدم اختبار الموافقات:** `test_approval_engine` صار يعطّل `ai-approver-test@example.com` في النهاية، ويتجاوز حصة `site_quota` أثناء الاختبار. نُقلت الأداتان إلى `tests/utils.py` (`quota_bypass`، `disable_users`).
- **Git:** المرحلة A محفوظة في فرع `feat/phase-a-permissions` منفصل عن `main`.

---

## المرحلة B — خفض استهلاك الـ tokens (2026-09-28)

### B-01: القياس يغلّف المزود والبوابة ولا يعدّل كود الإنتاج
`ai_agent/benchmarks/run.py` يعمل عبر `unittest.mock.patch` على `OpenAICompatibleProvider.create_step` و`ToolGateway.authorize_and_execute` أثناء التشغيل فقط، ويسجل لكل استدعاء:
- الـ tokens.
- مصدر أحرف المدخلات (تعليمات، تعريفات أدوات، نتائج أدوات...).

المستخدم `ai-bench@example.com` بأدوار Accounts/Sales/Purchase/Stock User، ويُعطَّل بعد القياس. تُمسح ذاكرته قبل كل سؤال حتى لا تؤثر تشغيلة على التي بعدها. التكلفة التقديرية بأسعار مفترضة في `PRICES` داخل الملف.

### B-02: خط الأساس محفوظ كما هو، بما فيه الانهيارات
أربعة أسئلة من عشرة انهارت في خط الأساس بسبب خطأ قديم: `has_permission` على DocType من نوع Single يرمي `PermissionError` لغير المدراء. أُصلح الخطأ في `PermissionContext.has_doctype_permission`، وبقي خط الأساس يعكس الوضع الفعلي قبل أي تحسين.

### B-03: ملف `docs/kpi-definitions.md` غير موجود
أُنشئت 18 مؤشراً و3 مؤشرات مركبة بتعريفات افتراضية، **كلها `pending_confirmation = 1`** حتى يؤكدها مدير الحسابات. التعريفات في `ai_agent/metrics/defaults.py`. الـ migrate يضيف الناقص فقط ولا يعدّل تعريفاً موجوداً أبداً.

أهم الافتراضات:
- المبيعات والمشتريات صافية قبل الضريبة (`base_net_total`)، والمرتجعات قيمة موجبة.
- الذمم هي المتبقي على الفواتير الآن، ولا تشمل القيود اليدوية.
- النقدية والبنوك حسب نوع الحساب Cash أو Bank.
- الإيرادات والمصروفات من دفتر الأستاذ حسب root_type، وصافي الربح = الإيرادات − المصروفات.

### B-04: بُعد طريقة الدفع
| الحالة | طريقة الدفع |
|---|---|
| فواتير POS | `Sales Invoice Payment.mode_of_payment`، ويُخصم الباقي المُرجَع من طريقة الدفع النقدية |
| الجزء غير المدفوع من فواتير POS | "آجل" |
| كل الفواتير غير POS، حتى لو سُددت لاحقاً بسند قبض | "آجل"، لأن طريقة الدفع وقت البيع كانت آجلة |

المبالغ على أساس الإجمالي شامل الضريبة، لأن الدفعات تسدد الإجمالي، ويُذكر ذلك في `notes`. **يحتاج تأكيداً.**

### B-05: البُعد غير المضبوط على الموقع يُتجاهل مع ملاحظة
مثال ذلك بُعد "الفرع": لا يوجد حقل `branch` على Sales Invoice في acc.dev، فلا يُسقط الاستعلام كله. يُجاب بباقي الأبعاد وتُضاف ملاحظة "البعد غير مضبوط".

### B-06: كتالوج المؤشرات في التعليمات الثابتة وليس أداة
الكتالوج حوالي 2,500 حرف، ويُخزَّن في `frappe.cache` ويُمسح عند تعديل أي مؤشر. لم تُضف أداة `metrics.catalog` لأنها ستزيد تعريفات الأدوات واستدعاءً إضافياً.

### B-07: الاحتياط يُسجَّل في AI Run
أي تشغيل استخدم `schema.*` أو `data.query` أو `data.count` أو `reports.run` يأخذ `metric_fallback = 1` مع `fallback_tools`. هذه قائمة الأسئلة المرشحة لمؤشرات جديدة في الكتالوج.

### B-08: مزود Anthropic
- **التنفيذ:** بمكتبة `anthropic` الرسمية (1.8، مضافة إلى `pyproject.toml`). يترجم صيغة OpenAI التي يستخدمها الـ orchestrator إلى Messages API ويعيدها بنفس الشكل، فلا يتغير باقي الكود.
- **التخزين المؤقت:** `cache_control` على الجزء الثابت من التعليمات (قبل `Per-run context:`) وعلى آخر تعريف أداة. الجلسة المضغوطة والمستخدم والتاريخ والأدوار نُقلت إلى ما بعد العلامة حتى لا تُبطل التخزين المؤقت.
- **كتل التفكير:** تُحفظ في `provider_blocks` وتُعاد كما هي مع نتائج الأدوات. المزود المتوافق مع OpenAI يحذفها قبل الإرسال.
- **البث:** دائماً (`messages.stream` + `get_final_message`).
- **رسائل الخطأ:** رمز الحالة ونص خطأ الـ API فقط، بلا ترويسات أو مفتاح.
- **لم يُقَس فعلياً:** acc.dev لا يملك مفتاح Anthropic، فالاختبارات تحاكي الـ API.

### B-09: التوجيه بقواعد كلمات مفتاحية، بلا نموذج
- `runtime/router.py` يصنف السؤال إلى 7 مسارات بقواعد ثابتة.
- الاستدعاء التمهيدي بالنموذج (preflight router) بقي فقط لمسار `open_analysis`.
- كل مسار له 8 أدوات كحد أقصى، منها `tools.request_more`. هذه الأداة لا تضيف إلا أدوات تعلنها مهارة ممنوحة للمستخدم، فتبقى حدود المنح كما هي.
- التوجيه معطل للوكلاء ذوي عقود JSON (فواتير الشراء) ولوكيل واتساب، ويمكن إيقافه كلياً بالإعداد `enable_tool_routing`.
- في آخر خطوة مسموحة يُستدعى النموذج بلا أدوات، فيجيب بما لديه بدل أن يتوقف عند الحد.

### B-10: مسار المؤشرات بتعليمات مختصرة
يستبدل `METRIC_ROUTE_INSTRUCTIONS` التعليمات العامة، ويحذف من هذا المسار فقط:
- تعليمات الواجهة التفاعلية.
- قائمة المهارات.
- نص `core-agent-behavior` الطويل.
- الجلسة المضغوطة.

### B-11: الضغط بعد الاستخدام يشمل نتائج الاستكشاف فقط
نتائج `schema.*` و`skills.*` و`reports.describe` و`data.get_document` الأكبر من 1,500 حرف تُستبدل بملخص بعد أن يقرأها النموذج. نتائج البيانات (`metrics.query` و`data.query` و`reports.run`) لا تُضغط: الإجابة النهائية تقتبس أرقامها، وضغطها قد ينتج أرقاماً خاطئة.
