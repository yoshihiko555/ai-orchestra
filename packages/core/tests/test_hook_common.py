import io
import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")


def test_read_hook_input_valid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Edit"}'))
    assert hook_common.read_hook_input() == {"tool_name": "Edit"}


def test_read_hook_input_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid-json"))
    assert hook_common.read_hook_input() == {}


def test_read_hook_input_returns_empty_dict_for_top_level_list(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([1, 2])))
    assert hook_common.read_hook_input() == {}


def test_read_hook_input_returns_empty_dict_for_top_level_string(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps("just a string")))
    assert hook_common.read_hook_input() == {}


def test_get_field_returns_value_or_empty_string() -> None:
    data = {"name": "alice", "empty": "", "none": None, "zero": 0}
    assert hook_common.get_field(data, "name") == "alice"
    assert hook_common.get_field(data, "missing") == ""
    assert hook_common.get_field(data, "empty") == ""
    assert hook_common.get_field(data, "none") == ""
    assert hook_common.get_field(data, "zero") == ""


def test_get_field_coerces_non_string_value_to_string() -> None:
    assert hook_common.get_field({"x": 5}, "x") == "5"
    assert hook_common.get_field({}, "x") == ""


# =========================================================================
# load_package_config
# =========================================================================


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestWriteJson:
    def test_write_json_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        data = {"key": "value", "nested": {"a": 1}}

        hook_common.write_json(str(path), data)

        assert json.loads(path.read_text(encoding="utf-8")) == data

    def test_write_json_does_not_leave_tmp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"

        hook_common.write_json(str(path), {"key": "value"})

        leftover_tmp_files = list(path.parent.glob(f"{path.name}.tmp.*"))
        assert leftover_tmp_files == []

    def test_write_json_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"

        hook_common.write_json(str(path), {"key": "first"})
        hook_common.write_json(str(path), {"key": "second"})

        assert json.loads(path.read_text(encoding="utf-8")) == {"key": "second"}

    def test_write_json_reraises_and_removes_tmp_file_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data.json"

        def _raise_os_error(_src: str, _dst: str) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", _raise_os_error)

        with pytest.raises(OSError):
            hook_common.write_json(str(path), {"key": "value"})

        leftover_tmp_files = list(path.parent.glob(f"{path.name}.tmp.*"))
        assert leftover_tmp_files == []

    def test_write_json_preserves_existing_file_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text(json.dumps({"key": "first"}), encoding="utf-8")
        os.chmod(str(path), 0o600)

        hook_common.write_json(str(path), {"key": "second"})

        assert os.stat(str(path)).st_mode & 0o777 == 0o600


class TestLoadPackageConfig:
    def test_project_local_overrides_orchestra_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    def test_falls_back_to_base_dir_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """project dir に .local がない場合は base と同じディレクトリの .local を使う。"""
        config_dir = tmp_path / "project" / ".claude" / "config" / "mypkg"
        _write_json(config_dir / "settings.json", {"key": "base"})
        _write_json(config_dir / "settings.local.json", {"key": "local-same-dir"})

        result = hook_common.load_package_config(
            "mypkg", "settings.json", str(tmp_path / "project")
        )
        assert result["key"] == "local-same-dir"

    def test_no_local_returns_base_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """local ファイルが存在しない場合は base のみ返す。"""
        config_dir = tmp_path / "project" / ".claude" / "config" / "mypkg"
        _write_json(config_dir / "settings.json", {"key": "base", "nested": {"a": 1}})

        result = hook_common.load_package_config(
            "mypkg", "settings.json", str(tmp_path / "project")
        )
        assert result == {"key": "base", "nested": {"a": 1}}


# =========================================================================
# resolve_path_within
# =========================================================================


class TestResolvePathWithin:
    def test_normal_relative_path_resolves_under_project_dir(self, tmp_path: Path) -> None:
        """通常の相対パスは project_dir 配下に解決される。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        result = hook_common.resolve_path_within(str(project_dir), "logs/sub", "out.jsonl")

        assert result == str(project_dir / "logs" / "sub" / "out.jsonl")

    def test_relative_path_with_parent_traversal_returns_none(self, tmp_path: Path) -> None:
        """`../` で project_dir の外を指す場合は None を返す。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (tmp_path / "outside").mkdir()

        result = hook_common.resolve_path_within(
            str(project_dir), "../../../tmp/outside", "out.jsonl"
        )

        assert result is None

    def test_absolute_path_outside_project_dir_returns_none(self, tmp_path: Path) -> None:
        """絶対パスが project_dir の外を指す場合は None を返す。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        result = hook_common.resolve_path_within(str(project_dir), str(outside_dir), "out.jsonl")

        assert result is None

    def test_symlink_escaping_project_dir_returns_none(self, tmp_path: Path) -> None:
        """project_dir 配下の symlink が外部を指す場合、realpath 解決で検出し None を返す。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        symlink_path = project_dir / "logs_link"
        symlink_path.symlink_to(outside_dir, target_is_directory=True)

        result = hook_common.resolve_path_within(str(project_dir), "logs_link", "out.jsonl")

        assert result is None

    def test_result_is_always_under_project_dir_when_not_none(self, tmp_path: Path) -> None:
        """None でない場合、返り値は必ず project_dir 配下にある不変条件を検証する。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_root = os.path.realpath(str(project_dir))

        result = hook_common.resolve_path_within(str(project_dir), "a/b/c", "out.jsonl")

        assert result is not None
        assert result == project_root or result.startswith(project_root + os.sep)
