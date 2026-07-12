# meta-harness scenario 実行 backend スパイク実証記録

**日付**: 2026-07-12
**関連**: ADR-20260712-034（scenario 実行基盤の Docker + ephemeral broker 移行）、
`docs/design/meta-harness-detailed.md` §2-2 / §8-2
**環境**: Docker daemon = OrbStack 29.4.0（`unix:///Users/yoshihiko/.orbstack/run/docker.sock`）

本ファイルは ADR-20260712-034 の承認根拠（Docker containment と ephemeral broker の実証）を、
クローン先からも追跡できる形で恒久記録するもの。スパイクの実行手順・進行中の作業メモは
`.claude/handoffs/20260712T-meta-harness-scenario-backend-spikes.md`（作業用・非追跡）にあるが、
**判定根拠の SSOT は本ファイルとする**。

---

## S3: Docker containment 実証 — PASS

Docker が ADR-033 の封じ込め要件（setsid 離脱回収・資源上限・egress 遮断）を満たすことの実測。
手順書は Docker Desktop 前提だったが、実測環境は OrbStack。差異も併記する。

| 項目 | 結果 | 実測 |
| --- | ---- | ---- |
| イメージ / CLI 起動 | PASS | `node:22-slim` + `git curl procps` + `npm i -g @anthropic-ai/claude-code@2.1.207`。ホスト `claude --version` = `2.1.207` とコンテナ内が完全一致。イメージ tag `mh-spike-claude:2.1.207` |
| setsid 離脱回収 | PASS | `docker top` で setsid 経由子 `sleep 3600` と親 shell の両方が可視。`docker rm -f` 後、`docker ps -a` 空・ホスト `ps aux \| grep 'sleep 3600'` 0 件。OrbStack は VM ベースだが `docker rm -f` で完全回収 |
| egress 遮断（`--internal`） | PASS | `--internal` network 上のコンテナから `https://api.anthropic.com` は `curl: (6) Could not resolve host`（DNS 解決失敗）、直 IP `1.1.1.1:443` も `(7) Couldn't connect`。**コンテナ名解決は成立**（`getent hosts <peer>` exit 0） |
| host.docker.internal 到達性 | 到達不可（ADR 想定通り） | `--internal` 上のコンテナからは `host.docker.internal` 解決不可（exit 2）。**通常 bridge network では解決・到達可**（`0.250.250.254`）。OrbStack は `host.docker.internal` を実装するが `--internal` はこれも遮断する。→ broker を host プロセス + host.docker.internal で置く方式は不可 |
| sidecar パターン | PASS | 同一 internal network 上の別コンテナへ `curl http://<peer>:8080/` exit 0（名前解決 + 到達）。**broker は sidecar コンテナ方式に確定** |
| dual-homed の必要性 | 実証 | sidecar 自身も `--internal` 単独では外部 API 不可。`docker network connect <external>` で追加接続後に api.anthropic.com へ到達確認。**broker は internal + external の dual-homed 必須**。シナリオ側 internal-only コンテナは引き続き外部到達不可を維持。broker イメージには `ca-certificates` 必須 |
| pids-limit | PASS | `--pids-limit 64` 下で 100 個の `sleep &` 投入 → 上限で頭打ち。上限到達後は `docker exec` すら失敗するほどの強制力。その後も `docker rm -f` で正常除去 |
| docker.sock 不在 / negative | PASS | `--internal` コンテナ内に `/var/run/docker.sock`・`/run/docker.sock` 不在。外部到達・DNS 解決とも全滅 |

---

## S1: ephemeral broker 疎通 — PASS（案B「Docker + broker」確定）

`ANTHROPIC_BASE_URL` 差し替え + broker による OAuth Bearer 注入で `claude -p` が完走するかの実証。
token は keychain（`claudeAiOauth.accessToken`、108 bytes）から取得し、broker が読んだ直後に unlink。
呼び出し側 env・ホスト disk・候補コンテナに実 token を残さない方式で全手順を実施。

broker 実装（検証用・stdlib のみ）は受信リクエストの `x-api-key`/`authorization` を剥離し、
`Authorization: Bearer <token>` + `anthropic-beta: oauth-2025-04-20` を注入して api.anthropic.com へ
HTTPS 転送。SSE はチャンク単位で素通し。

| 項目 | 結果 | 実測 |
| --- | ---- | ---- |
| ホスト疎通 | PASS | 空 `CLAUDE_CONFIG_DIR` + `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` + `ANTHROPIC_API_KEY=<dummy>` で `claude -p "Reply with exactly: OK"` → exit 0・`result:"OK"`・`total_cost_usd`/`usage` 取得可（予算制御成立） |
| broker が認証を担う証明 | PASS | broker を外し dummy キーで api.anthropic.com 直アクセス → `is_error=true` / `api_error_status=401`。成功は dummy キーではなく broker の Bearer 注入による |
| SSE 素通し | PASS | `--output-format stream-json --verbose` で完走（result イベント到達） |
| エンドポイント網羅 | PASS | broker が中継したのは `POST /v1/messages?beta=true` のみ |
| ヘッダ | PASS | `anthropic-beta: oauth-2025-04-20` の注入が必要十分。剥離は `x-api-key`/`authorization`（本実装では allowlist 方式推奨） |
| コンテナ内疎通（本番構成） | PASS | dual-homed sidecar broker + internal-only scenario container で `claude -p` → exit 0・`result:"OK"`。token は `security → docker exec -i → sidecar tmpfs(/run/secrets, noexec/nosuid)` へ直接注入。broker 中継は `/v1/messages` のみ・200 |
| egress 遮断（実 token 下で再確認） | PASS | 同 internal-only container から api.anthropic.com 直アクセスは exit 6（DNS 解決不可）。dual-homed sidecar だけが唯一の外部橋渡し |
| TTL | 補足 | access token は静的（broker は refresh しない）。本実装では起動時 `expiresAt` preflight で run 想定時間より十分長いことを確認する方針 |

**クリーンアップ**: sidecar/scenario container・network・イメージ・host broker proc・token ファイルは
すべて削除済み（port 8787 closed 確認）。

---

## S2: L1 最小化 OAuth フォールバック — SKIP

S1 PASS のため不要（選択肢C 不採用。ADR-20260712-034）。token-in-container 方式は実装対象外。

---

## 結論

案B（Docker + ephemeral broker）成立。broker はコンテナ内に実 token を置かず `ANTHROPIC_BASE_URL`
差し替え + Bearer 注入で OAuth 認証を代行できる。有効化条件は「S1/S3 PASS + S2 SKIP 承認 + 封じ込め
検証テストの整備」（ADR-20260712-034）。実装は `.claude/handoffs/20260712T-meta-harness-docker-broker-impl.md`
の指示に従う。
