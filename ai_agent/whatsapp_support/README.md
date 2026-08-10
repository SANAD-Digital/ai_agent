# WhatsApp Support Engine

محرك مستقل داخل `ai_agent` للتعامل مع رسائل الدعم الفني الواردة من تطبيق `snd_whatsapp`.

## طريقة العمل

- يستمع إلى `WhatsApp Webhook Message Received Log.after_insert` عبر `ai_agent/hooks.py`.
- يتجاهل المجموعات افتراضياً.
- يمنع تكرار الرد باستخدام سجل الرد المرتبط ومفتاح `message_id`.
- ينشئ أو يحدث تذكرة دعم `Issue` افتراضياً إذا كان الـ DocType موجوداً.
- يشغل `AI Run` باستخدام Agent باسم `WhatsApp Technical Support`.
- يرسل الرد عبر `snd_whatsapp.snd_whatsapp.doctype.whatsapp_log.whatsapp_log.add_notification_log`.
- يستخدم `WhatsApp Reply Device` من الإعدادات أولاً، ثم جهاز الرسالة الواردة أو جهاز واتساب الافتراضي كـ fallback.

## إعدادات الواجهة

بعد `bench migrate` ستجد Single DocType باسم:

`AI WhatsApp Support Settings`

وفيه يتم اختيار:

- `AI Agent`
- `WhatsApp Reply Device`
- `Run As User`
- `Company`
- إعدادات التذاكر والمجموعات ورسالة الخطأ

## إعدادات site_config.json

كل الإعدادات اختيارية وتعمل كـ fallback إذا لم تكن إعدادات الواجهة متاحة:

```json
{
  "ai_whatsapp_support_enabled": 1,
  "ai_whatsapp_support_agent": "WhatsApp Technical Support",
  "ai_whatsapp_support_whatsapp_device": "WTDS.00001",
  "ai_whatsapp_support_user": "Administrator",
  "ai_whatsapp_support_company": "",
  "ai_whatsapp_support_allow_groups": 0,
  "ai_whatsapp_support_create_issue": 1,
  "ai_whatsapp_support_issue_doctype": "Issue",
  "ai_whatsapp_support_history_messages": 6,
  "ai_whatsapp_support_error_reply": "تم استلام طلبك، لكن تعذر تجهيز رد آلي الآن. تم تسجيل الطلب وسيقوم فريق الدعم بمتابعته."
}
```

يمكن استخدام `"Support Issue"` بدلاً من `"Issue"` في `ai_whatsapp_support_issue_doctype` إذا كان هذا هو مسار الدعم المطلوب في الموقع.
