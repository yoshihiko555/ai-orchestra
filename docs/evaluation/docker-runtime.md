# docker-runtime 評価セット

**パッケージ**: `packages/docker-runtime`
**類型**: CLI ツール型（内部ライブラリ）
**作成日**: 2026-07-15
**最終レビュー日**: 未レビュー
**情報源**: `docs/design/loop-harness-isolation.md` §5.2・§6・§8、`docs/adr/ADR-20260712-034.md`

## 1. 責務定義

`docker-runtime` は複数の harness が共有する Docker CLI、セキュリティプロファイル、dual-homed broker、
所有 resource の cleanup を提供する。呼び出し元固有の namespace・image・config を引数として受け取り、
meta-harness と loop-harness の resource が交差しない状態で fail-closed な実行境界を構築する。

### Non-Goals

- harness 固有の worktree、git metadata、成果物、設定スキーマは扱わない。
- Phase 0 では image prune、BuildKit GC、並行 build lock の挙動を追加しない。
- `docker/broker/broker.py` の env var 契約（`MH_BROKER_*` / `MH_PRICE_*` prefix）と `server_version`/user-agent の汎化は Phase 1 で行う。`lib/` 配下（lifecycle/profile/cli builder）は namespace 注入型で共有化済みだが、`docker/broker/broker.py` 本体は meta-harness 固有のままである（既知のギャップ）。

## 2. 期待する入出力・副作用

| 構成要素 | 入力 | 期待する出力 | 副作用 |
| --- | --- | --- | --- |
| `docker_runtime_cli.py` | image、build context、build args、runner | immutable image ID または明示的エラー | Docker image inspect/build/remove |
| `docker_runtime_profile.py` | mount/resource/env 値 | hardened Docker CLI 引数 | なし |
| `docker_runtime_lifecycle.py` | namespace、broker spec、runner/callback | broker session または明示的エラー | container/network の作成・破棄 |

## 3. 評価観点

- [ ] EV-01（正常 / must）: 同一 image・context hash・build args の process-local build は 1 回だけ実行し、2 回目は再利用する — 根拠: Phase 0 の既存 meta-harness 挙動
- [ ] EV-02（異常 / must）: auto-build 無効時は `@sha256:<digest>` 形式でない mutable image を拒否する — 根拠: ADR-20260712-034
- [ ] EV-03（異常 / must）: container/network の削除失敗は明示的な missing-object 応答だけを「既に削除済み」として成功扱いし、permission error 等を成功扱いしない — 根拠: ADR-20260712-034 受容基準
- [ ] EV-04（正常 / must）: profile builder は read-only rootfs、cap drop、no-new-privileges、non-root、resource limit、noexec tmpfs を構成できる — 根拠: `docs/design/loop-harness-isolation.md` §3
- [ ] EV-05（異常 / must）: comma を含む bind source 等、安全に Docker CLI へ表現できない mount は拒否する — 根拠: meta-harness 既存実装挙動
- [ ] EV-06（正常 / must）: broker は internal/external network の dual-homed sidecar として起動し、scenario 側の direct egress や Docker socket mount を追加しない — 根拠: `docs/design/loop-harness-isolation.md` §2・§3
- [ ] EV-07（異常 / must）: broker 起動の途中失敗では作成済み container/network を逆順に cleanup し、非隔離実行へ降格しない — 根拠: `docs/design/loop-harness-isolation.md` §2.1・§7
- [ ] EV-08（境界 / must）: harness ごとの `DOCKER_LABEL` から owner/parent/created labels を導出し、meta-harness と loop-harness の cleanup namespace を分離する — 根拠: `docs/design/loop-harness-isolation.md` §6

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
