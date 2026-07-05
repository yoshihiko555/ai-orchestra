"""harness_common.py のテスト。

テスト対象:
- verify_hooks_trust(): 一致/改変/symlink/台帳なしの各ケース
- redact_secrets(): 各秘密パターンの置換
- write_atomic(): 一時ファイル経由の原子的書き込み
- check_codex_version(): subprocess をモックしたバージョン判定
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.module_loader import load_module

harness_common = load_module(
    "harness_common",
    "packages/codex-harness/scripts/harness_common.py",
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_ledger_project(tmp_path: Path, hook_json_content: str) -> Path:
    """codex_file_hashes 台帳と一致する .codex/hooks.json を持つプロジェクトを作る。"""
    project_dir = tmp_path / "project"
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True)
    codex_dir = project_dir / ".codex"
    codex_dir.mkdir(parents=True)

    hooks_json_path = codex_dir / "hooks.json"
    hooks_json_path.write_text(hook_json_content, encoding="utf-8")

    orch = {
        "installed_packages": ["codex-harness"],
        "codex_file_hashes": {
            ".codex/hooks.json": _sha256(hook_json_content),
        },
    }
    (claude_dir / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")
    return project_dir


class TestVerifyHooksTrust:
    def test_trusted_when_hash_matches(self, tmp_path: Path) -> None:
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is True
        assert result.reasons == []

    def test_untrusted_when_file_modified(self, tmp_path: Path) -> None:
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')
        (project_dir / ".codex" / "hooks.json").write_text(
            '{"hooks": ["tampered"]}', encoding="utf-8"
        )

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is False
        assert any("hash mismatch" in reason for reason in result.reasons)

    def test_untrusted_when_no_orchestra_json(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is False
        assert "orchestra.json" in result.reasons[0]

    def test_untrusted_when_ledger_has_no_hook_entries(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        orch = {"codex_file_hashes": {".codex/schemas/task_result.schema.json": "deadbeef"}}
        (claude_dir / "orchestra.json").write_text(json.dumps(orch), encoding="utf-8")

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is False
        assert "no hook entries" in result.reasons[0]

    def test_untrusted_when_file_missing(self, tmp_path: Path) -> None:
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')
        (project_dir / ".codex" / "hooks.json").unlink()

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is False
        assert any("missing" in reason for reason in result.reasons)

    def test_untrusted_when_target_is_symlink(self, tmp_path: Path) -> None:
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')
        hooks_path = project_dir / ".codex" / "hooks.json"
        real_target = tmp_path / "outside.json"
        real_target.write_text('{"hooks": []}', encoding="utf-8")
        hooks_path.unlink()
        hooks_path.symlink_to(real_target)

        result = harness_common.verify_hooks_trust(project_dir)

        assert result.trusted is False
        assert any("symlink" in reason for reason in result.reasons)

    def test_rules_file_is_ledger_tracked(self, tmp_path: Path) -> None:
        """.codex/rules/*.rules must also be verified against the ledger (H4)."""
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')
        rules_dir = project_dir / ".codex" / "rules"
        rules_dir.mkdir(parents=True)
        rules_content = 'prefix_rule(pattern=["git", "push"], decision="forbidden")\n'
        (rules_dir / "codex-harness.rules").write_text(rules_content, encoding="utf-8")

        orch_path = project_dir / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["codex_file_hashes"][".codex/rules/codex-harness.rules"] = _sha256(rules_content)
        orch_path.write_text(json.dumps(orch), encoding="utf-8")

        result = harness_common.verify_hooks_trust(project_dir)
        assert result.trusted is True

        (rules_dir / "codex-harness.rules").write_text("tampered", encoding="utf-8")
        tampered_result = harness_common.verify_hooks_trust(project_dir)
        assert tampered_result.trusted is False
        assert any("hash mismatch" in reason for reason in tampered_result.reasons)

    def test_validation_json_is_ledger_tracked(self, tmp_path: Path) -> None:
        """.codex/validation.json must also be verified against the ledger (H4)."""
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')
        validation_content = '{"commands": []}'
        (project_dir / ".codex" / "validation.json").write_text(
            validation_content, encoding="utf-8"
        )

        orch_path = project_dir / ".claude" / "orchestra.json"
        orch = json.loads(orch_path.read_text(encoding="utf-8"))
        orch["codex_file_hashes"][".codex/validation.json"] = _sha256(validation_content)
        orch_path.write_text(json.dumps(orch), encoding="utf-8")

        result = harness_common.verify_hooks_trust(project_dir)
        assert result.trusted is True

        (project_dir / ".codex" / "validation.json").write_text("tampered", encoding="utf-8")
        tampered_result = harness_common.verify_hooks_trust(project_dir)
        assert tampered_result.trusted is False
        assert any("hash mismatch" in reason for reason in tampered_result.reasons)


class TestResolveTrustFlags:
    def test_returns_bypass_flag_when_trusted(self, tmp_path: Path) -> None:
        project_dir = _write_ledger_project(tmp_path, '{"hooks": []}')

        flags = harness_common.resolve_trust_flags(project_dir, allow_untrusted=False, label="t")

        assert flags == ["--dangerously-bypass-hook-trust"]

    def test_returns_none_when_untrusted_and_not_allowed(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        flags = harness_common.resolve_trust_flags(project_dir, allow_untrusted=False, label="t")

        assert flags is None

    def test_returns_empty_list_when_untrusted_but_allowed(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        flags = harness_common.resolve_trust_flags(project_dir, allow_untrusted=True, label="t")

        assert flags == []


class TestRedactSecrets:
    def test_redacts_github_pat(self) -> None:
        text = f"token=ghp_{'a' * 36}"
        result = harness_common.redact_secrets(text)
        assert "a" * 36 not in result
        assert "[REDACTED:GitHub PAT (ghp_)]" in result

    def test_redacts_openai_style_key(self) -> None:
        text = f"key=sk-{'x' * 20}"
        result = harness_common.redact_secrets(text)
        assert "[REDACTED:API key (sk- prefix)]" in result

    def test_redacts_aws_access_key(self) -> None:
        text = "AKIAABCDEFGHIJKLMNOP"
        result = harness_common.redact_secrets(text)
        assert "AKIA" not in result

    def test_redacts_pem_block(self) -> None:
        text = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
        result = harness_common.redact_secrets(text)
        assert "abc123" not in result
        assert "[REDACTED:PEM private key block]" in result

    def test_leaves_plain_text_untouched(self) -> None:
        text = "This is a normal summary with no secrets."
        assert harness_common.redact_secrets(text) == text


class TestWriteAtomic:
    def test_writes_content_and_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "out.txt"
        harness_common.write_atomic(target, "hello\n")

        assert target.read_text(encoding="utf-8") == "hello\n"
        leftovers = list(target.parent.glob(".*.tmp-*"))
        assert leftovers == []

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        target.write_text("old", encoding="utf-8")

        harness_common.write_atomic(target, "new")

        assert target.read_text(encoding="utf-8") == "new"


class TestCheckCodexVersion:
    def test_ok_when_version_meets_minimum(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "--version"], returncode=0, stdout="codex-cli 0.142.5\n", stderr=""
        )
        with patch.object(harness_common.subprocess, "run", return_value=completed):
            result = harness_common.check_codex_version((0, 142))

        assert result.ok is True
        assert result.detected == (0, 142)

    def test_warns_when_version_below_minimum(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "--version"], returncode=0, stdout="codex-cli 0.100.0\n", stderr=""
        )
        with patch.object(harness_common.subprocess, "run", return_value=completed):
            result = harness_common.check_codex_version((0, 142))

        assert result.ok is False
        assert result.detected == (0, 100)

    def test_errors_when_codex_missing(self) -> None:
        with patch.object(harness_common.subprocess, "run", side_effect=OSError("not found")):
            result = harness_common.check_codex_version((0, 142))

        assert result.ok is False
        assert result.detected is None

    def test_errors_when_version_unparseable(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "--version"], returncode=0, stdout="unknown\n", stderr=""
        )
        with patch.object(harness_common.subprocess, "run", return_value=completed):
            result = harness_common.check_codex_version((0, 142))

        assert result.ok is False
        assert result.detected is None


class TestFindRepoRoot:
    def test_finds_root_with_git_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        assert harness_common.find_repo_root(nested) == tmp_path

    def test_returns_none_when_no_git_dir(self, tmp_path: Path) -> None:
        assert harness_common.find_repo_root(tmp_path) is None


class TestCheckRequiredCodexFiles:
    def test_returns_missing_paths_only(self, tmp_path: Path) -> None:
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "hooks.json").write_text("{}", encoding="utf-8")

        missing = harness_common.check_required_codex_files(
            tmp_path, [".codex/hooks.json", ".codex/validation.json"]
        )

        assert missing == [".codex/validation.json"]


class TestCoerceValidationTimeout:
    def test_passes_through_int(self) -> None:
        assert harness_common.coerce_validation_timeout(30, default=60) == 30

    def test_converts_numeric_string(self) -> None:
        assert harness_common.coerce_validation_timeout("30", default=60) == 30

    def test_falls_back_on_non_numeric_string(self) -> None:
        assert harness_common.coerce_validation_timeout("soon", default=60) == 60

    def test_falls_back_on_missing_value(self) -> None:
        assert harness_common.coerce_validation_timeout(None, default=60) == 60

    def test_falls_back_on_bool(self) -> None:
        assert harness_common.coerce_validation_timeout(True, default=60) == 60

    def test_falls_back_on_list(self) -> None:
        assert harness_common.coerce_validation_timeout([1, 2], default=60) == 60


class TestParseEventsRealFormat:
    """codex-cli 0.142.x の実イベント形式（E2E で採取）に対する回帰テスト。"""

    REQUIRED = {"status", "summary", "files_changed", "validation", "risks"}

    @staticmethod
    def _fallback(status: str, summary: str) -> dict:
        return {"status": status, "summary": summary}

    def _events_file(self, tmp_path: Path, lines: list[str]) -> Path:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return events_path

    def test_extracts_schema_json_from_agent_message_text(self, tmp_path: Path) -> None:
        payload = {
            "status": "success",
            "summary": "HARNESS-E2E-OK.",
            "files_changed": [],
            "validation": [],
            "risks": [],
        }
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_2", "type": "agent_message", "text": json.dumps(payload)},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 78}}),
        ]
        result = harness_common.parse_events(
            self._events_file(tmp_path, lines), 0, self.REQUIRED, self._fallback
        )
        assert result == payload

    def test_error_items_are_not_treated_as_agent_text(self, tmp_path: Path) -> None:
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "error", "message": "trust bypass warning"},
                }
            ),
        ]
        result = harness_common.parse_events(
            self._events_file(tmp_path, lines), 0, self.REQUIRED, self._fallback
        )
        assert result["status"] == "success"
        assert "trust bypass warning" not in result["summary"]

    def test_plain_agent_text_falls_back_to_summary(self, tmp_path: Path) -> None:
        lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "plain answer"},
                }
            ),
        ]
        result = harness_common.parse_events(
            self._events_file(tmp_path, lines), 0, self.REQUIRED, self._fallback
        )
        assert result == {"status": "success", "summary": "plain answer"}
