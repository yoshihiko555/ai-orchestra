"""set-model.py の CLI と差分編集のテスト。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.module_loader import load_module

set_model = load_module("set_model_test", "scripts/set-model.py")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/set-model.py"
SSOT = Path("packages/agent-routing/config/cli-tools.yaml")
MIRROR = Path(".claude/config/agent-routing/cli-tools.yaml")


@pytest.fixture
def model_tree(tmp_path: Path) -> Path:
    """対象ファイルの最小構成を一時ディレクトリに作る。"""
    config = """# 先頭コメントは維持する
codex:
  # sandbox: read-only は維持する
  model: gpt-6-sol
  model_allowlist:
    - gpt-6-sol
  sandbox: read-only
antigravity:
  model: gemini-3.8-flash-high
  model_allowlist:
    - gemini-3.1-pro-high
    - gemini-3.8-flash-high
    - gemini-3.1-flash
subagent:
  # 選択肢: sonnet, opus, haiku
  default_model: sonnet
other:
  model: gpt-6-sol
  default_model: sonnet
"""
    files = {
        SSOT: config,
        MIRROR: config,
        Path("packages/core/hooks/hook_common.py"): 'DEFAULT_CODEX_MODEL = "gpt-6-sol"\n',
        Path("templates/codex/config.toml"): 'model = "gpt-6-sol"\n',
        Path(".codex/config.toml"): 'model = "gpt-6-sol"\n',
        Path("docs/design/codex-cli-harness.md"): 'model = "gpt-6-sol"\n"model": "gpt-6-sol"\n',
        Path("docs/reference/configuration.md"): """### codex セクション
  model: gpt-5.3-codex
### antigravity セクション
  model: gemini-3.8-flash-high
  model_allowlist:
    - gemini-3.1-pro-high
    - gemini-3.8-flash-high
| `model` | string | `gemini-3.8-flash-high` | 説明 |
### subagent セクション
  default_model: sonnet
### モデル変更のみ
  default_model: opus
""",
        Path(
            "docs/reference/hooks.md"
        ): '`agy -p "..." --model gemini-3.8-flash-high 2>/dev/null`\n',
        Path("docs/design/architecture.md"): """codex:
  model: gpt-5.3-codex
antigravity:
  model: gemini-3.1-pro-high
subagent:
  default_model: sonnet
