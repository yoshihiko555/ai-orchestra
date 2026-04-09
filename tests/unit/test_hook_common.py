"""hook_common.py のユニットテスト。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import load_module

hook_common = load_module("hook_common_test", "packages/core/hooks/hook_common.py")


class TestDeepMerge:
    """deep_merge のテスト。"""

    def test_simple_merge(self):
        """フラットな辞書のマージ。"""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = hook_common.deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """ネストされた辞書の再帰マージ。"""
        base = {"codex": {"model": "gpt-5", "sandbox": {"analysis": "read-only"}}}
        override = {"codex": {"model": "o3-pro"}}
        result = hook_common.deep_merge(base, override)
        assert result == {"codex": {"model": "o3-pro", "sandbox": {"analysis": "read-only"}}}

    def test_override_replaces_non_dict(self):
        """override が非 dict の場合、base の dict を置き換える。"""
        base = {"key": {"nested": "value"}}
        override = {"key": "flat_string"}
        result = hook_common.deep_merge(base, override)
        assert result == {"key": "flat_string"}

    def test_empty_override(self):
        """空の override はベースをそのまま返す。"""
        base = {"a": 1}
        result = hook_common.deep_merge(base, {})
        assert result == {"a": 1}

    def test_empty_base(self):
        """空のベースに override をマージ。"""
        result = hook_common.deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_both_empty(self):
        """両方空。"""
        result = hook_common.deep_merge({}, {})
        assert result == {}

    def test_list_replaced_not_merged(self):
        """リスト値は置き換え（マージではない）。"""
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = hook_common.deep_merge(base, override)
        assert result == {"items": [4, 5]}

    def test_does_not_mutate_base(self):
        """base を変更しない。"""
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        hook_common.deep_merge(base, override)
        assert base == {"a": {"b": 1}}


class TestReadJsonSafe:
    """read_json_safe のテスト。"""

    def test_valid_json(self, tmp_path):
        """正常な JSON を読み込める。"""
        path = tmp_path / "data.json"
        path.write_text('{"key": "value"}', encoding="utf-8")
        result = hook_common.read_json_safe(str(path))
        assert result == {"key": "value"}

    def test_invalid_json(self, tmp_path):
        """壊れた JSON は空辞書を返す。"""
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        result = hook_common.read_json_safe(str(path))
        assert result == {}

    def test_nonexistent_file(self, tmp_path):
        """存在しないファイルは空辞書を返す。"""
        result = hook_common.read_json_safe(str(tmp_path / "missing.json"))
        assert result == {}

    def test_non_dict_json(self, tmp_path):
        """JSON が dict でない場合、空辞書を返す。"""
        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = hook_common.read_json_safe(str(path))
        assert result == {}


class TestWriteJson:
    """write_json のテスト。"""

    def test_writes_json(self, tmp_path):
        """JSON ファイルを書き出す。"""
        path = str(tmp_path / "output.json")
        hook_common.write_json(path, {"key": "value"})
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"key": "value"}

    def test_japanese_content(self, tmp_path):
        """日本語を含む JSON を正しく書き出す（ensure_ascii=False）。"""
        path = str(tmp_path / "jp.json")
        hook_common.write_json(path, {"名前": "テスト"})
        content = Path(path).read_text(encoding="utf-8")
        assert "テスト" in content
        assert "\\u" not in content


class TestAppendJsonl:
    """append_jsonl のテスト。"""

    def test_appends_line(self, tmp_path):
        """JSONL 形式で 1 行追記する。"""
        path = str(tmp_path / "log.jsonl")
        hook_common.append_jsonl(path, {"event": "test1"})
        hook_common.append_jsonl(path, {"event": "test2"})

        lines = Path(path).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "test1"
        assert json.loads(lines[1])["event"] == "test2"


class TestFindFirstText:
    """find_first_text のテスト。"""

    def test_top_level(self):
        """トップレベルのキーを見つける。"""
        data = {"name": "test", "value": 42}
        result = hook_common.find_first_text(data, {"name"})
        assert result == "test"

    def test_nested_dict(self):
        """ネストされた辞書内を探索する。"""
        data = {"outer": {"inner": {"target": "found"}}}
        result = hook_common.find_first_text(data, {"target"})
        assert result == "found"

    def test_nested_list(self):
        """リスト内の辞書を探索する。"""
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        result = hook_common.find_first_text(data, {"name"})
        assert result == "first"

    def test_not_found(self):
        """見つからない場合、空文字を返す。"""
        data = {"a": 1, "b": 2}
        result = hook_common.find_first_text(data, {"missing"})
        assert result == ""

    def test_empty_string_skipped(self):
        """空文字列の値はスキップされる。"""
        data = {"name": "", "other": {"name": "valid"}}
        result = hook_common.find_first_text(data, {"name"})
        assert result == "valid"


class TestFindFirstInt:
    """find_first_int のテスト。"""

    def test_int_value(self):
        """整数値を見つける。"""
        data = {"code": 42}
        result = hook_common.find_first_int(data, {"code"})
        assert result == 42

    def test_string_int_value(self):
        """文字列の数値を整数に変換する。"""
        data = {"code": "42"}
        result = hook_common.find_first_int(data, {"code"})
        assert result == 42

    def test_not_found(self):
        """見つからない場合、None を返す。"""
        data = {"a": "hello"}
        result = hook_common.find_first_int(data, {"code"})
        assert result is None

    def test_nested(self):
        """ネスト構造内を探索する。"""
        data = {"result": {"exit_code": 1}}
        result = hook_common.find_first_int(data, {"exit_code"})
        assert result == 1


class TestFindPackageConfig:
    """find_package_config のテスト。"""

    def test_project_path_preferred(self, tmp_path, monkeypatch):
        """プロジェクトパスを優先する。"""
        project_dir = str(tmp_path / "project")
        config_dir = tmp_path / "project" / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "cli-tools.yaml"
        config_file.write_text("test: true")

        result = hook_common.find_package_config("agent-routing", "cli-tools.yaml", project_dir)
        assert result == str(config_file)

    def test_orchestra_dir_fallback(self, tmp_path, monkeypatch):
        """プロジェクトにない場合、AI_ORCHESTRA_DIR にフォールバック。"""
        orchestra_dir = tmp_path / "orchestra"
        config_dir = orchestra_dir / "packages" / "agent-routing" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "cli-tools.yaml"
        config_file.write_text("test: true")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))

        project_dir = str(tmp_path / "empty-project")
        result = hook_common.find_package_config("agent-routing", "cli-tools.yaml", project_dir)
        assert result == str(config_file)

    def test_not_found(self, tmp_path, monkeypatch):
        """どちらにもない場合、空文字を返す。"""
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        result = hook_common.find_package_config("pkg", "config.yaml", str(tmp_path))
        assert result == ""


class TestLoadPackageConfig:
    """load_package_config のテスト。"""

    def test_base_only(self, tmp_path, monkeypatch):
        """ベース設定のみの場合、そのまま返す。"""
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.yaml").write_text(
            "codex:\n  model: gpt-5\n  sandbox:\n    analysis: read-only\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

        result = hook_common.load_package_config("agent-routing", "cli-tools.yaml", str(tmp_path))
        assert result["codex"]["model"] == "gpt-5"

    def test_local_override_merged(self, tmp_path, monkeypatch):
        """ローカル上書きをマージする。"""
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.yaml").write_text(
            "codex:\n  model: gpt-5\n  sandbox:\n    analysis: read-only\n",
            encoding="utf-8",
        )
        (config_dir / "cli-tools.local.yaml").write_text(
            "codex:\n  model: o3-pro\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)

        result = hook_common.load_package_config("agent-routing", "cli-tools.yaml", str(tmp_path))
        assert result["codex"]["model"] == "o3-pro"
        assert result["codex"]["sandbox"]["analysis"] == "read-only"

    def test_config_not_found(self, tmp_path, monkeypatch):
        """設定ファイルが見つからない場合、空辞書を返す。"""
        monkeypatch.delenv("AI_ORCHESTRA_DIR", raising=False)
        result = hook_common.load_package_config("missing", "config.yaml", str(tmp_path))
        assert result == {}


class TestSafeHookExecution:
    """safe_hook_execution のテスト。"""

    def test_normal_execution(self):
        """正常な関数は通常通り実行される。"""

        @hook_common.safe_hook_execution
        def ok_func():
            pass

        ok_func()  # 例外なし

    def test_exception_caught(self, capsys):
        """例外は stderr に出力され exit(0) する。"""

        @hook_common.safe_hook_execution
        def bad_func():
            raise ValueError("test error")

        with pytest.raises(SystemExit, match="0"):
            bad_func()

        captured = capsys.readouterr()
        assert "test error" in captured.err


class TestReadConfigFile:
    """_read_config_file のテスト。"""

    def test_json_file(self, tmp_path):
        """JSON ファイルを読み込む。"""
        path = tmp_path / "config.json"
        path.write_text('{"key": "value"}', encoding="utf-8")
        result = hook_common._read_config_file(str(path))
        assert result == {"key": "value"}

    def test_yaml_file(self, tmp_path):
        """YAML ファイルを読み込む。"""
        path = tmp_path / "config.yaml"
        path.write_text("key: value\n", encoding="utf-8")
        result = hook_common._read_config_file(str(path))
        assert result == {"key": "value"}

    def test_empty_path(self):
        """空パスは空辞書を返す。"""
        result = hook_common._read_config_file("")
        assert result == {}

    def test_nonexistent_file(self):
        """存在しないパスは空辞書を返す。"""
        result = hook_common._read_config_file("/nonexistent/file.yaml")
        assert result == {}

    def test_invalid_yaml(self, tmp_path):
        """不正な YAML は空辞書を返す。"""
        path = tmp_path / "bad.yaml"
        path.write_text(":\n  :\n    - [invalid", encoding="utf-8")
        result = hook_common._read_config_file(str(path))
        assert result == {}

    def test_yaml_returns_non_dict(self, tmp_path):
        """YAML が dict 以外を返す場合、空辞書を返す。"""
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        result = hook_common._read_config_file(str(path))
        assert result == {}
