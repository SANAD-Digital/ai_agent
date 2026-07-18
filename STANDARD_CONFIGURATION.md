# AI Agent Standard Configuration

This app ships its baseline agent configuration as app-managed data so every customer site can be initialized without manually copying prompts, skills, or grants from another site.

## Source of Truth

- Standard agents are defined in `ai_agent/install.py` in `_standard_agents()`.
- Standard skills are stored as app files in `ai_agent/skills/*/SKILL.md`.
- Standard skill grants are created by `ai_agent/install.py` in `create_default_skill_grants()`.
- Safe AI Settings defaults are created by `ai_agent/install.py` in `create_default_settings()`.

## What Syncs Automatically

The app runs `ai_agent.install.ensure_standard_configuration` after install and after migrate.

It will:

- Create or refresh the standard agents.
- Sync every app-file skill from `SKILL.md`.
- Re-enable trusted app-file skills when they are part of the shipped baseline.
- Grant app-file skills to the standard agents for all users.
- Fill missing safe settings defaults.

It will not:

- Copy API keys or provider secrets.
- Overwrite configured provider credentials.
- Export customer-specific companies, users, roles, or private business data.

## Customer Deployment

Run one of these after installing or updating the app:

```bash
bench --site <site-name> migrate
```

Or run the sync directly:

```bash
bench --site <site-name> execute ai_agent.install.ensure_standard_configuration
```

## Adding a Standard Skill

1. Create a folder under `ai_agent/skills/<skill-name>/`.
2. Add `SKILL.md` with frontmatter:

```markdown
---
name: skill-name
description: Short description.
allow_implicit_invocation: 1
---
```

3. Put the agent instructions below the frontmatter.
4. Run migrate or execute `ai_agent.install.ensure_standard_configuration`.

The skill will then be available on every customer site after app update.
