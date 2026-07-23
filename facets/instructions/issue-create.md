# Issue Create — GitHub Issue の作成

**引数から Issue の種類とタイトルを判定し、テンプレートに沿って GitHub Issue を作成します。**

## Usage

```
/issue-create bug ログインエラー
/issue-create feature ダークモード対応
/issue-create task CI パイプライン整備
/issue-create                          # 対話的に種類・タイトルを決定
```

## Context 収集

スキル実行時に以下の情報を収集する:

```bash
# 現在のブランチとリモート URL
git branch --show-current
git remote get-url origin

# リポジトリの既存ラベル一覧
gh label list --json name,description --limit 100
```

## Workflow

### Step 1: 引数の解析

`$ARGUMENTS` から種類とタイトルを判定する。

| パターン | 種類 | 例 |
|---------|------|-----|
| `bug ...` | bug | `/issue-create bug ログインエラー` |
| `feature ...` | feature | `/issue-create feature ダークモード` |
| `task ...` | task | `/issue-create task CI整備` |
| 引数なし | — | AskUserQuestion で種類・タイトルをヒアリング |

種類またはタイトルが不足している場合は AskUserQuestion で確認する。

### Step 2: テンプレートに基づく本文作成

種類に応じたテンプレートで本文を構成する。

#### bug テンプレート

```markdown
## バグの概要

{タイトルから推定、またはユーザーに確認}

## 再現手順

1. {ユーザーに確認}

## 期待される動作

{ユーザーに確認}

## 実際の動作

{ユーザーに確認}

## 受け入れ条件（修正完了とみなす条件）

- [ ] {再現手順を実行しても問題が再発しない条件} — judge: {判定基準。例: 上記「再現手順」を再実行し「期待される動作」と一致する}
- [ ] {回帰テストが追加され pass する条件} — verify: `{テストコマンド}`

## 環境

- OS: {自動検出}
- ブランチ: {自動検出}
```

#### feature テンプレート

```markdown
## 概要

{タイトルから推定}

## モチベーション

{ユーザーに確認}

## 提案する実装

{ユーザーに確認、または「未定」}

## 受け入れ条件

- [ ] {機能が意図通り動作する条件} — verify: `{テストコマンド}`
- [ ] {UI/UX 等の主観的な条件} — judge: {判定基準}
```

#### task テンプレート

```markdown
## タスク内容

{タイトルから推定}

## 完了条件

- [ ] {タスクの成果物が動作する条件} — verify: `{確認コマンド}`
- [ ] {レビュー観点等の主観的な条件} — judge: {判定基準}

## 備考

{任意}
```

### Step 3: 受け入れ条件のヒアリング（必須）

**種類（bug / feature / task）を問わず、受け入れ条件（Acceptance Criteria）の明記は必須。** 空欄や「{ユーザーに確認}」のまま次に進めない。

1. AskUserQuestion で受け入れ条件をヒアリングする。1 回の質問で聞く項目は **2〜3 個まで**（`dialog-rules` ポリシー準拠）。
2. 条件は次の 2 種類に書き分ける:
   - 機械検証可能な条件 → `- [ ] {条件} — verify: \`{コマンド}\``
   - 主観的な条件 → `- [ ] {条件} — judge: {判定基準}`
3. **最低 1 件は必須**。ユーザーが「わからない」と答えた場合も、AI がタイトル・本文の内容から候補（verify/judge どちらか）を提示し、ユーザーの合意を取ってから確定する。空欄のまま Step 4 に進まない。
4. 登録時は全条件を未チェック `- [ ]` で記載する。

> 記法の正本は `.claude/rules/task-memory-usage.md`。ここで確定した AC はそのまま Plans.md の `#### Acceptance Criteria` へ転記できる形式にする。

### Step 4: プレビューと確認

**役割**: プレビュー時の早期検査。不備があればここで Step 3 に差し戻し、ヒアリングをやり直す（Step 5 のゲートに到達する前に大半の不備をここで検出する）。

1. 作成する Issue のプレビューを表示する:
   - タイトル
   - ラベル
   - 本文
   - 受け入れ条件（件数と内訳: verify 件数 / judge 件数）
2. 確認前に AC を検査する:
   - 受け入れ条件が 1 件以上あるか
   - 各行に `— verify:` または `— judge:` が付記されているか
   - 受け入れ条件に未置換の `{...}` 形式のプレースホルダ（`{ユーザーに確認}` `{テストコマンド}` `{判定基準}` 等を含む）が 1 つでも残っていないか（`## 環境` の `{自動検出}` 等、受け入れ条件以外のプレースホルダは検査対象外）
   - 上記を満たさない場合は Step 3 に戻ってヒアリングをやり直す
3. AskUserQuestion で確認:
   - 「このまま作成」
   - 「修正してから作成」
   - 「キャンセル」

### Step 5: Issue 作成

**役割**: 作成直前の最終ゲート（フェイルセーフ）。Step 4 の検査をすり抜けた不備を、実際に `gh issue create` を実行する直前でもう一度捕捉する。

**ゲート（必須）**: `gh issue create` を実行する前に、以下のいずれかに該当する場合は **Issue を作成せず Step 3 に戻る**。このゲートは、ユーザーが当該 Issue について受け入れ条件の省略を明示的に指示した場合に限り免除される。AI の判断による免除は行わない。

- 受け入れ条件が 0 件
- 未置換の `{...}` 形式のプレースホルダ（`{ユーザーに確認}` `{テストコマンド}` `{判定基準}` 等を含む）が受け入れ条件に 1 つでも残っている

```bash
# ラベルが存在しない場合は作成
gh label create "bug" --description "バグ報告" --color "d73a4a" 2>/dev/null || true
gh label create "feature" --description "新機能" --color "a2eeef" 2>/dev/null || true
gh label create "task" --description "タスク" --color "0075ca" 2>/dev/null || true

# Issue 作成
gh issue create --title "{タイトル}" --body "{本文}" --label "{種類}"
```

### Step 6: 結果報告

作成された Issue の番号と URL を報告する。

```
Issue #42 を作成しました: https://github.com/owner/repo/issues/42
```

## 注意事項

- `gh` コマンドは認証済みであることを前提とする
- ラベル作成に失敗しても Issue 作成は続行する（権限不足の場合）
- 本文が長すぎる場合はファイル経由で渡す（`--body-file`）
- 説明・出力は日本語で行う
- 受け入れ条件の記法は `.claude/rules/task-memory-usage.md` に従う
