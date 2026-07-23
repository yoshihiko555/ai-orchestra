"""image-generator / image-gen 指示書の構造契約テスト（Issue #133）。

`packages/image-generation/` はロジックが Markdown 指示書にしかないため、実際の
`codex exec` 呼び出しをモックする代わりに「指示書に必須の指示・トークンが存在し、
正しい順序で並んでいる」ことを検証する。振る舞い（Codex が実際にその通り動くか）
を保証するテストではなく、指示書契約テストという位置づけ。

対象（正本のみ。生成物 `.claude/skills/image-gen/SKILL.md` は対象外）:
- packages/image-generation/agents/image-generator.md
- packages/image-generation/config/image-generation.yaml
- facets/instructions/image-gen.md

各テストは docs/evaluation/image-generation.md の EV-NN に対応する（自動化可能な
観点のみ。EV-07/08/10/11/15 等は同ドキュメント「手動 E2E でしか検証できない範囲」
のためここでは扱わない）。
"""

from __future__ import annotations

import json
import re

from tests.module_loader import REPO_ROOT

AGENT_MD_PATH = REPO_ROOT / "packages" / "image-generation" / "agents" / "image-generator.md"
CONFIG_PATH = REPO_ROOT / "packages" / "image-generation" / "config" / "image-generation.yaml"
SKILL_MD_PATH = REPO_ROOT / "facets" / "instructions" / "image-gen.md"
MANIFEST_PATH = REPO_ROOT / "packages" / "image-generation" / "manifest.json"
LOCAL_CONFIG_PATH = (
    REPO_ROOT / ".claude" / "config" / "image-generation" / "image-generation.local.yaml"
)
PACKAGE_STYLES_DIR = REPO_ROOT / "packages" / "image-generation" / "config" / "styles"
PACKAGE_STYLE_PATH = PACKAGE_STYLES_DIR / "isometric.md"
DISTRIBUTED_STYLE_PATH = (
    REPO_ROOT / ".claude" / "config" / "image-generation" / "styles" / "isometric.md"
)
LEGACY_STYLE_PATH = REPO_ROOT / "docs" / "assets" / "diagram-style-prompt.md"

AGENT_MD = AGENT_MD_PATH.read_text(encoding="utf-8")
CONFIG_YAML = CONFIG_PATH.read_text(encoding="utf-8")
SKILL_MD = SKILL_MD_PATH.read_text(encoding="utf-8")
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

_HEADING_RE = re.compile(r"^(#{2,3}) (.+)$", re.MULTILINE)


def _sections(content: str) -> dict[str, str]:
    """`##`/`###` 見出しをキーに、次の見出し直前までの本文を値とする辞書を作る。"""
    matches = list(_HEADING_RE.finditer(content))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[title] = content[start:end]
    return sections


def _section(sections: dict[str, str], prefix: str) -> str:
    """指定 prefix で始まる見出しの本文を返す。見つからなければ空文字列。"""
    for title, body in sections.items():
        if title.startswith(prefix):
            return body
    return ""


def _has_heading(sections: dict[str, str], prefix: str) -> bool:
    return any(title.startswith(prefix) for title in sections)


_FULL_PROMPT_ASSIGNMENT_RE = re.compile(r'FULL_PROMPT="((?:[^"\\]|\\.|\\\n)*)"')


def _extract_full_prompt_body(section: str) -> str:
    """`FULL_PROMPT="..."` bash 代入式の本文だけを抽出する。

    セクション末尾までではなく、ダブルクォート文字列(行末バックスラッシュ継続を
    含む)の終端で正しく閉じた範囲のみを返す。これにより `${OUTPUT_LANGUAGE}` 等が
    代入式の外(Step 2 の他の説明文)に移動した場合にテストが誤って通るのを防ぐ。
    """
    match = _FULL_PROMPT_ASSIGNMENT_RE.search(section)
    assert match, 'FULL_PROMPT="..." の代入式が見つかりません'
    return match.group(1)


AGENT_SECTIONS = _sections(AGENT_MD)
SKILL_SECTIONS = _sections(SKILL_MD)

