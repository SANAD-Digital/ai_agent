# Document naming (customize.request_naming) — Frappe v16

Verified against `frappe/model/naming.py` (set_new_name, NamingSeries, parse_naming_series, _format_autoname),
`frappe/core/doctype/doctype/doctype.py` (validate_series, validate_autoincrement_autoname),
`frappe/custom/doctype/customize_form/customize_form.py`,
`frappe/core/doctype/document_naming_settings/document_naming_settings.py` and
`frappe/core/doctype/document_naming_rule/document_naming_rule.py` (Frappe 16.28).

## 1. Which operation?

| The user wants | Operation |
|---|---|
| every new document of a DocType named a new way ("INV-2026-00001", "by the customer name") | `set_doctype_naming` with `autoname` |
| another series to pick from in the Series field, or a different default series | `set_doctype_naming` with `naming_series_options` (the **full** list; the first is the default) |
| only **some** documents named differently ("returns get RET-…", "branch A gets A-…") | `create_rule` (a Document Naming Rule with conditions) |
| change / switch off / on an existing rule | `update_rule` / `disable_rule` / `enable_rule` with `rule` (its name) |

Every change affects **new documents only**; existing names never change.

Who names first (`set_new_name`): autoincrement/UUID DocTypes → amended documents (`-1`, `-2`) → enabled naming rules
by **priority, highest first** (the first whose conditions all match) → the DocType's own code (`autoname` method) →
the `autoname` property → a random hash.

## 2. `autoname` values

| Value | Naming rule | Names | Notes |
|---|---|---|---|
| `naming_series:` | By "Naming Series" field | the series the user picks + `.#####` | the DocType needs a `naming_series` field |
| `INV-.YYYY.-.#####` | Expression | INV-2026-00001 | parts split by dots; **must contain `#`** with a dot before it. Parts: YY YYYY MM DD JJJ WW, a fieldname, `{fieldname}` |
| `format:INV-{YYYY}-{#####}` | Expression (old style) | INV-2026-00001 | braced parts: date parts, `{#####}`, fieldnames; Frappe discourages it |
| `field:customer_name` | By fieldname | the field's value | the field becomes **unique** (a schema change); existing duplicates block it |
| `hash` | Random | 10 random characters | |
| `prompt` | Set by user | typed by the user | imports and the agent must send a name |

`autoincrement` and `UUID` change the name column and cannot be set here (Frappe: "Cannot change to/from
autoincrement autoname in Customize Form").

```json customize kind=naming
{"operation": "set_doctype_naming", "doctype": "ToDo", "autoname": "TODO-.YYYY.-.#####",
 "purpose": "Readable task numbers per year"}
```

```json customize kind=naming
{"operation": "set_doctype_naming", "doctype": "Journal Entry",
 "naming_series_options": ["ACC-JV-.YYYY.-", "ACC-OPEN-.YYYY.-"], "purpose": "A separate series for opening entries"}
```

## 3. Document Naming Rule

`doctype`, `prefix` (date parts and `{fieldname}` allowed, **no `#`**: the rule adds its own counter),
`prefix_digits` (1–10, default 5), `priority` (default 0, higher first), `conditions`
(`[{field, condition: = != > < >= <=, value}]`, all must match; none = every document).

```json customize kind=naming
{"operation": "create_rule", "doctype": "ToDo", "prefix": "URG-.YY.-", "prefix_digits": 4, "priority": 10,
 "conditions": [{"field": "priority", "condition": "=", "value": "High"}], "purpose": "Urgent tasks stand out"}
```

## 4. Checks and fixes

| Code | Level | Meaning → fix |
|---|---|---|
| `naming.doctype` | critical | core / single / protected / child / log DocType → not possible |
| `naming.column_type` | critical | to/from autoincrement or UUID → done only in the DocType itself |
| `naming.expression` | critical | no `#`, or `#` without a dot before it, or characters outside letters, digits, `- # . / { }` → write `PREFIX-.YYYY.-.#####` |
| `naming.format` | critical | a braced part that is no date part, `{###}` or field |
| `naming.series_field` | critical | `naming_series:` / series options on a DocType without a `naming_series` field → use an expression |
| `naming.series_invalid`, `naming.series_taken` | critical | invalid series, or one another DocType already offers → pick a unique prefix |
| `naming.field` | critical | `field:` with a missing, unstored or non-value field (Text, Table…) |
| `naming.field_duplicates` | critical | existing documents repeat the field's values: the unique index would fail → clean the data first or pick another field |
| `naming.collision` | critical | the next name already exists → change the prefix |
| `naming.frappe` | critical | Frappe's `validate_series` refuses it (e.g. "Series X already used in Y") |
| `naming.prefix`, `naming.prefix_digits`, `naming.condition_field` | critical | rule prefix with `#`, digits out of 1–10, a condition on a field the DocType lacks |
| `naming.rule_ignored` | critical | a rule on an autoincrement/UUID DocType never runs |
| `naming.rules_first`, `naming.controller` | warning | an enabled rule or the DocType's code names documents before this setting |
| `naming.overrides_series`, `naming.overrides_code` | warning | a rule replaces the user's Series choice / the DocType's code for matching documents |
| `naming.field_optional`, `naming.prompt`, `naming.no_counter`, `naming.priority_tie`, `naming.shadows`, `naming.no_match`, `naming.series_unused` | warning | read the message; usually a design question for the user |

The preview shows the **next three names** (computed from the current counters without using them), the
series per option, other rules of the DocType, and how many recent documents match a rule's conditions.

## 5. Undo

`set_doctype_naming` → the previous autoname / naming rule / series options; `create_rule` → the rule is
**disabled**; `update_rule` → the previous values; `disable_rule` ↔ `enable_rule`.
