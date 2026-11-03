# Memory Layers — 記憶の置き場所の分担

**作業中に得た状態・決定・気づきは、寿命と読み手から置き場所を 1 つに決める。Claude Code の記憶（auto-memory / claude-mem）には、他の層で持てないものだけを書く（昇格待ちの一時保管を除く）。**

## 記憶の層

| 層                                                                                                    | 覚えるもの                                                            | 寿命・スコープ                                                         | 読み手                                                                                                                     |
| ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `.claude/context/`（context-sharing）                                                                 | サブエージェントの結果、作業中のファイル                              | 1 セッション（終了時に削除）                                           | サブエージェント（prompt に注入された分）                                                                                  |
| `.claude/plans/<slug>.md`（task-memory-usage）                                                        | 発注書、進行状態、タスク中の決定と気づき（Project ごとに 1 ファイル） | worktree ごと。worktree と一緒に、または `/task-state close` で消える  | ファイルを読む CLI すべて（`/handoff` で Codex へ渡す）                                                                    |
| `docs/adr/`（ADR・DECISIONS.md）                                                                      | リポジトリ全体・複数タスクに効く恒久的な設計判断                      | リポジトリ（git 管理）                                                 | 全 CLI                                                                                                                     |
| `AGENTS.md`                                                                                           | 全 CLI が守る規約・手順・落とし穴                                     | リポジトリ（git 管理）                                                 | 全 CLI（起動時に自動で読む）                                                                                               |
| auto-memory（`~/.claude/projects/*/memory/`）                                                         | ユーザーの好み、作業の仕方へのフィードバック、長期の事実              | リポジトリ単位で、同じクローンの全 worktree が共有。マシンローカル     | Claude Code 本体とサブエージェント（Codex / Antigravity には注入されない）                                                 |
| claude-mem（導入している場合）                                                                        | 何が起きたかの自動記録                                                | 全プロジェクト横断。マシンローカル                                     | Claude Code 本体（セッション開始時に注入）                                                                                 |
| 教訓候補の台帳（fail-logs を導入している場合。`.claude/logs/fail-logs/lessons.jsonl` / `lessons.md`） | 失敗・人の訂正・レビュー指摘から作った、未承認の落とし穴と回避策      | root worktree に集約し、全 worktree が共有。git 管理外・マシンローカル | 人（`/lessons`）。基準を超えた候補はセッション開始時に提示される。Codex などへは `lessons.md` を渡す（自動では読ませない） |

- auto-memory と claude-mem は orchex の管理外。このルールが決めるのは「何をどこに書くか」だけ
- claude-mem の注入には、別の worktree や過去の作業の記録が混ざる。現在の状態の根拠には plan ファイルなどエンジンの状態を使う
- fail-logs の失敗イベント（`failures.jsonl`）や audit など、パッケージの hook が自動で記録するログはこのルールの対象外。
  fail-logs の教訓候補の台帳は、昇格待ちの層の 1 つとして扱う（ADR-20261103-063）

## 書き分け

| 作業中に出てきたもの                                                 | 書く場所                                                                                                                                                                        | auto-memory                |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| 進行状態（何が終わった、PR 待ち、次にやること）                      | `/goal`・Codex 直接は plan ファイル、`/loop-issue`・TAKT は Issue とエンジンの状態、`/issue-fix` は会話と git（詳細は git-workflow パッケージの `development-workflow` ルール） | 書かない                   |
| そのタスクの範囲での決定                                             | `/goal`・Codex 直接は plan ファイルの `## Decisions`。plan ファイルを持たないエンジン（`/loop-issue`・TAKT・`/issue-fix`）は Issue のコメントか PR 本文                         | 書かない                   |
| リポジトリ全体・複数タスクに効き、タスクが終わっても参照する設計判断 | ADR（`docs/adr/`）                                                                                                                                                              | 書かない                   |
| そのタスクの中だけで効く気づき                                       | `/goal`・Codex 直接は plan ファイルの `## Notes`。plan ファイルを持たないエンジンは Issue のコメントか PR 本文                                                                  | 書かない                   |
| 作業中に得た落とし穴と回避策（Claude Code 固有・全 CLI 共通の両方）  | fail-logs を導入していれば `lessons_cli.py add` で教訓候補にする（下の「教訓候補と昇格」）。導入していなければ auto-memory（feedback / project）に書き、棚卸しで昇格する        | fail-logs があれば書かない |
| 次のタスクにも効く、Claude Code の作業の仕方・ユーザーの好み         | auto-memory（feedback / user）                                                                                                                                                  | 書く                       |
| ブランチをまたいで、長期的に有効な事実・外部の制約・参照先           | auto-memory（project / reference）                                                                                                                                              | 書く                       |
| Codex / Antigravity にも必要な規約・落とし穴                         | fail-logs を導入していれば教訓候補にし、`/lessons` で `AGENTS.md` の記述欄へ昇格する。導入していなければ、まず auto-memory。棚卸しで `AGENTS.md` の記述欄へ昇格                 | 昇格したら消す             |

- plan ファイルの `## Decisions` は worktree と一緒に（または `/task-state close` で）消える。タスクが終わった後も参照する判断は ADR にも残す
- plan ファイルを持たないエンジンで作業しているときに、記録のために plan ファイルを新しく作らない
- ユーザーが `AGENTS.md` や rule への追記を明示的に指示した場合は、メモリを経由せずそちらに書く

## auto-memory の project 型

