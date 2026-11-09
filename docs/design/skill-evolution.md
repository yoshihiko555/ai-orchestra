---
codd:
  node_id: "design:skill-evolution"
  kind: design
  status: draft
  depends_on:
    - id: "req:skill-evolution"
      relation: derives_from
  owner: ai-orchestra
---

# Skill Evolution（スキル改善候補の受け渡し）設計ドキュメント

**作成日**: 2026-07-01
**改訂日**: 2026-11-10（v1 の互換の CLI・設定のキー・gitignore のエントリを外した。§12）。2026-11-08（v2: 改善候補を集めて meta-harness に渡し、昇格後も追う役に絞る）
**ステータス**: draft（v2 の基本設計。実装は未着手）
**対象**: `packages/skill-evolution`
**関連**: `req:skill-evolution`、ADR-20261108-065（v2 の決定。ADR-20260701-028 を置き換える）、ADR-20260728-046（root への集約）、
ADR-20261103-063（fail-logs の教訓候補）、`design:fail-logs`、`design:meta-harness`

> CODD 注記: 本書から ADR へは依存の辺を張らない（ADR-065 が本書を `references` する。双方向に張ると循環になり
> `codd validate` が error を出すため）。

---

## 1. 目的

v1 は、スキルを呼ぶたびに自己申告を求め、学び（lessons）を注入し、オフライン層でスキルを改善する設計だったが、
改善は回らなかった（要件 §1）。改善案の生成・holdout 付きの比較・人の PR による昇格は meta-harness が既に持っている。

v2 の skill-evolution は、次の 2 つだけを担う。

- **前半**: 実運用の失敗を、どのスキルのどの実行で起きたかに結び付け、改善候補として溜め、承認済みのシナリオにして meta-harness に渡す
- **後**: 昇格した改善のマージ後に、同じ欠陥が再び起きていないかを確かめる

改善そのものは meta-harness が行う。skill-evolution はスキルを書き換えず、meta-harness の内部も呼び出さない。

最初の実装（MVP）は、ai-orchestra リポジトリでの観測だけを対象にする（2026-11-08 利用者の決定。要件 FT-19 は後回し）。
流れを最初に一周させるスキルは、実装後に台帳で人が欠陥と確定したスキルから選ぶ（同日の決定）。

---

## 2. 全体の流れ

```text
 スキル実行（毎回）                          ── hook は記録だけ。注入しない（NF-01）
   Skill ツール / /<skill> の直接起動
        │ 実行範囲の始まり・区切りを runs に追記（§4）
        ▼
 fail-logs の失敗・教訓候補、audit ──読むだけ──▶ 取り込みと帰属（§5）
                                                  │ 一意に決まるものだけ自動、ほかは unknown
                                                  ▼
                                            台帳: 観測（§6）
                                                  │【人】直すべき点があると認める
                                                  ▼
                                            候補 ── 受け渡しの対象外（facet スキルでない）
                                                  │【人】下書きを頼む
                                                  ▼
                                            シナリオ下書き（別セッションの LLM。スイートの外）（§7）
                                                  │【人】承認 ①（内容のハッシュを記録）
                                                  ▼
                                            シナリオの PR → main にマージ
                                                  │ スイートの一致を確かめる
                                                  ▼
                                            改善中: meta-harness の baseline・propose・evaluate（§8）
                                                  │ meta-harness が昇格 PR を作る
                                                  ▼
                                            昇格 PR ──【人】マージ ②──▶ 昇格済み
                                                                          │ マージ後の版の実行を数える（§9）
                                                                          │【人】同じ欠陥を確定
                                                                          ▼
                                                                         再開（候補へ戻る）
```

人が判断するのは、帰属の確定（自動で決まらないもの）、観測を候補にすること、シナリオの承認、昇格 PR のマージ、
同じ欠陥の確定の 5 つである。改善の反映に必須の承認は ① と ② の 2 回（NF-03）。シナリオの PR のマージは、① で承認した内容を
git に入れる手順で、別の承認ではない（§7.6）。

---

## 3. 構成要素と責務

### 3.1 パッケージの中身

| 要素                                        | 役割                                                                                                               | v1 との関係                               |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| `hooks/inject-lessons.py`                   | PreToolUse:Skill。Skill ツール経由の実行範囲の始まりを記録する。出力しない                                         | ファイル名を残し、中身を置き換える        |
| `hooks/capture-skill-telemetry.py`          | PostToolUse:Skill。Skill ツールの呼び出しが実際に走ったこと（`run.confirmed`）を記録する                           | 中身を置き換える                          |
| `hooks/capture-subagent-skill.py`           | SubagentStop。サブエージェントの終わりを記録する                                                                   | 中身を置き換える                          |
| `hooks/capture-skill-stop.py`               | Stop。ターンの終わりを記録する                                                                                     | 中身を置き換える（transcript を読まない） |
| `hooks/record-skill-scope.py`（新規）       | UserPromptExpansion・UserPromptSubmit・SubagentStart。直接起動の始まり、ターンとサブエージェントの始まりを記録する | 新規                                      |
| `lib/skill_evolution_common.py`             | 記録の書き込み、root の解決、設定の読み込み                                                                        | v1 の lessons・pending の処理を外す       |
| `lib/`（新規のモジュール）                  | 取り込みと帰属、台帳、下書き、受け渡し、再発の確認                                                                 | 新規                                      |
| `scripts/skill_candidates.py`（新規）       | 読むだけの CLI（一覧・詳細・状態）                                                                                 | 新規                                      |
| `scripts/skill_candidates_admin.py`（新規） | 状態を変える CLI（§10.2）                                                                                          | 新規                                      |
| `scripts/skill_evolution.py`                | v1 のサブコマンドを非推奨として残し、v1 のデータを消す `purge-v1` を足した。非推奨期間の後に丸ごと外した（§12.3）  | 外した                                    |
| `/skill-candidates`（新規のスキル）         | 人が観測と候補を確認し、状態を進める（§10）                                                                        | 新規                                      |

### 3.2 他のパッケージとの境界

| 相手         | skill-evolution がすること                                                                                                                  | しないこと                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| fail-logs    | 失敗の記録（`failures.jsonl`。`design:fail-logs` §3 の記録契約）と、`lessons_cli.py list --json` の出力を読む。伏せ字に `masking.py` を使う | 教訓候補を作る・状態を変える。原データを複製する。教訓候補の内部の写像（`lessons-state.json`）を直接読む |
| audit        | セッションの記録を、帰属の確認の表示に使う                                                                                                  | 書き込む                                                                                                 |
| meta-harness | スイートのハッシュを同じ方法で計算する。CLI の `--json` の出力を読む。シナリオのファイルと、スイートに伴う設定の変更を PR で足す            | Python のモジュールを import する。ストアに書き込む                                                      |

- meta-harness も skill-evolution を import しない。両者の契約は、シナリオのファイルの形（`schemas/scenario.schema.json`）、
  スイートのハッシュの計算方法、CLI の入出力の 3 つだけにする
- fail-logs への依存は、`masking.py` の読み込み（`design:fail-logs` §7.4 の、絶対パスからの読み込みとカナリアの確認）と、
  記録と CLI の出力の形である。manifest の `depends` には足さない（skill-evolution の導入で fail-logs の導入を強いないため）。
  fail-logs が無い・無効、または伏せ字が使えないときは、hook の記録だけを行い、取り込みと下書きは理由を示して止める

---

## 4. 実行範囲の記録

### 4.1 hook と記録する出来事

hook は記録を 1 行追記するだけで、標準出力に何も出さない（NF-01）。例外・不正な入力・書き込みの失敗は、
`safe_hook_execution` で握りつぶし、終了コード 0 で戻る（NF-02）。

| hook の出来事       | ファイル                     | 記録する出来事  | 条件                                                                                      |
| ------------------- | ---------------------------- | --------------- | ----------------------------------------------------------------------------------------- |
| PreToolUse:Skill    | `inject-lessons.py`          | `run.started`   | `trigger: skill_tool`                                                                     |
| PostToolUse:Skill   | `capture-skill-telemetry.py` | `run.confirmed` | 同じ `tool_use_id` の `run.started` がある                                                |
| UserPromptExpansion | `record-skill-scope.py`      | `run.started`   | `expansion_type` が `slash_command` で、スキルとして解決できる（§4.3）。`trigger: direct` |
| UserPromptSubmit    | `record-skill-scope.py`      | `turn.started`  | 常に                                                                                      |
| SubagentStart       | `record-skill-scope.py`      | `agent.started` | 常に                                                                                      |
| SubagentStop        | `capture-subagent-skill.py`  | `agent.stopped` | 常に                                                                                      |
| Stop                | `capture-skill-stop.py`      | `turn.stopped`  | 常に                                                                                      |