""",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _run_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """実際の CLI を一時ツリーで起動する。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments, "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(root: Path) -> dict[Path, tuple[str, int]]:
    """一時ツリー内の内容と更新時刻を記録する。"""
    return {
        path.relative_to(root): (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_codex_updates_all_targets_and_keeps_comments(model_tree: Path) -> None:
    """Codex のモデルと単一 allowlist を全対象に反映する。"""
    result = _run_cli(model_tree, "codex", "gpt-6-astra")
    assert result.returncode == 0, result.stderr
    assert "changed: packages/agent-routing/config/cli-tools.yaml" in result.stdout
    for relative in (SSOT, MIRROR):
        content = (model_tree / relative).read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        assert data["codex"]["model"] == "gpt-6-astra"
        assert data["codex"]["model_allowlist"] == ["gpt-6-astra"]
        assert data["other"]["model"] == "gpt-6-sol"
        assert "# 先頭コメントは維持する" in content
        assert "  # sandbox: read-only は維持する" in content
        assert "  sandbox: read-only" in content
    for relative in (
        "packages/core/hooks/hook_common.py",
        "templates/codex/config.toml",
        ".codex/config.toml",
        "docs/design/codex-cli-harness.md",
    ):
        content = (model_tree / relative).read_text(encoding="utf-8")
        assert '"gpt-6-astra"' in content
        assert '"gpt-6-sol"' not in content


def test_antigravity_preserves_allowlist_and_updates_docs(model_tree: Path) -> None:
    """Antigravity は既存 allowlist を保持し、新しいモデルを先頭に加える。"""
    result = _run_cli(model_tree, "antigravity", "gemini-4-pro")
    assert result.returncode == 0, result.stderr
    for relative in (SSOT, MIRROR):
        data = yaml.safe_load((model_tree / relative).read_text(encoding="utf-8"))
        assert data["antigravity"]["model"] == "gemini-4-pro"
        assert data["antigravity"]["model_allowlist"] == [
            "gemini-4-pro",
            "gemini-3.1-pro-high",
            "gemini-3.8-flash-high",
            "gemini-3.1-flash",
        ]
    doc = (model_tree / "docs/reference/configuration.md").read_text(encoding="utf-8")
    assert "  model: gemini-4-pro" in doc
    assert "    - gemini-4-pro" in doc
    assert "| `model` | string | `gemini-4-pro`" in doc
    assert "  model: gpt-5.3-codex" in doc
    hooks = (model_tree / "docs/reference/hooks.md").read_text(encoding="utf-8")
    assert "--model gemini-4-pro" in hooks


def test_antigravity_does_not_duplicate_existing_allowlist_item(model_tree: Path) -> None:
    """新モデルが allowlist にあれば重複追加しない。"""
    for relative in (SSOT, MIRROR):
        path = model_tree / relative
        content = path.read_text(encoding="utf-8")
        path.write_text(
            content.replace(
                "    - gemini-3.1-flash\n", "    - gemini-3.1-flash\n    - gemini-4-pro\n"
            ),
            encoding="utf-8",
        )
    result = _run_cli(model_tree, "antigravity", "gemini-4-pro")
    assert result.returncode == 0, result.stderr
    for relative in (SSOT, MIRROR):
        data = yaml.safe_load((model_tree / relative).read_text(encoding="utf-8"))
        assert data["antigravity"]["model_allowlist"].count("gemini-4-pro") == 1


def test_hooks_doc_matches_backtick_prefixed_argument() -> None:
    """--model の直前が空白でなくてもモデル値を更新する。"""
    updated = set_model._patch_hooks_doc(
        "`--model gemini-3.8-flash-high`\n", "gemini-3.8-flash-high", "gemini-4-pro"
    )
    assert updated == "`--model gemini-4-pro`\n"


@pytest.mark.parametrize("new_model", ["opus", "claude-sonnet-5"])
def test_claude_accepts_alias_and_full_id(model_tree: Path, new_model: str) -> None:
    """Claude の別名とフル ID を受け付ける。"""
    result = _run_cli(model_tree, "claude", new_model)
    assert result.returncode == 0, result.stderr
    for relative in (SSOT, MIRROR):
        content = (model_tree / relative).read_text(encoding="utf-8")
        assert yaml.safe_load(content)["subagent"]["default_model"] == new_model
        assert "# 選択肢: sonnet, opus, haiku, inherit, またはフルモデル ID" in content
        assert "other:\n  model: gpt-6-sol\n  default_model: sonnet" in content
    doc = (model_tree / "docs/reference/configuration.md").read_text(encoding="utf-8")
    assert f"  default_model: {new_model}" in doc
    assert "### モデル変更のみ\n  default_model: opus" in doc
    architecture = (model_tree / "docs/design/architecture.md").read_text(encoding="utf-8")
    assert f"  default_model: {new_model}" in architecture
    assert "  model: gpt-5.3-codex" in architecture


@pytest.mark.parametrize(
    ("tool", "model"),
    [
        ("claude", "claude sonnet"),
        ("claude", "other-model"),
        ("codex", "gpt 6"),
        ("codex", "gpt$6"),
        ("antigravity", 'gemini"4'),
    ],
)
def test_invalid_model_writes_nothing(model_tree: Path, tool: str, model: str) -> None:
    """無効なモデル名では全ファイルを保護する。"""
    before = _snapshot(model_tree)
    result = _run_cli(model_tree, tool, model)
    assert result.returncode == 1
    assert "invalid" in result.stderr
    assert _snapshot(model_tree) == before


def test_same_value_is_no_op(model_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """現在値と同じ場合はファイルを開いて書かない。"""
    before = _snapshot(model_tree)
    assert set_model.main(["codex", "gpt-6-sol", "--root", str(model_tree)]) == 0
    assert "already gpt-6-sol, no changes made" in capsys.readouterr().out
    assert _snapshot(model_tree) == before


def test_dry_run_prints_diff_without_writes(
    model_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry-run は変更案だけを表示する。"""
    before = _snapshot(model_tree)
    assert set_model.main(["codex", "gpt-6-astra", "--dry-run", "--root", str(model_tree)]) == 0
    output = capsys.readouterr().out
    assert "--- packages/agent-routing/config/cli-tools.yaml" in output
    assert "--- .codex/config.toml" in output
    assert _snapshot(model_tree) == before


@pytest.mark.parametrize("missing", ["  model: gpt-6-sol\n", "codex:\n", "  model_allowlist:\n"])
def test_missing_required_pattern_is_atomic(model_tree: Path, missing: str) -> None:
    """正本の必須パターンがない場合、他ファイルにも書き込まない。"""
    path = model_tree / SSOT
    path.write_text(path.read_text(encoding="utf-8").replace(missing, ""), encoding="utf-8")
    before = _snapshot(model_tree)
    result = _run_cli(model_tree, "codex", "gpt-6-astra")
    assert result.returncode == 1
    assert "error:" in result.stderr
    assert _snapshot(model_tree) == before


def test_missing_optional_file_is_skipped(model_tree: Path) -> None:
    """任意ファイルがなくても正本の更新を進める。"""
    (model_tree / ".codex/config.toml").unlink()
    result = _run_cli(model_tree, "codex", "gpt-6-astra")
    assert result.returncode == 0, result.stderr
    assert "skip: .codex/config.toml (file not found)" in result.stdout
    data = yaml.safe_load((model_tree / SSOT).read_text(encoding="utf-8"))
    assert data["codex"]["model"] == "gpt-6-astra"


def test_show_reports_local_override(model_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show は正本の値とローカル上書きを区別して表示する。"""
    local = model_tree / ".claude/config/agent-routing/cli-tools.local.yaml"
    local.write_text("codex:\n  model: o3-pro\n", encoding="utf-8")
    assert set_model.main(["show", "--root", str(model_tree)]) == 0
    output = capsys.readouterr().out
    assert "codex.model: gpt-6-sol" in output
    assert "antigravity.model: gemini-3.8-flash-high" in output
    assert "subagent.default_model: sonnet" in output
    assert "codex.model (cli-tools.local.yaml override): o3-pro" in output


def test_show_cli_reads_temporary_root(model_tree: Path) -> None:
    """show コマンドのエントリポイントも指定された root を読む。"""
    result = _run_cli(model_tree, "show")
    assert result.returncode == 0, result.stderr
    assert "codex.model: gpt-6-sol" in result.stdout
    assert "antigravity.model: gemini-3.8-flash-high" in result.stdout
    assert "subagent.default_model: sonnet" in result.stdout


def test_codex_update_refreshes_ledger_hash_of_mirror(model_tree: Path) -> None:
    """ミラーを書き換えたら orchestra.json の台帳ハッシュが変更後内容に追従する。"""
    import hashlib

    ledger = model_tree / ".claude/orchestra.json"
    stale = "0" * 64
    ledger.write_text(
        '{\n  "file_hashes": {\n    "agent-routing": {\n'
        f'      "config/agent-routing/cli-tools.yaml": "{stale}"\n'
        "    }\n  }\n}\n",
        encoding="utf-8",
    )

    result = _run_cli(model_tree, "codex", "gpt-next")

    assert result.returncode == 0, result.stderr
    expected = hashlib.sha256((model_tree / MIRROR).read_bytes()).hexdigest()
    assert f'"config/agent-routing/cli-tools.yaml": "{expected}"' in ledger.read_text(
        encoding="utf-8"
    )
