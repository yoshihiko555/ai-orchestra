import io
import json
import sys
from pathlib import Path

from tests.module_loader import load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")


def test_read_hook_input_valid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Edit"}'))
    assert hook_common.read_hook_input() == {"tool_name": "Edit"}


def test_read_hook_input_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid-json"))
    assert hook_common.read_hook_input() == {}


def test_get_field_returns_value_or_empty_string() -> None:
    data = {"name": "alice", "empty": "", "none": None, "zero": 0}
    assert hook_common.get_field(data, "name") == "alice"
    assert hook_common.get_field(data, "missing") == ""
    assert hook_common.get_field(data, "empty") == ""
    assert hook_common.get_field(data, "none") == ""
    assert hook_common.get_field(data, "zero") == ""


# =========================================================================
# load_package_config
# =========================================================================


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestLoadPackageConfig:
    def test_project_local_overrides_orchestra_base(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """base が orchestra dir にあっても project dir の .local が優先される。"""
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"

        # orchestra dir にベース設定を配置
        base_path = orchestra_dir / "packages" / "mypkg" / "config" / "settings.json"
        _write_json(base_path, {"key": "base", "only_base": True})

        # project dir にローカル上書きを配置
        local_path = project_dir / ".claude" / "config" / "mypkg" / "settings.local.json"
        _write_json(local_path, {"key": "local"})

        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        result = hook_common.load_package_config("mypkg", "settings.json", str(project_dir))
        assert result["key"] == "local"  # local override が効いている
        assert result["only_base"] is True  # base のキーは保持

    def test_falls_back_to_base_dir_local(self, tmp_path: Path, monkeypatch: object) -> None:
        """project dir に .local がない場合は base と同じディレクトリの .local を使う。"""
        config_dir = tmp_path / "project" / ".claude" / "config" / "mypkg"
        _write_json(config_dir / "settings.json", {"key": "base"})
        _write_json(config_dir / "settings.local.json", {"key": "local-same-dir"})

        result = hook_common.load_package_config(
            "mypkg", "settings.json", str(tmp_path / "project")
        )
        assert result["key"] == "local-same-dir"

    def test_no_local_returns_base_only(self, tmp_path: Path, monkeypatch: object) -> None:
        """local ファイルが存在しない場合は base のみ返す。"""
        config_dir = tmp_path / "project" / ".claude" / "config" / "mypkg"
        _write_json(config_dir / "settings.json", {"key": "base", "nested": {"a": 1}})

        result = hook_common.load_package_config(
            "mypkg", "settings.json", str(tmp_path / "project")
        )
        assert result == {"key": "base", "nested": {"a": 1}}