# Step 3 と Step 3.5 を区別するため、末尾スペース込みの prefix を使う
# ("Step 3.5 —" は "Step 3 " にはマッチしない: "3" の直後が "." であり " " ではない)
CONFIGURATION = _section(AGENT_SECTIONS, "Configuration")
SANDBOX_POLICY = _section(AGENT_SECTIONS, "Sandbox Policy")
STEP0 = _section(AGENT_SECTIONS, "Step 0")
STEP1 = _section(AGENT_SECTIONS, "Step 1")
STEP2 = _section(AGENT_SECTIONS, "Step 2")
STEP3 = _section(AGENT_SECTIONS, "Step 3 ")
STEP3_5 = _section(AGENT_SECTIONS, "Step 3.5")
STEP4 = _section(AGENT_SECTIONS, "Step 4")
FALLBACK = _section(AGENT_SECTIONS, "Fallback")

# "## Output Format" の本文はフェンスコードブロック内に "### 結果" 等の見出しっぽい
# 行を含み、_sections() の見出し検出（レベル2/3 とも拾う）がそこで区切ってしまう
# ため、_section() ではなくフェンスコードブロックそのものを正規表現で取り出す。
_OUTPUT_FORMAT_BLOCK_RE = re.compile(r"## Output Format\n\n```markdown\n(.*?)```", re.DOTALL)
_output_format_match = _OUTPUT_FORMAT_BLOCK_RE.search(AGENT_MD)
OUTPUT_FORMAT = _output_format_match.group(1) if _output_format_match else ""

SKILL_PHASE1 = _section(SKILL_SECTIONS, "Phase 1")
SKILL_PHASE2 = _section(SKILL_SECTIONS, "Phase 2")
SKILL_PHASE3 = _section(SKILL_SECTIONS, "Phase 3")


# ---------------------------------------------------------------------------
# EV-16: Step 0 kill-switch（codex.enabled）
# ---------------------------------------------------------------------------


class TestSandboxPolicyResidualRisk:
    """レビュー修正 2: Residual risk 節が style の supply-chain リスクに言及する。"""

    def test_residual_risk_mentions_style_as_untrusted_input(self) -> None:
        assert "Style definition files are untrusted input" in SANDBOX_POLICY

    def test_residual_risk_mentions_default_style_automatic_inclusion(self) -> None:
        assert "default_style" in SANDBOX_POLICY
        assert "automatically" in SANDBOX_POLICY.lower()

    def test_residual_risk_mentions_sync_propagation(self) -> None:
        assert "sync" in SANDBOX_POLICY.lower()
        assert "every project" in SANDBOX_POLICY.lower()

    def test_residual_risk_applies_existing_defense_in_depth_to_style(self) -> None:
        assert "defense-in-depth" in SANDBOX_POLICY.lower()


class TestStep0KillSwitch:
    """EV-16: Step 0 が最初の codex exec 呼び出しより前に配置され、DISABLED 時に停止する。"""

    def test_step0_heading_exists(self) -> None:
        assert _has_heading(AGENT_SECTIONS, "Step 0"), "Step 0 見出しが見つかりません"

    def test_step0_references_check_script(self) -> None:
        assert "check_image_gen_enabled.py" in STEP0

    def test_step0_disabled_stops_immediately(self) -> None:
        assert "DISABLED" in STEP0
        assert "stop" in STEP0.lower()

    def test_step0_mentions_local_yaml_fallback(self) -> None:
        assert "cli-tools.yaml" in STEP0
        assert ".local.yaml" in STEP0

    def test_step0_appears_before_first_codex_exec_invocation(self) -> None:
        """Step 0 見出しの文字位置が、最初の `codex exec` 実際の呼び出し行より前であること。

        単純な部分文字列 "codex exec" は Configuration 節などの説明文にも登場する
        （例: "This agent calls `codex exec` directly"）ため、行頭アンカー付き正規表現で
        「実際のコマンド行」のみを対象にする。
        """
        step0_match = re.search(r"^### Step 0\b", AGENT_MD, re.MULTILINE)
        assert step0_match, "Step 0 見出しが見つかりません"

        invocation_match = re.search(r"^codex exec\b", AGENT_MD, re.MULTILINE)
        assert invocation_match, "codex exec の実際の呼び出し行が見つかりません"

        assert step0_match.start() < invocation_match.start(), (
            "Step 0 は最初の codex exec 呼び出しより前に出現する必要があります"
        )

    def test_step0_no_placeholder_fallback(self) -> None:
        """EV-14 とも重なるが、kill-switch OFF 時も自前描画に倒れないことを Step 0 内で明示。"""
        assert "Pillow" in STEP0
        assert "placeholder" in STEP0.lower()


