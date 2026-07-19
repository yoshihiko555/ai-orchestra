# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`meta-harness`: proposer が routing config patch を提案可能に（Phase A）**: proposer 生成候補が `agent-routing/cli-tools.yaml` の `agents.*.tool` / `antigravity.model` を patch できるようになった（`codex.model` は human 限定のまま）。reward hacking 対策として quality 厳密優越・クロススキル回帰ゲート・レート制限等を同梱（ADR-20260717-040）。

### Fixed

- **`meta-harness`: Docker capability gate と実 judge コンテナが出力トークン上限を適用しておらず broker 予算超過を招いていた不具合を修正**: capability smoke コンテナと judge（`judge.tool: claude-bare`）コンテナの双方に `scenario_run.max_output_tokens_default`（既定 4096、`null` 明示時もフォールバック）を適用し、broker の worst-case 予算チェックによる評価不能を解消した。
- **`evaluation-set-checker` が `packages/` 配下に実体を持たない SSOT（orchex CLI 等）のテストを識別できない不具合を修正**: 評価セット ID とテストパスの明示マッピング（`.claude/config/quality-gates/evaluation-set-mapping.yaml`）を追加し、`test_orchestra_manager_core.py` が無関係な `core` パッケージへ誤誘導される問題も解消した。
- **`orchex uninstall --dry-run` が最後のパッケージ削除時に `settings.local.json` を書き換えていた不具合を修正**: dry-run では実ファイルを一切変更せず、プレビュー表示のみ行うようにした。
- **`orchex enable` が未インストールのパッケージにもフックを登録していた不具合を修正**: 対象パッケージが `install` 済みでない場合はエラーを表示し、フック登録を行わないようにした。
- **`orchex install` でユーザー変更済み config ファイルが上書きされ得る不具合を修正**: 配布時ハッシュとの比較で保護されたはずの config ファイルが、同一 `install` 実行内の後続同期処理で再上書きされないようにした。
- **ユーザー編集済み agent ファイルが再同期（SessionStart / 再インストール）で上書きされ得る不具合を修正**: config ファイルと同じ配布時ハッシュ比較ガードを agents ファイルにも適用した。
- **`orchex proxy stop`/`proxy status` の cocoindex 未導入判定を修正**: 判定基準をプロジェクトの `installed_packages` に基づくものに変更し、未導入時に確実にエラーとなるようにした。
- **`codex-suggestions` が `cli-tools.yaml` に `codex` セクション未定義でも発火していた不具合を修正**: `check-codex-before-write` / `check-codex-after-plan` は設定未定義時 `codex.enabled` を `false` 扱いにし、明示的に有効化しない限り提案しないようにした。さらに `agent-routing` を導入していないプロジェクト（project-local な `cli-tools.yaml` が存在しない）では、パッケージ同梱のフォールバック設定を「明示的な有効化」とみなさず提案しないようにした。

### Changed

- **`meta-harness`: 評価/judge の既定モデルを Sonnet に pin し、broker 予算上限価格を再較正**: `evaluate.model` / `judge.model` が未指定（セッション既定モデルに依存、Opus tier になり得た）から `claude-sonnet-5` 明示 pin に変わり、broker の `pricing_upper_bound_usd_per_million` 既定値も Sonnet 単価へ引き下げた。broker の model allowlist（`evaluate.isolation.broker.model_allowlist`）で candidate が pin より高価なモデルを指定して過小コスト計上する迂回を fail-closed で防ぐ。この再較正はコスト比較可能性に影響するため、旧価格下で評価済みの routing-config / facet 候補は evaluator hash が stale 判定となり再評価が必要になる。
- **`meta-harness`: scenario/judge/capability smoke コンテナで Claude Code の 1M context beta を無効化**: 3 経路すべての claude CLI 起動に `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` を設定し、premium 価格や毎ターン大量 cache 生成による予算前課金の乖離を抑制した。
- **`meta-harness`: skill 回帰シナリオスイート追加に伴い回帰予算上限を引き上げ**: `regression.max_affected_suites`（5→7）/ `regression.max_budget_usd`（54.0→78.0）の既定値を、新規 skill suite（`issue-fix` / `task-state`）の追加分に合わせて再計算した。
- **`meta-harness`: frontier の既定コスト軸を USD コストへ変更**: 全 target の `frontier.cost_axis` 既定値を `total_tokens` から `total_cost_usd` に変更したため、既存候補の frontier 順序が変わる場合がある。選択したコスト field を欠く run は従来どおり fail-closed し、purge 後の再評価が必要。

- **`image-generation`: `codex.enabled: false` 時は画像生成を実行しないように変更**: `/image-gen` スキル・`image-generator` エージェントが `cli-tools.yaml`（+ `.local.yaml`）の `codex.enabled: false` を尊重し、無効時は画像生成を行わず「利用不可」を報告するようになった。

- **`orchex setup all` から tmux-monitor を除外（opt-in 化）**: presets.json の `exclude` キーで除外した。必要な場合は `orchex install tmux-monitor` で明示的に導入する。

### Removed

- **`.claudeignore` の配布・生成を廃止**: `orchex setup` と SessionStart 同期で `.claudeignore` を作成しないようにした。除外設定は `.gitignore` で管理する。

### Added

- **meta-harness routing-config target**: human-registered candidates can now patch `agent-routing/cli-tools.yaml` keys (`agents.*.tool`, `codex.model`, `antigravity.model`) through the evaluation/promotion pipeline. Proposer candidates remain facets-only.

- **`loop-harness`: Maker commit 書き戻しの安全停止理由を追加**: 一時 ref への import 失敗、非 fast-forward、CAS 競合を `git_ref_import_failed` / `git_ref_not_fast_forward` / `git_ref_cas_rejected` として区別できるようにした。

- **`loop-harness`: LP-2 Docker 隔離用のイメージライフサイクル設定を追加**: recipe hash による再利用、保持世代数、専用 buildx builder の BuildKit cache GC を設定できる。隔離実行の既定値は引き続き `none`。

- **`meta-harness`: skill target の共有 facet 改善をクロススキル回帰評価で保護**: `regression.enabled: true` を既定化して composition closure 内の共有 facet を候補 overlay に許可し、影響 skill の train/holdout critical を evaluation batch 単位で hard gate する。suite 不在の影響先は PR 警告へ記録し、回帰コスト・suite 数・impact freshness も昇格前に検証する。

- **`loop-harness`: 利用者ガイド（図解つき）を追加**: `docs/guides/loop-harness.md` に `/loop-issue` の使い方・ループの仕組み（全体フロー・状態機械・PR レビュー反復・停止判定の Mermaid 図解）をまとめた。`packages/loop-harness/README.md` から導線を追加し、cron セットアップの記述を `is-alive`（pidfile/flock）方式に更新した。
- **`meta-harness`: `target=skill:<slug>` の探索・評価に対応**: skill ごとの scenario suite、target 別 frontier、composition closure に限定した安全な overlay、`handoff` / `issue-create` の train・holdout 評価を追加。skill-evolution の trigger 出力から `orchex meta propose` へ疎結合で誘導する。
- **`git-workflow`: `/review-respond` スキルを追加**: カレントブランチの PR に付いた bot レビュー指摘（CodeRabbit/Codex 等）を `pr_review_threads.py` で取得し、分類・修正・push・返信・スレッド解決までを単発実行で自動対応する。

- **`meta-harness`: `orchex meta loop`（Phase 3）を追加**: `propose` と `evaluate` を ledger 駆動で自動反復し、予算・反復上限・発散・収束で停止する。`--resume` は中断時の孤児候補を含む状態を ledger から復元する。

- **`meta-harness`: Docker 隔離 scenario 実行を解禁**: `orchex meta evaluate` / `loop` の既定実行 backend を Docker に変更し、internal network の候補コンテナと run-scoped OAuth broker を使って実資格情報を候補へ渡さず scenario・oracle・tool-less judge を実行する。Docker daemon・pin 済みイメージ・broker が利用できない場合は worktree 作成前に明示エラーで停止し、非隔離 backend へは降格しない。

- **`loop-harness`: LP-2 常駐トリガー（無人ループ実行）を追加**: ラベル付き Issue を検出し、無人（`claude -p`）でループを最後まで自律駆動する常駐運用（`cron`/`launchd` 登録）を追加した。実行状況の確認・不要データの掃除は `loop_status.py`（`list`/`show`/`purge`）で行い、Maker は push/PR 作成ができない構造になっている。

- **`loop-harness`: `/loop-issue` LP-1 スキルを追加**: Issue の実装・決定論的 Checker・PR レビュー対応を two-phase 契約で反復し、成功／失敗／安全停止の出口まで自律駆動する facet スキルを配布する。

- **`loop-harness`: PR レビュー待機・指摘取り込みの決定論モジュールを追加**: `pr_review_wait.py` で reviewer allowlist 必須検証、完了シグナル待機、severity 判定・分類結果の state 反映、肯定コメント除外、dedup を扱えるようにした。`checkrun_allowlist` / `severity_markers` / `dedup.*` 設定も追加。

- **`loop-harness`: LP-1 向け `loop_step.py` CLI を追加**: `start` / `attach` / `propose` / `complete` / `reconcile` / `heartbeat` / `resume` を JSON 出力と exit code 0/1/2/3 で利用できるようにした。あわせて `loop_start` / `loop_iteration` / `loop_stop` の audit event と checker artifact 保存に対応した。

- **`loop-harness`: 反復ループ基盤の core パッケージを追加**: Phase 1 として loop 定義 loader、state/journal/lock の決定論的コア、worktree 命名ユーティリティ、既定 config を追加。CLI・スキル配線・LP-2 常駐実行は後続フェーズで追加予定。

