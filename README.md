# kestrel-feature-code

Agent self-modification with constitutional approval for Kestrel Sovereign. Provides tools for reading, searching, editing, testing, and committing source code — all gated by constitutional security controls requiring approval for destructive operations.

## Installation

```bash
uv pip install git+https://github.com/KestrelSovereignAI/kestrel-feature-code.git
```

## Dependencies

- `kestrel-sovereign-sdk`

## Usage

Once installed, `CodeFeature` is automatically discovered by kestrel-sovereign via the `kestrel_sovereign.features` entry point.

> **v0.2.0 rename:** `CodeEditFeature` → `CodeFeature`. The old name is kept as an alias through v0.2.x for entry-point cutover; remove all `CodeEditFeature` references before v0.3.0. All `@tool` methods now return `kestrel_sdk.tools.result.ToolResult` (was: `Dict[str, Any]`); see #1042 for the honesty contract.

### Commands

- `!code-read` — Read source files
- `!code-search` — Search codebase with patterns
- `!code-edit` — Edit files with exact text matching (requires approval)
- `!code-diff` — Show uncommitted changes
- `!code-commit` — Commit changes to git (requires approval)
- `!code-test` — Run pytest tests
- `!code-lint` — Run ruff linter
- `!code-logs` — View application logs
- `!code-rollback` — Revert to previous commit (requires approval)
- `!code-restart` — Signal server restart (requires approval)

## Configuration

No environment variables required.

## Development

```bash
uv pip install kestrel-sovereign-sdk && uv pip install -e .
uv run pytest
```