- 利用者が `/<skill>` と打った直接起動は、PreToolUse:Skill を通らない（Claude Code の hooks の公式の説明。
  v1 の設計書 §3.8 の「slash 起動も Skill ツール呼び出しに展開される」は誤りだった）。直接起動は UserPromptExpansion で拾う
- UserPromptExpansion は、利用者が打ったコマンドが展開されたときに発火し、`expansion_type`・`command_name`・`command_args`・
  `command_source`・`prompt` を受け取る。tool_use_id は無い。展開を止めない限り実行されるので、直接起動は始まりの時点で確定とする
- Skill ツールの呼び出しは、権限で拒否されると PreToolUse の後に走らない。`run.confirmed` の無い `run.started` は、該当する機会（§9.1）に数えない
- `.claude/commands/` の旧形式のコマンドは記録しない（スキルとして解決できないため）
- `orchex sync` は manifest の `hooks` のキーをそのまま settings に書く（`scripts/lib/sync_engine.py`。出来事の名前の許可リストは無い）。
  UserPromptExpansion・UserPromptSubmit・SubagentStart を manifest に足せば導入先にも登録される。settings に知らない出来事の名前があっても、
  Claude Code はその名前を無視してほかの hook を動かす（§14.1 の 9）

### 4.2 記録の形

保存先は root に集約した `.claude/logs/skill-evolution/runs-YYYYMM.jsonl`（月ごとのファイル。§6.1）。

```json
{ "v": 2, "ts": "2026-11-08T10:00:00+09:00", "type": "run.started", "run_id": "R-1a2b3c4d5e6f",
  "sid": "...", "prompt_id": "...", "skill": "review", "trigger": "skill_tool",
  "tool_use_id": "toolu_...", "agent_id": null, "agent_type": null, "model": null,
  "skill_hash": "sha256:...", "project": "ai-orchestra",
  "src": { "tool_use_id": "hook_input", "agent_id": "absent", "model": "absent", "skill_hash": "project_skill_dir" } }
{ "v": 2, "ts": "...", "type": "run.confirmed", "sid": "...", "tool_use_id": "toolu_..." }
{ "v": 2, "ts": "...", "type": "turn.started", "sid": "...", "prompt_id": "..." }
{ "v": 2, "ts": "...", "type": "turn.stopped", "sid": "..." }
{ "v": 2, "ts": "...", "type": "agent.started", "sid": "...", "agent_id": "...", "agent_type": "Explore" }
{ "v": 2, "ts": "...", "type": "agent.stopped", "sid": "...", "agent_id": "..." }
```

| 項目          | 決め方                                                                                                                                     |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `run_id`      | `R-` + uuid4 の先頭 12 桁                                                                                                                  |
| `sid`         | hook の入力の `session_id`。サブエージェントの中でも親のセッションの ID なので、fail-logs の `sid` と audit のセッションとの結合の鍵になる |
| `prompt_id`   | hook の入力の `prompt_id`。直接起動の `run.started` と、同じ入力の `turn.started` を見分けるのに使う（§4.4）                               |
| `tool_use_id` | PreToolUse:Skill の入力にあれば入れる。直接起動では `null`（`src: "absent"`）                                                              |
| `agent_id`    | hook の入力にあれば入れる（サブエージェントの中の実行）。メインのスレッドでは `null`                                                       |
| `model`       | hook の入力に無い（SessionStart にしか無い）ので、常に `null`、`src: "absent"`。transcript から推定しない                                  |
| `skill`       | Skill ツールは `tool_input` のスキル名、直接起動は `command_name`                                                                          |
| `skill_hash`  | §4.3                                                                                                                                       |
| `project`     | root worktree のディレクトリ名。導入先と ai-orchestra を見分けるために持つ                                                                 |
| `src`         | 各値をどこから得たか（`hook_input` / `absent` / `project_skill_dir` / `user_skill_dir` / `unresolved`）（FT-02）                           |

- プロンプトの本文・スキルの引数（`command_args`・`tool_input.args`）は記録しない
- 文字列の値は書き込む前に形を確かめる。`skill`・`agent_type` は `^[A-Za-z0-9:_.-]{1,64}$`、`project` は `^[A-Za-z0-9._-]{1,128}$`、
  ID は各形式に合うものだけ。合わない出来事は記録しない（改行・制御文字・ANSI の混入を、表示と照合に流さない）
- 時刻はタイムゾーン付きの ISO 8601 で書き、比較は UTC に直して行う（fail-logs の失敗の `ts` は UTC）

### 4.3 スキルの版（ハッシュ）

- スキルのディレクトリを、`$CLAUDE_PROJECT_DIR/.claude/skills/<name>/`、`~/.claude/skills/<name>/` の順に探す
- 見つかったら、ディレクトリの中の通常ファイルの（相対パス, 中身）の組を相対パスの順に並べた sha256 を `skill_hash` にする。
  SKILL.md だけでなく `references/` などの変更も版の違いとして捉えるため
- `.` で始まる名前のファイルとディレクトリ（`.DS_Store` など）と `__pycache__` は含めない。マージのコミットから計算するとき（§8.3）も同じ規則にする
- スキルのディレクトリ自体が symlink のとき（`~/.claude/skills` を別の場所に置く環境）は、その実体のディレクトリで計算する。
  ディレクトリの中の symlink はたどらず、含めない
- `runs.hash_max_files`（既定 200）か `runs.hash_max_bytes`（既定 1 MiB）を超えるときは `null`（`src: "unresolved"`）
- plugin のスキル（名前に `:` を含む）と、見つからないスキルは `null`（`src: "unresolved"`）。直接起動で見つからないものは記録しない（§4.1）
- 版は、そのスキルが実行されたディレクトリの中身で決まる。古いブランチの worktree で動かした実行は、マージ後の版と一致しないので、
  該当する機会（§9.1）に数えない。これを意図した定義とする

### 4.4 実行範囲の区切り

実行範囲は、`run.started` の時刻（含む）から最初の区切りの時刻（含まない）までとする。

| 実行の場所                                | 区切り                                                                                  |
| ----------------------------------------- | --------------------------------------------------------------------------------------- |
| メインのスレッド（`agent_id` が `null`）  | 同じ `sid` の次の `turn.stopped`、または `prompt_id` が違う次の `turn.started` の早い方 |
| サブエージェントの中（`agent_id` がある） | 同じ `agent_id` の `agent.stopped`                                                      |

- 利用者が中断すると Stop は発火しない。次の利用者の入力（`turn.started`）で区切るので、中断の後の作業は実行範囲に入らない
- 直接起動では、UserPromptSubmit の直後に同じ `prompt_id` の UserPromptExpansion が来る（2026-11-08 に実機で確認）。`run.started` より後の、
  `prompt_id` が同じ `turn.started` では区切らない
- サブエージェントはバックグラウンドで動くことがある。メインのスレッドの Agent の呼び出しはすぐ戻り、メインのターンが終わった（Stop）後も
  サブエージェントは動き続ける。完了の通知でメインのスレッドに新しい入力（UserPromptSubmit）が来る（同日に実機で確認）。
  サブエージェントの中の実行範囲は `agent.stopped` で区切るので、メインのスレッドの区切りでは閉じない
- メインのスレッドでは、スキルの手順が終わった後も、同じターンの作業は実行範囲に入る（hook からスキルの手順の終わりは分からない）。
  同じターンに別のスキルが始まれば、§5.2 の条件 3 で自動の帰属から外れる。誤帰属ではなく取りこぼしの側に倒れる
- `/loop-issue` のように複数のターンにまたがる実行は、最初のターンの終わりで区切る。それより後の証拠は自動では帰属させず、
  人が帰属を決める（§5.3）
- 区切りが記録されていない実行範囲（hook の書き込みの失敗など）は「区切り不明」とし、自動の帰属に使わない

実行範囲は記録から毎回組み立てる。組み立てた結果は保存しない（記録が正本）。

---

## 5. 証拠の取り込みと帰属

### 5.1 取り込む証拠

取り込みは `/skill-candidates` を開いたときに、`skill_candidates_admin.py ingest` で行う（hook では行わない）。
前回の続きから読むため、読んだ位置を `ingest-state.json` に持つ。

