# reverse パッケージ

既存コードベースのリバースエンジニアリングを 5 フェーズ対話型で実行する `/reverse` スキルを提供する。

## トリガー

```
/reverse              # リポジトリルート全体を解析
/reverse <path>       # 指定パス配下を解析（相対/絶対パス対応）
```

## フェーズ構成

| Phase | 名前                              | 担当                                                                       | 成果物                             |
| ----- | --------------------------------- | -------------------------------------------------------------------------- | ---------------------------------- |
| 1     | 走査 (Scan)                       | Gemini + `collect-stats.py` + `find-entrypoints.py`                        | `scope.md`                         |
| 2     | 依存グラフ (Graph)                | general-purpose 経由 Gemini + `generate-mermaid.py`                        | `dependency.md` + `dependency.mmd` |
| 3     | 機能抽出 (Extract)                | Gemini                                                                     | `features.md`                      |
| 4     | ドキュメント化 (Document)         | Claude（集約）                                                             | `design.md`                        |
| 5     | 負債/脆弱性レポート (Debt Report) | `code-reviewer` + `security-reviewer` (claude-direct) + `collect-todos.py` | `debt-report.md` (tiered-review)   |

各フェーズ末で AskUserQuestion による受け入れ確認を行う。

## 成果物配置

```
.claude/docs/reverse/{YYYY-MM-DD}_{target-slug}/
  README.md              # インデックス
  scope.md
  dependency.md
  dependency.mmd
  features.md
  design.md
  debt-report.md
```

## 補助スクリプト

すべて言語非依存。`.claude/skills/reverse/scripts/` に配置される。

| スクリプト            | 役割                                                                                             |
| --------------------- | ------------------------------------------------------------------------------------------------ |
| `collect-stats.py`    | 拡張子ベースで言語別ファイル数/LOC を JSON 出力                                                  |
| `find-entrypoints.py` | 設定ファイル（package.json / pyproject.toml / Cargo.toml / go.mod 等）からエントリポイントを抽出 |
| `collect-todos.py`    | TODO/FIXME/HACK/XXX/DEPRECATED を集約（バイナリファイル自動除外）                                |
| `generate-mermaid.py` | imports JSON を Mermaid graph 構文に変換                                                         |

imports 抽出（依存関係抽出）は言語横断のため、Python スクリプトではなく instruction 内で `Task(subagent_type="general-purpose")` 経由で Gemini に委譲する。

## CLI 連携

`cli-tools.yaml` の設定に従う:

- `gemini.enabled: true` の場合は Gemini 主体で大規模理解
- `gemini.enabled: false` の場合は全フェーズ claude-direct フォールバック
- `code-reviewer` / `security-reviewer` は `tool: claude-direct` 前提

## 関連スキル

```
/reverse → 設計書・依存図・負債レポート
  ↓ （改修や拡張を行う場合）
/design → 要件定義・基本設計・詳細設計
  ↓
/preflight → タスク分解
  ↓
/startproject → 実装
```