# ---------------------------------------------------------------------------
# EV-01 / EV-02: 出力先パス解決
# ---------------------------------------------------------------------------


class TestOutputPathResolution:
    """EV-01: 既定出力先, EV-02: --out / 絶対パス解決。"""

    def test_ev01_default_output_path_in_agent_md(self) -> None:
        assert "generated-images/<slug>.png" in STEP1

    def test_ev01_default_output_path_in_skill_md(self) -> None:
        assert "generated-images/<slug>.png" in SKILL_MD
        assert "generated-images/" in SKILL_PHASE1 or "generated-images/" in SKILL_MD

    def test_ev02_agent_md_resolves_absolute_path(self) -> None:
        assert "absolute" in STEP1.lower()

    def test_ev02_skill_md_mentions_out_option_and_absolute_path(self) -> None:
        assert "--out" in SKILL_MD
        assert "絶対パス" in SKILL_PHASE1


# ---------------------------------------------------------------------------
# EV-03: 空プロンプト時の確認（評価セット「自動テスト可能な範囲」記載分の補完）
# ---------------------------------------------------------------------------


class TestEmptyPromptConfirmation:
    """EV-03: プロンプトが空の場合、生成を実行せず AskUserQuestion で確認する。"""

    def test_empty_prompt_triggers_ask_user_question(self) -> None:
        assert "AskUserQuestion" in SKILL_PHASE1
        assert "空" in SKILL_PHASE1


# ---------------------------------------------------------------------------
# EV-04: パストラバーサルガード
# ---------------------------------------------------------------------------


class TestPathTraversalGuard:
    """EV-04: リポジトリルート外への出力を拒否する。"""

    def test_uses_git_rev_parse_show_toplevel(self) -> None:
        assert "git rev-parse --show-toplevel" in STEP1

    def test_rejects_paths_outside_repo_root(self) -> None:
        assert "escapes repo root" in STEP1 or "outside the repo" in STEP1.lower()


# ---------------------------------------------------------------------------
# EV-05: フレッシュネスガード
# ---------------------------------------------------------------------------


class TestFreshnessGuard:
    """EV-05: マーカーより新しいファイルのみ採用。無ければ FAILURE。"""

    def test_marker_is_created_before_generation(self) -> None:
        assert "MARKER" in STEP3
        assert "newer" in STEP3.lower()

    def test_only_files_newer_than_marker_are_accepted(self) -> None:
        assert "-newer" in STEP3_5 or "newer than the" in STEP3_5.lower()

    def test_missing_fresh_file_is_treated_as_failure(self) -> None:
        assert "FAILURE" in STEP3_5
        assert "no image newer than the marker" in STEP3_5.lower()

    def test_stale_files_must_not_be_manually_recovered(self) -> None:
        """EV-05 の核心: `ls -t | head` 等での手動迂回禁止（過去の false-success 再発防止）。"""
        assert "ls -t" in STEP3_5


# ---------------------------------------------------------------------------
# EV-06: 生成物検証（PNG magic bytes / サイズ / フォールバックマーカー）
# ---------------------------------------------------------------------------


class TestOutputVerification:
    """EV-06: PNG magic bytes・サイズ閾値・フォールバックマーカー検知。"""

    def test_png_magic_bytes_check(self) -> None:
        assert "89504e47" in STEP4.lower()

    def test_size_threshold_check(self) -> None:
        assert re.search(r"\bSIZE\b", STEP4)
        assert re.search(r"\d{4,}", STEP4)  # 閾値の具体的な数値（バイト数）が存在する

    def test_fallback_marker_strings_detected(self) -> None:
        for marker in ("Pillow", "PIL", "ImageMagick", "matplotlib"):
            assert marker in STEP4, f"フォールバックマーカー {marker} への言及がありません"


# ---------------------------------------------------------------------------
# EV-09: コーディングモデルを image_model に使わない
# ---------------------------------------------------------------------------