| 証拠                      | 読む場所                                | 証拠の鍵           | 帰属の手がかり                                  |
| ------------------------- | --------------------------------------- | ------------------ | ----------------------------------------------- |
| 失敗（fail-logs）         | `.claude/logs/fail-logs/failures.jsonl` | `("failure", eid)` | `sid`・`ts`                                     |
| 教訓候補（fail-logs）     | `lessons_cli.py list --json` の出力     | `("lesson", lid)`  | 根拠のセッション・PR、根拠の失敗の `eid` の一覧 |
| セッションの記録（audit） | audit のセッションの記録                | （証拠にはしない） | 帰属を人が確かめるときの表示に使う              |

- fail-logs の教訓候補には、`/review-respond` が採用したレビューの指摘から作られたものを含む。fail-logs に入らない指摘は扱わない（要件 §5.2）
- 教訓候補の根拠の失敗の一覧は、セッション全体の失敗である（`distill_worker.py` がセッションの失敗をまとめて付ける）。教訓候補と実行範囲の因果は示さないので、教訓候補は自動で帰属させない
- 教訓候補の根拠（失敗の `eid` の一覧）は、最初に取り込んだ時点のものを使う。後から fail-logs 側で根拠が増えても観測は更新しない（同じ証拠の鍵は 1 回だけ受け付ける）
- 1 件の記録が壊れている（必須の項目が無い、形が合わない、1 行が長すぎる）ときは、その記録だけを飛ばして件数と理由を示し、読んだ位置は進める。1 件で取り込み全体を止めない
- 原データは複製しない。台帳には証拠の鍵と、伏せ字を通した短い抜粋（§6.3）だけを持つ

### 5.2 自動の帰属（FT-05）

失敗 1 件について、次をすべて満たすときだけ、その実行範囲に自動で帰属させる。満たさなければ帰属 `unknown` にする（NF-06）。

1. 同じ `sid` で、失敗の時刻を含む実行範囲がちょうど 1 つあり、それが実際に走った実行範囲である（直接起動か、`run.confirmed` がある Skill ツールの呼び出し）。
   `run.confirmed` の無い Skill ツールの実行範囲（権限で拒否されたもの）が失敗の時刻を含むときも `unknown` にする
2. その実行範囲の区切りが記録されている（§4.4）
3. その実行範囲の中に、別の実行範囲の始まりが無い（入れ子でない。A の中で B が動き、A に戻る場合もここで除く）
4. 失敗の時刻に、同じ `sid` で動いているサブエージェント（`agent.started` から `agent.stopped` まで）が 1 つも無い

- fail-logs の失敗の記録はエージェント ID を持たず、hook の入力にも親のエージェントを示す値は無い。サブエージェントが動いている間の失敗は、
  メインのスレッドから来たかサブエージェントから来たかが分からない（バックグラウンドのサブエージェントでも同じ）ので、自動では帰属させない（4）。
  そのため、サブエージェントの中で動いたスキルの失敗は、常に人が帰属を決める
- 失敗の記録の `tool_use_id`（新しい経路の失敗だけが持つ）は、失敗したツール（Bash など）の呼び出しの ID で、Skill の呼び出しの ID とは別物である。
  帰属の判定には使わない。テスト・lint の出力の検知で作られた失敗も、同じ条件で帰属させる
- セッションの再開は、再開後の最初の入力（`turn.started`）で前の実行範囲が区切られるので、再開の前の実行範囲が後の失敗を吸い込まない
- 自動で帰属させた観測には `attributed_by: auto` と、満たした条件を残す

### 5.3 人による帰属（FT-04、FT-06）

- 帰属 `unknown` の証拠ごとに、同じ `sid` で証拠の時刻より前に始まった実行範囲を新しい順に示す（区切りの後の証拠も選べる）。
  audit のセッションの記録から、その前後のスキルの呼び出しとサブエージェントの起動を並べて見せる
- 人は、実行範囲を 1 つ選ぶか、実行範囲を特定できないとき（PR のレビュー指摘など）はスキルだけを指定して、帰属を確定する（`attributed_by: human`）
- 帰属を確定した時点から、観測として再発回数に数える。`unknown` のままの証拠は数えない

---

## 6. 台帳

### 6.1 保存先とファイル

保存先は `.claude/logs/skill-evolution/`（設定の `storage.logs_dir`）で、ADR-20260728-046 の決定 3 に従って root worktree に集約する
（core の `resolve_log_root`）。`.claude/logs/` は配布する `.gitignore` に入っているので git 管理外になる。worktree を消しても残る（NF-08）。

| ファイル                | 内容                                       | 書き込み方                                          |
| ----------------------- | ------------------------------------------ | --------------------------------------------------- |
| `runs-YYYYMM.jsonl`     | 実行範囲の出来事（§4.2）                   | `O_APPEND` + flock の純追記                         |
| `candidates.jsonl`      | 観測と候補の出来事の履歴（§6.2）           | `candidates.lock` の中で `O_APPEND`                 |
| `candidates-state.json` | 観測と候補の現在の状態の写像               | `candidates.lock` の中で一時ファイル + `os.replace` |
| `candidates.lock`       | 台帳と写像の書き込みを直列にするロック     | flock                                               |
| `ingest-state.json`     | 取り込みで読んだ位置                       | `candidates.lock` の中で一時ファイル + `os.replace` |
| `drafts/<cid>/<n>/`     | シナリオ下書き（§7）。`<n>` は下書きの回数 | 一時ディレクトリに書いてから `os.replace`           |

- ファイルとディレクトリの権限、`O_NOFOLLOW`、`resolve_path_within` によるパスの検証は fail-logs の §6 と同じにする
- 台帳・写像・追いつき・壊れた行の扱いは fail-logs の §8.2・§8.3 と同じ作りにする（同じ `eid` は 1 回だけ適用する、
  表にない遷移は書き込み時に拒否する、写像が壊れたら台帳から作り直す）
- ロックは最大 `ledger.lock_wait_seconds`（既定 5）秒まで待ち、取れなければ書き込みを諦めて理由を示す。hook の追記の flock も同じ上限で諦め、何も出さずに戻る
- spool（root に書けないときの退避）は作らない。hook はサンドボックスの外で動くので root に書ける。状態を変える CLI は、
  `/skill-candidates` が利用者の承認を得てサンドボックスの外で実行する（fail-logs の `lessons_admin.py` と同じ扱い）
- `runs-YYYYMM.jsonl` は全セッションのターンの出来事を持つので、1 か月に数千行（1 行 200 バイト前後）になる。取り込みは必要な月のファイルだけを読む。
  古い月のファイルは自動では消さず、人が `skill_candidates_admin.py prune-runs --before YYYYMM` で消す（昇格済みの候補のマージの月以降と、`promotion_pr` の候補の、`handoff.promotion_linked` の月と PR の作成の月の早いほう以降は消せない。マージの確認より前に機会の記録を失わないため）

### 6.2 出来事

```json
{ "v": 1, "ts": "...", "eid": "12hex", "type": "observation.created", "oid": "O-1a2b3c4d", "actor": "ingest | human", "data": { ... } }
```

| `type`                        | 内容                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------ |
| `observation.created`         | 証拠の鍵、`sid`、`ts`、抜粋、帰属（`run_id` / `skill` / `unknown`）、`attributed_by`、満たした条件     |
| `observation.attributed`      | 人が確定した帰属（`run_id` か `skill`）                                                                |
| `observation.dismissed`       | 人が「直す点ではない」とした                                                                           |
| `candidate.created`           | `cid`、`skill`、仮説（直すべき点の 1 行と説明）、根拠の観測の一覧、受け渡しの可否（§6.5）              |
| `candidate.observation_added` | 観測を足した（同じ欠陥として人が確定したもの）                                                         |
| `candidate.merged`            | 重複した候補をまとめた。`into`                                                                         |
| `candidate.state_changed`     | `from`・`to`・`note`、遷移に必要な記録への参照（§6.4）                                                 |
| `scenario.drafted`            | 下書きの番号、ファイルごとの `scenario_hash`、下書きのセッションの情報（モデル・費用・入力のハッシュ） |
| `scenario.approved`           | 承認した下書きの番号、ファイルごとの `scenario_hash`、確認項目への回答、人が書いた行の有無（§7.5）     |
| `scenario.suite_verified`     | main の checkout で確かめた、承認したファイルの `scenario_hash`、スイート全体の `suite_hash`、コミット |
| `scenario.retired`            | 承認したシナリオをスイートから外した PR（§7.7）                                                        |
| `handoff.evaluation_linked`   | meta-harness の候補 ID、評価の ID、run の ID、`own_suite_hash`、`evaluator_hash`（§8）                 |
| `handoff.promotion_linked`    | 昇格 PR の URL、meta-harness の候補 ID、PR の作成時刻                                                  |
| `handoff.closed`              | 昇格 PR がマージされずに閉じられたことを `gh` で確かめた。PR の URL                                    |
| `handoff.merged`              | マージのコミット、マージ後のスキルの版のハッシュ、マージの時刻                                         |
| `recurrence.confirmed`        | 同じ欠陥と人が確定した観測（§9.2）                                                                     |

