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
