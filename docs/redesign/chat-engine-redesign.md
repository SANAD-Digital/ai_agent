# إعادة تصميم المحرك والشات — ai_agent

> الوثيقة الملزمة لإعادة بناء `agent-workspace` والمحرك تحته. الهدف: تجربة مطابقة لـ Claude.ai / Claude Code في الرد المتدفق، والتفكير الظاهر، وبطاقات الأدوات، والذاكرة، والمهارات، مع بقاء كل شيء داخل Frappe وبصلاحيات المستخدم نفسه.
> التاريخ: 2026-09-30. المصدر: ثلاث مراجعات مستقلة (واجهة الشات، المحرك، نموذج البيانات) على الكود الحالي.

---

## 1. لماذا نعيد البناء (خلاصة المراجعات)

| الطبقة | ما وُجد | الأثر |
|---|---|---|
| المحرك | `orchestrator.py` (1264 سطراً) حلقة دفعية بشكل OpenAI-chat، بلا بث، بلا تفكير، ست قواعد توقف إرشادية، موجّه بالكلمات المفتاحية، ردود جاهزة بلا نموذج (`local_answers`)، تلخيص المحادثة نصاً داخل رسالة المستخدم، `commit` في كل خطوة، وكتابة سياق كل تشغيل في مستند `AI Agent` المشترك بين المستخدمين | الرد يظهر دفعة واحدة بعد انتهاء التشغيل؛ لا تفكير؛ سلوك غير متوقع؛ تسرّب سياق بين المستخدمين |
| البيانات | المحادثة = سلسلة `AI Run`؛ لا جدول رسائل ولا كتل؛ التفكير لا يُخزَّن؛ نتائج الأدوات مبتورة (2000 حرف)؛ `final_answer` يُعدَّل لاحقاً عند تنفيذ الموافقة | إعادة فتح الجلسة لا تعيد ما جرى؛ لا يمكن إرسال التاريخ للنموذج كرسائل حقيقية |
| الواجهة | 2313 سطراً في إغلاق واحد، الـDOM هو الحالة، إعادة جلب المحادثة كاملة عند كل حدث + استطلاع كل ثانيتين (13 طلباً خلال ثانية لرسالة واحدة)، منطق أعمال يعتمد على مطابقة نصوص عربية | لا يشبه منصات الشات؛ ثقيل على الخادم؛ هش |

**ما يبقى كما هو (مكتبات):** `permission_context`, `approval_engine`, `write_guard`, `policy`, `masking`, `untrusted`, `document_context`, معالجات الأدوات في `tools/*`, `ToolRegistry` (البيانات) و`ToolGateway` (الفحوص)، صيغة `SKILL.md` و`AI Skill Grant`, `memory_learning`, `run_control` (الطابور والاسترداد)، `AI Approval Request`, MCP بالكامل.

**ما يُستبدل:** `orchestrator`, `model_provider`/`providers/*` (للاستخدام في الشات)، `context_builder`, `session_context`, `router`, `local_answers`, `step_narration`, `answer_contract`, `result_compaction`, `trace_service`، الصفحة `agent_workspace` وCSS الخاص بها.

---

## 2. المبادئ

