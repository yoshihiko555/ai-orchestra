# quality-gates

実装後の品質チェックを**セッションを止めずに**（fail-open で）自動化する hook 群と、
レビュー/TDD/リリース前確認のスキル群を提供するパッケージ。

## 何をするか

編集直後の formatter/lint 実行、変更規模に応じたレビュー・テスト実行の提案、テスト実行結果の
分析と Codex への相談提案、テスト改ざん（skip 追加・抑制コメント・テストファイル削除）の検知、
テストファイル変更時の評価セット（`docs/evaluation/<pkg>.md`）突合案内、ターン終了時の軽量
サマリー通知を行う。

**行わないこと**（Non-Goals）:

- 実際のテスト実行そのもの（実行を提案するのみ）
- コードレビューの実施主体（`review` スキルはサブエージェントへの委譲）
- CI/CD レベルのマージブロッキングゲート（`post-test-analysis.py` の exit code 2 はローカル
  セッションの当該 PostToolUse 呼び出しのみに影響）
- マージ可否の最終判断（`release-readiness` スキルは人間の確認を前提とする）
- 評価セットとの突合作業そのもの（`evaluation-set-checker.py` は確認を促す案内のみ）

## フック一覧

| フック                           | タイミング                            | 内容                                                                                          |
| -------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `check-context-optimization.py`  | PreToolUse (Read/Grep/Bash)             | 大きすぎる読み込みや `cat` 利用等を検出し、エスカレーション戦略への切り替えを提案                |
| `post-implementation-review.py`  | PostToolUse (Edit/Write)                | 変更ファイル数3以上または変更行数100以上でレビューを提案（TTL 24時間で再武装）                   |
| `post-test-analysis.py`          | PostToolUse (Bash)                      | テスト実行結果を分析し `quality_gate` イベントを記録。失敗時は既定でブロック（exit code 2）      |
| `lint-on-save.py`                | PostToolUse (Edit/Write)                | ファイル種別ごとの formatter/linter を実行し結果を報告                                          |
| `test-tampering-detector.py`     | PostToolUse (Edit/Write/Bash/Delete/MultiEdit) | skip 追加・抑制コメント・テストファイル削除を検知                                        |
| `test-gate-checker.py`           | PostToolUse (Edit/Write)                | 大きな変更後にテスト未実行を検知し実行を提案                                                    |
| `turn-end-summary.py`            | Stop                                     | working-context / Plans.md から次ターン向け `systemMessage` を生成                              |
| `evaluation-set-checker.py`      | PostToolUse (Edit/Write)                | テストファイル変更時に評価セット（`docs/evaluation/<pkg>.md`）との突合を案内                     |

すべての hook は `main()` 内で例外を捕捉し、stderr にログを出して exit code 0 で終わる
fail-open 設計を採用する（内部エラーでセッションを止めない）。`post-test-analysis.py` の
ブロック時のみ exit code 2 を使う。

## スキル

| スキル              | 内容                                             |
| ------------------- | ------------------------------------------------ |
| `review`             | マルチエージェントコードレビュー（スマート選定） |
| `tdd`                | テスト駆動開発ワークフロー                       |
| `design-tracker`     | 設計記録                                          |
| `release-readiness`  | リリース前最終チェック                            |

## 設定キー（`quality_gate.*`）

設定ファイルは `audit` パッケージの `.claude/config/audit/audit-flags.json`（`quality_gate` セクションは
`audit` と `quality-gates` の共有設定）。プロジェクト固有の上書きは
`.claude/config/audit/audit-flags.local.json` で行う（`config-loading` ルール準拠）。

| キー                                | デフォルト | 説明                                                                         |
| ----------------------------------- | ---------- | ----------------------------------------------------------------------------- |
| `quality_gate.enabled`              | `true`     | quality-gates の hook（`evaluation-set-checker.py` を除く）の有効/無効        |
| `quality_gate.block_on_failed_test` | `true`     | `post-test-analysis.py` がテスト失敗時に exit code 2 でブロックするか（opt-out 方式。`false` を明示設定した場合のみ提案に留める） |
| `quality_gate.test_file_threshold`  | `3`        | レビュー/テスト実行を促す変更ファイル数の閾値                                 |
| `quality_gate.test_line_threshold`  | `100`      | レビュー/テスト実行を促す変更行数の閾値                                       |
| `features.evaluation_set_check.enabled` | `true` | `evaluation-set-checker.py` 専用の独立フラグ（`quality_gate.enabled` とは別枠） |

`quality_gate.enabled=false` の場合、対象 hook は提案・警告・ブロック・audit イベント記録を
含む全動作を行わない。

```json
// .claude/config/audit/audit-flags.local.json
{
  "features": {
    "quality_gate": {
      "block_on_failed_test": false
    }
  }
}
```

`config/evaluation-set-mapping.yaml` は `evaluation-set-checker.py` が使う評価セット ID →
テストパス glob の明示マッピング（詳細は `docs/evaluation/quality-gates.md` EV-26 参照）。

## 秘匿情報の扱い

`additionalContext` に出力するコマンド文字列・テスト出力・formatter/linter 出力・追加行の
スニペットは、`packages/audit/hooks/secret_masking.py` の共通パターン（API キー・トークン・
秘密鍵等）でマスクしてから出力する。200 文字への切り詰めはマスキングの代替にはならない。

## 依存

- `core`（`hook_common`）
- `audit`（`event_logger`, `secret_masking`, 共有設定 `quality_gate.*`）
