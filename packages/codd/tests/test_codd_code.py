"""codd_code（コード⇔ドキュメントのトレーサビリティ抽出、Issue #98）の unit test。"""

from __future__ import annotations

from tests.module_loader import load_module

cc = load_module("codd_common", "packages/codd/lib/codd_common.py")
cx = load_module("codd_code", "packages/codd/lib/codd_code.py")

INLINE_CONFIDENCE = 0.7


# ---------------------------------------------------------------------------
# kind 推定（パス規約）
# ---------------------------------------------------------------------------


def test_infer_kind_detects_test_directory() -> None:
    assert cx.infer_kind("packages/codd/tests/test_codd_cli.py") == "test"


def test_infer_kind_detects_test_name_prefix() -> None:
    assert cx.infer_kind("src/test_helpers.py") == "test"


def test_infer_kind_detects_test_name_suffix() -> None:
    assert cx.infer_kind("src/helpers_test.go") == "test"


def test_infer_kind_defaults_to_code() -> None:
    assert cx.infer_kind("packages/codd/lib/codd_common.py") == "code"


# ---------------------------------------------------------------------------
# Python 抽出（AST ベース / module docstring）
# ---------------------------------------------------------------------------


def test_extract_python_reads_module_docstring_annotation() -> None:
    text = (
        '"""\ncodd:implements design:codd-coherence-layer\n"""\n\ndef hello() -> None:\n    pass\n'
    )
    node, errors = cx.extract_code_node("packages/codd/scripts/example.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.node_id == "code:example"
    assert node.kind == "code"
    assert node.status == "active"
    assert node.depends_on == (
        cc.Dependency(
            id="design:codd-coherence-layer", relation="implements", confidence=INLINE_CONFIDENCE
        ),
    )


def test_extract_python_ignores_annotation_outside_docstring() -> None:
    # 本文コード中の文字列リテラルに `codd:` 風の文字列があっても誤検出しない（AST 制約）。
    text = (
        '"""通常の docstring。"""\n\nMESSAGE = "codd:implements design:should-not-be-picked-up"\n'
    )
    node, errors = cx.extract_code_node("packages/codd/scripts/example.py", text, INLINE_CONFIDENCE)
    assert node is None
    assert errors == []


def test_extract_python_returns_none_on_syntax_error() -> None:
    node, errors = cx.extract_code_node("broken.py", "def (:\n", INLINE_CONFIDENCE)
    assert node is None
    assert errors == []


def test_extract_python_supports_implicit_string_concatenation_docstring() -> None:
    # tokenize ベースの軽量抽出でも、暗黙の文字列連結（"a" "b"）による docstring は
    # ast.get_docstring と同じ結果になるべき（Issue #98 レビュー対応: codd_code.py:131）。
    text = '"codd:implements " "design:codd-coherence-layer"\n'
    node, errors = cx.extract_code_node("packages/codd/scripts/concat.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.depends_on[0].id == "design:codd-coherence-layer"


def test_extract_python_supports_parenthesized_docstring() -> None:
    # `("""...""")` のように docstring を丸括弧で囲む書き方も、`ast.get_docstring()`
    # は同じ module docstring として認識する。先頭トークンが `(` だと即座に走査を
    # 打ち切る旧実装では黙って見落としていた（レビュー対応: codd_code.py:160）。
    text = '("""codd:implements design:codd-coherence-layer""")\n'
    node, errors = cx.extract_code_node("packages/codd/scripts/paren.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.depends_on[0].id == "design:codd-coherence-layer"


def test_extract_python_ignores_string_concatenation_expression() -> None:
    # `"""..."""  + "suffix"` は BinOp（二項演算式）であり、`ast.get_docstring()` は
    # Constant ではないため docstring と認識しない。先頭 STRING トークンだけを
    # 値化する旧実装は、この場合も docstring として誤抽出していた
    # （レビュー対応: codd_code.py:160）。
    text = '"""codd:implements design:should-not-be-picked-up""" + "suffix"\n'
    node, errors = cx.extract_code_node(
        "packages/codd/scripts/concat_expr.py", text, INLINE_CONFIDENCE
    )
    assert node is None
    assert errors == []


def test_extract_python_ignores_bytes_literal_as_first_statement() -> None:
    # bytes リテラル（b"..."）は str ではないため docstring として扱われない
    # （ast.get_docstring と同じ挙動）。
    text = 'b"""codd:implements design:should-not-be-picked-up"""\n'
    node, errors = cx.extract_code_node(
        "packages/codd/scripts/bytes_first.py", text, INLINE_CONFIDENCE
    )
    assert node is None
    assert errors == []


def test_extract_python_ignores_fstring_as_first_statement() -> None:
    # f-string は AST 上 Constant ではなく JoinedStr のため docstring 扱いされない
    # （ast.get_docstring と同じ挙動）。
    text = 'f"codd:implements design:should-not-be-picked-up {1}"\n'
    node, errors = cx.extract_code_node(
        "packages/codd/scripts/fstring_first.py", text, INLINE_CONFIDENCE
    )
    assert node is None
    assert errors == []


def test_extract_python_ignores_docstring_after_leading_statement() -> None:
    # docstring はモジュール先頭の最初の文である必要がある。先に他の文があれば
    # 以降の文字列リテラルは docstring として扱われない（ast.get_docstring と同じ）。
    text = 'from __future__ import annotations\n"""codd:implements design:x"""\n'
    node, errors = cx.extract_code_node(
        "packages/codd/scripts/after_stmt.py", text, INLINE_CONFIDENCE
    )
    assert node is None
    assert errors == []


def test_extract_python_returns_none_without_annotation() -> None:
    text = '"""通常の docstring。codd と無関係。"""\n'
    node, errors = cx.extract_code_node("packages/codd/lib/plain.py", text, INLINE_CONFIDENCE)
    assert node is None
    assert errors == []


def test_extract_python_supports_explicit_node_id_and_kind() -> None:
    text = (
        '"""\n'
        "codd:node_id code:custom-slug\n"
        "codd:kind test\n"
        "codd:owner ai-orchestra\n"
        "codd:references adr:ADR-20260624-026\n"
        '"""\n'
    )
    node, errors = cx.extract_code_node("packages/codd/lib/anything.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.node_id == "code:custom-slug"
    assert node.kind == "test"
    assert node.owner == "ai-orchestra"
    assert node.depends_on == (
        cc.Dependency(
            id="adr:ADR-20260624-026", relation="references", confidence=INLINE_CONFIDENCE
        ),
    )


def test_extract_python_supports_multiple_dependencies() -> None:
    text = (
        '"""\n'
        "codd:implements design:codd-coherence-layer\n"
        "codd:references adr:ADR-20260624-026\n"
        '"""\n'
    )
    node, errors = cx.extract_code_node("packages/codd/scripts/codd.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert len(node.depends_on) == 2


def test_extract_python_reports_error_for_relation_without_value() -> None:
    # `codd:implements` のみで参照先 value が無い注釈は、依存から黙って除外せず
    # エラーメッセージとして返す（Issue #98 レビュー対応）。
    text = '"""\ncodd:implements\n"""\n'
    node, errors = cx.extract_code_node(
        "packages/codd/lib/broken_annotation.py", text, INLINE_CONFIDENCE
    )
    assert node is not None
    assert node.depends_on == ()
    assert len(errors) == 1
    assert "codd:implements" in errors[0]
    assert "broken_annotation.py" in errors[0]


def test_extract_python_reserved_key_without_value_is_not_an_error() -> None:
    # 予約語（owner 等）は value 省略が許容される（依存宣言ではないため）。
    text = '"""\ncodd:owner\ncodd:implements design:codd-coherence-layer\n"""\n'
    node, errors = cx.extract_code_node(
        "packages/codd/lib/optional_owner.py", text, INLINE_CONFIDENCE
    )
    assert node is not None
    assert errors == []
    assert node.owner is None


# ---------------------------------------------------------------------------
# `//` 系言語抽出（先頭コメントブロック）
# ---------------------------------------------------------------------------


def test_extract_line_comment_reads_leading_block() -> None:
    text = "// codd:implements design:codd-coherence-layer\n// 通常のコメント\n\nfunc main() {}\n"
    node, errors = cx.extract_code_node("src/main.go", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.node_id == "code:main"
    assert node.depends_on[0].relation == "implements"
    assert node.depends_on[0].confidence == INLINE_CONFIDENCE


def test_extract_line_comment_skips_shebang() -> None:
    text = "#!/usr/bin/env node\n// codd:implements design:codd-coherence-layer\n"
    node, errors = cx.extract_code_node("scripts/run.js", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.node_id == "code:run"


def test_extract_line_comment_stops_at_non_comment_line() -> None:
    text = "console.log('start')\n// codd:implements design:should-not-be-picked-up\n"
    node, errors = cx.extract_code_node("scripts/run.js", text, INLINE_CONFIDENCE)
    assert node is None
    assert errors == []


def test_extract_returns_none_for_unsupported_extension() -> None:
    node, errors = cx.extract_code_node(
        "docs/notes.txt", "codd:implements design:x", INLINE_CONFIDENCE
    )
    assert node is None
    assert errors == []


def test_extract_line_comment_supports_mjs_and_cjs_extensions() -> None:
    # .mjs/.cjs も code_scope に含めれば言語判定される（Issue #98 レビュー対応）。
    text = "// codd:implements design:codd-coherence-layer\n"
    node_mjs, errors_mjs = cx.extract_code_node("src/main.mjs", text, INLINE_CONFIDENCE)
    node_cjs, errors_cjs = cx.extract_code_node("src/main.cjs", text, INLINE_CONFIDENCE)
    assert node_mjs is not None
    assert node_cjs is not None
    assert errors_mjs == []
    assert errors_cjs == []
    assert node_mjs.node_id == "code:main"
    assert node_cjs.node_id == "code:main"


def test_extract_line_comment_supports_mts_and_cts_extensions() -> None:
    # TypeScript の .mts/.cts も抽出対象（Issue #98 レビュー対応）。
    text = "// codd:implements design:codd-coherence-layer\n"
    node_mts, errors_mts = cx.extract_code_node("src/main.mts", text, INLINE_CONFIDENCE)
    node_cts, errors_cts = cx.extract_code_node("src/main.cts", text, INLINE_CONFIDENCE)
    assert node_mts is not None
    assert node_cts is not None
    assert errors_mts == []
    assert errors_cts == []
    assert node_mts.node_id == "code:main"
    assert node_cts.node_id == "code:main"


def test_is_supported_suffix_reflects_extract_code_node_coverage() -> None:
    # codd.py 側の事前フィルタ（issue #1）が抽出可否と食い違わないことを保証する。
    assert cx.is_supported_suffix("src/main.py") is True
    assert cx.is_supported_suffix("src/main.ts") is True
    assert cx.is_supported_suffix("src/main.mts") is True
    assert cx.is_supported_suffix("src/main.cts") is True
    assert cx.is_supported_suffix("assets/logo.png") is False
    assert cx.is_supported_suffix("docs/notes.txt") is False


# ---------------------------------------------------------------------------
# BOM 除去（Issue #98 レビュー対応）
# ---------------------------------------------------------------------------


def test_extract_line_comment_strips_leading_bom() -> None:
    # BOM 付き JS/TS は先頭行が `﻿//` になり、BOM を除去しないと
    # 行コメント判定（startswith("//")）に失敗して注釈が無言でスキップされる。
    text = "﻿// codd:implements design:codd-coherence-layer\n"
    node, errors = cx.extract_code_node("src/main.ts", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.depends_on[0].id == "design:codd-coherence-layer"


def test_extract_python_strips_leading_bom() -> None:
    # BOM 付き Python は `ast.parse` が構文エラーになるため、docstring 抽出前に除去する。
    text = '﻿"""\ncodd:implements design:codd-coherence-layer\n"""\n'
    node, errors = cx.extract_code_node("src/main.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.depends_on[0].id == "design:codd-coherence-layer"


# ---------------------------------------------------------------------------
# ソース注釈の kind 語彙制限（Issue #98 レビュー対応）
# ---------------------------------------------------------------------------


def test_extract_rejects_non_code_test_kind_and_falls_back_to_inferred() -> None:
    # ソースファイルで `codd:kind requirement` のようなドキュメント語彙を許すと
    # kind 体系が崩れるため、code/test 以外はエラー報告のうえ infer_kind() へ
    # フォールバックする。
    text = '"""\ncodd:kind requirement\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert node.kind == "code"  # infer_kind() へフォールバック
    assert len(errors) == 1
    assert "codd:kind requirement" in errors[0]


def test_extract_accepts_test_kind_for_source_annotation() -> None:
    text = '"""\ncodd:kind test\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert node.kind == "test"
    assert errors == []


# ---------------------------------------------------------------------------
# 不正な注釈構文の検出（Issue #98 レビュー対応）
# ---------------------------------------------------------------------------


def test_extract_reports_malformed_annotation_hyphenated_key() -> None:
    # `codd:node-id`（ハイフン）はタイプミスの可能性が高いが、既存の正規表現には
    # マッチせず黙って無視されていた。malformed_annotation として報告する。
    text = '"""\ncodd:node-id code:custom\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None  # 他の正しい注釈（implements）からノードは構築される
    assert any("codd:node-id" in e for e in errors)


def test_extract_reports_malformed_annotation_equals_syntax() -> None:
    # `codd:node_id=value`（`=` 区切り）も文法違反として報告する。
    text = '"""\ncodd:node_id=code:custom\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert any("codd:node_id=code:custom" in e for e in errors)
    # 不正構文はパースされないため node_id は自動生成のまま。
    assert node.node_id == "code:mod"


def test_extract_ignores_ordinary_comment_not_prefixed_with_codd() -> None:
    # `codd:` で始まらない通常のコメントは従来通り無視される（誤検出しない）。
    text = "// この関数は codd の実装例です\n// codd:implements design:x\n"
    node, errors = cx.extract_code_node("src/main.go", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []


# ---------------------------------------------------------------------------
# tokenize 早期打ち切り（レビュー対応: codd_code.py:156 / codd_code.py:159）
# ---------------------------------------------------------------------------


def test_extract_python_docstring_survives_later_token_error() -> None:
    # 先頭の文（docstring）の終端が判明した時点で tokenize を打ち切るため、
    # 後方の未閉じ文字列リテラル由来の TokenError に巻き込まれず、有効な
    # 先頭注釈を取り出せる（レビュー対応: codd_code.py:156）。
    text = (
        '"""\ncodd:implements design:codd-coherence-layer\n"""\n\n'
        'def broken():\n    s = "unterminated\n'
    )
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert errors == []
    assert node.depends_on[0].id == "design:codd-coherence-layer"


def test_extract_python_module_leading_indent_is_not_docstring() -> None:
    # モジュール先頭からインデントされた不正な Python（`ast.parse` なら
    # `IndentationError` になるケース）は docstring として誤って取り込まない
    # （レビュー対応: codd_code.py:159）。
    text = '    """\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is None
    assert errors == []


# ---------------------------------------------------------------------------
# 予約 key の重複検出（レビュー対応: codd_code.py:242）
# ---------------------------------------------------------------------------


def test_extract_reports_malformed_annotation_duplicate_node_id() -> None:
    # `codd:node_id` が複数回指定された場合、採用されるのは最初の値のみだが、
    # 重複自体を malformed_annotation として報告する（黙って握りつぶさない）。
    text = '"""\ncodd:node_id code:first\ncodd:node_id code:second\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert node.node_id == "code:first"  # 最初の値のみ採用
    assert any("codd:node_id" in e and "2 回" in e for e in errors)


def test_extract_reports_malformed_annotation_duplicate_kind() -> None:
    # 正しい `codd:kind code` の後に禁止された `codd:kind requirement` が
    # 続いても、従来は最初の truthy 値だけを見て検証をすり抜けていた。
    # 重複自体をエラーとして報告する。
    text = '"""\ncodd:kind code\ncodd:kind requirement\ncodd:implements design:x\n"""\n'
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert node.kind == "code"  # 最初の値のみ採用
    assert any("codd:kind" in e and "2 回" in e for e in errors)


def test_extract_reports_malformed_annotation_duplicate_status_and_owner() -> None:
    text = (
        '"""\ncodd:status active\ncodd:status draft\n'
        "codd:owner team-a\ncodd:owner team-b\n"
        'codd:implements design:x\n"""\n'
    )
    node, errors = cx.extract_code_node("src/mod.py", text, INLINE_CONFIDENCE)
    assert node is not None
    assert node.status == "active"
    assert node.owner == "team-a"
    assert any("codd:status" in e and "2 回" in e for e in errors)
    assert any("codd:owner" in e and "2 回" in e for e in errors)