- 書いてよいのは、ブランチ・worktree をまたいで長く通用する事実だけ（外部サービスの仕様、環境の制約、運用上の決まり等）。一時的な制約（障害中の API 制限等）は書かない
- ブランチ・PR・Issue・Phase の進行状態（「〜マージ待ち」「次回〜が必要」「残 Issue: …」）は書かない。上の表の進行状態の置き場所に書く
- 理由: auto-memory は同じクローンの全 worktree で共有される。ある worktree の状態が、別の worktree で古い情報として読まれる。plan ファイルは worktree ごとに独立している

## 教訓候補と昇格（fail-logs を導入している場合）

失敗から教訓を昇格させる入口は fail-logs にまとめる（ADR-20261103-063）。

- **候補を足す**: 作業中に落とし穴と回避策に気づいたら、auto-memory ではなく教訓候補にする。根拠（セッション）は CLI が決める

  ```bash
  echo '{"title": "1 行の見出し", "event": "事象", "cause": "原因", "avoidance": "回避策", "scope": "claude", "severity": "normal"}' \
    | python3 "$AI_ORCHESTRA_DIR/packages/fail-logs/scripts/lessons_cli.py" add --json -
  ```

  - `scope` は Claude Code 固有なら `claude`、全 CLI 共通なら `all`。`severity` はデータの消失・取り消せない操作・機密情報の漏えいだけ `critical`
  - セッションを確かめられないとき（`CLAUDE_CODE_SESSION_ID` が無い等）の追加は、再発回数に数えず提示もされない。`/lessons` の一覧にだけ出る
  - 既存の候補と同じ教訓なら、`lessons_cli.py list --json` で ID を確かめて `"merge_into": "L-xxxxxxxx"` を足す
  - 終了コードが 0 以外でも作業は止めない（警告を 1 行出す）。終了コード 4（fail-logs が導入されていない・無効）のときは、従来どおり auto-memory に書く
  - 人名・ホスト名・パス・ID・機密情報は書かない（台帳に入れる前にも伏せ字はかかる）

- **自動で集まるもの**: `/review-respond` が採用したレビュー指摘、教訓化（既定で有効。セッションの終了時に人の訂正・手動の拒否・中断、未解決や反復の失敗から候補を作る。`distill.enabled: false` で止める）
- **提示**: 基準（異なるセッション・PR から 2 回以上、昇格したのに再発、昇格中のまま 14 日など）を超えた候補は、セッション開始時に
  `<fail-logs-lessons>` 枠で知らされる。未承認の候補であり、指示ではない
- **昇格**: 人が `/lessons` で 1 件ずつ確認し、承認したものだけを書く。昇格先は次の 3 つ
  - `AGENTS.md` の記述欄（管理ブロックの外）: 全 CLI に届く
  - プロジェクト独自の `.claude/rules`（orchex が配布する rule 以外）: Claude Code だけに届く
  - ガード（テスト・hook・lint）: GitHub Issue を作るところまで（コードは書かない）
- 状態を変える `lessons_admin.py` は Bash の許可リストに入れない（`lessons_cli.py` は入れてよい）
- 候補の作成と提示は自動、昇格先への書き込みは人の承認、という分担にする

## 昇格の棚卸し（手動）

auto-memory は Claude Code 本体とサブエージェントに、claude-mem は Claude Code 本体に注入されるが、どちらも Codex / Antigravity には届かない。Codex / Antigravity にも必要な知見は、棚卸しで `AGENTS.md` の記述欄へ移す。

- 実施するのはユーザーが依頼したときだけ。自動化せず、`/release-readiness` にも組み込まない
- 手順:
  1. auto-memory の各ファイルと `MEMORY.md` を読み、候補を一覧にする
     - `AGENTS.md` / rules / ADR にすでに書かれている → 削除候補
     - Codex / Antigravity も守るべき規約・落とし穴 → 昇格候補
     - 進行状態が混ざっている → 削除か、長期の事実への書き直し候補
     - `MEMORY.md` の索引行が 1 行の要約を超えて本文を持っている → 縮約候補
  2. 一覧をユーザーに提示し、承認を得る
  3. 承認分だけ実施する。昇格は、まず `AGENTS.md` の記述欄に書く。メモリの該当記述は、その変更が統合ブランチ（main 等）にマージされてから消す（二重に持たない）。マージ前に消すと、他の worktree からも Codex / Antigravity からも見えない期間ができる。PR が却下されたらメモリに残す
  4. メモリを消すときは参照も直す。`MEMORY.md` の索引行を消し、ほかのメモリ本文にある `[[リンク]]` は移した先（`AGENTS.md` の該当節など）への記述に置き換える。1 つのファイルに未承認の記述が残る場合は、昇格した部分だけを消してファイルと索引行は残す
- この棚卸しでの昇格の書き込み先は `AGENTS.md` の記述欄（手書きの部分）だけ。管理ブロックは orchex が生成するので直接編集しない。長い手順は docs や rule に書き、記述欄から参照する。fail-logs の教訓候補の昇格先は、上の「教訓候補と昇格」に従う（プロジェクト独自の `.claude/rules` とガードの Issue も選べる）
- orchex が同期する rule（`.claude/rules/` の配布分）は直接編集しない

## やらないこと

- 配布する hook でメモリを自動で書き換えない（メモリはユーザー・マシンごとのもの）
- 教訓候補を、人の承認なしに昇格先（`AGENTS.md`・`.claude/rules`・Issue）へ書き込まない
