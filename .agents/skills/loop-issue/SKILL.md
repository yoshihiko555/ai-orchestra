---
name: loop-issue
description: 'GitHub Issue 番号を受け取り、loop-harness（LP-1）で実装→検証→修正の反復ループを駆動する。

  合格後は pr-create 資産で PR を作成し、外部レビュー対応反復まで自動で継続する。

  トリガー: /loop-issue

  '
metadata:
  short-description: Issue 消化ループ（伴走型自律反復）
---

# CLI Language Policy

**外部 CLI（Codex CLI / Antigravity CLI）と連携するスキルで守るべき共通ルール。**

## 言語プロトコル

| 対象                           | 言語       |
| ------------------------------ | ---------- |
| Codex / Antigravity への質問   | **英語**   |
| Codex / Antigravity からの回答 | **英語**   |
| ユーザーへの報告               | **日本語** |

## Config-Driven ルーティング

CLI ツールの利用可否と設定は `cli-tools.yaml` で一元管理する。

### 読み込み手順

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `{tool}.enabled` を確認する（`false` なら `claude-direct` にフォールバック）
4. `agents.{name}.tool` で実行先を決定する

### ルーティング規則

| `agents.{name}.tool` | 動作                                                                              |
| -------------------- | --------------------------------------------------------------------------------- |
| `codex`              | Codex CLI を使用                                                                  |
| `antigravity`        | Antigravity CLI（`agy`）を使用（旧値 `gemini` は読み替え）                        |
| `claude-direct`      | 外部 CLI を呼ばず Claude で処理                                                   |
| `auto`               | タスク種別に応じて選択（深い推論 → Codex、調査 → Antigravity、単純作業 → Claude） |

## サンドボックス実行

外部 CLI（Codex / Antigravity）は sandbox 内で直接実行する。
エラー時は `claude-direct` にフォールバックする。

---

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

| セクション   | 入力ソース               | 記述ルール                                                                             |
| ------------ | ------------------------ | -------------------------------------------------------------------------------------- |
| Summary      | コミット履歴 + diff stat | 変更内容を箇条書きで要約                                                               |
| Testing      | テスト実行結果           | 実施済みなら結果を記載、未実施なら理由を記載                                           |
| Release Note | 変更内容の分析           | ユーザー向け変更がある場合のみ記載（粒度・取捨選択は `changelog-policy` ルールに従う） |
| Checklist    | 自動チェック             | 可能な項目は事前にチェック済みにする                                                   |

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

---

# Tiered Review Output Contract

**レビュー系スキルの段階別出力形式。**

## フォーマット

```markdown
## Review Summary

**レビュアー**: {選定されたレビュアー一覧}
**変更ファイル**: {ファイル数} files, {追加行数} insertions(+), {削除行数} deletions(-)

### Critical ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 影響 + 修正案}
  ```{lang}
  {コードスニペット}
  ```

### High ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 修正案}

### Medium ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}

### Low ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}
```

## 重要度の定義

| 重要度 | 基準 | 対応 |
|--------|------|------|
| **Critical** | セキュリティ脆弱性、データ損失リスク、本番障害の可能性 | 必ず修正してから次に進む |
| **High** | バグの可能性、設計上の問題、パフォーマンス劣化 | ユーザーに確認（AskUserQuestion） |
| **Medium** | コード品質、可読性、軽微な改善 | 報告のみ。修正は任意 |
| **Low** | スタイル、命名、コメント改善 | 報告のみ。修正は任意 |

## 集約ルール

### 重複指摘の統合

複数レビュアーが同一ファイル・同一箇所を指摘した場合:

- severity が最も高いものを採用する
- 他のレビュアー名を `[{reviewer1}, {reviewer2}]` で併記する
- 異なる観点の指摘（例: security と performance）は別エントリとして残す

### 詳細度

- **Critical / High**: 詳細な説明 + 影響範囲 + 修正案（コードスニペット付き）
- **Medium / Low**: 1行サマリのみ

---

# Loop Issue — Issue 消化ループ（LP-1）

**GitHub Issue を起点に、loop-harness の LP-1（セッション内伴走）で Maker → Checker → 修正反復 → PR レビュー対応を駆動します。**

## Usage

```text
/loop-issue 42                         # 新規 Issue を開始
/loop-issue --attach <loop_id>         # クラッシュ・セッション断絶後に再接続
/loop-issue --resume <loop_id>         # failed / stopped から人間判断で再挑戦
```

このスキルが扱うのは LP-1 のみ。LP-2 の daemon / scheduler / status は起動・実装しない。

## 実行プロトコル（MUST）

