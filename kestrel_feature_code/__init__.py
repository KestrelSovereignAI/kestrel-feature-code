"""
Kestrel Feature Code — codebase tooling for Kestrel Sovereign agents.

Registers ``CodeFeature`` (renamed from ``CodeEditFeature`` in
v0.2.0) via the ``kestrel_sovereign.features`` entry-point group;
auto-discovered when installed alongside kestrel-sovereign.

The feature covers general codebase tooling — read, search, diff,
lint, logs, test — alongside the approval-gated mutation tools (edit,
commit, rollback, restart). All mutation requires explicit user
approval; read-only operations do not. Returns
``kestrel_sdk.tools.result.ToolResult`` from every @tool surface.

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

from importlib.metadata import PackageNotFoundError, version as _version

from .feature import CodeFeature

try:
    __version__ = _version("kestrel-feature-code")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = ["CodeFeature", "__version__"]


def __getattr__(name: str):
    """Lazy backward-compat for ``from kestrel_feature_code import
    CodeEditFeature``. Removed in v0.3.0.

    Emits the DeprecationWarning here directly (not via the
    ``feature`` submodule's __getattr__) so the stacklevel points at
    the user's import site. Going through the submodule alias would
    add an extra frame and attribute the warning to ``__init__.py``
    instead of the user's code.
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