class TestImageModelNotCodingModel:
    """EV-09: gpt-5.3-codex 等のコーディングモデルを image_model に使わない。"""

    def test_forbids_coding_model_example(self) -> None:
        assert "gpt-5.3-codex" in CONFIGURATION
        assert "Never use a coding model" in CONFIGURATION


# ---------------------------------------------------------------------------
# EV-17: 画像内テキストの出力言語
# ---------------------------------------------------------------------------


class TestOutputLanguage:
    """EV-17: output_language の既定値・上書き・プロンプト反映を検証する。"""

    def test_base_config_defaults_to_japanese(self) -> None:
        assert re.search(r"^output_language:\s*ja\s*$", CONFIG_YAML, re.MULTILINE)

    def test_configuration_resolves_local_override_and_fallback(self) -> None:
        assert "image-generation.local.yaml" in CONFIGURATION
        assert "output_language" in CONFIGURATION
        assert "OUTPUT_LANGUAGE" in CONFIGURATION
        assert "fall back to `ja`" in CONFIGURATION

    def test_full_prompt_uses_resolved_output_language(self) -> None:
        full_prompt_body = _extract_full_prompt_body(STEP2)
        assert "${OUTPUT_LANGUAGE}" in full_prompt_body
        assert "in-image text" in full_prompt_body

    def test_full_prompt_allows_english_technical_terms_and_proper_nouns(self) -> None:
        full_prompt = _extract_full_prompt_body(STEP2).lower().replace("\\\n", "")
        assert "technical terms" in full_prompt
        assert "proper nouns" in full_prompt
        assert "may remain in english" in full_prompt

    def test_explicit_user_language_takes_precedence(self) -> None:
        full_prompt = _extract_full_prompt_body(STEP2).lower()
        assert "user's prompt explicitly" in full_prompt
        assert "follow that request instead" in full_prompt
        # 逆検証: FULL_PROMPT 代入式の"後"にある Step 2 の説明文が
        # 抽出結果に混入していないこと（抽出が貪欲すぎないことのガード）。
        assert "do not tell codex" not in full_prompt
        assert "the cli query to codex is in english" not in full_prompt

    def test_output_format_reports_output_language(self) -> None:
        assert "{output_language}" in OUTPUT_FORMAT
        assert "fallback" in OUTPUT_FORMAT.lower()

    def test_configuration_validates_language_code_format(self) -> None:
        assert r"^[a-z]{2}(-[A-Z]{2})?$" in CONFIGURATION
        assert "invalid" in CONFIGURATION.lower()
        assert "fall back to `ja`" in CONFIGURATION


# ---------------------------------------------------------------------------
# EV-18 / EV-19: style 解決順・none 予約値・生成前検証
# ---------------------------------------------------------------------------


class TestStyleResolution:
    """EV-18/19: style は caller が優先順位どおり解決し、委譲前に検証する。"""

    def test_skill_documents_style_argument(self) -> None:
        assert "--style <name>" in SKILL_MD
        assert "--style none" in SKILL_MD

    def test_resolution_order_is_explicit(self) -> None:
        assert "`--style` > `default_style` > none" in SKILL_PHASE1
        assert "image-generation.local.yaml" in SKILL_PHASE1

    def test_none_is_reserved_and_overrides_default(self) -> None:
        assert "予約値" in SKILL_PHASE1
        assert "`default_style` より優先" in SKILL_PHASE1
        assert "`none.md` は style 定義として認めない" in SKILL_PHASE1

    def test_unknown_style_is_validated_before_delegation(self) -> None:
        assert ".claude/config/image-generation/styles/*.md" in SKILL_PHASE1
        assert "AskUserQuestion" in SKILL_PHASE1
        assert "委譲・画像生成より前" in SKILL_PHASE1
        assert "検証が完了するまで" in SKILL_PHASE1
        assert "黙って無視" in SKILL_PHASE1

    def test_phase2_passes_only_the_resolved_style_name(self) -> None:
        assert "スタイル: {検証済みの effective style 名" in SKILL_PHASE2

    def test_base_default_is_comment_only_and_local_override_is_set(self) -> None:
        assert not re.search(r"^default_style\s*:", CONFIG_YAML, re.MULTILINE)
        assert re.search(r"^# default_style:\s*isometric$", CONFIG_YAML, re.MULTILINE)
        assert LOCAL_CONFIG_PATH.read_text(encoding="utf-8").splitlines()[-1] == (
            "default_style: isometric"
        )


