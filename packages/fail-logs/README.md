# fail-logs

AI（Claude Code 等）の**失敗を貯め、そこから教訓を昇格させる**パッケージ。失敗イベントを記録して再発をセッション開始時に知らせ、
失敗・人の訂正・レビュー指摘から教訓候補を作り、繰り返し現れる候補を人が承認して AGENTS.md・rules・ガードに反映する。

```text
記録（hook）──▶ 再発の注入（SessionStart）
   │
   └──▶ 教訓化（SessionEnd。既定で無効）──▶ 教訓候補の台帳 ──▶ 提示（SessionStart）──▶ 昇格（/lessons。人が承認）
                                            ▲
                             /review-respond・作業中のエージェント（lessons_cli.py add）
```

仕様の正本は `docs/requirements/fail-logs.md`（要件）と `docs/design/fail-logs.md`（設計）、判断は ADR-20261103-063。

## 何をするか

### 1. 失敗の記録

`capture-failures.py` を `PostToolUse` / `PostToolUseFailure` / `PermissionDenied` に登録し、以下を `.claude/logs/fail-logs/failures.jsonl` に追記する:

| 失敗種別            | 検知対象                                                           |
| ------------------- | ------------------------------------------------------------------ |
| `tool_error`        | Bash の非ゼロ終了（`Exit code N`）、Bash 以外のツールのエラー      |
| `test_failure`      | pytest / npm test 等の失敗（非ゼロ終了、または出力の失敗マーカー） |
| `lint_failure`      | ruff / mypy 等の失敗                                               |
| `cli_failure`       | codex / agy 等の外部 CLI 失敗                                      |
| `permission_denied` | auto mode の分類器が拒否したツール呼び出し                         |

- **失敗のみを記録する**（ノイズ抑制）。成功を含む実行統計は audit の `quality_gate` が担う。中断（Esc）は記録しない
- 検知ロジックは `packages/core/hooks/failure_detector.py`（純粋関数）に集約。`exit_code` が 0/欠落でも test/lint コマンドで出力に
  失敗マーカーがあれば失敗とみなす 2 段判定で、`pytest ... | tail` のようなパイプの終了コードのマスクを回避する
- Edit の入力検査の失敗（置換元の文字列が無い等）は hook に届かないので記録されない（教訓化の前段判定ではセッションの記録から拾う）

### 2. 再発の注入（SessionStart）

`inject-failure-summary.py` が直近の失敗から再発しているシグネチャを集計し、`<fail-logs-summary>` 枠でコンテキストへ注入する。
新しいイベント（`PostToolUseFailure` / `PermissionDenied`）のテスト・lint 以外の行は、別の見出し（その他部）で足す。
`grep` の不一致のような意図した非ゼロ終了は集計しない。

### 3. 教訓候補の台帳と提示

- 教訓候補（事象・原因・回避策・根拠・再発回数・重大度・対象範囲・状態）を、root worktree の `.claude/logs/fail-logs/lessons.jsonl`（台帳）・
  `lessons-state.json`（写像）・`lessons.md`（人が読む一覧）に保存する。git で追跡しない
- 再発回数は「異なるセッション・異なる PR の数」。同じセッション・同じ PR から何度足しても 1
- `present-lessons.py`（SessionStart）が、基準を超えた候補（再発 2 回以上、昇格したのに再発、昇格中のまま 14 日 など）を `<fail-logs-lessons>`
  枠で知らせる。同じ内容は同じ root で 1 日 1 回まで。重大で再発 1 の候補は ID だけを示す。非対話のセッションでは示さない

### 4. 教訓化（既定で無効）

`distill-launch.py`（SessionEnd）が、教訓化が有効なときだけワーカーを切り離して起動する（セッションの終了を待たせない）。
ワーカーは LLM を使わない前段判定（人の訂正・手動の拒否・中断、未解決や反復の失敗）で合図のあったセッションだけを、
道具を持たない `claude -p`（`--restricted --tools ""`、hook・MCP・セッション保存なし、環境変数は許可リストだけ）に渡し、
出力を形式検査してから台帳に入れる。TDD の red → green や一過性の失敗は合図にしない。

