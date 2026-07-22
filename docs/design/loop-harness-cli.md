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

## 1. `loop_step.py`（LP-1: propose / complete / reconcile / heartbeat / resume / start / run-checker）

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
loop_step.py run-checker --loop-id <id> --action-id <id> --state-version <n> --lease-token <token> [--llm-result <reviewer>=@<file>]... [--project <path>]
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
    "maker_agent": "backend-python-dev", // issue-loop の保存済み Maker。未選定時は定義の auto
    "prompt_template": null, // run_maker / run_checker のときのみ facets 参照パス
  },
  "reason": "iteration 2: previous check failed (test_failure), guard not reached",
}
```

`action` 別の `params` 内容:

> **全 action 共通フィールド（フェーズ④実装レビュー反映）**: 下表の action 固有フィールドに
> 加えて、`issue_number` / `worktree_path` / `branch` / `repo_identity_verified` の 4 つを
> **全 action の `params` に共通付与**する。オーケストレーター（`/loop-issue` スキル）が
> Maker への cwd 固定・出口処理の Issue コメント可否判定（`repo_identity_verified`。
> pr-review 編 6.4 節）を、state.json を直接読まずに応答 JSON のみで行えるようにするため。
> `repo_identity_verified` は repo-identity-hash の再計算一致で導出する（3.4 節と同じ検証。
> 導出ロジックは `loop_common.py` の公開 API を単一ソースとして共有する）。
> **Issue #208（SEC-H2）強化**: `loop_common.is_repo_identity_verified()` は (1) 起動時に
> 記録した worktree `.git` gitlink 指紋の一致、(2) `find_dangerous_local_git_config()` による
> ローカル git config 改ざん（`insteadOf`/`pushurl`/`credential.helper` 等）の不在、(3) 起動時に
> root 側で記録した識別マテリアルの完全長（256bit）ダイジェスト一致、の 3 条件をこの順で確認する
> （いずれか失敗で `False`）。従来の 8 文字（32bit）切り詰めハッシュ再計算は、これらの新フィールド
> が存在しない既存ループ（state.json 移行前）向けのフォールバックとしてのみ残る。

| `action`               | `params` の主なフィールド                                                                                                                      |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `run_maker`            | `maker_agent`（`issue-loop` は保存済み値、未選定時は `auto`。他ループはフェーズ定義値）、`prompt_template`                                    |
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
4. `journal.jsonl` に `event: "completed"` を**先に**追記し（10.2 節の redaction を通す。durable な
   記録。core 編 6.4 節の順序に従う）、その後 `state.json` を更新する（`state_version` を
   インクリメント、`phase`/`iteration`/`last_check_result` 等）。この順序により、両者の間で
   クラッシュしても journal 優先の reconcile（core 編 2.4 節・7 章）が確実に復元できる
   （Codex レビュー指摘反映。P2）。
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
- 処理: `journal.jsonl` に `event: "resumed", actor: "human"` を**先に**追記し（durable な記録。
  core 編 6.4 節の順序に従う）、その後 現在フェーズの `iteration` を `0` に、`no_progress` カウンタ・
  `infrastructure_failure` カウンタをリセットし、`status` を `running` に戻す形で `state.json` を
  更新する。あわせて**新しい `lease_token` を発行して `lock.json` に書き込む**（1.9 節。旧 token は
  以後無効。人間判断による再開のたびに lease を再発行し、失効済み token を握った古い呼び出し元を
  確実に締め出す）。
- 応答: `propose` と同じ形式で、リセット後の最初のアクションを返す。加えて、新規発行した
  `lease_token` をレスポンスに含める（呼び出し側はこれを保持し、以後の `propose`/`complete`/
  `reconcile`/`heartbeat` に渡す。1.9 節）。
- **`loop_status.py purge` との TOCTOU 対策（2巡目レビュー反映。SN-flock）**: reload〜lease 発行〜
  `state.json` 書き込みの全区間を `loop_common.held_coord_lock`（削除対象外の固定パス
  `.claude/loop/<loop_id>.coord.lock`）で保護する。同一 `loop_id` に対する `purge`（4.3 節）が
  進行中であれば `resume` はその完了までブロックされ、`lock.json` 自体の inode 差し替え
  （`rmtree` による削除）を経由したレースは発生しない（詳細は 4.3 節）。

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

1. 対象 `loop_id` の `state.json.status` を確認する。`pending`/`running`/`waiting_external`
   以外（`passed`/`failed`/`stopped`）は exit `1`（`{"error": {"code": "invalid_state", ...}}`）。
   `failed`/`stopped` からの再開は `resume`（1.8 節）を使う。
   > **`pending` の受理（Issue #205 反映）**: `start` は初回 `run_maker` を pending 化した
   > 直後、呼び出し元がまだ `complete` を呼んでいない段階では `state.json.status` が
   > `pending` のまま残る。この段階で呼び出し元セッションが断絶すると、`resume` は
   > `failed`/`stopped` 専用で使えず、従来は復旧経路が存在しなかった（state ディレクトリを
   > 手動削除して `start` をやり直すしかなく、journal を失う）。`attach` は `pending` も
   > 受理し、旧 lease が stale であれば手順 3〜5 と同じ reconcile 経路（1.4 節の
   > `_mark_unresolved_pending`）で孤立した初回 `run_maker` pending action を infrastructure
   > failure として reconcile し、`run_maker` を再度 propose する（同一 `loop_id`・journal を
   > 維持したまま復旧できる）。ガードは対象プロジェクトの実効 config（5 節。
   > `loop-harness.local.yaml` の上書きを含む）で評価される（PR #229 レビュー反映。以前は
   > パッケージ既定値 `DEFAULT_CONFIG` に固定されており、プロジェクトが
   > `guards.infrastructure_failure.max_retries` を下げていても既定値まで re-propose し続ける
   > 不具合があった。`status` が `running` の場合の同経路にも共通する既存不具合であり、
   > `pending` 固有ではない）。`infrastructure_failure.max_retries` を使い切っていれば通常の
   > ガード評価どおり `failed` に倒れる。なお `loop_scheduler.py`（3.3 節）は `pending` を
   > discovery・自動 respawn から引き続き除外する（#G10、restart storm 回避）。この手動
   > `attach` 経路は、その除外方針とは独立した、人間／LP-1 が明示的に呼び出す復旧手段である。
2. 現在の `lock.json` の lease が**生存中**（TTL 内かつ heartbeat が継続している。基本設計 6.3 節
   `is_lease_alive()`）かどうかを判定する。生存中であれば、まだ別のプロセスが正当にループを保持
   していると判断し **exit `3`** で拒否する（二重 attach による同時書き込みを防ぐ。旧プロセスが
   実は生きている場合に誤って乗っ取らないための安全策）。
3. lease が stale（TTL 超過、または heartbeat 途絶）であることを確認できた場合のみ、
   `reacquire_lease()`（core 編 6.3 節）で新しい `lease_token` を発行し `lock.json` を更新する
   （TOCTOU 緩和のうえで奪取。旧 token は以後 `validate_lease()` に通らなくなる）。
   `reacquire_lease()` は 1.8 節と同じ `held_coord_lock`（SN-flock）でこの区間全体を保護するため、
   同一 `loop_id` に対する `purge`（4.3 節）が進行中であれば `attach` もその完了までブロックされる。
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

### 1.11 `run-checker`（決定論的 Checker 実行と封緘 artifact。フェーズ④実装レビュー反映）

> 参照: core 編 7.5 節（封緘検証の契約）、pr-review 編 5.3 節（Checker 実行の責務分担）。

pr-review 編 5.3.1 節の「機械検証は LLM を介さず Python が直接 subprocess 実行する」を LP-1 で
実現する配線。オーケストレーターは `propose` が `run_checker` を返したら、LLM レビュー
（pr-review 編 5.3.2 節）を Task で実行して結果 JSON をファイルに保存したうえで、本サブコマンドを
呼ぶ。本サブコマンドは**アクションを complete しない**（two-phase の complete は別途呼ぶ。
core 編 7.5 節の封緘検証が、complete に渡された結果と本サブコマンドの生成 artifact の
canonical JSON 一致を強制する）。

**引数**: `--loop-id` / `--action-id` / `--state-version` / `--lease-token`（必須）/
`--llm-result <reviewer>=@<file>`（繰り返し可。レビュアー名と結果 JSON ファイルの束縛）/
`--project`。

**処理順序**:

1. **fence 検証**: lease を検証・更新し、`pending_action` が
   `action_id`/`state_version`/`action == run_checker`/`phase` すべて一致することを確認する
   （不一致は exit `2`。1.2 節の規約どおり）
2. **LLM 結果の取り込み**: `--llm-result` は 1〜2 件（`code-reviewer` を必ず含み、レビュアー重複
   禁止。pr-review 編 5.3.2 節の選定規則）。各ファイルは 5.1 節スキーマのキー集合完全一致で
   検証し、違反は拒否する。ファイル読み込みは regular file 限定・シンボリックリンク拒否
   （`O_NOFOLLOW`）・**パーミッション 0600 必須**・サイズ上限付きで行う。取り込んだ各 finding の
   `source` は束縛されたレビュアー名で上書きする
3. **機械検証の実行**: ループ定義の `checker.mechanical.commands` を `worktree_path` を cwd として
   subprocess 実行し（タイムアウト既定 1800 秒/コマンド）、`failure_detector.analyze()` で正規化
   する。heartbeat と fence 再検証は各コマンドの実行前後で行う（コマンド実行中の連続監視では
   ない。長時間実行をまたぐ lease 失効・奪取をコマンド境界で検知する）
4. **集約と封緘**: 機械層 + LLM 層を `combine_check_results()`（`pass_criteria` はループ定義の
   `checker.llm_review.pass_criteria` 由来。core 編 7.5 節の単一ソース規則）で集約し、
   `metadata.reviewers` に実際に取り込んだレビュアー名の manifest を付与、redaction を通して
   `artifacts/<action_id>/check_result.json` に保存する（core 編 7.2 節の保存契約。これが
   core 編 7.5 節の「封緘 artifact」。署名は redaction 適用後の findings に対して計算する）。
   機械検証の生ログと LLM レビュー結果も core 編 7.2 節の命名（`mechanical_<n>.log` /
   `llm_review_<reviewer>.json`）で同 artifact ディレクトリに保存し、各層の
   `raw_artifact_path` は **artifacts 配下のこれらのパス**を指す（オーケストレーター側の
   一時ファイルを指さない。reconcile・監査の復元元は artifacts 配下で自己完結させる）
5. stdout に封緘した `PhaseCheckResult` JSON を 1 行で返す。オーケストレーターはこれを
   **改変せずそのまま** `complete --result` に渡す

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

> **多層防御（defense-in-depth）の追記（2026-07-12 実機検証反映。EV-49・EV-63）**: `claude -p`
> の headless 権限挙動を実測した結果、`--disallowedTools "Bash(git push:*)"` は Maker が
> **直接** `git push origin HEAD:main` をツール呼び出しした場合は拒否できるが、Maker が
> `bash -c "git push origin HEAD:main"` のようにラッパー経由で実行すると **push が貫通する**
> （disallow パターンはコマンド文字列のリテラル前方一致のみを検査し、サブシェル内のペイロードま
> では検査しない）。実際に検証で bare remote への push が進行した。したがって
> `--disallowedTools` は必要だが、単独では「Maker は push できない」という構造的保証にはならない
> と確定した。これを踏まえ、以下 4 層を組み合わせて防御する（層2 が主軸）:
>
> - **層1（プロンプト）**: Maker には push/PR 作成を一切指示しない。Maker の責務は編集と
>   `git commit` までとする（プロンプトテンプレートの骨子を参照）。
> - **層2（env 認証隔離。主軸）**: `run_claude_p()`（2.5 節）が Maker の子プロセスへ渡す `env` から
>   push 認証を剥奪する。具体的には `GIT_ASKPASS=/bin/false`・`GIT_TERMINAL_PROMPT=0` を設定し、
>   `SSH_AUTH_SOCK` を unset、`GH_TOKEN`/`GITHUB_TOKEN` 等の認証トークン系 env を継承しない。
>   remote が認証必須である前提のもとでは、`bash -c` を含むあらゆる wrapper 経由の push も認証段階
>   で失敗するため、コマンド文字列のパターンマッチに依存しない構造的な保証になる。この隔離は
>   Maker の子プロセス env のみに適用し、git worktree が共有する `.git/config` には触れないため、
>   `loop_driver.py`（親プロセス）自身の push 能力には影響しない。
> - **層3（disallowedTools + hook）**: 現行の `--disallowedTools
>   "Bash(git push:*),Bash(git remote:*),Bash(git worktree:*),Bash(gh pr:*)"` は多層防御の一枚として
>   維持する。加えて `claude -p` に渡す PreToolUse hook で Bash コマンド全文（`bash -c` のペイロード
>   を含む）を検査し、push/remote/gh pr を含む場合は hard-deny する（disallowedTools のリテラル
>   一致漏れを補う）。ただし文字列難読化（base64 化・変数展開等）で回避され得るため、層3 単独を
>   構造的保証の境界として扱わない。実体は `packages/loop-harness/lib/maker_bash_guard.py`
>   （stdlib のみ・PreToolUse プロトコルで stdin JSON を受け取り deny 時 exit code 2）で、
>   `loop_driver_support.build_claude_p_command()` が `maker_hook_settings_path()`（プロセス単位で
>   メモ化した scratch settings JSON を生成し `matcher: "Bash"` の PreToolUse hook として同スクリプトを
>   登録）の戻り値を常に `--settings` に付与して注入する（`loop_driver.py` 側の呼び出しコードは
>   変更不要）。
> - **層4（push 後整合性検証。安全網）**: `loop_driver.py` が Maker 実行の前後で「期待する
>   local HEAD」と「remote HEAD」を記録・照合し、想定外に remote HEAD が進行していた場合は
>   integrity violation として安全停止する（2.6 節）。
> - **層A（不採用）**: worktree の `origin` remote 除去・repoint は、git worktree が
>   `remote.*` を含む `.git/config` を共有するため、素朴に実装すると親プロセス（`loop_driver.py`）
>   の push も壊れる。既定では採用しない。どうしても worktree ローカルに remote 設定を分離したい
>   場合は `extensions.worktreeConfig=true` + `git config --worktree`（git 2.20+）が唯一の正しい
>   手段である。
> - **残余リスク（同一 UID 前提）**: 層2〜4 はいずれも Maker/Checker の**子プロセス境界**（別プロセス
>   としての env/権限分離）を防御対象としており、`.claude/loop/` 配下の journal/state ファイル自体
>   への書き込みに対して、driver と同一 UID で動作する別プロセスからの改ざんに対する完全性保証は
>   持たない。同一 UID 耐性の確保は本設計のスコープ外とし、Issue #211（Maker/Checker の完全プロセス
>   隔離）で別途対応する。

> **層2 が Checker（`mechanical.commands`）経由で迂回できる欠陥の修正（2026-07-12 LP-2 レビュー反映。
> SEC-C1・EV-81）**: `checker.mechanical.commands`（例: `pytest -q`）は直前の Maker 反復が仕込んだ
> コードを import/実行しうるにもかかわらず、`loop_common.run_mechanical_checks()` が
> `env=` を渡さず driver 自身の push 権限付き env（`os.environ`）を継承していたため、層2（Maker の
> `claude -p` 子プロセス env のみを隔離する）が Checker 経由で完全に迂回できた（Issue #196 相当を
> Checker 経由で再現可能）。`run_mechanical_checks()`/`_run_mechanical_command()` に `env`
> キーワード引数を追加し（省略時は従来どおり `os.environ` を継承。LP-1 の `loop_step.py` は
> 省略のまま）、`loop_driver.py`（LP-2 `_run_checker`）からは Maker と同じ隔離 env（下記
> `maker_env()` 強化版）を渡す。

> **`maker_env()` の env 変数以外の認証経路も遮断（2026-07-12 LP-2 レビュー反映。SEC-H3・EV-83）**:
> `GIT_ASKPASS`/`GIT_TERMINAL_PROMPT`/`SSH_AUTH_SOCK`/`GH_TOKEN`/`GITHUB_TOKEN` の剥奪だけでは
> `GIT_SSH_COMMAND`（カスタム SSH 経由の push 認証）と `$HOME` 相対の認証経路（`~/.netrc`、
> `credential.helper=store` の `~/.git-credentials`、macOS Keychain の `osxkeychain`、`gh` の
> `~/.config/gh/hosts.yml`）が素通りする。`maker_env()` は追加で `GIT_SSH_COMMAND` を unset し、
> `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` を常に `/dev/null` にリダイレクトして（`credential.helper`
> を含む）グローバル/システム git config を一切読ませないようにする。さらに任意で `scratch_home`
> （ループ単位の空ディレクトリ、`loop_driver_support.maker_scratch_home()`）を渡すと `HOME`/
> `XDG_CONFIG_HOME` もそこへリダイレクトされ、`~/.netrc`・`gh` 設定探索も空になる。Maker/LLM
> レビュアー/severity 分類/Checker（`mechanical.commands`。SEC-C1）の全 `claude -p` 子プロセス・
> mechanical コマンド実行に、この強化版 env を一貫して適用する。

> **層3の `Edit`/`Write` 迂回の修正 + driver 側 git config hardening（2026-07-14 LP-2 2巡目
> Codex セキュリティレビュー反映。SEC-CRIT）**: 層3 `maker_bash_guard.py` は `matcher: "Bash"`
> でのみ発火しており、Maker に無制限許可されている `Edit`/`Write`（`MAKER_BASE_ALLOWED_TOOLS`）
> は一切検査対象外だった。Maker は Bash を経由せず `Edit` で共有 worktree の `.git/config` を直接
> 書き換え、`[remote "origin"] url` の repoint や `[url "<evil>"] insteadOf`/`pushurl`/
> `credential.helper` エントリを仕込める。前者は「driver が `"origin"` という remote 名で解決する
> 先」を、後者は「driver が使う任意の URL 引数そのもの」を書き換え可能で、driver 自身が実資格情報
> で行う push/`ls-remote` を攻撃者リモートへリダイレクトさせ得る（**実機検証で確認**: 解決済みの
> 生 URL をリテラル引数として渡した push でも、`.git/config` に一致する `insteadOf` エントリが
> あれば依然としてリダイレクトされた — 単に remote 名解決を経由しないだけでは不十分）。
> 対応は 2 段構え（両方が揃って初めて十分。片方だけでは不十分）:
>
> 1. **主対策（driver 側 git config hardening）**: `loop_driver_support.hardened_git_config_args()`
>    （`-c credential.helper=`）を driver 発の全 git subprocess 呼び出しに付与する。加えて
>    `resolve_origin_url()` で `origin` の生 URL を**信頼できる最初のタイミング**（lease 取得直後、
>    このプロセスで Maker が一度も走っていない時点。`_reconstruct_push_integrity_baseline()`）で
>    一度だけ解決し `LoopDriver._trusted_origin_url` にキャッシュ、以降の push/`ls-remote`
>    （`get_remote_head()` の `origin_url` 引数）はこの生 URL を直接指定する（`"origin"` という
>    remote 名は二度と解決に使わない）。ただしこれだけでは `insteadOf`/`pushurl` によるリテラル
>    URL 引数の書き換えを防げない（上記実機検証）ため、`find_dangerous_local_git_config()` が
>    driver-owned push/`ls-remote` の**直前に毎回**共有 worktree のローカル git config を実際に
>    走査し、`insteadof`/`pushurl`/`credential\.helper`/`^alias\.` のいずれかが存在すれば
>    `LoopDriver._verify_no_git_config_tampering_or_stop()` が `git_config_tampered` として安全
>    停止する（`_verify_push_integrity_or_stop`/`_scan_for_leaked_secrets_or_stop` と同じ停止
>    シーケンス）。`protocol.file.allow=never` は意図的に含めない — このパッケージ自身のテスト
>    群がローカルパス remote を使うため、tamper の有無にかかわらず file transport を一律拒否する
>    ことになり、無害な既存構成まで壊す（実機検証で確認）。
> 2. **補助対策（層3 の `Edit`/`Write` 拡張）**: `maker_hook_settings_path()` の
>    matcher を `"Bash"` から `"Bash|Edit|Write"` に拡張し、`maker_bash_guard.py` が
>    `Edit`/`Write` の `tool_input.file_path` も検査、`.git` パスコンポーネントを含む書き込みを
>    hard-deny する（`is_git_metadata_path()`）。層3 はあくまでベストエフォートの補助層であり
>    （完全な shell 解析は目的としない）、主たる構造的保証は上記 1 の driver 側 hardening。

> **層3 の残バイパス強化（2026-07-14 LP-2 2巡目レビュー反映。SEC-MED、ベストエフォート）**:
> `maker_bash_guard.py` の全 deny パターンを大文字小文字を区別しない照合に統一し、クオート/
> バックスラッシュを除去した正規化テキストでも再スキャンする（`g"i"t push` のようなトークン分断
> 回避への対処）。また `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`/`GIT_CONFIG_COUNT`（env var 経由の
> git config 注入。`-c`/`config` という文字列を一切含まずに同じキーを設定できる）と
> `credential.helper` も deny 対象に追加した。これらはいずれも完全な shell 解析を代替するもの
> ではなく、層2（env 認証隔離）+ 上記 driver 側 git config hardening が構造的保証である点は
> 変わらない。

> **`_verify_maker_push_baseline_or_stop()` の TOCTOU（SEC-LOW、対応不要・受容）**: この関数の
> `get_remote_head()` 読み取りと `_run_maker` がその戻り値を新ベースラインとして採用するまでの
> 間に理論上の TOCTOU window があるが、この window で走る out-of-band push は次の driver-owned
> push 時点の層4チェック（`_verify_push_integrity_or_stop`）で改めて検出されるため「検出漏れ」
> ではなく「検出が1周期遅れる」だけであり、脅威モデル外として対応しない。

> **`mechanical.commands` denylist の追加バイパス対策（2巡目レビュー反映。SN3-extra）**:
> `loop_definition._validate_mechanical` の SEC-M1 denylist スキャンは、`find . -exec git push
> \;`（`find` 自身は wrapper 扱いではないため `-exec`/`-execdir`/`-ok`/`-okdir` 節の中身が
> 未走査だった）、`env -S 'git push'`（`--split-string` は単一の値引数ではなく埋め込みコマンド
> 文字列全体を意味するため、他の `env` フラグと同様に単純スキップすると中身が未検査になる）、
> `g\it push`（バックスラッシュでの命令名分断）の 3 パターンについて追加の正規化を行う。
> いずれも「層2主軸・完全な shell 解析は目的としない」というスキャンの既存スコープの範囲内の
> 部分対処であり、`find` の複数 `-exec` 節や `-o`/括弧を用いた複雑な条件式までは対象としない。

> **denylist の残バイパスは受容リスクとして確定（3巡目レビュー反映。SN3-accept）**: ネストした
> `find -exec ... find -exec ... ;` の組み合わせ、`xargs` 経由の実行（`echo git push | xargs -I{}
> sh -c '{}'`）、`if`/`then`/`fi` 等の shell 制御構文でのラップ、隣接する文字列リテラルへの分割
> （`g''it push`）、`env -Sfoo=bar`（スペース無しの attached 形式）等の追加バイパスは、これ以上
> 個別パッチしない。本 denylist は **信頼された** ループ定義作者の YAML に対する best-effort の
> 静的チェックであり、うっかり denylist 対象バイナリを書いてしまう事故の防止が目的であって、
> 悪意ある作者による意図的なバイパスへの耐性は元々スコープ外である。信頼できない実行主体
> （Maker）による任意コマンド実行の防止は、この denylist ではなく層2（push 認証剥奪。EV-49・
> EV-83）と層3（`--disallowedTools`/hook ベースの hard-deny。EV-49・EV-63）が担う、シェル文字列
> パターンマッチに依存しない構造的な境界である。信頼された作者という脅威モデルの前提が変わり
> severity が再評価されない限り、追加の denylist バイパスパッチは行わない。

> **driver 側 git config hardening の追加強化（3巡目 Codex セキュリティレビュー反映。RC1〜RC3・
> RH1〜RH3・RM1）**: 2巡目対応（上記 SEC-CRIT）で閉じた経路の残り穴を追加で塞ぐ。
>
> - **RC1（`remote.<name>.url` による URL pin 無効化。実機再現済み）**: git は push/`ls-remote`
>   の引数をまず**設定済み remote 名**として解決してからリテラル URL 扱いする。ピン留めした生
>   URL 文字列そのものを名前とする remote（例:
>   `git config remote.https://good.example/repo.git.url <evil>`）を Maker が追加すると、その
>   remote 名解決がハイジャックされ、リテラル URL 引数を渡した push/`ls-remote` でもリダイレクト
>   され得る。`_DANGEROUS_LOCAL_CONFIG_KEY_RE`（`loop_driver_support.py`）に
>   `remote\.(?!origin\.)\S+\.url`/`remote\.(?!origin\.)\S+\.pushurl` を追加して検出する。`origin`
>   サブセクションは意図的に除外する — 除外しない場合、全ての正規な push が依存する既存の
>   `remote.origin.url` 自体に常に一致してしまい、このチェックが恒久的に fail-closed（＝実用不可）
>   になるため。
> - **RC2（Bash リダイレクト/`tee` 経由の `.git` 書き込み）**: `maker_bash_guard.py` の deny
>   パターンは `git`/`gh` 系の語彙一致のみで、`printf '...' >> .git/config` /
>   `tee .git/hooks/pre-push` のような素のシェルリダイレクトは検査対象外だった。リダイレクト
>   演算子（`>`/`>>`）または `tee` の対象パスに `.git` パスセグメントが含まれる場合を deny する
>   パターンを追加した（`.gitignore`/`.github` 等の紛らわしい類似パスは誤検知しないよう境界を
>   厳密化）。
> - **RC3（attach/resume 時の pin 前チェック欠如）**:
>   `LoopDriver._reconstruct_push_integrity_baseline()` は `resolve_origin_url()` で origin URL
>   を信頼済みとしてピン留めする**前に** tampering チェックを行っていなかった。driver の
>   再起動/attach/resume はこの関数自身の存在理由（クラッシュ後の worker 再起動）そのものであり、
>   その時点で共有 worktree の `.git/config` は**既に**汚染されている可能性がある。ピン留めの
>   直前に `_verify_no_git_config_tampering_or_stop()` を先に走らせ、tampering 検出時はピン留め
>   せず安全停止する。
> - **RH1（`resolve_origin_url()` が `None` の場合の `"origin"` フォールバック再発）**:
>   `_push_verified_branch()`/`get_remote_head()` はいずれも `origin_url` 未解決時に素の
>   `"origin"` 名へフォールバックする汎用契約を持つ（この契約自体は他の呼び出し元のため意図的に
>   維持）。しかし driver 自身がこのフォールバックを本番経路で踏むことは、まさに RC1 が塞ごうと
>   している remote 名解決のインダイレクションを自ら経由することになり本末転倒。
>   `_reconstruct_push_integrity_baseline()` が `resolve_origin_url()` の失敗を検出した時点で
>   `origin_url_unresolvable` として即座に安全停止するようにし、driver プロセスが実際に
>   `_trusted_origin_url is None` のまま push/`ls-remote` へ進むことがないようにした。
> - **RH2（dangerous config key の網羅不足）**: `core.fsmonitor`/`core.sshCommand`/
>   `core.askpass`/`core.hooksPath`（外部コマンド実行）、`diff.*.command`/`diff.external`、
>   `filter.*.(clean|smudge|process)`（外部フィルタ実行）、`include.path`/`includeif.*`（別ファイル
>   の設定を注入）を `_DANGEROUS_LOCAL_CONFIG_KEY_RE` に追加。`include.path`/`includeif.*` は
>   注入先ファイルの中身も可視化する必要があるため、走査コマンドを
>   `git config --local --list` から `git config --local --list --includes` に変更した。
> - **RH3（`is_git_metadata_path()` の大文字小文字判定）**: macOS 既定の大文字小文字を区別しない
>   （だが保持はする）ファイルシステムでは `.GIT/config`/`.Git/config` が `.git/config` と同一
>   ファイルに書き込める。各パスコンポーネントを `casefold()` してから比較するよう修正。
> - **RM1（`core.hooksPath=/dev/null` の適用漏れ）**: `-c core.hooksPath=/dev/null` は
>   `_push_verified_branch()` の push 呼び出し 1 箇所にのみ inline 指定されており、
>   `ls-remote`/`status`/`branch --show-current`/`rev-parse`/`config` 系など他の driver-owned
>   git 呼び出しには未適用だった。`hardened_git_config_args()` に含めて全 driver-owned git
>   呼び出しへ一律適用する（push 呼び出し側の inline 指定は重複となるため削除）。
>
> **本節のガードは深層防御であり完全性を保証しない**: RC1〜RC3・RH1〜RH3・RM1 はいずれも
> 「Maker と driver が同一 OS ユーザー（同一 UID）で共有 worktree の `.git/` にファイルシステム
> レベルで書き込める」という構造的限界に対する best-effort の追加緩和であって、この限界自体を
> 解消するものではない。既知の悪用経路を追加でいくつか塞いだに過ぎず、この文書がまだ想定して
> いない新しい経路が存在しない保証はない。この限界の根本対応（Maker/Checker を別ユーザーまたは
> コンテナへ完全に分離し、同一 UID 前提そのものをなくす）は Issue #211 のスコープであり、本節の
> どの追加チェックもそれを代替しない。

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
2. `journal.jsonl` に `event: "stopped", payload: {stop_reason: "push_guard_violation", ...}` を**先に**
   追記し（durable な記録。core 編 6.4 節の順序に従う。Codex レビュー指摘反映。P2）、audit へ
   `loop_stop`（`stop_reason` 付き）を emit する（6 節）。
