# orchex CLI（orchestra-manager）評価セット

**対象**: `scripts/orchestra-manager.py`（CLI 本体）+ `scripts/lib/*.py`（`orchestra_hooks` / `orchestra_context` / `sync_engine` / `facet_builder` / `orchestra_models` / `gitignore_sync` / `toml_merge` / `agent_model_patch` / `settings_io` / `scaffold`）+ `ai_orchestra/cli.py`（`pip install orchex` 経由のエントリポイント）
**対象テストファイル（SSOT、参考）**: `tests/unit/test_orchestra_manager_core.py`, `test_orchestra_manager_context.py`, `test_orchestra_manager_force_flag.py`, `test_orchestra_manager_gitignore.py`, `test_orchestra_manager_run_passthrough.py`, `test_orchestra_manager_proxy.py`, `test_orchestra_manager_cocoindex_uninstall.py`, `test_orchestra_manager_gaps.py`, `test_orchestra_manager_help.py`, `test_ai_orchestra_cli.py`
**テスト所有権マッピング（解消済み、Issue #237）**: `packages/quality-gates/hooks/evaluation-set-checker.py` はかつて `packages/<pkg>/tests/` ディレクトリ名、またはトップレベル `tests/` 配下ファイル名のパッケージ名トークンマッチでのみ担当パッケージを判定していたため、`packages/` 配下にディレクトリを持たない本評価セットのテストファイル群は自動識別されず（`test_orchestra_manager_core.py` は `core` パッケージへの誤マッチも発生していた）、人手での突合が必要だった。`.claude/config/quality-gates/evaluation-set-mapping.yaml`（`orchex-cli` の `test_globs` に `tests/unit/test_orchestra_manager_*.py`/`tests/unit/test_ai_orchestra_cli.py` を明示登録）の追加により、上記テストファイル群は自動的に本評価セットへ誘導されるようになった
**類型**: CLI ツール型
**作成日**: 2026-07-15
**最終レビュー日**: 未レビュー（本 PR で新規作成。人間レビュー後に更新する）
**情報源**: README.md（コマンド一覧・「主要コマンド」節）, `.claude/rules/config-loading.md`, `.claude/rules/orchestra-usage.md`, `scripts/orchestra-manager.py`, `scripts/lib/*.py`, `ai_orchestra/cli.py`（実装挙動。仕様書として独立した設計ドキュメントは存在しないため、多くの観点の根拠は「実装挙動」となる）

## 1. 責務定義

orchex CLI（`scripts/orchestra-manager.py`、配布後は `orchex` / `ai-orchestra` コマンド）は、ai-orchestra が提供するパッケージ（hooks・agents・config・skills 等）を導入先プロジェクトへ install / uninstall / enable / disable / setup し、`.claude/` 配下の設定・指示書・フック登録をベース定義と同期させる。同期は「配布時ハッシュとの比較によりユーザー編集済みファイルを黙って上書き・削除しない」という安全側の方針を貫き、`*.local.*` によるプロジェクト固有の上書きを壊さない。あわせて、パッケージ付属スクリプトの実行（`run`/`scripts`）、指示書テンプレートの再生成・突合（`context build/check/sync`）、facet composition からの SKILL.md 生成（`facet build/extract`）、cocoindex mcp-proxy の操作委譲（`proxy stop/status`）、meta-harness への委譲実行（`meta`）を提供する。

### Non-Goals

- mcp-proxy のライフサイクル仕様そのもの（起動タイミング・永続化・reconnect 通知等）— `proxy stop/status` は cocoindex パッケージの `proxy_manager` に処理を委譲するのみで、詳細仕様は `docs/evaluation/cocoindex.md` が正本
- facet composition のマージアルゴリズム（`packages/*/facets/` 由来の複数ソースをどう合成するか）の詳細仕様 — CLI コマンド契約（`facet build`/`extract` の入出力・冪等性）のみを本セットの対象とし、合成ロジックの詳細は将来 facet 専用の評価セットで扱う候補とする
- meta-harness 自体の実行仕様（`packages/meta-harness/scripts/meta_harness.py` の挙動）— `docs/evaluation/meta-harness.md` が正本。orchex 側は「委譲経路（env 継承・引数転送・終了コード伝播）」のみが対象
- Codex/Antigravity 個別ハーネスの詳細（`.codex/config.toml` の TOML マージ内部仕様等）— `docs/evaluation/codex-harness.md` 等の関連パッケージ評価セットが対象範囲。orchex 側は「同期の呼び出し契約」のみを扱う

