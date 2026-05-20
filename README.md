# kestrel-feature-code

Codebase tooling feature for Kestrel Sovereign agents — 10 tools spanning read-only inspection (read, search, diff, lint, logs) and approval-gated mutation (edit, commit, rollback, restart, test).

## Installation

```bash
uv pip install kestrel-feature-code
```

The feature is auto-discovered by Kestrel Sovereign via the `kestrel_sovereign.features` entry point — install it alongside `kestrel-sovereign` and `CodeFeature` registers itself at startup.

> **v0.2.0 rename:** `CodeEditFeature` → `CodeFeature`. The old name is kept as a deprecated alias through v0.2.x for entry-point and import cutover; importing it emits a `DeprecationWarning`. Removed in v0.3.0. All `@tool` methods now return `kestrel_sdk.tools.result.ToolResult` (was: `Dict[str, Any]`); see kestrel-sovereign #1042 for the honesty contract.

## Configuration

| Variable | Description |
|----------|-------------|
| `KESTREL_CODE_ROOT` | Root directory of the codebase the feature operates on (default: project root) |

## Tools

| Tool | Category | Description |
|------|----------|-------------|
| `code_read` | DATA_ACCESS | Read a source file |
| `code_search` | DATA_ACCESS | Search the codebase |
| `code_diff` | DATA_ACCESS | Show uncommitted git changes |
| `code_lint` | DATA_ACCESS | Run ruff linter |
| `code_logs` | DATA_ACCESS | View recent application logs |
| `code_edit` | SYSTEM | Edit a source file (approval-gated) |
| `code_commit` | SYSTEM | Commit staged changes (approval-gated) |
| `code_rollback` | SYSTEM | Roll back to a previous commit (approval-gated) |
| `code_restart` | SYSTEM | Signal server restart (approval-gated) |
| `code_test` | SYSTEM | Run pytest (full suite is approval-gated) |

## Dependencies

- `kestrel-sovereign-sdk>=0.14.1,<1` — base `Feature`, `tool`, `ToolCategory`, `ToolResult`

No runtime dependency on `kestrel-sovereign` itself; the feature operates against any codebase via `KESTREL_CODE_ROOT`.

## Development

```bash
uv pip install -e '.[test]'
uv run pytest
```

## License

Apache-2.0