1. **المحرك مولّد أحداث نقي.** `engine/loop.py` لا يلمس Frappe DB؛ يستقبل واجهات (Provider, ToolExecutor, History, Memory, CancelToken) ويُنتج أحداثاً مكتوبة الأنواع. الحفظ والبث في `runner.py` فقط.
2. **الكتلة (Block) هي وحدة الحقيقة.** رسالة المساعد = تسلسل كتل `thinking | text | tool_use | tool_result | approval | artifact | error`. نفس الكتل تُعرض في الشات وتُعاد للنموذج كتاريخ حقيقي (رسائل user/assistant/tool)، لا كملخص نصي.
3. **لا إرشادات نصية بديلة عن قدرات المزوّد.** استدعاء أدوات أصلي (native tool calling)، بث أصلي، تفكير أصلي (`thinking` في Anthropic، `reasoning_content` في DeepSeek). لا موجّه بالكلمات، لا ردود جاهزة، لا فحص "نتيجة نهائية" بالنصوص.
4. **البث أولاً.** كل delta يصل المتصفح عبر socket.io مع حمولته؛ لا استطلاع إلا كخطة بديلة (reconnect).
5. **الواجهة عميل شات حقيقي.** مخزن رسائل في الذاكرة، عرض تدريجي بالمفاتيح، لا إعادة جلب كاملة، HTML/CSS/JS بلا إطار عمل ولا build خاص.
6. **الصلاحيات كما هي.** يعمل التشغيل بهوية صاحب الجلسة فقط؛ كل كتابة عبر `ApprovalEngine`.
7. **الإضافة قبل الحذف.** الجداول الجديدة إضافية؛ المسارات القديمة (MCP، WhatsApp، Purchase Invoice AI) تستمر على `AI Run` حتى تُنقل لاحقاً.

---

## 3. نموذج البيانات

### 3.1 `AI Chat Session` (يُرقّى إلى جذر المحادثة)
حقول تُضاف: `surface` (Desk/WhatsApp/Chatbot/MCP/API)، `provider` (Link)، `model` (Data)، `summary` (Long Text — ملخص الضغط المستمر) + `summary_through_seq` (Int)، `last_seq` (Int)، `pinned` (Check)، `input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens` (Int)، `estimated_cost` (Currency)، `external_key` (Data, index). يبقى: `title, user, agent, company, status(Active/Archived), last_message_on, message_count, root_run, last_run`.
الملكية: `user` عبر hooks الحالية. لا يُنشأ بعد الآن كأثر جانبي لإدراج `AI Run` من الشات؛ الـAPI ينشئه صراحة.

### 3.2 `AI Chat Message` (جديد، `hash`)
`session` (Link, reqd, index) · `seq` (Int, فريد داخل الجلسة) · `role` (user/assistant) · `status` (pending/streaming/completed/failed/cancelled/awaiting_approval) · `run` (Link AI Run — تنفيذ رسالة المساعد) · `parent_message` (Link) · `provider`, `model` · `stop_reason` · `error` (Small Text) · `text` (Long Text — تجميع كتل النص للبحث والمعاينة) · `input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, estimated_cost` · `context_tokens` (حجم السياق الفعلي عند آخر نداء) · `started_at, completed_at, duration_ms` · `user` (نسخة للملكية) · `context_doctype/context_name`.
المرفقات: `File.attached_to_doctype = "AI Chat Message"`.

### 3.3 `AI Message Block` (جديد، `hash`)
`message` (Link, reqd) · `seq` (Int) · فهرس `(message, seq)` · `block_type` (thinking/text/tool_use/tool_result/approval/artifact/skill/memory/error/status) · `status` (streaming/completed/failed/denied) · `content` (Long Text) · `content_json` (JSON) · `tool_call_id` (Data, index) · `tool_name` · `tool_call` (Link AI Tool Call) · `approval` (Link AI Approval Request) · `artifact` (Link AI Artifact) · `signature` (Long Text — توقيع التفكير عند Anthropic) · `is_user_visible` · `started_at, completed_at, duration_ms` · `input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens` (للنداء الذي انتهى عند هذه الكتلة).

### 3.4 `AI Run` (يبقى كسجل تنفيذ)
يُضاف: `session` (Link)، `message` (Link AI Chat Message). يتوقف `after_insert` عن إنشاء جلسة عندما يكون `session` معبّأ. الحقول الميتة تبقى للتوافق.

### 3.5 `AI Agent`
`compact_session` يُهمل (لا يُكتب ولا يُقرأ بعد الآن). يُضاف `thinking` (Select: off/adaptive/on) و`thinking_budget_tokens` (Int).