- ID は `O-` / `C-` + uuid4 の先頭 8 桁
- 台帳の記録（`actor` を含む）を信頼できる根拠は、root の台帳のディレクトリに書けるのが利用者だけであること。`handoff.*` の記録は、`gh` と `git` の確認を通った CLI の経路だけが作る
- `candidates.jsonl` は追記だけで、既存の行を書き換えない
- `note`・仮説・確認項目への回答など人が書く文字列は、1 行 200 字（仮説の説明は 1,000 字）までとし、§6.3 の文字種の検査を通す。
  PR の URL は `https://github.com/<このリポジトリ>/pull/<番号>` の形だけを受け付ける

### 6.3 抜粋と数え方

- 抜粋は、fail-logs の `masking.py` を通してから、`ledger.excerpt_max_chars`（既定 300）字で切る。伏せ字が使えないときは抜粋を空にし、
  その観測を「抜粋なし（伏せ字が使えない）」と示す（NF-05）
- 文字種の検査（Cc・Cf などの拒否）は fail-logs の §8.3 と同じにする
- 出来事の件数 = 根拠の観測の証拠を、失敗の `eid` の集合にほどいた数。教訓候補が根拠の失敗を持つときはその `eid` に、
  持たないときは教訓候補の `lid` 1 件にする。同じ失敗が失敗の記録と教訓候補の両方から来ても 1 件と数える（FT-07）
- 同じ証拠の鍵の `observation.created` は 1 回だけ受け付ける（取り込みを 2 回しても件数は変わらない）

### 6.4 候補の状態と遷移（FT-08）

状態の名前: `candidate`（候補）、`drafting`（シナリオ下書き）、`scenario_approved`（シナリオ承認）、`improving`（改善中）、
`promotion_pr`（昇格 PR）、`promoted`（昇格済み）、`rejected`（却下）、`archived`。

| 遷移                                       | 区分   | 遷移に必要なもの                                                     |
| ------------------------------------------ | ------ | -------------------------------------------------------------------- |
| （観測）→ `candidate`                      | 主経路 | 人の操作。観測 1 件以上                                              |
| `candidate` → `drafting`                   | 主経路 | 人の操作。受け渡しできる候補であること（§6.5）                       |
| `drafting` → `scenario_approved`           | 主経路 | `scenario.approved`（検査を通った下書き。§7.4・§7.5）                |
| `scenario_approved` → `drafting`           | 戻り   | 差し戻し（人の操作、理由）。または承認したファイルが変わった（§7.6） |
| `scenario_approved` → `improving`          | 主経路 | `scenario.suite_verified`（§7.6）                                    |
| `improving` → `drafting`                   | 戻り   | 承認したファイルが main で変わった（`verify-suite` の不一致。§7.6）  |
| `improving` → `promotion_pr`               | 主経路 | `handoff.evaluation_linked`（昇格する候補の holdout の評価を含む）と `handoff.promotion_linked`（§8） |
| `improving` → `candidate`                  | 戻り   | 改善案が採用基準を満たさなかった（人の操作、理由）                   |
| `promotion_pr` → `promoted`                | 主経路 | `handoff.merged`（マージの確認。§8.3）                               |
| `promotion_pr` → `candidate`               | 戻り   | `handoff.closed`（昇格 PR がマージされずに閉じられた。`gh` で確かめる） |
| `promoted` → `candidate`                   | 分岐   | 再開。`recurrence.confirmed`（§9.2）                                 |
| `rejected` と `archived` 以外 → `rejected` | 分岐   | 人の操作、理由                                                       |
| `rejected` → `candidate`                   | 分岐   | 却下の取り消し（人の操作）                                           |
| `candidate`・`rejected` → `archived`       | 分岐   | 人の操作（FT-16）                                                    |
| `archived` → `candidate`                   | 分岐   | 人の操作                                                             |

- 表にない遷移は拒否する
- 人の承認の記録（`scenario.approved`）が無いと `scenario_approved` に、マージの確認（`handoff.merged`）が無いと `promoted` に進めない（NF-03）

### 6.5 受け渡しの可否（FT-18）

- 受け渡しできるのは、ai-orchestra の checkout に `facets/compositions/skills/<slug>.yaml` があるスキルだけ（meta-harness の
  `resolve_skill_target` と同じ規則）。`<slug>` は `^[a-z0-9-]+$` に合うものだけを扱う
- それ以外（導入先独自・個人・plugin のスキル）の候補は「受け渡しの対象外」と示し、`drafting` に進めない。直すのは人が手で行う
- 導入先には `facets/` が無いので、導入先の候補はすべて「受け渡しの対象外（FT-19 未対応）」になる

---

## 7. シナリオの下書きと承認

### 7.1 下書きのセッション（FT-10）

`skill_candidates_admin.py draft C-…` が、proposer とは別のプロセスで LLM を呼ぶ。呼び出し方は fail-logs の教訓化
（ADR-20261103-063、`design:fail-logs` §7.5）と同じにする。

```text
claude -p --restricted --tools "" --strict-mcp-config --no-session-persistence \
  --settings '{"disableAllHooks": true}' --model <draft.model> --max-budget-usd <draft.max_budget_usd>
```

- 作業ディレクトリは空の一時ディレクトリ。道具・hook・MCP を持たない。環境変数は、認証と実行に要るものだけを渡す（fail-logs の教訓化と同じ）
- 入力は、候補の仮説、根拠の観測の抜粋（伏せ字済み）、対象のスキルの facet の composition と生成された SKILL.md、
  meta-harness のシナリオの形（`schemas/scenario.schema.json`）、使ってよい検査のスクリプトの一覧（§7.4）、同じスキルの既存の学習用シナリオ 1 本（形の見本）
- 抜粋は信頼できないデータとして扱う。実行ごとの乱数を含む区切りで囲み、`@` を全角に置き換える（`design:fail-logs` §7.4 の直列化と同じ）。
  抜粋の中の指示に見える文で下書きが歪められることは防げないので、防ぐのは §7.3〜§7.5 の検査と人の承認である
- スキルの SKILL.md・composition・既存のシナリオは、ai-orchestra リポジトリの git にあるものだけを入力にするので、信頼できる入力として扱う。
  導入先の候補を受け付ける（FT-19）ときは、この前提を見直す
- 出力は、シナリオの YAML の文字列の配列を持つ JSON にし、JSON として読めない出力は捨てる
- 指示: 実データの写しではなく再現用に作ったシナリオにする。実在のパス・ID・人名・URL・リポジトリ名を書かない。学習用と holdout を
  各 1 本以上書き、holdout は学習用と別の入力で同じ欠陥を確かめる。実行される行は §7.4 の形だけで書く
- LLM の呼び出しのあいだは台帳のロックを持たない。呼ぶ前に回数を確保し、結果を保存する前にロックを取り直して、候補の状態と回数が変わっていないことを確かめる
- 下書きは `drafts/<cid>/<n>/` に置く。スイート（`packages/meta-harness/scenarios/`）には書かない。meta-harness の proposer の
  読み取りは、`$HOME`・root・全 worktree を拒み、filtered view と追加の許可（`proposer.isolation.allow_read_extra`）だけを許す
  （`isolation.py` の `build_srt_settings`）。台帳と下書きは root の下にあるので proposer から読めない。`allow_read_extra` に台帳を足さないことを、§14.3 の回帰テストで守る

### 7.2 上限（NF-09）

| 設定                               | 既定     | 意味                                     |
| ---------------------------------- | -------- | ---------------------------------------- |
| `draft.model`                      | `sonnet` | 下書きに使うモデル                       |
| `draft.max_budget_usd`             | 1.0      | 1 回の下書きの費用の上限                 |
| `draft.timeout_seconds`            | 300      | 1 回の下書きの時間の上限                 |
| `draft.max_files`                  | 4        | 1 回の下書きのシナリオの本数の上限       |
| `draft.max_file_bytes`             | 16384    | シナリオ 1 本の大きさの上限              |
| `draft.max_attempts_per_candidate` | 3        | 1 つの候補で下書きを作り直せる回数の上限 |

