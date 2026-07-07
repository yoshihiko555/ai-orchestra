---
codd:
  node_id: "design:loop-harness-cli"
  kind: design
  status: active
  depends_on:
    - id: "design:loop-harness"
      relation: refines
  owner: ai-orchestra
---

# Loop Harness 詳細設計（CLI / config 編）

**作成日**: 2026-07-06
**ステータス**: active（詳細設計。Phase 3 相当。`scripts/` 配下 4 CLI + config 全キーの実装可能仕様）
**対象**: `feat/loop` ブランチ
**関連**: `design:loop-harness`（基本設計。本書はこれを refines する）

> 本書は基本設計（`design:loop-harness`）が「詳細設計フェーズで確定する」と申し送った事項
> （12 節）のうち、`scripts/` 配下 4 CLI（`loop_step.py` / `loop_driver.py` / `loop_scheduler.py` /
> `loop_status.py`）の引数・JSON スキーマ・exit code、および `config/loop-harness.yaml` の全キーを
> 実装可能な粒度で確定する。各章冒頭に対応する基本設計の参照節を明記する。
> 失敗シグネチャの正規化アルゴリズム詳細、severity 判定ロジック、redaction の具体的検出パターンは
> 別の詳細設計文書（core 編 / pr-review 編）に委ねる。

---

## 0. 本書の参照節対応表

| 本書の章                         | 対応する基本設計の節                            |
| -------------------------------- | ----------------------------------------------- |
| 1. `loop_step.py`                | 3 節（コンポーネント表）・5.3〜5.6 節・7 節     |
| 2. `loop_driver.py`              | 3 節・8 節                                      |
| 3. `loop_scheduler.py`           | 3 節・8 節・5.6 節（repo-identity 照合）        |
| 4. `loop_status.py`              | 3 節・5.1 節（artifacts）・10.3 節（retention） |
| 5. config 全キー                 | 10.3 節（既定値の例、多くが「詳細設計で確定」） |
| 6. audit 連携                    | 10.1 節・11 節（FT-11）                         |
| 7. manifest.json / packages 配布 | 3 節（コンポーネント構成）                      |
| 8. 基本設計との差分・要確認事項  | （本書独自。12 節の申し送り事項を含む）         |

---

## 1. `loop_step.py`（LP-1: propose / complete / reconcile / heartbeat / resume / start）

> 参照: 基本設計 3 節（コンポーネント表）、5.2〜5.6 節（state/lock/journal・two-phase・reconcile・
> クラッシュ回復・push 前ガード）、7 節（LP-1 実行フロー）。

### 1.1 サブコマンド一覧

```text
loop_step.py start      --issue <N> [--definition <id>] [--project <path>]
loop_step.py attach     --loop-id <id> [--project <path>]
loop_step.py propose    --loop-id <id> --lease-token <token> [--project <path>]
loop_step.py complete   --loop-id <id> --action-id <id> --state-version <n> --result <json|@file> --lease-token <token> [--project <path>]
loop_step.py reconcile  --loop-id <id> --lease-token <token> [--project <path>]
loop_step.py heartbeat  --loop-id <id> --lease-token <token> [--project <path>]
loop_step.py resume     --loop-id <id> --reset-counters [--project <path>]
```

> **`--lease-token` の扱い（Codex レビュー指摘反映。P1。詳細は 1.9 節）**: `start` / `attach` /
> `resume` は新規に lease を発行し、応答 JSON に `lease_token` を含める。それ以外の変更系サブコマンド
> （`propose` / `complete` / `reconcile` / `heartbeat`）は、呼び出し側が保持するその `lease_token`
> を `--lease-token` で**必須**渡しする。
>
> **`start` / `attach` / `resume` の使い分け（Codex レビュー指摘反映。P2。詳細は 1.10 節・基本設計
> 5.5 節）**: 新規作成は `start`、クラッシュ・セッション断絶後の再取得は `attach`、正規の失敗終了・
> 安全停止からの人間判断による再挑戦は `resume`。3 者は対象状態が排他的であり混同しない。

- 全サブコマンドは stdout に **1 行の JSON オブジェクト**のみを出力する（人間可読ログは stderr へ）。
- `--project`省略時は `find_repo_root()`（`harness_common.py` と同じ「`.git` を上に探索」方式。
  `packages/codex-harness/scripts/harness_common.py` の `find_repo_root` と同じ実装を踏襲）で解決する。
- state root の解決（root worktree 側の `.claude/loop/`）は基本設計 5.1 節の
  `_resolve_root_worktree` パターン（`git rev-parse --path-format=absolute --git-common-dir` の
  `dirname`）を `loop_common.py` 内部に実装し、全サブコマンドが共通して使う。

### 1.2 exit code 規約（全サブコマンド共通）