実行開始時に一度だけ、実 CLI の絶対参照を定義する。以後、`loop_step` の各 subcommand は必ずこの
変数を使って起動し、PATH 上の同名コマンドや current shell の cwd に依存しない。

```bash
LOOP_STEP="$AI_ORCHESTRA_DIR/packages/loop-harness/scripts/loop_step.py"
```

### 起動時の入口選択

対象 `loop_id` の状況に応じて、次の 3 つの入口から **1 つだけ**を呼ぶ。

| 状況                                                                                  | 呼ぶコマンド                                                                     |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| 新規 Issue（state 未存在）                                                            | `python3 "$LOOP_STEP" start --issue <N> --project <project_root>`               |
| 既存ループの再開（前回セッションがクラッシュ・断絶し `lease_token` を保持していない） | `python3 "$LOOP_STEP" attach --loop-id <id> --project <project_root>`           |
| 正規に `failed` / `stopped` で終了したループを、人間判断で再挑戦                      | `python3 "$LOOP_STEP" resume --loop-id <id> --reset-counters --project <root>` |

3 入口の応答 JSON はすべて、内部で `propose` 済みの **最初の proposal** として扱う。応答の
`lease_token` を保持し、以後の `propose` / `complete` / `reconcile` / `heartbeat` / `run-checker`
には同じ token を `--lease-token` で渡す。

`start` / `attach` / `resume` の直後に、アクションの実行と `complete` を挟まず `propose` を呼んでは
ならない。3 コマンドは最初の action をすでに `pending` として journal に記録している。直後に
`propose` を重ねると、未実行・未完了の action が孤立し、`reconcile` が孤立 `pending_action` として
扱うため、Maker 起動の欠落や `infrastructure_failure` への誤分類につながる。

### 入口応答の repo identity 検証と Issue 取得

入口応答を受けたら action の副作用より先に、`params.repo_identity_verified is true` を確認する。
`false` または欠落なら Issue 取得を含むすべての `gh` 操作を行わず、proposal の `action: stop` を確認して
「`stop` — 安全停止」の規則へ進む。もし非 `stop` action と矛盾していたら別 action を先取りせず、
リポジトリ副作用を伴わない失敗結果でその proposal を `complete` し、次の proposal の安全停止判断へ
委ねる。

`true` の場合だけ、応答の `params.issue_number`、`params.worktree_path`、`params.branch` をそのまま使う。
Issue 番号・worktree・branch を引数、loop id、current shell の cwd から再構成しない。対象 cwd を
`params.worktree_path` に固定したうえで、次の順に repository と Issue を取得する。

```bash
worktree_path="<params.worktree_path>"
issue_number="<params.issue_number>"
repo_json="$(cd "$worktree_path" && gh repo view --json nameWithOwner)"
issue_json="$(cd "$worktree_path" && gh issue view "$issue_number" --json number,title,body,labels)"
```

`issue_json` の labels は `.labels[].name` の文字列一覧へ正規化してから Maker 選定に渡す。生の
`body` は Maker prompt だけに利用し、Maker 選定の `detect_agent()` 入力（routing 入力）には含めない
（本文中の偶発的なキーワード一致による誤選定を防ぐため。EV-74）。`body` はユーザー応答、Issue
コメント、audit、Checker prompt、結果ファイルへも転載しない。`attach` / `resume` でも例外なく、この
入口応答の共通 `params` から同じ検証・取得経路を使う。

### two-phase サイクル

1. 入口応答、または `propose` 応答の `action` を確認する。
2. 応答の `action` に厳密に一致する処理だけを実行する。
3. 実行結果をファイルへ保存する。
4. 応答と同じ `action_id`、`state_version`、保持中の `lease_token` を使って完了する。

   ```bash
   python3 "$LOOP_STEP" complete \
     --loop-id <loop_id> \
     --action-id <応答の action_id> \
     --state-version <応答の state_version> \
     --result @<result_file> \
     --lease-token <保持中の lease_token> \
     --project <project_root>
   ```

5. `complete` 成功後に限り、同じ `lease_token` で次の `propose` を呼ぶ。

   ```bash
   python3 "$LOOP_STEP" propose \
     --loop-id <loop_id> \
     --lease-token <保持中の lease_token> \
     --project <project_root>
   ```

6. 新しい proposal について 1〜5 を繰り返す。

長時間処理中の lease 更新と、attach 時に孤立 pending の調停が必要な場合も同じ実 CLI を使う。

