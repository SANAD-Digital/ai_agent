# Chat client contract (binding for `public/js/chat/*`)

All endpoints live in `ai_agent.chat.api` and are called with `frappe.xcall("ai_agent.chat.api.<name>", {...})`.
All timestamps are server strings `YYYY-MM-DD HH:MM:SS(.ffffff)`; render with `frappe.datetime`.

## Objects

```jsonc
Session {
  "name": "CHAT-00012", "title": "…", "agent": "ERP Analyst", "company": "…|null",
  "provider": "Default", "model": "deepseek-v4-flash", "status": "Active|Archived", "pinned": 0|1,
  "last_message_on": "…", "message_count": 6, "preview": "first 120 chars of the last assistant text",
  "usage": {"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"estimated_cost":0},
  "context_tokens": 0,          // tokens the last model call held (live context size)
  "context_window": 128000,     // of the session model
  "last_seq": 6
}

Message {
  "name": "a1b2c3", "session": "CHAT-00012", "seq": 3, "role": "user|assistant",
  "status": "pending|streaming|completed|failed|cancelled|awaiting_approval",
  "text": "…",                  // user: the prompt; assistant: concatenated text blocks (may be empty while streaming)
  "created_at": "…", "completed_at": "…|null", "duration_ms": 0,
  "model": "…", "provider": "…", "stop_reason": "end_turn|tool_use|max_tokens|…", "error": "…|null",
  "usage": {…same keys as session…}, "context_tokens": 0,
  "attachments": [{"name":"File-1","file_name":"a.pdf","file_url":"/private/files/a.pdf","file_size":1234}],
  "blocks": [Block, …],         // ordered by seq
  "feedback": 0|1|-1
}

Block {
  "seq": 0, "kind": "thinking|text|tool_use|tool_result|approval|artifact|skill|memory|error|status",
  "status": "streaming|completed|failed|denied",
  "text": "…",                  // thinking/text content; tool_result: model-facing text (wrapped, may be long)
  "data": {…},                  // tool_use: the input; tool_result: UI result object (rows capped) or {"error": "…"};
                                // approval: snapshot (below); artifact: spec (below); skill: {title, description, tools}; memory: {memory}
  "id": "toolu_…",              // tool_use / tool_result / approval share it
  "name": "data__query",        // tool wire name (show as data.query) or skill name
  "is_error": false,
  "started_at": "…", "completed_at": "…", "duration_ms": 812
}

ApprovalSnapshot (block.data for kind=approval) {
  "approval": "APRQ-…", "approval_id": "APR-…", "status": "Pending|Approved|Rejected|Expired|Executed|Failed",
  "action_type": "Create|Bulk Create|Update|Submit|Cancel|Workflow|Approved Method",
  "doctype_name": "Sales Invoice", "document_name": "…|null",
  "action_summary": "…", "impact_summary": "…", "payload": {…document payload…}, "expires_at": "…",
  "result": {"document_name": "SINV-0001", …} | null,   // after execution
  "decided_by": "…", "decided_at": "…", "reason": "…"
}

ArtifactSpec (block.data for kind=artifact) {
  "artifact": "…", "artifact_type": "Table|Chart|Document Links|JSON|Text", "title": "…", "summary": "…",
  "row_count": 812, "is_truncated": true, "columns": ["…"], "rows": [{…}, …first 50…],
  "chart": {"type":"bar|line|pie|donut|percentage","labels":[…],"datasets":[{"name":"…","values":[…]}]},   // Chart only
  "links": [{"doctype":"…","name":"…","title":"…"}]                                                    // Document Links only
}
```

## Endpoints

| name | args | returns |
|---|---|---|
| `bootstrap` | — | `{user:{name,full_name,image,language,dir}, agents:[{name,agent_name,description,read_only,allow_actions,is_default}], providers:[{name,label,provider_type,is_default,models:[{model_name,label,is_default,supports_thinking}]}], settings:{third_party_warning, thinking_options:["off","adaptive","on"]}, sessions:[Session…first 50]}` |
| `list_sessions` | `limit=50, offset=0, include_archived=0, search=""` | `{sessions:[Session], has_more}` |
| `create_session` | `agent, company=null, provider=null, model=null` | `{session: Session}` |
| `get_session` | `session` | `{session: Session}` |
| `get_messages` | `session, before_seq=null, limit=40` | `{messages:[Message] ascending by seq, has_more}` |
| `get_message` | `message` | `{message: Message}` (resync after a gap) |
| `send_message` | `text, session=null, agent=null, company=null, provider=null, model=null, thinking=null, files=[File names], skill=null, context_doctype=null, context_name=null` | `{session: Session, user_message: Message, assistant_message: Message(status pending)}` — creates the session when `session` is null |
| `stop_message` | `message` | `{status:"cancelling"}` then a realtime `message.cancelled` |
| `retry_message` | `message` (assistant) | `{assistant_message: Message}` (old one is deleted) |
| `edit_message` | `message` (user), `text` | `{user_message, assistant_message}` (everything after is deleted) |
| `decide_approval` | `approval, decision:"approve"|"reject", reason=null` | `{approval: ApprovalSnapshot, message: name}` then the assistant message resumes streaming |
| `rename_session` / `archive_session` / `pin_session` / `delete_session` | `session, title` / `session, archived` / `session, pinned` / `session` | `{session}` |
| `list_skills` | `agent` | `{skills:[{name,title,description}]}` |
| `search_documents` | `txt, doctype=null` | `{results:[{doctype,name,title}]}` (for @mentions) |
| `feedback` | `message, value (1|-1|0)` | `{ok:true}` |
| `get_artifact` | `artifact` | full `{columns, rows, chart, links, title, artifact_type, row_count}` |
| `upload` | use Frappe `upload_file` with `doctype="AI Chat Message"` **after** `send_message`? No: upload first with `is_private=1` and no attachment, pass the returned `File.name`s as `files` to `send_message`; the server attaches them. |