上限を超える出力は捨て、下書きを作らない。

### 7.3 下書きの形の検査

LLM の出力は信頼できないものとして扱う。次のすべてを通ったものだけを、人の承認に出す。

- YAML として読め（`yaml.safe_load`）、`schemas/scenario.schema.json` に合う（スキーマのファイルを読んで検査する。meta-harness のコードは import しない）。
  読む前にアンカーとエイリアスを拒み、ノードの数と入れ子の深さに上限を設ける（エイリアスの展開で検査が止まらないようにする）。
  同じマッピングに重複したキーがあれば拒む（`safe_load` は後のキーで前の値を捨てるので、捨てられた値が公開の検査を通らずに承認されるのを防ぐ）
- `target` が `skill:<slug>`、`id` が `^[a-z0-9-]+$` で `sevo-<cid の 8 桁>-` で始まり、既存のシナリオの `id` と重ならない
- 学習用（`holdout` が無いか false）と holdout（`holdout: true`）が各 1 本以上
- 学習用のシナリオの `graded` の有無が、同じスイートの既存の学習用シナリオ（先に導入した `sevo-` のものを含む）とそろっている
  （meta-harness の `validate_target_suite` が、学習用のすべてが `graded` を持つか、すべてが持たないかを求める）。
  許可するツール・資源の上限・`cp` の組は、人が書いたシナリオ（`sevo-` でないもの）だけから決める。meta-harness は `id` の重複を検査しないので、重複の検査はここで行う
- 実行される行が §7.4 の形に合う
- 公開してはいけない文字列が無い。次を見つけたら不合格にする
  - 伏せ字で変わる文字列（機密情報らしきもの）
  - 絶対パス（`/` で始まるパス。`~` で始まるパス）、`..` を含むパス
  - メールアドレス、URL、IP アドレス、ホスト名、`github.com/…` などのリポジトリの参照、`owner/repo#N`、先頭の `/` の無いホームのパス（`Users/<名前>/`）
  - 照合は NFKC で正規化し、大文字小文字をそろえてから行う（全角の URL などで避けられないようにする）
  - 根拠の観測の抜粋と、正規化した 12 字以上が一致する部分（実データの写し）
- 文字種の検査（§6.3）

### 7.4 実行される行の形（2026-11-08 利用者の決定）

シナリオは評価のときに実行される行を持つ。LLM の下書きには、次の決まった形だけを許す。形に合わない行が 1 つでもあれば不合格にする。

| 項目                           | 下書きに許す形                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 検査（`critical` の `oracle`） | `artifact_exists`・`json_schema`・`rubric_judge`。`command_exit` は、`python3 packages/meta-harness/scenarios/fixtures/<既存のスクリプト>.py` に、`^[A-Za-z0-9._=:/-]+$` の引数（`/` で始まらず `..` を含まない）を並べた形だけ。使えるスクリプトとフラグは、`scenarios/skill/` の既存のシナリオでそのスクリプトに渡しているものだけ（2026-11-08 時点では `assert-task-state-outcome.py`・`assert-issue-fix-decision.py`・`assert-cli-skill-route.py`・`merge-local-cli-tools-override.py`）。`assert-function-behavior.py`（引数のモジュールを実行する）と、`merge-local-cli-tools-override.py` の `--project-root`（書き込み先を変える）は使わせない |
| 準備（`setup`）                | `mkdir -p <作業パス>`、`touch <作業パス>`、`printf '<文字列>' > <作業パス>`、`cp packages/meta-harness/scenarios/fixtures/<既存のファイル> <作業パス>`。`<作業パス>` は `.meta-harness/` の下で、その後の要素が `.` で始まらないもの。`cp` だけは、既存のシナリオにある（fixtures のファイル, コピー先）の組（例: `fake-gh-issue-view.py` → `bin/gh`）も許す。`printf` の文字列は `[A-Za-z0-9 ._,:#/-]` の文字だけ |
| `permission_mode`              | `acceptEdits` だけ（`bypassPermissions` は下書きに書かせない）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `allowed_tools`                | 省略しない。同じスイートの既存の学習用シナリオの値の部分集合。既存のシナリオが無ければ Read・Write・Edit・Glob・Grep の部分集合 |
| 予算・回数・時間（`budget`・`repeat`・`timeout_ms`・`command_timeout_ms` など） | 書かないか、同じスイートの既存のシナリオの最大値以下。既存のシナリオが無ければ書かない |
| 評価に渡る文（`prompt`・`rubric`・検査の `text`） | 長さの上限（`prompt` 4,000 字、ほかは 2,000 字）。実行される行と並べて承認のときに見せる |

- パイプ・リダイレクト（`printf` の `>` を除く）・`;`・`&&`・コマンド置換・環境変数の展開は、どの行にも許さない
- パスは `^[A-Za-z0-9._/-]+$` で、`/`・`-` で始まらず、`..` を含まないものだけ（§7.3 の公開の検査とは別に、この形の検査でも拒む）。検査の `path`・`schema` も同じ
- 「既存のシナリオ」は人が書いたもの（`id` が `sevo-` で始まらないもの）だけを指す。承認したシナリオを、次の下書きの許可の根拠にしない
- `.claude/`・`.git`・`.mcp.json`・`CLAUDE.md`・`AGENTS.md`・`packages/` への書き込みや、fixtures の上書きは、すべて人が書く行になる
- 抜粋との一致の検査（§7.3）は言い換えで避けられるので、公開してよいかの最後の判断は人の承認（§7.5）で行う
- これ以外の行（`git` を使う検査、`bypassPermissions` が要るスキルなど）が要るときは、人が下書きを直して書く。人が書いた行は、
  承認のときに「人が書いた」と確認項目で明示し、`scenario.approved` に残す。人が直した下書きも §7.3 の形以外の検査（公開してはいけない文字列など）は通す
- 評価は、meta-harness の `evaluate.isolation.execution_backend` が `docker` のときだけ進める（§7.6 の `verify-suite` で確かめる）

### 7.5 承認（承認 ①、FT-11）

`/skill-candidates` が下書きを見せ、人が承認する。

- 下書きは全文を、制御文字をエスケープした生の形で見せる。LLM の要約は見せない。実行される行は一覧にして別に見せる
- §7.3 の検査が見つけたもの（人が直した下書きで残ったもの）は強調して見せる
- 確認項目
  - 欠陥を再現しているか。holdout は学習用と別の入力か
  - 公開してよい内容か（このリポジトリは公開されている。実データの写し・個人の情報・社内の名前が無いか）
  - 実行される行に、人が書いた行があるか。あるなら、その行を確かめたか

承認には、表示したときの各ファイルの sha256 を添える。承認の時点のファイルがそれと違えば承認しない（表示から承認までのあいだに書き換えられた内容を承認しない）。
「人が書いた」とする行は、LLM が出した元の下書き（下書きのディレクトリに残す写し）と違う行だけを受け付ける。
承認すると `scenario.approved` に、ファイルごとの `scenario_hash`（ファイルの中身の sha256。meta-harness の `compute_scenario_hash` と同じ）を記録する。
承認の後に下書きが変わったら、承認は使う時点（`install`・`verify-suite`）で拒む。状態を `drafting` に戻すのは `verify-suite` の不一致のときだけ。

### 7.6 スイートへの導入と一致の確認

meta-harness は昇格 PR を origin/main から作り、昇格の時点で、実行している checkout のスイートのハッシュが評価のときと同じかを確かめる
（`promoter._has_current_hash_pair`）。そのため、承認したシナリオは評価の前に main に入れる。

1. `skill_candidates_admin.py install C-… --dest <checkout>` が、承認したファイルを `<checkout>/packages/meta-harness/scenarios/skill/<slug>/`
   にコピーする。skill-evolution は commit・push をしない
   - `<checkout>` は、このリポジトリの `git worktree list` に出る worktree のどれかで、既定のブランチでなく、origin/main を祖先に持つブランチであること
   - 書き込み先の途中のディレクトリを `lstat` で確かめ、symlink があれば止める。ファイルは `O_NOFOLLOW | O_EXCL` で作り、既存のファイルを上書きしない
   - コピーした後に、ファイルの `scenario_hash` が承認と同じかを確かめる
