# init-orchestra

<init-orchestra>

このプロジェクトで AI Orchestra を有効化し、必要な設定ファイルを配置します。

## 実行手順

### 1. 前提チェック

以下を確認:
- `AI_ORCHESTRA_DIR` 環境変数が設定されているか（`~/.claude/settings.json` の `env`）
  - 未設定 → orchestra-manager.py の install が自動設定するため、先にインストールを案内
- `$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py` が存在するか

### 2. パッケージのインストール

orchestra-manager.py を使って必要なパッケージをインストール:

```bash
# 基本パッケージ（全プロジェクト共通）
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install core --project .
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install tmux-monitor --project .
```

orchestra-manager が内部で以下を自動実行:
1. `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` を設定（初回のみ）
2. `.claude/orchestra.json` にパッケージ情報を記録
3. `.claude/settings.local.json` に hooks を登録（`$AI_ORCHESTRA_DIR/packages/...` 参照）
4. `sync-orchestra.py` の SessionStart hook を登録（初回のみ）
5. skills/agents/rules の初回同期を実行

### 3. オプションパッケージの確認

ユーザーに追加パッケージを提案:

```bash
# パッケージ一覧を確認
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" list
```

| パッケージ | 説明 | 推奨 |
|-----------|------|------|
| core | 共通基盤ライブラリ | 必須 |
| tmux-monitor | tmux サブエージェント監視 | 推奨 |
| cli-logging | Codex/Gemini ログ記録 | オプション |
| codex-suggestions | ファイル編集時の Codex 相談提案 | オプション |
| gemini-suggestions | Web検索時の Gemini リサーチ提案 | オプション |
| quality-gates | 実装後レビュー・テスト分析・自動 lint | オプション |
| route-audit | 期待ルート予測とルーティング監査 | オプション |

`AskUserQuestion` で追加パッケージの選択をユーザーに確認し、選択されたものをインストール。

### 4. CLAUDE.md の作成（条件付き）

**既存の CLAUDE.md がある場合: 何もしない**（プロジェクト固有情報を尊重）

**存在しない場合のみ:** `$AI_ORCHESTRA_DIR/templates/project/CLAUDE.md` のテンプレートを配置

### 5. .claude/docs/ と .claude/logs/ と .claude/checkpoints/ の作成

```
.claude/
├── docs/
│   ├── DESIGN.md              # 既存がなければテンプレート作成
│   ├── research/.gitkeep      # 常に作成
│   └── libraries/_TEMPLATE.md # 既存がなければテンプレート作成
├── logs/
│   └── .gitkeep               # 常に作成（Codex/Gemini ログ用）
└── checkpoints/
    └── .gitkeep               # 常に作成（チェックポイント保存用）
```

### 6. .codex/ の作成

```
.codex/
├── config.toml
├── AGENTS.md
└── skills/context-loader/SKILL.md
```

`$AI_ORCHESTRA_DIR/templates/codex/` の内容をコピー

### 7. .gemini/ の作成

```
.gemini/
├── settings.json
├── GEMINI.md
└── skills/context-loader/SKILL.md
```

`$AI_ORCHESTRA_DIR/templates/gemini/` の内容をコピー

### 8. 完了レポート

作成・スキップしたファイルを報告:

```
## Orchestra 有効化完了

### インストールしたパッケージ:
- core
- tmux-monitor
- ...

### 作成したファイル:
- .claude/settings.local.json（hooks 登録）
- .claude/orchestra.json（パッケージ情報）
- .claude/docs/DESIGN.md
- .claude/logs/.gitkeep
- .codex/config.toml
- ...

### スキップしたファイル（既存）:
- CLAUDE.md (プロジェクト固有設定を維持)
- ...

### 次のステップ:
1. CLAUDE.md にプロジェクト固有の情報を追加
2. .claude/docs/DESIGN.md に設計決定を記録
3. 開発を開始
```

## 重要な注意事項

- **CLAUDE.md は既存があれば触らない**（プロジェクト固有情報を尊重）
- オーケストラの使い方は `$AI_ORCHESTRA_DIR/rules/orchestra-usage.md` から自動読み込み
- Codex/Gemini 委譲ルールも `$AI_ORCHESTRA_DIR/rules/` から自動読み込み
- プロジェクト固有の Tech Stack, テストコマンド等は `CLAUDE.md` に記載

## パッケージと Hook の対応

| パッケージ | Hook イベント | 動作 |
|-----------|-------------|------|
| tmux-monitor | SessionStart | tmux 監視セッション作成 |
| tmux-monitor | SessionEnd | tmux セッション削除 |
| tmux-monitor | SubagentStart | tmux ペイン追加 |
| tmux-monitor | SubagentStop | ペイン完了通知 |
| cli-logging | PostToolUse (Bash) | Codex/Gemini ログ記録 |
| codex-suggestions | PreToolUse (Edit\|Write) | 設計ファイル編集時に Codex 提案 |
| codex-suggestions | PostToolUse (Task) | Plan 後に Codex レビュー提案 |
| gemini-suggestions | PreToolUse (WebSearch\|WebFetch) | リサーチ時に Gemini 提案 |
| quality-gates | PostToolUse (Edit\|Write) | 大量変更後にレビュー提案、lint 実行 |
| quality-gates | PostToolUse (Bash) | テスト失敗時に Codex デバッグ提案 |
| route-audit | SessionStart | オーケストレーション初期化 |
| route-audit | UserPromptSubmit | 期待ルート予測、ルーティング提案 |
| route-audit | PostToolUse | ルーティング監査 |

</init-orchestra>