## 2. 期待する入出力・副作用

| 構成要素                          | 入力                                                      | 期待する出力                                                     | 副作用                                                                                     |
| --------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `list`                            | なし                                                        | パッケージ一覧を標準出力                                            | なし（読み取り専用）                                                                          |
| `status [--project]`              | プロジェクトパス                                            | 各パッケージの installed/active/partial/`not found`（スペース区切り）状態表 | なし（読み取り専用）                                                                          |
| `install <package...> [--dry-run] [--force]` | パッケージ名（複数可）・プロジェクトパス                    | インストール結果の標準出力                                          | `.claude/` 配下への config/agents/context ファイルコピー、`settings.local.json` へのフック登録、`orchestra.json` 更新（installed_packages・file_hashes） |
| `uninstall <package> [--dry-run]` | パッケージ名                                                | アンインストール結果の標準出力                                       | 未変更配布ファイルの削除、フック解除、`orchestra.json` 更新                                    |
| `enable <package>` / `disable <package>` | パッケージ名                                                | 有効化/無効化結果                                                    | `settings.local.json` のフックエントリ追加/削除のみ（`installed_packages` は不変）             |
| `run <package> <script> [-- args]` | パッケージ名・スクリプト名（短縮名/ファイル名/フルパス）・追加引数 | サブプロセスの標準出力をそのまま透過                                 | 対象スクリプトの subprocess 実行、終了コードをそのまま返す                                     |
| `scripts [--package]`             | パッケージ名（任意）                                          | 実行可能スクリプト一覧                                               | なし（読み取り専用）                                                                          |
| `context build [--dry-run]`       | `templates/context/*.md`                                   | `templates/project/CLAUDE.md` 等の生成物                             | テンプレートソースからの再生成                                                                 |
| `context check`                   | 生成物 vs テンプレートソース                                | ドリフト有無（bool、CLI は非ゼロ終了で通知）                          | なし（読み取り専用）                                                                          |
| `context sync --project [--dry-run] [--force]` | プロジェクトパス                                            | 同期結果の標準出力                                                   | 欠落ファイルの作成（`--force` 時のみ既存ファイル上書き）、legacy 生成物（旧 `GEMINI.md` 等）の削除 |
| `proxy stop` / `proxy status`     | プロジェクトパス                                            | mcp-proxy の停止結果 / 状態表示                                      | cocoindex `proxy_manager` への委譲（`.claude/orchestra.json` の `installed_packages` に `cocoindex` が無い場合はエラー終了。詳細は EV-29）    |
| `facet build [--name] [--target]` / `facet extract` | composition 名（任意）・プロジェクトパス                    | SKILL.md 等の生成結果                                                | `.claude/skills/` 等への生成物書き込み                                                        |
| `meta <args...>`                  | meta-harness への引数                                        | meta-harness の標準出力をそのまま透過                                | `AI_ORCHESTRA_DIR` を継承した subprocess 実行、終了コードをそのまま返す                        |
| `setup [<preset>] [--project] [--dry-run]` | preset 名（省略可）                                          | preset 省略時は一覧表示、指定時はインストール結果                     | 解決済みパッケージ群を依存順で `install` するのと同じ副作用                                    |
| `ai_orchestra/cli.py`（`orchex` エントリポイント） | `sys.argv`（`-v`/`--version` 含む）                          | バージョン文字列、または `orchestra-manager.py` への委譲実行結果       | AI Orchestra ルートディレクトリの解決（同梱 → 開発時リポジトリ直下 → `AI_ORCHESTRA_DIR`）      |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `presets.json` の `packages: "__all__"` は `load_presets()` によって全パッケージ名に展開される — 根拠: 実装挙動（`OrchestraManager.load_presets`）
- [ ] EV-02（正常 / must）: preset の `exclude` キーに列挙したパッケージ名は、`__all__` 展開後の一覧・明示リストの両方から除外される — 根拠: Issue #221（tmux-monitor opt-in 化での追加要件）、実装挙動
- [ ] EV-03（境界 / should）: `exclude` に存在しないパッケージ名を指定してもエラーにならず無視される — 根拠: 実装挙動
- [ ] EV-04（正常 / must）: 複数パッケージを指定した `install` はパッケージ間の `depends` 関係をトポロジカルソートしたインストール順で実行される — 根拠: 実装挙動（`resolve_install_order`）
- [ ] EV-05（異常 / must）: 依存関係に循環が検出された場合、`resolve_install_order` は警告を出力し元の指定順にフォールバックする（クラッシュしない） — 根拠: 実装挙動
- [ ] EV-06（正常 / must）: プロジェクトが未初期化の状態で `install` を実行すると、`init` を自動実行してから続行する — 根拠: 実装挙動
- [ ] EV-07（異常 / should）: 依存パッケージが未インストールの場合、`install` は警告を出すが処理はブロックせず継続する — 根拠: 実装挙動
- [ ] EV-08（異常 / must）: 配布済み **config** / **agents** ファイルがインストール後にユーザーに変更されている場合、再インストール・再同期時は配布時ハッシュとの比較により上書きをスキップし警告する — 根拠: 実装挙動（`_copy_config_if_safe`。`pkg.config` のうち `config/` プレフィックスのファイルのみが対象）。**解消済み（Issue #236）**: `install()` は `_copy_config_if_safe` によるハッシュ保護コピーの直後に、同一呼び出し内で `run_initial_sync()` を実行するが、`run_initial_sync()` の config カテゴリ再同期にも同じ配布時ハッシュ比較ガードを追加し、`needs_sync()`（mtime 比較）のみによる再上書きを防止した（`scripts/orchestra-manager.py` `run_initial_sync`）。**解消済み（Issue #241）**: agents ファイルにも同等のハッシュガードを追加した。`sync_engine.is_user_modified()` を共通ヘルパーとして新設し、`run_initial_sync()`（config/agents 両カテゴリ）と `sync_packages()`（SessionStart hook が毎セッション呼ぶ経路、agents カテゴリ）の双方に適用。`sync_packages()` 側は `needs_sync()`（mtime 比較）が同期対象と判定した場合のみハッシュ検証するため、無変更時の毎セッションコストは増加しない。ハッシュ未記録（旧 orchestra.json / この修正以前に同期されたファイル）の場合は安全側だが後方互換を優先し、初回のみ従来どおり上書きしてから記録を開始する（`_copy_config_if_safe` と同じ方針）。**PR #244 レビュー指摘対応**: `patch_all_agents()`（model オーバーライド適用）はハッシュ記録後にファイル内容を書き換えるため、パッチ済み内容を誤ってユーザー編集と判定してしまう不具合があった。`patch_all_agents_paths()`（実際にパッチしたパスを返す）と `refresh_patched_agent_hashes()` を追加し、パッチ適用直後に台帳をパッチ後の内容で更新し直すことで解消した。
- [ ] EV-09（正常 / must）: 未変更の配布ファイルは再インストール時に最新版へ更新される — 根拠: 実装挙動
- [ ] EV-10（異常 / must）: `uninstall` は配布時ハッシュと現在の内容が一致するファイルのみを削除し、ユーザー変更済み・ハッシュ未記録のファイルは削除せず警告する（安全側スキップ） — 根拠: 実装挙動（`_remove_if_unchanged`）
- [ ] EV-11（境界 / must）: `.codex/` 配下配布物の削除は、manifest の target が絶対パスまたは `../` でプロジェクト外を指す場合に削除をスキップし警告する — 根拠: 実装挙動（`_remove_codex_file_if_unchanged` の `_is_within_project` 境界チェック）
- [ ] EV-12（正常 / must）: `install`/`uninstall`/`enable`/`disable` はいずれも `settings.local.json` へのフック登録・解除を冪等に行う（再実行しても重複登録・二重削除エラーが起きない） — 根拠: 実装挙動
- [ ] EV-13（正常 / must）: `disable` はフック登録のみを解除し `installed_packages` からは削除しない — 根拠: 実装挙動（`disable`）。**解消済み（Issue #236）**: `enable` は実行前に `.claude/orchestra.json` の `installed_packages` を確認し、未インストールのパッケージに対してはエラーメッセージを出して非ゼロ終了する（フックを登録しない）よう修正した。`disable` 自体の「installed_packages を変更しない」仕様は変更していない
- [ ] EV-14（正常 / must）: `status` は各パッケージを installed／active（他パッケージの依存として有効）／partial（一部フック欠落）／`not found`（スペース区切り。アンダースコアではない）のいずれかに分類する — 根拠: 実装挙動（`get_package_status`）
- [ ] EV-15（境界 / must）: `init` は複数回実行しても既存ディレクトリ・既存テンプレートファイル・`orchestra.json` を破壊せず、初回実行時と同じ結果になる（ディレクトリは `exist_ok`、テンプレートは欠落時のみコピー） — 根拠: 実装挙動
- [ ] EV-16（正常 / must）: `setup <preset>` は preset 解決後のパッケージ一覧を依存順でインストールし、preset 省略時は一覧表示に切り替わる — 根拠: 実装挙動
- [ ] EV-17（正常 / must）: `context build` はテンプレートソース（`templates/context/*.md`）から `templates/project/CLAUDE.md` 等の生成物と `AGENTS.md`（Codex/Antigravity セクション合成）を再生成する — 根拠: README.md「指示書テンプレートの責務」
- [ ] EV-18（正常 / should）: `context check` は生成物とテンプレートソースの間にドリフトがある場合 false を返し、CLI は非ゼロ終了する — 根拠: 実装挙動
- [ ] EV-19（正常 / must）: `context sync --project` は欠落ファイルを作成し、`--force` 無指定では既存ファイルを保持する。`--force` 指定時のみ既存ファイルを上書きする — 根拠: 実装挙動
- [ ] EV-20（境界 / must）: `context sync` はシンボリックリンクされたターゲット、およびプロジェクト外を指すシンボリックリンク親ディレクトリへの書き込みをスキップする — 根拠: 実装挙動（symlink escape 防御）
- [ ] EV-21（正常 / must）: 旧命名の生成物（例: 生成された `GEMINI.md`）は `sync` 時に削除されるが、手書きの `GEMINI.md` は保持される — 根拠: 実装挙動（agy 移行に伴う後方互換）
- [ ] EV-22（正常 / must）: config 同期（`install`）はベース設定ファイルのコピーのみを行い、`*.local.yaml`/`*.local.json` は書き換えずそのまま保持する。`config-loading` ルールが定めるベース設定と local override のディープマージ（local 未定義キーはベース値を継続使用）は、同期時ではなく読み込み時に消費側の `hook_common.load_package_config()` が行う — 根拠: 実装挙動（`_copy_config_if_safe`、`packages/core/hooks/hook_common.py` の `deep_merge`/`load_package_config`）、`.claude/rules/config-loading.md`。**注記**: `context sync`（`orchestra_context.py` の `context_sync`）は `.claude/config/**` を一切扱わない。トップレベルの `CLAUDE.md`/`AGENTS.md` 等（`CONTEXT_SPECS`、manifest の `context_files` 由来）の同期と legacy `GEMINI.md` の cleanup のみが対象であり、本 EV の「config 同期」には含まれない
- [ ] EV-23（正常 / must）: 旧 `gemini.enabled: false` / `tool: gemini` は `antigravity.enabled: false` / `tool: antigravity` として読み替えられ、明示的な `antigravity` 設定を上書きしない — 根拠: `.claude/rules/antigravity-delegation.md`（移行エイリアス節）
- [ ] EV-24（異常 / must）: sync の stale ファイル削除は、配布物として同期しなくなったファイルのみを対象とし、`*.local.*` および facet 管理下のファイル・参照は削除しない — 根拠: 実装挙動