3. `journal.jsonl` への追記後、`state.json.status = "stopped"`、`stop_reason = "push_guard_violation"`
   として記録する。
4. macOS 通知を**必ず**発火する。
5. Issue コメント投稿は、当該ループの repo-identity が検証済み（1.3 節の `start` 時点で記録した
   repo-identity と現在値が一致）の場合のみ行う。ブランチ検証違反のみで repo-identity 自体は正しい
   場合は投稿してよいが、repo-identity 不一致が絡むケース（3.4 節）は投稿しない。
6. worktree・state/journal は保持する（人間の調査・再開判断のため。FT-23 と同様の方針）。

**ephemeral GIT_DIR からの CAS 書き戻し失敗による安全停止（Issue #211 Phase 2）**:
Maker の commit を共有 common dir へ取り込む際は、完全修飾 ref のみを使い、
`git fetch --no-tags --no-write-fetch-head <ephemeral_dir>
refs/heads/<branch>:refs/loop-import/<action_id>` で action 固有の一時 import ref へ取り込んだ後、
fast-forward 検証と `git update-ref <branch_ref> <candidate_sha> <baseline_sha>` の原子的 CAS を行う。
次の 3 失敗は、後続の branch 書き戻し・push・PR 作成を中断する安全停止として区別する。

| 失敗点 | `stop_reason` | 必須の扱い |
| ------ | ------------- | ---------- |
| 一時 import ref への fetch 失敗、または import 後 SHA が事前に固定した ephemeral tip と不一致 | `git_ref_import_failed` | branch ref の更新へ進まず、action 固有の一時 import ref を削除する |
| `merge-base --is-ancestor <baseline_sha> <candidate_sha>` が exit 1 | `git_ref_not_fast_forward` | 非 fast-forward として CAS を実行せず、一時 import ref を削除する |
| `update-ref <branch_ref> <candidate_sha> <baseline_sha>` の期待値不一致 | `git_ref_cas_rejected` | この action による branch ref 更新を行わず、一時 import ref を削除する |