```bash
python3 "$LOOP_STEP" heartbeat \
  --loop-id <loop_id> \
  --lease-token <保持中の lease_token> \
  --project <project_root>

python3 "$LOOP_STEP" reconcile \
  --loop-id <loop_id> \
  --lease-token <保持中の lease_token> \
  --project <project_root>
```

`run_maker` / `run_checker` / `wait_external_review` / `advance_phase` に加え、終端 action の `stop` /
`exit_success` / `exit_failure` も、出口処理の実行後に必ず `complete` する。終端だからといって
`complete` を省略せず、孤立 `pending_action` を残さない。終端 action の `complete` 後は `propose` を
呼ばない。

### MUST NOT（禁止事項）

1. `start` / `attach` / `resume` の応答直後に、実行と `complete` を挟まず `propose` を呼ばない。
   孤立 `pending_action` を生む。
2. proposal が返した `action` と異なる処理を自己判断で実行しない。
3. `run_maker` が返ったのに `run_checker` や `exit_success` を実行するなど、反復上限・無進捗ガードや
   action を先取りしない。停止判断は proposal に委ねる。
4. `complete` を省略して次の `propose` へ進まない。終端 action も出口処理後に `complete` する。
5. 入口で取得した `lease_token` を保持せず省略したり、古い token、別ループの token を使ったり
   しない。同じ proposal の `action_id` / `state_version` も保持する。
6. 新規作成に `attach`、既存ループ再開に `start`、正常な断絶再開に `resume` を使うなど、3 入口を
   混同しない。
7. Maker / Checker の生出力をメインコンテキストやユーザー応答へ転載しない。要約と artifact /
   state / journal の参照だけを受け渡す。

action の実行に懸念があっても別 action を先取りしない。実際に試みた結果で `complete` し、継続・停止
は次の proposal のガード評価に委ねる。

## 状態源と Action 語彙

状態の正本は loop-harness が管理する state / journal であり、オーケストレーターが利用する状態源は
`loop_step` の JSON 応答だけとする。`state.json` を直接編集しない。proposal の `params` は state と
ループ定義から生成済みなので、worktree、branch、実行順、停止理由を独自に再構成しない。

すべての action の proposal は共通 context として `params.issue_number`、`params.worktree_path`、
`params.branch`、`params.repo_identity_verified` を供給する。`run_maker` だけでなく `run_checker`、
`wait_external_review`、`advance_phase`、`stop`、`exit_success`、`exit_failure` もこの cwd 供給を使う。
Task は cwd を明示し、すべての git は `git -C "<params.worktree_path>" ...` または同パスへ固定した
subshell、すべての `gh` / `pr-create` は同パスを明示した Task または subshell で実行する。current
shell の cwd に依存する git / gh / PR 操作は禁止する。

| Action                 | 実行内容                                                        |
| ---------------------- | --------------------------------------------------------------- |
| `run_maker`            | agent-routing で Maker を選定し、指定 worktree で Task 実行     |
| `run_checker`          | LLM 後、`python3 "$LOOP_STEP" run-checker` で検証・集約       |
| `wait_external_review` | 必要時だけ同 action で push し、決定論 API で待機・収集          |
| `advance_phase`        | `params.exec` 順を保ち baseline → push/PR → head を補助記録     |
| `stop`                 | リポジトリを変更せず安全停止通知                                |
| `exit_success`         | 成功コメント・通知を行い正常終了                                |
| `exit_failure`         | Draft PR、失敗コメント・通知を行い失敗終了                      |

## `run_maker`

### Maker の選定

`packages/agent-routing/hooks/route_config.py` の実 API を import して使用する。agent-routing の
`cli-tools.yaml` と loop-harness の config は別の設定源として扱う。

```python
import os
import sys

agent_routing_hooks = os.path.join(
    os.environ["AI_ORCHESTRA_DIR"], "packages", "agent-routing", "hooks"
)
loop_harness_lib = os.path.join(
    os.environ["AI_ORCHESTRA_DIR"], "packages", "loop-harness", "lib"
)
sys.path.insert(0, agent_routing_hooks)
sys.path.insert(0, loop_harness_lib)

from route_config import detect_agent, get_agent_tool, load_config
from loop_definition import load_config as load_loop_harness_config

params_worktree_path = params["worktree_path"]

# agent-routing 設定: cli-tools.yaml + cli-tools.local.yaml
routing_config = load_config({"cwd": params_worktree_path})
issue_text = f"{issue_title}\n{' '.join(issue_labels)}"  # 本文は含めない（誤検出対策。EV-74）
agent_name, trigger = detect_agent(issue_text)

if agent_name is None:
    # loop-harness 設定: loop-harness.yaml + loop-harness.local.yaml
    loop_harness_config = load_loop_harness_config(params_worktree_path)
    agent_name = loop_harness_config.get("maker", {}).get(
        "fallback_agent", "general-purpose"
    )

tool = get_agent_tool(agent_name, routing_config)
```

