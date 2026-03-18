# CLI Language Policy

**外部 CLI（Codex CLI / Gemini CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象 | 言語 |
|------|------|
| Codex / Gemini への質問 | **英語** |
| Codex / Gemini からの回答 | **英語** |
| ユーザーへの報告 | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作 |
|----------------------|------|
| `codex` | Codex CLI を使用 |
| `gemini` | Gemini CLI を使用 |
| `claude-direct` | 外部 CLI を呼ばず Claude で処理 |
| `auto` | タスク種別に応じて選択（深い推論 → Codex、調査 → Gemini、単純作業 → Claude） |

## サンドボックス実行

外部 CLI（Codex / Gemini）は sandbox 内で直接実行する。

### 前提条件（ユーザー設定）

以下の設定が `~/.claude/settings.json` に必要:

```json
{
  "env": {
    "NODE_OPTIONS": "--use-env-proxy"
  },
  "sandbox": {
    "enableWeakerNetworkIsolation": true,
    "network": {
      "allowedDomains": ["generativelanguage.googleapis.com", "accounts.google.com"]
    },
    "filesystem": {
      "allowWrite": ["~/.gemini"]
    }
  }
}
```

### フォールバック

- sandbox 内で実行エラーが発生した場合は `claude-direct` にフォールバックする
- `dangerouslyDisableSandbox` は使用しない（サブエージェントでは承認プロンプトが自動拒否されるため）

---

# AI Orchestra 使用ガイド

オーケストレーションシステムの使い方。

> **Note**: CLI のモデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
> 以下のコマンド例中のプレースホルダー（`<codex.model>` 等）は、config ファイルの値で置換して使用する。

---

## Quick Reference

### エージェント呼び出し

```
Task(subagent_type="planner", prompt="タスク分解: ユーザー認証機能")
Task(subagent_type="frontend-dev", prompt="実装: ログインフォーム")
```

### Codex CLI（深い推論）

```bash
# 設計相談・デバッグ・トレードオフ分析 — config の codex.model, codex.sandbox.analysis, codex.flags を展開
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "{質問}" 2>/dev/null

# 実装作業 — config の codex.sandbox.implementation を使用
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "{タスク}" 2>/dev/null
```

**使う場面:** 設計判断、デバッグ、比較検討、レビュー

### Gemini CLI（リサーチ）

```bash
# config の gemini.model を -m フラグに展開して使う

# リサーチ
gemini -m <gemini.model> -p "{質問}" 2>/dev/null

# コードベース分析
gemini -m <gemini.model> -p "{質問}" --include-directories . 2>/dev/null

# マルチモーダル（PDF/動画）
gemini -m <gemini.model> -p "{プロンプト}" < /path/to/file 2>/dev/null
```

**使う場面:** ライブラリ調査、ドキュメント検索、大規模分析、PDF/動画処理

### レビュー実行

```
/review              # スマート選定（code + 変更内容に応じた専門レビュアー）
/review all          # 全 6 レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系（code + security + performance）
/review design       # 設計系（spec + architecture）
/release-readiness   # マージ前最終チェック（テスト/レビュー/blocked確認）
```

**スマート選定**: `/review` はパスパターン + diff コンテンツスキャンでレビュアーを自動選定（平均 2-3 名）。全レビュアーが必要な場合は `/review all` を使用。

---

## Context Management

メインオーケストレーターのコンテキストを節約するため、サブエージェント経由で実行する。

| 状況 | 推奨方法 |
|------|----------|
| 大きな出力が予想される | サブエージェント経由 |
| 複数の分析が必要 | 並列サブエージェント |
| 詳細なレビュー | `/review` スキル使用 |
| Codex/Gemini 相談 | サブエージェント経由（出力が大きい場合） |

### Codex/Gemini 呼び出しパターン

```
# 大きな出力が予想される場合はサブエージェント経由
Task(subagent_type="general-purpose", prompt="""
Codex に設計を相談してください:
{質問内容}

codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "..." 2>/dev/null

結果を要約して返してください。
""")
```

