# Workspaces and sidebars (customize.request_workspace) — Frappe v16

Verified against `frappe/desk/doctype/workspace/workspace.py`, `desk/doctype/workspace_sidebar/workspace_sidebar.py`,
`desk/doctype/desktop_icon/desktop_icon.py`, `desk/desktop.py`, `boot.py get_sidebar_items` and
`public/js/frappe/ui/sidebar/sidebar.js` (Frappe 16.28). Every example passes `customize.check` (kind `workspace`).

## 1. What Frappe 16 shows

| Record | On screen | Notes |
|---|---|---|
| **Workspace** | the page: shortcuts, link cards, number cards, charts | `content` is the block layout; each block names a row by its **label** |
| **Workspace Sidebar** | the left menu | items: `Link` (DocType/Page/Report/Workspace/Dashboard/URL), `Section Break` (a collapsible group: the links after it have `child: 1`), `Spacer` |
| **Desktop Icon** | the tile on /desk | opens the sidebar of the same name |

The sidebar of a page is the one titled like the workspace, otherwise one that links the current DocType/report.
Frappe drops every sidebar item the user cannot open, and the whole sidebar when no link survives.

## 2. Operations

| operation | does | undo |
|---|---|---|
| `create` | a workspace (public by default) with its sidebar (default: a Home link) and desktop icon; `public: 0` makes a private one listed in the requester's My Workspaces | `delete` |
| `update` | replaces the sections you send (`shortcuts`, `cards`, `number_cards`, `charts`, `sidebar`) and `icon`, `indicator_color`, `parent_page`, `roles`; sections you do not send stay | `update` back |
| `disable` / `enable` | hides / shows a public workspace and its tile (Workspace Managers still see it) | the other one |
| `delete` | removes the workspace, its own sidebar and desktop icon | `create` (rebuilds it) |

**Replace semantics:** to add, remove or reorder, read the current lists with `customize.get` (kind `workspace`),
edit them and send the whole section back. Blocks keep their place and width; new ones go after the last block of
the same kind.

**App workspaces are never edited in place** (Selling, Accounting, Users… have a `module`/`app`: developer mode
writes the change into the app and migrate overwrites it). Copy instead: `create` with `copy_from: "Selling"` copies
its shortcuts, cards, widgets, layout and sidebar; change what you need in the same request.

## 3. Arguments

| Key | Format |
|---|---|
| `shortcuts` | `[{label, type: DocType|Report|Page|Dashboard|URL, link_to, url (URL), doc_view: List|Report Builder|Dashboard|Tree|New|Calendar|Kanban|Image, color, stats_filter}]` |
| `cards` | `[{label, icon, links: [{label, link_type: DocType|Page|Report, link_to, is_query_report, onboard}]}]` |
| `number_cards` / `charts` | names (or `{name, label}`) of existing Number Cards / Dashboard Charts (make them with `customize.request_dashboard`) |
| `sidebar` | `{header_icon, items: [...]}`, or `{remove: 1}`; a Section Break may carry its links in `items` |
| `roles` | only these roles see the workspace; empty = every desk user |
| `icon` | a Frappe icon name (lucide: `home`, `users`, `receipt-text`, `chart-column`, `wallet`, `package`…) or an emoji |
| `desktop_icon` | 1/0: the tile on /desk (public only) |

```json customize kind=workspace
{"operation": "create", "name": "Team Board (AI example)", "icon": "list-todo",
 "shortcuts": [{"label": "Open ToDos", "type": "DocType", "link_to": "ToDo", "doc_view": "List", "stats_filter": "[[\"ToDo\",\"status\",\"=\",\"Open\"]]"},
   {"label": "Notes", "type": "DocType", "link_to": "Note"}],
 "cards": [{"label": "Lists", "links": [{"label": "ToDo", "link_type": "DocType", "link_to": "ToDo"},
   {"label": "Note", "link_type": "DocType", "link_to": "Note"}]}],
 "sidebar": {"items": [
   {"label": "Home", "link_type": "Workspace", "link_to": "Team Board (AI example)", "icon": "home"},
   {"type": "Section Break", "label": "Work", "icon": "folder", "items": [
     {"label": "ToDo", "link_type": "DocType", "link_to": "ToDo", "icon": "list-todo"},
     {"label": "Note", "link_type": "DocType", "link_to": "Note", "icon": "notebook-pen"}]}]},
 "purpose": "One place for the team's tasks and notes"}
```

The card preview shows the sidebar as an indented list, before and after:

```text
- Home → Workspace: Team Board (AI example) (home)
▾ Work (folder)
    - ToDo → DocType: ToDo (list-todo)
    - Note → DocType: Note (notebook-pen)
```

## 4. Checks and how to fix them

| Code | Level | Fix |
|---|---|---|
| `roles.missing` | critical | needs System Manager **and** Workspace Manager (Frappe's own rule for public workspaces and sidebars) |
| `workspace.name_taken` / `workspace.sidebar_taken` | critical | another title, or `update` |
| `workspace.route_conflict` | critical | the title is already a DocType, Page or Workspace route (`/app/<slug>`): another title |
| `workspace.standard` | critical | app-owned workspace or sidebar: `create` with `copy_from` |
| `workspace.private` | critical | a private workspace cannot be hidden: `delete` it |
| `workspace.link_missing` | critical | every DocType/Report/Page/Workspace/Dashboard/Number Card/Chart must exist |
| `workspace.url` | critical | URLs start with `https://`, `http://` or `/` |
| `workspace.icon` | critical | the icon is not in Frappe's icon set: the message lists close names |
| `workspace.duplicate` / `workspace.block` | critical | labels must be unique per section (blocks find rows by label) and every block needs its row |
| `workspace.parent_page` | critical | nest only under an existing workspace |
| `workspace.frappe` | critical | Frappe's own validation failed in the dry run |
| `workspace.link_audience` | warning | some audience roles cannot open a link: Frappe hides it from them; fine when intended |
| `workspace.sidebar_empty` | warning | a sidebar without links is hidden by Frappe |
| `workspace.icon_taken` | warning | a desktop icon of that name exists: kept, no new tile |