- `detect_agent()` で検出できた場合は、その `agent_name` を変更しない。
- 検出不能時だけ loop-harness config の `maker.fallback_agent` を使う。既定は `general-purpose`。
- `get_agent_tool()` は agent-routing config の `agents.<name>.tool` を解決する。返された `tool` を無視して
  別ツールへ固定せず、既存 agent-routing 経路で Task を起動する。
- `maker.fallback_agent` を `cli-tools.yaml` から読んだり、agent の tool を loop-harness config から
  読んだりしない。

### Maker Task

proposal の `params.worktree_path` と `params.branch` をそのまま Task に渡す。どちらも state 由来であり、
オーケストレーターや Maker が探索・推測・再構成してはならない。Task の cwd は
`params.worktree_path` に固定し、background process として起動しない。

```text
Task(subagent_type="{agent_name}", prompt="""
## タスク
Issue #{params.issue_number}: {issue_title} の実装または修正を行ってください。
Issue 本文: {issue_body}

## 実行コンテキスト（MUST）
- 作業ディレクトリ（cwd）: {params.worktree_path}
- 現在のブランチ: {params.branch}
- 反復: {iteration}
- 上記 cwd 以外の source repository では編集・コミットしないこと

## 権限境界（MUST）
- 許可するのは {params.worktree_path} 内の read / edit / test / local commit だけ
- `git push`、`gh`、remote の作成・更新、branch / worktree の作成・切替は禁止
- state / journal / artifact の直接編集は禁止。background process の起動も禁止
- push / PR 作成・更新は行わず、proposal が後で返す `advance_phase` / 出口 action に委ねること

## 冪等性契約（MUST）
- `git -C {params.worktree_path} log --oneline -5` と `git -C {params.worktree_path} diff` で
  既存 commit / diff を確認してから着手すること
- 前回反復の変更を二重に実装・コミットしないこと
- 既存 PR へ追加する場合は同じ local branch へ追加 commit するだけに留めること。push は
  `advance_phase` など proposal が明示した action だけが行う

## 直前反復の情報（2 回目以降のみ）
### 前回の機械検証の要約
{params.previous_check.mechanical}

### 前回の LLM レビュー指摘（Critical / High のみ）
{params.previous_check.critical_high}

Medium / Low やレビュー・コマンドの生出力は入力に展開しないでください。
完了時は、生出力を返さず、変更要約・commit の有無・artifact/state/journal の参照だけを返してください。
""")
```

初回は直前反復の節を省略する。2 回目以降に渡すのは前回の機械検証要約と Critical / High の要約だけ
とし、値は proposal の `params.previous_check.mechanical` と `params.previous_check.critical_high` だけから
取る。未定義のローカル変数、state 直接読み出し、レビュー生本文から再構成しない。raw JSON、コマンド
stdout/stderr、レビュー本文全体を Task prompt に貼らない。Task の返却も要約と参照だけに制限し、
Maker の生出力をユーザーまたはメインオーケストレーターへ返さない。

オーケストレーターは Maker の要約を 0600 の result file に正規化し、少なくとも
`maker: {agent: <agent_name>, tool: <get_agent_tool の値>}`、変更・commit の短い要約、artifact / state /
journal の参照を保存する。この result file を改変せず
`python3 "$LOOP_STEP" complete --result @file ...` に渡し、
`loop_iteration` audit へ Maker の agent / tool と要約・参照を記録させる。Maker 自身は state / journal /
artifact や audit を直接編集しない。

## `run_checker`

### レビュアー選定

`implementation` フェーズの LLM レビューは変更がドキュメントのみでも省略禁止。必ず次を実行する。

1. `code-reviewer` をベースラインとして必ず含める。
2. `git -C "<params.worktree_path>" diff --stat <base>..HEAD` の **ファイルパスだけ**を
   `.claude/rules/skill-review-policy.md` のパスパターンへ照合する。
3. 必要なら専門レビュアーを追加し、合計最大 2 名にする。
4. diff の追加行・内容によるスキャンは行わない。
5. 合格条件は `critical == 0` かつ `high == 0`。Medium / Low だけなら LLM 層は合格とする。

### LLM レビュー結果ファイル

