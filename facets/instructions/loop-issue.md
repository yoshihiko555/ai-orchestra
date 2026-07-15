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
persisted_agent = params.get("maker_agent")
if isinstance(persisted_agent, str) and persisted_agent != "auto":
    # 初回 complete で state に保存済み。再検出せず、全反復・全フェーズで再利用する。
    agent_name = persisted_agent
    trigger = "persisted"
else:
    # loop-harness 設定: loop-harness.yaml + loop-harness.local.yaml
    loop_harness_config = load_loop_harness_config(params_worktree_path)
    maker_config = loop_harness_config.get("maker", {})
    allowed_agents = set(maker_config.get("allowed_agents", []))
    fallback_agent = maker_config.get("fallback_agent", "general-purpose")
    if fallback_agent not in allowed_agents:
        raise RuntimeError("maker.fallback_agent must be included in maker.allowed_agents")
    issue_text = f"{issue_title}\n{' '.join(issue_labels)}"  # 本文は含めない（EV-74）
    agent_name, trigger = detect_agent(issue_text, allowed_agents)
    if agent_name is None:
        agent_name = fallback_agent

tool = get_agent_tool(agent_name, routing_config)
```

- `params.maker_agent` が `auto` 以外の具体値なら state に保存済みの Maker として再検出せず再利用する。
- 未選定時だけ `detect_agent(issue_text, allowed_agents)` を呼び、非許可ロールを飛ばして次候補を探す。
- 検出不能時だけ loop-harness config の `maker.fallback_agent` を使う。fallback は
  `maker.allowed_agents` に含まれていなければならず、既定は `general-purpose`。
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
- `detect_pr_review_push_delta(loop_id, params.worktree_path, params.worktree_path)`
- `no_new_commit_completion_outcome(delta)`
- `wait_for_completion(...)`
- `record_ignored_untrusted_reviews(...)`
- `collect_review_findings(...)`
- `save_review_findings_snapshot(...)`（active lease / action に束縛し、構造化 redaction 後の collect
  結果全体を action-scoped artifact へ 0600 で保存）
- `load_review_findings_snapshot(...)`（lease / action / envelope / ファイル境界を検証して厳格に復元）
- `classify_severity(...)`（Step 2 分類応答の決定論パース。下記「severity 分類」参照）
- `apply_severity_classifications(...)`（分類結果の state 永続化と finding 除外を一括適用）
- `phase_check_from_completion_outcome(...)`
- `phase_check_from_review_findings(...)`

`wait_external_review` proposal の `params.push_required` は、この pending action 内で review 待機前の
追加 push が必要かを示す bool である。値を独自に推測せず、次の 2 経路だけを実行する。

- `params.push_required is true`: `pr_review_response` の Maker が同じ local branch へ追加 commit した後の
  経路。まず `params.worktree_path` に cwd を固定し、repo identity と branch guard を再検証する。
  `params.verified_branch` は loop-harness が push guard を通す対象として proposal に供給した branch
  であり、現在 branch や `params.branch`、Issue 情報から再構成しない。branch guard は
  `git -C "<params.worktree_path>" branch --show-current` が `params.branch` および
  `params.verified_branch` と厳密一致し、repo identity も引き続き一致することを確認する。
  この時点ではまだ `detect_pr_review_push_delta()` を呼ばない。guard 不合格ならショートカットを検討せず、
  push / poll も先取りせず、`push_guard` を含む失敗結果（例: `{"push_guard": {...}}`）で同じ action を
  `complete` して、次 proposal の停止判断（safety stop / `push_guard_violation` /
  `repo_identity_mismatch`）へ委ねる。`push_required` が欠落・bool 以外の場合も同様に安全側で失敗させ、
  どちらかを推測しない。
  - **pre-rebaseline drain（guard 合格後、`record_baseline` / `detect_pr_review_push_delta()` より前に
    必ず実行する）**: 直前の反復（`advance_phase` または前回の `wait_external_review`）が記録した
    既存 baseline をそのまま変更せず、`collect_review_findings(...)` を実行する。これは、直前の Maker
    反復が作業している間に別の信頼済みレビュアーが投稿した、まだ import されていないレビュー（例:
    2 人目のレビュアーによる `CHANGES_REQUESTED`）を、次の `record_baseline` が「処理済み」として
    飲み込み永久に喪失させる前に取り込むための手順である。collect の直後、別プロセスへ移る前に
    `ReviewFindingsResult` 全体を `save_review_findings_snapshot(...)` で同じ action の
    `artifacts/<action_id>/review_findings.json` へ保存する。戻り値に `needs_classification` の finding
    が 1 件以上含まれる場合は、上記「severity 分類（Step 2）」の手順をこの場でインラインに適用し、
    `apply_severity_classifications(...)` まで完了させてから次の判定に進む（分類を次サイクルへ持ち越さない）。
  - drain（severity 分類後）に **critical/high** の finding が **1 件以上** 残る場合、`record_baseline`、push、
    `record_iteration_head`、wait / poll をこの action では一切実行しない。代わりに
    `phase_check_from_review_findings(...)` の戻り値（`passed: false` になるはず）をそのまま
    `lc.phase_check_to_dict()` で ready-to-complete JSON に変換し、0600 の result file として保存して、
    元 proposal と同じ `state_version` で `complete` する。`detect_pr_review_push_delta()` はこの分岐では
    呼ばない。この complete の結果は次の guard 評価で修正反復（Maker）へ差し戻される。medium/low のみが
    残っている場合はこの分岐に該当しない（下記へ進む。B 軸: medium/low は非ブロッキング）。
  - drain の結果 **critical/high の finding が 0 件**（medium/low のみ残存、または finding 自体が
    0 件）の場合に限り、`detect_pr_review_push_delta(loop_id,
    params.worktree_path, params.worktree_path)` を呼び、戻り値 `delta.status` で以下のとおり分岐する。
    **drain の critical/high が 0 件であることを確認せずに `phase_check_from_review_findings()` を呼んで
    complete することは禁止する**（critical/high finding が存在しない場合、
    `phase_check_from_review_findings()` は `passed: true` を返すため、push もレビュー待機も行わずに
    誤って合格扱いにしてしまう。medium/low が `open` のまま残っていても非ブロッキングとして合格しうる
    点に注意する）。
    - `delta.status == "no_new_commit"` の場合、Maker は push すべき新規 commit を作っていない。
      この場合は `record_baseline`、push、`record_iteration_head`、wait / poll / collect をすべて実行しない。
      代わりに `no_new_commit_completion_outcome(delta)` で `CompletionOutcome` を取得し、続けて
      `phase_check_from_completion_outcome(outcome)` で `PhaseCheckResult` へ変換し、
      既存の timeout / API error 経路と同じく `lc.phase_check_to_dict()` で ready-to-complete JSON に変換する。
      その JSON を 0600 の result file として保存し、元 proposal と同じ `state_version` で `complete` する。
      オーケストレーターが `CompletionOutcome` や `PhaseCheckResult` を手書きで構築することは禁止する。
      必ず上記 2 つの library function を呼び、その戻り値をそのまま通す。この分岐は既存の
      `pr_review_timeout` 無進捗経路（FT-13）上の純粋な高速化であり、新しい失敗カテゴリを導入しない。
      ショートカットは guard 合格 **かつ pre-rebaseline drain が 0 件** を前提条件とするため、
      guard やこの drain をすり抜けて無進捗扱いにしてはならない。
    - `delta.status == "new_commit"` または `"unknown"` の場合は、既存フローを続行する。すなわち
      `record_baseline(..., action_id=<現在の action_id>)` を push 前に実行し、`params.verified_branch` を
      一字も変更せず push し、push 後に `record_iteration_head(..., action_id=<現在の action_id>)` を実行する。
      手順冒頭で guard は確認済みであり、この経路では push 直前の再検証を重複実行しない。guard 確認と
      push の間に worktree を変更する操作は行わない。そのまま wait / poll / collect へ進み、元 proposal と
      同じ `state_version` で `complete` する。`"unknown"` は git コマンド失敗や `iteration_head_sha` 未記録
      などの安全側フォールバックであり、ショートカットとして扱ってはならない。
- `params.push_required is false`: 初回 PR 作成直後など、対象 commit がすでに push 済みの経路。
  `advance_phase` が保存した既存 baseline / iteration head を使って poll から開始する。baseline の再記録、
  re-push、iteration head の上書きを行わない。この経路は初回の baseline がまだ collect されていないため、
  pre-rebaseline drain の対象ではない。

`wait_for_completion()` の heartbeat callback から保持中 token を使って
`python3 "$LOOP_STEP" heartbeat` を呼ぶ。完了シグナルが得られたら `collect_review_findings()` で許可済み
発信元だけを取り込み、直後に `save_review_findings_snapshot(...)` で同じ action の snapshot を保存する。
下記「severity 分類（Step 2）」を適用してから
`phase_check_from_review_findings()` で `PhaseCheckResult` に変換する。ただし
`outcome.signal == "reviewer_unavailable"` の場合はレビュー取り込みへ進まず、その outcome を
`phase_check_from_completion_outcome()` へそのまま渡す。timeout / API error も同じ API で変換する。
変換後は `lc.phase_check_to_dict()` の ready-to-complete JSON を 0600 の result file に保存し、元 proposal
と同じ `state_version` で complete する。専用 outcome や stop reason を手書きせず、独自の `gh` polling
も実装しない。

CodeRabbit のレート制限応答を検知しても、Codex 等の別 reviewer allowlist entry または
`checkrun_allowlist` が構成されている場合、`wait_for_completion()` は既存 timeout まで正常シグナルを
待つ。正常シグナルが到着すれば通常レビューを優先し、到着しなかった場合だけ
`reviewer_unavailable` を返す。この待機をオーケストレーター側で短縮したり、CodeRabbit 応答だけを
見て独自に停止したりしない。
API error は `phase_check_from_completion_outcome()` で変換する。独自の `gh` polling は実装しない。

### severity 分類（Step 2）

`collect_review_findings()` の戻り値で `needs_classification` が `true` の finding（Step 1 の明示的
表記にマッチしなかったコメント。fail-safe で暫定 high）が 1 件以上ある場合、
`phase_check_from_review_findings()` を呼ぶ**前に**、設計 pr-review 編 §3.2 の分類を実行する。

1. `collect_review_findings()` を実行したプロセスで、その戻り値全体を直ちに
   `save_review_findings_snapshot(loop_id, params.worktree_path, <現在の action_id>, result, lease_token)` へ
   渡す。pre-rebaseline drain と通常の post-poll collect のどちらでも必須とし、分類 Task の起動を
   先行させない。
2. 対象 finding ごとに下記の分類専用 Task を起動する（読み取り専用・分類のみ。コメント本文は
   Task 側が `source_comment_id` から取得し、メインコンテキストへは 2 行の応答だけを返す）。
3. Task 応答を受け取る別プロセスでは再 collect せず、
   `load_review_findings_snapshot(loop_id, params.worktree_path, <現在の action_id>, lease_token)` で同じ
   action の `review_findings.json` を復元する。別 loop / action の envelope、active lease 不一致、現在の
   phase / `pending_action` が同じ `wait_external_review` でない場合、snapshot の欠損・不正 JSON・
   不正スキーマ、symlink・非 regular file・0600 以外・1 MiB 超過はすべて fail-closed とする。空の
   `ReviewFindingsResult` を補ったり state から再構成したり、汎用 artifact reader や直接のファイル読み取りで
   検証を迂回したり、`phase_check_from_review_findings()` へ進んだりしない。
4. Task 応答を `source_comment_id` キーの map に集め、復元した `result` とともに
   `apply_severity_classifications(..., action_id=<現在の action_id>)` へ渡す。同 API が内部で
   `classify_severity()` を呼び、応答のパース失敗・`CONFIDENCE: low` を安全側の high に確定する
   （設計 §3.3）。Step 3 相当の判定、finding の差し替え、state 更新を独自実装しない。
5. API が返す `ClassificationApplicationResult.review_findings` を
   `phase_check_from_review_findings()` に渡す。確定 severity は同じ signature の
   `state.pr_review["findings"]` に永続化され、`none` は state と戻り値の両方から finding を除外する。
6. `ClassificationApplicationResult.classifications` を JSON 化し、`severity is null` は `none` として
   0600 の `artifacts/<action_id>/severity_classifications.json` に保存する
   （設計 §3.2。reconcile 復元用）。
7. severity を手書きで決めない。Task 応答から直接 severity を採用せず、必ず上記 API を経由する。
   `needs_classification` が `false` の finding（Step 1 で確定済み）は再分類しない。

```text
Task(subagent_type="code-reviewer", prompt="""
[PR Review Comment Severity Classification — 読み取り専用・分類のみ]

あなたはコードを修正しません。次の PR レビューコメント 1 件を
critical / high / medium / low / none のいずれか 1 つに分類することだけが役割です。

- cwd: {params.worktree_path}（`gh` はこの cwd に固定して実行すること）
- PR: #{params.pr_number}
- 対象コメント: {finding.source_comment_id}
  （`issue_comment:<id>` は `repos/<owner>/<repo>/issues/comments/<id>`、
  `review_comment:<id>` は `repos/<owner>/<repo>/pulls/comments/<id>`、
  `review:<id>` は `repos/<owner>/<repo>/pulls/{params.pr_number}/reviews/<id>` を
  `gh api` で取得して本文を読むこと）

## 分類基準
- critical: セキュリティ脆弱性・データ損失・本番障害に直結する指摘
- high: バグの可能性・設計上の欠陥・重大なパフォーマンス劣化
- medium: コード品質・可読性・軽微な改善提案
- low: スタイル・命名・コメント表現の改善提案
- none: 修正要求を含まない肯定的・情報提供のみのコメント（finding ではない）

## 出力形式（これ以外のテキストを含めないこと。コメント本文を転載しないこと）
SEVERITY: <critical|high|medium|low|none>
CONFIDENCE: <high|low>
""")
```

`record_baseline(...)`、`record_iteration_head(...)`、`collect_review_findings(...)`、
`apply_severity_classifications(...)` は、Python 側で
`state_version` を変更しない補助更新として実装された API だけを使う。各 API は対応する同じ pending
action の `action_id` / `lease_token` と `params.worktree_path` を渡して呼べるが、proposal の
`state_version` を取り直したり加算したりしない。`complete` は常に元 proposal と同じ
`state_version` を渡す。action_id-bound API は現在の pending `action_id` と一致する場合だけ補助更新を
許可し、旧 action の id は stale として拒否する。`action_id=None` の legacy mode は互換性のため
`state_version` を increment するので、このスキルでは使用禁止。4 API すべてへ現在の `action_id` を
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
   投稿する。critical/high はゼロだが medium/low が `open`（未 dismiss）のまま残っている場合も
   `exit_success` に到達しうる（非ブロッキング。Issue #213 B 軸）。この場合、`params` が提供する
   `non_blocking_open`（全反復累積の非 dismissed medium/low 一覧）を「残存した非ブロッキング指摘」
   セクションへ列挙する。0 件ならセクション自体を省略する。
3. macOS 通知を発火する。
4. 投稿・通知の直前に redaction を適用する。
5. auto-merge は付与せず、worktree を保持する。
6. 出口処理の結果を同じ proposal 識別子で `python3 "$LOOP_STEP" complete ...` し、終了する。

マージ判断は人間が行う。残存した非ブロッキング指摘がある場合は、上記コメントの一覧を参考に
人間が任意で対応するかを判断する（ループ自体はそれを理由に失敗させない）。

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

### 残存した非ブロッキング指摘（Low/Medium）

{PASSED かつ `params.non_blocking_open` が 1 件以上ある場合のみ表示。0 件ならこのセクション自体を省略する}

| severity | path:line | 抜粋（200 字まで） |
| -------- | --------- | ------------------- |
| {medium\|low} | `{path}:{line}` | {レビュー本文の抜粋} |

### 次のアクション

{FAILED: Draft PR を確認し、手動対応するか `python3 "$LOOP_STEP" resume --loop-id <loop_id> --reset-counters --project <project_root>` で再開してください}
{PASSED: マージ判断は人間が行ってください（auto-merge は付与されません）。残存した非ブロッキング指摘がある場合は上記一覧を確認してください}
```

