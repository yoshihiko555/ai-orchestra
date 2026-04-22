# Architecture Decision Records

AI Orchestra プロジェクトの意思決定記録。

## 運用方針

- 重要な設計・技術判断を ADR として記録する
- ファイル名: `ADR-NNNN-<slug>.md`（例: `ADR-0001-package-based-architecture.md`）
- テンプレート: [`_template.md`](./_template.md) を使用
- ステータス: `proposed` → `accepted` / `rejected` / `superseded`

## 一覧

| # | タイトル | ステータス | 日付 |
|---|---------|-----------|------|
| ADR-20260216-001 | docs ディレクトリのカテゴリ分類と ADR 導入 | accepted | 2026-02-16 |
| ADR-20260219-002 | startproject 実装フェーズの明示化と Codex 例外の厳格化 | accepted | 2026-02-19 |
| ADR-20260223-003 | Codex CLI 実装委譲のデッドロック解消と Implementation Method 強制 | accepted | 2026-02-23 |
| ADR-20260223-004 | task-memory サマリー表示仕様の明確化と marker 設定の堅牢化 | accepted | 2026-02-23 |
| ADR-20260223-005 | Codex/Gemini 運用記述の config-driven 統一 | accepted | 2026-02-23 |
| ADR-20260302-006 | cocoindex v2: mcp-proxy による MCP 共有化とポート自動導出 | accepted | 2026-03-02 |
| ADR-20260307-007 | cocoindex proxy モードのデフォルト設定維持（stdio） | accepted | 2026-03-07 |
| ADR-20260308-008 | サブエージェント model の config-driven 自動パッチ | accepted | 2026-03-08 |
| ADR-20260313-009 | CLI 間コンテキスト共有のファイルベース設計 | accepted | 2026-03-13 |
| ADR-20260315-010 | Faceted Prompting によるスキル・ルールのファセット分解と自動生成 | accepted | 2026-03-15 |
| ADR-20260322-011 | manifest-SSOT アーキテクチャ（facets 正本化・パッケージ skills 廃止） | accepted | 2026-03-22 |
| ADR-20260322-012 | facet build 完全委譲（rules 廃止・knowledge/scripts 層導入） | accepted | 2026-03-22 |
| ADR-20260409-013 | PR 作成スキル追加と issue-workflow パッケージの git-workflow への改名 | accepted | 2026-04-09 |
| ADR-20260412-014 | quality-gates に独立したテスト改ざん検出 Hook を追加 | accepted | 2026-04-12 |
| ADR-20260414-015 | agent-routing の未分類リサーチ入力は researcher 基点で解決する | accepted | 2026-04-14 |
| ADR-20260419-016 | quality gate の判定は quality-gates が担い、audit は記録と集計に限定する | accepted | 2026-04-19 |
| ADR-20260421-017 | cocoindex proxy 起動は proxy-only とし、state file と reconnect 通知で扱う | accepted | 2026-04-21 |
| ADR-20260423-018 | cocoindex proxy 停止は supervisor の idle shutdown で扱う | accepted | 2026-04-23 |