最初に `umask 077` を設定し、レビュアーごとに `mktemp` で一時ファイルを割り当ててパスを Task に
渡す。作成直後と Task 完了後に、regular file、symlink ではないこと、permission が 0600、サイズが
1 MiB（1048576 bytes）以下であることを検証する。1 つでも満たさなければ、その内容を読まず当該
reviewer を infrastructure failure とする。レビュアーは Tiered Output を reviewer ごとの
`lc.CheckResult` に正規化し、`lc.check_result_to_dict()` 互換 JSON としてそのファイルへ保存する。
保存直前に `lc.redact()` を適用する。

```bash
umask 077
review_result_1="$(mktemp "${TMPDIR:-/tmp}/loop-llm-review.XXXXXX")"
python3 -c 'import os, stat, sys; s = os.lstat(sys.argv[1]); ok = stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600 and s.st_size <= 1048576; raise SystemExit(0 if ok else 1)' "$review_result_1"
```

```text
Task(subagent_type="{reviewer}", run_in_background=true, prompt="""
Issue #{params.issue_number}、反復 {iteration} の変更を {params.worktree_path} でレビューしてください。
対象ファイルは次のパス一覧です: {changed_paths}

- cwd を {params.worktree_path} に固定し、別 worktree / repository を参照しないこと
- Critical / High / Medium / Low で分類すること
- 結果を lc.CheckResult(layer="llm_review", ...) として構成すること
- lc.check_result_to_dict() 互換 JSON を redaction 後に 0600 の {review_result_path} へ保存すること
- Task の返却には severity 件数の要約と {review_result_path} だけを含めること
- finding 本文やレビュー生出力をメインコンテキストへ返さないこと
""")
```

複数レビュアーは並列実行する。ただし `code-reviewer` は必須で合計最大 2 名を変えない。Task 返却から
finding 本文を収集せず、件数要約とファイルパスだけを受け取る。Task の timeout、例外、空出力、
不正 JSON、上記 file 検証失敗があっても、その reviewer を引数から省略しない。該当 reviewer 名で
`lc.CheckResult(passed=False, layer="llm_review", findings=[],
infrastructure_failure=True, ...)` を構成し、`lc.check_result_to_dict()` で同じ専用 file へ決定論的に
保存する。成功扱いの JSON や finding を手書きして補わない。

### 決定論的な Checker 集約

機械検証をオーケストレーターが独自に実行したり、集約済み `CheckResult` を手書きしたりしない。同じ
proposal の識別子で `python3 "$LOOP_STEP" run-checker` を呼ぶ。`--llm-result` は
`<reviewer>=@<file>` の形式でレビュアーごとに繰り返す。

```bash
umask 077
checker_result_file="$(mktemp "${TMPDIR:-/tmp}/loop-check-result.XXXXXX")"
python3 -c 'import os, stat, sys; s = os.lstat(sys.argv[1]); ok = stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600 and s.st_size <= 1048576; raise SystemExit(0 if ok else 1)' "$checker_result_file"

python3 "$LOOP_STEP" run-checker \
  --loop-id "$loop_id" \
  --action-id "$action_id" \
  --state-version "$state_version" \
  --lease-token "$lease_token" \
  --llm-result "code-reviewer=@$review_result_1" \
  --llm-result "security-reviewer=@$review_result_2" \
  --project "$project_root" \
  > "$checker_result_file"
```

レビュアーが 1 名なら、存在しない 2 個目の `--llm-result` は指定しない。ただし必須の
`code-reviewer=@...` は常に指定する。専門レビュアー名は選定結果に合わせ、例の
`security-reviewer` へ固定しない。

`run-checker` は state / loop definition 由来の cwd と commands を使い、
`lc.run_mechanical_checks()` → `failure_detector.analyze()` の決定論経路で機械検証を実行する。その後
LLM 結果を `pass_criteria: {critical: 0, high: 0}` で集約する。`run-checker` は named
`--llm-result` から reviewer manifest を作り、`code-reviewer` 必須・重複なし・合計最大 2 名を検証した
reviewer 名一覧を `check_result.json` の metadata に封印して artifact を保存する。オーケストレーターが
reviewer manifest、metadata、集約結果を手書き・差し替えしてはならない。

stdout は上記ファイルへ保存し、JSON の並べ替え・ラップ・要約・手修正を一切しない。`run-checker` が
同じ action 用に保存した `check_result.json` artifact と一致する stdout result だけを、そのまま
`python3 "$LOOP_STEP" complete --result @"$checker_result_file" ...` へ渡す。artifact 不一致、CLI 失敗、
欠落時は手書き result や `complete` の直接呼び出しで迂回せず、決定論経路の失敗として扱う。
`complete` は artifact 本文だけでなく封印済み reviewer manifest も validator に通す。クラッシュ後に
artifact から復旧する `reconcile` も同じ validator を必ず通し、不正・欠落した manifest の artifact を
適用しない。

