# クイックスタートガイド

AI Orchestra を初めて使うユーザー向けのセットアップ手順。

---

## 前提条件

| ツール | 必須 | 備考 |
|--------|------|------|
| Python 3.12+ | 必須 | `orchex` CLI の実行に必要 |
| Claude Code | 必須 | オーケストレーター本体 |
| Codex CLI | 任意 | 深い推論（設計判断・デバッグ）に使用。未インストールでも動作する |
| Gemini CLI | 任意 | リサーチ・マルチモーダル処理に使用。未インストールでも動作する |

Codex / Gemini CLI が未インストールの場合、該当エージェントは自動的に Claude Code 自身（`claude-direct`）にフォールバックする。

---

## 1. インストール

```bash
# uv（推奨）
uv tool install orchex

# pip
pip install orchex

# pipx
pipx install orchex
```

インストール確認:

```bash
orchex --version
```

---

## 2. プロジェクトへのセットアップ

### プリセットで一括セットアップ（推奨）

```bash
# チームメンバー向け: 最低限のパッケージ（core, route-audit, quality-gates）
orchex setup essential --project /path/to/project

# 全パッケージを一括インストール
orchex setup all --project /path/to/project

# 事前確認（dry-run）
orchex setup essential --project /path/to/project --dry-run
```

### 個別インストール

```bash
orchex install core --project /path/to/project
orchex install agent-routing --project /path/to/project
```

### セットアップ完了の確認

```bash
# 導入状況を確認
orchex status --project /path/to/project
```

以下が揃っていればセットアップ完了:

- `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` が設定されている
- `.claude/settings.local.json` に AI Orchestra の hooks が登録されている
- `.claude/orchestra.json` が存在し、インストール済みパッケージが記録されている

次回 Claude Code を起動すると、SessionStart hook が走り `.claude/` 配下へ自動同期される。

---

## 3. 基本的な使い方

### エージェントの呼び出し

Claude Code の会話中に、タスクの種類に応じたキーワードを使うと `agent-router` hook が自動的にエージェントを提案する。

```
「この機能のタスクを分解して」     → planner が提案される
「セキュリティをレビューして」     → security-reviewer が提案される
「Python で API を実装して」       → backend-python-dev が提案される
「このライブラリについて調べて」   → researcher（Gemini）が提案される
```

手動でエージェントを指定することもできる:

```
Task(subagent_type="planner", prompt="ユーザー認証機能のタスクを分解して")
Task(subagent_type="architect", prompt="マイクロサービス構成を設計して")
```

### スキルの実行

```bash
/review              # スマート選定レビュー（変更内容に応じて 2-3 名を自動選定）
/review all          # 全 6 レビュアー並列実行
/review code         # コードレビューのみ
/tdd                 # テスト駆動開発ワークフロー
/startproject        # マルチエージェント協調で新規開発を開始
/simplify            # コードの簡素化
/preflight           # 実装計画の策定
```

### レビュー

変更を加えた後、レビュースキルでチェックできる:

```
/review              # 変更内容に応じてレビュアーを自動選定
/review impl         # 実装系（code + security + performance）
/review design       # 設計系（spec + architecture）
/release-readiness   # マージ前の最終チェック
```

---

## 4. 設定のカスタマイズ

AI Orchestra の設定はレイヤード構成で管理されている。ベース設定を `.local` ファイルで上書きできる。

### Codex / Gemini のモデルを変更

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  model: o3-pro
gemini:
  model: gemini-2.5-flash
```

### Codex / Gemini を無効化

CLI が未インストールの環境では、明示的に無効化できる:

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false
gemini:
  enabled: false
```

### 特定エージェントのルーティングを変更

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
agents:
  researcher:
    tool: claude-direct    # Gemini の代わりに Claude で処理
  debugger:
    tool: claude-direct    # Codex の代わりに Claude で処理
```

### サブエージェントのデフォルトモデルを変更

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
subagent:
  default_model: opus      # 全エージェントを opus にする
```

---

## 5. トラブルシューティング

### SessionStart hook が動かない

```bash
# orchestra.json の存在を確認
cat .claude/orchestra.json

# settings.local.json に hook が登録されているか確認
cat .claude/settings.local.json | grep sync-orchestra
```

### Codex / Gemini CLI がエラーになる

1. CLI が正しくインストールされているか確認: `codex --version` / `gemini --version`
2. `.local.yaml` で無効化して Claude にフォールバック:

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
codex:
  enabled: false
```

### パッケージの同期が反映されない

```bash
# 手動で同期を実行
orchex setup essential --project . --force
```

### orchex コマンドが見つからない

```bash
# パスを確認
which orchex

# uv の場合、ツールパスが通っているか確認
uv tool list
```

---

## 次のステップ

- [パッケージリファレンス](../reference/packages.md) — 各パッケージの詳細
- [設定リファレンス](../reference/configuration.md) — 全設定オプションの解説
- [Facet システム](facet-system.md) — スキル・ルールの自動生成の仕組み
- [Hook リファレンス](../reference/hooks.md) — 各フックの動作と設定