`merge-base --is-ancestor` の exit 1 以外の非 0 は、非 fast-forward ではなく Git コマンドの
実行障害であるため `git_ref_not_fast_forward` として記録せず、infrastructure failure として扱う。
また CAS 成功後の worktree `reset --mixed` 失敗は、branch ref がすでに更新済みであるため上記 3 種類の
安全停止とは区別した post-CAS infrastructure failure とし、自動 rollback は行わない。

一時 import ref の削除は成功・失敗の両経路で `finally` 相当により行い、ephemeral runtime の削除も
冪等にする。`prepare_ephemeral_git` は同じ `action_id` の古い runtime を事前に削除し、クラッシュ後の
同一 action 再試行でも残骸を再利用しない。削除対象は検証済み `action_id` から導出した
`refs/loop-import/<action_id>` のみに限定し、別 action の ref を wildcard で削除しない。
cleanup 自体が失敗した場合は元の safety stop を保持したまま cleanup failure を追加報告し、次の
「一時 import ref は削除済み」という定型コメントは使用しない。

安全停止コメントは次の事実に限定する。

> この action による対象 branch ref の更新、push、PR 作成は行われていません。一時 import ref は
> 削除済みです。fetch 済み object は到達不能のまま共有 object DB に残る可能性があります。

fetch により共有 object DB への object 書き込みが起こり得るため、「リポジトリへの書き込みは
行われていない」とは表現しない。action を横断した一時 import ref の sweep は Phase 2 のスコープ外の
follow-up とし、将来導入する場合は lease/state を確認して進行中 action の ref を削除しない設計とする。

