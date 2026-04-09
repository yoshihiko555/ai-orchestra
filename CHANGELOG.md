# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `/handoff` スキル: Claude Code のレート制限時に Codex CLI へタスクを引き継ぐ指示書ファイルを生成
- `/pr-create` スキル: 現在のブランチから PR を作成（テンプレート自動生成・ラベル自動決定）
- `pr-standards` ポリシー: PR 作成ルールを `pr-create` と `issue-fix` で共通化

### Changed

- `issue-workflow` パッケージを `git-workflow` に改名（責務拡大に伴う名称整理）
- `issue-fix` の PR 作成ロジックを PR Standards Policy 参照に簡素化

## [0.2.3] - 2026-03-30

### Added

### Changed

### Fixed

<!-- release 時は Unreleased の内容を次のような version セクションへ確定する -->
<!-- ## [0.1.0] - YYYY-MM-DD -->
