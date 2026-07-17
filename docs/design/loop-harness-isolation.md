---
codd:
  node_id: "design:loop-harness-isolation"
  kind: design
  status: draft
  depends_on:
    - id: "req:loop-harness"
      relation: derives_from
    - id: "design:loop-harness-cli"
      relation: refines
    - id: "adr:ADR-20260712-034"
      relation: references
  owner: ai-orchestra
---

# Loop Harness 詳細設計（Maker/Checker Docker 完全隔離編）

**作成日**: 2026-07-15
**ステータス**: draft（Issue #211 の設計フェーズ成果物。実装は本書確定後に別 PR で行う）
**対象**: LP-2（`loop_driver.py` の headless `claude -p` 実行）のみ。LP-1（セッション内伴走型、`loop_step.py`）は対象外
**関連**: `design:loop-harness`（基本設計）、`design:loop-harness-cli`（CLI 詳細設計。§2.2 に現行の多層防御と本 Issue への申し送りがある）、
`adr:ADR-20260712-034`（meta-harness の Docker + ephemeral broker 移行。本設計の流用元）、`adr:ADR-20260715-037`（本設計の決定を記録する ADR）

> 本書は `docs/design/loop-harness-cli.md` §2.2 末尾の「残余リスク（同一 UID 前提）」および
> Issue #231（イメージキャッシュ肥大化）の教訓を踏まえ、Maker/Checker の実行境界を
> プロセスレベルの多層防御から Docker コンテナによる構造的隔離へ引き上げる設計を確定する。

---

## 0. 前提と非目標

- **前提**: LP-2 は現在、`docs/design/loop-harness-cli.md` §2.2 の 4 層防御（プロンプト／env 認証剥奪／
  disallowedTools+hook／push 後整合性検証）で運用されている。これは「同一 UID・同一プロセス空間」を
  前提とした振る舞いベースの防御であり、`.claude/loop/<loop_id>/` の journal/state ファイルや
  `.git/config` への直接書き込みに対する構造的な保証は持たない（同ドキュメントが明示的に本 Issue へ
  申し送っている）。
- **非目標**: 4 層防御を置き換えるのではなく、**その上に構造的な境界を追加する**。Docker daemon が
  使えない環境（CI・軽量サンドボックス等）向けに、既存の非隔離運用は `isolation.backend: none` として
  後方互換のまま残す（§7）。
- **非目標**: LP-1 のセッション内実行（人間が Claude Code のセッション内で直接操作する経路）は
  対象外。LP-1 は人間の監督下にあり、既存の Claude Code 権限モデルで十分と判断する（`design:loop-harness-cli` の
  スコープ分離を継承）。

---

## 1. 脅威モデルと隔離境界

### 1.1 攻撃者モデル

Maker（`claude -p` が生成・実行するコード）は Issue 本文・PR コメント等の**信頼できない入力**に
影響され得る。以下のいずれかが敵対的に振る舞う可能性を想定する。

- Maker が生成した任意のシェルコマンド（`Bash(pytest *)` 経由の間接実行を含む）
- Checker の `mechanical.commands`（Maker が直前の反復で仕込んだコードを import/実行し得る）
- LLM レビュアー呼び出し（読み取り専用だが、diff 内容次第でプロンプトインジェクションを受け得る）

### 1.2 コンテナに入るもの / driver に残るものの対照表

| 項目                                  | driver（host、信頼境界内）                                | container（Maker/Checker、信頼境界外）                              |
| ------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------------- |
| git/gh push・PR 作成の資格情報        | 保持し、push/PR 作成は driver が実行（既存方針を継続）      | 一切持たない（コンテナに認証系ファイル・env を注入しない）            |
| `.claude/loop/<loop_id>/` state/journal | rw、driver のみがアクセス                                  | **マウントしない**（同一 UID 残余リスクの解消。1.3 節）               |
| worktree ファイル                     | rw（Maker 実行前後の検証のため）                            | Maker: rw（1:1 パス）／ Checker: **ro**（確定方針 6）                  |
| worktree の `.git` ポインタファイル   | rw（本来の `gitdir: <common_dir>/worktrees/<name>` を保持） | **ro overlay マウント**（Maker/Checker とも改ざん不可。4.3.2 節 Fix-3） |
| 共有 git refs（`refs/heads/*` 全体）  | rw（driver 専任の CAS 更新。4 節）                          | **触れない**（Maker/Checker とも自身専用の ephemeral GIT_DIR 経由のみ。4 節） |
| 共有 git objects（`objects/`）        | rw                                                          | ro（alternates 経由の読み取りのみ。Maker/Checker 共通。4 節）           |
| `.git/config` / `hooks/` / reflog / 他ブランチの ref | rw（driver 側 hardening は継続。§2.2 の RC1〜RC3 は引き続き有効） | **マウントしない**（common dir 全体を渡さず、sanitized な ephemeral GIT_DIR のみを渡すため、改ざんだけでなく閲覧も物理的に不可。4.3.4 節） |
| Anthropic API 資格情報                | broker サイドカーのみ保持                                   | broker 経由でのみ利用可（`ANTHROPIC_BASE_URL` 差し替え）              |
| ネットワーク egress                   | 制限なし                                                    | `--internal` network のみ。broker 以外に到達不可                      |
| Docker socket                         | 使用（`docker run` の発行元）                                | 一切マウントしない                                                    |

### 1.3 解消される残余リスク / 引き続き残るリスク

- **解消**: `.claude/loop/` state/journal ファイルへの同一 UID 経由の直接改ざん（`design:loop-harness-cli` §2.2
  末尾の「残余リスク」として明記されていたもの）は、コンテナに当該ディレクトリを一切マウントしない
  ことで構造的に不可能になる。
- **解消**: `.git/config`/`hooks/` の直接書き換え（SEC-CRIT 系対応で検知・停止はできていたが、
  「書けてしまう」こと自体は防げていなかった）は、共有 common dir をコンテナに渡さないことで
  物理的にアクセス不能になる（4 節）。
- **解消**: Issue #231 の相当（イメージキャッシュの肥大化・並行ビルド未保護）は 5 節で構造的に修正する。
- **継続**: `mechanical.commands` の denylist バイパス（`design:loop-harness-cli` §2.2 の SN3-accept
  で受容済みの残バイパス）は、コンテナ隔離後も「信頼された作者の YAML に対する best-effort チェック」
  という位置づけ自体は変わらない。ただし敵対的コード実行の被害範囲はコンテナの egress 制限・
  ファイルシステム境界によって大幅に縮小される。
- **継続**: broker 自身（サイドカー）が乗っ取られた場合のリスクは ADR-20260712-034 のスコープと同じ
  受容基準（`api.anthropic.com` への egress 限定、tmpfs token、read-only rootfs）を踏襲する。

---

## 2. コンテナライフサイクル

### 2.1 起動〜破棄のシーケンス（1 アクション = 1 コンテナ、常駐なし）

```text
driver (host)                          broker (sidecar)          scenario (Maker/Checker)
  |
  |-- action = run_maker/run_checker --------------------------->
  |-- (Maker のみ) ephemeral git dir 準備（4 節）
  |-- broker 起動 (docker run -d --rm, dual-homed) ------------->|
  |                                                               |-- token 注入(tmpfs) → unlink
  |-- scenario コンテナ起動 (docker run --rm, --internal) ------------------------------->|
  |                                                                                          |-- claude -p / mechanical.commands
  |                                                                                          |   (ANTHROPIC_BASE_URL=broker)
  |<-- (正常終了 / timeout / 中断のいずれでも) ---------------------------------------------|
  |-- docker rm -f scenario ---------------------------------------------------------------->
  |-- broker cleanup (docker rm -f + network 2 本削除) --------->|
  |-- (Maker のみ) git 同期（fetch + CAS update-ref。4 節）
  |-- ephemeral git dir 破棄
  |-- 既存の loop_driver ロジック（_verify_push_integrity_or_stop 等）へ復帰
```