# ---------------------------------------------------------------------------
# EV-20: style 定義の package SSOT と nested config 配布
# ---------------------------------------------------------------------------


class TestStyleDistribution:
    """EV-20: isometric style が manifest 経由で所定の nested path へ配布される。"""

    def test_manifest_lists_nested_style_config(self) -> None:
        assert "config/styles/isometric.md" in MANIFEST["config"]

    def test_style_was_moved_from_legacy_docs_path(self) -> None:
        assert PACKAGE_STYLE_PATH.is_file()
        assert not LEGACY_STYLE_PATH.exists()

    def test_distributed_style_matches_package_source(self) -> None:
        assert DISTRIBUTED_STYLE_PATH.is_file()
        assert PACKAGE_STYLE_PATH.read_bytes() == DISTRIBUTED_STYLE_PATH.read_bytes()

    def test_bundled_style_remains_japanese(self) -> None:
        style = PACKAGE_STYLE_PATH.read_text(encoding="utf-8")
        assert "図解画像 共通スタイルプロンプト" in style
        assert "ビジュアルスタイル" in style


class TestStyleManifestDrift:
    """EV-20 補完: package の styles/*.md と manifest["config"] のエントリが

    双方向で一致することを検証する（スタイル追加時の配布漏れを CI で検出する）。
    """

    def test_every_style_file_is_listed_in_manifest(self) -> None:
        style_files = sorted(p.name for p in PACKAGE_STYLES_DIR.glob("*.md"))
        assert style_files, "packages/image-generation/config/styles/*.md が見つかりません"
        for name in style_files:
            expected_entry = f"config/styles/{name}"
            assert expected_entry in MANIFEST["config"], (
                f"{expected_entry} が manifest.json の config リストに列挙されていません"
            )

    def test_every_manifest_style_entry_exists_on_disk(self) -> None:
        manifest_style_entries = [
            entry for entry in MANIFEST["config"] if entry.startswith("config/styles/")
        ]
        assert manifest_style_entries, "manifest.json に config/styles/ エントリがありません"
        for entry in manifest_style_entries:
            style_path = REPO_ROOT / "packages" / "image-generation" / entry
            assert style_path.is_file(), (
                f"manifest.json に列挙された {entry} が実ファイルとして存在しません"
            )


# ---------------------------------------------------------------------------
# EV-21: style ファイルの安全なプロンプト埋め込み
# ---------------------------------------------------------------------------


class TestStylePromptEmbedding:
    """EV-21: agent は style 欠落を fail-fast し、literal block として埋め込む。"""

    def test_configuration_resolves_exact_style_file(self) -> None:
        assert ".claude/config/image-generation/styles/<name>.md" in CONFIGURATION
        assert r"^[a-z0-9][a-z0-9-]*$" in CONFIGURATION

    def test_missing_style_is_a_caller_bug_and_failure(self) -> None:
        assert re.search(r"caller\s+bug", CONFIGURATION)
        assert "report FAILURE" in CONFIGURATION
        assert "Never silently generate without the requested style" in CONFIGURATION

    def test_style_uses_a_quoted_heredoc(self) -> None:
        assert "STYLE_TEXT=$(cat <<'STYLE_EOF'" in STEP2
        assert "not translate it" in STEP2
        assert "unquoted heredocs" in STEP2

    def test_full_prompt_contains_bounded_style_block(self) -> None:
        full_prompt = _extract_full_prompt_body(STEP2)
        assert "${STYLE_BLOCK}" in full_prompt
        assert "BEGIN STYLE DEFINITION" in STEP2

    def test_none_style_has_an_explicit_code_branch(self) -> None:
        """レビュー修正 1: none 分岐がコード例に明示され、STYLE_BLOCK が空になる。"""
        assert 'if [ "$STYLE" = "none" ]' in STEP2
        assert 'STYLE_TEXT=""' in STEP2
        assert 'STYLE_BLOCK=""' in STEP2

    def test_style_name_is_mechanically_validated_before_reading_file(self) -> None:
        """レビュー修正 3: STYLE_EOF heredoc の前に regex + 実在チェックのゲートがある。"""
        assert r"grep -Eq '^[a-z0-9][a-z0-9-]*$'" in STEP2
        assert 'STYLE_FILE="$STYLES_DIR/$STYLE.md"' in STEP2
        assert '[ -f "$STYLE_FILE" ]' in STEP2

        gate_match = re.search(r"grep -Eq '\^\[a-z0-9\]\[a-z0-9-\]\*\$'", STEP2)
        heredoc_match = re.search(r"STYLE_TEXT=\$\(cat <<'STYLE_EOF'", STEP2)
        assert gate_match and heredoc_match
        assert gate_match.start() < heredoc_match.start(), (
            "検証ゲートは STYLE_EOF heredoc より前に実行される必要があります"
        )

    def test_style_eof_delimiter_collision_is_detected_mechanically(self) -> None:
        """レビュー修正 4: デリミタ衝突を grep で機械検出し FAILURE を報告する。"""
        assert "grep -qx 'STYLE_EOF' \"$STYLE_FILE\"" in STEP2
        assert "contains the heredoc delimiter line" in STEP2
        assert "END STYLE DEFINITION" in STEP2

    def test_output_format_reports_applied_style(self) -> None:
        assert "{style name / none}" in OUTPUT_FORMAT