| exit code | 意味                                                                                                                                                                                                            |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`       | 正常終了。stdout の JSON に結果が入る                                                                                                                                                                           |
| `1`       | 一般エラー（引数不正、state/journal の読み書き失敗、Issue 未検出等）                                                                                                                                            |
| `2`       | 検証拒否（`complete` の `action_id` / `state_version` 不一致＝stale。1.5 節。`propose`/`complete`/`reconcile`/`heartbeat` の `--lease-token` 不一致・欠落もここに含む。1.9 節）                                 |
| `3`       | lock 取得失敗（`start` の Issue ロック取得失敗、または `attach` 実行時に旧 lease が生存中〔TTL 内かつ heartbeat 継続中〕で再取得を拒否した場合。lease_token の不一致・欠落は exit `2` に統一。1.9 節・1.10 節） |

- `1`〜`3` いずれの場合も stdout には `{"error": {"code": ..., "message": ...}}` 形式の JSON を出す
  （オーケストレーター側が exit code とあわせて機械的に判定できるようにする。stderr にも同内容を
  人間可読で出す）。
- exit code はサブコマンド固有のセマンティクスを持たせず、全サブコマンドで意味を統一する
  （オーケストレーターが判定ロジックを 1 種類に集約できるようにするため）。

### 1.3 `start --issue <N>`（ループラン初期化）

state が存在しない状態からループランを開始する専用サブコマンド。`/loop-issue <N>` スキルの起動口
（基本設計 7 節・FT-02）はこれを最初に呼ぶ。

```bash
python3 loop_step.py start --issue 42 --definition issue-loop --project /path/to/repo
```

引数:

| 引数           | 必須 | 説明                                                                    |
| -------------- | ---- | ----------------------------------------------------------------------- |
| `--issue`      | ✓    | GitHub Issue 番号                                                       |
| `--definition` |      | ループ定義 ID（省略時 `issue-loop`。`loop_definition.py` がロードする） |
| `--project`    |      | プロジェクトルート（省略時は自動解決）                                  |

処理手順:

1. `repo-identity-hash`（5.1 節）を算出し、`loop_id = f"{repo_identity_hash[:8]}-issue-{issue}"` を
   決定論的に採番する。
2. `.claude/loop/<loop_id>/` に既存 `state.json` があれば `409` 相当のエラー（exit `1`、
   `{"error": {"code": "already_exists", ...}}`）を返す。既存ループの続行は、`lease_token` を
   既に保持している同一セッションなら `propose`、保持していない別セッション（クラッシュ後の
   再開等）なら `attach`（1.10 節）を使う。
3. Issue 単位のロック（`skill-evolution` の TTL 判定パターンを汎用化。基本設計 10.1 節・FT-07）を
   取得する。取得失敗（TTL 内の他プロセスが保持）は exit `3`。
4. `worktree_manager.py` で worktree を作成する（`issue-fix.md` のブランチ判定ヒューリスティックを
   移植。既に準備済みブランチがあれば流用、なければ新規作成。基本設計 7 節）。
5. `state.json` を初期化（`phase` はループ定義の先頭フェーズ、`iteration=0`、`state_version=0`）し、
   `journal.jsonl` に `event: "loop_started"` を追記する。
6. audit へ `loop_start` を emit する（6 節）。
7. 内部的に `propose` と同じロジックを呼び、最初のアクション（`run_maker`, `iteration=1`）を
   決定して返す。

応答 JSON（`propose` と同じ形に加え `lease_token` を含む。1.4 節・1.9 節参照）:

```jsonc
{
  "loop_id": "a1b2c3d4-issue-42",
  "action": "run_maker",
  "action_id": "act-000001",
  "state_version": 1,
  "phase": "implementation",
  "iteration": 1,
  "params": {},
  "reason": "loop initialized; first maker run",
  "lease_token": "6f1e...", // 呼び出し側が保持し、以後の propose/complete/reconcile/heartbeat に渡す（1.9 節）
}
```

> **`start` の応答は初回 `propose` と等価である（Codex レビュー指摘反映。P1。pr-review 編 5.1 節
> 参照）**: `start` は内部で最初のアクションを `pending` として journal に記録済みであるため、
> **呼び出し側はこの応答をそのまま最初の `propose` の結果として扱い、実行後に `complete` を呼ぶ**。
> `start` の直後に**あらためて `propose` を呼んではならない**。ここでもう一度 `propose` を呼ぶと、
> `start` が記録した `pending` action が `complete` されないまま次の `propose` の対象になり、
> reconcile（1.6 節）が「孤立した pending action」として扱ってしまう（実行されたはずの初回 Maker
> 起動が欠落する、または `infrastructure_failure` に誤分類される）。正しい呼び出し順序は
> ① `start` → ② その応答の `action` を実行 → ③ `complete --action-id <start 応答の action_id>` →
> ④ 以後は `propose` → 実行 → `complete` のループ、である。

**`start` と `propose`（state なし）の関係（基本設計との整合）**: 基本設計 7 節の図は
「`loop_step propose`（state なし）」が初期化まで行うと描いているが、5.3 節のサブコマンド一覧には
`start` は明記されていない。本書では `start` を明示的な入口として新設し、`propose` は
**既存 state に対してのみ**動作する（state が無い `loop_id` に対する `propose` は exit `1`
`{"error": {"code": "not_found", ...}}` を返す）ものとして役割を分離する。理由は、初期化
（Issue ロック取得・worktree 作成・repo-identity 記録）は副作用が大きく、`propose` のように
何度でも安全に呼べる操作とは性質が異なるためである。8 節で基本設計との差分として報告する。

### 1.4 `propose`

```bash
python3 loop_step.py propose --loop-id a1b2c3d4-issue-42 --lease-token 6f1e... --project /path/to/repo
```

引数:

| 引数            | 必須 | 説明                                                                                            |
| --------------- | ---- | ----------------------------------------------------------------------------------------------- |
| `--loop-id`     | ✓    |                                                                                                 |
| `--lease-token` | ✓    | `start`/`resume` 応答で取得した lease_token（1.9 節）。呼び出し側が保持し続ける値をそのまま渡す |
| `--project`     |      | プロジェクトルート（省略時は自動解決）                                                          |

処理手順（基本設計 5.3〜6 節）:

1. `--lease-token` で渡された値と `lock.json.lease_token` を照合する（fencing）。**この検証は
   `lock.json` の再読だけで自己完結させず、呼び出し側が保持する token との一致を必須の入力とする**
   （1.9 節）。不一致・欠落は exit `2`。
2. `reconcile`（1.5 節のロジックをそのまま内部呼び出し）を実行し、前回 `complete` が未確定のまま
   終了していないかを確認・復旧する。
3. 直近の Checker 結果があればガード評価（基本設計 6.1 節: 合格判定 → 無進捗判定 → 反復上限判定）
   を行い、次アクションを決定する。
4. `journal.jsonl` に `event: "pending"` として今回の action を記録し、`action_id` を新規採番する
   （フォーマット: `act-XXXXXX` の連番 6 桁 0 埋め。ループ内で単調増加）。
5. 応答 JSON を返す。

応答スキーマ:

```jsonc
{
  "loop_id": "a1b2c3d4-issue-42",
  "action": "run_maker", // run_maker | run_checker | wait_external_review | advance_phase | stop | exit_success | exit_failure
  "action_id": "act-000004",
  "state_version": 5, // この action を complete する際に必須の期待バージョン
  "phase": "implementation",
  "iteration": 2,
  "params": {
    // action ごとに内容が変わる可変フィールド。advance_phase 応答の verified_branch は
    // 5.6 節の push 前ガード強制結線（基本設計 5.6 節）を実現するための必須フィールド。
    "verified_branch": null, // advance_phase のときのみ検証済みブランチ名を含める
    "maker_agent": null, // run_maker のときのみ agent-routing 解決結果（cli-tools.yaml 由来）
    "prompt_template": null, // run_maker / run_checker のときのみ facets 参照パス
  },
  "reason": "iteration 2: previous check failed (test_failure), guard not reached",
}
```

`action` 別の `params` 内容:

| `action`               | `params` の主なフィールド                                                                                                                      |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `run_maker`            | `maker_agent`（`cli-tools.yaml` 解決結果）、`prompt_template`                                                                                  |
| `run_checker`          | `mechanical.commands`、`llm_review.baseline` / `selection`（ループ定義から転記）                                                               |
| `wait_external_review` | `pr_number`、`poll_interval_seconds`、`timeout_seconds`（config 由来。5 節）                                                                   |
| `advance_phase`        | `verified_branch`（5.6 節）、`next_phase`、`exec`（ループ定義の `on_success.exec` 転記）                                                       |
| `stop`                 | `stop_reason`（**安全停止の 3 条件のいずれか**: `push_guard_violation` / `repo_identity_mismatch` / `foreign_live_lease`。2.6 節・3.4 節参照） |
| `exit_success`         | `pr_number`                                                                                                                                    |
| `exit_failure`         | `stop_reason`、`draft_pr_exec`（`on_failure.exec` 転記）                                                                                       |

> **`stop` と `exit_failure` の区別（spec-reviewer 指摘反映）**: `action: stop`（`state.json.status
= "stopped"`）は**安全停止**専用であり、発生条件は上記 3 種類のみに限定する。
> `guards.infrastructure_failure.max_retries`（5 節）到達は `stop` ではなく、通常の
> `on_failure` 経路（`action: exit_failure`）として扱う。安全停止は「実行を続けると危険
> （誤ったリポジトリ・ブランチへの書き込み、他ホストとの競合）」と判断した場合の緊急停止であり、
> ガード評価の結果としての通常の失敗出口（Draft PR 作成等の `on_failure.exec` を伴う）とは
> 性質が異なる。安全停止時の具体的な挙動（exec 抑制・通知方針）は 2.6 節（push 前ガード違反）・
> 3.4 節（repo-identity 不一致）で確定する。

### 1.5 `complete`

```bash
python3 loop_step.py complete \
  --loop-id a1b2c3d4-issue-42 \
  --action-id act-000004 \
  --state-version 5 \
  --result @/tmp/result.json \
  --lease-token 6f1e... \
  --project /path/to/repo
