# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- `audit/scripts/dashboard-html.py`: `-o` 未指定時のデフォルト出力先を `.claude/YYYYMMDD-dashboard.html` に変更。`-o -` で stdout 出力をサポート
- `orchex scripts`: スクリプト一覧に説明（description）カラムと使い方ヒントを追加
- `packages/audit/manifest.json`: scripts エントリを `{path, description}` オブジェクト形式に拡張（文字列形式との後方互換あり）

### Added

- `scripts/lib/orchestra_models.py`: `ScriptEntry` データクラスを追加（manifest の scripts 値を型安全に扱う）
- `packages/audit/README.md`: audit パッケージの使い方ドキュメントを追加
- `packages/git-workflow/scripts/resolve_base_branch.py`: PR の base branch を `--base` 明示指定 > 環境変数 `AI_ORCHESTRA_BASE_BRANCH` > merge-base 自動推定 > fallback (`main`) の優先順で解決する CLI を追加。`main` + `stage` 等の多段ブランチ運用に対応 (#63)

### Changed

- `pr-create` / `issue-fix` / `pr-standards`: PR 作成時の base branch を `main` 固定から resolver スクリプト経由の解決に切り替え。`/pr-create --base <branch>` で明示指定可能 (#63)

### Fixed

- `audit/hooks/event_logger.py`: worktree 環境でログが分散する問題を修正。全 worktree のログを root worktree の `.claude/logs/audit/` に集約するようにした

## [0.2.6] - 2026-04-13

### Added

- `audit/scripts/dashboard-html.py`: 既存ログを横断集計し Chart.js で可視化する HTML ダッシュボード生成スクリプトを追加 (#31)
- `audit/scripts/dashboard_stats.py`: テキスト / HTML 両ダッシュボードで共有する集計ロジックモジュールを新設
- `quality-gates/test-tampering-detector.py`: PostToolUse で `it.skip()` / `@pytest.mark.skip` / `eslint-disable` / `noqa` / `type: ignore` の追加と、`rm` / `git rm` によるテストファイル削除を検出して警告する品質ゲートを追加

## [0.2.5] - 2026-04-12

### Changed

- `pr-standards` ポリシーのブランチプレフィックス→ラベル対応表を GitHub の実ラベル体系 (`bug` / `enhancement` / `documentation` / `refactor` / `task`) に合わせて更新。`gh pr create` がラベル未存在で失敗する問題を解消 (`facets/policies/pr-standards.md`、`pr-create` / `issue-fix` スキル再生成)
- `CONTEXT_SPECS` をパッケージ manifest の `context_files` から動的に構築するようリファクタ。`orchestra-manager.py` のハードコード定義を廃止し、`core` / `codex-suggestions` / `gemini-suggestions` の manifest に `source` / `template` キーを追加。`Package` dataclass に `context_files` フィールドを追加し、`init()` の hardcoded テンプレートコピーも init リストを SSOT とする whitelist 方式のデータ駆動ループに置換 (#45)

### Fixed

- `quality-gates/turn-end-summary.py` の Stop hook 出力が Claude Code のスキーマ違反（`hookSpecificOutput` は Stop では不可）となり `JSON validation failed` を起こしていた問題を修正。`systemMessage` フィールドに変更

## [0.2.4] - 2026-04-11

### Added

- `/handoff` スキル: Claude Code のレート制限時に Codex CLI へタスクを引き継ぐ指示書ファイルを生成
- `/pr-create` スキル: 現在のブランチから PR を作成（テンプレート自動生成・ラベル自動決定）
- `pr-standards` ポリシー: PR 作成ルールを `pr-create` と `issue-fix` で共通化
- `context_files` key in package manifests for context file ownership (#36)
- `required_package` field in CONTEXT_SPECS for data-driven distribution (#36)
- `escalation-strategy` ルール: コンテキスト節約のためのツール選択ガイドライン（Glob → Grep count → Grep files → Grep content → Read offset/limit の段階的絞り込み、判断基準、アンチパターン）を `core` パッケージに追加 (#9)
- 探索系サブエージェント定義にコンテキスト効率セクションを追加: `general-purpose`, `researcher`, `code-reviewer`, `debugger`, `architecture-reviewer` (#11)
- `audit` パッケージ: `route-audit` + `cli-logging` を統合した統一イベントログ監査基盤 (#38)
  - 統一スキーマ v1（`v`, `ts`, `sid`, `eid`, `type`, `tid`, `ptid`, `aid`, `ctx`, `data`）
  - セッション単位のログローテーション（`sessions/{session_id}.jsonl`）
  - 新規イベント: `session_start`, `session_end`, `subagent_start`, `subagent_end`
  - トレース ID によるプロンプト→ルーティング→ツール実行の呼び出しチェーン追跡
  - CLI 呼び出しのエラー分類（timeout, auth, rate_limit 等）と生レスポンス記録
- 新しい hook イベントへの対応（Claude Code の最新 hook API に合わせた拡張）
  - `core/precompact-dump.py`: PreCompact イベントで working-context と Plans.md を
    `.claude/context/shared/precompact-{timestamp}.md` に退避（圧縮前の重要情報退避）
  - `audit/audit-instructions-loaded.py`: InstructionsLoaded イベントで CLAUDE.md / ルール等の
    ロード状況（`load_reason`, `file_path`, `globs`）を audit v1 ログに記録
  - `quality-gates/turn-end-summary.py`: Stop イベントでターン終了サマリーを注入
    （編集ファイル数、Plans.md の WIP/TODO/blocked 件数、lint 未実行リマインダー）
  - audit 統一スキーマに `instructions_loaded`, `turn_end`, `precompact` イベント型を追加
- `quality-gates/check-context-optimization.py`: PreToolUse(Read|Grep|Bash) で非効率な
  ツール使用 (Read 全文読み・Grep content モード乱用・Bash の cat/grep/find 等) を検出し、
  エスカレーション戦略への切り替えを提案する Hook を追加 (#10)
  - `audit-flags.json` に `features.context_optimization` フラグを追加（閾値・無効化対応）

### Changed

- `issue-workflow` パッケージを `git-workflow` に改名（責務拡大に伴う名称整理）
- `issue-fix` の PR 作成ロジックを PR Standards Policy 参照に簡素化
- Context templates now use `<YOUR_...>` placeholders instead of ai-orchestra-specific content (#37)
- AGENTS.md / GEMINI.md distribution is now conditional on package install state (#36)
- `route-audit` + `cli-logging` を `audit` パッケージに統合（#38）
- `/design` スキル: 既存コードがあるプロジェクトでは Phase 0（既存コード調査と影響範囲分析）を必ず先行実施するよう変更。`researcher` サブエージェント経由で中粒度の影響範囲（直接変更対象／依存関係／リスク）を調査し、成果物を `.claude/docs/impact-analysis/{date}_{slug}.md` に出力する

### Fixed

- `quality-gates` の `lint-on-save.py` が、編集ファイルの種別に応じて formatter / linter を切り替えられるよう改善

## [0.2.3] - 2026-03-30

### Added

- `release-readiness` を強化し、`pyright` を導入。あわせて release workflow を追加 (#26)

### Fixed

- `inject-shared-context` の hook 出力フォーマットを修正 (#27)

## [0.2.2] - 2026-03-22

### Added

- `review` のレビュー自動修正ループ機能を追加 (#21)
- facet composition に Knowledge 層と Scripts を導入 (#24)

### Changed

- manifest-SSOT アーキテクチャへの移行に伴い、`packages/skills` を廃止 (#22)
- `packages/rules` を廃止し、facet build へ完全委譲する構成に整理 (#25)

## [0.2.1] - 2026-03-22

### Added

- ファセットシステムを導入し、E2E テストを追加 (#19)

### Changed

- モジュール分割を進め、ドキュメント体系を整理 (#19)

## [0.2.0] - 2026-03-14

### Added

- コンテキスト共有基盤と指示書テンプレート管理を導入 (#17)

### Changed

- `design-tracker` の運用乖離と migration guide の記載不整合を整理 (#16)

## [0.1.0] - 2026-03-06

### Added

- AI Orchestra の初期リリース
- Claude Code + Codex CLI + Gemini CLI のエージェントルーティング
- `Plans.md` による SSOT タスク管理
- PyPI パッケージ `orchex` として公開
- hook による自動品質ゲート
