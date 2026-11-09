---
codd:
  node_id: "design:logging"
  kind: design
  status: active
  depends_on:
    - id: "design:architecture"
      relation: references
    - id: "adr:ADR-20260728-046"
      relation: references
  owner: ai-orchestra
---

# ログ / 状態ファイル仕様（SSOT）

このドキュメントは、`ai-orchestra` 全体のログ・状態ファイル配置の正本（Single Source of Truth）です。
対象時点: 2026-07-28

## 0. 本書の位置づけ

- 本書はリポジトリ内の**全ログ / 状態ファイルの正本**です。個別パッケージの README や ADR に記載があっても、配置・root 解決方針は本書を正とします。
- 新しいパッケージ・hook がログ / 状態ファイルを追加する場合、**本書「2. ログ / 状態ファイル一覧」への追記を必須**とします（追記なしのマージは不可）。
- 本書は「実装済みの事実」と「決定済みだが未実装の方針」を区別して記載します。ADR で決定しても実装が別 PR に分かれる場合があるため、両者を混同しないでください（区別の凡例は §2 参照）。

## 1. 配置規約（ADR-20260728-046 正本）

蓄積ログの配置・root 解決方針の決定は **ADR-20260728-046** を正本とします。要点は以下の3点です。

1. **機械が書く蓄積ログ**は `.claude/logs/<pkg>/` 配下に配置する（例: `.claude/logs/audit/`, `.claude/logs/fail-logs/`, `.claude/logs/skill-evolution/`）。
2. **蓄積型 gitignore ログ**（`.claude/logs/` 配下のうち fail-logs の `failures.jsonl`・教訓候補の台帳と、skill-evolution の実行範囲の記録・改善候補の台帳）は **root worktree 解決**で書く。
   - `git rev-parse --path-format=absolute --git-common-dir` の親ディレクトリを root worktree として使用し、そこに集約する。
   - git コマンドが失敗する場合（非リポジトリ・タイムアウト等）は `project_dir` へフォールバックする（fail-safe。破壊的動作へは進まない）。
   - root 解決は hook 内部の git 呼び出しに限定し、config 経由で `project_dir` 外パスを指定させない（既存のパストラバーサルガードは維持）。
   - v1 の skill-evolution の pending / locks は対象外だった（ADR-20260728-046 Amendment。セッション単位の一時状態のため `project_dir` ローカル）。v2（ADR-20261108-065）はこれらを書かない。
3. **人が読む git 管理下の知識ファイル**（`docs/`・`.claude/rules/` 等）は root 解決の**対象外**。コミット → PR マージが生存経路であり、worktree 削除で消失する問題自体が発生しないため。

> 詳細な検討経緯・却下した代替案は `docs/adr/ADR-20260728-046.md` を参照してください。

## 2. ログ / 状態ファイル一覧

凡例（root 解決列）:

- **実装済み**: 現在のコードが root worktree 解決を行っている
- **対象外**: 性質上 root 解決の対象にしない（state はセッション/worktree スコープ）