```

引数:

| 引数              | 必須 | 説明                                                         |
| ----------------- | ---- | ------------------------------------------------------------ |
| `--loop-id`       | ✓    |                                                              |
| `--action-id`     | ✓    | `propose` が返した `action_id`                               |
| `--state-version` | ✓    | `propose` が返した `state_version`（提案時点のバージョン）   |
| `--result`        | ✓    | JSON 文字列、または `@path` でファイル参照（CheckResult 等） |
| `--lease-token`   | ✓    | `start`/`resume` 応答で取得した lease_token（1.9 節）        |

検証手順:

1. `--lease-token` で渡された値と `lock.json.lease_token` を照合する（fencing。1.9 節。`lock.json`
   の再読のみによる自己検証はしない）。不一致・欠落は exit `2`。
2. 現在の pending action と `action_id` / `state_version` を照合する。
   - **一致**: 3 へ進む。
   - **不一致（stale）**: exit `2`、`{"error": {"code": "stale_action", "message": "..."}}`。
   - **`action_id` が直近で `completed` 済み（冪等再送）**: state を再更新せず、前回の応答を
     そのまま再構築して exit `0` で返す（基本設計 5.3 節「`complete` の冪等性」）。
3. `--result` の内容（CheckResult、Maker サマリ等）を `.claude/loop/<loop_id>/artifacts/<action_id>/`
   に保存する（5.4 節 reconcile の復元元）。
4. `state.json` を更新（`state_version` をインクリメント、`phase`/`iteration`/`last_check_result` 等）し、
   `journal.jsonl` に `event: "completed"` を追記する（10.2 節の redaction を通す）。
5. 応答 JSON: `{"ok": true, "loop_id": ..., "state_version": <new>, "next": "call propose again"}`。

### 1.6 `reconcile`

基本設計 5.3 節の記述どおり、`reconcile` は `propose` 内部から自動的に呼ばれる。加えて本書では、
運用時の手動診断・障害調査のために**明示的にも呼び出せる**独立サブコマンドとして提供する
（基本設計 3 節の表現「サブコマンドとして提供する」と 5.3 節の「`propose` 内部から呼ばれる」は
一見矛盾するが、両立させる設計とし 8 節で明記する）。

```bash
python3 loop_step.py reconcile --loop-id a1b2c3d4-issue-42 --lease-token 6f1e... --project /path/to/repo
```

- `--lease-token`（必須）は `propose` と同じ検証を行う（1.9 節）。単体呼び出しも状態変更を伴う操作
  であるため、fencing を省略しない。
- `propose` 実行時に自動で呼ばれる経路と全く同じ内部関数（`loop_common.reconcile()`）を呼ぶ。
- 単体で呼んだ場合も副作用（state 更新・journal 追記）は `propose` 経由と同一。ただし新しい
  `action` の提案（次に何をすべきか）までは行わず、reconcile の結果のみを返す:

```jsonc
{
  "loop_id": "a1b2c3d4-issue-42",
  "reconciled": true, // 何らかの復旧処理を行ったか
  "resolved_action_id": "act-000003", // 復旧対象があった場合のみ
  "resolution": "artifact_restored", // journal_restored | artifact_restored | marked_infrastructure_failure | none
  "state_version": 4,
}
```

### 1.7 `heartbeat`

```bash
python3 loop_step.py heartbeat --loop-id a1b2c3d4-issue-42 --lease-token 6f1e... --project /path/to/repo
```

- `--lease-token`（必須）と `lock.json.lease_token` を照合してから（1.9 節）、`lock.json` の
  `heartbeat_at` を現在時刻で更新する（`lease_token` の値自体は変更しない）。不一致・欠落は exit `2`。
- LP-1 では長時間の単一アクション（例: Checker の LLM レビューが長引く場合）の間、オーケストレーター
  が `propose`/`complete` を呼ばずに `heartbeat` のみを定期実行して lease を延命できる。
- 応答: `{"loop_id": ..., "heartbeat_at": "...", "ttl": 3600}`。

### 1.8 `resume --reset-counters`

```bash
python3 loop_step.py resume --loop-id a1b2c3d4-issue-42 --reset-counters --project /path/to/repo
```

- 対象: `state.json.status == "failed"`（ガード到達による正規の失敗終了）**または `"stopped"`
  （2.6 節・3.4 節の安全停止）**のループランのみ（core 編 1.2/2.1/2.2 節・基本設計 5.6 節が正）。
  `running` / `waiting_external` 状態への `resume` は exit `1`（`{"error": {"code": "invalid_state"}}`）。
  `stopped` からの `resume` は、安全停止の原因（push ガード違反・repo-identity 不一致・他ホスト
  lease）が人間によって解消されたことを前提とする（原因未解消のまま resume すると同じ安全停止を
  即座に再度踏む可能性がある）。
- `--reset-counters` フラグ必須。フラグなしでの呼び出しは exit `1`
  （`{"error": {"code": "reset_counters_required"}}`）とし、誤操作による無制限リトライを防ぐ
  （基本設計 5.5 節）。
- 処理: 現在フェーズの `iteration` を `0` に、`no_progress` カウンタ・`infrastructure_failure`
  カウンタをリセットし、`status` を `running` に戻す。`journal.jsonl` に
  `event: "resumed", actor: "human"` を追記する。**新しい `lease_token` を発行して `lock.json` に
  書き込む**（1.9 節。旧 token は以後無効。人間判断による再開のたびに lease を再発行し、失効済み
  token を握った古い呼び出し元を確実に締め出す）。
- 応答: `propose` と同じ形式で、リセット後の最初のアクションを返す。加えて、新規発行した
  `lease_token` をレスポンスに含める（呼び出し側はこれを保持し、以後の `propose`/`complete`/
  `reconcile`/`heartbeat` に渡す。1.9 節）。

### 1.9 `lease_token` の呼び出し契約（Codex レビュー指摘反映。P1）

**問題**: 当初案は変更系サブコマンドが毎回 `lock.json` を読み直して自己完結的に検証する構造
だった。この構造では「渡された値と照合する」対象がなく、実質的に「その時点の `lock.json` の
値と一致するかどうか」を自分自身に対して確認するだけになり、fencing が機能しない。TTL 失効後に
別プロセスが新しい lease を取得したケースでも、旧プロセスの書き込みが（旧プロセス自身が
`lock.json` を読み直した時点で得る）新しい token との照合を素通りしてしまう。

**修正**: `lease_token` は **呼び出し側（LP-1 のオーケストレーター、または LP-2 の
`loop_driver`）が保持する契約**とし、CLI 引数として明示的に受け渡す。

- `start`（1.3 節）・`attach`（1.10 節）・`resume`（1.8 節）は新規に lease を発行し、応答 JSON に
  `lease_token` を含める。呼び出し側はこれをプロセス内で保持し続ける。
- 変更系サブコマンド `propose`（1.4 節）・`complete`（1.5 節）・`reconcile`（1.6 節）・
  `heartbeat`（1.7 節）は **`--lease-token <token>` を必須引数**として受け取り、渡された値と
  `lock.json.lease_token` を照合する。**`lock.json` の再読のみによる自己検証はしない**（呼び出し側が
  保持する token との一致を必須の入力とする）。
- 不一致・欠落は **exit code `2`**（検証拒否。1.2 節）で棄却する。`lock.json` に対応する lease が
  そもそも存在しない、`start` 自体のロック取得に失敗した場合、または `attach` 実行時に旧 lease が
  生存中で再取得を拒否した場合のみ exit `3` を用いる（1.2 節で明確化）。
- `loop_status.py`（4 節）の `list`/`show`（4.1 節・4.2 節）は read-only であり state/journal/lock
  への書き込みを行わないため `--lease-token` は不要。`purge`（4.3 節）も、対象を完了済み
  （`status` が `passed`/`failed` に確定し、lock が解放済み）のループランのみに限定しているため
  同様に不要。

### 1.10 `attach --loop-id <id>`（クラッシュ・セッション断絶後の lease 再取得。Codex レビュー

指摘反映。P2。FT-22）

**問題**: `propose` は `--lease-token` を必須とするが、クラッシュ・セッション断絶により
`lease_token` を保持していない**新しい呼び出し元**が、`running`/`waiting_external` のまま
残された既存ループランを再開する経路が存在しなかった（`start` は state が既に存在するため
`already_exists` で拒否し、`resume` は `failed`/`stopped` のみを対象とするため使えない）。この
ままでは FT-22（クラッシュ回復）が LP-1 の実運用シナリオで実装不能だった。

**修正**: `attach` を新設し、既存ループランに対して新しい呼び出し元が `lease_token` を再取得する
専用の入口とする。

```bash
python3 loop_step.py attach --loop-id a1b2c3d4-issue-42 --project /path/to/repo
```

引数:

| 引数        | 必須 | 説明                                   |
| ----------- | ---- | -------------------------------------- |
| `--loop-id` | ✓    |                                        |
| `--project` |      | プロジェクトルート（省略時は自動解決） |

処理手順:

1. 対象 `loop_id` の `state.json.status` を確認する。`running`/`waiting_external` 以外
   （`pending`/`passed`/`failed`/`stopped`）は exit `1`
   （`{"error": {"code": "invalid_state", ...}}`）。`failed`/`stopped` からの再開は `resume`
   （1.8 節）を使う。
2. 現在の `lock.json` の lease が**生存中**（TTL 内かつ heartbeat が継続している。基本設計 6.3 節
   `is_lease_alive()`）かどうかを判定する。生存中であれば、まだ別のプロセスが正当にループを保持
   していると判断し **exit `3`** で拒否する（二重 attach による同時書き込みを防ぐ。旧プロセスが
   実は生きている場合に誤って乗っ取らないための安全策）。
3. lease が stale（TTL 超過、または heartbeat 途絶）であることを確認できた場合のみ、
   `reacquire_lease()`（core 編 6.3 節）で新しい `lease_token` を発行し `lock.json` を更新する
   （TOCTOU 緩和のうえで奪取。旧 token は以後 `validate_lease()` に通らなくなる）。
4. `journal.jsonl` に `event: "attached", actor: "human"`（または LP-2 由来なら `actor: "scheduler"`）
   を追記する。
5. `state.json` の `pending_action` を確認し、内部的に `propose` と同じロジック（1.4 節の
   reconcile → ガード評価）を呼び、次に実行すべきアクションを決定して返す。

応答 JSON（`propose` と同じ形に加え `lease_token` を含む。1.4 節・1.9 節参照）:

```jsonc
{
  "loop_id": "a1b2c3d4-issue-42",
  "action": "run_maker",
  "action_id": "act-000005",
  "state_version": 6,
  "phase": "implementation",
  "iteration": 3,
  "params": {},
  "reason": "attached after lease expiry; resuming from reconciled state",
  "lease_token": "9c2d...", // 新規発行。呼び出し側はこれを保持し、以後の propose/complete/reconcile/heartbeat に渡す
}
```

- `attach` の応答は（`start` と異なり）**そのまま最初の propose 結果として実行してよい**が、
  ここで返るのは reconcile 後の**既存の次アクション**であり、`start` の「新規ループの最初の
  action」とは意味が異なる（`start` は初回 `run_maker` を新規に pending 化するのに対し、
  `attach` は既存 `pending_action` を reconcile した結果、または reconcile 後にガード評価で
  決定した次アクションを返す）。
- LP-2: `loop_scheduler`（3.3 節）が worker の異常終了を検知して同一 `loop_id` を再起動する際、
  再起動後の `loop_driver.py`（2.1 節）は内部的にこの `attach` と同じロジック
  （`loop_common.reacquire_lease()`）を呼んで新しい `lease_token` を取得してから続行する。
- `resume`（1.8 節）との違い: `attach` はガードカウンタに一切触れない（クラッシュからの技術的な
  lease 再取得であり、ガード評価の結果はそのまま引き継ぐ）。`resume` はガードカウンタを明示的に
  リセットする人間判断の再挑戦であり、対象状態も `failed`/`stopped` のみと排他的である
  （基本設計 5.5 節）。

---

## 2. `loop_driver.py`（LP-2 worker）

> 参照: 基本設計 3 節（コンポーネント表）、8 節（LP-2 実行フロー）。

### 2.1 起動と責務

```bash
python3 loop_driver.py --loop-id a1b2c3d4-issue-42 --project /path/to/repo
```

1 プロセス = 1 ループラン。`loop_scheduler.py`（3 節）が子プロセスとして起動する。`loop_step.py` の
CLI を経由せず、`loop_common.py` を直接呼び出す（8 節: 独立ドライバ）。

**起動時の `lease_token` 取得（Codex レビュー指摘反映。P1。1.9 節・1.10 節）**: `main()` は
`loop_common` を直接呼ぶため CLI の `--lease-token` 引数は経由しないが、契約自体は 1.9 節・
1.10 節と同一である。

- 新規ループ（discovery で初めて検出した Issue。3.1 節）: 対象 `loop_id` の `state.json` が
  存在しないため、`loop_common` の `start()` 相当の内部処理で lease を新規取得し `lease_token`
  を得る。
- 既存ループの再起動（`loop_scheduler` が worker の異常終了を検知し、同一 `loop_id` で
  再起動した場合。3.3 節）: `loop_common.reacquire_lease()`（`attach` の実体。1.10 節・core 編
  6.3 節）を呼び、新しい `lease_token` を取得する。旧 lease が生存中（TTL 内かつ heartbeat
  継続中）と判定された場合は `ForeignLeaseError` 相当の例外を送出し、worker は即座に終了する
  （二重起動防止。1.10 節の `attach` 拒否条件と同一）。
- 取得した `lease_token` は `main()` のプロセス内メモリに保持し、以後のすべての `loop_common`
  呼び出し（`propose`/`complete`/`heartbeat`）にそのまま引数で渡す（2.3 節）。

### 2.2 `claude -p` 起動コマンド構成

Maker/Checker（LLM レビュー）はいずれも `claude -p`（`--print`、非対話実行モード）で起動する。

> **`--allowedTools` の仕様訂正（Codex レビュー指摘反映。P1）**: `--allowedTools` は Claude Code CLI
> の仕様上「確認なしで自動承認するツールの許可リスト」であり、利用可能なツールそのものを**制限**
> するものではない。`Bash(git *)` のような広い自動承認パターンを与えると、driver の push ガード
> （基本設計 5.6 節・2.6 節）を通る前に Maker（`claude -p`）自身が `git push` を実行できてしまう
> （権限の「制限」と誤認していた設計上の欠陥）。権限制御は **`--disallowedTools`（明示拒否）と
> 狭い許可パターンの `--allowedTools` の組み合わせ**で行う。

```bash
claude -p \
  --output-format json \
  --permission-mode acceptEdits \
  --allowedTools "Read,Grep,Glob,Edit,Write,Bash(git add:*),Bash(git commit:*),Bash(git status:*),Bash(git diff:*),Bash(pytest *),Bash(ruff *)" \
  --disallowedTools "Bash(git push:*),Bash(git remote:*),Bash(git worktree:*),Bash(gh pr:*)" \
  --add-dir <worktree_path> \
  "<prompt>" < /dev/null
