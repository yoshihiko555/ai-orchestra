# docker-runtime 評価セット

**パッケージ**: `packages/docker-runtime`
**類型**: CLI ツール型（内部ライブラリ）
**作成日**: 2026-07-15
**最終レビュー日**: 未レビュー
**情報源**: `docs/design/loop-harness-isolation.md` §5.2・§5.4・§6・§8、`docs/adr/ADR-20260712-035.md`、`docs/adr/ADR-20260715-039.md`、`docs/adr/ADR-20260720-044.md`、`docs/adr/ADR-20260726-045.md`

## 1. 責務定義

`docker-runtime` は複数の harness が共有する Docker CLI、イメージライフサイクル、セキュリティ
プロファイル、dual-homed broker、所有 resource の cleanup を提供する。呼び出し元固有の
namespace・image・config を引数として受け取り、meta-harness と loop-harness の resource が
交差しない状態で fail-closed な実行境界を構築する。

### Non-Goals

- harness 固有の worktree、git metadata、成果物、設定スキーマは扱わない。
- daemon 全体で共有される cleanup 調停機構（checkout/worktree 横断で pending journal・pin
  lease ledger を可視化する仕組み）は提供しない。meta-harness/loop-harness は既に
  `ensure_recipe_image()` による recipe-addressed の永続ライフサイクル（EV-13〜EV-18・
  EV-32〜EV-37）へ移行済みであり、本項目は「その先の daemon-global 化」をスコープ外とする趣旨
  である（`docs/adr/ADR-20260726-045.md` 既知の制限を参照。旧稿の本項目は「process-local image
  ensure を永続ライフサイクルへ移行しない」という、この評価セット自体の内容と矛盾する記述に
  なっていたため訂正した）。

## 2. 期待する入出力・副作用

| 構成要素 | 入力 | 期待する出力 | 副作用 |
| --- | --- | --- | --- |
| `docker_runtime_cli.py` | image、build context、build args、runner | immutable image ID または明示的エラー | Docker image inspect/build/remove |
| `docker_runtime_image.py` | image recipe、manifest/lock path、GC policy、runner | recipe 固有の immutable image ID と tag | manifest 更新、buildx build、image/BuildKit prune |
| `docker_runtime_profile.py` | mount/resource/env 値 | hardened Docker CLI 引数 | なし |
| `docker_runtime_lifecycle.py` | namespace、broker spec、runner/callback | broker session または明示的エラー | container/network の作成・破棄 |
| `docker/broker/broker.py` | `DR_BROKER_*` / `DR_PRICE_*`、任意の broker namespace | namespace 固有の HTTP identity と upstream user-agent。新変数未設定時は `MH_*` fallback | OAuth proxy、run budget/metrics |

## 3. 評価観点