**有効にするには** `.claude/config/fail-logs/fail-logs.local.yaml` に次を書く（`claude -p` の費用が発生する）:

```yaml
distill:
  enabled: true
```

- 1 日の回数（`distill.daily_limit`、既定 10）と 1 回の費用（`distill.max_budget_usd`、既定 0.5 ドル）に上限がある。設定でも越えられない
  コード側の上限（1 日 50 回・1 回 2.0 ドル など）もある
- 抜粋はセッションの記録から機密情報を伏せてから切り詰めて渡す（audit の強化版マスキング + PII）。伏せ字が使えないときは呼ばない
- **`ANTHROPIC_BASE_URL` を差し替えている環境では、抜粋はその接続先に送られる**
- 実行の記録は `.claude/logs/fail-logs/distill-runs.jsonl`（理由付き。LLM の出力そのものは記録しない）
- 判断の質は `scripts/eval_distill.py --model sonnet` で手動評価できる（CI では実行しない）

### 5. 昇格（`/lessons`）

人が `/lessons` スキルで候補を 1 件ずつ確認し、昇格先を選び、承認した文面だけを書き込む:

| 昇格先                                   | 届く CLI                                          |
| ---------------------------------------- | ------------------------------------------------- |
| `AGENTS.md` の記述欄（管理ブロックの外） | Claude Code・Codex・Antigravity                   |
| プロジェクト独自の `.claude/rules`       | Claude Code                                       |
| ガード（テスト・hook・lint）             | GitHub Issue を作るところまで（コードは書かない） |

書き込んだ変更のコミットと PR は通常の手順（`/commit`・`/pr-create`）で行う。昇格中の候補は、次に `/lessons` を実行したときに統合ブランチへの反映を確かめる。

## CLI

| スクリプト                 | 使い手                                      | サブコマンド                                                                           |
| -------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------- |
| `scripts/lessons_cli.py`   | エージェント・`/review-respond`・`/lessons` | `list` / `show` / `add` / `check-text` / `render` / `path`（読み取りと候補の追加だけ） |
| `scripts/lessons_admin.py` | 人（`/lessons` で承認した操作だけ）         | `revise` / `set-state` / `merge` / `ack` / `verify` / `issue` / `rebuild`              |

- **`lessons_cli.py` は Bash の許可リストに入れてよいが、`lessons_admin.py` は入れないこと**。入れると、注入されたエージェントが
  状態の変更や公開 Issue の作成をできてしまう
- 終了コード（共通）: 0 成功 / 1 その他の失敗 / 2 伏せ字が使えない / 3 spool に退避した・サンドボックスの外での実行が要る /
  4 導入されていない・無効 / 5 入力の検査に失敗・確認が要る
- 作業中に落とし穴と回避策に気づいたエージェントは、auto-memory ではなく `lessons_cli.py add` で候補にする（memory-layers ルール）

```bash
echo '{"title": "1 行の見出し", "event": "事象", "cause": "原因", "avoidance": "回避策", "scope": "claude", "severity": "normal"}' \
  | python3 "$AI_ORCHESTRA_DIR/packages/fail-logs/scripts/lessons_cli.py" add --json -
```

- 既定のサンドボックスでは linked worktree から root の台帳に書けない。そのとき `add` は作業中の worktree の
  `.claude/logs/fail-logs/spool/` に退避し（終了コード 3）、次のセッション開始時か教訓化のワーカーが取り込む

## ログスキーマ（audit v1 互換）

```json
{
  "v": 1,
  "ts": "2026-06-12T...",
  "sid": "session-id",
  "eid": "12-hex",
  "type": "failure",
  "data": {
    "failure_type": "test_failure",
    "error_type": "assertion",
    "detected_by": "output_pattern",
    "command_kind": "test",
    "tool": "Bash",
    "command": "pytest ...",
    "error_excerpt": "FAILED ...",
    "exit_code": 0,
    "cwd": "/path",
    "branch": "main"
  }
}
```

- `PostToolUseFailure` / `PermissionDenied` から記録した行は、これに加えて `data.tool_use_id` を持つ（`detected_by` は `exit_code` /
  `tool_error` / `permission`）。この行の `command` / `error_excerpt` は強化版マスキングで伏せてから切り詰める（伏せ字が使えなければ空）
