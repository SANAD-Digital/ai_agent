# مراجعة الـ endpoints المكشوفة (A7) — 2026-09-28

36 دالة `@frappe.whitelist`. "الملكية" = هل تتحقق أن المستدعي يملك السجل.
"صاحب/تدقيق" = صاحب السجل فقط، أو System Manager عند تفعيل `allow_admin_conversation_audit` مع تسجيل في Access Log.

| # | Endpoint | من يستدعيه | الملكية قبل | الحالة بعد |
|---|---|---|---|---|
| 1 | `api.create_run` | واجهة الوكيل | ❌ `parent_run` لأي مستخدم، `company` بلا فحص صف | ✅ أُصلح: `parent_run` لصاحبه، فحص الشركة، `idempotency_key` لكل مستخدم |
| 2 | `api.list_available_providers` | الواجهة | لا بيانات حساسة ولا مفاتيح | ✅ سليم |
| 3 | `api.list_chat_sessions` | الشريط الجانبي | ⚠️ System Manager يرى الكل | ✅ أُصلح: صاحب/تدقيق |
| 4 | `api.rename_chat_session` | الشريط الجانبي | ⚠️ System Manager يعدّل محادثات غيره | ✅ أُصلح: صاحبها فقط |
| 5 | `api.archive_chat_session` | الشريط الجانبي | ⚠️ مثل 4 | ✅ أُصلح: صاحبها فقط |
| 6 | `api.get_chat_messages` | الواجهة | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 7 | `api.get_print_letter_heads` | الطباعة | `get_list` | ✅ سليم |
| 8 | `api.get_artifact_content` | الواجهة | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 9 | `api.get_run` | الواجهة | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 10 | `api.get_run_steps` | الواجهة | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 11 | `api.get_run_artifacts` | الواجهة | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 12 | `api.cancel_run` | الواجهة | ⚠️ System Manager يلغي تشغيل غيره | ✅ أُصلح: صاحبه فقط |
| 13 | `api.list_available_agents` | الواجهة | أسماء وأوصاف فقط | ✅ سليم |
| 14 | `api.list_available_skills` | الواجهة | محسوبة لمستخدم الجلسة | ✅ سليم |
| 15 | `api.approve_action` | زر الموافقة | صاحب الطلب أو معتمِد، والتنفيذ يفحص الـ DocType فقط | ✅ أُصلح: إعادة فحص الصف والحقول بصلاحيات المعتمِد |
| 16 | `api.reject_action` | زر الرفض | ❌ أي مستخدم يرفض أي طلب | ✅ أُصلح: صاحب الطلب أو صاحب التشغيل أو معتمِد |
| 17 | `api.get_pending_approvals` | الواجهة | System Manager يرى الكل | ✅ أُصلح: المعتمدون (System Manager + `approver_role`) يرون القائمة، والبقية طلباتهم فقط |
| 18 | `chatbot.api.get_config` | الـ widget | بلا أسرار | ✅ سليم |
| 19 | `chatbot.api.get_messages` | الـ widget | ⚠️ Administrator يقرأ الكل | ✅ أُصلح: صاحب/تدقيق |
| 20 | `chatbot.api.send_message` | الـ widget | ⚠️ Administrator | ✅ أُصلح: صاحبها فقط |
| 21 | `chatbot.api.start_message` | الـ widget | ⚠️ Administrator | ✅ أُصلح: صاحبها فقط |
| 22 | `chatbot.api.get_stream_content` | الـ widget | ❌ بلا فحص | ✅ أُصلح: مالك الـ stream فقط |
| 23 | `chatbot.api.start_conversation` | الـ widget | محادثة المستخدم نفسه | ✅ سليم |
| 24 | `gemini_ocr_settings.test_connection` | زر في الإعدادات | ❌ أي مستخدم يستهلك المفتاح | ✅ أُصلح: System Manager فقط |
| 25 | `mcp.api.handle` | عملاء MCP | `enable_mcp` + قائمة أدوار + Origin | ✅ سليم، والأدوات تمر عبر Gateway وApproval Engine |
| 26 | `purchase_invoice_ai.api.bootstrap` | صفحة الفواتير | ⚠️ كل الشركات عبر `get_all` | ✅ أُصلح: `get_list` |
| 27 | `purchase_invoice_ai.api.start_extraction` | صفحة الفواتير | فحص Company على مستوى الـ DocType فقط | ✅ أُصلح: فحص صف الشركة |
| 28 | `purchase_invoice_ai.api.read_selected_pages` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحبه فقط |
| 29 | `purchase_invoice_ai.api.get_session` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 30 | `purchase_invoice_ai.api.set_selected_pages` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحبها فقط |
| 31 | `purchase_invoice_ai.api.get_page_preview` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 32 | `purchase_invoice_ai.api.get_analysis` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 33 | `purchase_invoice_ai.api.prepare_journal_entry_analysis` | صفحة الفواتير | تحويل لحمولة المستدعي، بلا قراءة سجلات | ✅ سليم |
| 34 | `purchase_invoice_ai.api.start_discussion` | صفحة الفواتير | ⚠️ `parent_run` لـ System Manager، والشركة بلا فحص | ✅ أُصلح: `parent_run` لصاحبه، وفحص صف الشركة |
| 35 | `purchase_invoice_ai.api.get_discussion` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحب/تدقيق |
| 36 | `purchase_invoice_ai.api.cancel_analysis` | صفحة الفواتير | ⚠️ System Manager | ✅ أُصلح: صاحبه فقط |

## الوصول عبر Desk وREST (`/api/resource/...`)
لم يكن ضمن الـ 36 لكنه نفس البيانات: `AI Run` و`Financial Attachment Session` مفتوحان لدور `User` بلا تقييد بالمالك. أُضيف `has_permission` و`permission_query_conditions` لسبعة DocTypes (راجع `docs/decisions.md` A-10).
