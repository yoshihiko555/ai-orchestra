# AI Orchestra

Claude Code用のマルチエージェントオーケストレーションシステム

## 構成

```
ai-orchestra/
├── agents/           # 専門エージェント定義（25種）
├── packages/         # パッケージ（hooks・scripts・config）
│   ├── core/         # 共通基盤ライブラリ
│   ├── tmux-monitor/ # tmux サブエージェント監視
│   ├── cli-logging/  # Codex/Gemini ログ記録
│   └── ...
├── rules/            # 共通ルール（Codex/Gemini委譲、コーディング規約）
├── scripts/          # 管理CLI（orchestra-manager, sync-orchestra）
├── skills/           # ワークフロースキル（/review など）
└── templates/        # テンプレート
```

## エージェント一覧

### コア
- `planner` - タスク分解・マイルストーン策定
- `researcher` - リサーチ・ドキュメント解析

### 要件・仕様
- `requirements` - 要件抽出・NFR整理
- `spec-writer` - 仕様書生成

### 設計
- `architect` - アーキテクチャ設計・技術選定
- `api-designer` - API/IF設計
- `data-modeler` - データモデリング・スキーマ設計
- `auth-designer` - 認証認可設計

### 実装
- `frontend-dev` - React/Next.js/TypeScript
- `backend-python-dev` - Python API
- `backend-go-dev` - Go API

### AI/ML
- `ai-architect` - AIアーキテクチャ・モデル選定
- `ai-dev` - AI機能実装
- `prompt-engineer` - プロンプト設計
- `rag-engineer` - RAG実装

### テスト・デバッグ
- `debugger` - バグ原因分析
- `tester` - テスト戦略・実装

### レビュー（実装）
- `code-reviewer` - コード品質
- `security-reviewer` - セキュリティ
- `performance-reviewer` - パフォーマンス

### レビュー（設計・仕様）
- `spec-reviewer` - 仕様整合性
- `architecture-reviewer` - アーキテクチャ妥当性
- `ux-reviewer` - UX・アクセシビリティ

### ドキュメント
- `docs-writer` - 技術文書・手順書

---

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/yoshihiko555/ai-orchestra.git ~/ai-orchestra
```

### 2. パッケージをインストール

```bash
# プロジェクトに tmux-monitor をインストール（core は自動依存）
python3 ~/ai-orchestra/scripts/orchestra-manager.py install core --project /path/to/project
python3 ~/ai-orchestra/scripts/orchestra-manager.py install tmux-monitor --project /path/to/project
```

orchestra-manager が内部で以下を実行:
1. `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` を設定
2. `.claude/orchestra.json` にパッケージ情報を記録
3. `.claude/settings.local.json` に hooks を登録（`$AI_ORCHESTRA_DIR/packages/...` 参照）
4. `sync-orchestra.py` の SessionStart hook を登録（初回のみ）
5. skills/agents/rules の初回同期を実行

### 3. パッケージ管理コマンド

```bash
# パッケージ一覧
python3 ~/ai-orchestra/scripts/orchestra-manager.py list

# プロジェクトでの導入状況
python3 ~/ai-orchestra/scripts/orchestra-manager.py status --project .

# インストール / アンインストール
python3 ~/ai-orchestra/scripts/orchestra-manager.py install <package> --project .
python3 ~/ai-orchestra/scripts/orchestra-manager.py uninstall <package> --project .

# 一時的な有効化 / 無効化（hooks の登録/解除のみ）
python3 ~/ai-orchestra/scripts/orchestra-manager.py enable <package> --project .
python3 ~/ai-orchestra/scripts/orchestra-manager.py disable <package> --project .

# dry-run（変更内容を表示のみ）
python3 ~/ai-orchestra/scripts/orchestra-manager.py install <package> --project . --dry-run
```

---

## 更新フロー

| 変更内容 | 操作 |
|---------|------|
| Hook スクリプト修正 | `git pull` のみ（即反映） |
| Skills/Agents/Rules 修正 | `git pull`（次回 Claude Code 起動時に自動同期） |
| 新フックイベント追加 | `git pull` + `install` 再実行 |
| CLI スクリプト修正 | `git pull` のみ（即反映） |

---

## 使い方

### エージェントの呼び出し

```
Task(subagent_type="planner", prompt="このタスクを分解して")
Task(subagent_type="code-reviewer", prompt="このコードをレビューして")
```

### レビュースキル

```
/review              # 全レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系レビュー
/review design       # 設計系レビュー
```

---

## アーキテクチャ

```
Claude Code (Orchestrator)
    │
    ├── Codex CLI    # 深い推論・設計判断・デバッグ
    ├── Gemini CLI   # リサーチ・大規模分析・マルチモーダル
    │
    ├── $AI_ORCHESTRA_DIR/packages/
    │   ├── core/          # hook_common.py（共通ユーティリティ）
    │   ├── tmux-monitor/  # tmux リアルタイム監視
    │   ├── cli-logging/   # Codex/Gemini ログ記録
    │   └── ...
    │
    └── 25 Specialized Agents
        ├── Planning: planner, researcher, requirements
        ├── Design: architect, api-designer, data-modeler, auth-designer, spec-writer
        ├── Implementation: frontend-dev, backend-*-dev, ai-*, debugger, tester
        └── Review: code-reviewer, security-reviewer, performance-reviewer, ...
```

### 仕組み

- **Hooks**: `$AI_ORCHESTRA_DIR` 環境変数で直接参照（シンボリックリンク不要）
- **Skills/Agents/Rules**: SessionStart hook (`sync-orchestra.py`) で `$AI_ORCHESTRA_DIR` から `.claude/` に差分コピー
- **CLI Scripts**: `$AI_ORCHESTRA_DIR/packages/{pkg}/scripts/` を直接実行