### 3.6 `AI Provider Model`
يُضاف: `max_output_tokens`, `supports_tools`, `supports_thinking`, `input_price_per_million`, `output_price_per_million`.

### 3.7 الترحيل
Patch يحوّل كل `AI Run` تابع لجلسة Desk إلى رسالتين + كتل: `Tool` steps + `AI Tool Call` → `tool_use/tool_result`؛ `AI Approval Request` → `approval`؛ `AI Artifact` → `artifact`؛ `final_answer` → `text` (مع فصل ملاحظة تنفيذ الموافقة الملحقة)؛ `error_summary` → `error`. التفكير لا يُسترد. الجلسات التي مصدرها MCP/WhatsApp/PI تُوسم `surface` ولا تظهر في شريط Desk.

---

## 4. المحرك `ai_agent/engine/`

```
engine/
  types.py       Message, Block(kind, ...), ToolSpec, Usage, StopReason, ProviderCaps
  events.py      RunStarted, ThinkingDelta, TextDelta, ToolUseStart, ToolInputDelta, ToolUseReady,
                 ToolResult, SkillLoaded, MemoryNote, ApprovalRequested, Usage, Compaction,
                 RunCompleted, RunFailed, RunCancelled            (dataclasses، to_dict())
  providers/
    base.py      Provider.stream(request) -> Iterator[ProviderEvent]; caps(); count_tokens()
    openai_compat.py   openai SDK streaming: tool-call deltas، reasoning_content → thinking، usage
    anthropic.py       Messages API streaming: thinking(+signature)، tool_use، cache_control، usage
    factory.py         provider_for(session/agent) من AI Provider
  context/
    system_prompt.py   ثابت (قابل للتخزين المؤقت) + ديناميكي (المستخدم/التاريخ/الشركة/المهارات المتاحة/الذاكرة)
    history.py         load(session) -> [Message] من الكتل؛ ملخص الضغط يدخل كرسالة أولى
    compactor.py       ميزانية بالرموز: (1) تقليم tool_result القديمة (2) تلخيص بالنموذج وحفظ summary
    files.py           المرفقات ككتل document/image أصلية إن دعمها المزوّد، وإلا نص مستخلص
  tools.py       ToolExecutor: schemas() من ToolRegistry بعد ترشيح الوكيل/المهارة؛ execute() عبر ToolGateway؛
                 actions.request_* → ApprovalEngine → ApprovalRequested
  skills.py      advertise() كل المهارات الممنوحة (اسم+وصف)؛ load() يُدرج الجسم ككتلة skill دائمة
  memory.py      recall(query) عبر MemoryService؛ remember(text) أداة صريحة؛ لا تخزين تلقائي للحلقات
  loop.py        run(ctx) -> Iterator[Event]   (نقي)
  runner.py      وظيفة RQ: تستهلك الأحداث، تحفظ الكتل دفعات، تبث realtime، تفحص الإلغاء، تُنهي الرسالة
  approvals.py   استئناف الرسالة بعد قرار الموافقة (tool_result بنتيجة التنفيذ أو الرفض)
```

### 4.1 الحلقة
```
messages = history.load(session)            # كتل → رسائل أصلية
system   = system_prompt.build(...)
loop:
  compactor.ensure_fits(messages, budget)   # قبل كل نداء
  for ev in provider.stream(system, messages, tools, thinking):
      yield ev                               # thinking_delta / text_delta / tool_use_* / usage
  if stop_reason == "tool_use":
      results = executor.run(tool_uses)      # القراءات بالتوازي، الكتابات تسلسلية
      for r in results: yield ToolResult(r)
      if any(r.approval): yield ApprovalRequested; persist state; return   # الرسالة تنتظر
      messages.append(assistant_blocks); messages.append(tool_results); continue
  if stop_reason in ("end_turn","stop"): yield RunCompleted; return
  if stop_reason == "max_tokens": append "continue" turn (مرة واحدة) then continue
```
حدود: `maximum_steps` و`maximum_tool_calls` من الوكيل؛ الإلغاء يُفحص بين الأحداث (CancelToken) ويُقطع النداء الجاري بإغلاق البث.