- **`meta-harness`: population ベースのハーネス最適化基盤（Phase 1a: 計測基盤）を新設**: 候補ハーネス（facet ソースへの宣言的オーバーレイ）の登録・append-only 台帳・品質×コストの Pareto frontier 算出を行う `orchex meta` サブコマンド群（`init` / `register` / `frontier` / `status` / `purge`）を追加。ストアは worktree の寿命に依存しないメインルート配下 `.claude/meta-harness/` に永続化する。評価実行（evaluate）以降は Phase 1b 以降で追加予定。設計は `docs/design/meta-harness.md`（基本）/ `docs/design/meta-harness-detailed.md`（詳細）を参照
- **`meta-harness`: `orchex meta evaluate`（Phase 1b: evaluator）を追加**: 候補ハーネスを使い捨て worktree に実体化し、`claude -p` ヘッドレス実行 → oracle 判定（`command_exit` / `artifact_exists` / `rubric_judge`）→ 台帳記録までを自動実行する。CLI capability gate（バージョン pin + フラグ smoke test、fail-closed）、evaluate.lock（PID + heartbeat）、self-report 注入とペナルティ、baseline シナリオ 2 本を同梱。LLM judgeはtool-lessな`claude --bare`を既定とし、read範囲を制限できないCodex backendはfail-closedする
- **`meta-harness`: proposer 隔離設定（Phase 2 M1）を追加**: `proposer.tool` と `proposer.isolation.*`（srt backend / version pin / allowRead 追加）を導入
- **`meta-harness`: `orchex meta propose`（Phase 2 M4）を追加**: filtered view を srt 隔離 backend 内の proposer（既定 codex）へ渡し、構造化 proposal を検証して候補登録する。無効 proposal は候補登録せず `rejected/` に診断保存する。`proposer.timeout_seconds` で codex backend の wall-clock timeout を制御し、timeout 時はプロセスグループごと強制終了する。proposal 登録イベントには codex stdout 由来の `tokens_used` も記録する
- **`meta-harness`: `orchex meta promote`（Phase 2 M5）を追加**: frontier 上の候補を予約して promotion worktree に適用し、facet/context build と任意の `promote.verify_command` を通した上で PR を作成する。`--confirm` は PR merge と main 到達を検証した場合のみ `promoted` 遷移を記録する
- **`meta-harness`: proposer 出力経路の資格情報検知（Phase 2 L2/L3）を追加**: 候補登録時に proposal・overlay を走査し、staged auth の canary（L2）や `sk-`/JWT 等の汎用 secret（L3）を検出したら登録を拒否して台帳へ `proposer_security_violation` を記録する。L3 スキャンは `promote` 前提条件でも再実行する（検知層であり主対策は認証情報の最小化）

### Fixed

- **`loop-harness`: 死んだ scheduler が cron から復旧しない問題を修正**: 常駐 scheduler の cron 生存確認を、cron ラッパー自身のコマンド文字列に誤って一致しうる `pgrep -f` の正規表現マッチから、pidfile への `flock` に基づく `is-alive` チェックへ変更した。scheduler は起動時に自身で pidfile をロックするため、二重起動は cron/launchd/手動起動のいずれからでも確実に防止される。
- **`loop-harness`: 外部レビューが Low/Nitpick のみでも PR レビュー対応ループが無進捗失敗する問題を修正**: 実質的な指摘（Critical/High）が無いにもかかわらず Draft 化されていた不具合を修正した。Low/Medium の指摘は合格をブロックしない非ブロッキング扱いとし、残存分は成功時の Issue コメントに一覧で記録する。
- **`loop-harness`: 遅れて届いた新規の Critical/High 指摘が「無進捗」と誤判定される問題を修正**: 修正が正しく進んでいても、前回反復から新規の重大指摘が 1 件見つかっただけで反復せず失敗していた不具合を修正した。

- **`meta-harness` / `skill-evolution`: 無効な候補・target 入力で CLI が例外終了する問題を修正**: validation error として終了コード 2 を返し、skill 改善提案には対象プロジェクトを明示するようにした。

- **`reverse`: Python 3.12 でスキャンスクリプトが失敗する問題を修正**: Python 3.13 で追加された `pathlib` API への依存を除き、シンボリックリンクを辿らない既存挙動を維持したままサポート対象の Python 3.12 で実行できるようにした。

- **`meta-harness`: Docker broker が Claude CLI の既知トラフィックを拒否する問題を修正**: `?beta=true` と pin 済み CLI の client beta を allowlist で中継し、`/messages` と `/messages/count_tokens` の重なりを同時 upstream 1件のまま直列化して、Docker backend の scenario 実行が正常に完走するようにした。

- **`cli-tools.yaml` の旧 `gemini.enabled: false` が明示設定済みの `antigravity.enabled` を無条件に上書きしていた問題を修正**: 両キーが競合する場合は `antigravity.enabled` を優先するようにした。旧 `gemini.enabled: false` は `antigravity.enabled` が未設定の場合のみ後方互換フォールバックとして働く。base 設定が `antigravity.enabled` を既定で明示している通常の移行済みプロジェクトでも、`.local.yaml` の旧 `gemini.enabled: false` だけによる無効化が正しく機能するようにした。

- **`loop-harness`: `/loop-issue` の Maker に編集不能ロールが選ばれて反復が進まない問題を修正**: `debugger` を含む `issue-loop` の auto Maker 候補を実装可能ロールの allowlist に限定し、初回に選定した Maker を state に保存して実装反復・PR レビュー対応で一貫して再利用するようにした。custom loop のフェーズ固有 Maker と変更前の completed journal の reconcile は後方互換を維持する。

- **`loop-harness`: CodeRabbit のレート制限を無進捗失敗として扱う問題を修正**: 信頼済みのレート制限応答を検知し、CodeRabbit だけの構成では即時、Codex 等の代替レビュー経路がある構成では既存 timeout まで待ってから、人間の確認・マージ判断へ安全に引き継ぐようにした。
- **`loop-harness`: severity 分類を挟む PR レビュー取り込みで指摘が欠落する問題を修正**: 明示 severity の指摘と分類が必要な指摘が同時に届いた場合でも、action と lease に安全に束縛した snapshot で結果を引き継ぎ、分類後に一部の指摘が消えて誤って合格扱いにならないようにした。

- **`quality-gates`: hook 状態ファイルの保存規約を統一し、worktree 分離と排他制御を追加**: `test-gate-checker.py` / `post-test-analysis.py` / `post-implementation-review.py` / `test-tampering-detector.py` の状態ファイルを、全プロジェクト共有だった `/tmp/claude-*.json` から `.claude/state/`（`evaluation-set-checker.py` と同じ規約、worktree = project_dir 配下に閉じ込め）へ移行した。あわせて `test-tampering-detector.py` の状態更新に flock 排他ロックを追加し、他 3 hook と同じ保護レベルに揃えた。

- **`loop-harness`: bot の自動生成サマリコメントが phantom high 指摘として取り込まれる問題を修正**: CodeRabbit 等が投稿する非 actionable ステータスコメント（本文中に `High` 等の語を偶然含む）が explicit high severity の指摘として誤って取り込まれ、対応実体が無いまま `exit_success` に到達できなくなる不具合を修正した。`pr_review.auto_generated_markers`（config）で指定したマーカーを含むコメントは severity 判定前に除外される。

- **`loop-harness`: PR レビュー再ベースライン時に未取り込みの信頼済み指摘が失われる問題を修正**: 追加 commit を push する直前の re-baseline が、直前反復の作業中に届いた別レビュアーの指摘を「処理済み」として取り込む前に握りつぶしてしまう不具合を修正した。re-baseline の前に必ず一度取り込み（drain）を行い、指摘が残っている場合は push・再ベースラインを行わず修正反復へ差し戻すようにした。

- **`loop-harness`: Codex 等の issue コメント形式のレビュー応答が完了として検知されない問題を修正**: `@codex review` のようなコマンド応答が正式な GitHub review ではなく issue コメントとして投稿された場合、レビューが完了しているにもかかわらず毎回タイムアウトまで待機していた不具合を修正した。発信元検証済み・baseline 以降・非自動生成・終局判定文言に一致する issue コメントを完了シグナルとして扱うようにした。あわせて CodeRabbit のコマンド応答マーカーを自動生成コメント除外の既定リストに追加した。

- **`loop-harness`: linked worktree からループ実行時にプロジェクト固有設定が反映されない問題を修正**: loop worktree（`git worktree add` で作成した作業ディレクトリ）から実行した場合、`.claude/config/loop-harness/loop-harness.local.yaml` が root worktree 側にしか存在せず、上書き設定が無視されていた不具合を修正した。

- **worktree からの install/init がグローバル参照先を上書きする問題を修正**: 同じ Git リポジトリの linked worktree から実行した場合、`~/.claude/settings.json` の既存 `AI_ORCHESTRA_DIR`（main worktree）を保持するようにした。

- **`meta-harness`: ストア用 `.gitignore` エントリが SessionStart 同期で消える問題を修正**: `.claude/meta-harness/` を gitignore 管理ブロックの生成元（`gitignore_sync.py`）に追加し、同期のたびに手動追記が失われる Phase 1a の実装漏れを解消
- **`meta-harness`: `orchex meta propose` の Codex 起動失敗を修正**: srt 隔離下で repo 内 `proposal.schema.json` が `denyRead` に遮断されないよう schema を ephemeral `CODEX_HOME` へ staging し、非 secret の `models_cache.json` / `version.json` だけを staging するようにした。構造化出力時の streaming 通信は Codex backend に限り srt の TLS 終端から除外し、proposal schema は OpenAI structured output 互換に調整した

- **`loop-harness`: `start` 直後にセッションが断絶したループを復旧できない問題を修正**: 初回 `run_maker` の pending 化直後（`status=pending`）にセッションがクラッシュすると、`attach` が `pending` を拒否し `resume` も対象外のため復旧経路が無く、state ディレクトリを手動削除して journal を失いながら `start` をやり直すしかなかった。`attach` が `pending` も受理し、同一 `loop_id`・journal を維持したまま復旧できるようにした。

### Security

- **`reverse`: `generate-mermaid.py` の `escape_label` が改行・制御文字を素通しする問題を修正**: 解析対象コードベース由来のモジュール名/ラベルに改行を仕込むことで Mermaid ノード定義を複数行に分割し構文注入できる問題を修正した。`sanitize_cluster_name` と同様に制御文字を除去し、ノード定義が単一行を保つようにした。

### Changed

- **Codex の既定モデルを `gpt-5.6-sol` に更新**: エージェントルーティング、設定読込失敗時のフォールバック、新規導入用 `.codex/config.toml` テンプレートを同じモデルに揃えた。

