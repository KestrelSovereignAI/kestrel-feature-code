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

from kestrel_feature_code.feature import CodeFeature, CodeEditFeature, _run_subprocess
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
    cut over without thrashing. Pin the alias so a future cleanup
    doesn't break it silently."""
    assert CodeEditFeature is CodeFeature


def test_resolve_path_rejects_escape(feature):
    feat, _ = feature
    with pytest.raises(ValueError, match="escapes code root"):
        feat._resolve_path("../outside.py")


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
    assert result.data["content"] == "line1\nline2\n"
    assert result.data["total_lines"] == 3


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
    assert result.data["matches"][0]["file"] == "a.py"


@pytest.mark.asyncio
async def test_code_search_truncates_and_reports_truth(feature):
    """#1042 honesty: when matches exceed the 50-result display cap,
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
async def test_code_commit_nothing_to_commit_returns_ok_no_op(feature):
    """#1042 honesty: 'nothing to commit' is a no-op, NOT a success
    that pretends a commit happened. Confirmation says no-op."""
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
async def test_code_test_passed_vs_failed_in_confirmation(feature):
    """#1042 honesty: confirmation must distinguish pass from fail.
    Saying 'Tests run' on a failure is a confident lie."""
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
    assert result.status is ToolResultStatus.OK
    assert "FAILED" in result.confirmation
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
    """#1042 honesty: hard reset discards working-tree changes.
    Confirmation must distinguish hard from soft so the LLM can't
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
    """Verify _run_subprocess uses asyncio.to_thread, not direct subprocess.run."""
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
async def test_no_tool_method_raises_for_typical_failure_paths(feature):
    """#1042 envelope contract: every @tool method must return
    ToolResult, NEVER raise. Pin the contract for the common
    failure paths so a regression can't silently slip back to dict
    returns or raised exceptions."""
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