## `wait_external_review`

独自の `gh` ポーリング、reviewer 判定、severity 分類、dedup を実装しない。
`packages/loop-harness/lib/pr_review_wait.py` の次の決定論 API をそのまま使用する。

- `load_pr_review_config(params.worktree_path)`
- repo identity 検証済みの repository で構成した `GhApiClient`
- `wait_for_completion(...)`
- `record_ignored_untrusted_reviews(...)`
- `collect_review_findings(...)`
- `phase_check_from_completion_outcome(...)`
- `phase_check_from_review_findings(...)`

`wait_external_review` proposal の `params.push_required` は、この pending action 内で review 待機前の
追加 push が必要かを示す bool である。値を独自に推測せず、次の 2 経路だけを実行する。

- `params.push_required is true`: `pr_review_response` の Maker が同じ local branch へ追加 commit した後の
  経路。同じ `wait_external_review` action 内で、`params.worktree_path` に cwd を固定し、
  `record_baseline(..., action_id=<現在の action_id>)` を push 前に実行する。repo identity と branch guard
  を再検証してから `params.verified_branch` を一字も変更せず push し、push 後に
  `record_iteration_head(..., action_id=<現在の action_id>)` を実行する。そのまま wait / poll / collect へ
  進み、元 proposal と同じ `state_version` で `complete` する。
- `params.push_required is false`: 初回 PR 作成直後など、対象 commit がすでに push 済みの経路。
  `advance_phase` が保存した既存 baseline / iteration head を使って poll から開始する。baseline の再記録、
  re-push、iteration head の上書きを行わない。

`params.verified_branch` は loop-harness が push guard を通す対象として proposal に供給した branch であり、
現在 branch や `params.branch`、Issue 情報から再構成しない。`push_required is true` の push 直前に、
`git -C "<params.worktree_path>" branch --show-current` が `params.branch` および
`params.verified_branch` と厳密一致し、repo identity も引き続き一致することを branch guard で確認する。
guard 不合格なら push / poll を先取りせず、同じ action を失敗結果で `complete` して次 proposal の停止
判断へ委ねる。`push_required` が欠落・bool 以外なら安全側に失敗させ、どちらかを推測しない。

`wait_for_completion()` の heartbeat callback から保持中 token を使って
`python3 "$LOOP_STEP" heartbeat` を呼ぶ。完了シグナルが得られたら `collect_review_findings()` で許可済み
発信元だけを取り込み、`phase_check_from_review_findings()` で `PhaseCheckResult` に変換する。timeout /
API error は `phase_check_from_completion_outcome()` で変換する。独自の `gh` polling は実装しない。

`record_baseline(...)`、`record_iteration_head(...)`、`collect_review_findings(...)` は、Python 側で
`state_version` を変更しない補助更新として実装された API だけを使う。各 API は対応する同じ pending
action の `action_id` / `lease_token` と `params.worktree_path` を渡して呼べるが、proposal の
`state_version` を取り直したり加算したりしない。`complete` は常に元 proposal と同じ
`state_version` を渡す。action_id-bound API は現在の pending `action_id` と一致する場合だけ補助更新を
許可し、旧 action の id は stale として拒否する。`action_id=None` の legacy mode は互換性のため
`state_version` を increment するので、このスキルでは使用禁止。3 API すべてへ現在の `action_id` を
必ず渡す。state / journal を直接編集してこの契約を模倣しない。

repo identity 検証済みの `params.worktree_path` を cwd にして `GhApiClient` を構成し、current shell の
cwd や独自の `gh` polling に依存しない。各 API の戻り値は既存の `phase_check_from_*()` と
`lc.phase_check_to_dict()` で ready-to-complete JSON に変換し、0600 の結果ファイルへ保存する。その
ファイルを同じ action の `action_id` / proposal `state_version` / `lease_token` で改変せず
`python3 "$LOOP_STEP" complete --result @file` する。API が保存した要約、signature、artifact /
journal 参照だけをメインへ返し、外部レビューコメント全文や生 API 応答を転載しない。

## `advance_phase`

proposal の `params.exec` を正本とし、記載順を変更・省略しない。既定の implementation 成功経路は
次の順序で実行する。

1. `commit`: worktree の既存 commit / diff を確認し、未コミット差分だけを commit する。Maker が
   commit 済みなら二重 commit しない。