- **`codex-harness`: 旧 `project-edit` profile のアップグレード移行を追加**: 過去に同期済みの harness 所有 profile だけを限定検出して `.codex/config.toml` から削除し、Issue #161 の制限が既存導入先に残り続けないようにした
- **`codex-harness`: force-push と approval bypass 形を禁止**: branch push / PR 作成の plain 形は引き続き人間承認に委譲しつつ、force-push と option 挿入で native prefix rule を迂回する形は hook で block する
- **`codex-harness`: 非対話 runner の validation trust を pre-run snapshot 化**: workspace-write 実行中に validation 設定と台帳を同時改変しても、実行前 hash snapshot と不一致なら validation を実行しない
- **`loop-harness`: `pr_review_response` で Maker が変更を作らなかった場合の無進捗検知を高速化**: Maker が新規 commit を作らなかった反復では、外部レビュー待機（最大 60 分のポーリング）をスキップし、決定論的な commit sha 比較で即座に無進捗判定するようにした。行き詰まり検知までの時間が約 2 時間 → 数分に短縮される。
- **`codex-harness`: 対話 Codex を「承認ベース」に緩和（Issue #161）**: 対話 `codex` 向けの既定 `approval_policy` を `on-request` → `on-failure` に変更。sandbox が拒否した操作（git worktree の実体 Git dir への書き込み＝ `git add`/`git commit`、`gh`/`git fetch` 等のネットワーク）を人間承認で実行できるようになった。非対話 runner（`codex_run` / `codex_review`）は `approval_policy=never` を明示指定し、従来どおり承認なしの厳格 sandbox を維持する
- **`codex-harness`: `git push` / `gh pr create` を rules で `prompt`（人間承認付き許可）に緩和**: 従来のハードブロック（`forbidden`）から、対話時に人間が承認すれば実行できる `prompt` へ変更。`gh pr merge` / `gh release create` / `npm`・`pnpm publish` / `docker push` / `kubectl apply` / `terraform apply` / `rm -rf` 系は引き続き `forbidden`（承認不可）を維持

### Fixed

- **`codex-harness`: 対話 Codex が git worktree 内で `git add` / `git commit` に失敗する問題を解消（Issue #161）**: worktree の実体 Git dir が作業ディレクトリ外にあり sandbox 書き込みが拒否されていたが、承認ベース緩和により通常の Git ワークフローが実行できるようになった。あわせて `gh` / `git fetch` 等のネットワーク遮断も承認で通せるようになった

## [0.2.11] - 2026-07-05

### Added

- **スキルフロー評価セット（`docs/evaluation/skills/`）の新設**: 評価セットを二層構造に整理。パッケージ評価セット（pytest で強制、従来どおり `docs/evaluation/` 直下）と分離して、facets 由来スキル群の「あるべき振る舞い」をフロー単位で定義する層を追加。第 1 弾として `design-flow.md`（design + preflight + startproject の設計フロー、EV-01〜23）とスキル用 `_template.md` を作成。`evaluation-set-policy` ルールにスキル層の扱い（pytest 突合の対象外、PR レビュー時突合・`/config-analyze`・実行観察で検証）を追記

- **`/design`: 各フェーズ末に二段品質ゲート（セルフチェック → 自動レビュー）を導入**: Phase 1-3 の受け入れ確認前に、reference 末尾のセルフチェックリストとフェーズ対応レビュアー（要件 = `requirements`、基本設計 = `architecture-reviewer` + 条件付き `security-reviewer`、詳細設計 = `spec-reviewer`）による設計書レビューを必須化。設計ドキュメント専用の重要度定義・ゲート通過条件（Critical=0、High 処理済み）・フェーズ間ドリフトプロトコルを `references/design-review.md` に定義
- **`/preflight`: 設計要否判定（3 段階）と設計成果物の読み込みを追加**: 要件確定時に「設計不要 / 軽量設計メモ / フル設計（`/design` へ誘導）」を判定。Phase 2 で `docs/` 配下の既存設計書と impact-analysis をタスク分解の入力として読み込む

- **評価セット（docs/evaluation/）の導入（ADR-20260703-028）**: 全 14 パッケージについて「正しい状態とは何か」を自然言語で定義した評価セットを新設。AI 生成テストが実装の都合ではなく「あるべき仕様」に沿っているかをレビューするための判断基準として使う
  - **構成**: `docs/evaluation/README.md`（共通フォーマット・hook 型 / CLI ツール型 / スキル型の類型別観点チェックリスト・共通テストレビュー判断基準 6 項目）+ `_template.md`（雛形）+ `<pkg>.md` × 14（責務定義 / 入出力・副作用 / 評価観点 `EV-NN`（正常・異常・境界 × must/should、仕様根拠付き）/ 類型別観点 / パッケージ固有レビュー基準）
  - **運用ルール**: `.claude/rules/evaluation-set-policy.md`（facet: `evaluation-set-policy`）を新設。テスト改修時は該当評価セットと突合し、must 観点のギャップゼロを完了条件とする。突合マトリクスは一時成果物とし、ギャップはパッケージ単位の GitHub Issue で追跡（評価セットには恒久記録しない）。テスト変更時の hook 自動化は Issue #123 で追跡
  - **初回突合**: 既存テスト（`packages/<pkg>/tests/` + `tests/unit` + `tests/e2e` の二層）との突合を実施し、ギャップをパッケージ別 Issue として登録
- **`git-workflow`: CHANGELOG 記述ポリシー（`changelog-policy` ルール）**: CHANGELOG エントリを利用者向け変更のみ・見出し＋1〜2行に統制する新ルールを追加。パッケージの install / sync で `.claude/rules/changelog-policy.md` が配布される
- **`codex-harness`: Codex CLI 向け repo-local ハーネスパッケージを新設**: hooks（secret scan / pre-tool-use policy / stop 時の JSON 検証）・rules・schemas を `.codex/` 配下に配布し、非対話タスク実行（`codex_run`）と read-only セルフレビュー（`codex_review`）を提供する。設計は `docs/design/codex-cli-harness.md` を参照
- **`orchex install --force`**: `.codex/` 配下の配布ファイルがユーザー改変済みでも上書きインストールできるフラグを追加

### Changed

- **パッケージ manifest に `codex_files` / `facet_targets` フィールドを追加**: パッケージ作者が Codex CLI 向け配布ファイル（`.codex/` 配下、hash 保護対象）とファセットビルド対象を manifest で宣言できるようになった
- **`.codex/runs/` / `.codex/reports/` を `.gitignore` に自動追加**: codex-harness インストール時に run artifact / レビュー結果の出力先が自動的に無視されるようになった
- **`.codex/config.toml` への `default_permissions` / `[permissions.*]` 自動マージ**: codex-harness インストール時に permission 設定を既存の `.codex/config.toml` へ非破壊マージするようになった（既存のコメント・他設定は保持）
- **テスト改修時の評価セット突合案内 hook（quality-gates）**: テストファイル変更時に該当パッケージの `docs/evaluation/<pkg>.md` との突合確認を自動案内する `evaluation-set-checker.py`（PostToolUse）を追加。`audit-flags.json` の `features.evaluation_set_check.enabled` で無効化可能（Issue #123）
- **`/startproject`: 設計成果物（`docs/`）との連携を明記**: Phase 2 で既存設計書を読み込んで再質問を削減、Phase 3 の設計レビューと Phase 7 の実装後レビュー（`spec-reviewer` による実装と設計書の突合）で設計書との整合確認を追加
- **PR 自動レビュー（Codex / CodeRabbit）の日本語化とレビュー観点の統一**: GitHub 連携の自動レビューが英語で出力され判断しづらい問題に対応
  - **Codex**: `templates/context/codex.md`（正本）の言語プロトコルに「GitHub PR review は日本語で出力（コード例・識別子は原文のまま）」を追記。委譲フローの「Output: English → Claude が翻訳」設計は維持し、レビュー文脈のみ日本語を優先
  - **Codex**: 公式推奨の `## Review Guidelines` セクションを新設。共通観点（後方互換性・セキュリティ・正確性・正本と生成物の整合性・ドキュメント/テスト追従）、パス別観点（hooks / agent-routing config / scripts / tests。ルート `AGENTS.md` のみ）、ノイズ防止（具体的リスクを示せる場合のみ指摘・不確実なら質問）、重要度ラベル（Critical/High/Medium/Low、`skill-review-policy` と同一語彙）を定義
  - **CodeRabbit**: `.coderabbit.yaml` の `language` を `"ja"` → 正準ロケール `"ja-JP"` に修正。`path_instructions` に `path: "**"` の共通観点を追加し、Codex・内部 `/review` レビュアーと同じ観点・重要度語彙に統一
  - これにより Codex / CodeRabbit / 内部 `/review` の 3 系統で指摘の基準と読み方が揃う（反映は本 PR の main マージ後の PR から）

### Fixed

- **配布先 `CLAUDE.md` の AI Orchestra テンプレート参照パスを修正**: `templates/context/claude.md` の正本・再生成コマンド表記を `$AI_ORCHESTRA_DIR/...` 形式にし、導入先プロジェクトで存在しない相対パスを指さないようにした
- **`core`: サブエージェント結果の context 保存先が cwd に引きずられる問題**: `capture-task-result.py` がサブディレクトリ cwd を受け取っても、`CLAUDE_PROJECT_DIR` または親方向の `.claude` / `.git` 探索でプロジェクトルートを解決し、`.claude/context/` を誤った場所に生成しないよう修正
- **`skill-evolution`: メインループ実行のスキルテレメトリが起動直後に確定記録されていた問題**: `capture-skill-telemetry.py`（PostToolUse: Skill）は Skill ツールの応答（起動メッセージのみ）から自己申告を探すため、メインループ実行では常に `self_report: null`・`duration_ms` 数十 ms で記録が確定していた。設計 3.8 節の縮退方針どおり Stop hook（`capture-skill-stop.py`）を新設し、transcript から `[skill-self-report]` ブロックを抽出して `run_id` で pending と突合、正しい duration と自己申告で記録するよう修正。PostToolUse 側は自己申告が無い場合 pending を温存して Stop hook に委譲する（自己申告が見つからない stale pending は `pending.stale_after_seconds`（既定 600 秒）経過後に機械計測のみでフォールバック記録）
- **skill-evolution データの Git 管理を整備**: `.claude/skill-evolution/metrics/` と `.claude/skill-evolution/pending/`（環境ローカルなテレメトリ）を `.gitignore` sync の `ENTRIES` に追加。`lessons/`（人間可読の学習資産・注入対象）は Git 追跡対象のまま維持する
- `docs/design/skill-evolution.md` の lessons/metrics 保存先記述を実装に合わせて修正（`packages/skill-evolution/` 配下 → `.claude/skill-evolution/` 配下、正本は config `skill-evolution.yaml` の `storage.dir`）

- **`.gitignore` sync が `.claude/codd/` を毎回削除していた問題**: `scripts/lib/gitignore_sync.py` の管理ブロック（`>>> AI Orchestra (.claude) >>>`）は sync のたびに `ENTRIES` で丸ごと置換されるため、`ENTRIES` に無い `.claude/codd/`（codd スキルの生成物 `graph.jsonl` の出力先）はブロック内に手動追加しても次回 sync で消えていた。`ENTRIES` に `.claude/codd/` を追加し、SessionStart hook / orchestra-manager による sync で常に残るようにした（回帰テスト付き）
- `handoff` スキルの指示書（`facets/instructions/handoff.md` および生成物）に含まれていた文字化け（U+FFFD）4 箇所を修正

