# AI Orchestra

マルチエージェント協調フレームワーク。詳細は `README.md` を参照。

## 構成

packages/ 配下がメインのソースコード:
- core/              — 共通基盤（hooks・rules）
- agent-routing/     — エージェント定義・ルーティング hooks・config
- cli-logging/       — CLI ログ・checkpointing
- codex-suggestions/ — Codex 相談提案・rules
- gemini-suggestions/— Gemini リサーチ提案・rules
- quality-gates/     — 品質ゲート・review/tdd スキル
- route-audit/       — ルーティング監査
- tmux-monitor/      — tmux 監視

管理:
- scripts/           — orchestra-manager.py, sync-orchestra.py
- templates/         — テンプレート

## References

作業前に以下を確認すること（上から優先）:

1. `packages/agent-routing/config/cli-tools.yaml` — CLI モデル名・フラグ設定
2. `packages/agent-routing/rules/orchestra-usage.md` — エージェント・ワークフロー全体

## Gotchas

- `.claudeignore` は AI Orchestra が自動生成する。直接編集しないこと
  - プロジェクト固有パターンは `.claudeignore.local` に記載
  - SessionStart で ベース + `.claudeignore.local` がマージ生成される
- `packages/` 内の hooks は `$AI_ORCHESTRA_DIR` 経由で直接参照される（`git pull` で即反映）
- agents/skills/rules は SessionStart の `sync-orchestra.py` で `.claude/` に差分同期される
