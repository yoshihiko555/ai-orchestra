# core 評価セット

**パッケージ**: `packages/core`
**類型**: 主: hook 型、副: 共通ライブラリ
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-28（EV-26 を新設。ADR-20260728-046 の root worktree 解決パターンを共通観点として追加。前回レビュー 2026-07-04: precompact-dump / log_common / handoff の未文書化を「文書化すべき」と裁定し Issue #130 へ。Non-Goals の failure_detector 責務境界は今回対象外・現配置のまま）
**情報源**: docs/reference/packages.md（core セクション）, docs/design/architecture.md（4.3 / 5 / 9 章）, .claude/rules/task-memory-usage.md, .claude/rules/context-sharing.md, docs/adr/ADR-20260728-046.md（root worktree 解決パターン、EV-26）
**補助参照（構成要素の列挙のみ）**: packages/core/manifest.json, packages/core/hooks/ 配下のファイル名・docstring 冒頭

## 1. 責務定義

core は全パッケージが依存する共通基盤であり、(1) `Plans.md` によるタスク状態管理（状態マーカーの解析・自動アーカイブ・サマリー注入）、(2) plan gate によるサブエージェント実行フロー制御、(3) `.claude/context/` を介した CLI 間・サブエージェント間のコンテキスト共有（作業ファイル・前回結果の集約と注入）、(4) 全 hook が共有する設定読み込み・JSON I/O・ログ出力ユーティリティ（`hook_common.py` / `log_common.py` / `context_store.py`）を提供する。他パッケージに依存せず、他の全パッケージから依存される最下層のパッケージである。

### Non-Goals

- エージェントルーティングの判断ロジック（`agent-routing` パッケージの責務）
- lint/format の自動実行やテスト品質ゲート判定（`quality-gates` パッケージの責務）
- 失敗イベントの記録・集計そのもの（`fail-logs` パッケージの責務。ただし検知ロジック `failure_detector.py` は物理的に `packages/core/hooks/` に配置されている — 責務境界は情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）
- Codex/Antigravity 呼び出し要否の提案（`codex-suggestions` / `antigravity-suggestions` パッケージの責務）
- MCP プロキシのライフサイクル管理（`cocoindex` パッケージの責務）

## 2. 期待する入出力・副作用

