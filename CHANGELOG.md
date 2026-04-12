# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

### Changed

### Fixed

<!-- release 時は Unreleased の内容を次のような version セクションへ確定する -->
<!-- ## [0.1.0] - YYYY-MM-DD -->
