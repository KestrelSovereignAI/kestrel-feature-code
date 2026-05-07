# kestrel-feature-code — Agent Instructions

See [README.md](README.md) for package overview.

## Package Structure

```
kestrel_feature_code/
├── pyproject.toml
├── README.md
└── kestrel_feature_code/
    ├── __init__.py
    └── feature.py    # CodeFeature — all tools defined here
```

## Entry Points

- `kestrel_sovereign.features`: `CodeFeature = "kestrel_feature_code.feature:CodeFeature"`
- Backward-compat alias `CodeEditFeature` resolves to the same class via `__getattr__` (emits `DeprecationWarning`); removed in v0.3.0.

## Key Files to Read First

1. `kestrel_feature_code/feature.py` — Complete feature implementation with all code tools

## Running Tests

```bash
uv run pytest
```

## Agent-Specific Instructions

- Destructive operations (edit, commit, rollback, restart) require constitutional approval
- Read-only operations (read, search, diff, logs) do not require approval
- All file operations are sandboxed to the agent's working directory