2. `record_baseline`: push / PR 作成より前に、同じ pending action の補助更新として baseline を記録する。
3. `push`: `params.verified_branch` を対象に push 前ガードを通し、既存リモート branch へ push する。
4. `pr_create`: `pr-create` 資産を再利用して PR を作成する。
5. `record_iteration_head`: push / PR 作成後に、同じ pending action の補助更新として head を記録する。

proposal が `advance_phase` を返した時点では、`loop_step` 自体は commit / push を実行していない。
「push 済み」と誤認せず、`params.exec` の `push` を実行する。PR 作成はこの action 内だけで行う。

commit / push / PR 操作は `params.worktree_path` へ cwd を固定する。git は
`git -C "<params.worktree_path>" ...` または同パスの subshell、`gh` / `pr-create` は同パスを明示した
Task / subshell で実行し、current shell の cwd を使わない。`pr-create` には proposal の
`params.verified_branch` を **一字も組み替えずそのまま**対象 branch として渡す。現在 branch、
`params.branch`、Issue タイトルから別の branch 名を組み立てない。`--issue {params.issue_number}` を渡し、
既存 PR があれば新規作成せず継続する。auto-merge は有効化せず、worktree は保持する。

`pr_review_response` の Maker は同じ local branch への追加 commit までを行う。その後の再 push 用に
`advance_phase` は返らないため、再 push をこの action で先取りしない。次の proposal は
`wait_external_review` であり、その `params.push_required` に従って上記 `wait_external_review` 節の
同一 action 内経路で push と待機を行う。

PR 作成結果の `pr_number` と `params.next_phase` を結果 JSON に含め、補助 API 呼び出し前の proposal と
同じ `state_version` で `complete` する。`exit_success` で PR を二重作成しない。

PR 作成は次の Task テンプレートで実行する。`verified_branch` と cwd は proposal の値をそのまま渡し、
Task 側で探索・再構成しない。

```text
Task(subagent_type="general-purpose", prompt="""
`pr-create` スキルの Step 1〜4 に従い、implementation 成功時の PR を作成または再利用してください。

- cwd: {params.worktree_path}
- 対象ブランチ: {params.verified_branch}
- Issue: {params.issue_number}
- proposal の params.exec にある commit / record_baseline / push は実行済みです。追加 commit が無ければ
  pr-create の push は繰り返さないでください
- 既存 PR があれば新規作成せず、その PR を返してください
- auto-merge を有効化せず、worktree を保持してください

返却は PR 番号・URL・Open/Draft 状態の短い要約だけにし、Issue本文やコマンド生出力を含めないでください。
""")
```

## `exit_success`

1. 既存 PR と反復履歴・Checker 結果を確認する。新しい PR は作らない。
2. 下記「通常終了の Issue コメント」に `PASSED` と要約を入れ、`params.issue_number` の対象 Issue へ
   投稿する。
3. macOS 通知を発火する。
4. 投稿・通知の直前に redaction を適用する。
5. auto-merge は付与せず、worktree を保持する。
6. 出口処理の結果を同じ proposal 識別子で `python3 "$LOOP_STEP" complete ...` し、終了する。

マージ判断は人間が行う。

## `exit_failure`

proposal の `params.draft_pr_exec` を順序どおり実行する。

- PR が無ければ `params.worktree_path` を cwd に明示した `pr-create` 資産を再利用して Draft PR を作成する。
- 既存 PR があれば新規作成せず Draft に戻す。`pr_review_response` では `gh pr ready --undo` 相当を
  `params.worktree_path` の subshell で使用する。
- 反復履歴・Checker 結果を記録し、下記「通常終了の Issue コメント」に失敗理由を入れて投稿する。
- macOS 通知を発火する。
- auto-merge は付与せず、worktree を保持する。
- 投稿・通知の直前に redaction し、完了結果を `python3 "$LOOP_STEP" complete ...` して終了する。

Draft PR の作成または既存 PR の Draft 化は次の Task テンプレートで実行する。proposal に PR 番号が
無い場合だけ `pr-create` を使い、既存 PR がある場合は同じ PR を Draft に戻す。

```text
Task(subagent_type="general-purpose", prompt="""
`exit_failure` proposal の失敗出口を、次の固定コンテキストで処理してください。

- cwd: {params.worktree_path}
- 対象ブランチ: {params.branch}
- Issue: {params.issue_number}
- 既存 PR: {params.pr_number}
- params.draft_pr_exec の順序を変更・省略しないでください

既存 PR が無い場合は `pr-create` スキルを再利用して Draft PR を作成してください。既存 PR がある場合は
新規作成せず、cwd を固定した `gh pr ready --undo` 相当で同じ PR を Draft に戻してください。
auto-merge を有効化せず、worktree を保持してください。返却は PR 番号・URL・Draft 状態、実行した
params.draft_pr_exec の短い要約だけにし、レビュー本文やコマンド生出力を含めないでください。
""")
```

