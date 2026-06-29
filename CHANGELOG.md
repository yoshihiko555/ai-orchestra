# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`packages/codd`: impact 分析（変更影響の信頼度3帯域分類・Issue #94 / Phase 2）**: 変更 diff から下流ドキュメントへの影響を **Green（自動更新可）/ Amber（要確認）/ Gray（参考）** に分類する `codd impact --diff <ref>` を追加。Phase 1 の素朴な drift（コミット時刻比較）を、宣言された依存関係を証拠とした信頼度スコアへ発展させた
  - **信頼度スコア**: `git diff --name-status <ref>` の変更ファイルを frontmatter の `node_id` にマップし、`depends_on` の逆引きで下流を辿る（サイクル安全・`max_hops` 打ち切り）。`path_score = min(経路上の relation 重み) × decay^(hops-1)`、ノードは全経路・全起点の最良値を採る。重み（derives_from/refines/implements=1.0, supersedes=0.6, references=0.3）・閾値・減衰は `codd.yaml` の `impact:` ブロックで上書き可能
  - **帯域補正**: Corroboration rule（Green は「直接の強依存=事実」か「裏付け起点≥2」のみ。多段単一経路は Amber 上限）と co_changed cap（下流自身も同一 diff で変更済みなら Amber 上限にフラグ。スコアは下げず破壊的変更を Gray に隠さない）を適用。削除された上流ファイルは dangling 注意として別建て報告
  - **出力**: テキスト（帯域別）と `--json`（CI/機械処理向け）。スキル `/codd-impact` を facet build で配布（`.claude/skills/` と `.agents/skills/`）
  - **設計判断（ADR-026 D3）**: CODD は依存宣言を frontmatter に限定するため、証拠源は relation 種別とグラフ距離のみ。参考実装 codd-dev の Noisy-OR・エビデンス種別分類はコード静的解析由来の多様な証拠を確率合成する設計のため適用せず、Corroboration / testimony cap の思想のみ借用。設計は `docs/design/codd-coherence-layer.md` 4.5.1 に記録

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
