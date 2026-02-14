# AI Orchestra 使用ガイド

オーケストレーションシステムの使い方。

> **Note**: CLI のモデル名・オプションは `.claude/config/cli-tools.yaml` で一元管理。
> `.claude/config/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
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
/review              # 全レビュアー並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review impl         # 実装系（code + security + performance）
/review design       # 設計系（spec + architecture）
```

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

### Planning
| Agent | Role |
|-------|------|
| `planner` | タスク分解・マイルストーン計画 |
| `researcher` | 調査・情報収集 (+Gemini) |
| `requirements` | 要件整理・明確化 |

### Design
| Agent | Role |
|-------|------|
| `architect` | システムアーキテクチャ設計 (+Codex) |
| `api-designer` | REST/GraphQL API設計 (+Codex) |
| `data-modeler` | データベース・スキーマ設計 (+Codex) |
| `auth-designer` | 認証・認可設計 (+Codex) |
| `spec-writer` | 仕様書作成 |

### Implementation
| Agent | Role |
|-------|------|
| `frontend-dev` | React/Vue/Next.js 実装 |
| `backend-python-dev` | Python バックエンド |
| `backend-go-dev` | Go バックエンド |
| `ai-architect` | AI/ML システム設計 (+Codex/Gemini) |
| `ai-dev` | AI 機能実装 |
| `prompt-engineer` | プロンプト設計・最適化 |
| `rag-engineer` | RAG パイプライン実装 |
| `debugger` | バグ特定・修正 (+Codex) |
| `tester` | テストコード作成 |

### Review
| Agent | Role |
|-------|------|
| `code-reviewer` | 可読性・保守性・バグ検出 (+Codex) |
| `security-reviewer` | 脆弱性・権限・情報漏洩 (+Codex) |
| `performance-reviewer` | 計算量・I/O・最適化 (+Codex) |
| `spec-reviewer` | 仕様との整合性 (+Codex) |
| `architecture-reviewer` | アーキテクチャ妥当性 (+Codex) |
| `ux-reviewer` | UX・アクセシビリティ |
| `docs-writer` | ドキュメント作成 |

### Utility
| Agent | Role |
|-------|------|
| `general-purpose` | 汎用タスク・Codex/Gemini委譲 |

---

## Codex vs Gemini 使い分け

| タスク | Codex | Gemini |
|--------|-------|--------|
| 設計判断 | ✓ | |
| デバッグ | ✓ | |
| コード実装相談 | ✓ | |
| トレードオフ分析 | ✓ | |
| ライブラリ調査 | | ✓ |
| コードベース全体理解 | | ✓ |
| 最新ドキュメント検索 | | ✓ |
| PDF/動画/画像処理 | | ✓ |

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

---

## Tips

- 独立したタスクは並列実行で効率化
- レビューは `/review` スキルで一括実行
- 大きなタスクは `planner` で分解してから実行
- 設計判断で迷ったら Codex に相談
- 調査が必要なら Gemini でリサーチ
- 大きな出力が予想される CLI 呼び出しはサブエージェント経由

---

## 有効化方法

```bash
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" init --project /path/to/project
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install <package> --project /path/to/project
```
