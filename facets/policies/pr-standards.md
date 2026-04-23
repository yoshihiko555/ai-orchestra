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

- `main` / 解決済み base branch への直接 push は行わない
- マージ方式は GitHub 上の **Squash and merge** を前提とする
- 競合解決は PR ブランチ側で `origin/{base}` を取り込んで行う（`{base}` は後述の resolver で解決）
- Push は `-u` フラグでトラッキングを設定する: `git push -u origin {ブランチ名}`

## Base Branch Resolution

**PR の base branch を固定せず、resolver スクリプトで解決する。** `pr-create` / `issue-fix` / その他 PR を作成するスキルは、このルールに従って `$BASE` を取得する。

### Resolver スクリプト

```bash
: "${AI_ORCHESTRA_DIR:?AI_ORCHESTRA_DIR is not set}"
BASE=$(python3 "$AI_ORCHESTRA_DIR/packages/git-workflow/scripts/resolve_base_branch.py" \
  ${BASE_OVERRIDE:+--base "$BASE_OVERRIDE"})
```

- 実体: `packages/git-workflow/scripts/resolve_base_branch.py`
- 出力: stdout に解決済み base branch 名を 1 行（`origin/` プレフィックスは除去される）
- `AI_ORCHESTRA_DIR` 未設定時はガードで即座に失敗させ、`$BASE` が空のまま `gh pr create --base ""` が実行される事故を防ぐ
- `BASE_OVERRIDE` が未定義の場合 `${BASE_OVERRIDE:+...}` は空に展開され、`--base` 引数なしで resolver を呼ぶ

### 解決優先順位

1. **`--base <branch>` 明示指定** — ユーザーが `/pr-create --base stage` のように指定した値
2. **環境変数 `AI_ORCHESTRA_BASE_BRANCH`** — プロジェクト固有のデフォルト（shell 設定や `.envrc` 等で設定）
3. **自動推定** — 候補 `staging` / `stage` / `develop` / `main` / `master` の中で実在するものを対象に、各候補について `merge-base <candidate> HEAD` → `rev-list --count <merge-base>..<candidate>` を計算し、距離が最小のもの（≒ 最も近い親ブランチ）を選ぶ。remote を優先し、remote になければローカルブランチを見る。同距離の場合は **候補リストの先頭優先** で、多段ブランチ運用（`main` + `stage` 等）で両者が同一コミットを指すときは `stage` 系を選ぶ
4. **Fallback: `main`** — 候補が 1 つも存在しない場合

### スキル側の使い方

- Usage に `--base <branch>` 引数を追加する（明示指定を受け付ける）
- Context 収集の冒頭で resolver を呼び `$BASE` に格納する
- 差分収集 (`git log`, `git diff`) / プレビュー / `gh pr create` のすべてで `$BASE` を使う
- 「ベースブランチ: main」のような固定表記はしない（`ベースブランチ: $BASE` と表現する）

### 検証手順

| 運用パターン                                                     | 期待動作                                |
| ---------------------------------------------------------------- | --------------------------------------- |
| `main` only のリポジトリ                                         | `$BASE = main`                          |
| `main` + `stage` で `stage` から切った feature branch            | `$BASE = stage`                         |
| `main` + `stage` で `main` から切った feature branch (divergent) | `$BASE = main`                          |
| `main` + `stage` が同一コミットを指す状態 (tie-break)            | `$BASE = stage`（候補リストの先頭優先） |
| `--base release` を明示指定                                      | `$BASE = release`（他条件を無視）       |
| `AI_ORCHESTRA_BASE_BRANCH=develop`                               | `$BASE = develop`（明示指定がなければ） |

自動テストは `tests/unit/test_resolve_base_branch.py` が担保する。
