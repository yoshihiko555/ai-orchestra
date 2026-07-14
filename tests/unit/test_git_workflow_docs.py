"""`packages/git-workflow` の 3 スキル（プロンプト文書）+ PR Standards Policy のテスト。

`issue-create` / `issue-fix` / `pr-create` は実行コードを持たないプロンプト文書のため、
`docs/evaluation/git-workflow.md`（Issue #132）の指示に従い以下の 2 種類でテストする:

1. **決定表のテーブル駆動テスト**（対応表を厳密に全行検証）
   - EV-07 / EV-08: `facets/policies/pr-standards.md` のブランチプレフィックス→
     タイトルプレフィックス→ラベル対応表（9 行）
   - EV-13: `facets/instructions/issue-fix.md` の Issue ラベル→ブランチプレフィックス
     対応表（4 行）
2. **文書から抽出した実際の bash 片の実行検証**（純粋な文言一致より一段強い検証）
   - EV-03: `AI_ORCHESTRA_DIR` 未設定ガード（`pr-create.md` / `issue-fix.md` 双方）
   - EV-11: worktree 判定に使う `git rev-parse --git-dir` / `--git-common-dir` 比較
   - EV-16: `gh label create ... || true` の「失敗しても続行」動作

残りの EV（EV-01, EV-04〜EV-06, EV-09, EV-10, EV-12, EV-14, EV-15, EV-17〜EV-19）は
実行コードを持たないため、文書内の該当記述が存在し続けることを保証する
「文書とテストの突合チェック」として実装する（ドキュメント drift の検出が目的で、
実行時の振る舞い保証ではない点に注意）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACETS_INSTRUCTIONS = REPO_ROOT / "facets" / "instructions"
FACETS_POLICIES = REPO_ROOT / "facets" / "policies"
RESOLVER_SCRIPT = REPO_ROOT / "packages" / "git-workflow" / "scripts" / "resolve_base_branch.py"

PR_CREATE = (FACETS_INSTRUCTIONS / "pr-create.md").read_text(encoding="utf-8")
ISSUE_FIX = (FACETS_INSTRUCTIONS / "issue-fix.md").read_text(encoding="utf-8")
ISSUE_CREATE = (FACETS_INSTRUCTIONS / "issue-create.md").read_text(encoding="utf-8")
PR_STANDARDS = (FACETS_POLICIES / "pr-standards.md").read_text(encoding="utf-8")


def _load_resolver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("resolve_base_branch", RESOLVER_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load_resolver()


def _extract_table_rows(content: str, header_marker: str) -> list[list[str]]:
    """`header_marker` を含む見出し行から始まる Markdown テーブルの本体行を抽出する。

    ヘッダー行・区切り行（`---`）はスキップし、セルの前後空白と ` を除去して返す。
    """
    lines = content.splitlines()
    start = next(i for i, line in enumerate(lines) if header_marker in line)
    rows: list[list[str]] = []
    for line in lines[start + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [cell.strip().strip("`") for cell in stripped.strip("|").split("|")]
        rows.append(cells)
    return rows


def _extract_line(content: str, needle: str) -> str:
    for line in content.splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"line containing {needle!r} not found")


# ============================================================
# EV-07 / EV-08 (must): ブランチプレフィックス→タイトルプレフィックス→ラベル対応表
# ============================================================

BRANCH_PREFIX_LABEL_TABLE = [
    ("fix/", "fix:", "bug"),
    ("feat/", "feat:", "enhancement"),
    ("docs/", "docs:", "documentation"),
    ("chore/", "chore:", "task"),
    ("refactor/", "refactor:", "refactor"),
    ("test/", "test:", "task"),
    ("task/", "chore:", "task"),
    ("release/", "release:", "task"),
    ("その他", "chore:", "task"),
]


@pytest.mark.parametrize(("prefix", "title_prefix", "label"), BRANCH_PREFIX_LABEL_TABLE)
def test_pr_standards_branch_prefix_label_row(prefix: str, title_prefix: str, label: str) -> None:
    """EV-07 / EV-08: 対応表の各行が期待どおりであることを 1 行ずつ検証する。"""
    rows = _extract_table_rows(PR_STANDARDS, "ブランチプレフィックス | PR タイトルプレフィックス")
    assert [prefix, title_prefix, label] in rows


def test_pr_standards_branch_prefix_label_table_has_no_extra_or_missing_rows() -> None:
    """EV-07 / EV-08: 対応表がちょうど 9 行（想定した行以外を含まない）であることを保証する。"""
    rows = _extract_table_rows(PR_STANDARDS, "ブランチプレフィックス | PR タイトルプレフィックス")
    expected = [list(row) for row in BRANCH_PREFIX_LABEL_TABLE]
    assert rows == expected