---

## 利用可能なエージェント

> **Note**: 各エージェントの実行ツールは `cli-tools.yaml` の `agents.<name>.tool` で決まる。
> 下表はロール説明であり、Codex/Gemini の固定利用を意味しない。

### Planning
| Agent | Role |
|-------|------|
| `planner` | タスク分解・マイルストーン計画 |
| `researcher` | 調査・情報収集 |
| `requirements` | 要件整理・明確化 |

### Design
| Agent | Role |
|-------|------|
| `architect` | システムアーキテクチャ設計 |
| `api-designer` | REST/GraphQL API設計 |
| `data-modeler` | データベース・スキーマ設計 |
| `auth-designer` | 認証・認可設計 |
| `spec-writer` | 仕様書作成 |

### Implementation
| Agent | Role |
|-------|------|
| `frontend-dev` | React/Vue/Next.js 実装 |
| `backend-python-dev` | Python バックエンド |
| `backend-go-dev` | Go バックエンド |
| `ai-architect` | AI/ML システム設計 |
| `ai-dev` | AI 機能実装 |
| `prompt-engineer` | プロンプト設計・最適化 |
| `rag-engineer` | RAG パイプライン実装 |
| `debugger` | バグ特定・修正 |
| `tester` | テストコード作成 |

### Review
| Agent | Role |
|-------|------|
| `code-reviewer` | 可読性・保守性・バグ検出 |
| `security-reviewer` | 脆弱性・権限・情報漏洩 |
| `performance-reviewer` | 計算量・I/O・最適化 |
| `spec-reviewer` | 仕様との整合性 |
| `architecture-reviewer` | アーキテクチャ妥当性 |
| `ux-reviewer` | UX・アクセシビリティ |
| `docs-writer` | ドキュメント作成 |

### Utility
| Agent | Role |
|-------|------|
| `general-purpose` | 汎用タスク・外部CLI委譲 |

---

## `tool: auto` 時の使い分け目安

| タスク | 推奨 |
|--------|------|
| 設計判断 | Codex 候補 |
| デバッグ | Codex 候補 |
| コード実装相談 | Codex 候補 |
| トレードオフ分析 | Codex 候補 |
| ライブラリ調査 | Gemini 候補 |
| コードベース全体理解 | Gemini 候補 |
| 最新ドキュメント検索 | Gemini 候補 |
| PDF/動画/画像処理 | Gemini 候補 |

---

## ワークフロー例

### 新機能開発

1. **計画フェーズ**
   ```
   Task(subagent_type="planner", prompt="計画: {機能名}")
   Task(subagent_type="researcher", prompt="調査: {関連技術}")
   ```

2. **設計フェーズ**
   ```
   Task(subagent_type="architect", prompt="設計: {機能名}")
   Task(subagent_type="api-designer", prompt="API設計: {機能名}")
   ```

3. **実装フェーズ**
   ```
   Task(subagent_type="backend-python-dev", prompt="実装: {機能名}")
   Task(subagent_type="tester", prompt="テスト作成: {機能名}")
   ```

4. **レビューフェーズ**
   ```
   /review  # 全レビュアー並列実行
   ```

5. **仕上げフェーズ（任意だが推奨）**
   ```
   /release-readiness  # 最終チェック
   ```

---

## Tips

- 独立したタスクは並列実行で効率化
- レビューは `/review` スキルで一括実行
- 大きなタスクは `planner` で分解してから実行
- 設計判断で迷ったら `agents.<target>.tool` を確認し、`tool: auto` の場合は Codex を候補にする
- 調査が必要なら `agents.<target>.tool` を確認し、`tool: auto` の場合は Gemini を候補にする
- 大きな出力が予想される CLI 呼び出しはサブエージェント経由

---

## 有効化方法

```bash
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" init --project /path/to/project
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install <package> --project /path/to/project
```