2. 新しいスキルのスイートを作るとき、または既存のスイートにシナリオを足すときは、meta-harness の回帰の予算（`regression.max_budget_usd` と
   `max_affected_suites`）を見直す。meta-harness は、スキルのスイートを足すたびに予算を上げることを求めている（`meta_harness_common.py` の
   `DEFAULTS["regression"]` の注記）。予算の変更は同じ PR に入れ、`.claude/orchestra.json` の `file_hashes` も更新する。予算の値は人が決める。
   `evaluator_hash` には `regression` の設定全体と `scenarios/fixtures/` が入る（`compute_configured_evaluator_hash`）ので、予算を変えると、
   ほかのターゲットを含む評価済みの候補もすべて昇格できなくなる（評価のやり直しが要る）。進行中の meta-harness の昇格が無いときに PR をマージする
3. 人が PR を作り（`/pr-create`）、マージする。PR は ① で承認した内容を git に入れる手順で、PR の本文に候補 ID と承認したハッシュを書く
4. `skill_candidates_admin.py verify-suite C-…` を main の checkout で実行し、次を確かめて `scenario.suite_verified` を記録する
   - シナリオ置き場に未コミットの変更が無く、既定のブランチの checkout であること。ファイルの一覧と中身は作業ツリーではなく HEAD のコミットから読む
   - 承認したファイルがすべてあり、`scenario_hash` が承認と同じ。置き場の `sevo-` で始まるファイルは、どれも台帳のいずれかの候補で承認したものと一致する
   - スイート全体の `suite_hash` を、meta-harness と同じ方法（シナリオ置き場の全 `*.yaml` の `scenario_hash` をファイル名の順に連結した sha256）で計算する
   - そのコミットが origin/main から辿れる（ローカルの追跡の参照で判定するので、先に `git fetch origin main` する）
   - meta-harness の設定（`.local.yaml` の上書きを含む）の `evaluate.isolation.execution_backend` が `docker`（meta-harness がシナリオの実行の境界に使うキー）

スイートのハッシュは置き場の全ファイルから作られるので、手で書いた既存のシナリオが変わっても変わる。承認したファイルが同じままなら、
`verify-suite` をやり直して新しい `suite_hash` を記録してよい。承認したファイルが変わっていたら、`drafting` に戻す。
main に入った後に `drafting` に戻した候補（`improving` → `drafting`）は、新しい下書きを入れる PR で、変わった古いファイルを外す（§7.7 の `scenario.retired`）。
同じ `id` のファイルを main に 2 つ置かない。

### 7.7 承認したシナリオを外す

- 承認したシナリオは、実際に起きた欠陥の再現なので、候補が `improving` から `candidate` に戻ったとき・再開したときも、スイートに残して使う
- 候補を却下したとき、またはシナリオが欠陥を正しく再現していないと人が判断したときは、人がシナリオを外す PR を作る。
  `/skill-candidates` は外す対象のファイルを示し、マージを確かめて `scenario.retired` を記録する（そのファイルがマージのコミットの親にあり、マージのコミットに無いこと＝その PR が外したことを git で確かめる）
- `scenario_approved` からの差し戻しと作り直しは main に入れる前に行うので、スイートは増えない。main に入った後の作り直しは、§7.6 の末尾のとおり古いファイルを外す

---

## 8. meta-harness への受け渡し（FT-12）

### 8.1 手順

`improving` の候補について、`/skill-candidates` が次の手順を 1 つずつ示し、利用者の確認を得て実行する。meta-harness の評価は費用がかかるので、まとめて自動では流さない。

| #   | 実行するもの                                                                                                                                                   | skill-evolution が記録・確認すること                                                            |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| 1   | `verify-suite`（§7.6）                                                                                                                                         | 評価の直前に、スイートの `suite_hash` が記録と同じか。違えば止める（FT-11）                     |
| 2   | baseline の候補の登録（空の overlay で `orchex meta register`）と評価（`orchex meta evaluate --json`）、`orchex meta frontier --target skill:<slug> --rebuild` | 評価の `own_suite_hash` が記録と同じか。新しい学習用シナリオの run の ID                        |
| 3   | `orchex meta propose --target skill:<slug> --focus-run <2 の run の ID> --json`                                                                                | meta-harness の候補 ID                                                                          |
| 4   | 3 の候補の評価（`orchex meta evaluate --json`。1 回の実行で学習用の後に holdout も評価する。`--scenario` と `--repeat` は付けない）                            | `own_suite_hash` が記録と同じか、`evaluator_hash`。`handoff.evaluation_linked`                  |
| 5   | 採用基準を満たしたら `orchex meta promote <3 の候補>`                                                                                                          | 昇格 PR の URL。`handoff.promotion_linked`。満たさなければ人が `improving` → `candidate` に戻す |
| 6   | 人が昇格 PR をマージした後、`orchex meta promote <3 の候補> --confirm`                                                                                         | §8.3                                                                                            |

- 手順 2・3・4 の `--json` の出力を一時ファイルに受け、`skill_candidates_admin.py link C-… --from-json <path>` で読む。手順 5 の PR は `link C-… --pr <URL>` で結び付ける。
  `own_suite_hash` が `scenario.suite_verified` の `suite_hash` と違う評価は、受け渡しの記録に結び付けない。
  `improving` → `promotion_pr` には、手順 3 で記録した meta-harness の候補 ID の holdout の評価で `verdict` が `pass` のものが要る（その候補の holdout の評価のうち最新のもので判定する。baseline の候補の holdout の評価、fail の評価では進めない。採用基準の判定ではなく、通っていない評価を昇格の根拠にしないための確認）。
  `evaluations` の run の ID は、同じ出力の `runs` にあり、その run の候補 ID が評価の候補 ID と同じであること。
  `link --pr` は、`gh` で PR がこのリポジトリのもので OPEN か MERGED であり、`.claude/skills/<slug>/` の下のファイルを変えていることを確かめる
- 採用基準を満たさなかったときは `skill_candidates_admin.py withdraw C-… --json -`（`{"note": …}`）で `improving` → `candidate` に戻す
- いまの `evaluate --json` は run ごとの結果（`run_id`・`cand_id`・`scenario_id`・`verdict` など）しか出さず、評価の ID・`own_suite_hash`・
  `evaluator_hash`・holdout かどうかは meta-harness の台帳の `evaluation_completed` にしか無い（2026-11-08 にコードで確認）。
  skill-evolution は meta-harness の台帳を読まないので、これらを `evaluate --json` の出力に足す（§8.2）
- `--scenario` で一部のシナリオだけを評価すると holdout が抜け、`--repeat` を変えると holdout の網羅の検査に通らず、どちらも昇格できない
- baseline の候補の holdout の評価は、昇格の条件ではない（昇格の条件は、昇格する候補の holdout の評価が通っていること）
- 採用基準（baseline との比較・holdout）は meta-harness が持つ。skill-evolution は判定しない
- 評価と昇格のあいだに meta-harness の評価の仕組み（fixtures など）が変わると、`evaluator_hash` が変わり、meta-harness が昇格を拒む。
  そのときは手順 4 からやり直す（skill-evolution は記録した `evaluator_hash` を示すだけで、判定は meta-harness に任せる）

### 8.2 meta-harness 側の変更

- 要件 §5.1 のとおり、meta-harness の要件のうち、オフライン層からの委譲を前提にした記述（§3.1 と FT-09）を、
  「skill-evolution の承認済みシナリオと受け渡しの記録を受け付ける」趣旨に改める
- スイートを足すたびに、meta-harness の回帰の予算の設定を変える（§7.6 の 2）。これは設定の変更で、シナリオの PR に入る
- `orchex meta evaluate --json` の出力に、評価の ID・`own_suite_hash`・`evaluator_hash`・holdout かどうかを足す（meta-harness のコードの変更。既存の項目は変えない）

### 8.3 マージの確認

`skill_candidates_admin.py confirm-merge C-…` が、`gh pr view <番号> --repo <このリポジトリ> --json state,mergeCommit,mergedAt,url` で、
PR が MERGED であること、URL のリポジトリが記録と同じこと、マージのコミットが origin/main から辿れることを確かめる。
マージのコミットの `.claude/skills/<slug>/` から版のハッシュを §4.3 の規則で計算して、`handoff.merged` を記録する
（ファイルは `git ls-tree -r` と `git cat-file blob` で読み、hook と同じハッシュの関数と除外の規則を使う。symlink と submodule の項目は、hook の走査と同じく数えない）。
マージの時刻は `mergedAt`（タイムゾーン付き）を使う。
meta-harness の `confirm_promotion` と同じ条件で、互いに独立に確かめる。