## [0.2.10] - 2026-07-02

### Added

- **`packages/skill-evolution`: スキル自己改善ループ（Issue #5）**: スキル実行の品質を二軸（自己申告＋機械計測）で計測し、学び（lessons）を次回実行へ還元しつつ、停止条件付きのオフライン反復でスキル自体を改善する新パッケージ。設計は `req/design:skill-evolution` ＋ `ADR-20260701-032` に記録
  - **二層アーキ**: オンライン層＝スキル発火ごとに軽量収集（`inject-lessons.py` が発火前に lessons 注入＋`run_id` 発行、`capture-skill-telemetry.py`／`capture-subagent-skill.py` が完了時に二軸テレメトリを `metrics/<skill>.jsonl` へ記録）。オフライン層＝`skill_evolution.py` CLI が停止条件・3ガード・スコアリング・ロックの決定論部分を提供（シナリオ実行と改善案生成は人間承認ゲート下の実行時作業）
  - **発火検出**: `PreToolUse`/`PostToolUse` の `tool_name == "Skill"`（`tool_input.skill`）で捕捉（`packages/audit` の実績方式）。`context: fork` スキルは `SubagentStop` で補完
  - **成功判定**: スキルごとの `[critical]` チェックリスト全達成で初めて成功。反映先は provenance で塩梅（facet 製→facet 昇格＋`facet build`、非 facet 製→lessons/SKILL.md diff、判別不能→lessons のみ）。数値ガード（コスト・反復・holdout・注入行数）は `skill-evolution.yaml` で調整可能
- **`skill-review-policy`: 4視点網羅オプション（Security/Perf/Quality/a11y）**: 成果物をパスパターンに依存せず固定4視点で網羅レビューするオプションを追記（Issue #5 の A 縮小版。skill-evolution の「スキル実行品質」とは別に「成果物品質」を対象）
- **`packages/fail-logs`: 活用フェーズ — SessionStart で再発失敗サマリーを注入（Issue #81 / ADR-20260630-027）**: 記録した失敗（`.claude/logs/fail-logs/failures.jsonl`）をセッション開始時に集計し、**再発している失敗シグネチャ**をオーケストレーターのコンテキストへ注入する SessionStart hook `inject-failure-summary.py` を追加。記録 → 活用の学習ループ第 2 段階
  - **集計軸**: `failure_type` 別カウント（行動指針にならない）ではなく「再発シグネチャ中心」を採用。シグネチャは command ベースで `(command_kind, 先頭トークン)`、非 Bash は `(tool, failure_type)` にフォールバック。`min_occurrences`（既定 2）以上の再発のみ注入し、見出しに `failure_type` 別内訳を 1 行で添える
  - **フィルタ/抑制**: ログ末尾からチャンク単位で遡って `max_records` 行のみ読む末尾シーク方式（全行走査せず I/O を一定に制限）、`window_days`（既定 7・0 で無期限）で期間フィルタ。再発ゼロなら何も注入しない（ノイズ抑制）。`config/fail-logs.yaml` の `summary:` ブロックで制御（`fail-logs.local.yaml` で上書き可）
  - **セキュリティ**: 注入する command / error_excerpt はログ由来の信頼できない外部データのため `<fail-logs-summary>` 境界フレームで囲み `[log]` プレフィックスを付与し、本文中の山括弧を中和して境界フレーム偽造を防ぐ（間接プロンプトインジェクション対策）。`logs_dir` は `realpath` で project_dir 配下を検証（パストラバーサル防御）
  - 記録フェーズ（`capture-failures.py` / ログスキーマ / `failure_detector`）には手を入れない純粋追加。core 依存のみ
- **`packages/codd`: impact 分析（変更影響の信頼度3帯域分類・Issue #94 / Phase 2）**: 変更 diff から下流ドキュメントへの影響を **Green（自動更新可）/ Amber（要確認）/ Gray（参考）** に分類する `codd impact --diff <ref>` を追加。Phase 1 の素朴な drift（コミット時刻比較）を、宣言された依存関係を証拠とした信頼度スコアへ発展させた
  - **信頼度スコア**: `git diff --name-status <ref>` の変更ファイルを frontmatter の `node_id` にマップし、`depends_on` の逆引きで下流を辿る（サイクル安全・`max_hops` 打ち切り）。`path_score = min(経路上の relation 重み) × decay^(hops-1)`、ノードは全経路・全起点の最良値を採る。重み（derives_from/refines/implements=1.0, supersedes=0.6, references=0.3）・閾値・減衰は `codd.yaml` の `impact:` ブロックで上書き可能
  - **帯域補正**: Corroboration rule（Green は「直接の強依存=事実」か「裏付け起点≥2」のみ。多段単一経路は Amber 上限）と co_changed cap（下流自身も同一 diff で変更済みなら Amber 上限にフラグ。スコアは下げず破壊的変更を Gray に隠さない）を適用。削除された上流ファイルは dangling 注意として別建て報告
  - **出力**: テキスト（帯域別）と `--json`（CI/機械処理向け）。スキル `/codd-impact` を facet build で配布（`.claude/skills/` と `.agents/skills/`）
  - **設計判断（ADR-026 D3）**: CODD は依存宣言を frontmatter に限定するため、証拠源は relation 種別とグラフ距離のみ。参考実装 codd-dev の Noisy-OR・エビデンス種別分類はコード静的解析由来の多様な証拠を確率合成する設計のため適用せず、Corroboration / testimony cap の思想のみ借用。設計は `docs/design/codd-coherence-layer.md` 4.5.1 に記録
  - **レビュー対応の堅牢化（PR #103）**: (1) `git diff` 失敗（無効な ref / git エラー）を空の「影響なし」成功にせず `ImpactError` で非ゼロ終了するよう修正。(2) scope の単層 glob（`dir/*.md`）がサブディレクトリを跨いで誤一致する問題を segment-aware なパス判定に修正。(3) rename された上流（`R old new`）を、ref 側の旧 `node_id` が現グラフに残っていれば dangling 注意から除外し、移動の誤警告を解消。(4) `ImpactConfig` の `green/amber_threshold`（`[0, 1]`）と `corroboration_min_origins`（≥1）の値域検証を追加

### Changed

- **`/issue-fix` を worktree 前提のフローに整合（不要なブランチ作成を回収）**: Phase 2-1 を「ブランチ作成」から「ブランチの準備状況を確認」に変更。issue ごとに先に worktree を作成してその上で作業する運用に合わせ、worktree 内（`git rev-parse --git-dir` ≠ `--git-common-dir`）または base 以外のブランチにいる場合は追加のブランチ作成をスキップし、現在ブランチでそのまま作業を開始する。base 上かつ非 worktree のときのみ従来どおりラベル起点でブランチを作成する（後方互換維持）
  - `$BASE` 解決失敗時は統合ブランチ（`main` / `master` / `develop` / `stage` / `staging`）上でのみブランチを作成し、それ以外は準備済み扱いでスキップ（統合ブランチでの直接作業を回避する安全側設計）
  - 編集ソースは facet `facets/instructions/issue-fix.md`。`.claude/skills/` と `.agents/skills/` の SKILL.md は facet build で再生成

### Fixed

- **`packages/audit`: ルーティング適合率メトリクスの修復と秘密情報マスキングの統一（全パッケージレビュー指摘対応）**: Antigravity（agy）移行後に壊れていた監査機能を修復
  - **エイリアスマージの union 化（Critical）**: `delegation-policy.json` の `aliases` が `cli-tools.yaml` 由来のエイリアスを丸ごと上書きし、`claude-direct` ルーティングの Task 呼び出しが常に `matched=False` に誤記録されていた問題を修正。キーごとに順序保持・重複排除で union する
  - **`detect_route()` の agy 検出追加（Critical）**: codex / gemini のみでルート一致判定から agy が漏れていた問題を修正。単語境界付き正規表現で `bash:agy` を検出し、判定順は `audit-cli.py` と統一（codex → agy → gemini）
  - **`calc_cli_stats` の Counter 化**: antigravity 呼び出しが合計に入るのに内訳・グラフから消えていた問題を修正。`by_tool` 集計を追加し、`dashboard.py` / `dashboard-html.py` も antigravity を表示
  - **SECRET_PATTERNS の共通化**: `audit-prompt.py` に Azure SAS / PEM 秘密鍵の redact パターンが無く、プロンプト経由の秘密情報がマスクされずログに残っていた問題を修正。新設の `hooks/secret_masking.py` に完全版パターンを集約し両 hook から参照
  - **PEM 秘密鍵をブロック全体マスク（PR #108 レビュー対応 / P1）**: PEM パターンが BEGIN 行のみにマッチし鍵本文（base64）と END 行がログに残っていた問題を修正。`-----BEGIN ... PRIVATE KEY-----.*?-----END ... PRIVATE KEY-----`（`re.DOTALL`）でブロック全体を 1 マッチ redact
  - **`detect_route()` を実行ファイル分類へ変更（PR #108 レビュー対応）**: プロンプト本文の "codex"/"agy" 文字列で判定していたため `agy -p 'compare with codex'` が `bash:codex` に誤分類されていた。`_detect_cli_executable()` を新設し、コマンドを `&& || ; |` でセグメント分割して各先頭トークン（env 代入は読み飛ばし）の basename で実行ファイルを判定。プロンプト引数の文字列は分類根拠にしない
  - **テストの秘密情報リテラル分割（CI 対応）**: GitGuardian / Betterleaks が検出していたテスト内の Bearer token / PEM 直書きを文字列連結に分割（実行時の値は不変）
