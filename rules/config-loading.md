# Config Loading Rule

**設定ファイルはレイヤード構成。ローカル上書きファイルがあればそちらを優先する。**

## 読み込み手順

CLI ツールの設定値（モデル名・フラグ等）を参照する際は、以下の順序で読み込む:

1. `.claude/config/{name}.yaml` — ベース設定（ai-orchestra から自動同期）
2. `.claude/config/{name}.local.yaml` — プロジェクト固有の上書き（存在する場合のみ）

**ローカルファイルに定義されたキーはベースを上書きする。** 未定義のキーはベースの値がそのまま使われる。

## 例: cli-tools.yaml

ベース（自動同期される）:
```yaml
# .claude/config/cli-tools.yaml
codex:
  model: gpt-5.3-codex
  sandbox:
    analysis: read-only
    implementation: workspace-write
  flags: --full-auto
gemini:
  model: gemini-2.5-pro
```

プロジェクト固有の上書き:
```yaml
# .claude/config/cli-tools.local.yaml
codex:
  model: o3-pro
```

→ この場合の最終的な値:
- `codex.model` = `o3-pro`（local で上書き）
- `codex.sandbox.analysis` = `read-only`（ベースの値を継続使用）
- `gemini.model` = `gemini-2.5-pro`（ベースの値を継続使用）

## 対象ファイル

| ファイル | 用途 |
|---------|------|
| `cli-tools.yaml` | CLI ツールのモデル名・サンドボックス・フラグ |
| `delegation-policy.yaml` | キーワードベースのルーティング設定 |
| `orchestration-flags.yaml` | 機能フラグ（route_audit, quality_gate 等） |

## 同期の仕組み

- ベースファイルは `sync-orchestra.py` により SessionStart 時に ai-orchestra から自動同期される
- `*.local.yaml` は同期・削除の対象外（プロジェクトに残り続ける）
- ai-orchestra 側で新しいキーが追加された場合、ベースファイル経由で全プロジェクトに反映される

## 重要

- 設定を参照する際は **必ず両ファイルを確認** してからコマンドを構築すること
- `.local.yaml` が存在しない場合はベースのみを使用する（エラーにしない）