## 通常終了の Issue コメントと通知

```markdown
## Loop 実行結果: {PASSED | FAILED (max_iterations reached) | FAILED (no_progress)}

**Loop ID**: `{loop_id}`
**フェーズ**: {implementation | pr_review_response}
**総反復回数**: {iteration_count}
**PR**: {pr_url}（{Open | Draft}）

### 反復サマリ

| # | フェーズ | Checker 結果 | 停止/継続理由 |
| --- | -------- | -------------- | ------------- |
| {iteration} | {phase} | {severity 件数・失敗種別だけの要約} | {reason} |

### 無視した非許可指摘

- {count} 件（許可リスト不一致のため対象外。詳細は journal 参照）

### 次のアクション

{FAILED: Draft PR を確認し、手動対応するか `python3 "$LOOP_STEP" resume --loop-id <loop_id> --reset-counters --project <project_root>` で再開してください}
{PASSED: マージ判断は人間が行ってください（auto-merge は付与されません）}
```

```bash
osascript -e 'display notification "{結果: 成功/失敗} — 反復 {n} 回, 停止理由: {stop_reason_code}" with title "Loop Issue #{params.issue_number}" sound name "Glass"'
```

Issue コメントには severity 件数・失敗シグネチャ種別だけを載せ、レビュー本文やコマンド生出力を
載せない。通知は Issue 番号・結果・停止理由コードの件名レベルに限定する。通常終了の Issue / PR
操作も `params.repo_identity_verified is true` を確認し、`params.worktree_path` を cwd に固定してから
行う。current shell の cwd で `gh` / `pr-create` を呼ばない。

## `stop` — 安全停止

`stop` は通常の成功・失敗出口とは別に扱う。proposal の `params.stop_reason` は正規化済みコードなので、
変換・言い換えせずそのまま報告・通知・結果 JSON に使う。

安全停止では source repository のファイル編集、commit、push、PR 作成、PR 更新、Draft 化を一切
行わない。macOS 通知は停止理由や repo identity の判定可否にかかわらず **常時発火**する。Issue
コメントは `params.repo_identity_verified is true` と厳密に確認できる場合だけ、
`params.worktree_path` を cwd に固定して投稿する。値が `false`、欠落、型不正の場合、
または `params.stop_reason == "repo_identity_mismatch"` の場合は投稿禁止とする。
`params.stop_reason == "foreign_live_lease"` であること自体は投稿禁止条件ではない。この場合も
`params.repo_identity_verified is true` なら仕様どおり投稿し、`false` / 欠落なら投稿しない。

```bash
osascript -e 'display notification "安全停止: {params.stop_reason}" with title "Loop Issue #{params.issue_number} — SAFETY STOP" sound name "Basso"'
```

`params.repo_identity_verified is true` の場合だけ、次のテンプレートを `params.issue_number` の Issue に
投稿する。

```markdown
## Loop 安全停止: {params.stop_reason}

**Loop ID**: `{loop_id}`
**フェーズ**: {implementation | pr_review_response}
**発生時刻**: {timestamp}

このループは安全機構（push 前ガード / repo-identity 照合 / lease 排他制御）により停止しました。
リポジトリへの書き込み（push / PR 作成・更新）は行われていません。

### 次のアクション

状況を確認し、問題を解消した上で
`python3 "$LOOP_STEP" resume --loop-id <loop_id> --reset-counters --project <project_root>` で再開するか、
手動で対応してください。
```

macOS 通知と条件付き Issue コメントの本文を組み立てた後、表示・投稿 API 呼び出しの直前に redaction
を適用する。通知完了後はリポジトリ変更を含まない結果で、同じ action を
`python3 "$LOOP_STEP" complete ...` し、終了する。

## コンテキスト分離と機密保護（EV-44 / NF-05）

- Maker / Checker Task の返却は件数・変更・合否の短い要約と artifact / state / journal 参照だけにする。
- Maker のコマンドログ、Checker の finding 本文、外部レビューコメント、API 生応答をメイン
  コンテキストやユーザー応答へ転載しない。
- raw は loop-harness が 0600・redaction 付きで管理する artifact にだけ保存する。
- `state.json` を直接編集せず、`loop_step` JSON を唯一の操作上の状態源とする。
- Issue コメント・macOS 通知は本文組み立て後、送信直前に redaction する。