### 4.2 المزوّدون
- **OpenAI-compatible (DeepSeek الحالي):** `openai` SDK، `stream=True`، `stream_options.include_usage`، تجميع `tool_calls[].function.arguments` بالدلتا، `reasoning_content` دلتا → `ThinkingDelta`. خيارات إضافية للطلب من `AI Provider.extra_body` (مثل `thinking`).
- **Anthropic:** `client.messages.stream`، `thinking={"type":"adaptive"}` أو ميزانية، `cache_control` على system وآخر أداة وآخر دور مستخدم، إعادة كتل `thinking` بتوقيعها، `tool_result` ككتل.
- **القدرات** من `AI Provider Model`؛ إن غابت: تقدير محافظ.

### 4.3 الموافقات
`actions.request_*` تُنشئ `AI Approval Request` عبر `ApprovalEngine` وتُصدر `ApprovalRequested`؛ الرسالة تُحفظ بحالة `awaiting_approval` مع كتلة `approval`. عند القرار (`chat.api.decide_approval`): تنفيذ/رفض عبر `ApprovalEngine`، ثم استئناف نفس الرسالة بوظيفة جديدة: يُضاف `tool_result` بنتيجة التنفيذ ويكمل النموذج (يشرح ما تم ويقترح التالي). لا رسالة جديدة ولا تعديل لاحق على نص قديم.

### 4.4 الذاكرة
- الاستدعاء: `MemoryService.build_context` (كما هو) → كتلة نص في الجزء الديناميكي من system prompt.
- التذكّر: أداة `memory.remember` (تجعل `memory.store_context` صريحة) + التعلم من المستندات المعتمدة (`memory_learning`).
- يُحذف: تخزين "episode/lesson" التلقائي، و`compact_session`.
- الواجهة تعرض كتلة `memory` ("تم حفظ: …") عند كل كتابة.

### 4.5 المهارات
كل المهارات الممنوحة تُعلن (اسم + وصف) في system prompt؛ `skills.load` يُعيد الجسم ويُثبّته ككتلة `skill` تُعاد للنموذج كرسالة مستخدم ثابتة (لا تُضغط). أوامر `/skill` من المؤلّف تُرسل الطلب مع `skill` محدد فيُحمَّل قبل أول نداء.

---

## 5. عقد البث (realtime)

الحدث الوحيد للمتصفح: `ai_agent:chat` بحمولة `{session, message, seq, event}` حيث `event.type` أحد:
`message.started`, `block.start {block_seq, block_type, tool_name?}`, `block.delta {block_seq, delta}`, `block.done {block_seq, block}`, `usage {...}`, `message.done {status, stop_reason, usage}`, `message.failed {error}`, `approval.requested {approval, block_seq}`, `session.updated {title, ...}`.
`seq` تسلسل أحادي لكل رسالة؛ العميل الذي يفقد أحداثاً (فجوة في seq) يستدعي `chat.api.get_message(message)` ويُعيد بناء الرسالة من الكتل المحفوظة + ذيل الدلتا من Redis (`ai:chat:<message>`).

دفعات الحفظ: النص/التفكير يُجمّعان في الذاكرة ويُكتبان للـDB عند `block.done` وكل 2 ثانية كحد أقصى؛ البث للمتصفح لا ينتظر الحفظ.

---

## 6. API `ai_agent/chat/api.py`

