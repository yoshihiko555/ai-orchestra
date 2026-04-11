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
- [ ] 適切なラベルを付けた (`bug` / `enhancement` / `documentation` / `refactor` / `task` / ...)
- [ ] ユーザー向け変更がある場合は `CHANGELOG.md` の `Unreleased` を更新した
```

### セクション埋め込みルール

| セクション   | 入力ソース               | 記述ルール                                   |
| ------------ | ------------------------ | -------------------------------------------- |
| Summary      | コミット履歴 + diff stat | 変更内容を箇条書きで要約                     |
| Testing      | テスト実行結果           | 実施済みなら結果を記載、未実施なら理由を記載 |
| Release Note | 変更内容の分析           | ユーザー向け変更がある場合のみ記載           |
| Checklist    | 自動チェック             | 可能な項目は事前にチェック済みにする         |

## PR タイトル

- 形式: `{prefix}: {要約}`
- タイトルは **GitHub Release にそのまま載っても読める** 簡潔さにする
- 70 文字以内を目安にする

## ブランチプレフィックスとラベルの対応

ラベルは GitHub リポジトリで実際に定義されているものに合わせる。存在しないラベルを指定すると `gh pr create` がエラーを返すため、ポリシーと実リポジトリを同期させる。

| ブランチプレフィックス | PR タイトルプレフィックス | ラベル          |
| ---------------------- | ------------------------- | --------------- |
| `fix/`                 | `fix:`                    | `bug`           |
| `feat/`                | `feat:`                   | `enhancement`   |
| `docs/`                | `docs:`                   | `documentation` |
| `chore/`               | `chore:`                  | `task`          |
| `refactor/`            | `refactor:`               | `refactor`      |
| `test/`                | `test:`                   | `task`          |
| `task/`                | `chore:`                  | `task`          |
| `release/`             | `release:`                | `task`          |
| その他                 | `chore:`                  | `task`          |

> **Note**: `bug` / `enhancement` / `documentation` は GitHub のデフォルトラベルをそのまま採用している。`refactor` / `task` はプロジェクト固有ラベル。リポジトリが異なるラベル体系を使っている場合は、この表と実ラベルを個別に調整すること。

## Issue 連携

- Issue がある場合、PR 本文冒頭に `Closes #{番号}` を追加する
- Issue のラベルも参照してラベル決定を補完する

## Git 操作ルール

- `main` への直接 push は行わない
- マージ方式は GitHub 上の **Squash and merge** を前提とする
- 競合解決は PR ブランチ側で `origin/main` を取り込んで行う
- Push は `-u` フラグでトラッキングを設定する: `git push -u origin {ブランチ名}`
