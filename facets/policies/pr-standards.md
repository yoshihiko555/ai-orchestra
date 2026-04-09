# PR Standards Policy

**Pull Request 作成時に守るべき共通ルール。`pr-create` および `issue-fix` から参照される。**

## PR テンプレート

PR 本文は以下のテンプレート構造に従う。プロジェクトに `.github/PULL_REQUEST_TEMPLATE.md` がある場合はそれを優先する。

### フォールバックテンプレート

```markdown
## Summary

-

## Testing

- [ ] テスト実施済み
- [ ] 未実施（理由を記載）

## Release Note

- ユーザー向け変更点:
- `CHANGELOG.md` 更新:

## Checklist

- [ ] PR タイトルが GitHub Release にそのまま載っても読める
- [ ] 適切なラベルを付けた (`feature` / `fix` / `docs` / `chore` / ...)
- [ ] ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` を更新した
```

### セクション埋め込みルール

| セクション | 入力ソース | 記述ルール |
|-----------|-----------|-----------|
| Summary | コミット履歴 + diff stat | 変更内容を箇条書きで要約 |
| Testing | テスト実行結果 | 実施済みなら結果を記載、未実施なら理由を記載 |
| Release Note | 変更内容の分析 | ユーザー向け変更がある場合のみ記載 |
| Checklist | 自動チェック | 可能な項目は事前にチェック済みにする |

## PR タイトル

- 形式: `{prefix}: {要約}`
- タイトルは **GitHub Release にそのまま載っても読める** 簡潔さにする
- 70 文字以内を目安にする

## ブランチプレフィックスとラベルの対応

| ブランチプレフィックス | PR タイトルプレフィックス | ラベル |
|---------------------|----------------------|--------|
| `fix/` | `fix:` | `fix` |
| `feat/` | `feat:` | `feature` |
| `docs/` | `docs:` | `docs` |
| `chore/` | `chore:` | `chore` |
| `refactor/` | `refactor:` | `refactor` |
| `test/` | `test:` | `test` |
| `task/` | `chore:` | `chore` |
| `release/` | `release:` | `release` |
| その他 | `chore:` | `chore` |

## Issue 連携

- Issue がある場合、PR 本文冒頭に `Closes #{番号}` を追加する
- Issue のラベルも参照してラベル決定を補完する

## Git 操作ルール

- `main` への直接 push は行わない
- マージ方式は GitHub 上の **Squash and merge** を前提とする
- 競合解決は PR ブランチ側で `origin/main` を取り込んで行う
- Push は `-u` フラグでトラッキングを設定する: `git push -u origin {ブランチ名}`
