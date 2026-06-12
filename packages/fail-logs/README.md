# fail-logs

AI（Claude Code 等）の**失敗イベントを記録する基盤**。蓄積した失敗を次回以降の改善に活かす学習ループの第一歩。

## 何をするか

PostToolUse hook が以下の失敗を検知し、`.claude/logs/fail-logs/failures.jsonl` に追記する:

| 失敗種別       | 検知対象                                  |
| -------------- | ----------------------------------------- |
| `tool_error`   | Bash の非ゼロ終了、他ツールの明示的エラー |
| `test_failure` | pytest / npm test 等の失敗                |
| `lint_failure` | ruff / mypy 等の失敗                      |
| `cli_failure`  | codex / agy 等の外部 CLI 失敗             |

**失敗のみを記録する**（ノイズ抑制）。成功を含む実行統計は audit の `quality_gate` が担う。

## 検知の特徴

検知ロジックは `packages/core/hooks/failure_detector.py`（純粋関数）に集約。

- **2 段判定**: ① exit_code が非ゼロ → 失敗。② exit_code が 0/欠落でも test/lint コマンドで出力に失敗マーカー → 失敗。
- これにより `pytest ... | tail` のようにパイプで終了コードがマスクされる**誤検知バグ**を回避する。

## ログスキーマ（audit v1 互換）

```json
{
  "v": 1,
  "ts": "2026-06-12T...",
  "sid": "session-id",
  "eid": "12-hex",
  "type": "failure",
  "data": {
    "failure_type": "test_failure",
    "error_type": "assertion",
    "detected_by": "output_pattern",
    "command_kind": "test",
    "tool": "Bash",
    "command": "pytest ...",
    "error_excerpt": "FAILED ...",
    "exit_code": 0,
    "cwd": "/path"
  }
}
```

- ログファイルは所有者限定パーミッション（`0600`）。
- 機密情報（API キー・トークン等）は記録前に `[REDACTED]` へマスクする。
- `.claude/logs/` は gitignore 済み（ローカル蓄積）。

## 設定

`config/fail-logs.yaml`（プロジェクト固有の上書きは `fail-logs.local.yaml`）:

```yaml
enabled: true # 全体の有効/無効
targets: # 失敗種別ごとのトグル
  tool_error: true
  test_failure: true
  lint_failure: true
  cli_failure: true
max_excerpt_chars: 500 # 抜粋の最大文字数
logs_dir: .claude/logs/fail-logs
```

## 責務境界（audit / quality-gates との関係）

| パッケージ      | 責務                                                        |
| --------------- | ----------------------------------------------------------- |
| `audit`         | オーケストレーションの compliance / observability（KPI 等） |
| `quality-gates` | テストゲート + `quality_gate` イベント（合格率の分母）      |
| `fail-logs`     | **失敗知識の蓄積**（学習ループの入力）                      |

- 失敗キャプチャは目的・スキーマが異なるため、当面 quality-gates との重複を意図的に許容する。
- 失敗検知ロジックは core の共通ユーティリティに集約済み。quality-gates への適用（パイプマスクバグ修正）と将来の責務移行は ADR-20260612-025 を参照。

## 依存

- `core`（`hook_common`, `failure_detector`）のみ。audit には依存しない。
