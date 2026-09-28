### AI Agent

AI Agent

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app ai_agent
```

Standard agents, skills, grants, and safe default settings are installed automatically and refreshed on migrate. See `STANDARD_CONFIGURATION.md` for the app-managed baseline and customer deployment notes.

### MCP Server

The app includes an authenticated MCP Streamable HTTP endpoint for ChatGPT,
Claude, and other MCP clients. It reuses the existing tool registry, Frappe
permissions, audit trail, and approval workflow. See [MCP_SERVER.md](MCP_SERVER.md)
for secure setup and connection instructions.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ai_agent
pre-commit install
```

Pre-commit is configured to use the following tools for checking your code:

- ruff (lint, with autofix)
- basic hygiene hooks (valid JSON/YAML, merge-conflict markers, EOF newline on Python files)

Run the full backend test suite with:

```bash
bench --site <your-dev-site> run-tests --app ai_agent
```

### License

mit
# ai_agent
# ai_agent