# ============================================================
# EV-13 (must): issue-fix の Issue ラベル→ブランチプレフィックス対応表
# ============================================================

ISSUE_LABEL_PREFIX_TABLE = [
    ("bug", "fix/"),
    ("feature", "feat/"),
    ("task", "chore/"),
    ("その他", "fix/"),
]


@pytest.mark.parametrize(("label", "prefix"), ISSUE_LABEL_PREFIX_TABLE)
def test_issue_fix_label_prefix_row(label: str, prefix: str) -> None:
    """EV-13: Issue ラベル→ブランチプレフィックス対応表の各行を検証する。"""
    rows = _extract_table_rows(ISSUE_FIX, "ラベル  | プレフィックス")
    matching = [row for row in rows if row[0] == label]
    assert matching, f"label {label!r} not found in table"
    assert matching[0][1] == prefix


def test_issue_fix_label_prefix_table_has_no_extra_or_missing_rows() -> None:
    """EV-13: 対応表がちょうど 4 行であることを保証する（テーブル駆動の網羅性担保）。"""
    rows = _extract_table_rows(ISSUE_FIX, "ラベル  | プレフィックス")
    labels_and_prefixes = [(row[0], row[1]) for row in rows]
    assert labels_and_prefixes == ISSUE_LABEL_PREFIX_TABLE


# ============================================================
# EV-16 (must): issue-create のラベル種別網羅 + 失敗しても続行する挙動
# ============================================================


def _extract_gh_label_create_lines(content: str) -> list[str]:
    return [
        line.strip() for line in content.splitlines() if line.strip().startswith("gh label create")
    ]


def test_issue_create_label_creation_covers_all_three_types() -> None:
    """EV-16: bug/feature/task の 3 種別すべてに `gh label create` があることを検証する。"""
    lines = _extract_gh_label_create_lines(ISSUE_CREATE)
    labels = {
        match.group(1) for line in lines if (match := re.search(r'gh label create "([^"]+)"', line))
    }
    assert labels == {"bug", "feature", "task"}


def test_issue_create_label_creation_continues_on_failure(tmp_path: Path) -> None:
    """EV-16: ラベル作成コマンドが失敗（権限不足等）しても後続処理が継続することを、
    ドキュメントから抽出した実際の bash 片を、常に失敗するダミー `gh` を PATH に置いて実行し検証する。
    """
    lines = _extract_gh_label_create_lines(ISSUE_CREATE)
    assert lines, "gh label create lines not found in issue-create.md"
    assert all(line.endswith("|| true") for line in lines), (
        "gh label create lines must fall back to `|| true` so failures do not abort Issue creation"
    )

    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    script = "\n".join(lines) + "\necho SCRIPT_CONTINUED"
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "SCRIPT_CONTINUED" in result.stdout


# ============================================================
# EV-03 (must): AI_ORCHESTRA_DIR 未設定ガード（pr-create.md / issue-fix.md）
# ============================================================

GUARD_PATTERN = re.compile(r': "\$\{AI_ORCHESTRA_DIR:\?[^}]*\}"')


def _guard_lines(content: str) -> list[str]:
    return GUARD_PATTERN.findall(content)


