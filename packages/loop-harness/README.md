# loop-harness パッケージ

Trigger → Maker → Checker → stop decision を繰り返す、自律改善ループの基盤。Issue 起点でコードを実装し、機械チェック（テスト/lint）と LLM レビューを経て PR を作成・完了させるまでを自動化します。

> **利用者向けガイド**: `/loop-issue` の使い方・ループの仕組み（図解）は [`docs/guides/loop-harness.md`](../../docs/guides/loop-harness.md) を参照してください。本 README はパッケージの構成・config・cron/launchd セットアップ手順を扱います。

## 概要

loop-harness には 2 つの実行形態があります。

- **LP-1（`/loop-issue` スキル）**: `loop_step.py` を人間（オーケストレーター）が対話的に呼び出し、1 ステップずつ進める方式。既存の `/loop-issue` スキルフローの内部実装。
- **LP-2（常駐 scheduler）**: `loop_scheduler.py` が常駐し、ラベル付き Issue をポーリングして `loop_driver.py`（headless worker）を自動起動する無人運用方式。人手を介さず複数 Issue を並行して回す。**experimental**: Maker/Checker のプロセス隔離が未完成（Issue #211）のため、信頼できるリポジトリでのみ自己責任で使用してください。

両者とも `lib/loop_common.py`（状態機械・ロック・journal）を共有し、状態は `.claude/loop/<loop_id>/state.json` に永続化されます。

## コンポーネント

| ファイル                     | 説明                                                                          |
| ----------------------------- | ----------------------------------------------------------------------------- |
| `scripts/loop_step.py`        | LP-1 CLI。`start/attach/propose/complete/reconcile/heartbeat/resume` を JSON 出力で提供 |
| `scripts/loop_driver.py`      | LP-2 headless worker。`claude -p` で Maker/Checker を駆動し `loop_common` を直接呼ぶ |
| `scripts/loop_scheduler.py`   | LP-2 常駐 scheduler。ラベル付き Issue の発見・並行数制御・worker の起動/再起動 |
| `scripts/loop_status.py`      | 状態確認 CLI。`list/show/purge` でループ実行の状態を確認・整理                |
| `lib/loop_common.py` ほか     | 状態機械・ロック・journal・worktree 管理などの共通ロジック                    |

## LP-2 セットアップ（cron / launchd）

scheduler 自身は cron/launchd の登録テンプレートを生成するのみで、自動登録は行いません。

> **`--project` はサブコマンドの前に置くこと。** 後ろに置くと argparse エラーになります。
>
> - OK: `loop_scheduler.py --project /path/to/repo print-cron`
> - NG: `loop_scheduler.py print-cron --project /path/to/repo`

### cron の場合

```bash
python3 packages/loop-harness/scripts/loop_scheduler.py --project /path/to/repo print-cron
```

出力された 1 行（`*/5 * * * * ... loop_scheduler.py --project ... is-alive || ... loop_scheduler.py --project ... >> .../scheduler.log 2>&1`）を `crontab -e` で追記します。5 分おきに `is-alive` サブコマンド（pidfile + flock による単一起動判定。Issue #216。旧 `pgrep` ベースの生存確認から置き換え済み）で生存確認し、死んでいれば再起動するガード付きエントリです。

### launchd の場合

```bash
python3 packages/loop-harness/scripts/loop_scheduler.py --project /path/to/repo print-launchd
```

出力された plist を `~/Library/LaunchAgents/com.ai-orchestra.loop-scheduler.plist` に保存し、以下でロードします。

```bash
launchctl load ~/Library/LaunchAgents/com.ai-orchestra.loop-scheduler.plist
```

`RunAtLoad` / `KeepAlive` が有効なため、ログイン時起動・異常終了時再起動を launchd 自身が担います。

> **launchd 採用時は cron 登録を併用しない。** どちらも「scheduler が死んでいたら再起動する」役割を持つため、両方登録すると二重起動の原因になります（設計 §3.5）。

## 状態確認（loop_status.py）

```bash
# 全ループ実行を一覧表示（--status で絞り込み、--json で機械可読出力）
python3 packages/loop-harness/scripts/loop_status.py list [--status <phase>] [--json]

# 1 件の詳細（state.json 全体 + journal 末尾。--full-journal で全件）
python3 packages/loop-harness/scripts/loop_status.py show --loop-id <id> [--journal-lines N] [--full-journal]

# 完了済みループの掃除（既定: 30日以上経過した passed/failed のみ）
python3 packages/loop-harness/scripts/loop_status.py purge [--force] [--dry-run] [--yes]
```

`purge` は `running` / `waiting_external` を常に保護します（`--force` でも削除しません。削除直前に state を再読込し、保護対象へ遷移していれば直前でもスキップします）。通常モードでは `passed` / `failed` かつ `retention.purge_after_days` 経過後のみが対象です。`--dry-run` を付けない実削除は対話確認（非対話環境では `--yes` 必須）を経て実行します。

## push 多層防御（安全性）

LP-2 の Maker は `claude -p` で起動されますが、push や PR 作成は Maker 自身には行わせません。実際の push は worker（`loop_driver.py`）が安全ガードを通過した後にのみ実行します。

- **層1（プロンプト制約）**: Maker への指示に「push/PR 作成を行わない」旨を明記
- **層2（env 認証隔離）**: Maker プロセスの環境から push に使える認証情報を隔離
- **層3（`--disallowedTools`）**: Maker の `claude -p` 起動オプションで push 系ツールを構造的に禁止
- **層4（push 前後の整合性検証）**: push 前後で remote HEAD を比較し、想定外の変更があれば `push_integrity_violation` として安全停止

詳細は `docs/design/loop-harness-cli.md` §2.2（Maker 起動）/ §2.6（push ガード）を参照してください。

## config

主要キーは `packages/loop-harness/config/loop-harness.yaml`（プロジェクト固有の上書きは `.claude/config/loop-harness/loop-harness.local.yaml`。`config-loading` ルール準拠）。

LP-2 の driver entrypoint・definition module・実効 base config は Maker の action worktree 外から
読み込む必要があります。`loop_driver.py` は起動時にこの境界を検証し、action worktree 内へ runtime を
コピーして起動する wrapper/self-hosting 構成を fail-closed で拒否します。root worktree の runtime と
別 linked action worktree を使う通常構成は許可されます。

| キー                              | デフォルト | 説明                                                    |
| --------------------------------- | ---------- | ------------------------------------------------------- |
| `lp2.concurrency_limit`           | `2`        | scheduler が同時に起動する worker の最大数              |
| `lp2.wall_clock_timeout_seconds`  | `7200`     | 1 ループ実行あたりの実時間タイムアウト（秒）            |
| `lock.ttl_seconds.lp2`            | `300`      | LP-2 worker のロック TTL（秒）                          |
| `retention.purge_after_days`      | `30`       | `loop_status.py purge` の既定経過日数                    |
| `lp2.priority_labels`             | `[]`       | discovery のソート優先度に使うラベル（先頭ほど高優先度）|

## 設計・評価の正本

- `docs/requirements/loop-harness.md` — 要件
- `docs/design/loop-harness.md` — 総論
- `docs/design/loop-harness-core.md` — 状態機械・ロック等コア設計
- `docs/design/loop-harness-cli.md` — CLI（`loop_step`/`loop_driver`/`loop_scheduler`/`loop_status`）契約
- `docs/design/loop-harness-pr-review.md` — PR レビュー待ち設計
- `docs/evaluation/loop-harness.md` — 評価セット