```

**設計原則: Maker プロセスは push 能力を構造的に持たない。** push・PR 作成は Maker には一切
実行させず、push ガード（基本設計 5.6 節）通過後に `loop_driver.py`（Python、2.6 節）が自ら実行する。

**権限方針（`--dangerously-skip-permissions` は使わない）**:

- headless 実行で人間の承認は得られないが、包括的な承認バイパス（`--dangerously-skip-permissions`）
  は「任意コマンド実行を無条件許可」であり NF-04（秘匿情報保護）・5.1 節（state 直接改ざんの残存
  リスク）と両立しない。
- 代わりに `--permission-mode acceptEdits`（ファイル編集は自動承認するが、許可リスト外の危険操作は
  ブロックされる Claude Code 標準モード）+ `--allowedTools`（`Read`/`Grep`/`Glob`/`Edit`/`Write` と、
  `git add`/`git commit`/`git status`/`git diff`/`pytest`/`ruff` に限定した狭い `Bash` プレフィックス
  許可）+ `--disallowedTools`（`git push`/`git remote`/`git worktree`/`gh pr` 系の明示拒否）を
  組み合わせる。
- Maker が `.claude/loop/` へアクセスする必要は元々ない（基本設計 5.1 節: cwd は常に `worktree_path`）
  ため、許可リストにその経路を含めない。
- ループ定義の `checker.mechanical.commands`（例: `pytest -q`, `ruff check .`）に応じて
  `--allowedTools` の `Bash(...)` 許可リストを動的に組み立てる（ループ定義に無いコマンドは
  許可しない。ホワイトリスト方式）。push/PR 作成系コマンドは、このホワイトリスト組み立てとは
  独立に常に `--disallowedTools` へ固定で含める（動的組み立ての不備でも push 系が漏れ出ない
  ようにする多層防御）。

**プロンプトテンプレートの骨子**（`facets/instructions/loop-issue.md` の `#maker` / `#checker`
アンカーを参照する。実体は `loop_definition.py` がロードした `prompt_template` パスから読み込み、
以下の変数を埋め込んで渡す）:

```text
[Role] あなたは Issue #{issue_number} の実装を担当する Maker です。
[Context] 現在 iteration {iteration}/{max_iterations}。前回の失敗: {last_check_result.signature}
[Task] {issue の本文サマリ}
[Constraints] 冪等性契約: 既存のコミット・差分を確認し、二重にコミットしない（基本設計 5.4 節）。
[Output] 変更をコミットし、変更点の要約を1段落で報告してください。
```

Checker（LLM レビュー）は `code-reviewer` 等のレビュアー定義プロンプト（`skill-review-policy.md`
準拠）をそのまま `claude -p` の task として渡す。Maker/Checker は**別プロセス・別コンテキスト**
（NF-03）として起動する（同一プロセス内でロールを切り替えない）。

### 2.3 バックグラウンド heartbeat スレッド

```python
def _heartbeat_loop(
    loop_id: str,
    project_root: Path,
    lease_token: str,
    interval: int,
    stop_event: threading.Event,
    on_lease_lost: Callable[[], None],
) -> None:
    while not stop_event.wait(interval):
        if not loop_common.heartbeat(loop_id, project_root, lease_token):
            # lease_token 不一致（他プロセスに lease を奪取された）。継続すると
            # 二重書き込みのリスクがあるため、即座に安全停止相当で終了する。
            on_lease_lost()
            return
```

- `loop_driver.py` の `main()` は 2.1 節で取得した `lease_token` をこのスレッドの引数として渡す。
  ワーカースレッドとして起動し（`daemon=True`）、`config.lock.heartbeat_interval_seconds`
  （既定 60 秒。5 節）ごとに `lock.json` の `heartbeat_at` を更新する（`loop_common.heartbeat()`
  は `--lease-token` と同じ検証を内部で行う。1.9 節・core 編 6.3 節）。
- **lease 喪失時の即時終了（Codex レビュー指摘反映。P1）**: `loop_common.heartbeat()` が
  `lease_token` 不一致（TTL 失効後に別プロセス・別セッションが `attach` で新しい lease を取得
  済み）で `False` を返した場合、`on_lease_lost` コールバックが現在実行中の子プロセス
  （`claude -p` 等）を 2.5 節の kill-tree で強制終了し、`main()` に終了を要求する。この時点で
  `state.json` の正当な所有者は新しい lease を保持する側であるため、**このプロセス自身は
  `state.json`/`journal.jsonl` を書き換えない**（fencing 上、書いても `validate_lease()` に
  通らない）。stderr へ警告ログを出し、macOS 通知を発火したうえで exit する（2.6 節の安全停止と
  同様、書き込みを伴う exec は一切行わない）。
- worker プロセスの終了時（正常/異常問わず）は `stop_event.set()` で確実に停止させる（`finally` 節）。

### 2.4 wall-clock 監視（NF-02）

- **確定値: 2 時間（`lp2.wall_clock_timeout_seconds=7200`）で強制停止**。
- `loop_driver.py` の `main()` は起動時刻を記録し、各反復ループの先頭で経過時間をチェックする。
  上限到達時は現在実行中の子プロセス（`claude -p` 等）を 2.5 節の kill-tree で強制終了し、
  `state.json.status = "failed"`、`stop_reason = "wall_clock_timeout"` として失敗出口へ遷移する
  （`on_failure.exec` は実行する。Draft PR 作成は行うが、それ以上の反復は行わない）。
- 監視は `loop_driver` プロセス自身が行う（`loop_scheduler` 側の監視は 3.4 節の二重監視として
  補完的に働く）。

### 2.5 子プロセスの timeout / kill-tree

`claude -p` はサブプロセスとして `git`/`pytest` 等をさらに起動しうるため、単純な `process.kill()`
では子孫プロセスが残る可能性がある。

```python
import os
import signal
import subprocess

def run_claude_p(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # 新しいプロセスグループを作る（kill-tree の前提）
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        raise
```

- `start_new_session=True` でプロセスグループを分離し、`os.killpg` でグループ全体に `SIGTERM` を送る。
  10 秒待って終了しなければ `SIGKILL` にエスカレーションする。
- 1 反復あたりの `claude -p` 呼び出しには個別 timeout（config には露出しない。ループ定義の
  `guards.max_iterations` とは独立し、2.4 節の wall-clock 上限から残り時間を按分する形で実装する）
  を設定し、無応答プロセスが wall-clock 上限まで居座らないようにする。

> **`wall_clock_timeout` は安全停止ではない（spec-reviewer 指摘反映）**: 2.4 節の壁時計時間上限
> 到達は 1.4 節の安全停止 3 条件に含まれない。通常の `on_failure` 経路（`state.json.status =
"failed"`、`stop_reason = "wall_clock_timeout"`）として扱い、`on_failure.exec`（Draft PR 作成等の
> 書き込みを伴う出口処理）をそのまま実行する。安全停止（`status = "stopped"`）の挙動は 2.6 節を参照。

### 2.6 安全停止（push 前ガード違反）

`on_success.exec` の `push` 実行直前（基本設計 5.6 節）に `loop_driver.py` が検証するブランチ検証・
repo-identity 照合のいずれかに失敗した場合、`loop_driver.py` は以下を行う（本節・3.4 節共通の
安全停止仕様）:

1. `push`/`pr_create` 等、**リポジトリへの書き込みを伴う exec を一切実行しない**（`on_success.exec`
   の残りステップを中断する）。
2. `state.json.status = "stopped"`、`stop_reason = "push_guard_violation"` として記録する。
3. macOS 通知を**必ず**発火する。
4. Issue コメント投稿は、当該ループの repo-identity が検証済み（1.3 節の `start` 時点で記録した
   repo-identity と現在値が一致）の場合のみ行う。ブランチ検証違反のみで repo-identity 自体は正しい
   場合は投稿してよいが、repo-identity 不一致が絡むケース（3.4 節）は投稿しない。
5. `journal.jsonl` に `event: "stopped", payload: {stop_reason: "push_guard_violation", ...}` を追記し、
   audit へ `loop_stop`（`stop_reason` 付き）を emit する（6 節）。
6. worktree・state/journal は保持する（人間の調査・再開判断のため。FT-23 と同様の方針）。

---

## 3. `loop_scheduler.py`

> 参照: 基本設計 3 節（コンポーネント表）、8 節（LP-2 実行フロー）、5.6 節（repo-identity 照合）。

### 3.1 discovery（ラベル付き Issue キュー）

```bash
gh api repos/{owner}/{repo}/issues \
  --method GET \
  -f labels=loop-ready \
  -f state=open \
  -f sort=created \
  -f direction=asc
```

- ラベル `loop-ready` が付いた open Issue を取得する（ループ定義の `trigger.lp2.label` から解決。
  基本設計 4 節の `issue-loop.yaml` 例では `label: "loop:queue"` としているが、実運用のラベル名は
  `config/loops/*.yaml` 側で定義するためスケジューラ自体はラベル名をハードコードしない）。
- **優先度順**: `created_at` 昇順（先に登録された Issue を優先。公平性を優先し、複雑な優先度スコア
  は当面導入しない）。Issue に `priority:high` 等の追加ラベルがある場合はそれを最優先ソートキーとし、
  同順位内は `created_at` 昇順とする（優先度ラベルの語彙は config で定義。5 節）。