@pytest.mark.parametrize(
    ("doc_name", "content"),
    [
        ("pr-create.md", PR_CREATE),
        ("issue-fix.md", ISSUE_FIX),
    ],
)
def test_ai_orchestra_dir_guard_fails_fast_when_unset(doc_name: str, content: str) -> None:
    """EV-03: `AI_ORCHESTRA_DIR` 未設定時、resolver 呼び出し前のガードが即座に失敗することを、
    ドキュメントから抽出した実際のガード行を実行して検証する（gh pr create --base "" を防ぐ前段）。
    """
    guards = _guard_lines(content)
    assert guards, f"AI_ORCHESTRA_DIR guard not found in {doc_name}"

    env = {k: v for k, v in os.environ.items() if k != "AI_ORCHESTRA_DIR"}
    result = subprocess.run(
        ["bash", "-c", f"{guards[0]}\necho AFTER_GUARD"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "AFTER_GUARD" not in result.stdout


@pytest.mark.parametrize(
    ("doc_name", "content"),
    [
        ("pr-create.md", PR_CREATE),
        ("issue-fix.md", ISSUE_FIX),
    ],
)
def test_ai_orchestra_dir_guard_passes_when_set(doc_name: str, content: str) -> None:
    """EV-03: `AI_ORCHESTRA_DIR` 設定時はガードを通過し、後続の resolver 呼び出しに進むことを検証する。"""
    guards = _guard_lines(content)
    assert guards, f"AI_ORCHESTRA_DIR guard not found in {doc_name}"

    env = {**os.environ, "AI_ORCHESTRA_DIR": str(REPO_ROOT)}
    result = subprocess.run(
        ["bash", "-c", f"{guards[0]}\necho AFTER_GUARD"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "AFTER_GUARD" in result.stdout


def test_issue_fix_has_guard_at_both_branch_prep_and_pr_create_steps() -> None:
    """EV-03: issue-fix.md は Phase 2-1（ブランチ準備判定）と Phase 4-6（PR 作成）の
    両方でガードを踏んでから resolver を呼ぶ（片方だけ守られていないドキュメント drift を検出する）。
    """
    guards = _guard_lines(ISSUE_FIX)
    assert len(guards) >= 2


# ============================================================
# EV-11 (must): worktree 判定に使う実際の git コマンド比較
# ============================================================

_GIT_DIR_LINE = _extract_line(ISSUE_FIX, "GIT_DIR=$(git rev-parse --git-dir)")
_GIT_COMMON_DIR_LINE = _extract_line(ISSUE_FIX, "GIT_COMMON_DIR=$(git rev-parse --git-common-dir)")
_CURRENT_BRANCH_LINE = _extract_line(ISSUE_FIX, "CURRENT_BRANCH=$(git branch --show-current)")

_WORKTREE_DETECTION_SCRIPT = "\n".join(
    [
        _GIT_DIR_LINE,
        _GIT_COMMON_DIR_LINE,
        _CURRENT_BRANCH_LINE,
        'echo "$GIT_DIR|$GIT_COMMON_DIR|$CURRENT_BRANCH"',
    ]
)


def _git(args: list[str], cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


def _write_commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(["add", filename], repo)
    _git(["commit", "-q", "-m", message], repo)


def _run_worktree_detection(cwd: Path) -> tuple[str, str, str]:
    result = subprocess.run(
        ["bash", "-c", _WORKTREE_DETECTION_SCRIPT],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    git_dir, git_common_dir, current_branch = result.stdout.strip().split("|")
    return git_dir, git_common_dir, current_branch


def test_worktree_detection_git_dir_equals_common_dir_on_base_branch(tmp_path: Path) -> None:
    """EV-11: 通常の（非 worktree）リポジトリで base branch 上にいる場合、
    `git rev-parse --git-dir` と `--git-common-dir` が一致する（「未準備」判定の前提）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=main", "--template="], repo)
    _write_commit(repo, "README.md", "# test\n", "init")

    git_dir, git_common_dir, current_branch = _run_worktree_detection(repo)
    assert git_dir == git_common_dir
    assert current_branch == "main"


def test_worktree_detection_git_dir_differs_from_common_dir_in_worktree(tmp_path: Path) -> None:
    """EV-11: worktree 内で実行している場合、`git rev-parse --git-dir` と
    `--git-common-dir` が異なる（「準備済み」判定の最も確実なシグナル）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "--initial-branch=main", "--template="], repo)
    _write_commit(repo, "README.md", "# test\n", "init")

    worktree_path = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "fix/issue-1-example", str(worktree_path)], repo)

    git_dir, git_common_dir, current_branch = _run_worktree_detection(worktree_path)
    assert git_dir != git_common_dir
    assert current_branch == "fix/issue-1-example"


# ============================================================
# 以下は実行コードを持たないプロンプト文書の「記述が残っているか」を保証する
# 文書とテストの突合チェック（ドキュメント drift 検出。振る舞いの実行検証ではない）。
# ============================================================


def _fenced_code_blocks(content: str) -> list[str]:
    return re.findall(r"```(?:bash)?\n(.*?)```", content, flags=re.DOTALL)


def test_pr_create_propagates_resolved_base_to_diff_collection_and_pr_creation() -> None:
    """EV-01 (must, partial): 差分収集・`gh pr create` のすべてが解決済み `$BASE` を
    参照している（ハードコードされた固定ブランチ名に戻っていない）ことを検証する。
    `gh pr create` は複数行コマンド（`\\` 継続）のためコードブロック単位で判定する。
    """
    blocks = _fenced_code_blocks(PR_CREATE)
    relevant_blocks = [
        block
        for block in blocks
        if re.search(r"git log --oneline|git diff --stat|gh pr create", block)
    ]
    assert relevant_blocks, "expected $BASE-consuming code blocks not found in pr-create.md"
    for block in relevant_blocks:
        assert "$BASE" in block, f"code block does not reference resolved $BASE: {block!r}"


def test_pr_create_errors_when_base_equals_current_branch() -> None:
    """EV-04 (must): base branch 上での実行はエラー終了する旨の記述を保証する。"""
    assert (
        "解決済み `$BASE` と `$BRANCH` が一致する場合（= base branch 上で実行された場合）は"
        "エラーで終了する（PR 作成対象のブランチに移動するよう案内）。" in PR_CREATE
    )


def test_pr_create_errors_when_no_commits() -> None:
    """EV-05 (must): `$BASE..HEAD` のコミットが 0 件の場合はエラー終了する旨の記述を保証する。"""
    assert "コミットが 0 件の場合はエラーで終了する。" in PR_CREATE


def test_pr_create_template_priority_order() -> None:
    """EV-06 (must): PR テンプレートの優先順位（ローカル > community profile > フォールバック）
    の記述順が保たれていることを検証する。
    """
    local_idx = PR_CREATE.index(".github/PULL_REQUEST_TEMPLATE.md")
    community_idx = PR_CREATE.index("community/profile")
    fallback_idx = PR_CREATE.index(
        "テンプレートが見つからない場合は PR Standards Policy のフォールバックテンプレートを使用する"
    )
    assert local_idx < community_idx < fallback_idx


def test_pr_create_existing_pr_ask_user_question_choices() -> None:
    """EV-09 (should): 既存 open PR がある場合の選択肢が両方存在することを検証する。"""
    assert "既存 PR を開く" in PR_CREATE
    assert "新規 PR を作成" in PR_CREATE


def test_pr_create_adds_closes_issue_reference() -> None:
    """EV-10 (must): `--issue` 指定時に PR 本文冒頭へ `Closes #{番号}` を追加する記述を保証する。"""
    assert "Issue がある場合、本文冒頭に `Closes #{番号}` を追加する。" in PR_CREATE


def test_issue_fix_safety_fallback_branch_list_matches_resolver_candidates() -> None:
    """EV-12 (should): `$BASE` 解決失敗時の安全側判断で列挙される統合ブランチ一覧が、
    resolver 側の `CANDIDATES` と同じ集合であることを検証する（片方だけ更新される drift を検出）。
    """
    line = _extract_line(ISSUE_FIX, "安全側の判断")
    match = re.search(r"現在ブランチが ((?:`[a-z]+`\s*/\s*)+`[a-z]+`)", line)
    assert match, f"branch list not found in safety fallback line: {line!r}"
    branch_tokens = re.findall(r"`([a-z]+)`", match.group(1))
    assert set(branch_tokens) == set(resolver.CANDIDATES)


def test_issue_fix_critical_review_findings_require_returning_to_phase2() -> None:
    """EV-14 (must): レビュー Critical 指摘は必ず Phase 2 に戻って修正する記述を保証する。"""
    assert "**Critical**: Phase 2 に戻り修正する（必須）" in ISSUE_FIX


def test_issue_fix_plan_approval_gate_precedes_phase2() -> None:
    """EV-15 (should): Phase 1-4 の承認ゲートが Phase 2（実装）の記述より前に置かれていることを検証する。"""
    approval_idx = ISSUE_FIX.index("承認されなければ修正または中止する。")
    phase2_idx = ISSUE_FIX.index("### Phase 2: 実装")
    assert approval_idx < phase2_idx


def test_issue_fix_task_prompt_respects_cli_tools_routing() -> None:
    """EV-18 (should): 実装委譲の Task プロンプトに cli-tools.yaml ルーティング尊重指示を含める。"""
    assert "IMPORTANT: cli-tools.yaml の設定に従い実装すること。" in ISSUE_FIX


def test_issue_fix_new_branch_naming_rule() -> None:
    """EV-19 (should): 新規ブランチ名の slug 規約（英語 kebab-case、最大 30 文字）を検証する。"""
    assert "`{slug}` は Issue タイトルから英語 kebab-case で生成（最大 30 文字）" in ISSUE_FIX
    assert "git checkout -b {prefix}issue-{番号}-{slug}" in ISSUE_FIX


@pytest.mark.parametrize(
    ("doc_name", "content", "min_count"),
    [
        ("issue-create.md", ISSUE_CREATE, 2),
        ("issue-fix.md", ISSUE_FIX, 3),
        ("pr-create.md", PR_CREATE, 2),
    ],
)
def test_ask_user_question_used_at_decision_points(
    doc_name: str, content: str, min_count: int
) -> None:
    """EV-17 (must): 重要な意思決定点で `AskUserQuestion` を使用していることを最低出現数で保証する
    （厳密な網羅は手動チェックリスト化が望ましいとの Issue #132 の指摘のとおり、ここでは
    ドキュメントから `AskUserQuestion` 記述が失われる drift のみを検出する）。
    """
    assert content.count("AskUserQuestion") >= min_count, (
        f"{doc_name} has fewer AskUserQuestion mentions than expected decision points"
    )