- **`packages/cocoindex`: proxy プロセス管理と設定ファイル書き込みの安全性を強化（全パッケージレビュー指摘対応）**: 無関係プロセスの誤 kill と他ツール設定の破壊を防止
  - **ポート占有 PID の同一性検証（Critical）**: `start_proxy` / `stop_proxy` / `cleanup_orphan` がポートから見つけた PID を無検証で採用・SIGTERM/SIGKILL していた問題を修正。`ps -o command=` で `mcp-proxy` / `proxy_supervisor` のプロセスであることを検証し、検証失敗時は採用せず（start は `proxy_state=failed` + 理由記録）、kill せずクリーンアップのみ行う
  - **破損 JSON の無警告上書き防止**: 構文エラーのある `.mcp.json` / `.gemini/settings.json` を「空」とみなしてユーザーの手動編集ごと上書きしていた問題を修正。非空でパース不能な場合は stderr 警告を出して提供・削除をスキップ
  - **TOML エスケープ**: `.codex/config.toml` 生成時に `command` / `url` の `"` `\` をエスケープし、想定外の値でファイル全体が壊れる問題を修正
  - **proxy 恒久失敗時の stdio フォールバック**: `proxy_state == "failed"` の場合に SSE/HTTP エントリを書き続けて cocoindex-code が使用不能のままになる問題を修正。stdio エントリへフォールバックし "falling back to stdio" を出力（同一 tick 内の自動再起動はしない）
  - **transient failure の永続降格を回避（PR #107 レビュー対応）**: 上記フォールバックが、一時的な起動失敗（ポート衝突・ランチャー不調）でも `failed` 状態を永続化させ、ポートが空いても stdio へ恒久降格したまま再起動されない問題を修正。`failed` かつ**ポートが空いている**場合は stdio に落とさず後続の `start_proxy_background()` による再起動を許し、**ポートを他プロセスが占有している場合のみ** stdio フォールバック（従来の PID 同一性検証による「無関係プロセスを kill しない」安全性は維持）
  - **テストのモジュールロード衝突を解消**: `tests/module_loader.load_module` が呼び出しごとに新しいモジュールを `sys.modules["proxy_manager"]` へ登録するため、`tests/unit/test_proxy_manager.py` との併走時に文字列指定 `@patch("proxy_manager.xxx")` が別オブジェクトを patch して 16 件が収集順依存で失敗していた。package テストに autouse fixture を追加し、各テスト直前に本ファイルの proxy_mgr へ再バインドして収集順に依存しないようにした
- **hook 判定精度と依存整理（agent-routing / codex-suggestions / antigravity-suggestions、全パッケージレビュー指摘対応）**
  - **`is_cli_enabled` を core へ引き上げ**: codex-suggestions / antigravity-suggestions が agent-routing 所有の `route_config` へ try/except 外のトップレベル import で依存し、agent-routing 未導入構成で hook がハードクラッシュしていた問題を修正。`hook_common.is_cli_enabled` に移動し（route_config は再エクスポートで後方互換）、manifest の `depends: ["core"]` と実態を一致させた
  - **agent-routing の単語境界マッチ化**: `"ui" in "quick"`、`"test" in "latest"` 等の部分文字列誤検知で UserPromptSubmit のほぼ毎回誤ルーティング提案が注入されていた問題を修正。英語トリガーは `\b` 境界の正規表現（コンパイルキャッシュ付き）、日本語トリガーは従来の部分一致を維持
  - **codex-suggestions の誤抑制修正**: `str(tool_response)` への "error"/"failed" 部分一致で「エラーハンドリング設計」を含む正常な plan の提案が抑制されていた問題を、構造化フィールド（`is_error` / `error`）のみの判定へ変更
  - **antigravity-suggestions の "version" 過剰抑制修正**: 研究シグナル（RESEARCH_INDICATORS）を抑制パターンより優先する順序に変更し、`"version"` は `"latest version"` / `"what version"` の具体的フレーズへ置き換え
  - **日本語隣接 ASCII トリガーの回帰修正（PR #111 レビュー対応）**: `\b` 単語境界マッチは Python が日本語文字を単語文字として扱うため `ReactのUIを実装して` の `UI` 等が境界なしと判定されマッチしなくなっていた。ASCII 限定 lookaround（`(?<![A-Za-z0-9_])...(?![A-Za-z0-9_])`）へ変更し、日本語隣接 ASCII を検出しつつ `quick` の `ui` 等の誤検知防止は維持
- **`packages/fail-logs` / `packages/reverse`: 書き込み側パストラバーサル防御とドキュメント整合（全パッケージレビュー指摘対応）**
  - **fail-logs 書き込み側のパストラバーサル防御**: 読み込み側（`inject-failure-summary.py`）には realpath 検証があるのに、書き込み側（`capture-failures.py`）は `logs_dir` config 値を無検証で結合しており、`.local.yaml` の `logs_dir: ../../..` でプロジェクト外に書き込めた非対称を修正。共通関数 `hook_common.resolve_path_within()` を新設して両 hook から使用し、project_dir 外を指す場合は `DEFAULT_LOGS_DIR` へフォールバック（失敗記録を黙って捨てない）
  - **reverse README の Antigravity 表記更新**: 実装・配布先 SKILL.md は agy / `antigravity.enabled` へ完全移行済みなのに README だけ旧 Gemini 表記（`gemini.enabled` 等）のままで、設定が効かないと誤解を招く状態を修正（旧設定の読み替え互換の注記も追加）
  - **reverse の manifest depends 宣言**: `depends: []` を実態（cli-tools.yaml と general-purpose / code-reviewer / security-reviewer エージェントへの依存）に合わせ `["core", "agent-routing"]` へ修正
  - **読み側の実効パスフォールバック（PR #114 レビュー対応）**: 書き込み側は無効な `logs_dir` を `DEFAULT_LOGS_DIR` へ退避するのに、読み側（`inject-failure-summary.py`）は設定パスのみ解決して無効なら return していたため、退避された失敗が再発サマリーに載らず学習ループが無効化されていた。読み側にも同じデフォルトフォールバックを適用し両 hook の実効パスを一致させた
- **`packages/codd`: validate の無音化防止と非 ASCII ファイル名対応（全パッケージレビュー指摘対応）**
  - **`checks:` の語彙バリデーション**: 検査レベルに typo（例: `dangling: eror`）があると Finding が error / warning のどちらにも集計されず validate が出力ゼロ・exit 0 になり、CI ゲートがサイレント無効化されていた問題を修正。`normalize_check_level` が `{error, warning, off}` 以外を `ValueError` で拒否する（YAML 1.1 の bare `off` → False 読み替えは維持）
  - **`git diff --name-status -z` への切り替え**: `core.quotePath=true`（デフォルト）で日本語等の非 ASCII ファイル名が 8 進エスケープされ、impact の変更検出から漏れる（silent false negative）問題を修正。NUL 区切りパースで R（rename）/ C（copy）/ D も正しく処理し、C のコピー元を changed に誤算入していた挙動も併せて解消。`_git_output` に `encoding="utf-8"` を明示
- **`packages/quality-gates`: 共有状態のプロジェクトスコープ化と設定判定の一元化（全パッケージレビュー指摘対応）**
  - **/tmp 状態ファイルのプロジェクトスコープ化**: `test-gate-checker` / `post-test-analysis` / `post-implementation-review` が固定パス `/tmp/claude-*-state.json` を共有し、複数プロジェクト並行時に編集件数・テスト結果が相互汚染して閾値判定が誤る問題（Issue #83 と同系統）を修正。`test-tampering-detector` と同じ `get_project_state_key()`（git-common-dir 優先）でプロジェクトごとにネストする形式へ変更し、共有ヘルパー `hooks/quality_gate_config.py` を新設（manifest 宣言済み）。同一リポジトリの worktree 間は tampering-detector と同様に意図的に状態を共有する
  - **`quality_gate.enabled` デフォルトの一元化**: `test-gate-checker.py`（False）と `post-test-analysis.py`（True）で真逆だったデフォルトを、ベース config（`enabled: true`）と対称な True に統一（`QUALITY_GATE_ENABLED_DEFAULT` を共有モジュールに定義）
  - **`review_suggested` のリセット経路追加**: 一度提案すると二度と提案されなかった `post-implementation-review` に TTL（24 時間、定数化）による再アームと、提案時のカウンタリセットを追加。7 フック中唯一テストが無かった同 hook に初のテスト（閾値・TTL・プロジェクト分離・main() E2E）を新設
  - **状態更新のロック + アトミック書き込み（PR #112 レビュー対応）**: プロジェクトスコープ状態の read-modify-write がロックなし・非アトミックで、並行 worktree/セッションで lost update や書き込み中断による JSON 破損（全プロジェクト分喪失）が起きうる問題を修正。単一トランザクション API `update_project_scoped_state()` を新設し、`fcntl.flock` による排他区間で read → mutate → write（tmp + `os.replace`）を実行して TOCTOU を構造的に排除（`context_store.py` の既存 flock パターンを踏襲）。`post-implementation-review` の更新もこの API 経由に統一
  - **`DEFAULT_TEST_GATE_STATE` の重複解消（PR #112 レビュー対応）**: 同一の共有状態ファイルを使う `test-gate-checker` / `post-test-analysis` が独自に持っていたデフォルト状態辞書を `quality_gate_config.py` に集約し、両 hook から import してスキーマドリフトを防止
- **`packages/core`: `write_json` のアトミック化と plan-gate のフェイルオープン修正（全パッケージレビュー指摘対応）**: hook の並列実行・タイムアウト kill に対する書き込み安全性を改善
  - **`write_json` アトミック化**: `open(path, "w")` の直接上書きを「一時ファイル書き込み → `os.replace()`」へ変更。書き込み途中に他 hook が読んで不完全 JSON を掴む競合（`working-context.json` / `plan-gate.json`）と、SessionStart の 15 秒タイムアウト kill による `.mcp.json` 等の破損（cocoindex の provision も本関数を使用）を防止。例外時は一時ファイルを削除して再送出。既存ファイルのパーミッション（例: mode 0600 の `.mcp.json`）は `os.replace` で失われないよう一時ファイルへ複製してから置換する
  - **plan-gate の `subagent_type: null` フェイルオープン修正**: `tool_input.get("subagent_type", "").lower()` は値が `null` のとき `None.lower()` で例外になり、`@safe_hook_execution` が握りつぶして「ブロックすべき実装エージェント呼び出しが素通り」していた。plan-gate 系 3 hook（check/set/clear）の stdin 読みを `hook_common.read_hook_input()` に、フィールド取得を None 安全な `get_field()` に統一
  - **入力バリデーションの底上げ（PR #106 レビュー対応）**: `read_hook_input()` はトップレベル JSON が dict でない（list / string 等）場合に `{}` を返すよう正規化し、`data.get(...)` での例外による同種のフェイルオープンを全 hook で防止。`get_field()` は非文字列 truthy 値（整数等）を `str` 化して後続の `.lower()` クラッシュを回避
  - 回帰テスト追加: `subagent_type: null` / 非文字列 / `tool_input` 欠落・null / トップレベル非 dict、`write_json` のラウンドトリップ・一時ファイル非残存・パーミッション保持・`os.replace` 失敗時クリーンアップ
- **`packages/tmux-monitor`: シェルインジェクション修正と hook ハング・リソースリーク対策（全パッケージレビュー指摘対応）**
  - **ディレクトリ名経由のシェルコマンドインジェクション（Critical）**: `project_name`（`basename(cwd)`）を tmux が `$SHELL -c` で実行する文字列へ無エスケープ埋め込みしていた 2 箇所（respawn-pane / new-session）を修正。`shell_quote()` を `tmux_common.py` へ共通化し、`build_wait_cmd()` 抽出で動的値のみエスケープ（`$(date)` の意図的展開は維持）
  - **`run_tmux()` / `ps` への timeout 追加**: 同期 hook から呼ばれる subprocess に timeout（5 秒）が無く、tmux/ps ハングが Claude Code 全操作のブロックに直結していた問題を修正。`TimeoutExpired` は非ゼロ returncode の疑似結果へフォールバック
  - **孤児クリーンアップの整合**: PID 検出失敗時のフォールバックキー（16 進）のセッション・info ファイルが `isdigit()` 判定に合わず永久残留していた問題と、削除対象が 3 拡張子のみで `.shared-dir` / `.task-queue` と shared-dir 実体（`/tmp/claude-shared-*`）が異常終了時に永続リークしていた問題を修正。session-end と同じ 5 拡張子 + 実体削除に統一（`remove_session_files()` 共通化）
  - **`tmux-subagent-start.py` の `main()` 分割**: 約 150 行・ネスト 4-5 段を責務ごとの 5 関数へ純粋抽出（挙動不変、回帰テスト付き）
  - **フォールバッククリーンアップの誤削除防止（PR #109 レビュー対応）**: フォールバックキーのクリーンアップ判定が current `project_name` から tmux セッション名を再構成していたため、後続プロジェクトの起動時に別プロジェクトのフォールバックセッション（`.tmux-session` / `.shared-dir` / キュー）を誤削除しうる問題を修正。記録された `.tmux-session` の実名で生存判定し、現在の prefix で始まらない他プロジェクトの session info には触れない
- **配布基盤: ユーザー編集ファイルの保護（全パッケージレビュー指摘対応）**: 配布時 SHA-256 ハッシュを `orchestra.json`（`file_hashes`）に記録し、変更検知で破壊的操作を防止
  - **`uninstall` の無条件削除防止（Critical）**: config / agents ファイルを diff 確認なしで `unlink()` していた問題を修正。削除前にハッシュ比較し、ユーザー編集済み・ハッシュ未記録（旧 install 由来）は警告してスキップ（安全側）。dry-run でも同じ判定を表示
  - **`install` 再実行の無条件上書き防止**: `run_initial_sync()` に `sync_engine.needs_sync()` ゲートを追加し SessionStart 側の同期と挙動を一致。ユーザー変更が静かに消える問題を解消
  - **`install` の config コピーもハッシュ保護（PR #110 レビュー対応）**: `run_initial_sync()` 以外に `install()` 内の config コピーループが hash 比較なしで無条件 `copy2` していたため、「編集 → uninstall（保護スキップ）→ 再 install」でユーザー編集が消えていた（Codex 実機再現）。`_copy_config_if_safe()` ヘルパーで uninstall と対称の変更検知を適用（編集済みは警告してスキップ。ただし hash 未記録時はコピーを通す＝非破壊操作の自己修復を優先）
  - **`patch_all_agents` の所有権チェック**: `.claude/agents/*.md` 全件を対象にしていた model パッチを、インストール済みパッケージの manifest `agents` 宣言から構築した allowlist のみに限定。ユーザー独自エージェントの `model:` が毎 SessionStart で上書きされる問題を解消
  - 後方互換: `file_hashes` の無い既存 `orchestra.json` でも全機能が動作（未記録ファイルは削除スキップの安全側）

## [0.2.9] - 2026-06-26

### Fixed

- **quality-gates のパイプマスク誤検知を修正（Issue #83）**: `quality_gate` イベントの `passed` 判定を `exit_code == 0` 単独から `failure_detector.analyze` の 2 段判定（exit_code + 出力パターン）へ統一。`pytest ... | tail -30` のようにパイプで終了コードがマスクされた失敗を `passed: false` と正しく記録し、`block_on_failed_test` 有効時はブロックする
  - `post-test-analysis.py` の独自 `is_test_failure()` を削除し `packages/core/hooks/failure_detector.py` に一本化（検知ロジックの重複解消）。`extract_failure_summary` は存続
  - `emit_quality_gate_event` は呼び出し側が導出した `gate_passed` を受け取る形に変更。payload キー（command/exit_code/passed/output_excerpt/blocking）は不変で後方互換。検知根拠を示す `detected_by`（`exit_code` / `output_pattern`）を任意で追記
- **`/image-gen` スキルの画像生成失敗を修正（codex 0.140.0 対応・実機 E2E 検証済）**: codex-cli 0.140.0 への更新で `image-generator` エージェントの呼び出し契約が壊れていた問題を修正。実機の生成検証で原因を特定し対処した（赤リンゴ画像の出力まで確認）
  - **保存回帰の解決（`--enable imagegenext` 必須）**: codex 0.140.0 の `exec` モードは built-in `image_gen` の画像を**ディスクに保存しなくなっていた**（`image_generation_end` イベントから `saved_path` キーが消失。画像は base64 で返るのみ）。0.137.0 からの回帰で、`--enable imagegenext` フラグを付けると保存が復活する。これが無いと `~/.codex/generated_images/` は空のままで、エージェントが過去の画像を誤って掴む（虚偽成功）原因になっていた
  - **保存ファイル名の変化に追従**: imagegenext 有効時の保存名は `ig_*.png` ではなく `call_*.png`。鮮度ガードの検索を両パターン対応（`\( -name 'call_*.png' -o -name 'ig_*.png' \)`）に更新
  - **鮮度ガードの手動迂回を明示的に禁止**: 鮮度判定で対象なしのとき `ls -t | head` 等で session ディレクトリを漁って「最新ファイル」を掴む improvise を禁止（無関係な古い画像を誤コピーして虚偽成功する実害を確認）。対象なしは必ず FAILURE 報告
  - **sandbox レベル回帰**: `--sandbox workspace-write` だけでは `image_gen` の in-process app-server が `Operation not permitted` で起動しない（backend 通信に network が必要）。`-c sandbox_workspace_write.network_access=true` で **network のみ開放**し、FS は `workspace-write`（repo 内に OS 強制で限定）のまま維持。`danger-full-access` は untrusted prompt 経由の悪用を避けるため**採用しない**
  - **`--full-auto` 廃止**: codex 0.140.0 で deprecated（`--sandbox` に統合）のためコマンドから削除。併せて `-c model_reasoning_effort=low` を指定（default effort は自己評価ループで長時間ハングするため必須）
  - 設計改訂は ADR-20260605-023 の Update（2026-06-17）に記録

### Added

- **`packages/codd`: ドキュメント整合性レイヤー（CODD 思想の独立パッケージ・essential 化）**: 設計書・ADR・計画・ルール・指示書の依存関係をドキュメント先頭のフロントマター（`codd:` ブロック）で宣言し、`scan` で依存グラフを構築（`.claude/codd/graph.jsonl`）、`validate` で整合性を検証する。`core` のみ依存
  - **検査**: dangling（リンク切れ）/ duplicate / cycle（循環）/ unknown（未定義 kind・relation・status）を error、missing_frontmatter / orphan（孤立）/ drift（上流が下流より新しい追従漏れ疑い）を warning として検出。error 検出時は非ゼロ終了（CI/フック組み込み可）。drift の時刻ソースは `git log -1 --format=%ct`（未コミットは mtime フォールバック）
  - **配布**: essential プリセットに追加し常時有効化。`config/codd.yaml` で scope glob・kind/relation 語彙・検査レベル・グラフ保存先を制御（`.local.yaml` 上書き対応）。スキル `/codd-scan`・`/codd-validate`、ルール `codd-frontmatter-policy` を facet build で配布
  - **生成スキル連携**: `design` / `design-tracker` / `task-state` スキルが成果物（要件・設計・ADR・Plans.md）に `codd:` フロントマターを自動付与するよう改修。導入先プロジェクトが essential セットアップだけで生成ドキュメントを CODD 管理下に置ける
  - **方針**: 1 ファイル = 1 ノード。依存宣言の正本はフロントマター 1 箇所（外部 doc_links.yaml は作らない）。impact 分析（Green/Amber/Gray）・hook 自動配線・CI verdict・コード⇔ドキュメントトレースは Phase 2/3（別 Issue #94〜#98）。設計と決定は `docs/design/codd-coherence-layer.md` / ADR-20260624-026 に記録
- `packages/fail-logs`: AI の失敗イベントを記録する基盤パッケージ（`core` のみ依存）。PostToolUse hook（`capture-failures.py`）がツール実行エラー・テスト/lint 失敗・外部 CLI 失敗を検知し、**失敗のみ**を `.claude/logs/fail-logs/failures.jsonl`（audit v1 互換スキーマ・所有者限定 `0600`・機密マスク済み）に追記する。「失敗を蓄積して次回以降に活かす」学習ループの記録基盤（活用は次フェーズ）
  - 失敗検知ロジックを `packages/core/hooks/failure_detector.py`（純粋関数）に集約。`exit_code` が 0/欠落でも test/lint コマンドの出力に失敗マーカーがあれば失敗と判定する 2 段構成で、`pytest ... | tail` のようにパイプで終了コードがマスクされる誤検知を回避
  - `config/fail-logs.yaml` で全体の有効/無効・失敗種別ごとのトグル・抜粋文字数・保存先を制御（`.local.yaml` 上書き対応）
  - 責務境界: `audit`=compliance/observability、`quality-gates`=ゲート + 合格率分母、`fail-logs`=失敗知識の蓄積。`quality_gate` emit は存続させ後方互換を維持。設計と移行パスは ADR-20260612-025 に記録
  - 次フェーズの課題（失敗サマリー注入・教訓のルール化・quality-gates への detector 適用によるパイプマスクバグ修正・差し戻し検知）を Issue #81〜#85 として登録
- `docs/adr/ADR-20260612-025.md`: fail-logs 新設と失敗検知ロジック共通化の設計判断を ADR として記録

### Changed

- **`/reverse` の Phase 1（走査）をサブエージェント委譲化（試作）**: Phase 1 の重い処理（統計収集・エントリポイント抽出・Antigravity 概観・`scope.md` 合成）を新規 `reverse-coordinator` サブエージェントに委譲し、メインオーケストレーターには **要約＋成果物パスのみ** を返すようにした。中間 JSON（`stats.json` / `entrypoints.json`）と Antigravity 生出力がメインコンテキストに流入しなくなる（コンテキスト保護）。ユーザー確認ゲート（AskUserQuestion: 続行/再実行/中止）は従来どおりメインに残置し、Phase 2〜5 は不変
  - 新規エージェント `reverse-coordinator`（`tools` に `Agent` を含みネスト起動可能）を **`reverse` パッケージに同梱**（skill と同一パッケージ）。内部で Antigravity スキャンを nested 起動する（メイン→coordinator→agy の深さ 2〜3）。`antigravity.enabled: false` 時は coordinator 内で Read/Grep/Glob フォールバック
  - ルーティング tool は `cli-tools.yaml` に明示登録せず、未登録エージェントの既定（`claude-direct`）に委ねる。これにより `agent-routing` パッケージから reverse 固有の参照を排除し、`orchex install reverse` 単体でも agent が同梱・`agent-routing` 単体でも宙ぶらりんな agent が残らないようにした
  - まず Phase 1 のみの試作（検証後に Phase 2〜3 へ段階展開予定）。`reverse-coordinator` は `/reverse` スキル内部から Task 起動される専用エージェントのため、キーワードルーティング（`AGENT_TRIGGERS`）の対象外
- **cli-language ポリシーの重複出力を排除**: `cli-language` policy を参照する rule composition を `orchestra-usage` のみに集約し、`codex-delegation` / `antigravity-delegation` の composition からは参照を外した（`policies: []`）。これまで 3 つの生成ルール（`.claude/rules/*.md`）に同一の「CLI Language Policy」ブロックが inline され 3 回重複していたが、毎セッション読み込まれるルール群から約 3.6KB の重複を削減。全ルールは同時ロードされるため委譲ルール側からの参照可能性は不変で、instruction 本文・振る舞いは変更なし
- **外部 CLI 向けスキルの出力先を `.agents/skills/` に統一**: facet build の非 claude ターゲットが生成する SKILL.md の出力先を `.codex/skills/` → `.agents/skills/` に変更。`.agents/skills/` は Codex CLI と Antigravity CLI（agy）の両方がプロジェクトローカルで自動検出する共有ディレクトリ（agy 1.0.7 / Codex 0.139 で実機確認）。これにより、これまで同期されていなかった agy へのスキル配布が解決
  - 移行: 横展開先の旧 `.codex/skills/{name}` に残る facet スキルは、facet build 時に **facet manifest 記録分のみ**を対象に一度だけ削除（`_cleanup_legacy_codex_skills`）。template 配布の `context-loader` 等（manifest 非記録）・手書きファイル・`.codex/config.toml`・execpolicy 用 `.codex/rules/*.rules` は対象外。symlink は辿らない
  - 単一ビルド（`orchex facet build --target codex --name <skill>` 等の `build_one`）でも対象スキルの旧 `.codex/skills/<name>` を掃除するよう修正。これまで一括ビルド（`build_all`）でしか掃除されず、targeted rebuild 時に旧パスの重複スキルが残る問題を解消。cleanup の `shutil.rmtree`/`unlink` は `OSError` を捕捉し、1 件の失敗で全体が止まらないようにした
  - ターゲット名（`codex`）・manifest 配置は現状維持（`.agents/skills/` は config なしで自動検出されるため `.codex/config.toml` 変更は不要）

### Removed

- **外部 CLI へのルール同期を廃止**: facet build の非 claude ターゲットでルール composition をビルドしないように変更（`build_one` で skip）。Claude のルール（`.claude/rules/*.md` は振る舞い指示）と Codex/agy のルール（execpolicy 等のコマンドポリシー）は思想が異なり、Markdown ルールを外部 CLI に同期する意味がないとの判断
  - 移行: 旧 `.codex/rules/*.md`（生成物）は facet build 時に削除（`_cleanup_legacy_codex_rules`）。execpolicy 用 `.codex/rules/*.rules` は保持。`.claude/rules/` は従来どおり生成。単一ビルド（`build_one`）で非 claude ルールが skip される際にも、対象の旧 `.codex/rules/<name>.md` を掃除する
  - 各プロジェクトが Codex/agy のルール（コマンドポリシー等）をどう設定するかは別途検討（このリポジトリ対象外）
- **LLM モデル名ハードコードの SSOT 参照化**: モデルを変更してもテストが壊れない状態に整理
  - Codex コマンド生成 hook 4 本（`route_config.py` / `check-codex-before-write.py` / `check-codex-after-plan.py` / `post-test-analysis.py`）のフォールバック既定値（model / sandbox.analysis / flags）を `hook_common` の定数（`DEFAULT_CODEX_MODEL` ほか）に集約。各 hook に散在していた既定値の重複・値ズレを解消し、フォールバックモデルを `gpt-5.3-codex` → `gpt-5.5` に統一。これらの定数は config が読めない障害時のみ使う最終安全網であり、`cli-tools.yaml` とは意図的に独立（同期不要）
  - `packages/core/tests/test_config_loading.py`: 実 `cli-tools.yaml` のモデル値を literal で比較していた 2 テストを、同じ yaml を独立に読んで期待値を導出する方式に変更。`codex.model` / `antigravity.model` を変更してもテストが壊れない（非空 str・allowlist 包含の構造契約は維持）

## [0.2.8] - 2026-06-12

### Changed

- **Gemini CLI → Anti-Gravity CLI（agy）移行**: Google の方針変更（Gemini CLI 廃止・Antigravity への移行）に伴い、リサーチ系 CLI 連携を `agy` に置き換え
  - `cli-tools.yaml`: `antigravity:` セクションを新設（`model: gemini-3.1-pro-high`、`model_allowlist`、`requires_sandbox_disable: false`）、`gemini:` セクションを廃止。`agents.researcher.tool` は `antigravity` に変更
  - 後方互換: 横展開先の `.local.yaml` に残る旧 `gemini` 設定は読み込み時に正規化（`hook_common.normalize_cli_tools_config`）。`gemini.enabled: false` は `antigravity.enabled` に反映、`agents.*.tool: gemini` は `antigravity` に読み替え、`gemini.model` は Gemini CLI 固有値のため引き継がない
  - agy は無効なモデル slug でも exit 0 でデフォルトに黙ってフォールバックするため、コマンド提案時に `model_allowlist` と突合して `[WARN]` を付与
  - `packages/gemini-suggestions` を `packages/antigravity-suggestions` にリネーム。hook は `suggest-antigravity-research.py`（`[Antigravity Suggestion]` 出力、`agy -p '...' --model <slug>` 提案。agy は stdin 封じ不要）
  - 横展開先の `orchestra.json` に残る旧パッケージ名は SessionStart 時に自動移行（`sync-orchestra.py` の `RENAMED_PACKAGES` 読み替え）。旧 hook 登録は既存の hooks 同期が自動除去
  - facets を antigravity 系にリネーム（`antigravity-system` スキル / `antigravity-delegation`・`antigravity-suggestion-compliance` ルール）。旧 gemini スキル・ルールは facet build の orphan cleanup で自動削除
  - **`GEMINI.md` の生成・配布を廃止**: Antigravity 向け指示は `AGENTS.md` に統合（`context_files.fragments` による `codex.md` + `antigravity.md` のセクション合成。Codex CLI / Antigravity CLI 共用）。横展開先の旧 `.gemini/GEMINI.md` は生成物マーカーを確認した上で context sync 時に自動削除（手書きファイルは保持）
  - `templates/gemini/`（GEMINI.md / settings.json / skills）と `templates/context/gemini.md` を削除
  - cocoindex: MCP プロビジョニングのターゲットを `targets.antigravity` に改名（出力先は agy の仕様に合わせて `.gemini/settings.json` を維持。旧 `targets.gemini` の `enabled: false` は読み替え）
  - audit: `agy -p` / `--print` / `--prompt` の呼び出しを `tool: antigravity` の `cli_call` として記録（旧 `gemini -p` 検知はレガシーログ用に残置）。checkpoint スクリプトも antigravity 集計に対応
  - 設計判断を ADR-20260612-024 として記録

### Fixed

- `codex exec` の非対話実行で stdin を封じていなかった問題を修正。stdin が開いたままだと "Reading additional input from stdin..." で無限ハングするため（特にバックグラウンド実行・サブエージェント実行時）、コマンド生成 hook 4 本（`route_config.py` / `check-codex-before-write.py` / `check-codex-after-plan.py` / `post-test-analysis.py`）が提案するコマンドと、全ドキュメント・エージェント定義・テンプレートのコマンド例に `< /dev/null` を追加。`audit-cli.py` のプロンプト抽出正規表現も `< /dev/null` 付きコマンドに対応
- `codex-delegation` ルールに「Non-Interactive 実行（MUST）」セクションを新設（stdin 封じ・タイムアウト・exit code 判定・ハング調査プロトコル。無効モデル名が 400 リトライループで無限ハングに見える事象の調査手順を含む）
- `packages/core/tests/test_config_loading.py` のモデル期待値を実際の設定値（`gpt-5.5`）に追従

### Added

- `packages/image-generation`: Claude Code から API キー不要・非対話で Codex 組み込み `image_gen`（ChatGPT 認証, 本物の AI 画像生成）を呼ぶ `/image-gen <プロンプト>` スキルと `image-generator` サブエージェントを追加した自己完結パッケージ（`core` のみ依存）。エージェントは入力サニタイズ・出力パス境界検証・PNG/サイズ/キーワード検証・sandbox 二層構造ポリシーを内包し、CLI ログでメインコンテキストを汚さないよう生成を委譲する。モデルは `config/image-generation.yaml` の `image_model`（既定 `gpt-5.5`）。出力先デフォルトは `generated-images/`（`--out` で変更可・`.gitignore` 管理）。起動は Claude のネイティブ subagent dispatch ＋ `/image-gen` からの明示 `Task()` で行い、agent-routing への登録（`cli-tools.yaml` / `route_config.py`）は持たない。設計は ADR-023
- `docs/adr/ADR-20260605-023.md`: Codex 画像生成統合の設計（sandbox 二層構造・呼び出し契約・モデル既定 `gpt-5.5`・自己完結パッケージ化・リスク）を ADR として記録

## [0.2.7] - 2026-05-13

### Changed

- `agent-routing`: 未分類のリサーチ入力を `Gemini` 固定ではなく `researcher` 基点の config-driven 解決に変更。`cli-tools.yaml` / `.local.yaml` の `agents.researcher.tool` に追従するようにした
- `quality-gates` / `audit`: quality gate の責務を整理し、判定と block は `quality-gates`、監査ログと集計は `audit` が担う構成に変更
- `cocoindex`: proxy モードを proxy-only に変更。`proxy.enabled: true` 時は stdio fallback を行わず、決定論的 URL と state file、reconnect 通知で扱うようにした
- `cocoindex`: proxy 実体を supervisor 管理に変更。外向き URL を維持したまま inner `mcp-proxy` を転送し、`idle_timeout` ベースの自動停止を追加
- `audit/scripts/dashboard-html.py`: `-o` 未指定時のデフォルト出力先を `.claude/YYYYMMDD-dashboard.html` に変更。`-o -` で stdout 出力をサポート
- `orchex scripts`: スクリプト一覧に説明（description）カラムと使い方ヒントを追加
- `packages/audit/manifest.json`: scripts エントリを `{path, description}` オブジェクト形式に拡張（文字列形式との後方互換あり）

### Added

- `cocoindex`: `start-mcp-proxy.py` と `notify-proxy-reconnect.py` を追加し、`.claude/state/cocoindex-proxy.json` / `.claude/state/cocoindex-sessions/<session_id>.json` による runtime state 管理を導入
- `cocoindex`: `proxy_supervisor.py` を追加し、`active_clients` と `idle` 状態を持つ proxy supervisor を導入
- `scripts/lib/orchestra_models.py`: `ScriptEntry` データクラスを追加（manifest の scripts 値を型安全に扱う）
- `packages/audit/README.md`: audit パッケージの使い方ドキュメントを追加
- `packages/git-workflow/scripts/resolve_base_branch.py`: PR の base branch を `--base` 明示指定 > 環境変数 `AI_ORCHESTRA_BASE_BRANCH` > merge-base 自動推定 > fallback (`main`) の優先順で解決する CLI を追加。`main` + `stage` 等の多段ブランチ運用に対応。候補が同距離の場合は `staging` / `stage` 系を `main` / `master` より優先する (#63)
- `packages/reverse/`: 既存コードベースのリバースエンジニアリングを 5 フェーズ対話型で実行する `/reverse` スキルを追加。`collect-stats.py` / `find-entrypoints.py` / `collect-todos.py` / `generate-mermaid.py` の言語非依存補助スクリプト 4 本を同梱し、Gemini 主体で依存グラフ・機能抽出・設計書・負債レポートを生成する
- `docs/adr/ADR-20260513-019.md`: スキル/ルールは必ずパッケージ manifest に登録する（孤立 composition の禁止）ポリシーを ADR として記録
- `tests/unit/test_composition_manifest_consistency.py`: 全 composition が manifest に登録されていることを検証する pytest を追加（ADR-019 の強制機構）

### Changed

- `pr-create` / `issue-fix` / `pr-standards`: PR 作成時の base branch を `main` 固定から resolver スクリプト経由の解決に切り替え。`/pr-create --base <branch>` で明示指定可能 (#63)

### Fixed

- `agent-routing`: `このPDF見てください` のような入力が大小文字不一致で Gemini fallback に乗らなかった問題を修正し、`researcher` ルーティングに統一
- `quality-gates/test-gate-checker.py`: 旧 `route-audit/orchestration-flags.json` 参照を廃止し、現行 `audit/audit-flags.json` を正として読むよう修正
- `quality-gates/post-test-analysis.py`: quality gate の pass/fail を exit code 基準へ戻し、`ruff check` / `mypy` を再び gate 対象に含めるよう修正
- `cocoindex/proxy_manager.py`: `project_dir` の別表現 (`/tmp` / `/private/tmp` など) で別ポートになる問題を修正し、proxy status / 再利用判定の不整合を解消
- `audit/hooks/event_logger.py`: worktree 環境でログが分散する問題を修正。全 worktree のログを root worktree の `.claude/logs/audit/` に集約するようにした
- `packages/core/manifest.json`: `handoff` スキルがどの manifest にも登録されていない孤立状態だったため、`core` の skills 配列に追加してライフサイクル管理対象とした
- `scripts/lib/facet_builder.py`: composition の `scripts:` でサブディレクトリ込みのパスを指定した場合に、スキル配下に二重ネストで配置される問題を修正。`Path(sname).name` で basename のみを使い `.claude/skills/<name>/scripts/<file>` のフラット構造に統一した

## [0.2.6] - 2026-04-13

### Added

- `audit/scripts/dashboard-html.py`: 既存ログを横断集計し Chart.js で可視化する HTML ダッシュボード生成スクリプトを追加 (#31)
- `audit/scripts/dashboard_stats.py`: テキスト / HTML 両ダッシュボードで共有する集計ロジックモジュールを新設
- `quality-gates/test-tampering-detector.py`: PostToolUse で `it.skip()` / `@pytest.mark.skip` / `eslint-disable` / `noqa` / `type: ignore` の追加と、`rm` / `git rm` によるテストファイル削除を検出して警告する品質ゲートを追加

## [0.2.5] - 2026-04-12

### Changed

- `pr-standards` ポリシーのブランチプレフィックス→ラベル対応表を GitHub の実ラベル体系 (`bug` / `enhancement` / `documentation` / `refactor` / `task`) に合わせて更新。`gh pr create` がラベル未存在で失敗する問題を解消 (`facets/policies/pr-standards.md`、`pr-create` / `issue-fix` スキル再生成)
- `CONTEXT_SPECS` をパッケージ manifest の `context_files` から動的に構築するようリファクタ。`orchestra-manager.py` のハードコード定義を廃止し、`core` / `codex-suggestions` / `gemini-suggestions` の manifest に `source` / `template` キーを追加。`Package` dataclass に `context_files` フィールドを追加し、`init()` の hardcoded テンプレートコピーも init リストを SSOT とする whitelist 方式のデータ駆動ループに置換 (#45)

### Fixed

- `quality-gates/turn-end-summary.py` の Stop hook 出力が Claude Code のスキーマ違反（`hookSpecificOutput` は Stop では不可）となり `JSON validation failed` を起こしていた問題を修正。`systemMessage` フィールドに変更

## [0.2.4] - 2026-04-11

### Added

- `/handoff` スキル: Claude Code のレート制限時に Codex CLI へタスクを引き継ぐ指示書ファイルを生成
- `/pr-create` スキル: 現在のブランチから PR を作成（テンプレート自動生成・ラベル自動決定）
- `pr-standards` ポリシー: PR 作成ルールを `pr-create` と `issue-fix` で共通化
- `context_files` key in package manifests for context file ownership (#36)
- `required_package` field in CONTEXT_SPECS for data-driven distribution (#36)
- `escalation-strategy` ルール: コンテキスト節約のためのツール選択ガイドライン（Glob → Grep count → Grep files → Grep content → Read offset/limit の段階的絞り込み、判断基準、アンチパターン）を `core` パッケージに追加 (#9)
- 探索系サブエージェント定義にコンテキスト効率セクションを追加: `general-purpose`, `researcher`, `code-reviewer`, `debugger`, `architecture-reviewer` (#11)
- `audit` パッケージ: `route-audit` + `cli-logging` を統合した統一イベントログ監査基盤 (#38)
  - 統一スキーマ v1（`v`, `ts`, `sid`, `eid`, `type`, `tid`, `ptid`, `aid`, `ctx`, `data`）
  - セッション単位のログローテーション（`sessions/{session_id}.jsonl`）
  - 新規イベント: `session_start`, `session_end`, `subagent_start`, `subagent_end`
  - トレース ID によるプロンプト→ルーティング→ツール実行の呼び出しチェーン追跡
  - CLI 呼び出しのエラー分類（timeout, auth, rate_limit 等）と生レスポンス記録
- 新しい hook イベントへの対応（Claude Code の最新 hook API に合わせた拡張）
  - `core/precompact-dump.py`: PreCompact イベントで working-context と Plans.md を
    `.claude/context/shared/precompact-{timestamp}.md` に退避（圧縮前の重要情報退避）
  - `audit/audit-instructions-loaded.py`: InstructionsLoaded イベントで CLAUDE.md / ルール等の
    ロード状況（`load_reason`, `file_path`, `globs`）を audit v1 ログに記録
  - `quality-gates/turn-end-summary.py`: Stop イベントでターン終了サマリーを注入
    （編集ファイル数、Plans.md の WIP/TODO/blocked 件数、lint 未実行リマインダー）
  - audit 統一スキーマに `instructions_loaded`, `turn_end`, `precompact` イベント型を追加
- `quality-gates/check-context-optimization.py`: PreToolUse(Read|Grep|Bash) で非効率な
  ツール使用 (Read 全文読み・Grep content モード乱用・Bash の cat/grep/find 等) を検出し、
  エスカレーション戦略への切り替えを提案する Hook を追加 (#10)
  - `audit-flags.json` に `features.context_optimization` フラグを追加（閾値・無効化対応）

### Changed

- `issue-workflow` パッケージを `git-workflow` に改名（責務拡大に伴う名称整理）
- `issue-fix` の PR 作成ロジックを PR Standards Policy 参照に簡素化
- Context templates now use `<YOUR_...>` placeholders instead of ai-orchestra-specific content (#37)
- AGENTS.md / GEMINI.md distribution is now conditional on package install state (#36)
- `route-audit` + `cli-logging` を `audit` パッケージに統合（#38）
- `/design` スキル: 既存コードがあるプロジェクトでは Phase 0（既存コード調査と影響範囲分析）を必ず先行実施するよう変更。`researcher` サブエージェント経由で中粒度の影響範囲（直接変更対象／依存関係／リスク）を調査し、成果物を `.claude/docs/impact-analysis/{date}_{slug}.md` に出力する

### Fixed

- `quality-gates` の `lint-on-save.py` が、編集ファイルの種別に応じて formatter / linter を切り替えられるよう改善

## [0.2.3] - 2026-03-30

### Added

- `release-readiness` を強化し、`pyright` を導入。あわせて release workflow を追加 (#26)

### Fixed

- `inject-shared-context` の hook 出力フォーマットを修正 (#27)

## [0.2.2] - 2026-03-22

### Added

- `review` のレビュー自動修正ループ機能を追加 (#21)
- facet composition に Knowledge 層と Scripts を導入 (#24)

### Changed

- manifest-SSOT アーキテクチャへの移行に伴い、`packages/skills` を廃止 (#22)
- `packages/rules` を廃止し、facet build へ完全委譲する構成に整理 (#25)

## [0.2.1] - 2026-03-22

### Added

- ファセットシステムを導入し、E2E テストを追加 (#19)

### Changed

- モジュール分割を進め、ドキュメント体系を整理 (#19)

## [0.2.0] - 2026-03-14

### Added

- コンテキスト共有基盤と指示書テンプレート管理を導入 (#17)

### Changed

- `design-tracker` の運用乖離と migration guide の記載不整合を整理 (#16)

## [0.1.0] - 2026-03-06

### Added

- AI Orchestra の初期リリース
- Claude Code + Codex CLI + Gemini CLI のエージェントルーティング
- `Plans.md` による SSOT タスク管理
- PyPI パッケージ `orchex` として公開
- hook による自動品質ゲート
