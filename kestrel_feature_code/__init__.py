"""
Kestrel Feature Code — Agent self-modification with constitutional approval.

Extracted from kestrel-sovereign as a standalone feature package.

Enables the Kestrel Agent to edit its own source code with proper
security controls and approval flows. Sovereign-only: only agents that
own their own codebase should use this feature.

Key Principles:
1. All code edits require explicit user approval
2. Changes are tracked via git commits
3. Edits use exact text matching (no regex) for safety
4. Server restart can be signaled after changes

Tools:
    !code-read <path>           Read a source file
    !code-search <pattern>      Search for text in codebase
    !code-edit <path>           Edit a source file (requires approval)
    !code-diff <path>           Show uncommitted changes
    !code-commit <message>      Commit staged changes (requires approval)
    !code-restart               Signal server restart (requires approval)
    !code-test [path]           Run pytest tests
    !code-lint [path]           Run ruff linter
    !code-logs                  View recent application logs
    !code-rollback [commit]     Rollback to previous commit (requires approval)
"""

from .feature import CodeFeature

__all__ = ["CodeFeature"]


def __getattr__(name: str):
    """Lazy backward-compat for ``from kestrel_feature_code import
    CodeEditFeature``. Removed in v0.3.0.

    Emits the DeprecationWarning here directly (not via the
    ``feature`` submodule's __getattr__) so the stacklevel points at
    the user's import site. Going through the submodule alias would
    add an extra frame and attribute the warning to ``__init__.py``
    instead of the user's code (claude review round 2 finding #3).
    """
    if name == "CodeEditFeature":
        import warnings
        warnings.warn(
            "CodeEditFeature is a deprecated alias for CodeFeature; "
            "the alias will be removed in v0.3.0. Update imports to "
            "``from kestrel_feature_code import CodeFeature``.",
            DeprecationWarning,
            stacklevel=2,
        )
        return CodeFeature
    raise AttributeError(
        f"module 'kestrel_feature_code' has no attribute {name!r}"
    )
