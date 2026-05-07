"""Direct contracts for the CodeFeature surface (formerly CodeEditFeature).

All @tool methods return ``kestrel_sdk.tools.result.ToolResult``;
these tests pin the success/failure shapes so the framework's
narration-honesty audit hook (kestrel-sovereign #1042 layer 3) can
trust the wire format.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kestrel_feature_code.feature import CodeFeature, _run_subprocess
from kestrel_sdk.tools.result import ToolResult, ToolResultStatus


@pytest.fixture
def feature(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    agent = SimpleNamespace(features={})
    feat = CodeFeature(agent=agent, code_root=str(root))
    return feat, root


def test_backward_compat_alias_resolves_to_codefeature():
    """v0.2.0 rename: CodeEditFeature is kept as an alias for one
    release so external entry-point references and import sites can
    cut over without thrashing. Importing it MUST resolve to
    CodeFeature AND emit a DeprecationWarning."""
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from kestrel_feature_code.feature import CodeEditFeature as _alias
    assert _alias is CodeFeature
    assert any(
        issubclass(w.category, DeprecationWarning) for w in caught
    ), "CodeEditFeature import via feature module must emit DeprecationWarning"

    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always")
        from kestrel_feature_code import CodeEditFeature as _alias2
    assert _alias2 is CodeFeature
    assert any(
        issubclass(w.category, DeprecationWarning) for w in caught2
    ), "CodeEditFeature import via package root must emit DeprecationWarning"


def test_codeedit_feature_attribute_access_path_emits_warning():
    """``import kestrel_feature_code; kestrel_feature_code.feature.
    CodeEditFeature`` form goes through ``feature.__getattr__``. Pin
    explicit coverage so a regression in the submodule alias is
    caught (the from-import paths go through the package
    ``__init__.__getattr__`` instead)."""
    import warnings
    import kestrel_feature_code

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cls = kestrel_feature_code.feature.CodeEditFeature
    assert cls is CodeFeature
    assert any(
        issubclass(w.category, DeprecationWarning) for w in caught
    )


def test_resolve_path_rejects_escape(feature):
    feat, _ = feature
    with pytest.raises(ValueError, match="escapes code root"):
        feat._resolve_path("../outside.py")


def test_resolve_path_rejects_sibling_prefix_escape(tmp_path):
    """Codex round-1 finding #1: ``str.startswith`` lets sibling-
    prefix paths escape — code root ``/x/repo`` would match
    ``/x/repo2/secret.py``. The fix uses ``Path.is_relative_to`` for
    proper path-component containment."""
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo2"
    sibling.mkdir()
    (sibling / "secret.py").write_text("secret\n", encoding="utf-8")

    feat = CodeFeature(agent=SimpleNamespace(features={}), code_root=str(root))
    with pytest.raises(ValueError, match="escapes code root"):
        feat._resolve_path("../repo2/secret.py")


@pytest.mark.asyncio
async def test_code_read_returns_tool_result_ok_with_file_contents(feature):
    feat, root = feature
    target = root / "sample.py"
    target.write_text("line1\nline2\n", encoding="utf-8")

    result = await feat.code_read("sample.py")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "Read" in result.confirmation
    assert "sample.py" in result.confirmation
    assert result.data["path"] == "sample.py"
    # Content is preserved byte-for-byte when no line range is
    # requested (so callers writing back get the same trailing
    # newline they read). The COUNT is what splitlines fixes.
    assert result.data["content"] == "line1\nline2\n"
    # ``splitlines()`` correctly returns 2 for "line1\nline2\n"; the
    # legacy split("\n") returned 3 because of the phantom empty
    # element from the trailing newline.
    assert result.data["total_lines"] == 2
    assert result.data["shown_lines"] == 2


@pytest.mark.asyncio
async def test_code_read_path_escape_returns_failed(feature):
    feat, _ = feature
    result = await feat.code_read("../escaped.py")
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "escapes code root" in result.error


@pytest.mark.asyncio
async def test_code_read_missing_file_returns_failed(feature):
    feat, _ = feature
    result = await feat.code_read("does_not_exist.py")
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_code_search_returns_tool_result_with_total_and_truncation(feature):
    feat, root = feature
    (root / "a.py").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    (root / "b.py").write_text("gamma\n", encoding="utf-8")

    result = await feat.code_search("alpha")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["total_matches"] == 2
    assert result.data["truncated"] is False


@pytest.mark.asyncio
async def test_code_search_truncates_and_reports_truth(feature):
    """#1042 honesty: when matches exceed the 50-result cap,
    confirmation must say 'showing first 50' and total_matches must
    be the real count."""
    feat, root = feature
    big = root / "big.py"
    big.write_text("\n".join(["match"] * 75) + "\n", encoding="utf-8")

    result = await feat.code_search("match")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert result.data["total_matches"] == 75
    assert len(result.data["matches"]) == 50
    assert result.data["truncated"] is True
    assert "showing first 50" in result.confirmation


@pytest.mark.asyncio
async def test_code_edit_requires_unique_match(feature):
    feat, root = feature
    target = root / "sample.py"
    target.write_text("dup\ndup\n", encoding="utf-8")

    result = await feat.code_edit("sample.py", "dup", "new")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "must be unique" in result.error
    assert result.data["occurrences"] == 2


@pytest.mark.asyncio
async def test_code_edit_old_text_not_found_returns_failed(feature):
    feat, root = feature
    target = root / "sample.py"
    target.write_text("hello\n", encoding="utf-8")
    result = await feat.code_edit("sample.py", "missing", "new")
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "not found in file" in result.error


@pytest.mark.asyncio
async def test_code_edit_applies_change_after_approval(feature):
    feat, root = feature
    target = root / "sample.py"
    target.write_text("old\n", encoding="utf-8")
    feat._request_approval = AsyncMock(return_value=True)

    result = await feat.code_edit(
        "sample.py", "old", "new", description="replace text"
    )

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "Edited" in result.confirmation
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result.data["description"] == "replace text"


@pytest.mark.asyncio
async def test_code_edit_unapproved_returns_failed_with_requires_approval(feature):
    feat, root = feature
    target = root / "sample.py"
    target.write_text("old\n", encoding="utf-8")
    feat._request_approval = AsyncMock(return_value=False)

    result = await feat.code_edit("sample.py", "old", "new")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "not approved" in result.error.lower()
    assert result.data["requires_approval"] is True
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_code_commit_invalid_path_rejected_before_approval_consumed(feature):
    """Validation must happen BEFORE approval is consumed. Otherwise
    the user approves an operation we already knew was structurally
    invalid."""
    feat, _ = feature
    approval_calls = []

    async def _spy_approve(*args, **kwargs):
        approval_calls.append((args, kwargs))
        return True
    feat._request_approval = _spy_approve

    result = await feat.code_commit(message="x", files="../escaped")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "escapes code root" in result.error
    assert approval_calls == []


@pytest.mark.asyncio
async def test_code_commit_nothing_to_commit_returns_ok_no_op(feature):
    """#1042 honesty: 'nothing to commit' is a no-op, NOT a success
    that pretends a commit happened."""
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=True)

    async def _fake_subprocess(*args, **kwargs):
        cmd = args[0]
        import subprocess as _sp
        if cmd[1] == "add":
            return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "commit":
            return _sp.CompletedProcess(
                cmd, returncode=1,
                stdout="nothing to commit, working tree clean",
                stderr="",
            )
        return _sp.CompletedProcess(cmd, returncode=0, stdout="abc12345", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _fake_subprocess):
        result = await feat.code_commit(message="test")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "no-op" in result.confirmation.lower()
    assert result.data["committed"] is False


@pytest.mark.asyncio
async def test_code_commit_unapproved_does_not_invoke_git(feature):
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=False)

    invoked = []

    async def _spy(*args, **kwargs):
        invoked.append(args[0])
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _spy):
        result = await feat.code_commit(message="test")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "not approved" in result.error.lower()
    assert invoked == []


@pytest.mark.asyncio
async def test_commit_rev_parse_nonzero_returncode_returns_partial(feature):
    """``git rev-parse HEAD`` can exit non-zero without raising
    (corrupted repo, detached refs). Without the explicit returncode
    check the result was ``ToolResult.ok`` with empty commit_hash and
    the broken confirmation ``Committed : message``."""
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=True)

    async def _fake_subprocess(*args, **kwargs):
        cmd = args[0]
        import subprocess as _sp
        if cmd[1] == "add":
            return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "commit":
            return _sp.CompletedProcess(
                cmd, returncode=0, stdout="committed", stderr=""
            )
        if cmd[1] == "rev-parse":
            return _sp.CompletedProcess(
                cmd, returncode=128,
                stdout="",
                stderr="fatal: bad object HEAD",
            )
        return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _fake_subprocess):
        result = await feat.code_commit(message="msg")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert "commit hash unavailable" in result.confirmation.lower()
    assert "non-zero" in result.error.lower()
    assert result.data["committed"] is True
    assert result.data["rev_parse_returncode"] == 128


@pytest.mark.asyncio
async def test_code_diff_no_changes_returns_honest_confirmation(feature):
    feat, _ = feature

    async def _fake(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _fake):
        result = await feat.code_diff()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "no uncommitted" in result.confirmation.lower()
    assert result.data["has_changes"] is False


@pytest.mark.asyncio
async def test_code_test_passed_vs_failed_in_status_and_confirmation(feature):
    """#1042 honesty: failing test runs return PARTIAL (not OK) so
    audit hooks reading ``result.status`` see non-OK on failure."""
    feat, _ = feature

    async def _passing(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="ok", stderr="")

    async def _failing(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=1, stdout="FAILED", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _passing):
        result = await feat.code_test(path=".")
    assert result.status is ToolResultStatus.OK
    assert "passed" in result.confirmation.lower()
    assert result.data["passed"] is True

    with patch("kestrel_feature_code.feature._run_subprocess", _failing):
        result = await feat.code_test(path=".")
    assert result.status is ToolResultStatus.PARTIAL
    assert "FAILED" in result.confirmation
    assert "non-zero" in result.error.lower()
    assert result.data["passed"] is False


@pytest.mark.asyncio
async def test_code_lint_clean_vs_issues_in_confirmation(feature):
    feat, _ = feature

    async def _clean(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    async def _issues(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(
            args[0], returncode=1,
            stdout="file.py:1:1: E501 line too long\nfile.py:2:1: F401 unused import\n",
            stderr="",
        )

    with patch("kestrel_feature_code.feature._run_subprocess", _clean):
        result = await feat.code_lint()
    assert result.status is ToolResultStatus.OK
    assert "clean" in result.confirmation.lower()
    assert result.data["has_issues"] is False
    assert result.data["issue_count"] == 0

    with patch("kestrel_feature_code.feature._run_subprocess", _issues):
        result = await feat.code_lint()
    assert result.status is ToolResultStatus.OK
    assert result.data["has_issues"] is True
    assert result.data["issue_count"] == 2


@pytest.mark.asyncio
async def test_code_lint_invocation_error_returns_failed_not_ok(feature):
    """Codex round-1 finding #2: ruff exiting non-zero with stderr
    is an INVOCATION error (missing config, bad args, ruff not
    installed), not a lint finding. Previous code returned
    ToolResult.ok with ``Lint found 0 issue line(s)`` — a false
    confirmation."""
    feat, _ = feature

    async def _ruff_missing(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(
            args[0], returncode=2, stdout="",
            stderr="error: invalid configuration",
        )

    with patch("kestrel_feature_code.feature._run_subprocess", _ruff_missing):
        result = await feat.code_lint()

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR, (
        "ruff invocation errors must NOT be reported as 0-issue OK"
    )
    assert "invocation failed" in result.error.lower()
    assert result.data["returncode"] == 2


@pytest.mark.asyncio
async def test_code_lint_legitimate_findings_with_no_stdout_still_ok(feature):
    """Edge: rc=1 (lint findings) with no stderr and no stdout is
    weird but not necessarily an invocation error — rc=1 is the
    'findings exist' signal, so treat it as OK with 0 issues. The
    invocation-error gate only fires on rc>=2 OR rc!=0+stderr."""
    feat, _ = feature

    async def _rc1_empty(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=1, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _rc1_empty):
        result = await feat.code_lint()

    assert result.status is ToolResultStatus.OK
    assert result.data["has_issues"] is True
    assert result.data["issue_count"] == 0


@pytest.mark.asyncio
async def test_code_logs_default_path_does_not_search_outside_root(tmp_path):
    """Codex round-1 finding #3: the default fallback used to
    include ``/tmp/kestrel-claw.log``, which violated the sandbox.
    With no in-root logs, the result must be ``ToolResult.failed``
    rather than reading a /tmp file."""
    root = tmp_path / "repo"
    root.mkdir()
    # Plant a /tmp file that the previous default would have read.
    tmp_log = Path("/tmp/kestrel-claw.log")
    if tmp_log.exists():
        # Don't trample an existing file; skip if present.
        pytest.skip("/tmp/kestrel-claw.log already exists on this host")
    feat = CodeFeature(agent=SimpleNamespace(features={}), code_root=str(root))

    result = await feat.code_logs(lines=10)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "No log file found" in result.error
    # Searched paths must NOT include /tmp.
    assert not any(
        p.startswith("/tmp/")
        for p in result.data["searched"]
    ), (
        f"default log search reached outside code_root: "
        f"{result.data['searched']}"
    )


@pytest.mark.asyncio
async def test_code_logs_user_path_relative_escape_is_sandboxed(feature):
    """``log_file=`` is user input and MUST go through _resolve_path.
    The relative escape ``../../../etc/passwd`` would otherwise read
    outside the code root."""
    feat, _ = feature
    result = await feat.code_logs(log_file="../../../etc/passwd")
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "escapes code root" in result.error


@pytest.mark.asyncio
async def test_code_logs_user_path_inside_root_works(feature):
    feat, root = feature
    log = root / "custom.log"
    log.write_text("entry 1\n", encoding="utf-8")

    result = await feat.code_logs(log_file="custom.log")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "entry 1" in result.data["content"]


@pytest.mark.asyncio
async def test_code_logs_tail_phrasing_does_not_overstate_count(feature):
    """#1042 honesty: confirmation phrases ``lines`` as the tail
    REQUEST, not the count actually returned."""
    feat, root = feature
    log = root / "kestrel.log"
    log.write_text("line A\nline B\n", encoding="utf-8")

    result = await feat.code_logs(lines=100)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.OK
    assert "tail" in result.confirmation.lower()
    assert "100" in result.confirmation
    assert result.data["lines_requested"] == 100
    assert result.data["lines_returned"] <= 3


@pytest.mark.asyncio
async def test_code_rollback_hard_vs_soft_in_confirmation(feature):
    """#1042 honesty: hard reset discards working-tree changes. The
    confirmation must distinguish hard from soft so the LLM can't
    narrate a hard reset as 'rolled back'."""
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=True)

    async def _ok(*args, **kwargs):
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _ok):
        soft_result = await feat.code_rollback(commit="HEAD~1", hard=False)
        hard_result = await feat.code_rollback(commit="HEAD~1", hard=True)

    assert soft_result.status is ToolResultStatus.OK
    assert "rolled back" in soft_result.confirmation.lower()
    assert "discarded" not in soft_result.confirmation.lower()

    assert hard_result.status is ToolResultStatus.OK
    assert "hard-reset" in hard_result.confirmation.lower()
    assert "discarded" in hard_result.confirmation.lower()


@pytest.mark.asyncio
async def test_subprocess_calls_are_offloaded_via_to_thread():
    """Verify _run_subprocess uses asyncio.to_thread, not direct
    subprocess.run."""
    import subprocess

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout="ok", stderr=""
        )
        result = await _run_subprocess(
            ["echo", "test"], capture_output=True, text=True
        )
        mock_thread.assert_awaited_once()
        assert result.returncode == 0


@pytest.mark.asyncio
async def test_code_rollback_rejects_option_injection(feature):
    """Codex round-2 finding #1: ``commit="--hard"`` + ``hard=False``
    must NOT silently become ``git reset --hard`` (which discards
    the working tree). The leading-hyphen guard rejects this
    before invoking git."""
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=True)

    invoked = []

    async def _spy(*args, **kwargs):
        invoked.append(args[0])
        import subprocess as _sp
        return _sp.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    with patch("kestrel_feature_code.feature._run_subprocess", _spy):
        result = await feat.code_rollback(commit="--hard", hard=False)

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "git-option injection" in result.error.lower()
    # CRITICAL: git was NEVER invoked, so no working-tree damage.
    assert invoked == []


@pytest.mark.asyncio
async def test_code_rollback_rejects_other_option_like_commits(feature):
    feat, _ = feature
    feat._request_approval = AsyncMock(return_value=True)
    for bad in ["-f", "--force", "-x", "--recurse-submodules"]:
        result = await feat.code_rollback(commit=bad, hard=False)
        assert isinstance(result, ToolResult), bad
        assert result.status is ToolResultStatus.ERROR, bad
        assert "must not start with '-'" in result.error or "injection" in result.error.lower()


@pytest.mark.asyncio
async def test_code_search_skipped_files_returns_partial(tmp_path):
    """Codex round-2 finding #2: silently skipping unreadable files
    while still returning OK is partial-success masquerading as
    complete. Now downgrades to PARTIAL with skipped_files in data."""
    root = tmp_path / "repo"
    root.mkdir()
    # A readable file with a match.
    (root / "good.py").write_text("alpha\n", encoding="utf-8")
    # A binary-content file the .py glob will pick up but
    # read_text(utf-8) will reject.
    bad = root / "bad.py"
    bad.write_bytes(b"\xc3\x28invalid utf8\n")

    feat = CodeFeature(agent=SimpleNamespace(features={}), code_root=str(root))

    result = await feat.code_search("alpha")

    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.PARTIAL
    assert result.data["skipped_count"] == 1
    assert any(
        "bad.py" in s["file"] for s in result.data["skipped_files"]
    )
    assert result.data["total_matches"] == 1


@pytest.mark.asyncio
async def test_code_read_none_path_returns_failed_not_attribute_error(feature):
    """Codex round-2 finding #3: tool args can be malformed (None,
    non-str). _resolve_path now rejects them as ValueError so the
    @tool envelope catches them, instead of AttributeError-ing out
    of ``path.startswith``."""
    feat, _ = feature
    result = await feat.code_read(path=None)
    assert isinstance(result, ToolResult)
    assert result.status is ToolResultStatus.ERROR
    assert "must be a string" in result.error.lower()


@pytest.mark.asyncio
async def test_no_tool_method_raises_for_typical_failure_paths(feature):
    """#1042 envelope contract: every @tool method must return
    ToolResult, NEVER raise. Pin the contract for the common
    failure paths."""
    feat, _ = feature

    cases = [
        ("code_read", {"path": "no/such/file.py"}),
        ("code_search", {"pattern": "x", "path": "nonexistent_dir"}),
        ("code_edit", {"path": "no/such/file.py", "old_text": "a", "new_text": "b"}),
    ]
    for method, kwargs in cases:
        result = await getattr(feat, method)(**kwargs)
        assert isinstance(result, ToolResult), method
        assert result.status is ToolResultStatus.ERROR, method
