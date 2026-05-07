"""
Code Feature - Source-code reading + modification capabilities for Kestrel agents.

This feature exposes a code tool surface (read, search, diff, lint, test,
logs, edit, commit, rollback, restart) with constitutional approval gating
on the destructive operations. All @tool methods return
``kestrel_sdk.tools.result.ToolResult`` per the kestrel-sovereign #1042
narration-honesty contract.

Renamed from ``CodeEditFeature`` in v0.2.0 — the surface is broader than
just edits. The old class name is kept as a deprecated alias for v0.2.x
and removed in v0.3.0.
"""
import asyncio
import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import ToolCategory
from kestrel_sdk.tools.result import ToolResult

logger = logging.getLogger(__name__)

# Find binaries
GIT_PATH = shutil.which("git") or "/usr/bin/git"
PYTHON_PATH = shutil.which("python3") or shutil.which("python") or "/usr/bin/python3"

# Default to the kestrel-sovereign project root
DEFAULT_CODE_ROOT = os.environ.get(
    "KESTREL_CODE_ROOT",
    str(Path(__file__).parent.parent.parent.parent)  # Up to kestrel-sovereign/
)

# Timeout constants (in seconds)
CODE_REVIEW_TIMEOUT = 300  # 5 minutes for user approval of code changes
TEST_SUITE_TIMEOUT = 300   # 5 minutes for running test suite
LINT_TIMEOUT = 60          # 1 minute for linting operations
GIT_OPERATION_TIMEOUT = 30  # 30 seconds for git commands (diff, commit, rollback)
GIT_QUICK_TIMEOUT = 10     # 10 seconds for quick git commands (rev-parse)