**push 後整合性検証（層4。2.2 節の多層防御）による安全停止**: 2.2 節の層2（env 認証隔離）を
主軸としつつ、その安全網として `loop_driver.py` は `on_success.exec` の `push` 実行の前後で
「期待する local HEAD」と「remote HEAD」を記録・照合する。具体的には、直前の反復完了時点の
remote HEAD を baseline として記録しておき、Maker（`claude -p`）実行直後・`push` 実行直前に
`git ls-remote origin <branch>` 等で現在の remote HEAD を取得し baseline と比較する。
`loop_driver.py` 自身がまだ push していないにもかかわらず remote HEAD が baseline から進行して
いた場合、Maker がガードを迂回して push した疑いがある（Issue #196 相当の「push したのに報告
しない」ケース）ため、以下の**安全停止**を行う（本節 1〜6 と同じ順序契約。`stop_reason` のみ
異なる）:

1. `push`/`pr_create` 等、**リポジトリへの書き込みを伴う exec を一切実行しない**（`on_success.exec`
   の残りステップを中断する。すでに想定外の push が行われているため、二重 push・PR 作成による
   状態悪化を避ける）。
2. `journal.jsonl` に `event: "stopped", payload: {stop_reason: "push_integrity_violation", ...}` を
   **先に**追記し（durable な記録。core 編 6.4 節の順序に従う）、audit へ `loop_stop`（`stop_reason`
   付き）を emit する（6 節）。