```bash
osascript -e 'display notification "{結果: 成功/失敗} — 反復 {n} 回, 停止理由: {stop_reason_code}" with title "Loop Issue #{params.issue_number}" sound name "Glass"'
```

Issue コメントには severity 件数・失敗シグネチャ種別だけを載せ、レビュー本文やコマンド生出力を
載せない（「残存した非ブロッキング指摘」の抜粋は 200 字までの短い引用に限り可とする）。通知は
Issue 番号・結果・停止理由コードの件名レベルに限定する。通常終了の Issue / PR 操作も
`params.repo_identity_verified is true` を確認し、`params.worktree_path` を cwd に固定してから
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

### 外部レビュアー利用不可の人間引き継ぎ

`params.stop_reason == "external_reviewer_unavailable"` の場合だけ、下記の専用通知とコメントを使用する。
この停止でも source repository の編集、commit、push、PR 作成・状態変更、Draft 化、auto-merge は一切
行わない。許可する GitHub 書き込みは、下記条件を満たすコメント投稿だけとする。レート制限コメント本文や
GitHub API 生応答は通知・コメント・結果 JSON に含めず、理由コードと Issue / PR / Loop の識別子だけを使う。

```bash
osascript -e 'display notification "外部レビュアーを利用できません。確認とマージ判断をお願いします" with title "Loop Issue #{params.issue_number} — HUMAN REVIEW REQUIRED" sound name "Basso"'
```

`params.repo_identity_verified is true` の場合だけ、既存 `params.pr_number` があればその PR、無ければ
`params.issue_number` の Issue に、`params.worktree_path` を cwd に固定して次を投稿する。

```markdown
## Loop 停止: external_reviewer_unavailable

**Loop ID**: `{loop_id}`
**フェーズ**: `pr_review_response`
**PR**: {params.pr_number | なし}
**発生時刻**: {timestamp}

外部レビュアーが現在利用できないため、無進捗や実装失敗として扱わず安全停止しました。
コード変更、push、PR の Draft 化、auto-merge は行っていません。

### 次のアクション

PR の内容を人間が確認し、マージ可否を判断してください。再レビューが必要な場合は、外部レビュアーが
利用可能になった後に
`python3 "$LOOP_STEP" resume --loop-id <loop_id> --reset-counters --project <project_root>` で再開してください。
```

専用通知と条件付きコメントを完了したら、下記の汎用安全停止通知・コメントは重ねて送らず、
リポジトリ変更を含まない結果で同じ `stop` action を complete して終了する。

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