- コンテナは **run スコープでオンデマンド起動・実行後破棄**する（確定方針 1）。`loop_scheduler.py` の
  worker 常駐とは独立で、常駐するのは driver プロセスのみ。
- 正常終了・timeout・例外の**全経路**で `docker rm -f`（scenario）→ broker `cleanup()` の順に
  try/finally で実行する（meta-harness の `cleanup_docker_launch()` と同じ規律。SIGKILL 等の
  異常経路はアイドルタイムアウト + 起動時 stale cleanup で有限時間内に回収する。ADR-20260712-034 と同基準）。

### 2.2 Maker と Checker の差分

| 項目               | Maker                                                         | Checker                                                  |
| ------------------ | -------------------------------------------------------------- | --------------------------------------------------------- |
| worktree マウント  | rw（1:1 パス）                                                 | **ro**（確定方針 6。誤書き込みを物理的に不可能にする。#196） |
| git メタデータ     | ephemeral GIT_DIR（rw、index は baseline から `read-tree` 初期化。4.3.1 節）+ 共有 `objects/` ro（4 節） | **Maker と同様の ephemeral GIT_DIR を Checker 専用に別途生成し ro で渡す**（4.3.4 節。common dir 全体を渡すと `.git/config`・他ブランチの ref・reflog・hooks の**閲覧**が可能になり資格情報隔離が破れるため、Checker にも sanitize が必要。Codex レビュー指摘反映） |
| 実行内容           | `claude -p`（編集 + `git commit` まで。push はしない）          | `mechanical.commands`（subprocess）+ LLM レビュアー `claude -p`（読み取り専用） |
| コンテナ後の driver 処理 | git 同期（4 節）+ push 前整合性検証（既存 §2.2 のロジックを継続） | artifact 保存のみ（既存 `run-checker` 相当のロジックを継続） |

---

## 3. コンテナ実行時のセキュリティプロファイル

meta-harness（`packages/meta-harness/lib/scenario_docker_profile.py`）の scenario コンテナ設定を
そのまま踏襲する（確定方針 4）。

- `--read-only`（rootfs）/ `--cap-drop=ALL` / `--security-opt=no-new-privileges` / non-root user
- `--tmpfs` で `$HOME`・`/tmp` を用意（書き込みは対象 mount と tmpfs のみに限定）
- `--pids-limit` / `--memory` / `--cpus` の資源上限（config で調整可。7 節）
- Docker socket は**決してマウントしない**
- ネットワークは `--internal` の Docker network のみに接続し、broker 以外に到達不可
- Docker socket 非搭載・`--internal` の DNS/egress 遮断は ADR-20260712-034 のスパイク S3/S3b で
  実証済みの性質をそのまま継承する（再検証は不要。同じ Docker daemon 機構に依存するため）

---

## 4. git commit をコンテナ内で成立させる方式（common dir 共有問題）

### 4.1 問題設定

loop-harness の issue worktree は `git worktree add` で作成される **linked worktree** であり、
`<worktree>/.git` は実体を持たず `gitdir: <common_dir>/worktrees/<name>` を指すテキストファイルに
過ぎない。共有される `<common_dir>`（ルート worktree の `.git` 実体）には `objects/`・`refs/`
（全 issue の全ブランチ + `main` を含む）・`config`・`hooks/` が同居する。

Maker はコンテナ内で `git commit` を成立させる必要がある（確定方針 2: push/PR 作成は driver、
commit までは Maker）。しかし `<common_dir>` をそのままコンテナへ rw マウントすると、Maker が
**他 issue のブランチや `main` の ref・共有 `config`/`hooks` を直接書き換えられてしまう**
（1.3 節で解消したい残余リスクをそのまま再現することになり、コンテナ化の意味が薄れる）。

### 4.2 検討した選択肢

#### 選択肢A: common dir 全体を rw マウント（不採用）

- メリット: 実装が単純（既存の `.git` 解決をそのまま使える）。
- デメリット: 1.3 節で解消したいはずの「同一 UID・任意 ref/config 書き換え」リスクをコンテナ内でも
  再現してしまう。コンテナの隔離境界が git メタデータに関しては無意味になる。**不採用**。

#### 選択肢B: meta-harness の使い捨て bare snapshot（`_prepare_isolated_git`）をそのまま流用（不採用）

- meta-harness の scenario 実行は `git init --bare` した使い捨てスナップショットへ現在の作業ツリー
  内容を 1 コミットとして固め、`git` wrapper 経由でそのスナップショットだけを見せる。scenario の
  git 操作は完全に使い捨てで、**実行後にホスト側の実リポジトリへ書き戻す必要がない**（proposer/scenario
  の成果物は diff・ログとして評価されるのみ）。
- loop-harness の Maker は逆に、**コンテナ終了後もその commit がホスト側の実ブランチに存在し、
  driver が push できる状態**であることが要件（確定方針 2 で Maker が commit まで行う設計を維持する
  以上、必須）。使い捨てスナップショットを discard する meta-harness の方式は**この要件を満たせない**
  ため、そのままの流用は**不採用**。ただし「独立した git メタデータをコンテナに見せる」という
  発想自体は次の採用案に引き継ぐ。

#### 選択肢C: ephemeral GIT_DIR（alternates 経由の読み取り共有）+ driver 側 CAS 書き戻し（採用）

コンテナには「このアクション専用の使い捨て bare repo」を GIT_DIR として渡し、オブジェクトは
共有 `objects/` から **alternates で読み取り専用に借りる**。commit で新規生成されるオブジェクトは
すべて ephemeral repo 自身の `objects/` に書かれ、共有 `objects/` には一切書き込まれない。
ref は ephemeral repo 内の 1 本（このループのブランチ名と同名）だけが存在し、他ブランチの ref は
そもそも見えない。

コンテナ終了後、**driver（host、信頼境界内）だけ**が共有 common dir に対して
fast-forward-only の `fetch` + 期待値照合つき `update-ref`（CAS）を実行し、実ブランチへ反映する。
共有 ref への書き込み権限を持つプロセスは driver だけであり、コンテナ内プロセスは共有 ref に
**構造的にアクセスできない**。

- メリット: 他 issue・`main` の ref/config/hooks への書き込み経路が物理的に存在しない。オブジェクト
  DB は読み取り専用共有（alternates）なので二重化コストもない。driver 側の CAS 書き戻しは既存の
  `state_version`/`lease_token` の CAS パターンと同じ設計原則で説明が付く。
- デメリット: Maker はコンテナ内で自ブランチ以外の ref（`main` との diff 等）を参照できない
  （4.5 節「既知の制約」）。書き戻しステップの実装が増える（ただし driver 側の単純な git 呼び出し
  数手順で済み、新規の外部依存はない）。

**選択肢Cを採用する。**

### 4.3 選択肢Cの具体的な手順

> **2026-07-15 Codex 設計レビュー反映（Critical 6件）**: 初版の手順には (1) ephemeral repo の
> index 未初期化によるファイル全消失 commit のリスク、(2) checkout 済みブランチへの直接 `fetch` が
> git 自身に拒否され実行不能、CAS も原子的でない、(3) worktree 内の `.git` ポインタが rw マウント
> されたままで Maker が改ざんし host 側操作を乗っ取れる、という 3 件の Critical 不備があった。
> 以下は指摘を反映した確定手順。

#### 4.3.1 事前準備（driver、host。Maker 用）

