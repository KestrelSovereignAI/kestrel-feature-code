"""
Kestrel Feature Code — codebase tooling for Kestrel Sovereign agents.

Extracted from kestrel-sovereign as a standalone feature package.
Registers ``CodeEditFeature`` via the ``kestrel_sovereign.features``
entry-point group; auto-discovered when installed alongside
kestrel-sovereign.

Despite the historical class name, the feature covers general codebase
tooling — read, search, diff, lint, logs, test — alongside the
approval-gated mutation tools (edit, commit, rollback, restart). All
mutation requires explicit user approval; read-only operations do not.

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

from .feature import CodeEditFeature

try:
    __version__ = _version("kestrel-feature-code")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = ["CodeEditFeature", "__version__"]