# ---------------------------------------------------------------------------
# EV-13: 非対話完結性（stdin 封じ・成否判定）
# ---------------------------------------------------------------------------


class TestNonInteractiveExecution:
    """EV-13: codex exec に < /dev/null が付き、成否をマーカー/検証で判定する。

    Bash `timeout` はツール呼び出しメタデータであり、指示書の文言があっても実行時に
    強制されるとは限らない（ハーネス側の挙動に依存）。ここでは「指示書に 180000 への
    言及があるか」のみを確認し、実行時に適用されることまでは保証しない。
    """

    def test_codex_exec_invocation_has_stdin_redirect(self) -> None:
        invocation_match = re.search(r"^codex exec\b", STEP3, re.MULTILINE)
        assert invocation_match, "Step 3 内に codex exec の実際の呼び出し行が見つかりません"
        # `< /dev/null` は複数行コマンドの末尾（プロンプト引数の直後）に付与される
        assert "< /dev/null" in STEP3[invocation_match.start() :]

    def test_timeout_180000_mentioned_in_step3(self) -> None:
        assert "180000" in STEP3

    def test_success_failure_determined_by_marker_and_verification(self) -> None:
        """成否判定はコマンドの exit code 単体ではなく、フレッシュネスマーカー
        （Step 3.5 / EV-05）と出力検証（Step 4 / EV-06）の組み合わせで行われる。"""
        assert "exit 1" in STEP3_5
        assert "FAILURE" in STEP4 or "SUCCESS" in STEP4


# ---------------------------------------------------------------------------
# EV-14: Codex 利用不能時に自前描画をしない
# ---------------------------------------------------------------------------


class TestNoSelfDrawnFallback:
    """EV-14: Codex 利用不能時に Pillow 等の自前描画へフォールバックしない。"""

    def test_fallback_section_forbids_placeholder_drawing(self) -> None:
        assert "do NOT draw a placeholder" in FALLBACK

    def test_fallback_section_reports_unavailable(self) -> None:
        assert "unavailable" in FALLBACK.lower()


# ---------------------------------------------------------------------------
# facets/instructions/image-gen.md 側の EV-16
# ---------------------------------------------------------------------------


class TestSkillMdKillSwitchReference:
    """EV-16: 委譲前の Step 0 / codex.enabled kill-switch への言及。"""

    def test_phase2_references_step0_before_delegation(self) -> None:
        assert "Step 0" in SKILL_PHASE2
        assert "codex.enabled" in SKILL_PHASE2

    def test_phase2_mentions_unavailable_report(self) -> None:
        assert "利用不可" in SKILL_PHASE2

    def test_unavailable_condition_includes_codex_enabled_false(self) -> None:
        assert "codex.enabled: false" in SKILL_PHASE3
        assert "利用不可" in SKILL_PHASE3