Errors: Frappe exceptions (`frappe.throw`) → show `exc` message in an inline error state, never a `frappe.msgprint` dialog.

## Realtime

`frappe.realtime.on("ai_agent:chat", payload)` with
```jsonc
{ "session": "CHAT-…", "message": "<assistant message name>", "seq": 17,   // seq is monotonic per message
  "event": { "type": "…", … } }
```
Event types (fields beyond `type`):
- `message.started` `{model, provider, resumed}`
- `block.start` `{block_seq, kind, id?, name?}`
- `block.delta` `{block_seq, delta}` (thinking/text text; tool_use partial JSON — client may ignore tool_use deltas and wait for block.done)
- `block.done` `{block_seq, block: Block}`
- `tool.result` `{block_seq, block: Block(kind tool_result), duration_ms}`
- `skill.loaded` / `memory.note` / `artifact.created` `{block_seq, block}`
- `approval.requested` `{block_seq, block: Block(kind approval), approval}`
- `usage` `{usage, context_tokens, model}`
- `compaction` `{before_tokens, after_tokens, dropped_messages}`
- `message.done` `{status:"completed|awaiting_approval", stop_reason, usage, steps, tool_calls}`
- `message.failed` `{error, kind}` · `message.cancelled` `{usage}`
- `session.updated` `{session: Session}` (title auto-generated, usage totals)
- `approval.decided` `{block_seq, block}` (the approval block updated with status/result)

Gap rule: if a payload's `seq` is not `last_seq + 1` for that message, call `get_message` and replace the message wholesale, then continue applying events with `seq` greater than the fetched message's `last_event_seq` (returned in `Message` as `event_seq`).

## Rendering rules (Claude.ai parity)

- Assistant text renders as Markdown progressively (re-render the current text block on each delta with a cheap throttle ~50 ms; never flash).
- Thinking block: collapsed row "فكّر لمدة 4 ث" / "Thought for 4s" with a chevron; expanded shows the text in a muted, smaller font; while streaming show a shimmering label "يفكّر…".
- Tool call: one card per `tool_use` + its `tool_result`: icon, human name (`data.query` → "استعلام بيانات"), status (running spinner / done check / error), duration, expandable input (pretty JSON) and output (result object; if it has `rows` show a small table, else JSON).
- Approval: card with action summary, doctype, impact, payload summary table, buttons موافقة / رفض (with reason) / التفاصيل; after decision show the outcome and link the created document.
- Artifact: table with sticky header, "عرض الكل" loading `get_artifact`, CSV export; chart via `frappe.Chart`.
- Skill: chip "تم تحميل المهارة: X". Memory: chip "تم الحفظ في الذاكرة: …". Error block: red inline box with retry.
- User message: right-aligned in LTR (start-aligned in RTL is fine), light surface bubble, attachments as chips, hover actions: تعديل, نسخ.
- Assistant message: no bubble, avatar/agent name row, footer actions on hover: نسخ · إعادة التوليد · 👍 · 👎 · plus meta (model, tokens, duration).
- While pending/streaming: a pulsing dot + "يعمل…" with an elapsed timer inside the message; the composer's send button becomes "إيقاف".
- Sidebar: "محادثة جديدة", search, groups (مثبّت / اليوم / أمس / آخر 7 أيام / أقدم), hover menu (إعادة تسمية / تثبيت / أرشفة / حذف). Collapsible; a drawer under 992px.
- Composer: textarea autosize (max 40vh), Enter sends / Shift+Enter newline / `isComposing` guard, `/` opens a skill palette (list_skills), `@` opens document search, paste/drop files → `upload_file`, chips with progress, agent + model pickers as compact popovers, thinking toggle (off/adaptive/on) when the model supports it, context meter (context_tokens / context_window).
- Keyboard: `Ctrl/Cmd+K` new chat, `Esc` stop generation, `↑` in empty composer edits last user message.
- Auto-scroll only when the user is at the bottom; otherwise show a "↓ الأحدث" pill.
- Theme: our own tokens on `:root` with light/dark via Frappe's `[data-theme]` attribute; logical CSS properties for RTL; no `!important`, no JS height math (`100dvh` grid).
- Language: Arabic UI strings through `__()` with Arabic default text where the app has no translation.