- [ ] EV-01（正常 / must）: 同一 image・context hash・build args の process-local build は 1 回だけ実行し、2 回目は再利用する — 根拠: Phase 0 の既存 meta-harness 挙動
- [ ] EV-02（異常 / must）: auto-build 無効時は `@sha256:<digest>` 形式でない mutable image を拒否する — 根拠: ADR-20260712-035
- [ ] EV-03（異常 / must）: container/network の削除失敗は明示的な missing-object 応答だけを「既に削除済み」として成功扱いし、permission error 等を成功扱いしない — 根拠: ADR-20260712-035 受容基準
- [ ] EV-04（正常 / must）[2026-09-07 改訂（Issue #409、`docker_runtime_profile.tmpfs()` に `exec_ok` パラメータ追加）]: profile builder は read-only rootfs、cap drop、no-new-privileges、non-root、resource limit、tmpfs を構成できる。tmpfs は既定で `noexec`（`exec_ok` 省略時、既存呼び出しは無変更）だが、呼び出し側が明示的に要求した場合のみ `noexec` を外した exec 可能な tmpfs（`nosuid`/`nodev`/固定 non-root uid・gid/`mode=0700` は同一のまま）も構成できる — 根拠: `docs/design/loop-harness-isolation.md` §3, `packages/docker-runtime/lib/docker_runtime_profile.py`（`tmpfs`）, loop-harness EV-161
- [ ] EV-05（異常 / must）: comma を含む bind source 等、安全に Docker CLI へ表現できない mount は拒否する — 根拠: meta-harness 既存実装挙動
- [ ] EV-06（正常 / must）: broker は internal/external network の dual-homed sidecar として起動し、scenario 側の direct egress や Docker socket mount を追加しない — 根拠: `docs/design/loop-harness-isolation.md` §2・§3
- [ ] EV-07（異常 / must）: broker 起動の途中失敗では作成済み container/network を逆順に cleanup し、非隔離実行へ降格しない — 根拠: `docs/design/loop-harness-isolation.md` §2.1・§7
- [ ] EV-08（境界 / must）: harness ごとの `DOCKER_LABEL` から owner/parent/created labels を導出し、meta-harness と loop-harness の cleanup namespace を分離する — 根拠: `docs/design/loop-harness-isolation.md` §6
- [ ] EV-13（正常 / must）: `recipe_hash` は context hash、key 順に正規化した build args、`docker_label`、platform、build target の全てから決まり、いずれかが異なれば別 hash/tag になる — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-14（正常 / must）: 同一 recipe の manifest entry と Docker image ID が一致する場合、別プロセス相当の新しい runtime instance でも build を省略し、`last_used_at` と `latest` alias を更新して再利用する — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-15（異常 / must）: manifest entry が存在しても image が削除済み、または inspect した image ID が記録値と異なる場合は cache hit とせず、安全に再 build する — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-16（境界 / must）[2026-07-26 pin-lease 除外の一時的超過を明記（PR #320 レビュー第5弾）]: image prune は family ごとの `last_used_at` 上位 N 世代を保持し、**このマニフェストに記録済みの** hash tag のうち古いものだけを削除する。label 不一致・別 family・`latest` alias・このマニフェストに記録されていない hash tag（例: 同じ repository/label を共有する別プロジェクトのビルド）は削除しない。個別の `docker image rm` が使用中コンテナ等の理由で失敗しても best-effort の warning に留め、直前に成功したビルド（`ensure_recipe_image` の戻り値）を失敗にしない。加えて、`keep_generations` の保持数超過分であっても、その世代の `image_id` に有効な pin lease（`IMAGE_ID_LEASE_TTL_SECONDS`、既定 6h）がある間は prune 対象から除外される（`test_prune_skips_generation_with_active_pin_lease`）ため、`keep_generations` は**恒久的な上限ではなく**、lease TTL の窓（最大 6h）の間は一時的に超過し得る。lease が期限切れになれば次回以降の prune で自然に解消する（自己回復、追加の対応は不要） — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-17（境界 / must）: 同一 `recipe.family` の build は専用の per-family lock（`policy.lock_path` から派生）で直列化され、先行プロセスが build 済みにした同一 recipe を後続プロセスが重複 build しない。manifest 自体の読み書き（cache-hit 確認・build 完了後の記録）はベースの `policy.lock_path` を短時間だけ保持して整合を守る（Issue #250 Fix B） — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-18（正常 / must）: build は呼び出し元専用の buildx builder のみを使用し、成功後に同 builder へ age-based prune を実行する。使用量が上限を超える場合は同 builder に限って `until=0` prune へフォールバックする。同名の builder/context が `docker-container` 以外の driver で既存の場合は無条件に再利用せず拒否する。`docker buildx create` が他プロジェクトとのレースで失敗した場合は `docker buildx inspect` を再試行し、既存ビルダーが driver 検証を通れば採用する（driver 不一致ならこのケースでも拒否する） — 根拠: `docs/design/loop-harness-isolation.md` §5.2
- [ ] EV-19（境界 / must）: manifest の個別 entry が不正でも、その record だけを cache miss として除外し、同じ manifest 内の検証済み entry は引き続き再利用する。`built_at`/`last_used_at` が timezone-aware ISO 形式で parse できない場合も不正 record として扱う（`last_used_at` をテキストソートする prune が壊れた値を「最新」と誤認するのを防ぐため）— 根拠: `packages/docker-runtime/lib/docker_runtime_image.py`（`_load_valid_manifest`）
- [ ] EV-20（境界 / must）: `exclusive_file_lock` は lock 取得時の `OSError` だけを lock error に変換し、critical section または unlock で発生した `OSError` は元の例外のまま伝播する — 根拠: `packages/docker-runtime/lib/docker_runtime_image.py`（`exclusive_file_lock`）
- [ ] EV-21（正常 / must）: broker は全設定について `DR_BROKER_*` / `DR_PRICE_*` を優先して読み、未設定の項目だけ同名 suffix の `MH_BROKER_*` / `MH_PRICE_*` へ fallback する。これにより新しい呼び出し元は harness 非依存の契約を使い、digest pin 済み旧 broker image を使う meta-harness は従来契約を継続できる。既存設定は新旧どちらの env 名も未設定の場合に両変数名を含む `KeyError` で起動失敗する（fail-loud）。後方互換のため追加する `*_BROKER_INPUT_BYTES_PER_TOKEN` だけは両名未設定時に後方互換の既定値 1（従来の 1:1 byte 換算と同値）を使い、設定時は同じ優先順位で解決し、不正値を fail-loud で拒否する — 根拠: `docs/design/loop-harness-isolation.md` §6、ADR-20260715-039、Issue #356
- [ ] EV-22（境界 / must）: `DR_BROKER_NAMESPACE` が明示された場合だけ `server_version=<namespace>-broker` と user-agent `ai-orchestra-<namespace>-broker/0.1` を導出し、未指定時は既存の `meta-harness-broker` / `ai-orchestra-meta-harness-broker/0.1` を維持する。不正な namespace は fail-closed で拒否する — 根拠: `docs/design/loop-harness-isolation.md` §6、ADR-20260715-039
- [ ] EV-23（境界 / must）: broker の `budget_rejected_count` は token/cost upper bound による 429 の前課金拒否だけで増加し、成功、token 不一致、query/model/header/body 入力不正では増加しない — 根拠: ADR-20260720-044
- [ ] EV-24（境界 / must）[2026-07-22 追加（Issue #301）]: `align_mount_ownership()` は `_is_owner_only_permission()`（`st_mode & 0o077 == 0`）が真のパスを chown 対象から常にスキップする。root 実行時にこの再 own が働くのは、人間が意図的に group/other 権限ビットを一切持たない restrictive mode（例: `.env`/`.netrc` を `0600`）で残した secret を、固定 non-root コンテナ identity へ新たにアクセス可能にしないためであり、通常の worktree ファイル（`0644`/`0755` 等）は従来通り re-own される。restrictive mode のディレクトリ自体も chown 対象からスキップされるが、`rglob()` はそのディレクトリの走査を止めない（配下エントリは個別に owner-only 判定される） — 根拠: `packages/docker-runtime/lib/docker_runtime_profile.py`（`align_mount_ownership`/`_is_owner_only_permission`）, 対応テスト: `test_align_mount_ownership_skips_owner_only_permission_secrets`, `test_align_mount_ownership_reowns_owner_only_permission_directory_ancestor`
- [ ] EV-25（異常 / must）[2026-07-22 追加（Issue #301）]: 非 root ホスト実行時、`align_mount_ownership()`／独立エントリポイントの `reject_owner_only_secrets()` は owner-only permission なパスが 1 件でも存在すれば `DockerProfileError` で fail-closed に拒否する。root 実行時の chown スキップ（EV-24）と異なり、非 root ホストでは `non_root_identity()` がコンテナを同一 host uid/gid へマップするため、chown の有無に関わらず secret がそのまま Maker/Checker から読めてしまい、ownership 変更では解決できないため — 根拠: `packages/docker-runtime/lib/docker_runtime_profile.py`（`align_mount_ownership`/`reject_owner_only_secrets`）, 対応テスト: `test_align_mount_ownership_rejects_owner_only_secret_for_non_root_host`, `test_reject_owner_only_secrets_rejects_for_non_root_host`, `test_reject_owner_only_secrets_is_noop_for_root_host`
- [ ] EV-26（境界 / must）[2026-07-22 追加（Issue #301）]: `exclude` に指定されたパスは owner-only 判定に関わらず chown 対象・非 root reject 判定の両方から除外される。この除外は `Path` の一致だけでなく、`_stat_identity()` によるハードリンクエイリアス（同一 inode）にも及ぶため、`exclude` に列挙した除外ファイルへの別名パスを通じて非対称に re-own・拒否判定が漏れることはない — 根拠: `packages/docker-runtime/lib/docker_runtime_profile.py`（`align_mount_ownership`/`_reject_owner_only_secrets`/`_stat_identity`）, 対応テスト: `test_align_mount_ownership_skips_excluded_leaf_paths`, `test_align_mount_ownership_skips_hardlink_alias_of_an_excluded_file`, `test_reject_owner_only_secrets_honors_exclude_for_non_root_host`
- [ ] EV-27（境界 / must）[2026-07-22 追加（Issue #301）]: `protect_owner_only=False` を渡した呼び出しは owner-only permission による保護（EV-24 の chown スキップ、EV-25 の非 root fail-closed 拒否）の両方をバイパスする。これは `loop_docker_action.py` が driver 生成の ephemeral Git ランタイムディレクトリに対して使う脱出口であり、そこに現れる restrictive mode はプロセス umask 由来であって人間が意図的に置いた secret ではないため、非 root ホストでも起動を阻害しない。ユーザーの worktree 本体には既定値（`protect_owner_only=True`）のまま保護を適用し続ける — 根拠: `packages/docker-runtime/lib/docker_runtime_profile.py`（`align_mount_ownership` の `protect_owner_only` 引数）, `packages/loop-harness/lib/loop_docker_action.py`（ephemeral Git ランタイムディレクトリの `protect_owner_only=False` 呼び出し）, 対応テスト: `test_align_mount_ownership_protect_owner_only_false_bypasses_reject_for_non_root_host`
- [ ] EV-28（境界 / must）[2026-07-22 追加（Issue #250）]: `recipe.family` ごとに独立した build lock（`policy.lock_path` から派生、family 名は builder 名と同じ charset でサニタイズ）を使うため、同一 `policy`（manifest/lock を共有する namespace、例: meta-harness の scenario と broker）でも異なる family の build は互いを待たない。不正な family 名（lock ファイル名として安全でない文字を含む）は fail-closed で拒否する — 根拠: Issue #250 review（lock 直列化解消）
- [ ] EV-29（異常 / must）[2026-07-22 追加（Issue #250）]: manifest への書き込み（build 完了後の記録）は、build 開始前に読み込んだ古いスナップショットではなく、書き込み直前にディスクから再読込した内容へ自 family のエントリをマージしてから行う。これにより、別 family が build 中に書き込んだ manifest エントリを、後から書き込む family が消失させない（lost update 防止） — 根拠: Issue #250 review（lock 直列化解消）
- [ ] EV-30（境界 / must）[2026-07-22 追加（Issue #250）]: manifest 読み込み時の `docker image inspect` による実在検証は、今回 `ensure_recipe_image` が要求している digest のエントリ 1 件だけに限定する。他のスキーマ上有効なエントリは inspect せずそのまま保持する（EV-15 の drift 検出は要求 digest について維持される） — 根拠: Issue #250 review（cache hit 時の全件 inspect 解消）
- [ ] EV-31（異常 / must）[2026-07-22 追加（Issue #250）]: `exclusive_file_lock` はロック対象パスがシンボリックリンクに差し替えられている場合（TOCTOU）、`O_NOFOLLOW` により追跡せず fail-closed でエラーにする。シンボリックリンク先のファイルを誤って `chmod`/ロックしない — 根拠: Issue #250 review（flock パスの TOCTOU 対策）
- [ ] EV-32（異常 / must）[2026-07-26 追加（Issue #231）、2026-07-26 sidecar namespace 修正（PR #320 レビュー第3弾）]: `ensure_recipe_image` は build 開始前（per-family build lock 保持下）に、manifest とは別ファイルの pending journal（`<manifest_path>.sidecars/pending.json`。別 policy の manifest 名との衝突を避けるための専用サブディレクトリ）へタグを in-flight として記録し、build 成功・manifest 書き込み直後に消し込む。`--load` 成功後・manifest 書き込み前にプロセスがクラッシュして journal にだけ記録が残った場合、次回以降の `ensure_recipe_image` 呼び出し（キャッシュヒットの再利用パスを含む）が opportunistic cleanup でそのタグを回収する — 根拠: `packages/docker-runtime/lib/docker_runtime_image.py`（`_record_pending_build`/`_clear_pending_entry`/`_cleanup_stale_owned_images`）, 対応テスト: `test_cleanup_reclaims_orphaned_tag_left_by_a_pending_build`
- [ ] EV-33（正常 / must）[2026-07-26 追加（Issue #231）、2026-07-26 pin-lease 契約修正（PR #320 レビュー第4弾）]: opportunistic cleanup は owner label 付きの dangling（`<none>:<none>`）image を、pending journal の記録有無に関わらず、かつ有効な pin lease（`IMAGE_ID_LEASE_TTL_SECONDS` 以内に `ensure_recipe_image` が返した image_id の予約）を持つものを除いて回収する。同一タグへの再ビルドでタグを失った旧世代 image は `_prune_image_family` のタグ限定走査では検出されないため、この経路で GC される — 根拠: `docker_runtime_image._dangling_image_ids`/`_cleanup_stale_owned_images`/`_purge_expired_pin_leases`, 対応テスト: `test_cleanup_removes_dangling_owner_labelled_image`, `test_cleanup_protects_dangling_image_with_active_pin_lease`, `test_cleanup_removes_dangling_image_after_pin_lease_expires`, `test_cleanup_re_reads_pin_ledger_immediately_before_each_dangling_removal`
- [ ] EV-34（境界 / must）[2026-07-26 追加（Issue #231）]: opportunistic cleanup は、自 label が付いていない image、または label は一致するが pending journal に記録のないタグ付き image（他プロジェクトが同じ repository/label を共有してビルドした image 等）を削除対象にしない。所有証明は「pending journal の記録」と「owner label」の両方に基づく — 根拠: `docker_runtime_image._cleanup_stale_owned_images`/`_partition_pending`, 対応テスト: `test_cleanup_leaves_other_projects_tagged_image_alone`
- [ ] EV-35（異常 / must）[2026-07-26 追加（Issue #231）、2026-07-26 スコープ・自己修復性の明記（PR #320 レビュー第4弾）]: build 失敗時、`try`/`except` は `_build_image` の呼び出しから `_tag_latest` の呼び出しまで（manifest 書き込みの前）をカバーしており、この区間で例外が送出された場合に限り `ensure_recipe_image` は pending journal のエントリと当該タグを best-effort で回収してから元の例外をそのまま re-raise する。cleanup 自体の失敗は元の build 失敗のエラーを上書きしない。manifest 書き込み以降（`_prune_image_family`・`_write_manifest`・`_clear_pending_entry` 等）の失敗はこの try/except の対象外であり明示的な cleanup は行わないが、pending journal のエントリは既に消し込まれていない状態のまま残るため、次回以降の `ensure_recipe_image` 呼び出しの opportunistic cleanup（EV-32）が同じ残留エントリを回収する形で自己修復する — 根拠: `docker_runtime_image._best_effort_remove_pending_tag`, 対応テスト: `test_build_failure_triggers_best_effort_pending_tag_cleanup`
- [ ] EV-36（境界 / must）[2026-07-26 追加（Issue #231）、2026-07-26 契約修正（PR #320 レビュー指摘）、2026-07-26 補足追記（PR #320 レビュー第4弾）]: opportunistic cleanup の `docker image ls`/`buildx du` は、`_partition_pending` が判定した **stale 候補**（family lock 未保持・grace 期間超過の両方を満たし、in-flight build の可能性を排除できたエントリ）が label スコープの pending journal に 1 件も無く、かつ `CLEANUP_TTL_SECONDS`（既定 6h）が未経過の場合にのみ抑制される。stale 候補が 1 件でもあれば TTL を無視して必ず実行する。pending journal に記録があっても、それが live（in-flight build の可能性を排除できない）エントリしかない場合は stale 候補ゼロ扱いとなり、TTL に従い抑制される。注記: この TTL 抑制の対象は `docker image ls`/`buildx du` のみであり、`_partition_pending` が候補を判定するために `_pending_entry_resolution` 経由で行う `docker image inspect`（manifest 記録との一致確認）自体は TTL の抑制対象ではなく、label スコープの pending journal にエントリが 1 件でもあれば毎回実行される — 根拠: `docker_runtime_image._cleanup_due`/`_partition_pending`/`_cleanup_stale_owned_images`, 対応テスト: `test_cleanup_is_suppressed_within_ttl_when_nothing_pending`（pending 自体が無い場合）, `test_cleanup_issues_no_docker_commands_when_pending_entry_is_live_within_ttl`（live pending のみの場合も抑制される）
- [ ] EV-37（異常 / must）[2026-07-26 追加（Issue #231）]: opportunistic cleanup 内の `docker image rm` 失敗は warning に留めて best-effort で次回呼び出しに持ち越し、`ensure_recipe_image` 呼び出し全体を失敗させない — 根拠: `docker_runtime_image._remove_image_best_effort`, 対応テスト: `test_cleanup_rm_failure_does_not_fail_ensure_recipe_image`

## 4. 類型別観点

- [ ] EV-09（正常 / must）: 後方互換性 — meta-harness の既存公開関数・例外・Docker command profile と既存テストを変更せず維持する — 根拠: `docs/design/loop-harness-isolation.md` §8 Phase 0
- [ ] EV-10（正常 / must）: 配布ライフサイクル — `meta-harness` manifest が `docker-runtime` へ依存し、package install 時に共有実装が欠落しない — 根拠: Phase 0 package 分割
- [ ] EV-11（正常 / must）: `sweep_stale_resources` は owner label が一致し、かつ stale 判定（`container_is_stale`/`network_is_stale`）が true のコンテナ・ネットワークのみ削除し、稼働中で owner が一致し stale でないリソースは削除しない — 根拠: `packages/docker-runtime/lib/docker_runtime_lifecycle.py`（`sweep_stale_resources`／`container_is_stale`／`network_is_stale`）
- [ ] EV-12（境界 / must）: `container_is_stale`/`network_is_stale` は owner label が呼び出し元の owner id と一致しないリソースに対して常に `False`（非 stale）を返し、他 owner のリソースを誤って stale 判定・削除対象にしない — 根拠: `packages/docker-runtime/lib/docker_runtime_lifecycle.py`（`container_is_stale`／`network_is_stale` の owner label 比較）
- N/A: 独立 CLI コマンドを公開しないため、引数・exit code・JSON 出力の契約は持たない。
- N/A: config を所有しないため、`*.local.*` レイヤリングは呼び出し元 harness の責務である。

## 5. テストレビュー判断基準（パッケージ固有）

- Docker daemon を使わない単体テストでも、生成された command 全体を確認し、検証対象の security flag を mock で置き換えない。
- cleanup テストは「削除コマンドを呼んだ」だけでなく、途中失敗時の対象と逆順を確認する。
- namespace テストは同一 label の happy path だけでなく、meta/loop の値が異なることを確認する。
- テストの scaffolding（一時ディレクトリ/ファイル）は実行シェルの umask に依存させず、生成直後に期待モードを明示的に `chmod` する。owner-only permission 判定（EV-24〜EV-27）を意図する対象だけ `0600`/`0700` を明示し、それ以外のディレクトリ・ファイルは通常モード（`0755`/`0644` 等）を明示する（Issue #301: `umask 077` 環境でテストが意図せず owner-only 判定を誘発し失敗する事故の再発防止）。