---

## 9. 昇格後の確認と再開

### 9.1 確認された再発なし（FT-13）

`promoted` の候補ごとに、`/skill-candidates` は次を示す。

- **該当する機会**: マージの時刻より後に始まり、`skill` が対象のスキルで、`skill_hash` が `handoff.merged` の版のハッシュと同じで、
  実際に走った（直接起動か、`run.confirmed` がある）実行範囲の数
- **帰属 `unknown` の証拠の数**: 該当する機会と同じセッションで、帰属が `unknown` のままの証拠の数
- 人が同じ欠陥を確定していなければ「N 回の該当する機会で、確認された再発なし（帰属 unknown の証拠 M 件）」と示す。率は出さない

### 9.2 再開

- 人が `/skill-candidates` で、マージ後の版の観測を「この候補と同じ欠陥」と確定すると、`recurrence.confirmed` を記録して `promoted` → `candidate` に戻す
- 再開した候補は、前の受け渡しの記録を履歴として持ったまま、§6.4 の主経路をもう一度たどる。前に承認したシナリオはスイートに残っている（§7.7）

### 9.3 確認を促す（FT-14、FT-17。should）

- マージ後の版で同じスキルに帰属した観測は、候補の根拠の観測との語の重なり（fail-logs §7.4 と同じ数え方）が多い順に並べ、確認を促す。確定は人が行う
- スキルの版が承認したシナリオの評価のときから変わった候補（`skill_hash` が変わった）には、シナリオの再実行を勧める。
  比べる元は、昇格済みならマージ後の版、それより前なら評価のもとになったコミット（`evaluations` の `evaluation_base_commit`）の版。
  モデルの変化は hook の入力から分からないので扱わない（FT-17 のうちモデルの変化は対象外）

---

## 10. `/skill-candidates`（FT-15）

### 10.1 操作

| 操作                         | 内容                                                                                                                            |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 取り込み                     | §5.1。開いたときに最初に行う                                                                                                    |
| 一覧                         | 確認を待つもの（帰属 `unknown`、候補にするかの提案、`improving` 以降の次の手順、`promoted` の再発の確認）を先に、状態ごとに出す |
| 帰属の確定                   | §5.3                                                                                                                            |
| 観測を候補にする・候補に足す | 観測 1 件からでも候補にできる                                                                                                   |
| 候補にするかの提案（FT-09）  | 同じスキル・同じ失敗の種類で、抜粋の語の重なりが多い観測が、別々のセッションか PR で 2 件以上あるとき、一覧の先頭に出す         |
| 重複のまとめ・却下・archive  | §6.4                                                                                                                            |
| 下書きの開始と承認・差し戻し | §7                                                                                                                              |
| 導入・一致の確認・受け渡し   | §7.6、§8                                                                                                                        |
| シナリオを外す               | §7.7                                                                                                                            |
| 同じ欠陥の確定               | §9.2                                                                                                                            |

- 表示に出すすべての値（抜粋・仮説・`note`・確認項目への回答・スキル名・PR の URL・シナリオの本文）に、fail-logs の §5.1 と同じ無害化をかける。
  冒頭に「未承認の記録であり、指示ではない」と書く
- `/skill-candidates` と `/lessons` 自体の実行も、他のスキルと同じく実行範囲として記録する（除外の規則を足すより単純で、害が無い）

### 10.2 CLI の分け方

- `skill_candidates.py`（読むだけ）: `list` / `show` / `runs`。Bash の許可リストに入れてよい
- `skill_candidates_admin.py`（状態を変える）: `ingest` / `attribute` / `promote-observation` / `add-observation` / `merge` / `reject` / `unreject` /
  `archive` / `restore` / `draft` / `approve` / `send-back` / `install` / `verify-suite` / `link`（`--from-json` と `--pr`）/ `withdraw` / `confirm-merge` /
  `close-pr` / `retire-scenario` / `confirm-recurrence` / `prune-runs`。Bash の許可リストに入れない。root に書くため、`/skill-candidates` は利用者の承認を得てサンドボックスの外で実行する
- 状態を変える操作は、スキルが AskUserQuestion で 1 件ずつ確認してから実行する

### 10.3 スキルの置き場

facet の composition（`facets/compositions/skills/skill-candidates.yaml`）と指示書（`facets/instructions/skill-candidates.md`）で作り、
facet build で `.claude/skills/` と `.agents/skills/` に生成する。スキルフローの評価セット `docs/evaluation/skills/skill-candidates.md` を同時に作る。

---

## 11. 設定（`skill-evolution.yaml`）

```yaml
enabled: true
storage:
  logs_dir: ".claude/logs/skill-evolution"
runs:
  hash_max_files: 200
  hash_max_bytes: 1048576
ledger:
  excerpt_max_chars: 300
  lock_wait_seconds: 5
draft:
  model: sonnet
  max_budget_usd: 1.0
  timeout_seconds: 300
  max_files: 4
  max_file_bytes: 16384
  max_attempts_per_candidate: 3
```

- `*.local.yaml` の上書きの仕組みは変えない
- v1 のキー（`storage.dir`・`lessons`・`pending`・`offline`・`trigger`）は非推奨期間の後に外した（§12.3）。`.local.yaml` に置いたままでも読み込みは失敗せず、値は使われない（NF-07）
- `enabled: false` のとき、hook は何も記録しない
- config を変えたら `.claude/orchestra.json` の `file_hashes` の該当エントリを同じコミットで更新する（AGENTS.md の変更ガードレール）

---

## 12. v1 の廃止と移行

### 12.1 廃止するもの（FT-21）

| v1 の機能                         | v2                                                             |
| --------------------------------- | -------------------------------------------------------------- |
| 自己申告ブロックと lessons の注入 | `inject-lessons.py` は出力しない（NF-01）                      |
| lessons の追記・圧縮              | 行わない。v2 の hook は lessons を読みも書きもしない           |
| 自己申告による成功判定            | outcome は既定で `unknown`。自己申告を読まない（FT-03）        |
| `check-trigger` の閾値            | 非推奨期間は「起動を勧めない」と返し、その後に外した（§12.3）  |
| オフライン層の構想（Issue #139）  | 作らない。#139 は ADR-20261108-065 で置き換えたとして閉じる    |
| metrics・pending・locks           | 書かない                                                       |
| transcript からの自己申告の抽出   | 行わない                                                       |

### 12.2 導入先の互換性（FT-22、NF-07）

- hook のファイル名と manifest の既存のエントリ（PreToolUse:Skill・PostToolUse:Skill・SubagentStop・Stop）を残す。
  `orchex sync` は manifest にない orchestra の hook を settings から消すので、エントリを消すと導入先の登録も消える。残すことで、
  導入先の settings の既存の登録は v2 の中身の hook を指したまま動く
- 新しいエントリ（UserPromptExpansion・UserPromptSubmit・SubagentStart）は `orchex sync` で導入先の settings に足される。
  これらに対応していない古い Claude Code では、その hook が動かないだけで、ほかの hook は動く（§14.1 の 9 からの推定）。その環境では直接起動が記録されない
- `orchex uninstall skill-evolution` は manifest から hook を外すので、manifest のエントリがそろっていれば成功する
- 非推奨期間は、`skill_evolution.py` の `status` / `check-trigger` / `evaluate` / `provenance` / `lock` を残して非推奨の旨を返し
  （`check-trigger` は v1 の「起動しない」と同じ終了コード）、`gitignore_sync.py` の `.claude/skill-evolution/metrics/`・
  `.claude/skill-evolution/pending/` も残した。期間の後に、v1 の設定のキーと一緒に外した（§12.3）

### 12.3 旧データの削除

- hook と CLI の通常の操作は、旧データを自動で削除しない
- 非推奨期間（v0.3.8）は、`skill_evolution.py purge-v1` が root と全 worktree の v1 のデータ（`storage.dir` の lessons、
  `logs_dir` の `metrics/`・`pending/`・`locks/`）を絶対パスの一覧にして出し、`--apply` を付けたときだけ消した
  （`.claude/` の下に収まるものだけ。v2 の保存先とその祖先、symlink は消さない）。期間の後に、ほかの v1 のサブコマンドと一緒に外した