3. `journal.jsonl` への追記後、`state.json.status = "stopped"`、`stop_reason = "push_integrity_violation"`
   として記録する（baseline HEAD と検知時 remote HEAD の両方を payload に残す）。
4. macOS 通知を**必ず**発火する。
5. Issue コメント投稿は、当該ループの repo-identity が検証済みの場合のみ行う（本節 5 と同じ条件）。
6. worktree・state/journal は保持する（人間の調査のため。想定外 push が到達した remote ブランチの
   状態も含め証跡を残す。FT-23 と同様の方針）。

> **fail-closed 化（2026-07-12 LP-2 レビュー反映。SEC-H1・EV-82）**: `git ls-remote` 失敗（sabotage・
> ネットワーク障害等で current HEAD が取得不能）や baseline 未記録（`None`）を「違反なし」として
> push を通過させる fail-open な実装は、層2を突破した攻撃者が `git ls-remote` を意図的に失敗させる
> ことで層4を無力化できてしまう。`current_head` 取得は 1 回まで再試行したうえで、なお `None` の場合
> （または `baseline_head` が `None` の場合）は「違反」でも「合格」でもなく **`stop_reason =
> "push_integrity_unverifiable"`** として本節と全く同じ手順（journal 先行・状態停止・通知必須・
> 条件付き Issue コメント・worktree/state 保持）で安全停止する（fail-closed）。baseline 未記録が
> クラッシュ再起動直後の空白期間で起きないよう、`loop_driver.py` は `attach()` 直後（lease 取得
> 直後）に一度 `git ls-remote` で baseline を再構築する。

> **確認できたブランチ不在の区別（2026-07-13 レビュー反映。Issue F6）**: 上記の「`git ls-remote`
> 失敗」は「照会コマンド自体が失敗し、真偽を確認できない」場合であり、「照会は成功したが対象
> ブランチが `origin` にまだ存在しない」場合（ラベル付き新規 Issue の loop が最初の push を行う
> 前の状態）とは区別する。後者は `git ls-remote` が正常終了した上での確認済みの不在であり、
> `get_remote_head()` は専用の sentinel（`REMOTE_HEAD_ABSENT`）を返すことで `None`（照会失敗）
> と書き分ける。baseline・current の両方が確認済み不在の場合は初回 push の正当な状態として
> `"ok"` を許可し、片方だけが確認済み不在（もう一方が sha）の場合は従来通り `"violation"` と
> なる。照会自体が失敗した場合（`None`）のみ、引き続き fail-closed で `"unverifiable"` とする。

---

## 3. `loop_scheduler.py`

> 参照: 基本設計 3 節（コンポーネント表）、8 節（LP-2 実行フロー）、5.6 節（repo-identity 照合）。

### 3.1 discovery（ラベル付き Issue キュー）

```bash
gh api repos/{owner}/{repo}/issues \
  --method GET \
  -f labels=<trigger.lp2.label の解決値> \
  -f state=open \
  -f sort=created \
  -f direction=asc
```

- `<trigger.lp2.label の解決値>` は、対象ループ定義（`config/loops/*.yaml`）の `trigger.lp2.label`
  から解決する値であり、`loop_scheduler.py` 自体はラベル名をハードコードしない（Codex レビュー
  指摘反映。P2。コマンド例のプレースホルダ表記を実際の解決方法と一致させた）。基本設計 4 節の
  `issue-loop.yaml` 例では `label: "loop:queue"` としているが、実運用のラベル名はプロジェクトごとに
  `config/loops/*.yaml` 側で定義する。
- **優先度順**: `created_at` 昇順（先に登録された Issue を優先。公平性を優先し、複雑な優先度スコア
  は当面導入しない）。Issue に `priority:high` 等の追加ラベルがある場合はそれを最優先ソートキーとし、
  同順位内は `created_at` 昇順とする（優先度ラベルの語彙は config で定義。5 節）。
- 既に `loop_id`（`<repo-hash8>-issue-<N>` 決定論的採番）に対応する `state.json` が
  `running`/`waiting_external` で存在する Issue は discovery 対象から除外する（二重起動防止）。
- 加えて、`state.json.status` が `passed`/`failed`/`stopped`（terminal）である `loop_id` の Issue も
  discovery 対象から除外する。ラベルが外されないまま残っている完了済み Issue を、ラベルだけを見て
  再度ループ生成しないようにするための除外である（`discover_loop_ids` で running/waiting_external と
  同様に扱う）。
- **purge 済み loop の tombstone 除外（2巡目レビュー反映。SN2）**: `loop_status.py purge` は
  `state.json`/`journal.jsonl`/`artifacts/` を削除した後、`<loop_id>.tombstone.json`（最小構成:
  `loop_id`/`status`/`purged_at`）を `.claude/loop/` 直下に残す。`_terminal_loop_ids` はこの
  tombstone も terminal 扱いに含めるため、purge 済み Issue のラベルが残っていても discovery が
  同一 `loop_id` を即座に再生成することはない（上記の terminal 除外と同じ理由）。tombstone 自体は
  purge の対象外（`--force` でも削除しない）。`loop_status.py list` は tombstone を
  purge 時点の status のまま 1 行として表示し続ける（`PHASE` 列には `purged` を表示）。

### 3.2 同時実行 cap

- **確定値: 2（`lp2.concurrency_limit=2`、config で変更可）**。
- `loop_scheduler.py` は起動中の worker プロセス数を内部で追跡し、cap に達している間は discovery
  結果があっても新規 worker を spawn しない（次回ポーリング時に再評価）。
- **occupancy 計算は 3 経路で共通化（2巡目レビュー反映。SN1）**: cap の空き枠計算は
  `_available_worker_slots`（`runtime.workers` の追跡数 + 未追跡だが lease が生存中の
  active loop 数）に一本化されている。`spawn_new_workers`（新規 discovery からの spawn）だけでなく、
  `respawn_orphaned_active_loops`（3.3 節の scheduler-restart 復旧）と foreign-lease cooldown 経過後の
  再起動（3.3 節）も同じ計算を使う。以前はこの 2 経路が `concurrency_limit - len(runtime.workers)`
  のみで空き枠を判定しており、scheduler 再起動直後などに存在する「未追跡だが lease 生存中」の
  active loop を勘定に入れず、cap を超えて worker を spawn しうる欠陥があった。
