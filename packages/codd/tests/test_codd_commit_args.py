"""`git commit` 引数の分類（`classify_commit_invocation`）のテスト。

対象: packages/codd/lib/codd_commit_args.py（Issue #349 で hook から分割）。
評価セット対応: docs/evaluation/codd.md §4.2 EV-74 / EV-85 / EV-86 / EV-90 / EV-91。
"""

from __future__ import annotations

import pytest

from tests.module_loader import load_module

commit_args = load_module("codd_commit_args", "packages/codd/lib/codd_commit_args.py")


# ---------------------------------------------------------------------------
# validate hook: `git commit -a/--all` 候補ツリー再現（Issue #338 反復3、bot レビュー P1）
# ---------------------------------------------------------------------------


class TestValidateHookCommitAllClassification:
    """`classify_commit_invocation` の単体テスト。"""

    def test_detects_dash_a_alone(self) -> None:
        assert commit_args.classify_commit_invocation('git commit -a -m "msg"') == (
            True,
            False,
        )

    def test_detects_combined_dash_am(self) -> None:
        assert commit_args.classify_commit_invocation('git commit -am "msg"') == (True, False)

    def test_detects_dash_dash_all(self) -> None:
        assert commit_args.classify_commit_invocation('git commit --all -m "msg"') == (
            True,
            False,
        )

    def test_plain_commit_has_neither(self) -> None:
        assert commit_args.classify_commit_invocation('git commit -m "msg"') == (False, False)

    def test_no_args_commit_has_neither(self) -> None:
        assert commit_args.classify_commit_invocation("git commit") == (False, False)

    def test_dash_a_with_only_flag_is_unsupported(self) -> None:
        assert commit_args.classify_commit_invocation(
            'git commit -a --only docs/x.md -m "msg"'
        ) == (True, True)

    def test_patch_mode_is_unsupported_without_all(self) -> None:
        assert commit_args.classify_commit_invocation("git commit --patch") == (False, True)

    def test_pathspec_after_double_dash_is_unsupported(self) -> None:
        assert commit_args.classify_commit_invocation('git commit -m "msg" -- docs/x.md') == (
            False,
            True,
        )

    def test_trailing_bare_pathspec_is_unsupported(self) -> None:
        assert commit_args.classify_commit_invocation('git commit docs/x.md -m "msg"') == (
            False,
            True,
        )

    @pytest.mark.parametrize(
        ("invocation_suffix", "expected"),
        [
            ("-amfix", (True, False)),
            ("-ma", (False, False)),
        ],
    )
    def test_dash_amfix_attached_message_value_is_not_misread_as_interactive(
        self, invocation_suffix: str, expected: tuple[bool, bool]
    ) -> None:
        """`-amfix` は `-a` + `-m` の attached value `"fix"` であり値中の `i` を
        `-i`（interactive）と誤認してはならない。`-ma` は `-m` の attached value
        `"a"` であり `--all` 相当ではない（B-1: bot レビュー Critical 対応）。"""
        assert commit_args.classify_commit_invocation(f"git commit {invocation_suffix}") == expected

    def test_dash_u_bare_does_not_consume_following_dash_m_value(self) -> None:
        """`-u`（`--untracked-files`）は attached optional value のみを取り、次トークン
        （`-m` のフラグ自体）を値として飲み込んではならない（B-1 の回帰防止）。"""
        assert commit_args.classify_commit_invocation('git commit -u -m "msg"') == (
            False,
            False,
        )

    def test_dash_s_keyid_with_letter_a_is_not_misread_as_dash_a(self) -> None:
        """`-Sabc1234` は `-S`（`--gpg-sign`）の attached value であり、`--all` 相当では
        ない（Issue #338 反復7 bot レビュー対応。keyid は 16 進表記が一般的で
        `a` は頻出するため、`S` を `_COMMIT_VALUE_SHORT_CHARS` から外すと value 中の
        `a` を `-a`/`--all` と誤認し、未ステージ変更を候補ツリーへ誤って含めてしまう）。
        """
        assert commit_args.classify_commit_invocation("git commit -Sabc1234 -m x") == (
            False,
            False,
        )

    def test_dash_s_with_separate_token_is_not_consumed_as_keyid(self) -> None:
        """`-S abc`（値が別トークン）は git の挙動どおり `abc` を keyid として消費せず、
        pathspec 指定として扱う（`-S` は attached optional value のみを取るため）。"""
        assert commit_args.classify_commit_invocation("git commit -S abc -m x") == (
            False,
            True,
        )

    def test_pathspec_from_file_with_equals_is_unsupported(self) -> None:
        """`--pathspec-from-file=<file>` は再現困難モードとして分類する（B-2）。"""
        assert commit_args.classify_commit_invocation(
            'git commit -m "msg" --pathspec-from-file=/tmp/x.txt'
        ) == (False, True)

    def test_pathspec_from_file_separate_token_is_unsupported(self) -> None:
        """`--pathspec-from-file <file>`（値が別トークン）も再現困難モードとして分類する（B-2）。"""
        assert commit_args.classify_commit_invocation(
            'git commit -m "msg" --pathspec-from-file /tmp/x.txt'
        ) == (False, True)

    def test_trailer_value_is_not_misread_as_pathspec(self) -> None:
        """`--trailer <value>` の値はパススペックと誤認されず、`-a` 候補ツリー再現が
        有効なままになる（Issue #338 反復6: bot レビュー P1 対応）。"""
        assert commit_args.classify_commit_invocation(
            'git commit -a --trailer "Acked-by: dev" -m x'
        ) == (True, False)

    def test_no_all_after_dash_a_clears_all_flag(self) -> None:
        """`-a --no-all` は git の `-a, --[no-]all` 仕様どおり、後置の否定形が勝つため
        all 扱いにならない（Issue #338 反復6: bot レビュー P2 対応）。"""
        assert commit_args.classify_commit_invocation("git commit -a --no-all -m x") == (
            False,
            False,
        )

    def test_no_all_before_dash_a_still_yields_all(self) -> None:
        """`--no-all -a`（順序が逆）では最後に現れた `-a` が勝ち、all 扱いになる
        （Issue #338 反復6: bot レビュー P2 対応の回帰防止）。"""
        assert commit_args.classify_commit_invocation("git commit --no-all -a -m x") == (
            True,
            False,
        )
