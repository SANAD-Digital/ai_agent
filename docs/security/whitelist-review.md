# مراجعة الـ endpoints المكشوفة — 2026-10-22

كل دوال `@frappe.whitelist` في التطبيق كما هي بعد الفحص الأمني بتاريخ 2026-10-22. لا توجد دالة تسمح للزائر غير المسجل (Guest).
الدوال التي تغيّر البيانات أو تبدأ تشغيل نموذج تقبل **POST فقط**، فلا يقدر رابط GET أن يشغّلها (CSRF).

| الدالة | HTTP | الفحص قبل أي شيء |
|---|---|---|
| `chat.api.bootstrap`، `list_sessions` | أي | `_chat_page` (دور المحادثة الكاملة) |
| `chat.api.create_session` | POST | `_chat_page`، والوكيل مسموح، وصلاحية قراءة الشركة |
| `chat.api.get_session`، `export_session`، `get_messages` | أي | `_get_session` (المالك، أو قراءة مدير النظام) |
| `chat.api.rename_session`، `archive_session`، `pin_session`، `delete_session`، `set_approval_mode` | POST | `_get_session(…, "write")` (المالك فقط) |
| `chat.api.get_message` | أي | `_get_message` (المالك) |
| `chat.api.send_message` | POST | `_chat_page` لجلسات Desk. جلسات غير Desk لا تغيّر الوكيل ولا الشركة ولا النموذج ولا وضع الموافقة. وحد طلبات لكل مستخدم |
| `chat.api.stop_message`، `feedback` | POST | `_get_message(…, "write")` |
| `chat.api.retry_message`، `edit_message`، `remember_now` | POST | فحص المالك، وحد طلبات لكل مستخدم |
| `chat.api.decide_approval` | POST | `ApprovalEngine` (دور المعتمد، المعتمد الثاني، أدوار النوع) |
| `chat.api.list_skills`، `list_memories`، `view_html` | أي | `_any_chat` |
| `chat.api.search_documents` | أي | `_any_chat`. و`get_list` والبحث العام يطبقان الصلاحيات. والحد ≤ 50 |
| `chat.api.get_artifact` | أي | مالك تشغيل الـ artifact |
| `chat.api.delete_memory` | POST | مالك الذاكرة |
| `chat.api.view_call` | POST | مالك الجلسة. أدوات القراءة و`view__*` فقط. وحد طلبات |
| `chat.support.bootstrap`، `list_skills`، `list_sessions` | أي | الفقاعة مفعّلة و`can_access` |
| `chat.support.send_message` | POST | `can_access`. والجلسة يجب أن تكون جلسة Chatbot للمستخدم نفسه |
| `mcp.api.handle` | POST/GET/DELETE | مستخدم مسجل يملك دور الـ MCP. وفحص Origin. وتوكنات موصلات الذكاء الاصطناعي تعمل هنا فقط (`mcp/token_scope.py`). وحد لاستدعاءات الأدوات |
| `mcp.api.diagnostics`، `mcp.setup.get_status` | أي | مدير النظام |
| `mcp.setup.fix`، `grant_role`، `revoke_client` | POST | مدير النظام |
| `ai_provider.fetch_models` | POST | صلاحية كتابة على AI Provider (مدير النظام) |
| `gemini_ocr_settings.test_connection` | POST | مدير النظام |

السجلات:
- **AI Chat Session:** حقول النظام (المستخدم، والواجهة، والعدادات، والملخص) لا تُعدَّل عبر `/api/resource`.
- **AI Provider:** يقرؤه مدير النظام فقط. المحادثة تقرأ المزودين عبر `available_providers` التي لا تُرجع أسراراً.
- **مفاتيح API:** في حقول كلمة مرور ولا تُرجَع أبداً.