| method | الغرض |
|---|---|
| `list_sessions(limit, include_archived, search)` | جلسات المستخدم (surface=Desk) مع العنوان والمعاينة والوكيل |
| `create_session(agent, company, provider, model)` | جلسة فارغة |
| `get_session(session)` | رأس الجلسة + الاستخدام الكلي |
| `get_messages(session, before_seq, limit)` | رسائل بكتلها (مقسّمة، الأحدث أولاً) |
| `get_message(message)` | رسالة واحدة (لإعادة المزامنة) |
| `send_message(session, text, files, skill, context_doctype, context_name)` | رسالة مستخدم + رسالة مساعد pending + AI Run + enqueue |
| `stop_message(message)` | إلغاء فعلي (CancelToken + stop job) |
| `retry_message(message)` / `edit_message(message, text)` | إعادة توليد / تعديل وإعادة الإرسال (تحذف ما بعدها) |
| `decide_approval(approval, decision, reason)` | موافقة/رفض ثم استئناف الرسالة |
| `rename/archive/pin/delete_session` | إدارة |
| `list_agents()` | الوكلاء المفعّلون التي للمستخدم منح عليها |
| `list_skills(agent)` | لأوامر `/` |
| `list_models()` | مزوّدون ونماذج مسموحة |
| `feedback(message, value)` | 👍/👎 |
الرفع عبر `upload_file` القياسي ثم ربط بالرسالة (`attach_files(message, files)`).

---

## 7. الواجهة `public/js/chat/`

بلا إطار عمل: ملفات JS بسيطة تُحمَّل بالترتيب عبر `frappe.require` من صفحة `agent-workspace`، وCSS بمتغيرات تصميم خاصة (فاتح/داكن، RTL بالخصائص المنطقية).

```
core.js        h()/dom helpers، markdown آمن (frappe.markdown + تعقيم)، تنسيق الأرقام/الوقت، i18n
store.js       {sessions, current, messages: Map<name, Message{blocks: []}>, streaming}; مشتركون
transport.js   realtime subscribe + gap detection + resync؛ استدعاءات API
blocks.js      مُصيّر لكل نوع كتلة: Text (markdown تدريجي)، Thinking (قابلة للطي "فكّر لمدة …")،
               ToolCall (بطاقة: الاسم، الحالة، المدة، المدخلات/المخرجات قابلة للطي)، Approval
               (ملخص + الأثر + موافقة/رفض/تفاصيل)، Artifact (جدول/رسم)، Skill، Memory، Error
message.js     رسالة المستخدم (فقاعة خفيفة، مرفقات، تعديل)، رسالة المساعد (بلا فقاعة، إجراءات: نسخ/إعادة/تغذية راجعة)
composer.js    متعدد الأسطر، Enter/Shift+Enter مع حماية IME، أوامر / للمهارات، @ للمستندات، لصق/سحب ملفات، إيقاف
sidebar.js     الجلسات: بحث، تجميع بالتاريخ، تثبيت/إعادة تسمية/أرشفة، جديد، درج في الجوال
app.js         التركيب، التوجيه، اختصارات لوحة المفاتيح، مؤشر السياق/الاستخدام
```
تخطيط: شريط جانبي 280px قابل للطي · عمود محتوى بعرض أقصى 768px · المؤلّف ثابت أسفل · `100dvh` · درج في الجوال. مؤشر "يعمل…" داخل الرسالة مع مؤقّت، زر "إيقاف" يعكس التأكيد الفعلي.

---

## 8. الاختبارات
- `engine/`: اختبارات وحدة نقية بمزوّد وهمي (FakeProvider) تُغطّي: نص فقط، تفكير + نص، أداتان بالتوازي، موافقة ثم استئناف، إلغاء أثناء البث، `max_tokens` ثم إكمال، ضغط عند تجاوز الميزانية.
- `chat/api`: تكامل: إرسال → رسالة pending → run؛ صلاحية الملكية؛ الحذف يحذف الكتل؛ get_messages مقسّمة.
- الواجهة: playwright headless ضد الصفحة الحية (بث، بطاقة أداة، موافقة، RTL، جوال).