- 既に `loop_id`（`<repo-hash8>-issue-<N>` 決定論的採番）に対応する `state.json` が
  `running`/`waiting_external` で存在する Issue は discovery 対象から除外する（二重起動防止）。

### 3.2 同時実行 cap

- **確定値: 2（`lp2.concurrency_limit=2`、config で変更可）**。
- `loop_scheduler.py` は起動中の worker プロセス数を内部で追跡し、cap に達している間は discovery
  結果があっても新規 worker を spawn しない（次回ポーリング時に再評価）。

### 3.3 worker の spawn / 監視 / kill / restart

```python
def spawn_worker(loop_id: str, project_root: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["python3", str(_SCRIPT_DIR / "loop_driver.py"), "--loop-id", loop_id, "--project", str(project_root)],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
```

- `loop_scheduler.py` はメインループで `poll_interval_seconds`（discovery 用。ループ定義
  `trigger.lp2.poll_interval_seconds` に従う）ごとに以下を行う:
  1. 起動中 worker の生存確認（`Popen.poll()` が `None` でなければ終了済み）。
  2. 異常終了（returncode が `0` 以外）を検知した場合、対象 `loop_id` の `state.json.status` を
     確認し、`failed`（正規の失敗出口）でも `stopped`（2.6 節・3.4 節の安全停止）でもなければ
     5.5 節（基本設計）の reconcile 経路に従い `loop_driver.py` を同一 `loop_id` で再起動する。
     `stopped` は人間の調査を要する明示的な緊急停止であり、スケジューラが自動的に再起動して
     はならない。
  3. cap に空きがあれば discovery 結果から新規 worker を spawn する。
- 再起動回数には上限を設けず、`loop_common` 側のガード（`infrastructure_failure` の
  `max_retries=3`。5 節）が最終的な打ち切りを担う（スケジューラ自体は無限再起動しうるが、
  ガード評価が失敗出口へ導く）。

### 3.4 起動時の repo-identity 照合（安全停止）

- `loop_scheduler.py` 起動時に、実行対象ディレクトリから `repo-identity-hash`（基本設計 5.1 節）を
  再計算し、既存の `.claude/loop/*/state.json` に記録された値と照合する。不一致（想定外の
  リポジトリで誤って起動された等）の場合、当該 `loop_id` は 2.6 節と同じ**安全停止**の扱いとする:
  1. その `loop_id` の worker（起動中であれば）を discovery/監視対象から除外し、以後
     spawn/restart の対象にしない。
  2. `state.json.status = "stopped"`、`stop_reason = "repo_identity_mismatch"` として記録する。
  3. macOS 通知を必ず発火する。**repo-identity 自体が不一致のケースでは Issue コメントは投稿しない**
     （どの Issue に紐づくループかを安全に確定できないため）。
  4. `journal.jsonl` に `event: "stopped"` を追記し、audit へ `loop_stop`（`stop_reason` 付き）を
     emit する。
  - stderr にも警告を出す（診断用）。
- push 直前の repo-identity 再照合は `loop_driver.py`（2 節）側の責務であり、`loop_scheduler.py`
  は起動時の 1 回のみ行う。

### 3.5 cron / launchd 登録例

**cron**（5 分おきにスケジューラの起動確認。`loop_scheduler.py` 自体は常駐しポーリングするため、
cron 側は「落ちていたら起動し直す」監視役に留める）:

```cron
*/5 * * * * pgrep -f loop_scheduler.py || /usr/bin/python3 /path/to/packages/loop-harness/scripts/loop_scheduler.py --project /path/to/repo >> /path/to/repo/.claude/loop/scheduler.log 2>&1
```

**launchd**（macOS、常駐サービスとして登録する場合の plist 骨子）:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ai-orchestra.loop-scheduler</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/packages/loop-harness/scripts/loop_scheduler.py</string>
    <string>--project</string>
    <string>/path/to/repo</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/path/to/repo/.claude/loop/scheduler.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/repo/.claude/loop/scheduler.stderr.log</string>