| 構成要素                                                                          | 入力                                                 | 期待する出力                                                                                             | 副作用                                                                                                                                                   |
| --------------------------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `load-task-state.py`（SessionStart）                                              | SessionStart イベント JSON、`.claude/Plans.md`       | WIP タスク一覧 / 次の TODO / blocked タスク一覧のサマリーをコンテキストに注入                            | 全フェーズ `cc:done` のプロジェクトを `.claude/Plans.archive.md` に日付付きで追記し `Plans.md` から除去。`.claude/context/`（session/, shared/）を初期化 |
| `set-plan-gate.py`（PostToolUse: Agent\|Task）                                    | PostToolUse イベント JSON                            | —                                                                                                        | プランゲート状態を設定                                                                                                                                   |
| `check-plan-gate.py`（PreToolUse: Agent\|Task）                                   | PreToolUse イベント JSON                             | ゲート pending 時: exit code 2 でツール呼び出しをブロック                                                | 実装系エージェント呼び出しの中断（core hook 群で唯一 fail-open ではなく意図的にブロックする）                                                            |
| `clear-plan-gate.py`（UserPromptSubmit）                                          | UserPromptSubmit イベント JSON                       | —                                                                                                        | プランゲート状態を解除                                                                                                                                   |
| `inject-shared-context.py`（PreToolUse: Agent\|Task）                             | PreToolUse イベント JSON（`tool_input.prompt` 含む） | 直近のサブエージェント結果＋working-context を `[Shared Context]` 形式で prompt 末尾に付加               | なし（読み取りのみ）                                                                                                                                     |
| `capture-task-result.py`（PostToolUse: Agent\|Task）                              | PostToolUse イベント JSON（`tool_response` 含む）    | —                                                                                                        | 結果サマリー（先頭 2000 文字）を `session/entries/{agent_id}_{timestamp}.json` に書き出し                                                                |
| `update-working-context.py`（PostToolUse: Edit\|Write）                           | PostToolUse イベント JSON（`file_path` 含む）        | —                                                                                                        | 変更ファイルパスを `working-context.json` の modified_files に追記（`.claude/` 配下は除外）                                                              |
| `cleanup-session-context.py`（SessionEnd）                                        | SessionEnd イベント JSON                             | —                                                                                                        | `session/` ディレクトリと `working-context.json` を削除                                                                                                  |
| `precompact-dump.py`（PreCompact）                                                | manifest.json にのみ記載                             | 未文書化（2026-07-04 裁定: 文書化すべき）                                                                | 未文書化 → packages.md / architecture.md へ文書化する（Issue #130）                                                                                      |
| `hook_common.py`（util）                                                          | 呼び出し元 hook からの設定名・パッケージ名           | base 設定と `*.local.yaml` を deep_merge した dict                                                       | なし                                                                                                                                                     |
| `context_store.py`（util）                                                        | session / shared エントリーの読み書きリクエスト      | エントリー一覧・working-context dict                                                                     | `.claude/context/` 配下のファイル読み書き（fcntl ファイルロック付き、architecture.md 4.3）                                                               |
| `log_common.py`（util）                                                           | イベント種別・メタ情報                               | —                                                                                                        | 統一イベントログへの書き出し（詳細フォーマット未文書化 → 2026-07-04 裁定: 文書化すべき・Issue #130）                                                     |
| `file_migration.py`（util）                                                       | source_path / destination_path / max_bytes / writer コールバック | —                                                                                                        | claim rename → 有界 tail 決定 → writer 呼び出し → 確定 rename（fail-logs / skill-evolution の旧ログ移行処理が委譲する共通プリミティブ。writer の実装は呼び出し側が注入）           |
| `task-memory.yaml`（config）                                                      | —                                                    | `Plans.md` のパス・マーカー定義                                                                          | なし                                                                                                                                                     |
| `preflight` / `startproject` / `task-state` / `design`（skill）                   | ユーザー対話                                         | packages.md 記載の一行責務（計画策定/新規開発協調/Plans.md作成更新/要件設計文書作成）                    | 詳細フローは情報源に記載なく本評価セットの対象外（スキル型チェックリストは別評価セットで扱う）。`checkpointing` は 2026-07-23 に廃止（claude-mem / skill-evolution へ役割移管）                                                           |
| `handoff`（skill、manifest.json のみ記載）                                        | —                                                    | 未文書化（2026-07-04 裁定: 文書化すべき）                                                                | 未文書化 → 文書化する（Issue #130）                                                                                                                      |
| `explain-visually`（skill）+ `verify_page.py`（scripts）                          | 対象（PR/Issue/差分/ファイル/会話中の計画）＋生成 HTML＋`--dom-file`/`--chrome`/`--skip-screenshot` 等の CLI オプション | 図解 HTML の生成、`verify_page.py` による描画検証 JSON（`ok`/`mermaidSources`/`mermaidRendered`/`mermaidReady`/`pageHeight`/`screenshot`/`warnings`）。exit code は正常0/警告2/致命的1の三段階 | HTML ファイル・スクリーンショット（`{html.stem}-shot.png`）の書き込み、一時 Chrome プロファイルディレクトリの作成・削除（[keitakn/engineering-skills](https://github.com/keitakn/engineering-skills) f972ef4a より移植・適応、MIT） |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `load-task-state.py` が SessionStart 時に `Plans.md` の状態マーカー（`cc:TODO`/`cc:WIP`/`cc:done`/`cc:blocked`）を解析し、WIP タスク一覧・次の TODO タスク・blocked タスク一覧をコンテキストに注入する — 根拠: task-memory-usage.md
- [ ] EV-02（正常 / must）: 全フェーズが `cc:done` のプロジェクトは `Plans.archive.md` に日付付きで追記され、`Plans.md` から該当プロジェクトセクション（区切り線含む）が除去される — 根拠: task-memory-usage.md
- [ ] EV-03（境界 / should）: Decisions / Notes セクションは**全プロジェクトが完了した場合のみ**アーカイブへ移動し、一部プロジェクトのみ完了時は `Plans.md` に残存する — 根拠: task-memory-usage.md
- [ ] EV-04（正常 / should）: `cc:blocked` マーカーには `— 理由: {ブロック理由}` の付記があるものとして解析・表示される — 根拠: task-memory-usage.md
- [ ] EV-05（正常 / must）: サブエージェント（Agent/Task）完了の PostToolUse でプランゲートが設定される — 根拠: docs/reference/packages.md, architecture.md 5.1
- [ ] EV-06（異常 / must）: プランゲートが pending の状態で実装系エージェントが呼び出された場合、`check-plan-gate.py` が exit code 2 でツール呼び出しをブロックする — 根拠: architecture.md 5.1 / 5.2
- [ ] EV-07（正常 / must）: UserPromptSubmit（ユーザーの次メッセージ送信）でプランゲートが解除される — 根拠: docs/reference/packages.md, architecture.md 5.1
- [ ] EV-08（正常 / must）: SessionStart 時に `.claude/context/`（`session/`, `shared/`）が初期化される — 根拠: context-sharing.md
- [ ] EV-09（正常 / must）: サブエージェント（Agent/Task）起動前に、直近のサブエージェント結果と working-context が `[Shared Context]`（`## Previous Agent Results` + `## Working Context`）形式で prompt 末尾に注入される — 根拠: context-sharing.md
- [ ] EV-10（境界 / should）: 注入されるサブエージェント結果エントリーは最新 5 件までに制限され、各エントリーの summary は 200 文字にトランケートされる — 根拠: context-sharing.md
- [ ] EV-11（境界 / should）: 注入される modified_files は最新 20 件までに制限される — 根拠: context-sharing.md
- [ ] EV-12（正常 / must）: サブエージェント完了後、結果サマリー（`tool_response` 先頭 2000 文字）が `session/entries/{agent_id}_{timestamp}.json` として書き出される — 根拠: architecture.md 9.2
- [ ] EV-13（正常 / must）: Edit/Write 完了後、変更ファイルパスが `working-context.json` の modified_files に追記される — 根拠: context-sharing.md, architecture.md 9.2
- [ ] EV-14（境界 / must）: `.claude/` 配下のファイル変更は `working-context.json` の modified_files に記録されない — 根拠: context-sharing.md
- [ ] EV-15（正常 / must）: SessionEnd 時に `session/` ディレクトリと `working-context.json` が削除され、セッション間で状態を持ち越さない — 根拠: context-sharing.md
- [ ] EV-30（正常 / must）: `verify_page.py` の `resolve_chrome_path` は Chrome 実行パスを「`--chrome` 明示指定 → 環境変数 `EXPLAIN_VISUALLY_CHROME` → macOS 既定パス（`/Applications/Google Chrome.app/...`）→ PATH 上の候補（`google-chrome`/`google-chrome-stable`/`chromium`/`chromium-browser`、この優先順）→ 見つからなければ None」の優先順位で解決する — 根拠: facets/instructions/explain-visually.md「前提条件」
- [ ] EV-31（異常 / must）: `verify_page.py` の `main` は三段階の exit code を返す。正常終了（`EXIT_OK`=0、warnings 空）、警告あり（`EXIT_WARNINGS`=2、`build_warnings` が非空リストを返した場合）、致命的失敗（`EXIT_FATAL`=1、対象 HTML 不在や Chrome 起動失敗等の `VerificationError` 発生時、stdout に `error` キー付き JSON を出力）— 根拠: facets/instructions/explain-visually.md「4. 検証する」の exit code 表（0=正常終了、1=致命的エラー、2=警告あり）
- [ ] EV-32（正常 / must）: `parse_dom_metrics` はレンダリング済み DOM 文字列から、描画済み `<svg id="fig-N">` の数・未描画 `<pre class="mermaid">` の数・その合計（sources）・`data-mermaid-ready="1"` の有無（ready）・`data-page-height` の値・`<title>` の値を抽出する。空 DOM 入力では全て 0/False/空文字列の既定値を返す — 根拠: facets/instructions/explain-visually.md「トラブルシューティング」表（`mermaidReady`/`mermaidRendered`/`mermaidSources`/`pageHeight` の各症状記述）
- [ ] EV-33（異常 / must）: `build_warnings` は sources が存在するのに ready でない場合、sources と rendered の数が食い違う場合、page_height を取得できなかった場合にそれぞれ独立した警告文を追加し、いずれにも該当しなければ空リストを返す — 根拠: facets/instructions/explain-visually.md「トラブルシューティング」表（`mermaidReady` が false・`mermaidRendered` が `mermaidSources` より少ない・`pageHeight` が取得できない、の各症状と対応）
- [ ] EV-34（境界 / should）: template.html ↔ verify_page.py の契約 — テンプレートは Mermaid の描画に `'fig-' + i` 形式の id を渡し、描画完了時に `dataset.mermaidReady`/`dataset.pageHeight`（レンダリング後の DOM では `data-mermaid-ready`/`data-page-height` 属性になる）を設定するスクリプトブロック、`{{TITLE}}`/`{{BODY}}` プレースホルダ、Mermaid CDN を読み込む `<script type="module">` を保持する。これらが失われると `verify_page.py` の正規表現前提が崩れる — 根拠: facets/scripts/explain-visually/template.html のコメント（「検証スクリプトが...高さを知るための出力。表示には影響しないので削除しない」「Mermaid を使う図が1つも無い場合は...script 要素ごと削除する」）
- [ ] EV-35（異常 / must）: `verify_page.py` の `lint_injected_markup` は生成 HTML 本文に対し、テンプレート由来の `<script>` 数（2個）を超える混入、`on\w+=` 形式のイベントハンドラ属性、`javascript:` スキーム、CSP meta（`Content-Security-Policy`）の欠落、meta refresh、`<base>` / `<iframe>` / `<object>` / `<embed>` / `<form>`、未置換の `{{TITLE}}` / `{{BODY}}` をそれぞれ独立した警告として検出し、`lint_template_integrity` は CSP `content` の完全一致と `<script>` 本文列のテンプレート先頭一致（0〜2個）を検証し、いずれにも該当しなければ空リストを返す — 根拠: facets/scripts/explain-visually/template.html の CSP meta コメント（「script を編集したら再計算」）と実装挙動（facets/scripts/explain-visually/verify_page.py の lint_injected_markup / lint_template_integrity）
- [ ] EV-36（異常 / must）: `verify_page.py` の `_validate_html_path` は、対象 HTML 自体またはその祖先ディレクトリが symlink の場合、および解決後のパスがカレント作業ディレクトリ配下に無い場合に致命的エラー（exit 1、JSON `error`）で Chrome を起動しない — 根拠: facets/instructions/explain-visually.md「3. HTMLを作る」の出力先 symlink 拒否・解決後パスの範囲チェック（`verify_page.py` も同じ検査を行い致命的エラーにする）

## 4. 類型別観点

<!-- docs/evaluation/README.md の hook 型チェックリストを core の実情で具体化する -->

- [ ] EV-16（境界 / must）: stdin/stdout 契約 — 各 hook は Claude Code の hook イベント JSON を stdin から受け取り、コンテキスト注入結果は `hookSpecificOutput.additionalContext` 形式で返す（Claude Code hook 仕様に準拠） — 根拠: architecture.md 5.2
- [ ] EV-17（異常 / must）: fail-safe 方針 — `check-plan-gate.py` を除く全 core hook は内部例外発生時に exit code 0 で正常終了しセッション/ツール実行をブロックしない（fail-open）。`check-plan-gate.py` のみ意図的に exit code 2 でブロック可能な設計上の例外である — 根拠: architecture.md 5.2
- [ ] EV-18（境界 / should）: 冪等性 — 全フェーズ完了プロジェクトの自動アーカイブは、初回実行で `Plans.md` から該当セクションが除去済みのため、同一セッション内で再実行しても二重アーカイブされない（構造的冪等性） — 根拠: task-memory-usage.md
- [ ] EV-19（正常 / must）: config 駆動 — `hook_common.load_package_config()` は `{package}/{name}.yaml`（base）と `{name}.local.yaml`（local）を deep_merge し、local の値が base を上書きする — 根拠: architecture.md 4.3
- [ ] EV-20（境界 / should）: config 駆動（部分上書き） — base 設定にのみ存在するキーは local が存在してもそのまま base の値が使われる（local はキー単位の上書きであり全置換ではない） — 根拠: architecture.md 4.3
- [ ] EV-21（正常 / must）: フェーズに `#### Acceptance Criteria` の未チェック `- [ ]` 行が残る場合、当該プロジェクトはアーカイブされない。フェーズ見出しが `cc:done` でも同様（AC が優先） — 根拠: task-memory-usage.md
- [ ] EV-22（境界 / must）: AC セクションのないフェーズは従来通り `cc:` マーカーのみで完了判定される（後方互換） — 根拠: task-memory-usage.md
- [ ] EV-23（正常 / should）: タスク全て `cc:done` かつ AC 全て `[x]` のプロジェクトはアーカイブされる — 根拠: task-memory-usage.md
- [ ] EV-24（境界 / should）: AC チェックリスト行（`- [ ]` / `- [x]`）はタスクサマリー注入の対象にならない（`cc:` マーカー行のみ注入） — 根拠: task-memory-usage.md
- [ ] EV-25（正常 / must）: `#### Acceptance Criteria` セクションを持つフェーズは、フェーズ見出しが `cc:done` でも配下に `cc:done` 以外のマーカーを持つタスク行が残っていればアーカイブされない（AC なしフェーズは従来通り見出しマーカーで判定＝後方互換） — 根拠: task-memory-usage.md
- [ ] EV-26（正常 / must）: root worktree 解決の共通関数 — git worktree 環境では root worktree の絶対パスを返し、通常リポジトリではリポジトリルートを返し、git 実行不能・結果不正時は None を返して呼び出し側フォールバックを可能にする（audit の `event_logger.py` はこの共通関数への委譲ラッパーとして実装済み。実装パターンの共通化ではなく、audit 固有の解決ロジックそのものが core への委譲に置き換わっている） — 根拠: docs/adr/ADR-20260728-046.md
- [ ] EV-27（正常 / must）: 有界移行プリミティブの claim/確定 契約 — `file_migration.migrate_bounded_file` は destination と source の実体が同一なら no-op、そうでなければ `<source>.migrating.<pid>-<monotonic_ns>` へ原子的 rename して排他 claim し、writer が正常完了した場合に限り同一 suffix の `<source>.migrated.<suffix>` へ確定 rename する。ファイルサイズが `max_bytes` を超える場合は行境界を壊さず末尾 `max_bytes` に収まるよう先頭側の途中行のみを読み捨てる（cut-1 バイト目が改行かどうかで判定） — 根拠: 実装挙動（packages/core/hooks/file_migration.py docstring）
- [ ] EV-28（異常 / must）: stale claim の非破壊と writer 例外の伝播 — rename による claim 取得に失敗した場合（既に他プロセスが claim 済み、または stale な `.migrating.*` が残存する場合を含む）は何もせず return し、既存の `.migrating.*` ファイルには一切触れない。writer が例外を送出した場合はその例外を呼び出し元へ伝播し、claim は `.migrating.*` のまま残す（握りつぶさない。fail-open にするかどうかは呼び出し側ラッパーの責務） — 根拠: 実装挙動（packages/core/hooks/file_migration.py docstring）
- [ ] EV-29（正常 / must）: 書き込み方式の注入分離 — 実際の書き込み方式（flock の要否・write の分割方針・fchmod の有無等）は `writer: Callable[[BinaryIO, str], None]` として呼び出し側が注入する。fail-logs（複数 write を許容する flock 排他下のストリームコピー）と skill-evolution（単発 write + short-write リカバリ、flock なし）は整合性前提が異なったまま `migrate_bounded_file` を共通の薄いラッパーとして利用し、共通化によって互いの整合性前提を壊さない — 根拠: 実装挙動（packages/core/hooks/file_migration.py, packages/fail-logs/hooks/log_migration.py, packages/skill-evolution/lib/skill_evolution_common.py）
- N/A: 秘匿情報（マスキング） — core が扱う注入対象（ファイルパス・タスクサマリー・エントリー summary）に対する秘密情報マスキング処理は情報源に記載がなく、該当する取り扱い自体が定義されていないため対象外
- N/A: 性能 — 同期 hook（特に SessionStart の `load-task-state.py`）がセッション開始を遅延させないための定量的な性能要件が packages.md / architecture.md / task-memory-usage.md / context-sharing.md のいずれにも記載がないため、本評価セットでは具体化しない
- N/A: `explain-visually`（skill 型サブコンポーネント） — `verify_page.py` 自体は非対話の CLI であり、対話規約（AskUserQuestion）やルーティング尊重（cli-tools.yaml）はスキル指示書（explain-visually.md）側の責務であるため、hook 型チェックリストの対話系・ルーティング系項目は該当しない。EV-30〜EV-36 は同スクリプトの入出力契約を個別観点として扱う

## 5. テストレビュー判断基準（パッケージ固有）

- Plans.md の自動アーカイブに関するテストは「一部プロジェクトのみ完了」（EV-03 の境界）と「全プロジェクト完了」の両ケースを分けて検証しているか確認する。単一ケースのみのテストは gap として扱う
- `check-plan-gate.py` の exit code（0 か 2 か）が明示的にアサートされているか確認する。fail-open 原則からの逸脱は critical 相当のバグとみなす
- `context_store.py` は fcntl ファイルロック付きと明記されている（architecture.md 4.3）が、ロック競合時の具体的挙動（待機/エラー等）は情報源に記載がない。ロック競合ケースをテストする場合、期待値が実装追認になっていないか重点確認する
- 注入・保存時のトランケーション境界値（5 件目/6 件目、200 文字/201 文字、2000 文字/2001 文字、20 件目/21 件目）をテストしているか確認する。境界値を跨がないテストは EV-10/EV-11/EV-12/EV-14 の観点をカバーしたとみなさない
- `file_migration.py`（EV-27〜EV-29）の claim/rename 契約は、fail-logs / skill-evolution 側の既存テストが通ることのみで代替せず、ちょうど `max_bytes` 境界・改行直後の cut・非改行の途中行 cut・writer 例外発生時に claim（`.migrating.*`）が残存すること・stale claim に触れないことを独立に検証しているか確認する
- `verify_page.py`（EV-30〜EV-36）のテストは Chrome を実際に起動しない範囲（`resolve_chrome_path`/`parse_dom_metrics`/`build_warnings`/`main` の `--dom-file`+`--skip-screenshot`）に限定されているか確認する。`dump_dom`/`screenshot`/`run_chrome` の実 Chrome 起動を要する経路は本評価セットの対象外であり、無理に CI で Chrome を起動させるテストを追加していないか（環境依存で不安定になるため）を重点確認する