## 9. المراحل
1. **الأساس:** DocTypes + engine + chat API + عميل جديد (نص/تفكير/أدوات/موافقة/بث) + الترحيل. ← هذا التسليم.
2. تعديل/إعادة توليد، تثبيت، بحث الخادم، مؤشر السياق بدقة، تسعير.
3. نقل الودجت وWhatsApp إلى الجلسات (surface).
4. حذف الشيفرة القديمة بعد أسبوعين من التشغيل المتوازي.

---

## 10. حالة التنفيذ (2026-09-30 مساءً)

| البند | الحالة |
|---|---|
| المحرك `ai_agent/engine/` (حلقة، مزوّدان ببث أصلي، ضغط سياق، تاريخ ككتل، أدوات عبر البوابة، موافقة واستئناف) | منجز، 18 اختبار وحدة |
| DocTypes `AI Chat Message` / `AI Message Block` + الحقول الجديدة | منجز، migrate نُفّذ على acc.dev |
| `chat/api.py` (إرسال/إيقاف/إعادة توليد/تعديل/موافقة/جلسات/مهارات/بحث/تغذية راجعة) | منجز، 9 اختبارات تكامل |
| العميل `public/js/chat/*` + `chat.css` | منجز، 29 لقطة على محاكاة + فحص حي |
| الرد الحي من DeepSeek (أدوات متوازية، تفكير، Markdown، جدول) | متحقق |
| البث الحرفي عبر socket.io | معطّل على مستوى الـbench (`developer_mode: 1` يوجّه مصادقة realtime إلى `:8000`)؛ العميل يستطلع كل 1.5 ث أثناء البث فقط كبديل |
| نقل الجلسات القديمة | سكربت يدوي `scripts/migrate_chat_history.py` بانتظار قرار المالك؛ حتى ذلك الحين لا تظهر الجلسات القديمة في الشريط (`last_seq = 0`) |
| المسارات القديمة (orchestrator، الودجت، WhatsApp، Purchase Invoice AI، MCP) | لم تُمس؛ جلساتها تُوسم `API`/`MCP` فلا تظهر في الشات |

### 10.1 تحسينات الاستهلاك والذاكرة (2026-09-30 ليلاً)

| البند | قبل | بعد |
|---|---|---|
| السياق الديناميكي (كان يحوي 60 دوراً) | ≈393 رمز/نداء | ≈25 رمز/نداء |
| الأدوات في كل نداء | 31 أداة ≈4.8k رمز | نواة 17 أداة + `tools__enable` ≈3.5k رمز؛ المجموعات `erp_details`, `diagnostics`, `knowledge`, `documents`, `actions_more` تُفعَّل عند الطلب أو عند تحميل مهارة |
| تجاوز السياق من المزوّد | فشل الرسالة | ضغط قسري بميزانية 60% مما أُرسل ثم إعادة المحاولة مرة واحدة |
| الذاكرة | تُكتب فقط بنداء صريح من النموذج | استخلاص تلقائي (`engine/memory.extract_session`) للجلسات الخاملة 20 دقيقة كل ساعة، وعند الأرشفة، وبزر «تذكّر الآن»؛ التفضيلات تُحقن دائماً والحقائق حسب الصلة؛ واجهة عرض/حذف؛ حارس تشابه 80% ضد التكرار |

يحتاج `bench migrate` لحقل `AI Chat Session.memory_extracted_seq` (بدونه يعمل الاستخلاص اليدوي فقط ويُعاد استخلاص الجلسة كاملة كل مرة).

**التالي (المرحلة 2):** تسعير النماذج في `AI Provider Model`، مؤشر السياق من عدّ الرموز الفعلي عند Anthropic، تنفيذ القراءات بالتوازي، ترحيل الودجت وWhatsApp إلى الجلسات، ثم حذف `orchestrator` وأخواته بعد أسبوعين من التشغيل المتوازي.
