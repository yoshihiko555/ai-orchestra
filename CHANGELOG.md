# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `/handoff` スキル: Claude Code のレート制限時に Codex CLI へタスクを引き継ぐ指示書ファイルを生成
- `/pr-create` スキル: 現在のブランチから PR を作成（テンプレート自動生成・ラベル自動決定）
- `pr-standards` ポリシー: PR 作成ルールを `pr-create` と `issue-fix` で共通化
- `context_files` key in package manifests for context file ownership (#36)
- `required_package` field in CONTEXT_SPECS for data-driven distribution (#36)
- `escalation-strategy` ルール: コンテキスト節約のためのツール選択ガイドライン（Glob → Grep count → Grep files → Grep content → Read offset/limit の段階的絞り込み、判断基準、アンチパターン）を `core` パッケージに追加 (#9)
- `audit` パッケージ: `route-audit` + `cli-logging` を統合した統一イベントログ監査基盤 (#38)
  - 統一スキーマ v1（`v`, `ts`, `sid`, `eid`, `type`, `tid`, `ptid`, `aid`, `ctx`, `data`）
  - セッション単位のログローテーション（`sessions/{session_id}.jsonl`）
  - 新規イベント: `session_start`, `session_end`, `subagent_start`, `subagent_end`
  - トレース ID によるプロンプト→ルーティング→ツール実行の呼び出しチェーン追跡
  - CLI 呼び出しのエラー分類（timeout, auth, rate_limit 等）と生レスポンス記録

### Changed

- `issue-workflow` パッケージを `git-workflow` に改名（責務拡大に伴う名称整理）
- `issue-fix` の PR 作成ロジックを PR Standards Policy 参照に簡素化
- Context templates now use `<YOUR_...>` placeholders instead of ai-orchestra-specific content (#37)
- AGENTS.md / GEMINI.md distribution is now conditional on package install state (#36)
- `route-audit` + `cli-logging` を `audit` パッケージに統合（#38）

### Fixed

- `quality-gates` の `lint-on-save.py` が、編集ファイルの種別に応じて formatter / linter を切り替えられるよう改善

## [0.2.3] - 2026-03-30

### Added

### Changed

### Fixed

<!-- release 時は Unreleased の内容を次のような version セクションへ確定する -->
<!-- ## [0.1.0] - YYYY-MM-DD -->