- **空き枠計算そのものの非アトミック性（受容リスク。3巡目レビュー反映。RM1-accept）**: `_available_
  worker_slots` で空き枠を計算した直後・実際に spawn する直前の間隙で、期限切れ lease を
  `attach()` が奪取する（別の呼び出し元が同じ loop_id を再アタッチする）と、その一瞬だけ cap を
  超えうる。これは lease 設計全体が「厳密な cap 遵守」より「安全性（二重 attach の防止・lease
  fencing）」を優先する既知のトレードオフであり、本レビューでは追加のロック機構を導入せず受容する。

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
     はならない。**この即時再起動も 3.2 節の `_available_worker_slots` による cap 判定を経てから
     spawn する（3巡目レビュー反映。RH4）**: 全ての終了 worker を reap（`runtime.workers` から
     除去）し終えてから再起動候補をまとめて集約し、空き枠がある分だけ spawn する。cap に達して
     いて spawn を見送った候補は、その `state.json` が非終端のままなので次サイクル以降
     `respawn_orphaned_active_loops`（lease 失効後）に拾われる。
  3. cap に空きがあれば discovery 結果から新規 worker を spawn する。
- 再起動回数には上限を設けず、`loop_common` 側のガード（`infrastructure_failure` の
  `max_retries=3`。5 節）が最終的な打ち切りを担う（スケジューラ自体は無限再起動しうるが、
  ガード評価が失敗出口へ導く）。
- **foreign-lease cooldown（restart-storm 対策）**: worker が lease 取得時点で他プロセス保有と
  思われる lease を検出して起動を拒否した場合、`state.json.status` はまだ `running`（旧所有者の
  ものである可能性がある）ため、通常の異常終了と同様にすぐ再起動すると restart-storm を招く。
  スケジューラはこの `loop_id` の再起動を `lp2.lease_ttl_seconds` 分クールダウンさせ、その間は
  discovery・再起動の対象から除外する（クールダウン経過後に再評価する。`SchedulerRuntime.
  foreign_lease_cooldown_until` で追跡）。
- **`pending` 孤児回復（Codex レビュー指摘反映 #H3/#H11。1.10 節の Issue #205 反映で `attach`
  自体は `pending` を受理できるようになったが、本節の自動 respawn 抑止方針は変更しない）**:
  `should_restart("pending")` は `False` を返す。`lc.attach()` 自体はもはや `pending` を
  拒否しないが（1.10 節）、scheduler はこれを**自動で**呼び出さない設計を維持する — 毎サイクル
  無条件に auto-respawn すると、旧 worker がまだ正当に完了しつつある最中でも re-attach を
  試み続け restart-storm になりうるため（#G10）。一方 `pending` は discovery からも常に
  除外される（3.1 節）ため、worker が初回 `run_maker` 完了前に死んだ場合や scheduler 自体が
  再起動した場合、そのままでは誰も拾えず永久に取り残される。`recover_orphaned_pending_loops`
  が毎サイクル `spawn_new_workers` の前に実行され、lease が実際に失効した（生存 owner が
  いない）`pending` loop のみを対象に、state dir を `.claude/loop/<loop_id>.orphaned-<n>` へ
  リネーム退避する（worker の respawn は行わない。Issue #205 の手動運用回避策の自動化）。
  これにより当該 Issue は次サイクルで新規 `loop_id` として discovery され直す（元の
  `loop_id`・journal は post-mortem 用に退避されるのみで失われないが、再開はされない）。
  lease が生存中の `pending` loop には触れない。人間／LP-1 が `recover_orphaned_pending_loops`
  による退避より前に気づいた場合は、`attach`（1.10 節）で同一 `loop_id`・journal を維持した
  まま直接復旧することもできる。
  > **retirement と attach の競合対策（PR #229 レビュー反映。SN-flock）**: 上記の事前フィルタ
  > （lease 失効チェック）は unlocked かつ TOCTOU の余地がある安価な絞り込みに過ぎない。実際の
  > rename は per-loop coord lock（1.10 節・4.3 節参照。`attach()` の `reacquire_lease` と同じ
  > 固定パス）の下で state・lease を再読込・再検証してから行うため、`attach` が僅差でこの
  > retirement に先行して lease を再取得していた場合はロック内の再検証で正しく no-op する
  > （逆に retirement が先にロックを取得していれば `attach` 側が `lock_unavailable`/
  > `invalid_state` で正しく失敗する）。

### 3.4 起動時の repo-identity 照合（安全停止）

- `loop_scheduler.py` 起動時に、実行対象ディレクトリから `repo-identity-hash`（基本設計 5.1 節）を
  再計算し、既存の `.claude/loop/*/state.json` に記録された値と照合する。不一致（想定外の
  リポジトリで誤って起動された等）の場合、当該 `loop_id` は 2.6 節と同じ**安全停止**の扱いとする:
  1. その `loop_id` の worker（起動中であれば）を discovery/監視対象から除外し、以後
     spawn/restart の対象にしない。
  2. `journal.jsonl` に `event: "stopped"` を**先に**追記し（durable な記録。core 編 6.4 節の順序に
     従う。Codex レビュー指摘反映。P2）、audit へ `loop_stop`（`stop_reason` 付き）を emit する。
  3. `journal.jsonl` への追記後、`state.json.status = "stopped"`、`stop_reason = "repo_identity_mismatch"`
     として記録する。
  4. macOS 通知を必ず発火する。**repo-identity 自体が不一致のケースでは Issue コメントは投稿しない**
     （どの Issue に紐づくループかを安全に確定できないため）。
  - stderr にも警告を出す（診断用）。
- push 直前の repo-identity 再照合は `loop_driver.py`（2 節）側の責務であり、`loop_scheduler.py`
  は起動時の 1 回のみ行う。
- **stale な事前読み込み state の再検証（3巡目レビュー反映。RH1）**: `verify_repo_identity_at_
  startup` は `state.json` を per-loop coord lock 取得**前**に読む（lock 自体が `state.loop_id` を
  キーにするため、先に一度読まないと lock を取得できない）。読み込みから
  `_safe_stop_repo_identity_mismatch` が実際に coord lock を取得するまでの間隙で、当該 loop の
  purge（tombstone 書込 + `rmtree`。4.3 節 RH3）が完了しきっていた場合、lock.json も消えている
  ため lease 生存チェック単体ではこの race を検知できない（`_is_lease_expired` は lock.json 不在を
  「失効」と判定するため）。この状態のまま古い `state` を書き込むと、削除済みディレクトリに新規
  `stopped` state.json を再生成し、既に書かれた tombstone（purge 済み/terminal を表す）と矛盾する。
  対策として、coord lock 取得後に `state.loop_id` の現在状態を**再読込・再検証**し、以下のいずれか
  に該当する場合は書き込みをスキップして安全停止しない: (a) tombstone が存在する、(b) state.json
  自体が消失している、(c) status が既に terminal（`failed`/`stopped`/`passed`）に遷移済み、
  (d) `state_version` が事前読み込み時点から変化している、(e) repo-identity 不一致が既に解消
  している。再検証後の値を使って初めて journal/state への書き込みを行う。
- **lease 生存中は安全停止を保留する（SN8）**: `_safe_stop_repo_identity_mismatch` は自身で lease
  を保有していないため、lease が生存中に安全停止を書き込むと、生存中の（他ホスト/他プロセスの）
  worker が次の in-flight persist で無条件に上書きしてしまい、安全停止が黙って無効化される。lease
  が生存中の不一致 loop は書き込みをスキップし、`stopped` にも `stopped_loop_ids` にも含めない
  （警告ログのみ）。次回、lease が実際に失効した時点で再評価される想定だった。
