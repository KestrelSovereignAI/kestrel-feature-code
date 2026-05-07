"""
Code Feature - Source-code reading + modification capabilities for Kestrel agents.

This feature exposes a code-edit / code-read tool surface with constitutional
controls (approval queue) for destructive operations. Returns
``kestrel_sdk.tools.result.ToolResult`` from every @tool — see #1042 layer 4b
for the honesty contract.
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

GIT_PATH = shutil.which("git") or "/usr/bin/git"
PYTHON_PATH = shutil.which("python3") or shutil.which("python") or "/usr/bin/python3"

DEFAULT_CODE_ROOT = os.environ.get(
    "KESTREL_CODE_ROOT",
    str(Path(__file__).parent.parent),
)

CODE_REVIEW_TIMEOUT = 300
TEST_SUITE_TIMEOUT = 300
LINT_TIMEOUT = 60
GIT_OPERATION_TIMEOUT = 30
GIT_QUICK_TIMEOUT = 10


async def _run_subprocess(*args, **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess.run off the event loop via asyncio.to_thread."""
    return await asyncio.to_thread(functools.partial(subprocess.run, *args, **kwargs))


class CodeFeature(Feature):
    """Feature for reading and modifying source code (with approval).

    Renamed from ``CodeEditFeature`` in v0.2.0 — the surface is broader
    than just edits (read, search, diff, lint, test, logs, commit,
    rollback, restart). Edit-and-write paths require user approval via
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

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to code root, with security checks.

        Raises ``ValueError`` only on the path-escape security check;
        every @tool method that calls this MUST catch ValueError and
        convert to ``ToolResult.failed`` so the security violation
        lands in the envelope, not as a raised exception.
        """
        if path.startswith("/"):
            path = path.lstrip("/")
        resolved = (self.code_root / path).resolve()
        if not str(resolved).startswith(str(self.code_root)):
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
        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        if not resolved.exists():
            return ToolResult.failed(f"File not found: {path}", data={"path": path})
        if not resolved.is_file():
            return ToolResult.failed(f"Not a file: {path}", data={"path": path})
        try:
            content = resolved.read_text()
        except Exception as e:
            return ToolResult.failed(str(e), data={"path": path})

        all_lines = content.split("\n")
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

        matches: List[Dict[str, Any]] = []
        try:
            for file_path in resolved.rglob(file_pattern):
                if not file_path.is_file():
                    continue
                try:
                    content = file_path.read_text()
                except (UnicodeDecodeError, PermissionError, OSError):
                    continue
                for i, line in enumerate(content.split("\n"), 1):
                    if pattern in line:
                        matches.append({
                            "file": str(file_path.relative_to(self.code_root)),
                            "line": i,
                            "content": line.strip()[:200],
                        })
        except Exception as e:
            return ToolResult.failed(str(e), data={"pattern": pattern, "path": path})

        # ``matches[:50]`` is a display cap — surface BOTH the truncated
        # list AND total_matches so the LLM doesn't narrate "found 50
        # matches" when there are 200 (#1042 honesty contract).
        truncated = matches[:50]
        total = len(matches)
        confirmation = (
            f"Found {total} match(es) for '{pattern}'"
            if total <= 50
            else f"Found {total} match(es) for '{pattern}' (showing first 50)"
        )
        return ToolResult.ok(
            confirmation=confirmation,
            data={
                "pattern": pattern,
                "matches": truncated,
                "total_matches": total,
                "truncated": total > 50,
            },
        )

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
        try:
            resolved = self._resolve_path(path)
        except ValueError as e:
            return ToolResult.failed(str(e), data={"path": path})
        if not resolved.exists():
            return ToolResult.failed(f"File not found: {path}", data={"path": path})

        try:
            content = resolved.read_text()
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
            resolved.write_text(new_content)
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
        # Honest confirmation: "no changes" vs "changes present"
        # (#1042). Don't say "Showing diff" when there's nothing.
        confirmation = (
            "No uncommitted changes" if not has_changes
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
        # Validate the path BEFORE asking for approval (claude
        # review finding #2). If the user approves only to find
        # out the operation was structurally invalid, we've burned
        # their attention on something we knew couldn't proceed.
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

        # Stage files. Use capture_output so a failed `git add` lands
        # in the envelope instead of raising CalledProcessError out of
        # subprocess(check=True). This was a #1042 escape vector
        # because the previous code used check=True without capture.
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
            # ``git commit`` returns non-zero when there's nothing to
            # commit. That's a no-op, NOT an error — but the previous
            # code returned ``{success: True, message: "Nothing to
            # commit"}`` which lied via the success flag. Return OK
            # with an honest no-op confirmation instead.
            stdout_text = result.stdout or ""
            stderr_text = result.stderr or ""
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
                confirmation=f"Committed (commit hash unavailable)",
                error="git rev-parse HEAD timed out after the commit succeeded",
                data={"committed": True, "message": message},
            )
        except Exception as e:
            return ToolResult.partial(
                confirmation=f"Committed (commit hash unavailable)",
                error=f"git rev-parse failed after commit: {e}",
                data={"committed": True, "message": message},
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
            restart_file.write_text(reason or "Code changes applied")
        except Exception as e:
            # Approval granted, but we couldn't write the signal file.
            # Partial: we set _pending_restart in memory but the
            # external signal isn't there.
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
        """Check if a restart has been requested."""
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
        # Status-level honesty (claude review finding #5): the tool
        # ran correctly either way, but a failing test run is a
        # PARTIAL result. Without this, audit hooks / narration
        # guards reading ``result.status`` would see OK on every
        # invocation and conclude "tests succeeded" regardless of
        # outcome.
        data = {
            "passed": passed,
            "return_code": result.returncode,
            "output": out[-2000:] if len(out) > 2000 else out,
            "errors": err[-1000:] if err else None,
        }
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

        has_issues = result.returncode != 0
        # Compute issue count from REAL stdout, not the display
        # fallback (claude review finding #3). Without this, ruff
        # exiting non-zero with empty stdout would count the
        # placeholder string ``(no issues)`` as 1 issue line.
        raw_stdout = result.stdout or ""
        issue_lines = [l for l in raw_stdout.splitlines() if l.strip()]
        issue_count = len(issue_lines) if has_issues else 0
        # Display-time fallback (separate from the count above).
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
                "issue_count": issue_count,
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
        # User-supplied ``log_file`` MUST go through _resolve_path
        # (claude review finding #1). Without this, log_file=
        # "/etc/passwd" or "../../../sensitive.txt" reads outside
        # the code root. ``_resolve_path`` enforces the same
        # sandbox other tools use.
        user_path: Optional[Path] = None
        if log_file is not None:
            try:
                user_path = self._resolve_path(log_file)
            except ValueError as e:
                return ToolResult.failed(str(e), data={"log_file": log_file})

        # The two well-known fallbacks (``/tmp/kestrel-claw.log``
        # and ``code_root/...``) are operator-controlled, NOT user
        # input — those don't need _resolve_path. The user-supplied
        # path comes first so an explicit log_file= overrides
        # auto-detect.
        log_paths: List[Optional[Path]] = [
            user_path,
            Path("/tmp/kestrel-claw.log"),
            self.code_root / "logs" / "kestrel.log",
            self.code_root / "kestrel.log",
        ]

        log_content: Optional[str] = None
        used_path: Optional[str] = None
        try:
            for p in log_paths:
                if p is None:
                    continue
                if p.exists():
                    log_content = p.read_text()
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
        # Tail honesty: report the tail REQUEST, not a fabricated
        # count (#1042). The actual count returned IS surfaced in
        # data so the user can see both.
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
        # Honest confirmation: distinguish hard from soft because
        # hard discards working-tree changes — narrating "rolled
        # back" without that distinction is misleading.
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


# Backwards-compat alias for the v0.1.0 class name. Removed in
# v0.3.0. Importing ``CodeEditFeature`` from this module still
# resolves to ``CodeFeature``, but emits a DeprecationWarning so
# external callers learn to migrate before the v0.3.0 cutover.
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
