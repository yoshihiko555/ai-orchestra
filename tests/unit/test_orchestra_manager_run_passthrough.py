"""`orchestra-manager.py run -- <args>` パススルーとグローバルオプション共存のテスト
（PR #162 レビュー指摘 FIX 3）。

前回修正で `argv[0] == "run"` 限定にした結果、`--orchestra-dir <dir> run ... -- <args>`
のようにグローバルオプションが `run` より前に置かれるケースで `--` 以降が剥がされず
unrecognized arguments になっていた。`_split_run_passthrough` / `_first_positional_command`
はこの分割ロジックを単独関数として切り出したもの。ここでは argparse 実行や実際の
スクリプト起動を伴わない、純粋なトークン列変換として決定論的に検証する。
"""

from __future__ import annotations

from tests.module_loader import load_module

manager_mod = load_module("orchestra_manager_run_passthrough", "scripts/orchestra-manager.py")

_first_positional_command = manager_mod._first_positional_command
_split_run_passthrough = manager_mod._split_run_passthrough


class TestFirstPositionalCommand:
    def test_run_as_first_token(self) -> None:
        assert _first_positional_command(["run", "pkg", "script"]) == "run"

    def test_orchestra_dir_option_is_skipped(self) -> None:
        argv = ["--orchestra-dir", "/some/dir", "run", "pkg", "script"]
        assert _first_positional_command(argv) == "run"

    def test_orchestra_dir_equals_form_is_skipped(self) -> None:
        argv = ["--orchestra-dir=/some/dir", "run", "pkg", "script"]
        assert _first_positional_command(argv) == "run"

    def test_meta_is_not_confused_with_run(self) -> None:
        argv = ["meta", "register", "--overlay", "d", "--", "--extra"]
        assert _first_positional_command(argv) == "meta"

    def test_empty_argv_returns_none(self) -> None:
        assert _first_positional_command([]) is None


class TestSplitRunPassthrough:
    # case 1: `run pkg script -- args`
    def test_run_passthrough_without_global_options(self) -> None:
        argv = ["run", "my-pkg", "my-script", "--", "--foo", "bar"]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == ["run", "my-pkg", "my-script"]
        assert script_args == ["--foo", "bar"]

    # case 2: `--orchestra-dir X run pkg script -- args`
    def test_run_passthrough_with_leading_orchestra_dir_option(self) -> None:
        argv = [
            "--orchestra-dir",
            "/some/dir",
            "run",
            "my-pkg",
            "my-script",
            "--",
            "--foo",
            "bar",
        ]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == [
            "--orchestra-dir",
            "/some/dir",
            "run",
            "my-pkg",
            "my-script",
        ]
        assert script_args == ["--foo", "bar"]

    def test_run_passthrough_with_leading_orchestra_dir_equals_form(self) -> None:
        argv = ["--orchestra-dir=/some/dir", "run", "my-pkg", "my-script", "--", "--foo"]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == ["--orchestra-dir=/some/dir", "run", "my-pkg", "my-script"]
        assert script_args == ["--foo"]

    # case 3: `meta register --overlay d -- --extra` — meta は argparse.REMAINDER で
    # 自前パススルーするため、run 以外のコマンドでは一切分割しないこと
    def test_meta_command_with_double_dash_is_not_split(self) -> None:
        argv = ["meta", "register", "--overlay", "d", "--", "--extra"]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == argv
        assert script_args == []

    def test_run_without_double_dash_is_not_split(self) -> None:
        argv = ["run", "my-pkg", "my-script"]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == argv
        assert script_args == []

    def test_non_run_command_without_orchestra_dir_is_not_split(self) -> None:
        argv = ["install", "some-package"]

        parser_argv, script_args = _split_run_passthrough(argv)

        assert parser_argv == argv
        assert script_args == []