- `purge-v1` を外した後に残った v1 のデータは、人が消す（場所は `docs/reference/logging.md` §2。`logs_dir` そのものは v2 の記録・台帳なので消さない）
- このリポジトリの `.claude/skill-evolution/lessons/`（git 管理）は、v2 の PR で削除した
- 非推奨期間は、v2 を含むリリース（v0.3.8）の 1 リリースとした。次のリリースで、`skill_evolution.py`（`purge-v1` を含む）、v1 の設定のキー、
  `gitignore_sync.py` のエントリを外した（CHANGELOG に **BREAKING**）。当初は「次のマイナーリリースまで」としていたが、
  2026-11-10 に利用者の決定で短くし、`purge-v1` も同時に外した（Issue #554）。hook のファイル名は v2 の役割で使い続けるので消さない

### 12.4 ドキュメントの追従

- `README.md`（skill-evolution の説明）、`docs/reference/packages.md`、`docs/reference/logging.md`
- `facets/policies/` の skill-review-policy（「二軸評価」の記述）と、その生成物
- `docs/evaluation/skill-evolution.md` と `.checks.yaml`（実装の前に更新する。evaluation-set-policy）、`docs/evaluation/skills/` の 5 ファイルの v1 への言及
- `docs/requirements/meta-harness.md` の §3.1 と FT-09、`docs/design/meta-harness.md` §9、loop-harness のドキュメントの v1 への言及

---

## 13. 要件への対応

| 要件                | 設計                                                                                                 |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| FT-01、FT-02        | §4.1〜§4.3                                                                                           |
| FT-03               | §12.1（outcome は `unknown`）。FT-20（外部の確認による outcome）は MVP の対象外                      |
| FT-04、FT-06        | §5.1、§5.3                                                                                           |
| FT-05、NF-06        | §4.4、§5.2                                                                                           |
| FT-07、NF-05        | §6.2、§6.3                                                                                           |
| FT-08、NF-03        | §6.4                                                                                                 |
| FT-09               | §10.1                                                                                                |
| FT-10、NF-09        | §7.1〜§7.5                                                                                           |
| FT-11               | §7.5、§7.6、§8.1                                                                                     |
| FT-12               | §6.2、§8                                                                                             |
| FT-13、FT-14        | §9.1〜§9.3                                                                                           |
| FT-15               | §10                                                                                                  |
| FT-16               | §6.4（人の操作の archive。自動では archive も削除もしない）                                          |
| FT-17               | §9.3（スキルの版の変化だけ。モデルの変化は対象外）                                                   |
| FT-18、FT-19        | §6.5（FT-19 は MVP の対象外）                                                                        |
| FT-21、FT-22、NF-07 | §12                                                                                                  |
| NF-01、NF-02        | §4.1                                                                                                 |
| NF-04               | §7.1（下書きはスイートの外、proposer は root の下を読めない）。holdout の保護は meta-harness の D-03 |
| NF-08               | §6.1                                                                                                 |

---

## 14. 確かめた前提と、残る確認

### 14.1 実機で確かめたこと（2026-11-08、Claude Code 2.1.319、macOS）

捨ててよい作業ディレクトリに、すべての出来事の入力を記録する hook と試験用のスキルを置き、`claude -p --model haiku --setting-sources project` で確かめた。

| #   | 確かめたこと                              | 結果                                                                                                                                                  |
| --- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | PreToolUse:Skill の `tool_input`          | `{"skill": "<name>"}`（引数があれば `args` も付く）。`tool_use_id` があり、PostToolUse と同じ値                                                       |
| 2   | `/<skill>` の直接起動                     | PreToolUse:Skill は発火しない。UserPromptSubmit の直後に UserPromptExpansion が来て、両者の `prompt_id` は同じ。`command_source` は `projectSettings` |
| 3   | メインのスレッドの `agent_id`             | 無い。サブエージェントの中の PreToolUse:Skill・SubagentStart・SubagentStop には `agent_id` と `agent_type` がある                                     |
| 4   | 権限で拒否された Skill の呼び出し         | PreToolUse だけが発火し、PostToolUse も PostToolUseFailure も発火しない（`run.confirmed` の設計どおり）                                               |
| 5   | 存在しないスキル名での呼び出し            | どの hook も発火しない（呼び出しの検査で止まる）                                                                                                      |
| 6   | サブエージェント                          | バックグラウンドで動き、メインの Stop の後に SubagentStop が来る。完了の通知でメインに UserPromptSubmit が来る                                        |
| 7   | `prompt_id` が入る出来事                  | UserPromptSubmit・UserPromptExpansion・PreToolUse・PostToolUse・SubagentStart・SubagentStop・Stop のすべて                                            |
| 8   | `model`                                   | どの出来事の入力にも無い                                                                                                                              |
| 9   | settings に知らない出来事の名前があるとき | その名前を無視し、ほかの hook は動く（settings 全体は拒まない）。古い版が新しい出来事の名前を同じように無視するかは、この版での挙動からの推定         |
| 10  | hook の実行時間                           | Python の起動が中央値 23 ms。スキルのディレクトリのハッシュは 44 個で中央値 0.2 ms、最大 2 ms（上限を超えたディレクトリでも 8 ms）                    |

### 14.2 コードで確かめたこと（2026-11-08）

| #   | 確かめたこと                                | 結果と設計への反映                                                                                                                               |
| --- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| 11  | `evaluate --json` / `propose --json` の出力 | 評価の ID・`own_suite_hash`・`evaluator_hash` が無い → meta-harness に足す（§8.1・§8.2）                                                         |
| 12  | holdout の評価の起動                        | 1 回の `evaluate` で学習用の後に holdout も評価する。baseline の holdout は昇格の条件ではない（§8.1）                                            |
| 13  | baseline の登録                             | 空の overlay を受け付ける。手順は `docs/design/meta-harness-detailed.md` の baseline の節（§8.1 の手順 2）                                       |
| 14  | シナリオの `id`                             | meta-harness は重複を検査しない。学習用の `graded` の有無をそろえる必要がある（§7.3）                                                            |
| 15  | `evaluator_hash` に入るもの                 | `regression` の設定全体と `scenarios/fixtures/` を含む（§7.6 の 2）                                                                              |
| 16  | Docker の backend での検査                  | `command_exit` と `rubric_judge` はコンテナの中で動く。`artifact_exists`・`json_schema` はホストで作業ディレクトリを読むだけ                     |
| 17  | proposer の読み取り                         | `$HOME`・root・全 worktree を拒む。台帳は読めない（§7.1）                                                                                        |
| 18  | fixtures のスクリプトの引数                 | `assert-function-behavior.py` は引数のモジュールを実行し、`merge-local-cli-tools-override.py --project-root` は書き込み先を変える（§7.4 で除外） |

### 14.3 実装のときに足すもの

- proposer の読み取り制限が `.claude/logs/skill-evolution/` を拒むことの回帰テスト（meta-harness のテスト。いまは root の包括の拒否で守られている）
- `evaluate --json` に足す項目のテスト（meta-harness）

---

## 15. 実装の順序と変更するファイル

| #   | 内容                                                                                                                | 主な変更                                                                                                           |
| --- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| 1   | 要件・設計・ADR、meta-harness の要件（§3.1・FT-09）、評価セット                                                     | `docs/requirements/`、`docs/design/`、`docs/adr/`、`docs/evaluation/`                                              |
| 2   | 実行範囲の記録と注入の停止。v1 の CLI の非推奨化                                                                    | `packages/skill-evolution/hooks/`、`lib/`、`manifest.json`、`config/`、`.claude/orchestra.json`                    |
| 3   | 取り込み・帰属・台帳と `/skill-candidates` の一覧・帰属・候補の操作                                                 | `lib/`、`scripts/skill_candidates*.py`、`facets/compositions/skills/skill-candidates.yaml`、`facets/instructions/` |
| 4   | 下書き・形の検査・承認・導入・一致の確認                                                                            | `lib/`、`scripts/skill_candidates_admin.py`                                                                        |
| 5   | 受け渡しの記録・マージの確認・再発の確認                                                                            | `lib/`、`scripts/skill_candidates_admin.py`                                                                        |
| 6   | `purge-v1`、このリポジトリの lessons の削除、ドキュメントの追従（§12.4）、`design:meta-harness` の追従か `codd ack` | `scripts/skill_evolution.py`、`.claude/skill-evolution/`、`README.md`、`docs/reference/`                           |

- 2 だけで v1 の注入が止まる（NF-01）。2 を先に出し、3 以降は台帳に観測が溜まるのと並行して進める
- meta-harness の `evaluate --json` に項目を足す変更（§8.2）は、5 の前に入れる