- **respawn 経路での再照合（6巡目レビュー指摘反映。J1）**: 起動時 1 回のみの照合という上記の設計
  では、lease 生存中で保留された不一致 loop がその後 lease 失効した場合に、誰も repo-identity を
  再チェックしないまま 3.3 節の 3 経路（`respawn_orphaned_active_loops`・foreign-lease cooldown
  経過後の再起動・異常終了の即時再起動）のいずれかが worker を respawn してしまう欠陥があった。
  各経路は実際に worker を spawn する直前に `_recheck_repo_identity_before_respawn`
  （`_safe_stop_repo_identity_mismatch` を再利用）で repo-identity を再照合するようになった。
  再照合の結果、lease が失効していれば安全停止（`stopped_loop_ids` へ追加）に回し、lease がまだ
  生存中であれば起動時と同様に保留して当該サイクルでは何もしない（次サイクルで再評価）。

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
- **cron エントリの CR/LF fail-closed（2巡目レビュー反映。SN-cron）**: `render_cron_entry` に
  補間される `project_dir`/スクリプトパス/インタプリタパス/`--definition` 値のいずれかに
  literal な CR/LF が含まれる場合は `ValueError` で fail-closed する。`%` は既存の SN7 対策
  （`\%` へのエスケープ）で crontab のファイル解析段階での改行分割を防げるが、CR/LF 自体は
  crontab レベルでエスケープする手段がなく（`shlex.quote` はシェル側の語分割/メタ文字のみを
  防ぐもので、crontab ファイル解析自体の行分割は防げない）、レンダリングを拒否する以外に安全な
  対処がないため。
- **`pgrep -f` の自己一致（既知の限界。#13、受容済みリスク）**: cron 版の生存確認 `pgrep -f
  <pattern>` は、cron 行自身のテキスト（フォールバックの `python3 <script> --project <project>`
  呼び出し部分）にも一致しうるため、liveness チェックとして完全ではない。正規表現ベースの
  liveness には構造的な限界があり、確実な単一起動保証には pidfile/flock ベースの liveness
  チェックへの移行が必要（`loop_scheduler.py` 起動時に自身で pidfile を書き、cron 側はそれを
  見る方式等）。テンプレートレンダリングの修正で閉じられる範囲を超えるため、本レビューでは
  現状のまま受容し、別 Issue でのフォローアップを提案する。**2026-07-15 追記（Issue #216 で
  pidfile/flock 方式へ全面置換。詳細は本節末尾の新規記述を参照）**: 上記の pgrep 自己一致問題
  および `$$`/`$PPID` 除外フィルタ（#219 P2-3）は、`is-alive` サブコマンドの pidfile/flock
  liveness チェックへの移行により解消・撤去された。
- **launchd plist の CR/制御文字 fail-closed（3巡目レビュー反映。RM3）**: 上記 cron の CR/LF
  fail-closed（SN-cron）は launchd 側（`render_launchd_plist`）には未適用だった。`&`/`<`/`>` は
  `xml_escape` で escape 済みだが、それだけでは制御文字を防げない: literal な CR はそれ自体は
  不正な XML ではないが、XML 1.0 のパース時改行正規化（CR・CRLF・単独 CR はすべて LF へ畳み込ま
  れる）により、このファイルを読み戻す任意の XML パーサ（launchd 自身の plist リーダーを含む）が
  レンダリング時とは異なる値へ黙って解決してしまう。TAB(`#x9`)/LF(`#xA`)/CR(`#xD`) 以外の C0 制御
  文字は正規化ではなく単純に不正な XML 1.0 コンテンツであり、どの XML パーサでもパース不能な
  plist を生成する。`render_launchd_plist` に補間される `project_dir`/スクリプトパス/インタプリタ
  パス/`--definition` 値のいずれかにこれらが含まれる場合、cron と同様に `ValueError` で
  fail-closed する（LF 単体は XML 1.0 上合法でパース時も変化しないため、cron 側と異なり拒否
  **しない**）。
- **cron liveness guard への definition id 反映（6巡目レビュー指摘反映。J4）**: 非デフォルトの
  `--definition` 向け cron エントリを生成する際、`pgrep -f` のパターンにこれまで definition id が
  含まれていなかった。同一プロジェクトで別の（あるいはデフォルトの）loop definition 用の
  scheduler が既に起動していると、このガードがそれを「自分の definition の scheduler は既に
  生存している」と誤認し、当該 definition 用の scheduler を一切起動しないまま cron エントリが
  無限にスキップされてしまう。`definition_id` が `DEFAULT_DEFINITION_ID` と異なる場合、
  `re.escape` 済みの `--definition <definition_id>` を `script`/`project` と同様にパターンへ含める
  ことで解消した。
- **launchd label への definition id 反映（6巡目レビュー指摘反映。J6）**: 同一プロジェクトで
  デフォルトの loop と非デフォルトの `--definition` の両方の plist を生成すると、
  `ProgramArguments` は異なるが `Label` は（#H16 のプロジェクトハッシュ suffix のみで）同一になって
  いた。`launchd.plist(5)` は `Label` がジョブを一意に識別すると規定しており、2 つ目の plist を
  ロードすると 1 つ目と衝突し、片方の definition の label キューが永久にスケジュールされない。
  `definition_id` が `DEFAULT_DEFINITION_ID` と異なる場合、`Label` に `.{definition_id}` を追加の
  suffix として含めることで解消した。
- **scheduler 単一起動保証を pidfile/flock 方式へ全面置換（Issue #216）**: cron の生存確認は
  `pgrep -f <pattern>` の正規表現マッチから、`loop_scheduler.py <script> --project <project>
  [--definition <id>] is-alive` サブコマンドへ置き換えた。`loop_scheduler.py` は起動
  （`run_scheduler`）時に、プロジェクト・loop definition ごとに固定された pidfile
  （`.claude/loop/scheduler.pid`、非デフォルト definition は
  `.claude/loop/scheduler.<definition_id>.pid`）へ `flock(LOCK_EX | LOCK_NB)` を試み、取得できな
  ければ即座に終了する。これが実際の単一起動保証であり、`is-alive`／cron の `|| フォールバック`
  はあくまで無駄な起動試行を避ける最適化に過ぎない — 二重起動が発生しても、後から起動した側の
  flock 取得は必ず失敗し、ループ状態には一切触れずに終了する。liveness の正本は「flock を保持
  しているか」のみであり、pid の生存確認は行わない（stale pidfile・pid 再利用問題を構造的に回避
  する）。この方式は cron・launchd・手動起動のいずれから起動されたかによらず一様に適用される。
  pgrep 方式の構造的限界（#13 の自己一致、`$$`/`$PPID` 除外フィルタの祖先チェーン非対応、#219
  P2-3）はすべて解消され、対応する pgrep ベースのコード・テスト・上記の受容済みリスク注記は撤去
  した。pidfile はプロジェクト単位ではなく (project_dir, definition_id) 単位で分離されており、
  J4 の cron liveness パターン方針を踏襲して、同一プロジェクトで複数の非デフォルト definition
  の scheduler を並行起動できる既存の意図的な運用を妨げない。launchd は `KeepAlive: true` による
  既存の自動再起動があるため pgrep 相当のテンプレートガードを元々持たず、そちらの変更は不要
  だった（手動起動と launchd の併存等の二重起動は、上記の scheduler 自身の起動時 flock によって
  防がれる）。