1. `common_dir = git rev-parse --path-format=absolute --git-common-dir`（`state.worktree_path` を cwd に。
   既存の `_resolve_root_worktree` と同じ解決方式を流用）。runtime の起点は呼び出し引数をそのまま
   信頼せず、この `common_dir` から導出した root worktree とし、runtime の既存の親要素が symlink
   の場合は fail-closed にする。**[Fix-12. 2026-07-18 PR #256 レビュー指摘反映（3巡目）。High]**
   runtime dir 自体の起点は `common_dir.parent` ではなく、呼び出し引数 `project_dir` を
   `git -C <project_dir> rev-parse --git-common-dir` で解決し直し `common_dir` と一致することを
   検証済みの `requested_project` とする。`git init --separate-git-dir=<elsewhere>` 等で共有
   Git dir が worktree の外の任意の場所へ再配置されている構成では `common_dir.parent` は
   呼び出し元 `project_dir` と一致しないため、そのまま使うと runtime dir（pinned snapshot を含む）
   が呼び出し元の意図しないディレクトリ配下に作られてしまう。「runtime は worktree の外」という
   既存の `_validate_runtime_location` 検証はそのまま維持する。
2. `baseline_sha = git -C <worktree_path> rev-parse <branch>`（`_persist_pre_maker_head` が既に
   記録している値と同一のもので構わない）。同時に `symbolic-ref HEAD` が対象 branch ref と一致する
   ことを確認し、別 branch / detached HEAD の worktree は受理しない。
3. `ephemeral_dir = .claude/loop/<loop_id>/docker-runtime/<action_id>/git-ephemeral/`
   （アクション単位。既存の journal/state ディレクトリとは独立し、driver がアクション完了後に
   `shutil.rmtree` する使い捨てディレクトリ）。
4. `git init --bare <ephemeral_dir>`。**[Fix-13. 2026-07-18 PR #256 レビュー指摘反映（3巡目）。
   High]** `--object-format` を指定しない `git init --bare` は常に既定の sha1 リポジトリを
   作る。SHA-256 object format で運用されているソースリポジトリに対してこれを行うと、
   手順7の `update-ref` が渡す 64 桁 hex の baseline SHA を 40 桁 hex 前提の sha1 リポジトリが
   拒否し、ephemeral repo の初期化そのものが失敗する。`git -C <worktree_path> rev-parse
   --show-object-format` でソース側の object format を検出し、検出に成功した場合のみ
   `--object-format=<検出値>` を `init --bare` に渡す（検出に失敗する場合は旧来どおり既定の
   sha1 のまま初期化し、動作を変えない）。
5. **[Fix-6] commit identity の seed**:
   `git --git-dir=<ephemeral_dir> config user.name "loop-harness-maker"` /
   `git --git-dir=<ephemeral_dir> config user.email "loop-harness-maker@invalid"`
   （コンテナの `HOME` は `maker_scratch_home()` の空 scratch ディレクトリであり `~/.gitconfig` を
   持たない。共有 `common_dir/config` の `user.*` もコンテナには見せない設計のため、これを行わないと
   Maker の `git commit` が `Author identity unknown` で失敗する。meta-harness の
   `_prepare_isolated_git()` が使い捨てスナップショットに `-c user.name=meta-harness` を与える
   のと同じ考え方で、repo ローカルの ephemeral repo にのみ設定する合成 identity とする）
   identity 設定後の trusted な `config` は runtime の mount 対象外ファイルにも snapshot し、
   事後処理では Maker 所有 config をこの内容へ原子的に戻してから最初の host Git を起動する。
   これにより任意名の `filter.*.clean/process` 等、Fix-5 の既知キー以外の executable config も
   host 権限で実行されない。
6. `echo <common_dir>/objects > <ephemeral_dir>/objects/info/alternates`
   （**host パスをそのまま書く**。4.4 節の「1:1 パスマウント」により、コンテナ内でも同じパスで
   解決できるため、パス変換は不要）
7. `git --git-dir=<ephemeral_dir> update-ref refs/heads/<branch> <baseline_sha>`
   （ネットワーク・オブジェクト転送なしの ref 作成のみ。baseline はすでに alternates 経由で
   読めるオブジェクトなので追加の fetch は不要）
8. `git --git-dir=<ephemeral_dir> symbolic-ref HEAD refs/heads/<branch>`
9. **[Fix-1] `GIT_DIR=<ephemeral_dir> git read-tree <baseline_sha>`**
   （ephemeral repo の index を baseline のツリー内容で初期化する。これを省略すると
   ephemeral repo の index は空のままになり、Maker が変更ファイルだけ `git add` しても
   「baseline の全ファイルが削除された」commit が生成されてしまう。alternates 経由で
   baseline のオブジェクトは既に読めるため、追加の fetch なしでこの操作は完結する）。
   **[Fix-11. 2026-07-18 PR #256 レビュー指摘反映（3巡目）。Critical]** この `read-tree`
   を含め、driver（host）が起動する全ての git 呼び出しの環境は、呼び出し元 Python プロセス自身の
   *ambient* な `GIT_INDEX_FILE`/`GIT_DIR`/`GIT_WORK_TREE` 等の Git repository-location 系
   環境変数を明示的に除去したものから構築する（loop-harness 自身が別の git hook/wrapper の内側で
   実行される等で、driver プロセスの環境にたまたま `GIT_INDEX_FILE` が設定されていた場合、
   素朴に `os.environ` を継承すると本手順の書き込み先が意図した `<ephemeral_dir>/index` ではなく
   その ambient なパスへ静かにすり替わり、ephemeral repo の実 index が空のまま残ってしまう
   ため）。`GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` を明示的に指定する呼び出し（本手順、
   4.3.1 手順11・Fix-10、4.3.3 手順3・Fix-9）はこの除去後に明示値を上書きするため、意図した
   値は変わらず優先される。
10. **[Fix-3] `.git` ポインタの改ざん防止準備**: `<worktree_path>/.git` は本来
    `gitdir: <common_dir>/worktrees/<name>` を指すテキストファイルである。この内容を
    `pinned_git_pointer = <runtime dir>/pinned-dotgit` にコピーしておく（コンテナ起動前、
    trusted な内容として一度だけスナップショット）。4.3.2 節でこのファイルを
    `<worktree_path>/.git` に **ro で上書き bind mount** することで、worktree 自体は rw でも
    `.git` ポインタだけはコンテナから書き換え不能にする。
11. **[Fix-10. 2026-07-17 PR #256 レビュー指摘反映。High]** `ephemeral_dir` の作成（手順4）より前に、
    Maker がまだ到達できない `common_dir` を `GIT_DIR` とし、`baseline_sha` の tree から
    `read-tree` で新規構築した host 専用の一時 index に対して、**`target_sha`（＝`baseline_sha`）
    相対の比較のみ**で worktree が dirty でないことを検証する（`HEAD` は一切参照しない。
    **[Fix-15. 2026-07-18. Critical]** 詳細下記）。`worktree_manager.create_worktree()`
    は前回アクションの worktree を再利用し得るため、これを省略すると前回中断アクションの
    未コミット変更（あるいは `--skip-worktree`/`--assume-unchanged` で隠された変更）が
    `ephemeral_dir` の index seed に紛れ込み、事後処理（4.3.3 節手順3・Fix-9）の trusted-tree
    検証をすり抜けたまま共有 branch へ書き戻され得る。検証には 4.3.3 節手順3（Fix-9）と同じ
    trusted-index 比較ロジックを、比較先を `ephemeral_dir`/`new_sha` ではなく `common_dir`/
    `baseline_sha` に差し替えて再利用する。dirty と判定された場合は `ephemeral_dir` を作成せず
    `EphemeralGitInfrastructureError` で fail-closed する。
    **[Fix-15. 2026-07-18 発見・同日修正。Critical]** 当初の実装は `git status --porcelain` を
    そのまま使っており、その staged 列（index vs `HEAD`）が本手順の `GIT_DIR=<common_dir>` 経由で
    primary worktree（通常 `main`）の `HEAD` に解決されてしまい、`main` が Maker branch と独立に
    進んだだけ（＝通常運用で常時発生）で worktree に実 drift が無くても常に dirty 判定される不具合が
    あった。`_verify_worktree_matches_trusted_tree`（`packages/loop-harness/lib/loop_git_ephemeral_support.py`）
    は現在 `update-index --refresh` + `diff-files --name-status`（追跡ファイル、index vs worktree
    のみ）と `ls-files --others --exclude-standard`（未追跡ファイル）の組み合わせで判定しており、
    `HEAD` を一切参照しない。回帰テストは `test_prepare_accepts_clean_worktree_when_primary_worktree_head_has_diverged`
    （`packages/loop-harness/tests/test_loop_git_ephemeral.py`）。

#### 4.3.2 コンテナ実行

- マウント（すべて **1:1 パス**。4.4 節）:
  - `<worktree_path>` → 同パス、rw
  - **`pinned_git_pointer` → `<worktree_path>/.git`、ro**（worktree 全体の rw マウントより後に
    重ねてマウントする。より限定的なマウントが優先されるため `.git` だけが ro になる。**[Fix-3]**）
  - `<ephemeral_dir>` → 同パス、rw
  - `<common_dir>/objects` → 同パス、**ro**
- env: 既存 `maker_env()`（層2 の認証剥奪。git/gh 資格情報を渡さない方針は継続）に加え、
  `GIT_DIR=<ephemeral_dir>` / `GIT_WORK_TREE=<worktree_path>` を明示する。これにより Maker の
  `git add`/`git commit`/`git status`/`git diff`（既存 `--allowedTools` の許可範囲。§2.2 参照）は
  ephemeral repo の index/ref に対して働き、ファイル自体は実 worktree 上で編集される。
- **[Fix-3] 補足**: `.git` を ro 保護しても、Maker が `GIT_DIR`/`GIT_WORK_TREE` を無視して
  `.git` 経由の自動解決（`common_dir/worktrees/<name>` への到達）を試みる可能性はゼロではない。
  ただし到達先の `common_dir/worktrees/<name>` 自体はマウントされていないため、コンテナ内からは
  どのみち解決不能である（`.git` の ro 化は主として **4.3.3 節のホスト側事後処理**が汚染された
  `.git` を踏まないようにするための保護であり、Maker 自身の到達可否は「マウントしない」という
  4 節冒頭の原則で別途担保されている）。

#### 4.3.3 事後処理（driver、host。コンテナ破棄後）

> **[Fix-2] checkout 済みブランチへの直接 `fetch` は git 自身が
> `refusing to fetch into branch ... checked out` として拒否するため実行不能だった。
> 一時 ref を経由し、`update-ref <ref> <new> <old>` の原子的 CAS で確定する手順に修正する。**

> **[Fix-5. 2026-07-16 PR #245 レビュー指摘反映。Critical]** `<ephemeral_dir>` はコンテナ内で
> Maker が rw で書き込めるディレクトリであり、その `config` ファイル自体も Maker が自由に
> 書き換えられる。コンテナ破棄後、**host（driver、フルホスト権限）** がこの `<ephemeral_dir>` を
> 対象に複数回 git コマンドを実行するため、Maker が `<ephemeral_dir>/config` に
> `core.fsmonitor=<悪意あるコマンド>` を仕込んでいた場合、以下の手順3（`git status`）実行時に
> host 権限でそのコマンドが実行される（`core.hooksPath=/dev/null` は `core.fsmonitor` を
> カバーしない、別の config キーのため）。同様に手順4の `git fetch <ephemeral_dir> ...` は
> `<ephemeral_dir>` を fetch の**転送元**として `git upload-pack` を起動するため、
> `uploadpack.packObjectsHook` が仕込まれていれば同じく host 権限でコマンド実行され得る。
> **`<ephemeral_dir>` に触れる全ての host 側 git 呼び出し（手順1・3・4のすべて）は、
> 既存の `hardened_git_config_args()`（`-c credential.helper= -c core.hooksPath=/dev/null`）に加えて
> `-c core.fsmonitor= -c uploadpack.packObjectsHook=` を明示的に追加した引数で実行する**こと
> （`<ephemeral_dir>` は共有 common dir とは異なり Maker が config ファイルそのものを完全に
> 所有しているため、既存ヘルパーがカバーする脅威モデルより広い上書きが必要。実装時は
> `hardened_git_config_args()` を拡張するか、ephemeral dir 専用の派生ヘルパーを新設する）。
> さらに、任意名を持てる `filter.*` のように固定キーの上書きでは列挙しきれない executable config
> を排除するため、4.3.1 手順5の trusted snapshot を host Git 起動前に原子的に復元する。host の
> global/system config に同名 filter が定義されている場合も Maker 管理 `.gitattributes` から起動
> できないよう、これらの host Git 環境は `GIT_CONFIG_GLOBAL=/dev/null` /
> `GIT_CONFIG_NOSYSTEM=1` に固定する。**Fix-5 のこの `-c` 上書き集合（`hardened_git_config_args()`
> への集約）は `<ephemeral_dir>` に触れる呼び出しに限定せず、driver が所有する全ての git 呼び出し
> （common dir 側の呼び出しも含む）に一律で適用される実装とした。個々の呼び出しごとに
> 「この呼び出しは ephemeral_dir に触れるか」を判定する分岐を持たせるより、単純さ・レビュー容易性
> を優先した結果であり、ephemeral_dir に無関係な呼び出しへの適用は過剰防御（実害はない）である。**

> **[Fix-7. 2026-07-17 レビュー指摘反映。Critical]** `<ephemeral_dir>/objects/info/alternates`
> は Fix-5 の `config` と同じ脅威モデルに属する: Maker が rw で書き換え放題であり、host がこの
> ディレクトリを対象に git コマンドを実行する前提が崩れる。Maker が `alternates` を host から
> 到達可能な任意の object store（Maker のコンテナからは見えなくても、host のフルファイルシステム
> 権限では読める場所）に書き換え、その store にしか存在しないオブジェクトを参照する baseline の
> 子孫 commit（`merge-base --is-ancestor` の祖先チェックは commit の親リンクのみを辿るため、
> tree/blob の中身がどこから来たかまでは検証しない）を fabricate すると、手順4の `fetch` はその
> 外部オブジェクトを解決・pack し、**共有 `common_dir/objects` へ永続的にコピーしてしまう**
> （confused deputy: host が Maker の代わりに信頼境界外のオブジェクトを共有領域へ持ち込む）。
> 対策として、`config` の trusted snapshot 復元（Fix-5）と**同じ finalize 冒頭のタイミング**で、
> `alternates` を `<common_dir>/objects` の 1 行だけへ原子的に強制上書きする（内容は
> `common_dir` から常に導出できる決定論的な値のため、`config` と異なり別途 snapshot を持つ必要は
> ない）。同じ理由で `objects/info/http-alternates`（loop-harness 自身は書き込まないため、存在
> 自体が改ざんの証跡）も削除する。`<ephemeral_dir>/objects/` 配下に Maker が直接書き込んだ loose
> object・pack file（`info/` の外側）は対象外とする（alternates 経由の外部オブジェクト参照では
> なく、Maker が worktree 上で正当に編集したファイル内容から `git add`/`git commit` が新規生成
> するオブジェクトと区別がつかないため）。件数・サイズの上限が無いこと自体は別の DoS 観点として
> Issue #255（フォローアップ、9.2 節の受容リスクにも追記）で追跡する。

0. **[Fix-7]** 手順1（最初に host git が `<ephemeral_dir>` に触れる箇所）より前に、`config`
   の trusted snapshot 復元（4.3.1 手順5・Fix-5）と合わせて `alternates` を
   `<common_dir>/objects` 1 行のみへ原子的に強制上書きし、`http-alternates` を削除する。
1. `new_sha = git --git-dir=<ephemeral_dir> <hardened args + Fix-5 追加分> rev-parse refs/heads/<branch>`
2. `new_sha == baseline_sha` なら「Maker がコミットしなかった」として扱う（既存 `_verify_maker_commit`
   のロジックを、比較対象を「worktree の `git status --porcelain`」から「ephemeral repo の ref 移動」
   へ差し替える形で継続利用する）。
3. **[Fix-1 検証 / Fix-5 適用]** コミットがある場合、`GIT_DIR=<ephemeral_dir>
   GIT_WORK_TREE=<worktree_path>` の下で `target_sha`（＝`new_sha`）相対の trusted-index 比較
   （**[Fix-15]** 下記。`HEAD` は一切参照しない）が **dirty 無しであること**を必須検証する
   （Maker の最終 commit 後に working directory と ephemeral repo の内容が一致するか）。
   **[Fix-14. 2026-07-18 PR #256 レビュー指摘反映（3巡目）。Major]**
   この検証（および 4.3.1 手順11・Fix-10 の prepare 側検証）の dirty 判定では、`??`（untracked）
   かつ `.claude/config/**/*.local.yaml` または `*.local.json` に一致する行のみを dirty から
   除外する。これらは `config-loading` ルールが定義する意図的なプロジェクトローカル上書きであり
   （`.claude/rules/config-loading.md`）、他アクションから再利用された worktree に未追跡のまま
   残っていても正常な状態である。除外は untracked かつこのパスパターンに一致する行のみに限定し、
   tracked ファイルの変更や他の untracked ファイルは従来どおり dirty として扱う（残骸検出という
   本検証の主目的は維持する）。dirty が検出された場合は「未コミットの変更が残っている」
   infrastructure failure として扱い、共有 common dir への書き戻しは行わない。
   **[Fix-15. 2026-07-18 発見・同日修正。Critical]** 当初の実装（Fix-1/Fix-9 導入時点）は
   ここも `git status --porcelain` をそのまま使っており、その staged 列（index vs `HEAD`）が
   comparison 対象を誤って `HEAD` に依存させていた。`_verify_worktree_matches_trusted_tree`
   （4.3.1 手順11・Fix-10 と共通のヘルパー。`packages/loop-harness/lib/loop_git_ephemeral_support.py`）
   は現在 `update-index --refresh` + `diff-files --name-status`（追跡ファイル、index vs worktree
   のみ）と `ls-files --others --exclude-standard`（未追跡ファイル）の組み合わせで判定しており、
   本手順・4.3.1 手順11 のいずれの呼び出しも `HEAD` を一切参照しない。詳細な設計上の理由（単純な
   `git diff --name-status` を採用しなかった理由を含む）は同ヘルパーの docstring を参照。
4. コミットがある場合、共有 common dir への書き戻しを次の手順で行う（`ephemeral_dir` 自体は
   checkout されていないため fetch 可能。宛先を一時 ref にすることで「checkout 済みブランチへの
   fetch 拒否」を回避する）:
   - `import_ref = refs/loop-import/<action_id>`
   - `git -C <common_dir> <hardened args + Fix-5 追加分> fetch <ephemeral_dir> <branch>:<import_ref>`
     （`<import_ref>` はどの worktree にも checkout されていないため拒否されない。
     Fix-5 追加分は fetch の転送元である `<ephemeral_dir>` 側の `uploadpack.packObjectsHook`
     を無効化するために必須。手順0（Fix-7）で `alternates` が既に強制済みのため、この fetch が
     解決できるオブジェクトは `<ephemeral_dir>` 自身のローカルストアと `<common_dir>/objects`
     のみに限定されている）
   - `imported_sha = git -C <common_dir> rev-parse <import_ref>`
     （事前に ephemeral branch から固定した `new_sha` と完全一致することを確認し、不一致は
     `git_ref_import_failed` として CAS へ進まない）
   - `git -C <common_dir> merge-base --is-ancestor <baseline_sha> <imported_sha>`
     （fast-forward であることを明示的に検証。非 ff なら安全停止。`git_ref_not_fast_forward`
     等の新規 stop_reason。9 節）
   - CAS 直前に worktree の `.git` pointer と checkout branch を再検証してから、
     `git -C <common_dir> update-ref refs/heads/<branch> <imported_sha> <baseline_sha>`
     （**`update-ref <ref> <new> <old>` は git 組み込みの原子的 compare-and-swap**。
     `<old>` に渡した `baseline_sha` が現在の ref 値と一致しない場合、git 自身が更新を拒否する
     ため、初版で別コマンドとして行っていた「事前の SHA 比較」は不要になる）
   - `git -C <worktree_path> reset --mixed HEAD`（CAS 前に checkout branch が対象 branch と一致する
     ことを確認済みであり、CAS 後の `HEAD` は新 tip を指す。明示した別 branch ref へ reset すると
     checkout branch の切替レース時に別 branch を動かし得るため、`HEAD` に限定する。worktree 固有の index だけを
     新しい commit tree に合わせる。**working directory のファイルには触れない** —
     Maker が実 worktree 上で直接編集済みのため、reset 前後でファイル内容は変化しない。
     `.git` は 4.3.1/4.3.2 節で ro 保護済みの正規ポインタを指したままなので、この操作は
     汚染された gitdir を踏まない。この `reset` 自体は `<common_dir>` を `--git-dir` として
     解決するため `<ephemeral_dir>` の汚染config は影響しない）
   - `import_ref = refs/loop-import/<action_id>` の削除（`git -C <common_dir> update-ref -d
     <import_ref>`）は、CAS と reset の間の一手順としては行わない。**実装は fetch/CAS/reset を
     含む finalize 本体全体を包み、成功・失敗いずれの経路でも（`finally` 相当のクリーンアップと
     して）finalize の最初と最後の両方で `import_ref` を削除する**（`finalize_ephemeral_git()` の
     wrapper が本体呼び出しの前後で行う）。これにより、fetch 失敗・非 fast-forward・CAS 競合・
     reset 失敗のいずれで finalize が異常終了しても import ref は必ず片付く（9 節の「全経路で
     削除される」保証は、特定ステップ間への配置ではなくこの前後クリーンアップ構造によって
     成立している）。
5. `shutil.rmtree(ephemeral_dir)`。`pinned_git_pointer` の ro bind mount も併せて解除する。

この手順により、共有 common dir への書き込みは**手順 4 の一時 ref 経由 fetch + CAS update-ref +
reset のみ**に限定され、いずれも driver（host、信頼境界内のトラステッドコード）が実行する。
コンテナ内プロセスが共有 ref・config・hooks に触れる経路は存在しない。`<ephemeral_dir>` に触れる
呼び出しは全て trusted config の復元後に Fix-5 の追加 config 上書きを適用するため、Maker が同
ディレクトリの `config` をどう書き換えても host 側でのコード実行には至らない。

#### 4.3.4 Checker 向けの sanitized ephemeral GIT_DIR（ro）

> **[Fix-4] 初版は Checker に共有 common dir 全体を ro マウントする設計だったが、
> ro は改ざんを防ぐだけで、`.git/config` に埋め込まれた認証情報付き URL・他ブランチの ref・
> reflog・hooks の**閲覧**は防げない。Checker も敵対的実行主体である前提（1.1 節）に反するため、
> Maker と同様の sanitized ephemeral GIT_DIR を Checker 専用に生成し ro で渡す。**

1. **[PR #245 レビュー指摘反映]** Checker 用にも 4.3.1 節と**全く同じ手順（1〜10。除外なし）**で
   `ephemeral_dir_checker` を生成する（baseline は Checker 実行時点のブランチ tip）。初版は
   手順6（alternates 設定）を誤って除外していたため、Checker のコンテナが `<common_dir>/objects`
   を後でマウントしても `update-ref`/`read-tree` が baseline のオブジェクトを解決できず
   起動不能になる不備があった。Checker は commit しないため手順5（commit identity の seed）は
   実質使われないが、選択的な除外がまた別の抜け漏れを生む再発防止のため一律で同じ手順を適用する。
2. コンテナには `<worktree_path>` を **ro**（確定方針 6）、`ephemeral_dir_checker` を **ro**、
   `<common_dir>/objects` を **ro** でマウントする。`.git` の ro overlay（4.3.1 節 Fix-3）は
   Checker のコンテナにも同様に適用する（Checker は書き込まないため実害は小さいが、
   4.3.3 節の driver 側事後処理は Checker 実行後にも共通して走るため一貫性のため適用する）。
3. コンテナ破棄後、`ephemeral_dir_checker` は書き戻し不要（Checker は commit しない）のため
   `shutil.rmtree` するのみ。共有 common dir への書き込みは一切発生しない。

この結果、Maker・Checker とも「共有 common dir を直接見せない」という原則を一貫して満たす
（Checker のみ例外扱いにしていた初版の非対称性を解消する）。

### 4.4 「1:1 パスマウント」の採用理由

meta-harness の Docker backend（`scenario_docker_profile.py` の `container_paths=True`）は
`/workspace` 等の固定パスへ**変換**してコンテナへマウントする。loop-harness では代わりに
**host のパスとコンテナ内のパスを同一の絶対パスに揃える**（`docker run -v <path>:<path>`）。

理由:

- `loop_driver.py` の既存コードは `cwd=state.worktree_path` / `add_dirs=[state.worktree_path]` を
  多数の箇所（`_run_maker` / `_run_checker` / `_run_one_llm_reviewer` / `_classify_one_finding`）で
  直接使っており、パス変換を導入すると変更箇所が広範囲になる。
- 4.3.1 の alternates ファイルは単純なテキスト（パス 1 行）であり、host 側で作成する時点で
  コンテナ内から見える最終パスを書き込む必要がある。1:1 パスであればこの変換ロジックが不要になる。
- OrbStack/Docker Desktop のバインドマウントは任意のコンテナ内パスを指定でき、host パスと同一の
  パス文字列を使うこと自体に技術的制約はない（meta-harness が `/workspace` 変換を選んだのは
  scenario worktree のパスが任意でありコンテナ内で安定した規約を持ちたかったためで、loop-harness には
  同じ制約はない）。

### 4.5 既知の制約（受容）

- Maker はコンテナ内で**自ブランチ以外の ref を参照できない**（`main` との diff、他ブランチの
  参照は不可）。既存プロンプト設計（`_maker_prompt`）は Issue 本文・PR レビュー指摘等を
  プロンプトのテキストとして渡す方式であり、ref 参照に依存していないため実害は小さいと判断する。
  将来的に Maker が `main` との diff を必要とする場合は、driver が host 側で計算してプロンプトに
  埋め込む（既存の issue snapshot 埋め込みと同じパターン）。
- Checker も Maker と同様、自ブランチ以外の ref を参照できない（4.3.4 節）。共有 common dir を
  まるごと ro マウントする案は Codex レビューで confidentiality 上の欠陥が指摘されたため不採用と
  した（4.3.4 節）。

---

## 5. イメージライフサイクル（Issue #231 対応）

### 5.1 #231 の根本原因の要約

`packages/meta-harness/lib/scenario_docker_cli.py` の `_ensure_image()` は次の問題を持つ
（Issue #231）。

1. `_BUILT_CONTEXTS` / `_TRUSTED_IMAGE_IDS` は**プロセス内グローバル**であり、CLI 呼び出しの
   たびに空になる。
2. `auto_build` 時は毎回 `docker build --no-cache -t <固定タグ>` を実行し、旧イメージを
   タグ上書きで dangling 化させる。
3. `DOCKER_CONTEXT_HASH_LABEL` はビルド時に書き込むだけで、既存イメージとの照合（再利用判定）に
   使われていない。
4. dangling image・**BuildKit の build cache（イメージ本体とは別に BuildKit が保持するレイヤー
   キャッシュ）**の prune が存在しない。Issue #231 のタイトルが「image / BuildKit cache が肥大化」
   と両方を指している点に注意（イメージタグの GC だけでは根本原因の半分しか解決しない）。
5. 並行ビルド（複数 driver が同時に image が無い状態からスタート）に対する排他制御がない。

### 5.2 修正方針（loop-harness 向けに新設する共有モジュールで解決）

> **2026-07-15 Codex 設計レビュー反映（Critical 2件）**: 初版は (1) content-hash がビルド
> コンテキストのファイルのみを対象とし `--build-arg`/platform/target を含んでいなかったため
> バージョン違いのイメージを誤って同一とみなし得た、(2) 修正方針が `docker image rm` によるタグ
> GC のみで、BuildKit 自体の build cache 肥大化（Issue #231 のタイトルが指す問題の片方）に
> 対処していなかった、という 2 件の Critical 不備があった。以下は指摘を反映した確定方針。

meta-harness と同じ問題を抱えたまま loop-harness 用に再実装すると同じ不具合を複製することに
なるため、**Docker イメージ ensure/prune ロジックを共有モジュールとして切り出し**、
meta-harness・loop-harness の双方が利用する形を設計目標とする（8 節フェーズ0）。

- **content-hash 再利用（build recipe 全体をハッシュ化）**: 既存 `_context_hash()`（ビルド
  コンテキストのファイル内容のみ）は**単独では不十分**であり、**recipe_hash = sha256(context_hash
  + 正規化した `--build-arg` の key=value 一覧をソートしたもの + docker_label + target platform
  + build target)** を計算し、これを**タグ自体に埋め込む**（例:
  `ai-orchestra/loop-harness-scenario:sha-<recipe_hash12>`）。
  `CLAUDE_CODE_VERSION` 等の build arg や `docker_label`、platform/target が変われば別タグになり、
  誤ったバージョンの再利用や label 違いの image の cache hit を防ぐ。同一 recipe_hash のタグが既に存在し `docker image inspect` で実体確認
  できれば **ビルドをスキップして再利用**する。プロセスをまたいだ再利用が成立するよう、判定は
  プロセス内グローバル変数ではなく**ディスク上のマニフェスト**（`.claude/loop/docker-image-cache.json`。
  スキーマ: `{"<recipe_hash>": {"image_id": "sha256:...", "built_at": "...", "last_used_at": "..."}}`）
  に記録する。読み込み時は `docker image inspect` で `image_id` の実在も必ず再検証する
  （マニフェストと Docker 側の状態がずれるケース、例えば手動 `docker image rm` を許容する）。
- **タグ戦略**: 内容不変・ハッシュ付きタグ（`sha-<recipe_hash12>`）を実体とし、人間可読な
  `ai-orchestra/loop-harness-scenario:latest` は**そのハッシュタグへの `docker tag` エイリアス**
  として都度更新する（解決には使わない。あくまで `docker images` で目視しやすくするための補助）。
- **イメージタグの prune ポリシー**: マニフェストの `last_used_at` で世代管理し、**image family
  （scenario/broker）ごとに直近使用された N 世代（既定 3）だけを保持**、それより古く、かつ
  **このマニフェストに記録済みの** ハッシュタグ付きイメージだけを `docker image rm` する。prune は
  `DOCKER_LABEL` でラベル付けされたイメージのみを対象にし（既存の `f"{DOCKER_LABEL}=..."` ラベル
  方式を継続）、開発者が別途手動 build したイメージには影響しないようスコープする。同じ
  repository/label を共有していてもこのマニフェストに記録がないハッシュタグ（例: 別プロジェクトが
  別の `.claude/loop/docker-image-cache.json` で管理しているビルド）は「不明なタグ」として扱い、
  古く見えても削除しない。prune のトリガーは「新規ビルド成功直後」（ビルドの都度、都度
  軽量に掃除する）とし、専用の定期ジョブは新設しない。**prune は best-effort とし**、対象イメージが
  実行中コンテナに使用中などの理由で `docker image rm` が失敗しても warning に留め、直前に成功した
  ビルド全体（`ensure_recipe_image` の戻り値）を失敗にしない。削除できなかった世代はマニフェストに
  残り、次回 prune 実行時に再試行される。
- **BuildKit build cache の GC（イメージタグ GC とは別に必須）**: loop-harness 専用の
  `docker buildx` ビルダーインスタンス（`docker buildx create --name loop-harness-builder`）を
  新設し、**ビルドは常にこの専用ビルダー経由**で行う（開発者のデフォルトビルダーの cache を
  誤って prune しないようスコープを分離するため）。ビルド成功後、`docker buildx prune --builder
  loop-harness-builder --filter until=<image_cache.buildkit_cache_max_age>`（既定 168h = 7日）を
  実行し、経過時間ベースで build cache を GC する。加えて `image_cache.buildkit_cache_max_size`
  （既定 10g）を安全弁として設定し、`docker buildx du --builder loop-harness-builder` で使用量が
  上限を超えていれば `until=0`（無条件 prune）にフォールバックする。**専用ビルダーの初回作成は
  プロジェクト間でレースしうる**（異なる `.claude/loop/docker-image-build.lock` を持つ複数プロジェクトが
  同じグローバルビルダー名を同時に初期化しようとするケース）ため、`docker buildx create` が失敗しても
  即座に致命エラーとせず、`docker buildx inspect` を再試行して driver 検証（`docker-container`）を
  通れば既存ビルダーとして採用する。
- **並行ビルドのロック**: 「マニフェスト確認 → 必要ならビルド → マニフェスト更新」の区間全体を
  `.claude/loop/docker-image-build.lock`（`flock`。`loop_common.held_coord_lock` と同じ
  ファイルロックパターンを踏襲）で保護し、`lp2.concurrency_limit`（既定 2）で複数 driver が
  同時に起動しても、同一イメージの重複ビルドが発生しないようにする。ロック保持区間はビルド
  そのものに限定し（run 全体をロックしない）、ビルド不要な場合（キャッシュヒット）はロック取得後
  即座に解放される。

### 5.3 meta-harness への波及（本 PR のスコープ外・フォローアップ）

上記の共有モジュール抽出が実現すれば、meta-harness 側の `scenario_docker_cli.py` も同じ修正の
恩恵を受けられる。本設計書は loop-harness の隔離設計を確定するものであり、meta-harness 側の
移行は**別 Issue（フォローアップ）として提案する**（本 ADR・本設計のスコープには含めない。
10 節「オープン事項」参照）。

---

## 6. broker 流用範囲と loop-harness 固有の差分

meta-harness の `packages/meta-harness/lib/scenario_docker.py` / `scenario_docker_profile.py` と、
`packages/docker-runtime/docker/broker/broker.py`（ADR-20260712-034 で確立した dual-homed sidecar
broker。meta-harness の旧パスは互換 shim）を共有して流用する。

| 項目                             | meta-harness                                        | loop-harness（差分）                                                    |
| -------------------------------- | ----------------------------------------------------- | --------------------------------------------------------------------- |
| broker 配置                      | internal + external dual-homed sidecar                | 同一（変更なし）                                                       |
| broker の資格情報                | ホストの Claude OAuth credential を都度ロード          | 同一（変更なし）                                                       |
| token 注入                       | tmpfs 注入 + 読了後 unlink                             | 同一（変更なし）                                                       |
| judge (`claude --bare`)          | broker 経由                                            | loop-harness には judge 概念がない（Checker の LLM レビューは通常の `claude -p`。broker 経由の認証代行は同様に適用） |
| ネットワーク命名                 | `NAME_PREFIX = "mh-"` / `DOCKER_LABEL = "ai.orchestra.meta-harness"` | `NAME_PREFIX = "lh-"` / `DOCKER_LABEL = "ai.orchestra.loop-harness"`（名前空間衝突回避のため独立） |
| worktree/git マウント            | 使い捨て bare snapshot（discard 前提）                  | ephemeral GIT_DIR + alternates + driver 側 CAS 書き戻し（4 節。commit を永続化する必要があるため） |
| 資格情報の起動元プロセス         | `orchex meta loop` / `evaluate`（CLI サブコマンドの子） | `loop_driver.py`（LP-2 worker。1 loop run = 1 プロセスの子として起動）  |

**共有モジュール抽出の設計方針**（8 節フェーズ0で具体化）: broker 起動・コンテナライフサイクル・
セキュリティプロファイルの生成ロジックは、`worktree` と `git` の扱いを除いてほぼ共通であるため、
`packages/` 配下に共有ライブラリ（案: `packages/docker-runtime`）を新設し、meta-harness・
loop-harness の双方が薄いラッパーとして利用する構成を目標とする。共通化の詳細な API 設計・
パッケージ分割は実装フェーズで確定する（本書は「どの部分が共通化可能か」の分析までを示す）。
Phase 0 実装で `lib/` 配下（lifecycle/profile/cli builder）を namespace 注入型で共有化し、Phase 1-a
で broker 本体も `DR_BROKER_*` / `DR_PRICE_*` 優先・旧 `MH_*` fallback の共通契約へ移行した。
`DR_BROKER_NAMESPACE` を明示した場合だけ `<namespace>-broker` と
`ai-orchestra-<namespace>-broker/0.1` を導出し、未指定時は meta-harness の既存 identity を維持する。

---

## 7. config スキーマ

`.claude/config/loop-harness/loop-harness.yaml` の `lp2` セクションに `isolation` を追加する
（既定は非隔離を維持し、後方互換を壊さない。確定方針 7）。

```yaml
lp2:
  concurrency_limit: 2
  wall_clock_timeout_seconds: 7200
  priority_labels: []
  isolation:
    backend: none # none | docker（#211）。none = 既存の層1〜4深層防御のみで実行（現行動作を維持）
    execution_backend: none # docker 有効化は封じ込め検証テストの整備後（ADR-20260712-034 と同じ fail-closed 原則）
    image: ai-orchestra/loop-harness-scenario:<pin> # sha-<recipe_hash12> タグへの解決は 5.2 節参照
    image_pin: null # null = Claude CLI バージョン一致検証をスキップ
    auto_build_images: true # false の場合 image は @sha256:<digest> 形式必須（タグ形式は DockerImageError）
    image_cache:
      manifest_path: .claude/loop/docker-image-cache.json # メインルート相対
      keep_generations: 3 # image family ごとの保持世代数（5.2 節）
      lock_path: .claude/loop/docker-image-build.lock
      builder_name: loop-harness-builder # 専用 buildx ビルダー（5.2 節。開発者既定ビルダーの cache と分離）
      buildkit_cache_max_age: 168h # BuildKit build cache の GC 閾値（5.2 節）
      buildkit_cache_max_size: 10g # 安全弁。超過時は age を無視し無条件 prune
    resources:
      pids_limit: 64
      memory: 1g
      cpus: 1.0
    checker:
      read_only_worktree: true # 確定方針 6。false 化は将来のオプトアウト用に残すが既定は true 固定を推奨
    broker:
      image: ai-orchestra/loop-harness-broker:<pin>
      port_range: [8790, 8990]
      idle_timeout_sec: 300
      startup_timeout_sec: 30
```

- `isolation.backend`/`execution_backend` を分ける理由は ADR-20260712-034 と同じ
  （「名前を設定しただけでは利用可能扱いにしない」。封じ込め検証テストが揃うまで
  `execution_backend` は `none` に固定する）。
- `Docker daemon 不在・イメージ pin 不一致・broker 起動失敗は非隔離実行へ降格せず run error とする`
  という ADR-20260712-034 の fail-closed 原則を、`backend: docker` 有効時にも同様に適用する
  （静かなフォールバックによる隔離境界の無効化を防ぐ）。
- `.claude/config/loop-harness/loop-harness.local.yaml` によるプロジェクト固有上書きは
  `config-loading.md` の既存レイヤードルールにそのまま従う。

---

## 8. 段階導入計画

既存の層2〜4 深層防御（`design:loop-harness-cli` §2.2）は**そのまま残し**、Docker 隔離は
config で切替可能な追加バックエンドとして導入する（確定方針 7。非隔離運用との後方互換維持）。

| フェーズ | 内容                                                                                                       | ゲート                                     |
| -------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------- |
| Phase 0  | meta-harness の Docker/broker ロジックから共有モジュール（6 節）を抽出。振る舞い変更なしのリファクタとして meta-harness 側の既存テストで回帰確認 | 既存 meta-harness テストが green のまま        |
| Phase 1  | loop-harness 用 scenario イメージ新設（loop-harness 専用 Dockerfile）、image ensure/prune/lock（5 節）の実装 | イメージ再利用・prune の単体テスト整備          |
| Phase 2  | ephemeral GIT_DIR + alternates + driver 側 CAS 書き戻し（4 節）の実装。Maker 専用                              | 単体テスト + 実機での commit 往復検証           |
| Phase 3  | Checker 専用 sanitized ephemeral GIT_DIR（ro）の実装（4.3.4 節。書き戻し不要なぶん Phase 2 より単純） | 単体テスト                                     |
| Phase 4  | `loop_driver.py` への配線。**実行可否の分岐は必ず `isolation.execution_backend: docker` で行う**（`backend` はイメージ・実装ロジックの選択のみに使い、単独では実行を許可しない。7 節の fail-closed 原則）。封じ込め検証テスト（cgroup 回収・network 遮断。meta-harness スパイク S3 相当）を整備 | 封じ込め検証テストが揃うまで `execution_backend` の既定値は `none` のまま。PASS 後に初めて `execution_backend: docker` を有効化可能にする |
| Phase 5  | 受容リスクの再評価・ドキュメント更新（`design:loop-harness-cli` §2.2 の残余リスク欄を本設計の内容で更新）        | 人間レビュー                                   |

各フェーズは独立した PR とし、Phase 4 完了までは `execution_backend` の既定値を `none` に保つ。

---

## 9. 受容リスクの更新

### 9.1 解消されるリスク（Docker 隔離導入後）

- `.claude/loop/<loop_id>/` state/journal への同一 UID 経由の直接改ざん（1.3 節）
- `.git/config`/`hooks/`/reflog/他ブランチ ref への直接書き換え、および**閲覧**（1.3 節・4.3.4 節。
  Checker も sanitized ephemeral GIT_DIR 経由にしたことで、改ざんだけでなく情報漏洩経路も塞ぐ）
- worktree の `.git` ポインタ改ざんによる host 側 git 操作の乗っ取り（4.3.1/4.3.2 節 Fix-3）
- 他 issue ブランチ・`main` ブランチの ref への書き換え（4 節。CAS `update-ref` により原子的に保護）
- Issue #231（イメージタグの肥大化・BuildKit build cache 肥大化・並行ビルド未保護）相当の
  運用不具合（5 節。content-hash に build recipe 全体を含めたことでバージョン誤再利用も解消）

### 9.2 引き続き残るリスク

- `mechanical.commands` denylist の残バイパス（`design:loop-harness-cli` §2.2 SN3-accept）は
  受容方針を変更しない。コンテナ化により被害範囲（ファイルシステム境界・egress）は縮小するが、
  denylist 自体の性質（信頼された作者向け best-effort チェック）は変わらない。
- broker サイドカーが乗っ取られた場合のリスクは ADR-20260712-034 と同一の受容基準を継続する。
- Maker/Checker がコンテナ内で自ブランチ以外の ref を参照できない制約（4.5 節）はトレードオフ
  として受容する。
- Docker daemon 自体の脆弱性・ホスト側 Docker Desktop/OrbStack の実装依存のリスクは
  ADR-20260712-034 のスコープと同じく対象外とする。
- 4.3.2 節 Fix-3 の `.git` ro overlay は「host 側の事後処理が汚染された gitdir を踏まない」ことを
  保証するものであり、Maker 自身が `.git` 経由でコンテナ内から `common_dir/worktrees/<name>` へ
  到達すること自体は、当該パスをそもそもマウントしていないことで防いでいる（二重の防御）。
- ephemeral fetch（4.3.3 手順4）が転送するオブジェクトのサイズ・件数には上限がない。Maker が
  worktree 上で正当に生成した巨大ファイルをそのまま commit した場合、host 権限で共有
  `common_dir/objects` へ書き込まれディスク枯渇 DoS を招き得る（Fix-7 が閉じた alternates 経由の
  confused deputy とは別の量的リスク）。Phase 2 では対応せず、Issue #255 でフォローアップする。

---

## 10. オープン事項（実装フェーズで確定）

- 共有モジュール（`packages/docker-runtime` 案。6 節）の具体的な API 境界とパッケージ分割方法
- meta-harness 側 `scenario_docker_cli.py` へのイメージライフサイクル修正（5.3 節）のフォローアップ Issue 起票
- loop-harness 専用 Dockerfile の具体的なベースイメージ選定（Claude CLI バージョン pin の運用は
  meta-harness の `image_pin` 方式を踏襲する想定）
- ~~ephemeral GIT_DIR の一時 ref fetch・`merge-base --is-ancestor` 失敗・CAS `update-ref` 失敗時
  （4.3.3 手順4）の安全停止シーケンスの詳細~~ → Phase 2（Issue #211）で確定済み:
  `git_ref_import_failed`（fetch/import ref 不一致） /
  `git_ref_not_fast_forward`（`merge-base --is-ancestor` exit 1） /
  `git_ref_cas_rejected`（CAS 競合）の 3 `stop_reason` として実装され、`EphemeralGitSafetyStop`
  経由で `loop_git_ephemeral.py` の各経路に対応するテストがある（`docs/evaluation/loop-harness.md`
  EV-118）。
- 封じ込め検証テスト（cgroup 回収・network 遮断）の具体的な自動テスト形式（meta-harness の
  スパイク手順を自動テスト化する方式を想定。`docs/design/meta-harness-scenario-backend-spikes.md`
  を参考にする）
- Docker Desktop/OrbStack のバインドマウントで、ファイル単位の ro overlay マウント（4.3.1 節
  Fix-3 の `.git` ポインタ保護）が意図どおり機能することの実機検証（ディレクトリ単位のマウントは
  meta-harness で実証済みだが、単一ファイルへの overlay は本設計で新規に導入するため個別に確認する）
- ~~[未修正。2026-07-18 発見（PR #256 レビュー3巡目の回帰テスト作成中に判明。Critical 疑い）]
  `_verify_worktree_matches_trusted_tree` の `git status --porcelain` staged 列（index vs
  `HEAD`）が primary worktree の `HEAD` に誤って反応し、`main` が独立して進むだけで
  `prepare_ephemeral_git` が常に fail-closed する不具合~~ → **Fix-15（2026-07-18）で解消済み**:
  `git status --porcelain` を `update-index --refresh` + `diff-files --name-status`（追跡ファイル、
  index vs worktree のみで `HEAD` を一切参照しない）と `ls-files --others --exclude-standard`
  （未追跡ファイル）の組み合わせへ置き換えた。単純な `git diff --name-status`（no-revision）も
  検討したが、index が記録する OID の実オブジェクトを読みに行くため、Fix-7 の alternates 復元後に
  意図的に解決不能な blob を参照させる改ざんシナリオ（`test_finalize_neutralizes_alternates_confused_deputy_object_smuggling`）で
  誤って早期に infrastructure error を投げてしまい、本来期待される finalize の fetch 段階での
  `git_ref_import_failed` 安全停止に到達できなくなるため不採用とした。`update-index --refresh` は
  worktree ファイルの実バイト列からハッシュを再計算し index 記載の OID と文字列比較するだけで、
  index 側 OID の実オブジェクトを読みに行かないため、この経路を壊さずに `HEAD` 依存だけを除去できる。
  回帰テストは `test_prepare_accepts_clean_worktree_when_primary_worktree_head_has_diverged` /
  `test_prepare_commit_finalize_succeeds_when_primary_worktree_head_diverges_mid_action`
  （`packages/loop-harness/tests/test_loop_git_ephemeral.py`）。詳細は `_verify_worktree_matches_trusted_tree`
  の docstring（`packages/loop-harness/lib/loop_git_ephemeral_support.py`）を参照。