async def _run_subprocess(*args, **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess.run off the event loop via asyncio.to_thread."""
    return await asyncio.to_thread(functools.partial(subprocess.run, *args, **kwargs))


async def _read_text(path: Path) -> str:
    """Read text off the event loop."""
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


async def _write_text(path: Path, content: str) -> int:
    """Write text off the event loop."""
    return await asyncio.to_thread(path.write_text, content, encoding="utf-8")


def _search_file_contents(
    code_root: Path,
    resolved: Path,
    pattern: str,
    file_pattern: str,
) -> tuple[list[dict], int, list[dict]]:
    """Search files synchronously for offloading via asyncio.to_thread.

    Returns ``(matches[:50], total_matches, skipped_files)``.

    Caller MUST surface ``total_matches`` and ``skipped_files`` so
    the LLM doesn't claim a complete search when binary files,
    permission errors, or decode failures excluded part of the tree
    (#1042 honesty contract; codex round-2 finding #2).

    Symlink-safe (codex round-3 finding #2): each candidate file is
    resolved and re-checked for containment under code_root before
    being read. A repo file ``leak.py -> /etc/passwd`` would
    otherwise pass the search-root check but leak data on read.
    """
    matches = []
    total_matches = 0
    skipped: list[dict] = []

    for file_path in resolved.rglob(file_pattern):
        if not file_path.is_file():
            continue

        # Re-check containment after following the symlink. ``rglob``
        # itself doesn't traverse through symlinks to directories,
        # but it WILL list a symlinked file whose resolution escapes.
        try:
            real = file_path.resolve()
            if not real.is_relative_to(code_root):
                # Display name uses the relative path (relative to
                # code_root) when possible; fall back to the link
                # name if the link itself is outside (shouldn't
                # happen since rglob is rooted in resolved, but be
                # defensive).
                try:
                    display = str(file_path.relative_to(code_root))
                except ValueError:
                    display = str(file_path)
                skipped.append({
                    "file": display,
                    "reason": "symlink_escape: target is outside code_root",
                })
                continue
        except (OSError, RuntimeError, ValueError) as e:
            try:
                display = str(file_path.relative_to(code_root))
            except ValueError:
                display = str(file_path)
            skipped.append({
                "file": display,
                "reason": f"resolve_failed: {e}",
            })
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            skipped.append({
                "file": str(file_path.relative_to(code_root)),
                "reason": f"decode_error: {e}",
            })
            continue
        except (PermissionError, OSError) as e:
            skipped.append({
                "file": str(file_path.relative_to(code_root)),
                "reason": f"{type(e).__name__}: {e}",
            })
            continue

        for i, line in enumerate(content.split("\n"), 1):
            if pattern in line:
                total_matches += 1
                if len(matches) < 50:
                    matches.append({
                        "file": str(file_path.relative_to(code_root)),
                        "line": i,
                        "content": line.strip()[:200],
                    })

    return matches, total_matches, skipped


class CodeFeature(Feature):
    """Feature for reading and modifying source code with approval gates.

    Renamed from ``CodeEditFeature`` in v0.2.0 — the surface is broader
    than just edits (read, search, diff, lint, test, logs, commit,
    rollback, restart). All mutation paths require user approval via
    SecurityFeature.
    """

    tool_name = "code"
    tool_description = "Read and modify the agent's own source code with approval"

    def __init__(self, agent=None, code_root: str = None):
        super().__init__(agent)
        self.code_root = Path(code_root or DEFAULT_CODE_ROOT).resolve()
        self._pending_restart = False

    async def initialize(self):
        logger.info(f"CodeFeature initialized with root: {self.code_root}")

    @staticmethod
    def _coerce_optional_int(name: str, value: Any) -> Optional[int]:
        """Accept None / int / str-of-digits; reject other shapes
        with a ValueError that callers convert to ToolResult.failed
        (codex round-3 finding #5)."""
        if value is None:
            return None
        if isinstance(value, bool):
            # bool is a subclass of int but ``code_read(start_line=True)``
            # is meaningless and almost certainly a mistake.
            raise ValueError(
                f"Invalid {name} '{value}': must be an int or None, "
                f"not bool"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ValueError(
            f"Invalid {name} '{value}': must be an int, a numeric "
            f"string, or None"
        )

    @staticmethod
    def _coerce_required_int(name: str, value: Any, *, default: int) -> int:
        """Like _coerce_optional_int but None falls back to the
        default. ``code_logs(lines=None)`` is not great UX but
        shouldn't be a hard failure."""
        if value is None:
            return default
        if isinstance(value, bool):
            raise ValueError(
                f"Invalid {name} '{value}': must be an int, not bool"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ValueError(
            f"Invalid {name} '{value}': must be an int or numeric "
            f"string"
        )

    def _is_inside_root(self, path: Path) -> bool:
        """Resolve ``path`` (following symlinks) and check
        ``is_relative_to(code_root)``. Used by tool methods that
        traverse the tree (code_search via rglob, code_logs default
        fallbacks, code_restart writing .restart_requested) to
        re-check containment AFTER following symlinks.

        Without this, a symlink ``leak.py -> /etc/passwd`` planted
        in the repo would pass the entry-level _resolve_path check
        and then leak data when the tool reads through it (codex
        round-3 findings #2, #3, #4).
        """
        try:
            resolved = path.resolve()
            return resolved.is_relative_to(self.code_root)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to code root, with security checks.

        Raises ``ValueError`` for any malformed/escaping argument;
        every @tool method that calls this MUST catch ValueError and
        convert to ``ToolResult.failed`` so the security violation
        lands in the envelope, not as a raised exception.

        Use ``Path.is_relative_to`` for the containment check, NOT
        ``str.startswith``. The string form lets sibling-prefix paths
        escape: code root ``/tmp/repo`` would match
        ``/tmp/repo2/secret.py``.

        Codex round-2 finding #3: also reject ``None`` / non-string
        inputs as ValueError so a malformed @tool arg lands in the
        envelope instead of raising AttributeError out of
        ``path.startswith``.
        """
        if not isinstance(path, str):
            raise ValueError(
                f"Path must be a string, got {type(path).__name__}"
            )
        if path.startswith("/"):
            path = path.lstrip("/")
        try:
            resolved = (self.code_root / path).resolve()
        except (OSError, RuntimeError) as e:
            # ``Path.resolve`` can raise on certain edge cases
            # (windows path syntax, recursion loops). Surface as a
            # ValueError so callers' standard envelope handling
            # picks it up.
            raise ValueError(f"Could not resolve path '{path}': {e}") from e
        try:
            is_inside = resolved.is_relative_to(self.code_root)
        except (TypeError, ValueError):
            is_inside = False
        if not is_inside:
            raise ValueError(f"Path escapes code root: {path}")
        return resolved

    def _get_security_feature(self):
        if hasattr(self.agent, "get_feature"):
            return self.agent.get_feature("security")
        if hasattr(self.agent, "features"):
            return self.agent.features.get("security")
        return None

    async def _request_approval(self, action: str, details: Dict[str, Any]) -> bool:
        security = self._get_security_feature()
        if not security or not hasattr(security, "approval_queue"):
            logger.warning("SecurityFeature not available, cannot proceed with code edit")
            return False
        try:
            approved, _ = await security.approval_queue.request_approval(
                feature_name="code",
                tool_name=action,
                tool_args=details,
                timeout=CODE_REVIEW_TIMEOUT,
            )
            return approved
        except Exception as e:
            logger.error(f"Approval request failed: {e}", exc_info=True)
            return False

    # ============== Read Operations (No Approval Required) ==============

    @tool(
        name="code_read",
        description="Read a source file from the agent's codebase.",
        category=ToolCategory.DATA_ACCESS,
        command_prefix="!code-read",
    )
    async def code_read(
        self,
        path: str,
        start_line: int = None,
        end_line: int = None,
    ) -> ToolResult:
        # Coerce optional int args BEFORE doing anything else so a
        # malformed ``start_line="x"`` lands in ToolResult.failed
        # instead of TypeError-ing out of the slice (codex round-3
        # finding #5).
        try:
            start_line = self._coerce_optional_int(
                "start_line", start_line
            )
            end_line = self._coerce_optional_int("end_line", end_line)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})

        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        if not resolved.exists():
            return ToolResult.failed(f"File not found: {path}", data={"path": path})
        if not resolved.is_file():
            return ToolResult.failed(f"Not a file: {path}", data={"path": path})
        try:
            content = await _read_text(resolved)
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        # ``splitlines()`` correctly handles trailing newlines —
        # ``"line1\nline2\n".splitlines()`` is ``["line1", "line2"]``
        # (2 lines), not the 3-element list ``split("\n")`` produces.
        all_lines = content.splitlines()
        if start_line or end_line:
            start_idx = (start_line - 1) if start_line else 0
            end_idx = end_line if end_line else len(all_lines)
            shown = all_lines[start_idx:end_idx]
            content = "\n".join(shown)
        else:
            shown = all_lines

        rel = str(resolved.relative_to(self.code_root))
        return ToolResult.ok(
            confirmation=f"Read {len(shown)} line(s) from {rel}",
            data={
                "path": rel,
                "content": content,
                "total_lines": len(all_lines),
                "shown_lines": len(shown),
            },
        )

    @tool(
        name="code_search",
        description="Search for text in the agent's codebase.",
        category=ToolCategory.DATA_ACCESS,
        command_prefix="!code-search",
    )
    async def code_search(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str = "*.py",
    ) -> ToolResult:
        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        if not resolved.exists():
            return ToolResult.failed(f"Path not found: {path}", data={"path": path})

        try:
            matches, total_matches, skipped = await asyncio.to_thread(
                _search_file_contents,
                self.code_root,
                resolved,
                pattern,
                file_pattern,
            )
        except Exception as e:
            return ToolResult.failed(
                str(e), data={"pattern": pattern, "path": path}
            )

        # Surface BOTH the truncated match list AND the real total
        # so the LLM can't lie about the result count (#1042).
        truncated = total_matches > len(matches)
        confirmation = (
            f"Found {total_matches} match(es) for '{pattern}'"
            if not truncated
            else (
                f"Found {total_matches} match(es) for '{pattern}' "
                f"(showing first {len(matches)})"
            )
        )
        data = {
            "pattern": pattern,
            "matches": matches,
            "total_matches": total_matches,
            "truncated": truncated,
            "skipped_files": skipped,
            "skipped_count": len(skipped),
        }

        # Skipped files = partial-success: the search WAS performed,
        # but excluded files outside the LLM's view. Downgrade to
        # PARTIAL so the audit hook sees non-OK and the user knows
        # the result isn't a complete picture (codex round-2
        # finding #2).
        if skipped:
            return ToolResult.partial(
                confirmation=(
                    f"{confirmation}; {len(skipped)} file(s) were skipped "
                    f"(unreadable or undecodable)"
                ),
                error=(
                    f"{len(skipped)} file(s) skipped during search; "
                    f"see skipped_files for details"
                ),
                data=data,
            )

        return ToolResult.ok(confirmation=confirmation, data=data)

    # ============== Write Operations (Require Approval) ==============

    @tool(
        name="code_edit",
        description="Edit a source file by replacing exact text. Requires approval.",
        category=ToolCategory.SYSTEM,
        command_prefix="!code-edit",
    )
    async def code_edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        description: str = None,
    ) -> ToolResult:
        # Validate text args are strings (codex round-3 finding #5).
        # ``old_text=123`` would TypeError out of ``content.count``
        # otherwise.
        for arg_name, arg_val in (("old_text", old_text), ("new_text", new_text)):
            if not isinstance(arg_val, str):
                return ToolResult.failed(
                    f"Invalid {arg_name}: must be a string, got "
                    f"{type(arg_val).__name__}",
                    data={"path": path, "arg": arg_name},
                )

        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        if not resolved.exists():
            return ToolResult.failed(f"File not found: {path}", data={"path": path})

        try:
            content = await _read_text(resolved)
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        count = content.count(old_text)
        if count == 0:
            return ToolResult.failed(
                "Text to replace not found in file",
                data={
                    "path": path,
                    "hint": "Ensure old_text matches exactly, including whitespace",
                },
            )
        if count > 1:
            return ToolResult.failed(
                f"Text appears {count} times, must be unique",
                data={
                    "path": path,
                    "occurrences": count,
                    "hint": "Add more context to make the match unique",
                },
            )

        try:
            approved = await self._request_approval(
                "code_edit",
                {
                    "path": path,
                    "old_text": old_text[:500] + ("..." if len(old_text) > 500 else ""),
                    "new_text": new_text[:500] + ("..." if len(new_text) > 500 else ""),
                    "description": description or "Code modification",
                },
            )
        except Exception as e:
            return ToolResult.failed(
                f"Approval check failed: {e}",
                data={"path": path, "requires_approval": True},
            )

        if not approved:
            return ToolResult.failed(
                "Edit not approved",
                data={"path": path, "requires_approval": True},
            )

        try:
            new_content = content.replace(old_text, new_text, 1)
            await _write_text(resolved, new_content)
        except Exception as e:
            return ToolResult.failed(
                f"Approved edit failed during write: {e}",
                data={"path": path, "warning": "approval granted but write failed"},
            )

        rel = str(resolved.relative_to(self.code_root))
        logger.info(f"Applied code edit to {rel}: {description or 'no description'}")
        return ToolResult.ok(
            confirmation=f"Edited {rel}",
            data={
                "path": rel,
                "description": description,
                "chars_removed": len(old_text),
                "chars_added": len(new_text),
            },
        )

    @tool(
        name="code_diff",
        description="Show uncommitted git changes in the codebase.",
        category=ToolCategory.DATA_ACCESS,
        command_prefix="!code-diff",
    )
    async def code_diff(self, path: str = ".") -> ToolResult:
        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        try:
            result = await _run_subprocess(
                [GIT_PATH, "diff", str(resolved)],
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=GIT_OPERATION_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                "Git diff operation timed out",
                data={"path": path, "timeout_seconds": GIT_OPERATION_TIMEOUT},
            )
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        if result.returncode != 0:
            return ToolResult.failed(
                result.stderr or "git diff returned non-zero",
                data={"path": path, "returncode": result.returncode},
            )

        diff_text = result.stdout or ""
        has_changes = bool(diff_text.strip())
        confirmation = (
            "No uncommitted changes"
            if not has_changes
            else f"Showing uncommitted diff for {path}"
        )
        return ToolResult.ok(
            confirmation=confirmation,
            data={
                "diff": diff_text or "(no changes)",
                "has_changes": has_changes,
                "path": path,
            },
        )

    @tool(
        name="code_commit",
        description="Commit staged changes to git. Requires approval.",
        category=ToolCategory.SYSTEM,
        command_prefix="!code-commit",
    )
    async def code_commit(
        self,
        message: str,
        files: str = ".",
    ) -> ToolResult:
        # Validate the path BEFORE asking for approval. If we already
        # know the operation is structurally invalid, don't burn the
        # user's attention on it.
        try:
            resolved = self._resolve_path(files)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"files": files})

        try:
            approved = await self._request_approval(
                "code_commit",
                {"message": message, "files": files},
            )
        except Exception as e:
            return ToolResult.failed(
                f"Approval check failed: {e}",
                data={"requires_approval": True},
            )
        if not approved:
            return ToolResult.failed(
                "Commit not approved",
                data={"requires_approval": True},
            )

        # Use capture_output so a failed `git add` lands in the
        # envelope instead of raising CalledProcessError out of
        # subprocess(check=True).
        try:
            add_result = await _run_subprocess(
                [GIT_PATH, "add", str(resolved)],
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=GIT_OPERATION_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed("Git add operation timed out")
        except Exception as e:
            return ToolResult.failed(str(e))
        if add_result.returncode != 0:
            return ToolResult.failed(
                add_result.stderr or "git add failed",
                data={"step": "add", "returncode": add_result.returncode},
            )

        try:
            result = await _run_subprocess(
                [GIT_PATH, "commit", "-m", message],
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=GIT_OPERATION_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed("Git commit operation timed out")
        except Exception as e:
            return ToolResult.failed(str(e))

        if result.returncode != 0:
            stdout_text = result.stdout or ""
            stderr_text = result.stderr or ""
            # ``git commit`` returns non-zero on "nothing to commit".
            # That's a no-op, NOT a success that pretends a commit
            # happened (#1042 honesty).
            if "nothing to commit" in stdout_text or "nothing to commit" in stderr_text:
                return ToolResult.ok(
                    confirmation="Nothing to commit (no-op)",
                    data={"committed": False, "message": message},
                )
            return ToolResult.failed(
                stderr_text or "git commit failed",
                data={"step": "commit", "returncode": result.returncode},
            )

        try:
            hash_result = await _run_subprocess(
                [GIT_PATH, "rev-parse", "HEAD"],
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=GIT_QUICK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.partial(
                confirmation="Committed (commit hash unavailable)",
                error="git rev-parse HEAD timed out after the commit succeeded",
                data={"committed": True, "message": message},
            )
        except Exception as e:
            return ToolResult.partial(
                confirmation="Committed (commit hash unavailable)",
                error=f"git rev-parse failed after commit: {e}",
                data={"committed": True, "message": message},
            )

        # rev-parse can also exit non-zero without raising (corrupted
        # repo, detached refs). Check the returncode explicitly so
        # an empty commit_hash doesn't become "Committed : message".
        if hash_result.returncode != 0:
            return ToolResult.partial(
                confirmation="Committed (commit hash unavailable)",
                error=(
                    f"git rev-parse HEAD returned non-zero "
                    f"({hash_result.returncode}) after the commit succeeded: "
                    f"{hash_result.stderr or '(no stderr)'}"
                ),
                data={
                    "committed": True,
                    "message": message,
                    "rev_parse_returncode": hash_result.returncode,
                },
            )

        commit_hash = hash_result.stdout.strip()[:8]
        logger.info(f"Committed changes: {commit_hash} - {message}")
        return ToolResult.ok(
            confirmation=f"Committed {commit_hash}: {message}",
            data={
                "committed": True,
                "commit": commit_hash,
                "message": message,
            },
        )

    @tool(
        name="code_restart",
        description="Signal that the server should restart to apply code changes.",
        category=ToolCategory.SYSTEM,
        command_prefix="!code-restart",
    )
    async def code_restart(self, reason: str = None) -> ToolResult:
        try:
            approved = await self._request_approval(
                "code_restart",
                {"reason": reason or "Apply code changes"},
            )
        except Exception as e:
            return ToolResult.failed(
                f"Approval check failed: {e}",
                data={"requires_approval": True},
            )
        if not approved:
            return ToolResult.failed(
                "Restart not approved",
                data={"requires_approval": True},
            )

        try:
            self._pending_restart = True
            restart_file = self.code_root / ".restart_requested"
            # Re-check containment AFTER symlink resolution (codex
            # round-3 finding #4). If ``.restart_requested`` is a
            # pre-existing symlink to a file outside code_root,
            # writing through it would escape the sandbox.
            if restart_file.exists() and not self._is_inside_root(restart_file):
                return ToolResult.partial(
                    confirmation="Restart approved (in-memory flag set)",
                    error=(
                        ".restart_requested exists as a symlink pointing "
                        "outside code_root; refusing to write through it"
                    ),
                    data={
                        "pending_restart": True,
                        "warning": (
                            "external process managers may not see the signal"
                        ),
                    },
                )
            await _write_text(restart_file, reason or "Code changes applied")
        except Exception as e:
            return ToolResult.partial(
                confirmation="Restart approved (in-memory flag set)",
                error=f"could not write .restart_requested: {e}",
                data={
                    "pending_restart": True,
                    "warning": "external process managers may not see the signal",
                },
            )

        logger.info(f"Restart signaled: {reason}")
        return ToolResult.ok(
            confirmation="Restart signaled. Server will restart when possible.",
            data={"pending_restart": True, "reason": reason},
        )

    @property
    def pending_restart(self) -> bool:
        return self._pending_restart

    # ============== Testing & Validation ==============

    @tool(
        name="code_test",
        description="Run pytest tests on the codebase. Requires approval for full test suite.",
        category=ToolCategory.SYSTEM,
        command_prefix="!code-test",
    )
    async def code_test(
        self,
        path: str = None,
        verbose: bool = False,
        fail_fast: bool = True,
    ) -> ToolResult:
        cmd = [PYTHON_PATH, "-m", "pytest"]

        if path:
            try:
                resolved = self._resolve_path(path)
            except ValueError as e:
                return ToolResult.failed(str(e), data={"path": path})
            cmd.append(str(resolved))
        else:
            try:
                approved = await self._request_approval(
                    "code_test",
                    {"scope": "full test suite", "reason": "Running all tests"},
                )
            except Exception as e:
                return ToolResult.failed(
                    f"Approval check failed: {e}",
                    data={"requires_approval": True},
                )
            if not approved:
                return ToolResult.failed(
                    "Full test suite requires approval",
                    data={"requires_approval": True},
                )

        if verbose:
            cmd.append("-v")
        if fail_fast:
            cmd.append("-x")
        cmd.extend(["--tb=short", "--no-header", "-q"])

        try:
            result = await _run_subprocess(
                cmd,
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=TEST_SUITE_TIMEOUT,
                env={**os.environ, "PYTHONPATH": str(self.code_root)},
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                f"Test timeout ({TEST_SUITE_TIMEOUT}s)",
                data={"path": path, "timeout_seconds": TEST_SUITE_TIMEOUT},
            )
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        passed = result.returncode == 0
        out = result.stdout or ""
        err = result.stderr or ""
        data = {
            "passed": passed,
            "return_code": result.returncode,
            "output": out[-2000:] if len(out) > 2000 else out,
            "errors": err[-1000:] if err else None,
        }
        # Status-level honesty: failed tests = PARTIAL, not OK. The
        # tool ran correctly but the test run failed; audit hooks
        # reading ``result.status`` need to see non-OK on failure.
        if passed:
            return ToolResult.ok(confirmation="Tests passed", data=data)
        return ToolResult.partial(
            confirmation=f"Tests FAILED (return code {result.returncode})",
            error=f"pytest exited with non-zero return code {result.returncode}",
            data=data,
        )

    @tool(
        name="code_lint",
        description="Run linters (ruff) on source files.",
        category=ToolCategory.DATA_ACCESS,
        command_prefix="!code-lint",
    )
    async def code_lint(self, path: str = ".") -> ToolResult:
        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})

        try:
            result = await _run_subprocess(
                [PYTHON_PATH, "-m", "ruff", "check", str(resolved), "--output-format=text"],
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=LINT_TIMEOUT,
                env={**os.environ, "PYTHONPATH": str(self.code_root)},
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                "Linting operation timed out",
                data={"path": path, "timeout_seconds": LINT_TIMEOUT},
            )
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        # Distinguish lint-found-issues (rc=1) from invocation
        # errors (rc=2 or stderr present). Codex round-1 finding #2:
        # if ruff is missing, config parsing fails, etc., the
        # previous code returned ``Lint found 0 issue line(s)`` —
        # a false confirmation. Treat those as ToolResult.failed so
        # the operator sees the real problem.
        raw_stdout = result.stdout or ""
        raw_stderr = result.stderr or ""
        issue_lines = [l for l in raw_stdout.splitlines() if l.strip()]
        issue_count = len(issue_lines)

        # ruff exit code semantics: 0=clean, 1=lint issues found,
        # 2=invocation error (missing config, bad args, etc.).
        # Anything ≥2 OR (non-zero with no issue output AND stderr
        # present) is an invocation error, not a lint finding.
        if result.returncode >= 2:
            return ToolResult.failed(
                f"Ruff invocation failed (return code {result.returncode}): "
                f"{raw_stderr.strip() or 'unknown error'}",
                data={
                    "path": path,
                    "returncode": result.returncode,
                    "stderr": raw_stderr[-1000:] if raw_stderr else None,
                },
            )
        if result.returncode != 0 and issue_count == 0 and raw_stderr.strip():
            # Non-zero exit, no findings on stdout, but stderr says
            # something happened — that's an invocation error too.
            return ToolResult.failed(
                f"Ruff invocation failed: {raw_stderr.strip()}",
                data={
                    "path": path,
                    "returncode": result.returncode,
                    "stderr": raw_stderr[-1000:],
                },
            )

        has_issues = result.returncode != 0
        out = raw_stdout if raw_stdout.strip() else "(no issues)"
        confirmation = (
            "Lint clean (no issues)"
            if not has_issues
            else f"Lint found {issue_count} issue line(s)"
        )
        return ToolResult.ok(
            confirmation=confirmation,
            data={
                "has_issues": has_issues,
                "output": out[-2000:] if len(out) > 2000 else out,
                "issue_count": issue_count if has_issues else 0,
            },
        )

    @tool(
        name="code_logs",
        description="View recent application logs.",
        category=ToolCategory.DATA_ACCESS,
        command_prefix="!code-logs",
    )
    async def code_logs(
        self,
        lines: int = 50,
        errors_only: bool = False,
        log_file: str = None,
    ) -> ToolResult:
        # Coerce ``lines`` and ``errors_only`` (codex round-3 finding
        # #5): a malformed ``lines="10"`` would TypeError out of the
        # ``log_lines[-lines:]`` slice; ``errors_only="true"``
        # (string) is truthy and would silently filter unintended
        # results.
        try:
            lines = self._coerce_required_int("lines", lines, default=50)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"lines": lines})
        if not isinstance(errors_only, bool):
            return ToolResult.failed(
                f"Invalid errors_only: must be a bool, got "
                f"{type(errors_only).__name__}",
                data={"errors_only": errors_only},
            )

        # User-supplied ``log_file`` MUST go through _resolve_path —
        # without it, a relative escape like ``../../../etc/passwd``
        # would read outside the code-root sandbox.
        user_path: Optional[Path] = None
        if log_file is not None:
            try:
                user_path = self._resolve_path(log_file)
            except ValueError as e:
                return ToolResult.failed(str(e), data={"log_file": log_file})

        # Default-log discovery is constrained to ``code_root``
        # (codex round-1 finding #3). The previous default included
        # ``/tmp/kestrel-claw.log``, which violated the package's
        # sandbox contract: read-only ops must stay inside the
        # working directory unless the user explicitly opts out via
        # an in-root ``log_file=`` (which goes through _resolve_path).
        log_paths: List[Optional[Path]] = [
            user_path,
            self.code_root / "logs" / "kestrel.log",
            self.code_root / "kestrel.log",
        ]

        log_content: Optional[str] = None
        used_path: Optional[str] = None
        try:
            for p in log_paths:
                if p is None:
                    continue
                if not p.exists():
                    continue
                # Re-check containment after symlink resolution
                # (codex round-3 finding #3). The default fallbacks
                # construct paths from ``code_root / "kestrel.log"``
                # which look fine syntactically but could be
                # symlinks pointing outside the sandbox.
                if not self._is_inside_root(p):
                    continue
                log_content = await _read_text(p)
                used_path = str(p)
                break
        except Exception as e:
            return ToolResult.failed(str(e))

        if log_content is None:
            return ToolResult.failed(
                "No log file found",
                data={"searched": [str(p) for p in log_paths if p]},
            )

        log_lines = log_content.split("\n")
        if errors_only:
            log_lines = [l for l in log_lines if "ERROR" in l or "CRITICAL" in l]

        recent = log_lines[-lines:]
        # Phrase ``lines`` as the tail REQUEST, not a fabricated
        # count. The actual count returned IS surfaced in data.
        return ToolResult.ok(
            confirmation=(
                f"Retrieved logs (tail: {lines}, errors_only={errors_only})"
            ),
            data={
                "log_file": used_path,
                "lines_returned": len(recent),
                "lines_requested": lines,
                "errors_only": errors_only,
                "content": "\n".join(recent),
            },
        )

    @tool(
        name="code_rollback",
        description="Rollback to a previous commit. Requires approval.",
        category=ToolCategory.SYSTEM,
        command_prefix="!code-rollback",
    )
    async def code_rollback(
        self,
        commit: str = "HEAD~1",
        hard: bool = False,
    ) -> ToolResult:
        # Reject ``commit`` values that look like git options (codex
        # round-2 finding #1). Without this guard,
        # ``commit="--hard"`` becomes ``git reset --hard``, which
        # discards the working tree even though the approval payload
        # said ``"hard": false``. The confirmation would also lie:
        # "Rolled back to --hard".
        if not isinstance(commit, str) or commit.startswith("-"):
            return ToolResult.failed(
                f"Invalid commit ref '{commit}': must not start with '-' "
                f"(possible git-option injection)",
                data={"commit": commit, "hard": hard},
            )

        # Validate ``hard`` is an actual bool (codex round-3 finding
        # #1). A string like ``"false"`` is truthy in Python, so
        # without this check ``hard="false"`` would run ``git reset
        # --hard`` even though the approval payload preserved the
        # string verbatim. This is a confirmation/approval mismatch
        # AND a destructive security issue.
        if not isinstance(hard, bool):
            return ToolResult.failed(
                f"Invalid hard '{hard}': must be a real bool, not "
                f"{type(hard).__name__}",
                data={"commit": commit, "hard": hard, "hard_type": type(hard).__name__},
            )

        try:
            approved = await self._request_approval(
                "code_rollback",
                {
                    "commit": commit,
                    "hard": hard,
                    "warning": "This will modify git history",
                },
            )
        except Exception as e:
            return ToolResult.failed(
                f"Approval check failed: {e}",
                data={"requires_approval": True},
            )
        if not approved:
            return ToolResult.failed(
                "Rollback not approved",
                data={"requires_approval": True, "commit": commit, "hard": hard},
            )

        cmd = [GIT_PATH, "reset"]
        if hard:
            cmd.append("--hard")
        # The leading-hyphen check above is sufficient for option
        # injection; we can't use ``--`` here because in ``git
        # reset`` it separates refs from PATHS, not options. With
        # ``--`` present, ``<commit>`` would be treated as a path
        # and the reset would silently target HEAD with that path
        # filter.
        cmd.append(commit)

        try:
            result = await _run_subprocess(
                cmd,
                cwd=self.code_root,
                capture_output=True,
                text=True,
                timeout=GIT_OPERATION_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failed(
                "Git rollback operation timed out",
                data={"commit": commit, "hard": hard},
            )
        except Exception as e:
            return ToolResult.failed(
                str(e), data={"commit": commit, "hard": hard}
            )

        if result.returncode != 0:
            return ToolResult.failed(
                result.stderr or "git reset failed",
                data={
                    "commit": commit,
                    "hard": hard,
                    "returncode": result.returncode,
                },
            )

        logger.info(f"Rolled back to {commit}")
        # Distinguish hard from soft because hard discards working-
        # tree changes — narrating "rolled back" without that
        # distinction is misleading.
        confirmation = (
            f"Hard-reset to {commit} (working tree changes discarded)"
            if hard
            else f"Rolled back to {commit}"
        )
        return ToolResult.ok(
            confirmation=confirmation,
            data={
                "commit": commit,
                "hard": hard,
                "output": result.stdout,
            },
        )


# Backwards-compat alias for the v0.1.0 class name. Removed in v0.3.0.
# Importing ``CodeEditFeature`` from this module emits a
# DeprecationWarning so external callers learn to migrate before the
# v0.3.0 cutover.
def __getattr__(name: str):
    if name == "CodeEditFeature":
        import warnings
        warnings.warn(
            "CodeEditFeature is a deprecated alias for CodeFeature; "
            "the alias will be removed in v0.3.0. Update imports to "
            "``from kestrel_feature_code.feature import CodeFeature``.",
            DeprecationWarning,
            stacklevel=2,
        )
        return CodeFeature
    raise AttributeError(
        f"module 'kestrel_feature_code.feature' has no attribute {name!r}"
    )