</dict>
</plist>
```

- `KeepAlive: true` で `loop_scheduler.py` プロセスが落ちた場合に launchd が自動再起動する
  （cron の `pgrep` 監視と役割が重複するため、launchd 採用時は cron 登録を行わない）。
- 実際の配置パス（`~/Library/LaunchAgents/com.ai-orchestra.loop-scheduler.plist`）へのインストール
  手順は `loop_scheduler.py --install-launchd` 等の補助コマンドとして実装するかは、実装フェーズで
  費用対効果を見て判断する（本書では手動配置の骨子のみ確定する）。

---

## 4. `loop_status.py`

> 参照: 基本設計 3 節（コンポーネント表）、5.1 節（`artifacts/` を含む state root）、10.3 節
> （retention）、FT-20。

### 4.1 一覧表示

```bash
python3 loop_status.py list [--project <path>] [--status pending|running|waiting_external|passed|failed|stopped]
```

`.claude/loop/*/state.json` を走査し、以下の表形式で出力する（人間可読。`--json` 指定時は JSON 配列）:

```text
LOOP_ID                  PHASE                 ITERATION  STATUS            ELAPSED
a1b2c3d4-issue-42        implementation        2/3        running           00:14:32
e5f6a7b8-issue-58        pr_review_response    1/3        waiting_external  01:02:10
```

| 列          | 内容                                                      |
| ----------- | --------------------------------------------------------- |
| `LOOP_ID`   | `state.json` の `loop_id`                                 |
| `PHASE`     | 現在フェーズ                                              |
| `ITERATION` | `iteration / guards.max_iterations`（フェーズごとの上限） |
| `STATUS`    | `state.json.status`                                       |
| `ELAPSED`   | `updated_at - created_at`（`HH:MM:SS`）                   |

`--json`:

```jsonc
[
  {
    "loop_id": "a1b2c3d4-issue-42",
    "definition_id": "issue-loop",
    "phase": "implementation",
    "iteration": 2,
    "max_iterations": 3,
    "status": "running",
    "created_at": "2026-07-06T10:00:00+09:00",
    "updated_at": "2026-07-06T10:14:32+09:00",
    "pr_number": null,
  },
]
```

### 4.2 詳細表示

```bash
python3 loop_status.py show --loop-id a1b2c3d4-issue-42 [--project <path>]
```

`state.json` の内容に加え、`journal.jsonl` の直近 N 件（既定 10 件、`--journal-lines` で変更可）を
時系列表示する。`--full-journal` で全件表示。

### 4.3 purge

```bash
python3 loop_status.py purge [--project <path>] [--force]
```

- **既定: 完了後（`status` が `passed`/`failed` に確定してから）30 日経過**したループランの
  `state.json`/`journal.jsonl`/`artifacts/` を削除する（`retention.purge_after_days`。5 節）。
- `--force` 指定時は経過日数を無視し、完了済み（`running`/`waiting_external` 以外）の全ループランを
  即座に purge する。**`running`/`waiting_external` のループランは `--force` でも purge しない**
  （実行中データを誤って消さないためのガード）。
- worktree 自体（FT-23）は `loop_status.py purge` の対象外とし、別途 `worktree_manager.py` の
  明示的な後始末コマンド（`loop_status.py purge --with-worktree` 等）を将来拡張として残す
  （本書では state/journal の purge のみを確定する）。
- purge 前に対象 `loop_id` 一覧を stderr に出し、`--dry-run` で削除対象確認のみ行える。

---

## 5. config 全キー（`config/loop-harness.yaml`）

> 参照: 基本設計 10.3 節（既定値の例。多くは「詳細設計で確定」と申し送られていた）。
> 本節はそれらを実装可能な具体値として確定する。**基本設計の既存の具体値と異なる箇所は
> 8 節で明示する。**

### 5.1 全キーツリー

```yaml
guards:
  max_iterations: 3 # int. フェーズ共通の反復上限
  no_progress:
    repeat: 2 # int. 同一失敗シグネチャ連続回数による無進捗停止のしきい値
  infrastructure_failure:
    max_retries: 3 # int. infrastructure_failure の連続許容回数（超過で失敗出口）

lock:
  ttl_seconds:
    lp1: 3600 # int. LP-1 の lease TTL（秒）。1アクションの最大想定時間ベース
    lp2: 300 # int. LP-2 worker の lease TTL（秒）。短命プロセス想定で短め
  heartbeat_interval_seconds: 60 # int. heartbeat 更新間隔（LP-1/LP-2 共通）

lp2:
  concurrency_limit: 2 # int. LP-2 の同時実行ループ数上限
  wall_clock_timeout_seconds: 7200 # int. LP-2 worker の壁時計時間上限（2時間）

pr_review:
  poll_interval_seconds: 120 # int. PR レビュー完了シグナルのポーリング間隔
  timeout_seconds: 3600 # int. 完了シグナル待機のタイムアウト（60分）
  # reviewer_allowlist は必須キー（未設定・空リストは起動時エラー）。プロジェクトごとに設定する。
  # list[dict] であり list[str] ではない点に注意（実スキーマの正は pr-review 編 2.2 節）。
  # reviewer_allowlist:
  #   - app_slug: "chatgpt-codex-connector"
  #     type: Bot

retention:
  purge_after_days: 30 # int. 完了済みループランの state/journal を purge するまでの保持日数

notifications:
  macos_enabled: true # bool. macOS 通知の有効/無効
  issue_comment_enabled: true # bool. Issue コメント通知の有効/無効
```

### 5.2 キー一覧表（型・既定値・上書き可否）

| キー                                        | 型                                                                                                    | 既定値                                                       | 上書き可否                                   |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------- |
| `guards.max_iterations`                     | int                                                                                                   | `3`                                                          | `.local.yaml` で全体上書き可                 |
| `guards.no_progress.repeat`                 | int                                                                                                   | `2`                                                          | 同上                                         |
| `guards.infrastructure_failure.max_retries` | int                                                                                                   | `3`                                                          | 同上                                         |
| `lock.ttl_seconds.lp1`                      | int（秒）                                                                                             | `3600`                                                       | 同上                                         |
| `lock.ttl_seconds.lp2`                      | int（秒）                                                                                             | `300`                                                        | 同上                                         |
| `lock.heartbeat_interval_seconds`           | int（秒）                                                                                             | `60`                                                         | 同上                                         |
| `lp2.concurrency_limit`                     | int                                                                                                   | `2`                                                          | 同上                                         |
| `lp2.wall_clock_timeout_seconds`            | int（秒）                                                                                             | `7200`                                                       | 同上                                         |
| `pr_review.poll_interval_seconds`           | int（秒）                                                                                             | `120`                                                        | 同上                                         |
| `pr_review.timeout_seconds`                 | int（秒）                                                                                             | `3600`                                                       | 同上                                         |
| `pr_review.reviewer_allowlist`              | list[dict]（`app_slug`/`login`/`type`/`author_association` 等。実スキーマは pr-review 編 2.2 節が正） | **必須キー・既定値なし**（キー欠落・空リストは起動時エラー） | 上書き可（確定値は pr-review 編 2.2 節が正） |
| `retention.purge_after_days`                | int                                                                                                   | `30`                                                         | 同上                                         |
| `notifications.macos_enabled`               | bool                                                                                                  | `true`                                                       | 同上                                         |
| `notifications.issue_comment_enabled`       | bool                                                                                                  | `true`                                                       | 同上                                         |

ループ定義側の `guards.max_iterations` / `guards.no_progress.signature`（基本設計 4 節）は
`loop-harness.yaml` の値を**フェーズ単位で上書きするローカル指定**として扱う（ループ定義に値が
あればそちらを優先し、無ければ `loop-harness.yaml` の全体既定値にフォールバックする）。

### 5.3 `.local.yaml` 上書きと `loops/*.yaml` 探索順序

`config-loading` ルール（`.claude/rules/config-loading.md`）に準拠する。

1. `packages/loop-harness/config/loop-harness.yaml` — 配布ベース設定
2. `.claude/config/loop-harness/loop-harness.local.yaml` — プロジェクト固有の上書き（存在する場合）

`loops/*.yaml`（ループ定義）の探索順序:

1. `packages/loop-harness/config/loops/*.yaml` — 配布バンドルのループ定義（`issue-loop.yaml` 等）
2. `.claude/config/loop-harness/loops/*.yaml` — プロジェクト固有の追加ループ定義

同一 `id` のループ定義がベースとプロジェクト側の両方に存在する場合、プロジェクト側
（`.claude/config/loop-harness/loops/*.yaml`）が優先される（`config-loading` の
「ローカルファイルに定義されたキーはベースを上書きする」原則をファイル単位に適用）。
`loop_definition.py` はロード時に両ディレクトリを走査し、`id` をキーに dict へマージすることで
この優先順位を実現する。

---

## 6. audit 連携

> 参照: 基本設計 10.1 節（既存資産の再利用）、11 節（FT-11トレーサビリティ）。

### 6.1 emit タイミング

| イベント         | emit タイミング                                                               |
| ---------------- | ----------------------------------------------------------------------------- |
| `loop_start`     | `loop_step.py start`（1.3 節）の worktree 作成・state 初期化直後              |
| `loop_iteration` | `loop_step.py complete`（1.5 節）の state 更新直後（1 反復＝1 イベント）      |
| `loop_stop`      | `propose` が `exit_success`/`exit_failure`/`stop` を返し state を確定した直後 |

LP-2（`loop_driver.py`）も同じタイミングで同じ関数を呼ぶ（`loop_common.py` に emit 呼び出しを
集約し、LP-1/LP-2 で二重実装しない）。

### 6.2 payload スキーマ

`packages/audit/hooks/event_logger.py` の `emit_event(event_type, data, *, session_id, tid, ptid,
aid, ctx, project_dir)` をそのまま呼ぶ。`loop_common.py` は他パッケージからの import で
`packages/quality-gates/hooks/post-test-analysis.py` と同じ sys.path 追加パターン
（`packages/audit/hooks` を候補ディレクトリとして追加してから `from event_logger import
emit_event, ...` する）を踏襲する。

```jsonc
// loop_start
{
  "loop_id": "a1b2c3d4-issue-42",
  "definition_id": "issue-loop",
  "issue_number": 42,
  "worktree_path": "/path/to/worktree",
  "branch": "loop/issue-42",
  "trigger": "lp1" // lp1 | lp2
}

// loop_iteration
{
  "loop_id": "a1b2c3d4-issue-42",
  "phase": "implementation",
  "iteration": 2,
  "action_id": "act-000004",
  "maker": { "agent": "backend-python-dev", "tool": "codex" }, // agent-routing 解決結果
  "checker": {
    "mechanical": { "passed": false, "failure_type": "test_failure", "error_type": "assertion" },
    "llm_review": { "reviewers": ["code-reviewer"], "critical": 0, "high": 1 }
  },
  "guard_snapshot": { "iteration": 2, "no_progress_count": 1 },
  "result": "continue" // continue | advance_phase | exit_failure
}

// loop_stop
{
  "loop_id": "a1b2c3d4-issue-42",
  "phase": "pr_review_response",
  "final_status": "exit_success", // exit_success | exit_failure | stopped（安全停止。1.4節）
  "stop_reason": null, // exit_failure時: guard_max_iterations | guard_no_progress | wall_clock_timeout 等
                        // stopped時: push_guard_violation | repo_identity_mismatch | foreign_live_lease（1.4節・2.6節・3.4節）
  "iterations_total": 4,
  "pr_number": 123
}
```

`aid`（agent-routing 解決で選ばれたサブエージェント識別子）を `emit_event` の `aid` 引数に渡すことで、
NF-03（Maker/Checker が別サブエージェントであることの事後確認）を audit ログから検証できるようにする。

### 6.3 `EVENT_TYPES` への追加差分

`packages/audit/hooks/event_logger.py` の `EVENT_TYPES`（`frozenset`）へ、既存値を変更せず
以下 3 件を additive に追加する（NF-01: 既存値には影響しない）。

```diff
 EVENT_TYPES = frozenset(
     {
         "session_start",
         "session_end",
         "prompt",
         "route_decision",
         "cli_call",
         "subagent_start",
         "subagent_end",
         "quality_gate",
         "instructions_loaded",
         "turn_end",
         "precompact",
+        "loop_start",
+        "loop_iteration",
+        "loop_stop",
     }
 )
```

- 本追加は `packages/audit` 側の変更であり、`loop-harness` パッケージの `manifest.json` は
  `packages/audit` を `depends`（7 節）に含める。
- `emit_event()` は未知の `event_type` に対し `ValueError` を送出する実装のため、この追加を
  行わない限り `loop_common.py` からの emit は失敗する（実装順序の制約として明記）。

---

## 7. manifest.json / packages 配布

> 参照: 基本設計 3 節（コンポーネント構成）。

```jsonc
{
  "name": "loop-harness",
  "version": "0.1.0",
  "description": "Trigger → Maker → Checker → 停止判定の反復ループ実行基盤（LP-1 伴走型 / LP-2 自律型）",
  "depends": ["audit", "quality-gates", "git-workflow"],
  "hooks": {},
  "files": [],
  "skills": ["loop-issue"],
  "agents": [],
  "rules": [],
  "config": ["config/loop-harness.yaml", "config/loops/issue-loop.yaml"],
  "scripts": [
    {
      "path": "scripts/loop_step.py",
      "description": "LP-1: propose/complete/reconcile/heartbeat/resume/start サブコマンド（JSON出力）",
    },
    {
      "path": "scripts/loop_driver.py",
      "description": "LP-2: 1 ループ = 1 プロセスの worker（claude -p でMaker/Checkerを駆動）",
    },
    {
      "path": "scripts/loop_scheduler.py",
      "description": "LP-2: discovery・同時実行cap・timeout監視・worker管理を行う常駐スケジューラ",
    },
    {
      "path": "scripts/loop_status.py",
      "description": "ループラン一覧・詳細確認・purge",
    },
  ],
  "facet_targets": ["claude"],
}
```

- `hooks: {}` — 本パッケージは hook を配布しない（基本設計 8 節: 合否判定は hooks に依存しない
  決定論的検証のみで完結させる方針のため、既存 hook 基盤への新規フック追加は行わない）。
- `depends: ["audit", "quality-gates", "git-workflow"]`（Codex レビュー指摘反映。P2）:
  - `audit` は 6 節の emit 先、`quality-gates` は Checker の機械検証（`failure_detector.analyze()`）
    の実体を提供する既存パッケージ（基本設計 10.1 節）。
  - `git-workflow` は `issue-fix`（worktree・ブランチ判定ロジックの移植元）・`pr-create`（成功出口
    の PR 作成。基本設計 10.1 節の既存資産再利用表）の配布元パッケージ。orchex の依存解決は
    **直接依存のみ**をチェックするため、間接的な資産再利用（ロジックの移植・スキルの踏襲）だけでは
    `git-workflow` 未インストール環境で `pr-create` 等が解決できず、ループの出口処理が機能しない。
    そのため `depends` に明示的に追加する。
- `config: ["config/loop-harness.yaml", "config/loops/issue-loop.yaml"]`（Codex レビュー指摘反映。
  P2）: 既存 `packages/*/manifest.json`（例: `packages/audit/manifest.json` の
  `"config": ["config/delegation-policy.json", "config/audit-flags.json"]`）と同形式の
  **文字列パスのリスト**とする。既存の package manager（`Package.load`/`install`/`sync`）は
  `config` を文字列パスの list として処理する実装であり、dict エントリにすると orchex
  install/sync が壊れる。dict 案で表現していた説明情報は失わず、以下に注記として残す:
  - `config/loop-harness.yaml`: ガード既定値・LP-2 並列上限・ポーリング間隔・保持期間・通知設定
    （5 節）
  - `config/loops/issue-loop.yaml`: Issue 消化ループ定義（基本設計 4 節）
- `skills: ["loop-issue"]` — `facets/instructions/loop-issue.md` +
  `facets/compositions/skills/loop-issue.yaml` から生成される `/loop-issue` スキル本体
  （基本設計 FT-02）。**両ファイルは本書執筆時点で未作成**であり、実装フェーズで新規作成が必要
  （8 節で申し送る）。
- `lib/` 配下（`loop_common.py` 等）は `manifest.json` の `scripts`/`files` いずれにも列挙しない
  （`packages/codex-harness` の `harness_common.py` が `manifest.json` の `scripts` に現れず
  `codex_run.py`/`codex_review.py` からの内部 import 専用モジュールとして扱われているのと同じ
  前例に倣う）。

---

## 8. 基本設計との差分・要確認事項（報告事項）

本書作成にあたり、基本設計（`design:loop-harness`）の記述と、詳細設計として与えられた確定値との
間に差分が生じた箇所があった。値・キー名が基本設計と食い違っていた 3 点（PR レビューポーリング間隔・
PR レビュー完了タイムアウトのキー名/値・通知チャネルの表現）は、**ドリフトプロトコル（上流である
基本設計を正とする）に従い、5 節の値・構造を基本設計に合わせて解消済み**である。残る差分は矛盾では
なく、基本設計が未確定のまま詳細設計に委ねていた構造の具体化、または基本設計の記述を補完する拡張
であるため、そのまま採用している。

| #   | 項目                           | 基本設計（`design:loop-harness`）の記述                                                                                                      | 本書での扱い                                                                                          | 種別                                                         |
| --- | ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 1   | 無進捗ガードのキー名           | `guards.no_progress.repeat`（10.3節・6.2節・FT-09で一貫）                                                                                    | `guards.no_progress.repeat`（同一名称を採用）                                                         | 名称差異なし                                                 |
| 2   | `lock.ttl_seconds` の構造      | 単一キー `lock.ttl_seconds`（LP-1/LP-2で目安が異なると5.2節で言及するのみ。10.3節では未分割の単一キーとして「詳細設計で確定」）              | `lock.ttl_seconds.lp1` / `lock.ttl_seconds.lp2` に分割                                                | 構造拡張（矛盾ではなく、基本設計が未確定だった構造を具体化） |
| 3   | `loop_step start` サブコマンド | 5.3節のサブコマンド一覧・3節のコンポーネント表のいずれにも `start` は明記されていない（7節の図では `propose`（state なし）が初期化まで担う） | `start --issue <N>` を独立サブコマンドとして新設し、`propose` は既存 state 専用に役割を限定           | 拡張（1.3節で理由を明記）                                    |
| 4   | `reconcile` の位置づけ         | 5.3節「`propose` 内部から呼ばれる照合処理」（内部処理）と、3節の表現「サブコマンドとして提供する」が文面上両立しにくい                       | 内部関数として`propose`から自動呼び出しされつつ、診断用に独立サブコマンドとしても公開する両立案を採用 | 明確化（1.6節）                                              |

---

## 9. 積み残し（本書のスコープ外）

以下は基本設計 12 節から引き継いだ申し送り事項のうち、本書（CLI/config 編）では扱わず、
別の詳細設計文書に委ねる:

| 項目                                               | 委譲先                                                          |
| -------------------------------------------------- | --------------------------------------------------------------- |
| 失敗シグネチャ正規化アルゴリズムの詳細             | core 編（`loop_common.py` / `loop_definition.py` 等の詳細設計） |
| severity 判定ロジック・`reviewer_allowlist` の実値 | pr-review 編                                                    |
| redaction の具体的検出パターン・実装共有           | core 編                                                         |
| `facets/instructions/loop-issue.md` の指示書本文   | skill 編（未着手）                                              |
