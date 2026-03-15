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

## 実行手順

1. 直近のテスト実行結果を確認
2. 直近の `/review` 結果を確認
3. `.claude/Plans.md` の状態を確認
4. Gate 判定（PASS/BLOCKED）を出力

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

### Required Actions (if BLOCKED)
1. {action}
2. {action}
```

## Notes

- このスキルはテスト作成やレビュー実行の代替ではない
- テストは `/tdd`、レビューは `/review` の責務
- 役割は「最終判定の可視化」