- ログファイルは所有者限定パーミッション（`0600`）。機密情報（API キー・トークン等）は記録前に `[REDACTED]` へマスクする
- `.claude/logs/` は gitignore 済み（ローカル蓄積）

## 保存先

Git の root worktree を解決でき、そこに `.claude/` がある場合、各 linked worktree のログは root worktree の `.claude/logs/fail-logs/` に集約する。root worktree 自身から実行している場合は `git init --separate-git-dir` 構成でも常に正しく解決する。linked worktree から main worktree（root）を辿る場合、main worktree 自体が `--separate-git-dir` で作られていると Git 自身が実体パスを保持していないため理論上解決できないことがあり、その場合は安全側で解決失敗として扱う。root の解決に失敗した場合、または root に `.claude/` がない場合は `project_dir` へフォールバックする。アップグレード前の worktree-local ログは、初回実行時に新しい方から最大 1 MiB だけを root 側へ追記し、旧ファイルを `failures.jsonl.migrated.<pid>-<seq>` に変更して二重移行を防ぐ。古い履歴は破棄されるが、サマリーはログ末尾だけを使うため実用上の影響はない。試行中の claim 名も pid と monotonic suffix を持つ `failures.jsonl.migrating.*` とし、クラッシュした試行の stale claim は上書きせず手動復旧用に残す。

| ファイル               | 内容                                         |
| ---------------------- | -------------------------------------------- |
| `failures.jsonl`       | 失敗イベント                                 |
| `lessons.jsonl`        | 教訓候補の出来事の履歴（台帳）               |
| `lessons-state.json`   | 教訓候補の現在の状態（写像）                 |
| `lessons.md`           | 人が読む教訓候補の一覧（Codex などに渡せる） |
| `lessons-present.json` | 提示の頻度制御の状態                         |
| `distill-runs.jsonl`   | 教訓化の実行記録                             |
| `distill-daily.json`   | その日の教訓化の回数                         |
| `spool/`               | root に書けないときに退避した候補の追加      |

## 設定

`config/fail-logs.yaml`（プロジェクト固有の上書きは `fail-logs.local.yaml`）。主なキー:

```yaml
enabled: true # 全体の有効/無効（false で記録・注入・教訓化・提示・CLI のすべてを止める）
targets: # 失敗種別ごとのトグル
  tool_error: true
  test_failure: true
  lint_failure: true
  cli_failure: true
  permission_denied: true
max_excerpt_chars: 500 # 抜粋の最大文字数
logs_dir: .claude/logs/fail-logs
summary: # 再発の注入（top_signatures_new・expected_nonzero_commands など、その他部の設定を含む）
  enabled: true
lessons: # 教訓候補の台帳と提示
  add_daily_limit: 20
  present:
    enabled: true # false で提示だけを止める
    threshold: 2
distill: # 教訓化（既定で無効）
  enabled: false
  model: sonnet
  daily_limit: 10
  max_budget_usd: 0.5
```

全キーと既定値は `config/fail-logs.yaml` と設計書 §12 を参照。

## 責務境界（audit / quality-gates との関係）

| パッケージ      | 責務                                                        |
| --------------- | ----------------------------------------------------------- |
| `audit`         | オーケストレーションの compliance / observability（KPI 等） |
| `quality-gates` | テストゲート + `quality_gate` イベント（合格率の分母）      |
| `fail-logs`     | **失敗知識の蓄積と教訓の昇格**（学習ループ）                |

- 失敗キャプチャは目的・スキーマが異なるため、当面 quality-gates との重複を意図的に許容する。
- 失敗検知ロジックは core の共通ユーティリティに集約済み。quality-gates への適用（パイプマスクバグ修正）と将来の責務移行は ADR-20260612-025 を参照。

## 依存

- `core`（`hook_common`, `failure_detector`）のみ。audit パッケージには依存しない（伏せ字は audit の `secret_masking.py` を
  ファイルとして読み込むだけで、読み込めなければ機密情報を含みうる項目を空にする・教訓化を呼ばない）。