## 4. 類型別観点

<!-- docs/evaluation/README.md の CLI ツール型チェックリストを具体化。ID は EV-NN の連番を継続する -->

- [ ] EV-25（正常 / must）: コマンド契約 — `orchex run <package> <script> [-- args...]` は `--` 以降を対象スクリプトへの引数として分離し、`--orchestra-dir` 等のグローバルオプションと混同しない — 根拠: 実装挙動（`_split_run_passthrough`）
- [ ] EV-26（異常 / must）: 入力バリデーション — `run_script`/`resolve_script_path` はパッケージ manifest の `scripts` エントリに対してのみ短縮名・ファイル名・フルパスの3形式で解決するホワイトリスト方式であり、未登録の名前・存在しないパッケージ・存在しないスクリプトファイルに対しては非ゼロ終了のエラーメッセージ（利用可能一覧を含む）を返す — 根拠: 実装挙動
- [ ] EV-27（異常 / must）: 入力バリデーション — 存在しないパッケージ名・preset 名を指定した `install`/`uninstall`/`enable`/`disable`/`setup` はクラッシュせず、エラーメッセージを stderr に出力し非ゼロ終了する — 根拠: 実装挙動
- [ ] EV-28（境界 / must）: 破壊的操作の安全策 — `install`/`uninstall`/`context sync` の `--dry-run` は実ファイルの書き込み・削除・`orchestra.json`/`settings.local.json` の変更を一切行わないことが望ましい — 根拠: 実装挙動。**解消済み（Issue #236）**: `uninstall --dry-run` で対象パッケージが最後の `installed_packages` エントリだった場合に `remove_sync_hook(settings)` / `save_settings()` が `dry_run` 分岐の外側で無条件に呼ばれ `settings.local.json` が実際に書き換わっていたバグを修正し、`dry_run` 時は書き込みを行わず `[DRY-RUN]` プレビュー表示のみを行うよう変更した（`scripts/orchestra-manager.py` `uninstall()` の `if not installed:` ブロック）
- [ ] EV-29（正常 / should）: コマンド契約 — `proxy stop`/`proxy status` は cocoindex パッケージの `proxy_manager` に処理を委譲する。導入判定は `.claude/orchestra.json` の `installed_packages` に `cocoindex` が含まれるかどうかで行い、含まれない場合はエラーメッセージを返して非ゼロ終了する（config ファイルの発見可否は判定に用いない） — 根拠: 実装挙動（`_require_cocoindex_installed`、`_load_proxy_modules`）。**解消済み（Issue #236）**: 従来は `hook_common.load_package_config()`（`find_package_config()`: プロジェクト `.claude/config/cocoindex/` → `$AI_ORCHESTRA_DIR/packages/cocoindex/config/` の順で探索）が config を発見できるか否かで判定していたため、`AI_ORCHESTRA_DIR` が ai-orchestra リポジトリ自体を指す通常構成ではベース設定が常に発見され、cocoindex 未導入プロジェクトでもエラー分岐が実質的に発火しなかった。導入判定を `installed_packages` ベースに変更し、この既知のギャップを解消した
- [ ] EV-30（正常 / must）: manifest hooks エントリの `timeout`（正の int 秒）は `settings.local.json` 登録時に反映され、未指定時は既定 5 秒（後方互換）。既存登録の timeout が manifest と異なる場合も SessionStart 同期（`sync_hooks`）で補正される。不正値（0 以下・非 int・bool）は既定値へフォールバックする — 根拠: 実装挙動（`hook_utils.parse_hook_entry` / `add_hook_to_settings`、`sync_engine.sync_hooks`）。PR #337 レビュー対応（T3）で追加、人間レビュー保留
- [ ] EV-30（正常 / must）: コマンド契約 — `meta` サブコマンドは `AI_ORCHESTRA_DIR` を継承した subprocess で `packages/meta-harness/scripts/meta_harness.py` に残り引数をそのまま渡し、その終了コードをそのまま返す — 根拠: 実装挙動
- [ ] EV-31（異常 / must）: 入力バリデーション — `orchex` エントリポイント（`ai_orchestra/cli.py`）は AI Orchestra ルートを「同梱パッケージ内 → 開発時のリポジトリ直下 → `AI_ORCHESTRA_DIR` 環境変数」の順に解決し、いずれも解決できない場合はエラーメッセージを出し非ゼロ終了する — 根拠: 実装挙動（`get_orchestra_dir`）
- [ ] EV-32（正常 / should）: コマンド契約 — `orchex -v`/`--version` は `orchestra-manager.py` への委譲を行わず `ai_orchestra.__version__` を出力する — 根拠: 実装挙動
- [ ] EV-33（正常 / should）: facet コマンド契約 — `facet build`/`facet extract` は `--name` 省略時に全 composition を対象にする。composition マージの詳細仕様は Non-Goals の通り対象外 — 根拠: 実装挙動
- N/A: 出力の安定性 — `list`/`status` は人間可読テキストのみを出力し、機械可読出力（JSON 等）の契約は現状定義されていない（将来 `--json` 等を追加する場合は本項を EV 化する）
- [ ] EV-34（正常 / must）: `init` は CLI サブコマンドとして露出し `orchex init --help` が引ける — 根拠: 実装挙動、PR #302
- [ ] EV-35（正常 / must）: COMMAND_REGISTRY の keys と subparsers.choices は常に一致し、トップレベル help のグループ化一覧は registry から導出される — 根拠: 実装挙動、ドリフト防止テスト
- [ ] EV-36（正常 / must）: 全トップレベルコマンドは description と 1 件以上の examples を help に表示する — 根拠: 実装挙動
- [ ] EV-37（正常 / must）: hook 起動のインタプリタ解決 — `settings.local.json` に登録される hook コマンド（パッケージ hook / sync-orchestra hook の両方）は `"${AI_ORCHESTRA_PYTHON:-python3}"` 経由で Python を起動する。利用者が起動する `init`/`setup`/`install`/`enable` は `~/.claude/settings.json` の `env.AI_ORCHESTRA_PYTHON` が未設定、または設定値が実行可能な インタプリタとして解決できない（`shutil.which` が None）場合に現在の `sys.executable` を 書き込む。解決できる値（絶対パス・`PATH` 相対名のどちらも）は利用者の明示指定として 上書きしない。SessionStart 同期（`sync_hooks`）はこの env を書き換えない — hook 自身は `PATH` の `python3` で起動されうるため、そこで観測した `sys.executable` を固定すると 誤ったインタプリタを恒久化してしまう。未設定の既存導入は `PATH` の `python3` にフォール バックし、利用者が上記コマンドを 1 度実行するまで従来挙動のままとなる — 根拠: Issue #343、実装挙動（`orchestra_hooks._resolve_python_update`）
- [ ] EV-38（正常 / must）: 旧インタプリタ表記からの移行 — 旧形式（リテラル `python3`）で 登録済みのプロジェクトは、SessionStart 同期（`sync_hooks`）と `install`/`enable`/`uninstall`/`disable`（`_apply_hooks`）の両経路で現行形式へ書き換えられる。新旧が併存する 場合は同一エントリ内で 1 件へ畳み、hook の二重起動を残さない。移行対象は ai-orchestra が 登録した hook（`packages/*/hooks/*` と `scripts/sync-orchestra.py`）に限り、利用者自身が 登録した `python3` hook は書き換えない。dry-run では移行も行わない — 根拠: Issue #343、実装挙動（`hook_utils.migrate_hook_interpreters` / `canonical_hook_command`）。**既知の制限**: 旧形式のみが登録された状態では、SessionStart 同期または `install`/`enable` が 走るまで `status` が当該パッケージを partial と表示する

## 5. テストレビュー判断基準（パッケージ固有）

- 配布ファイルの安全側スキップ（EV-08〜EV-11）を検証するテストは、「ハッシュ未記録」「ハッシュ不一致（ユーザー変更）」「ハッシュ一致（未変更）」の3状態を独立したケースとして区別しているか。1ケースに複数状態を混在させて期待値を曖昧にしていないか。
- `init` の冪等性（EV-15）テストは、`install` 経由の間接検証だけでなく `OrchestraManager.init()` を直接複数回呼び出し、2回目の実行が1回目と同じファイル内容・ディレクトリ構成になることを検証しているか。
- symlink escape 防御（EV-20）・path escape 防御（EV-11）のテストは、実際にシンボリックリンク/`../`を含むパスを用意した境界値テストになっているか（正常系のディレクトリ構造のコピーのついでに確認していないか）。
- `resolve_install_order` の循環フォールバック（EV-05）テストは、警告メッセージの有無ではなく、フォールバック後も `install` 処理自体が異常終了しないことまで確認しているか。
- legacy 読み替え（EV-21, EV-23）のテストは、「読み替えられるケース」と「明示設定が優先されるケース」の両方を独立に検証しているか（片方のみでは後方互換の破壊を検出できない）。