- **pidfile のパス解決を root worktree 基準へ修正（PR #230 Codex P1 反映）**: 上記初版実装の
  `scheduler_pidfile_path` は `Path(project_dir).resolve() / ".claude" / "loop"`（`--project` に
  渡された worktree をそのまま使う単純な resolve）で pidfile を配置していたが、ループ状態
  （`state.json`/`journal.jsonl`/coord lock、いずれも `loop_common.loop_root` 経由で root
  worktree に解決される）は `--project` にどの worktree を渡しても常に root worktree 側
  `.claude/loop/` に集約される。そのため `--project <linked-worktree>` で起動した scheduler と
  `--project <root-worktree>` で起動した scheduler が**別々の pidfile**を持ってしまい、flock が
  競合せず、同じ共有状態に対して 2 つの scheduler が discovery/spawn できてしまう（単一起動保証と
  同時実行 cap の破れ）。`scheduler_pidfile_path` を `loop_common.loop_root`（root-worktree 解決込
  み）ベースに変更し、`run_scheduler`・`is_scheduler_alive`・cron テンプレートの `is-alive` 呼び
  出しの 3 者が worktree に依らず常に同一 pidfile に一致するよう修正した。「git subprocess を挟み
  たくない」という初版の判断より、単一起動保証の正しさを優先している。
  **root worktree 解決失敗時（git 不在・非 git ディレクトリ等）は fail-closed**: `loop_root`/
  `resolve_root_worktree` は解決不能な場合 `RootResolutionError` を送出する（既存の fail-closed
  設計を踏襲）。`run_scheduler` はこれを「他の scheduler が既に起動中」と同じ扱いで捕捉し、
  ループ状態に一切触れず起動を拒否する（保護が確認できない状態で無防備に起動を続けるより安全）。
  `is_scheduler_alive`（`is-alive` CLI・cron の liveness プローブ）は `False`（=生存なし）を返して
  フォールバックの起動試行に委ねる — その起動試行も同じ理由で `run_scheduler` 側が起動を拒否する
  ため、プローブ側で例外を送出してトレースバックをログに残すより、実際の単一起動保証を担う
  `run_scheduler` 側のメッセージに一本化する設計とした。

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
python3 loop_status.py purge [--project <path>] [--force] [--dry-run] [--yes]
python3 loop_status.py untombstone --loop-id <id> [--project <path>]
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
- **実削除時の対話確認**: `--dry-run` を指定しない実削除時は、誤操作防止のため確認を要求する。
  TTY 実行時は `yes` の入力で削除を続行する。`--yes` フラグ指定時は確認をスキップする。
  非対話 stdin・EOF・`yes` 以外の入力の場合は削除せず exit `1` とする。
- purge 完了後は `state.json`/`journal.jsonl`/`artifacts/` に加え、tombstone を残す（3.1 節
  「purge 済み loop の tombstone 除外」参照）。
- **tombstone は削除より先にアトミック公開する（3巡目レビュー反映。RH3）**: 以前は `rmtree` を
  先に実行してから `path.write_text` で tombstone を直書きしていたが、この順序では
  「ディレクトリは既に消えたが tombstone はまだ書かれていない」間隙（プロセス kill 等でこの
  間隙に落ちると両方とも存在しない）や、非アトミックな直書きが途中で失敗した場合の壊れた
  tombstone を生みうる。いずれのケースでも `loop_scheduler.discover_loop_ids` は purge 済みの
  Issue を新規候補として再検出してしまい、purge の目的（同じ Issue の自動再生成防止）を損なう。
  `purge_loop` は coord lock 保持下で、まず `lc._write_text`（一時ファイル + `os.replace` による
  既存のアトミック書き込みヘルパー。`state.json`/`journal.jsonl` と共通）で tombstone を先に
  公開し、それが成功して初めて `rmtree` を実行する。tombstone の書き込み自体が失敗した場合は
  `LoopHarnessError` を送出し、`rmtree` は一切実行しない（削除だけが先行して記録が残らない
  事態を避ける）。
- **orphaned-pending スナップショットの purge は tombstone を書かない（3巡目レビュー反映。
  RH2）**: `loop_scheduler.recover_orphaned_pending_loops` が退避した `.orphaned-N` ディレクトリ
  （SM1）を purge する際、その coord lock はスナップショット自身のディレクトリ名でキーされる
  （元の no-suffix `loop_id` ではない）。以前はこの purge が `state.json` に残る**元の** loop_id
  で tombstone を書いていたため、元 loop_id 自身の coord lock を一度も取得しないまま元 loop_id を
  tombstone 化でき、再開/再 spawn された当該 Issue の現行 run と競合しうる（list 上の矛盾・
  再発見不能の原因になる）。この設計上のキー不一致を解消するため、orphaned-pending スナップ
  ショットの purge では tombstone を一切書かず、ディレクトリを削除するだけにする。元の loop の
  終端記録（tombstone）は、その loop 自身が（自身の coord lock の下で）改めて purge された場合に
  のみ書かれる。
- **tombstone 解除コマンド（3巡目レビュー反映。RM2）**: `untombstone --loop-id <id>` で tombstone
  を明示的に削除し、当該 Issue を discovery 経由で新規ランとして再開できるようにする（従来は
  `.tombstone.json` を手動で削除する以外の運用経路がなかった）。`purge`/`resume`/`attach`/
  スケジューラの安全停止書き込みと同じ固定 coord lock の下で実行するため、削除の瞬間に別プロセスが
  同じ `loop_id` を purge して tombstone を再生成する競合とは競合しない。
- **purge/resume/attach 間の inode 差し替えレース対策（2巡目レビュー反映。SN-flock）**:
  `_purge_if_still_safe` は削除対象の `.claude/loop/<loop_id>/lock.json` 自体への flock（F19）に
  加え、`.claude/loop/<loop_id>.coord.lock`（`loop_common.held_coord_lock`。削除対象**外**の固定
  パスで、purge 後も消えない）を reload〜purge の全区間で保持する。`lock.json` 自体への flock は
  `rmtree` でそのファイルの inode ごと消えるため、purge の途中で `resume`/`attach`
  （`loop_common.reacquire_lease`）が新しい `lock.json`（別 inode）を作って書き込んでも、既に
  失効した inode 上の flock とは競合しない。`resume`/`reacquire_lease` 側もこの同じ固定パスを
  reload〜書き込みの全区間で保持するよう変更済みのため、purge と resume/attach は常にこの
  1 本の固定ロックで直列化される（1.8 節・1.10 節も参照）。`loop_scheduler.py` の
  `_safe_stop_repo_identity_mismatch`（3.4 節の安全停止書き込み）も同じロックを使う。
  **`loop_scheduler.recover_orphaned_pending_loops`（`_retire_if_still_orphaned_pending`。3.3
  節、PR #229 レビュー反映）も同じ固定ロックの下で reload〜rename を行う**: `attach` が
  `pending` を受理するようになったこと（Issue #205、1.10 節）により、この retirement 経路の
  rename と `attach()`（`reacquire_lease`）の lease 再取得が同一 `loop_id` に対して競合しうる
  ようになったため（retirement 側の cheap な事前フィルタは `state.json` を読んだ直後に
  ディレクトリが rename される TOCTOU の余地を残す）、いずれか一方が coord lock を先に取得した
  側が「勝ち」、負けた側はロック内での再読込・再検証（`state.status`・lease 生存性）が
  最新状態を反映して自然に no-op する。

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
  priority_labels: [] # list[str]. discovery ソートで最優先扱いするラベル語彙（先頭ほど高優先）。§3.1 参照。既定 [] は created_at 昇順の純 FIFO

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

maker:
  fallback_agent: general-purpose # allowed_agents 内であること
  allowed_agents: # Edit/Write/Bash または同等の実装経路を持つ positive allowlist
    - ai-dev
    - backend-go-dev
    - backend-python-dev
    - debugger
    - frontend-dev
    - general-purpose
    - prompt-engineer
    - rag-engineer
    - tester
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
| `lp2.priority_labels`                       | list[str]                                                                                             | `[]`                                                         | 同上（§3.1 の優先度ラベルソート語彙）        |
| `pr_review.poll_interval_seconds`           | int（秒）                                                                                             | `120`                                                        | 同上                                         |
| `pr_review.timeout_seconds`                 | int（秒）                                                                                             | `3600`                                                       | 同上                                         |
| `pr_review.reviewer_allowlist`              | list[dict]（`app_slug`/`login`/`type`/`author_association` 等。実スキーマは pr-review 編 2.2 節が正） | **必須キー・既定値なし**（キー欠落・空リストは起動時エラー） | 上書き可（確定値は pr-review 編 2.2 節が正） |
| `retention.purge_after_days`                | int                                                                                                   | `30`                                                         | 同上                                         |
| `notifications.macos_enabled`               | bool                                                                                                  | `true`                                                       | 同上                                         |
| `notifications.issue_comment_enabled`       | bool                                                                                                  | `true`                                                       | 同上                                         |
| `maker.allowed_agents`                      | list[str]（`issue-loop` の auto Maker 用）                                                             | 実装可能な 9 ロール                                          | 上書き可                                     |
| `maker.fallback_agent`                      | str（`allowed_agents` の要素）                                                                        | `general-purpose`                                            | 上書き可                                     |

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
                        // stopped時: push_guard_violation | repo_identity_mismatch | foreign_live_lease |
                        // git_ref_import_failed | git_ref_not_fast_forward | git_ref_cas_rejected
                        // （1.4節・2.6節・3.4節）
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
