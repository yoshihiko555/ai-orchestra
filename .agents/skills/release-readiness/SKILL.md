---
name: release-readiness
description: Run a final pre-merge readiness check using test/review/task status evidence.
disable-model-invocation: true
---

# Release Readiness

`/release-readiness` は **マージ直前の最終確認フロー** です。

## 目的

- テスト結果・レビュー結果・タスク状態を一度に確認する
- 「マージしてよいか」を PASS / BLOCKED で明確化する

## チェック項目

1. **テスト**
   - 直近の必須テストが成功しているか
2. **レビュー**
   - `Critical` が未解消でないか
   - `High` の扱いが運用方針どおりに処理されているか
3. **タスク状態**
   - `Plans.md` に未解消の `cc:blocked` がないか
   - `cc:TODO` が残っている場合、マージ対象外として合意済みか
4. **差分健全性**
   - 不要な変更や未整理差分がないか（`git status`, `git diff --stat`）
5. **関連ドキュメントの更新**
   - 差分に対応する README / CHANGELOG / API ドキュメントが更新されているか
   - 新機能・破壊的変更がある場合、ユーザー向け説明が追加されているか
   - CHANGELOG の `Unreleased` セクションが差分内容を反映しているか
   - ドキュメント更新漏れは BLOCKED とする
6. **テスト実行の正当性**
   - 変更対象のコードパスに対してテストが実際に実行されているか
   - テストが「通っている」だけでなく、変更箇所の振る舞いを検証しているか
   - カバレッジツール出力が利用可能な場合はそれを根拠に含める
   - 利用できない場合は `git diff` の変更関数・クラスとテストファイル内の参照を突き合わせて判定する
   - 変更コードパスのテスト未実行は BLOCKED とする
7. **テストケースの過不足**
   - 差分で追加・変更されたロジックに対応するテストケースが存在するか
   - 削除されたコードに対応する不要なテストケースが残っていないか
   - 正常系・異常系・境界値の主要パターンがカバーされているか
   - テストケースの明らかな欠落は BLOCKED とする

## 実行手順

1. `git diff --stat` と `git diff` でマージ対象の差分を取得
2. 直近のテスト実行結果を確認
3. 直近の `/review` 結果を確認
4. `.claude/Plans.md` の状態を確認
5. 差分に対応するドキュメント更新を確認（README / CHANGELOG / API ドキュメント）
6. 変更コードパスに対するテストカバレッジを確認
   - カバレッジツール出力が利用可能ならそれを使用
   - 利用できない場合は diff の変更関数・クラスとテストファイル内の参照を突き合わせる
7. テストケースの過不足を評価（追加ロジック・削除コード・境界値）
8. Gate 判定（PASS / BLOCKED）を出力

## 出力フォーマット

```markdown
## Release Readiness

### Decision
- status: PASS / BLOCKED
- reason: {one-line summary}

### Evidence
- tests: pass/fail (source)
- review: critical/high status (source)
- plans: blocked/todo status (source)
- diff: summary
- docs: updated/missing/n/a — {対象ドキュメント}
- test-coverage: covered/gaps-found/n/a (source: coverage-tool|diff-analysis)
- test-completeness: sufficient/gaps-found/stale-tests-found — {詳細}

### Required Actions (if BLOCKED)
1. {action}
2. {action}
```

## BLOCKED 判定基準

| チェック項目 | BLOCKED 条件 | 例外（skip 可） |
|-------------|-------------|----------------|
| テスト | 必須テストが失敗 | — |
| レビュー | Critical が未解消 | — |
| タスク状態 | `cc:blocked` が未解消 | — |
| 差分健全性 | 未整理差分あり | — |
| ドキュメント更新 | ユーザー向け変更でドキュメント未更新 | 内部リファクタのみ（テスト・lint 整理など） |
| テストカバレッジ | 変更コードパスのテスト未実行 | — |
| テストケース過不足 | 追加ロジックに対応するテストケースが明らかに欠落 | — |

## Notes

- このスキルはテスト作成やレビュー実行の代替ではない
- テストは `/tdd`、レビューは `/review` の責務
- 役割は「最終判定の可視化」
- カバレッジツールが未導入の環境では Evidence の source に `diff-analysis` を明記し、判定の根拠を透明にする
