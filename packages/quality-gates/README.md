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

| フック                          | タイミング                                     | 内容                                                                                        |
| ------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `check-context-optimization.py` | PreToolUse (Read/Grep/Bash)                    | 大きすぎる読み込みや `cat` 利用等を検出し、エスカレーション戦略への切り替えを提案           |
| `post-implementation-review.py` | PostToolUse (Edit/Write)                       | 変更ファイル数3以上または変更行数100以上でレビューを提案（TTL 24時間で再武装）              |
| `post-test-analysis.py`         | PostToolUse (Bash)                             | テスト実行結果を分析し `quality_gate` イベントを記録。失敗時は既定でブロック（exit code 2） |
| `lint-on-save.py`               | PostToolUse (Edit/Write)                       | ファイル種別ごとの formatter/linter を実行し結果を報告                                      |
| `test-tampering-detector.py`    | PostToolUse (Edit/Write/Bash/Delete/MultiEdit) | skip 追加・抑制コメント・テストファイル削除を検知                                           |
| `test-gate-checker.py`          | PostToolUse (Edit/Write)                       | 大きな変更後にテスト未実行を検知し実行を提案                                                |
| `turn-end-summary.py`           | Stop                                           | working-context / Plans.md から次ターン向け `systemMessage` を生成                          |
| `evaluation-set-checker.py`     | PostToolUse (Edit/Write)                       | テストファイル変更時に評価セット（`docs/evaluation/<pkg>.md`）との突合を案内                |

すべての hook は `main()` 内で例外を捕捉し、stderr にログを出して exit code 0 で終わる
fail-open 設計を採用する（内部エラーでセッションを止めない）。`post-test-analysis.py` の
ブロック時のみ exit code 2 を使う。

## スキル

| スキル              | 内容                                             |
| ------------------- | ------------------------------------------------ |
| `review`            | マルチエージェントコードレビュー（スマート選定） |
| `tdd`               | テスト駆動開発ワークフロー                       |
| `design-tracker`    | 設計記録                                         |
| `release-readiness` | リリース前最終チェック                           |

## 設定キー（`quality_gate.*`）

設定ファイルは `quality-gates` パッケージ自身が所有する `.claude/config/quality-gates/quality-gates.json`
（Issue #153 で `audit` の `audit-flags.json` から分離）。プロジェクト固有の上書きは
`.claude/config/quality-gates/quality-gates.local.json` で行う（`config-loading` ルール準拠）。

| キー                                       | デフォルト      | 説明                                                                                                                                                                  |
| ------------------------------------------ | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `quality_gate.enabled`                     | `true`          | quality-gates の hook（`evaluation-set-checker.py` を除く）の有効/無効                                                                                                |
| `quality_gate.block_on_failed_test`        | `true`          | `post-test-analysis.py` がテスト失敗時に exit code 2 でブロックするか（opt-out 方式。`false` を明示設定した場合のみ提案に留める）                                     |
| `quality_gate.test_file_threshold`         | `3`             | `test-gate-checker.py` がテスト実行を促す変更ファイル数の閾値（`post-implementation-review.py` のレビュー提案には適用されず、`FILE_THRESHOLD`（定数値 3）が使われる） |
| `quality_gate.test_line_threshold`         | `100`           | `test-gate-checker.py` がテスト実行を促す変更行数の閾値（`post-implementation-review.py` のレビュー提案には適用されず、`LINE_THRESHOLD`（定数値 100）が使われる）     |
| `context_optimization.enabled`             | `true`          | `check-context-optimization.py` の有効/無効                                                                                                                           |
| `context_optimization.read_line_threshold` | `200`           | 警告を出すファイル読み込み行数の閾値                                                                                                                                  |
| `context_optimization.max_file_size_bytes` | `5242880`       | 警告を出すファイルサイズの閾値（バイト）                                                                                                                              |
| `features.evaluation_set_check.enabled`    | `true`          | `evaluation-set-checker.py` 専用の独立フラグ（`quality_gate.enabled` とは別枠）                                                                                       |
| `paths.state_dir`                          | `.claude/state` | hook の状態ファイル（`test-gate-checker.json` 等）の保存先ディレクトリ                                                                                                |

`quality_gate.enabled=false` の場合、対象 hook は提案・警告・ブロック・audit イベント記録を
含む全動作を行わない。

`.claude/config/quality-gates/quality-gates.local.json`:

```json
{
  "features": {
    "quality_gate": {
      "block_on_failed_test": false
    }
  }
}
```

### 旧 `audit-flags.local.json` からの移行（0.4.x の間は読み替え）

Issue #153 で `quality_gate` / `context_optimization` / `evaluation_set_check` の機能フラグと
`paths.state_dir` の所有パッケージを `audit` から `quality-gates` に移した。既存プロジェクトの
`.claude/config/audit/audit-flags.local.json` にこれらのキーが残っていても、0.4.x の間は
読み替えて従来どおり動作する。実効値の優先順位（後勝ち）は次のとおり:

1. `.claude/config/quality-gates/quality-gates.json`（base）
2. `.claude/config/audit/audit-flags.local.json` の該当キー（deprecated 読み替え）
3. `.claude/config/quality-gates/quality-gates.local.json`（最優先）

新規プロジェクトおよび今後の変更は `.claude/config/quality-gates/quality-gates.local.json` を使うこと。
`audit-flags.local.json`（または配布先 base の未同期 `audit-flags.json`）に該当キーが残っている場合、
SessionStart 時（`audit-bootstrap.py`）に 1 行の移行案内が出る。この読み替えは 0.4.x の間維持し、
0.5.0 で削除を再検討する。

`config/evaluation-set-mapping.yaml` は `evaluation-set-checker.py` が使う評価セット ID →
テストパス glob の明示マッピング（詳細は `docs/evaluation/quality-gates.md` EV-26 参照）。

## 秘匿情報の扱い

`additionalContext` に出力するコマンド文字列・テスト出力・formatter/linter 出力・追加行の
スニペットは、`packages/audit/hooks/secret_masking.py` の共通パターン（API キー・トークン・
秘密鍵等）でマスクしてから出力する。200 文字への切り詰めはマスキングの代替にはならない。

## 依存

- `core`（`hook_common`）
- `audit`（`event_logger`, `secret_masking`）。`quality_gate.*` 等の機能フラグはもはや `audit` との
  共有設定ではなく `quality-gates` 自身が所有するが、旧 `audit-flags.local.json` の読み替え
  （上記「旧 `audit-flags.local.json` からの移行」参照）のみ `audit` のディレクトリ構造を参照する