| 書き込み元 | パス | 形式 | git管理 | root解決 | 用途 |
|---|---|---|---|---|---|
| `audit`（`packages/audit/hooks/event_logger.py` 経由、`audit-*` hook 全般） | `.claude/logs/audit/sessions/<session_id>.jsonl` | JSONL | ignore（`.claude/logs/`） | **実装済み** | セッション単位の統一監査イベントログ |
| `audit`（`audit-bootstrap.py`, `audit-prompt.py`） | `.claude/state/audit-trace.json` | JSON | ignore（`.claude/state/`） | 対象外（project_dir ローカル） | トレース ID / expected route の受け渡し |
| `audit`（`audit-subagent-start.py`） | `.claude/state/audit-subagent-<agent_id>.json` | JSON | ignore | 対象外（project_dir ローカル） | サブエージェント固有のトレース状態 |
| `quality-gates`（`post-test-analysis.py`、audit の sessions ログへ相乗り） | `.claude/logs/audit/sessions/<session_id>.jsonl`（`type: quality_gate`） | JSONL | ignore | **実装済み**（audit 経由） | テストコマンド実行結果の記録 |
| `fail-logs`（`capture-failures.py`） | `.claude/logs/fail-logs/failures.jsonl` | JSONL | ignore（`.claude/logs/`） | **実装済み** | ツール実行失敗イベントの記録 |
| `fail-logs`（台帳・CLI） | `.claude/logs/fail-logs/lessons.jsonl` / `lessons-state.json` / `lessons.md` | JSONL / JSON / Markdown | ignore（`.claude/logs/`） | **実装済み** | 教訓候補の出来事の履歴・現在の状態・人が読む一覧 |
| `fail-logs`（教訓化） | `.claude/logs/fail-logs/distill-runs.jsonl` / `distill-daily.json` | JSONL / JSON | ignore（`.claude/logs/`） | **実装済み** | 教訓化の実行記録（理由付き。LLM の出力は書かない）とその日の回数 |
| `skill-evolution`（v2 の hook） | `.claude/logs/skill-evolution/runs-YYYYMM.jsonl` | JSONL | ignore（`.claude/logs/`） | **実装済み** | スキルの実行範囲の出来事（開始・確認・ターンとサブエージェントの区切り）。追記だけ。古い月は人が `skill_candidates_admin.py prune-runs` で消す |
| `skill-evolution`（`skill_candidates_admin.py`） | `.claude/logs/skill-evolution/candidates.jsonl` / `candidates-state.json` / `candidates.lock` / `ingest-state.json` | JSONL / JSON / lockfile | ignore（`.claude/logs/`） | **実装済み** | 改善候補の台帳（追記だけ）・その写像・更新の排他・取り込みの位置 |
| `skill-evolution`（`skill_candidates_admin.py draft`） | `.claude/logs/skill-evolution/drafts/<cid>/<n>/` | YAML / JSON | ignore（`.claude/logs/`） | **実装済み** | シナリオの下書きと、LLM が出した元の写し |
| `skill-evolution`（v1。廃止） | `.claude/logs/skill-evolution/metrics/`・`pending/`・`locks/`、`.claude/skill-evolution/lessons/`、それより前の `.claude/skill-evolution/metrics/`・`pending/` | — | — | — | v2 は読みも書きもせず、自動では消さない。不要なら人が消す（`.claude/logs/skill-evolution/` そのものは v2 の記録・台帳なので消さない） |
| （予約領域・現状書き込み元なし） | `.claude/logs/orchestration/`（`scripts/lib/scaffold.py` が新規プロジェクトに `.gitkeep` 付きで作成） | — | ignore（`.claude/logs/`） | 対象外（未使用） | 将来のオーケストレーションログ用に予約されたディレクトリ。現時点で書き込む hook / スクリプトは存在しない |

### v1 の metrics の移行

v1 の metrics の one-shot migration（ADR-20260728-046 決定3）は、v2 では行わない（v2 の hook は metrics を読まない）。残った v1 のデータは、上の表のパスを確かめて人が消す
（v0.3.8 にあった `skill_evolution.py purge-v1` は、非推奨期間を終えて外した）。

## 3. 主なイベント種別（audit sessions ログ）

| type | 主な出力元 | 用途 |
|---|---|---|
| `session_start` / `session_end` | `audit-bootstrap.py`, `audit-session-end.py` | セッション開始 / 終了の集計 |
| `prompt` | `audit-prompt.py` | expected route と入力抜粋の記録 |
| `route_decision` | `audit-route.py` | expected / actual route の照合 |
| `routing_suggestion` | `packages/agent-routing/hooks/agent-router.py` | エージェント提案の判定（engine・status・Jev の確率・キーワード判定の結果。入力の本文は含まない） |
| `quality_gate` | `packages/quality-gates/hooks/post-test-analysis.py` | テストコマンド実行結果の記録 |
| `cli_call` | `audit-cli.py` | Codex / Antigravity CLI 呼び出しの記録 |
| `subagent_start` / `subagent_end` | `audit-subagent-start.py`, `audit-subagent-end.py` | サブエージェントのライフサイクル |
| `instructions_loaded` | `audit-instructions-loaded.py` | 読み込まれた指示書の監査 |

> `fail-logs` の `failures.jsonl` は audit v1 互換のレコード形（`v` / `ts` / `sid` / `eid` / `type: "failure"` / `data{...}`）を採用しますが、audit の sessions ログとは**別ファイル**に記録されます（`docs/design/fail-logs.md` §3 参照）。上表の `type` 一覧には含みません。

## 4. 集計と確認

### audit

- 全体を時系列で確認する: `orchex run audit log-viewer`
- ルーティング精度を見る: `prompt` と `route_decision` を `log-viewer` / `kpi-report` で確認。提案の判定は `orchex run audit log-viewer -- --type routing_suggestion`
- CLI 利用状況を見る: `orchex run audit analyze-cli-usage`
- セッション全体を俯瞰する: `orchex run audit dashboard`
- HTML で共有する: `orchex run audit dashboard-html -- -o dashboard.html`

`kpi-report.py` や `dashboard-html.py` の出力ファイル名は固定ではなく、呼び出し側が `--output` / `-o` で指定します。

### fail-logs

- 専用ビューアはなく、`failures.jsonl` を直接参照するか SessionStart 時の再発サマリー注入（`inject-failure-summary.py`、設定は `packages/fail-logs/config/fail-logs.yaml` の `summary.*`）で確認します。
- 集計仕様（再発シグネチャ・無害化・有界性保証）の正本は `docs/design/fail-logs.md` です。
- 教訓候補は `lessons.md`（人が読む一覧。Codex などに渡せる）か `python3 "$AI_ORCHESTRA_DIR/packages/fail-logs/scripts/lessons_cli.py" list` で確認し、`/lessons` で昇格・却下します。台帳・写像・一覧は root worktree に集約され、`lessons.lock` の中で更新されます（`docs/design/fail-logs.md` §8）。
- 教訓化（既定で有効）の実行は `distill-runs.jsonl` に理由付きで 1 件ずつ記録されます（`skipped` の理由: `no_signal` / `daily_limit` / `reserved` / `locked` / `no_masking` など。`docs/design/fail-logs.md` §7.6）。

### skill-evolution

- 実行範囲と改善候補は `packages/skill-evolution/scripts/skill_candidates.py` の `list` / `show` / `runs`（読み取り）で確認し、状態の変更は `/skill-candidates` から `skill_candidates_admin.py` で行います（`docs/design/skill-evolution.md` §10）。
- 記録・台帳・下書きの設定（`runs.*`・`ledger.*`・`draft.*`）の正本は `packages/skill-evolution/config/skill-evolution.yaml` です。
- v1 の CLI（`skill_evolution.py`）は廃止しました。残った v1 のデータは自動では消さないので、不要なら §2 の表のパスを人が消します（`.claude/logs/skill-evolution/` そのものは v2 の記録・台帳なので消しません）。

## 5. 補足

- 旧 `route-audit` / `cli-logging` 系の個別 JSONL は現行実装では使っていません。
- audit ログの正本は `audit` パッケージのセッション単位 JSONL です。fail-logs / skill-evolution はそれぞれ独立したログ系統であり、ADR-20260612-025 の重複許容方針を踏襲した ADR-20260728-046 の裁定により統合しません（目的・スキーマが異なるため）。
- root worktree 解決の実装パターンは ADR-20260728-046 により `packages/core/hooks/hook_common.py` に共通関数（`resolve_root_worktree` / `resolve_log_root`）として抽出され、fail-logs（`capture-failures.py` / `inject-failure-summary.py`）はこれを利用しています。audit（`packages/audit/hooks/event_logger.py`）は現状も自前実装（`_resolve_root_worktree` / `_resolve_log_root`）のままで、共通関数への載せ替えは挙動同一のリファクタとして別 Issue の後続作業です。
