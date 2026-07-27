# codd 評価セット

**パッケージ**: `packages/codd`
**類型**: CLI ツール型
**作成日**: 2026-07-03
**最終レビュー日**: 評価保留（2026-07-04）— パッケージ実装が未完了のため、実装完了後に改めて人間レビューを行う。それまで本評価セットの観点は暫定（ドラフト）扱いとし、テスト改修時の突合基準としては未確定とする。EV-26〜EV-53（Issue #98 / コード⇔ドキュメントのトレーサビリティ）は 2026-07-27 追加、同様に人間レビュー保留。
**情報源**: docs/design/codd-coherence-layer.md, docs/requirements/coherence-guardrail.md, .claude/rules/codd-frontmatter-policy.md（補助: packages/codd/manifest.json — 構成要素の列挙のみ）

## 1. 責務定義

codd は AI Orchestra 自身および導入先プロジェクトが生成するドキュメント群（要件・設計・ADR・計画・ルール・指示書）の依存関係を、各ドキュメント先頭の `codd:` フロントマターブロックで宣言させ、`scan` によって依存グラフを構築する。`validate` はそのグラフを検査し、リンク切れ・重複・循環・未定義語彙をエラーとして、孤立・ドリフト・フロントマター欠落を警告として検出する。`impact` は diff から下流ドキュメントへの影響を Green/Amber/Gray の信頼度帯域で分類する。essential プリセットとして全導入先へ配布され、`/codd-scan` `/codd-validate` `/codd-impact` スキル経由でも利用できる。加えて `code_scope.include`（opt-in・既定空、Issue #98）でソースファイルを指定すると、1行の軽量注釈（`codd:<key> <value>`）から `code` / `test` ノードを抽出し、同じグラフへ低信頼度（既定 `inline_confidence: 0.7`）のリンクとして統合する。

### Non-Goals

- hook 自動配線（PostToolUse scan / pre-commit validate）の導入先展開 — Phase 2 以降、別Issue（codd-hook-distribution）
- CI（PR への verdict 投稿） — 別Issue（codd-ci-guardrail）
- ノードのサブ粒度化（1ファイル内 FT-xxx 等の細粒度ノード化） — 別Issue（codd-subnode-granularity、D7）
- コード全体へのフロントマター強制（code_scope は opt-in。未注釈ファイルは missing_frontmatter を出さず黙ってスキップする） — Issue #98 の設計判断（4.3.1）

## 2. 期待する入出力・副作用

| 構成要素                       | 入力                                                                | 期待する出力                                                           | 副作用                                                                                                                              |
| ------------------------------ | ------------------------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `scan`                         | scope 内 `.md` 群 + `codd.yaml`（include/exclude）                  | 依存グラフ                                                             | `.claude/codd/graph.jsonl` への書き込み（上書き/追記は不明。情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡） |
| `validate`                     | フロントマター付きドキュメント群（`scan` 済みグラフとの関係は不明） | error/warning レポート、error 件数 > 0 で非ゼロ終了                    | 標準出力へのレポート出力のみ（想定）                                                                                                |
| `impact --diff <ref> [--json]` | git diff 対象（`ref` 省略時 `HEAD`）                                | Green/Amber/Gray に分類された下流ノード一覧（`--json` で機械可読形式） | なし（読み取り専用の想定）                                                                                                          |
| `graph`（可視化）              | 不明（設計書に記載なし）                                            | 不明（設計書に記載なし）                                               | 不明（設計書に記載なし）                                                                                                            |
| `/codd-scan` スキル            | ユーザー起動                                                        | `scan` 実行結果の要約報告                                              | `graph.jsonl` 更新（`scan` 経由）                                                                                                   |
| `/codd-validate` スキル        | ユーザー起動                                                        | `validate` 結果の要約報告                                              | なし                                                                                                                                |
| `/codd-impact` スキル          | ユーザー起動 + diff 対象                                            | `impact` 分類結果の要約報告                                            | なし                                                                                                                                |
| `codd:` フロントマターブロック | ドキュメント作成/編集者による記述                                   | node_id/kind/status/depends_on/owner の宣言                            | なし（宣言のみ）                                                                                                                    |

## 3. 評価観点

- [ ] EV-01（正常 / must）: 各ドキュメント先頭に `codd:` ブロック（node_id/kind/status/depends_on/owner）を宣言できる — 根拠: docs/requirements/coherence-guardrail.md (FR-1), docs/design/codd-coherence-layer.md §4.2
- [ ] EV-02（正常 / must）: parser はドキュメント先頭の YAML frontmatter ブロックのみを読み、本文中のコードブロック内 `---` や YAML 例を依存として誤認しない — 根拠: docs/design/codd-coherence-layer.md §4.2 (M-1), .claude/rules/codd-frontmatter-policy.md
- [ ] EV-03（正常 / must）: `scan` はスコープ内ドキュメントから依存グラフを構築し `.claude/codd/graph.jsonl` に出力する — 根拠: docs/requirements/coherence-guardrail.md (FR-2, 受け入れ基準), docs/design/codd-coherence-layer.md §4.6
- [ ] EV-04（異常 / must）: `depends_on.id` が既存 node_id に存在しない場合、`validate` は dangling として error 判定し非ゼロ終了する — 根拠: docs/design/codd-coherence-layer.md §4.5, docs/requirements/coherence-guardrail.md (FR-3, NFR-3)
- [ ] EV-05（異常 / must）: 同一 node_id が複数ドキュメントに存在する場合、`validate` は duplicate として error 判定する — 根拠: docs/design/codd-coherence-layer.md §4.5, docs/requirements/coherence-guardrail.md (FR-3)
- [ ] EV-06（異常 / must）: `depends_on` を辿って循環が生じる場合、`validate` は cycle として error 判定する — 根拠: docs/design/codd-coherence-layer.md §4.5, docs/requirements/coherence-guardrail.md (FR-3)
- [ ] EV-07（異常 / must）: 未定義の kind / relation / status を使用した場合、`validate` は unknown として error 判定する — 根拠: docs/design/codd-coherence-layer.md §4.5, docs/requirements/coherence-guardrail.md (FR-3)
- [ ] EV-08（境界 / should）: scope 内なのに `codd:` ブロックが無いドキュメントは missing_frontmatter として warning 判定される（error にはならない） — 根拠: docs/design/codd-coherence-layer.md §4.5
- [ ] EV-09（境界 / should）: 被参照ゼロかつ参照ゼロのノードは orphan として warning 判定されるが、`roots` 指定 kind（requirement, instruction）は除外される — 根拠: docs/design/codd-coherence-layer.md §4.5, §4.6
- [ ] EV-10（境界 / should）: 上流ノードの最終コミット時刻が下流ノードより新しい場合 drift として warning 判定され、時刻ソースは `git log -1 --format=%ct` のコミット時刻を優先し、未コミット（ワーキングツリーのみ）の場合のみファイル mtime にフォールバックする — 根拠: docs/design/codd-coherence-layer.md §4.5 (H-3)
- [ ] EV-11（正常 / must）: `status` の許容語彙は kind に依存する（adr: proposed/accepted/rejected/superseded/deprecated、requirement/design/plan/rule/instruction: draft/active/deprecated）。範囲外の値は unknown 扱いになる — 根拠: docs/design/codd-coherence-layer.md §4.2 (M-2), .claude/rules/codd-frontmatter-policy.md
- [ ] EV-12（正常 / must）: `node_id` は `<kind>:<file-slug>` 形式であり、1 ファイル = 1 ノードとして扱う（集約ファイルも全体で1ノード） — 根拠: docs/design/codd-coherence-layer.md §4.3 (D7)
- [ ] EV-13（正常 / should）: AI Orchestra 自身のドキュメント群に対して `validate` を実行すると error 0 で通る（ドッグフード） — 根拠: docs/requirements/coherence-guardrail.md (受け入れ基準)
- [ ] EV-14（正常 / must）: `impact --diff <ref>` は変更/削除ファイルを frontmatter の node_id にマップし、`depends_on` の逆引き（incoming）で下流ノードをサイクル安全に列挙する — 根拠: docs/design/codd-coherence-layer.md §4.5.1
- [ ] EV-15（境界 / must）: impact のノードスコアは `score >= 0.8` で Green、`>= 0.4` で Amber、それ未満で Gray に分類される — 根拠: docs/design/codd-coherence-layer.md §4.5.1
- [ ] EV-16（境界 / must）: Green 判定は「1 hop・強 relation の直接依存」または「裏付け起点が corroboration_min_origins（2）以上」の場合のみ許可され、多段単一経路（推論的）は Amber 上限になる（Corroboration rule） — 根拠: docs/design/codd-coherence-layer.md §4.5.1
- [ ] EV-17（境界 / should）: 下流ノードが同一 diff 内で既に変更済みの場合、co_changed cap により Amber 上限としてフラグ表示される（スコア自体は下げない） — 根拠: docs/design/codd-coherence-layer.md §4.5.1
- [ ] EV-18（正常 / must）: relation は `derives_from` / `refines` / `implements` / `references` / `supersedes` の5種のみが有効である — 根拠: docs/design/codd-coherence-layer.md §4.4, .claude/rules/codd-frontmatter-policy.md
- [ ] EV-19（正常 / must）: 依存宣言の正本はドキュメント内フロントマター1箇所のみであり、外部の `doc_links.yaml` 等の二重管理は行わない — 根拠: docs/requirements/coherence-guardrail.md (NFR-2), docs/design/codd-coherence-layer.md (D3)
- [ ] EV-20（正常 / must）: `packages/codd` は essential プリセットに含まれ、`setup essential` で導入先に `config/codd.yaml` → `.claude/config/codd/codd.yaml`、skill/rule → facet build 経由で自動展開される — 根拠: docs/requirements/coherence-guardrail.md (FR-4), docs/design/codd-coherence-layer.md §4.8

## 4. 類型別観点

<!-- docs/evaluation/README.md の CLI ツール型チェックリストを具体化。ID は EV-NN の連番を継続する -->

- [ ] EV-21（正常 / must）: コマンド契約 — `scan` / `validate` / `impact` は `orchex run codd codd -- <subcommand>` として後方互換に呼び出せる — 根拠: docs/design/codd-coherence-layer.md §4.8
- [ ] EV-22（異常 / should）: 入力バリデーション — 壊れた frontmatter（YAML パース不能）や存在しない scope パスに対して、CLI はクラッシュせず適切なエラーメッセージを返す。`scope.include` / `code_scope.include` の型不正や `impact.*` への bool 混入（`_as_glob_list` / `_reject_bool_as_number` が投げる `ValueError` / `TypeError`）も `main()` が捕捉し、トレースバックではなく `[codd] ERROR: ...`（非ゼロ終了）として整形する — 根拠: 実装挙動（設計書・要件書に明示なし）、packages/codd/scripts/codd.py（`main`）
- [ ] EV-23（境界 / should）: 破壊的操作の安全策 — `scan` による `graph.jsonl` の再構築時、書き込み失敗（中断等）が既存グラフを破損させない — 根拠: 実装挙動（設計書に上書き/追記方針の明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）
- [ ] EV-24（正常 / should）: 出力の安定性 — `impact --json` は機械可読 JSON を返す（フラグの存在は設計書に明記。フィールド構成は実装挙動） — 根拠: docs/design/codd-coherence-layer.md §4.5.1（フラグ存在）、実装挙動（スキーマ詳細）
- [ ] EV-25（境界 / must）: 設定レイヤリング — `codd.yaml` の `checks` レベルは `codd.local.yaml` で上書き可能（`config-loading` ルール準拠、同期対象外で sync により消えない）。検査レベルに `off` を指定する場合、YAML 1.1 では bare `off` が boolean `False` と解釈されるが、`normalize_check_level`（`packages/codd/lib/codd_common.py`）が `False` を `LEVEL_OFF` へ正規化するため、bare `off` と引用符付き `"off"` はどちらも同じ扱いになる — 根拠: packages/codd/lib/codd_common.py（`normalize_check_level`）, .claude/rules/codd-frontmatter-policy.md, .claude/rules/config-loading.md

### 4.1 コード⇔ドキュメントのトレーサビリティ（Issue #98）

- [ ] EV-26（正常 / must）: `code_scope.include` は既定で空リストであり、未設定プロジェクトでは `scan_code_nodes` が常に空を返す（既存挙動への影響ゼロ） — 根拠: docs/design/codd-coherence-layer.md §4.3.1 (D9)
- [ ] EV-27（正常 / must）: `code_scope.include` に glob を追加すると、対象ファイル先頭の 1行注釈（`codd:<key> <value>`）から `code` / `test` kind の CoddNode が抽出され、doc ノードと同じグラフに統合される — 根拠: docs/design/codd-coherence-layer.md §4.3.1
- [ ] EV-28（境界 / should）: `code_scope` 内で `codd:` 注釈が無いファイルは、doc scope の `missing_frontmatter` と異なり黙ってスキップされる（warning にならない） — 根拠: docs/design/codd-coherence-layer.md §4.3.1
- [ ] EV-29（正常 / must）: Python ファイルはモジュール docstring のみを注釈探索対象とし、本文コード中の文字列リテラルにある `codd:` らしき文字列を誤検出しない。抽出はファイル全体を構文解析する `ast.parse` ではなく、`tokenize` で先頭のコメント/空行トークンのみ読み飛ばす軽量実装（結果は `ast.get_docstring(tree, clean=False)` と同一。暗黙の文字列連結は連結し、bytes リテラル/f-string は docstring 扱いしない。`("""...""")` のように丸括弧で囲んだ docstring も認識し、`"""..."""  + "suffix"` のような文字列連結式は docstring として誤抽出しない） — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`_python_leading_text`）
- [ ] EV-30（正常 / should）: `//` 系言語（TS/JS/Go/Java/Rust/C 系）はファイル先頭から連続する行コメントのみを注釈探索対象とし、shebang 行はスキップする — 根拠: docs/design/codd-coherence-layer.md §4.3.1
- [ ] EV-31（正常 / must）: kind 省略時はパス規約（`tests/` 等のディレクトリ名、`test_*` / `*_test` ファイル名）から `test` / `code` を推定し、node_id 省略時は `<kind>:<file-stem>` を自動導出する。`codd:node_id` / `codd:kind` で明示上書きできる — 根拠: docs/design/codd-coherence-layer.md §4.3.1
- [ ] EV-32（正常 / must）: コード注釈由来の depends_on は `codd.yaml` の `inline_confidence`（既定 0.7）を confidence として持ち、doc frontmatter 由来のリンク（既定 1.0）と区別される — 根拠: docs/design/codd-coherence-layer.md §4.3.1 (D9)
- [ ] EV-33（境界 / must）: `impact` のエッジ重みは `relation 重み × confidence` で計算され、低信頼なコード由来リンクは下流影響スコアに比例して弱く反映される — 根拠: docs/design/codd-coherence-layer.md §4.3.1, §4.5.1
- [ ] EV-34（正常 / should）: `graph.jsonl` の depends_on エントリは confidence が既定値 1.0 のとき `confidence` キーを省略し、doc のみのグラフでは既存の JSONL 出力と互換を保つ — 根拠: packages/codd/scripts/codd.py（`_dependency_to_record`）
- [ ] EV-35（正常 / should）: code/test ノードは既存の dangling / duplicate / cycle / unknown / orphan / drift 検査を特別扱いなしで受ける（validate 側の分岐追加なし） — 根拠: docs/design/codd-coherence-layer.md §4.3.1
- [ ] EV-36（異常 / must）: `inline_confidence`（config）および depends_on の `confidence`（doc frontmatter）は有限な `[0, 1]` へ正規化される。範囲外の有限値（例: `-0.1` / `1.5`）は境界へクランプし、NaN/Inf のような非有限値（YAML の `.nan` 等）は既定値へフォールバックする。`bool` は `int` のサブクラスで `float(False) == 0.0` が例外なく通ってしまうため明示的に不正値扱いとし（`inline_confidence: false` は全エッジ重みゼロの一斉 Gray 化を招くため）、`inline_confidence` / `confidence` / `impact.*`（decay・thresholds・weights 等）のいずれも bool を既定値へフォールバック（`impact.*` は数値以外と同様 config エラーとして拒否）する — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_common.py（`_clamp_unit_float` / `_load_inline_confidence` / `_as_confidence` / `_reject_bool_as_number` / `ImpactConfig.from_dict`）
- [ ] EV-37（異常 / must）: code_scope の relation 注釈（予約語以外の key）に参照先 value が無い場合（例: `codd:implements` のみ）、依存として黙って除外せず `malformed_annotation` として error 判定する。予約語（node_id/kind/status/owner）の value 省略はエラーにしない — 根拠: docs/design/codd-coherence-layer.md §4.3.1, §4.5, packages/codd/lib/codd_code.py（`_entries_to_node`）
- [ ] EV-38（正常 / must）: `.mjs` / `.cjs` / `.mts` / `.cts` 拡張子のファイルも `//` 系言語として行コメント抽出の対象になる — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`_LINE_COMMENT_SUFFIXES`）
- [ ] EV-39（境界 / should）: Python ファイルの読み込みは PEP 263 の宣言済みエンコーディング（coding cookie / BOM）を `tokenize.detect_encoding` で尊重し、UTF-8 以外（例: Latin-1）でも `UnicodeDecodeError` にならない — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/scripts/codd.py（`_read_source_text`）
- [ ] EV-40（境界 / must）: `code_scope.exclude` の既定 3 パターン（`__pycache__` / `node_modules` / `.venv`）は、末尾を `/**/*` にすることで配下ファイルを正しく除外する（`Path.glob` の `/**` 末尾はディレクトリのみ返す環境があるため） — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/config/codd.yaml
- [ ] EV-41（境界 / should）: `impact` の削除上流検出は doc scope だけでなく code_scope 内の注釈付きコードファイル削除も対象にし、ref 時点の内容からコード注釈を再抽出して旧 node_id を回収する。ref 側の Python も working tree と同じ PEP 263 規約（`git show` の生バイト列を `tokenize.detect_encoding` で復号）で読むため、Latin-1 等の coding cookie を宣言した Python ファイルの削除でも `UnicodeDecodeError` にならない。working tree 側（`scan_code_nodes`）と同様、`is_supported_suffix` で対応外拡張子を `git show` 実行前に除外する（未対応拡張子の blob を無駄に読み込まない） — 根拠: docs/design/codd-coherence-layer.md §4.3.1, §4.5.1, packages/codd/scripts/codd.py（`_old_node_id_at_ref` / `_decode_ref_source` / `path_in_code_scope`）
- [ ] EV-42（境界 / must）: `code_scope.include` に混在ディレクトリ glob（画像等の対応外拡張子を含む）を指定しても、抽出対象言語（Python / `//` 系）に対応しない拡張子のファイルは読み込み前に除外され、UTF-8 テキストとして復号されない — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`is_supported_suffix`）, packages/codd/scripts/codd.py（`scan_code_nodes`）
- [ ] EV-43（境界 / should）: 行コメント抽出・Python docstring 抽出の前に先頭 BOM（U+FEFF）を取り除く。BOM 付き `//` 系ファイルは先頭行のコメント判定に失敗せず、BOM 付き Python は `ast.parse` が構文エラーにならない — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`extract_code_node`）
- [ ] EV-44（異常 / must）: `scope.include` / `scope.exclude` / `code_scope.include` / `code_scope.exclude` は文字列（単要素扱い）またはリスト（要素は全て文字列）のみ許容し、それ以外の型（数値・非文字列要素を含むリスト等）は config ロード時に `ValueError` にする。ただし空文字列（`""`）は「対象なし」を表す既存設定との後方互換のため空リストとして扱う（`[""]` にはしない） — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_common.py（`_as_glob_list`）
- [ ] EV-45（異常 / must）: ソースファイルの `codd:kind` 注釈は `code` / `test` のみ有効。それ以外の値（`requirement` 等のドキュメント語彙）は `malformed_annotation` として error 判定したうえで、パス規約による推定 kind へフォールバックする — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`_entries_to_node`）
- [ ] EV-46（異常 / must）: `codd:` で始まりながら `codd:<key>` / `codd:<key> <value>` の文法に一致しない行（例: `codd:node-id`、`codd:node_id=value`）は、無関係なコメント行として黙って無視せず `malformed_annotation` として error 判定する。`codd:` で始まらない通常のコメント行は従来通り無視する — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/lib/codd_code.py（`_parse_annotation_lines`）
- [ ] EV-47（境界 / must）: `code_scope.include` / `scope.include` に `../*.py` のような相対パスを含む glob を指定しても、プロジェクトルート外に解決されるファイルは走査対象から除外される — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/scripts/codd.py（`_glob_relpaths`）
- [ ] EV-48（境界 / must）: working tree 側のソース読み込み（`_read_source_text`）が UTF-16 保存の `//` 系ファイルや不正な coding cookie を持つ Python ファイルの復号に失敗した場合、`_decode_ref_source`（削除済みファイルの ref 側読み込み）と同じ規約で `None` を返し、`scan_code_nodes` は当該ファイルを注釈なしとして黙ってスキップする（`UnicodeDecodeError` / `SyntaxError` / `LookupError` で scan/validate/impact 全体を落とさない） — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/scripts/codd.py（`_read_source_text` / `scan_code_nodes`）
- [ ] EV-49（境界 / must）: `scope.include` / `scope.exclude` / `code_scope.include` / `code_scope.exclude` の glob に含まれる文字クラス（`[seq]` / `[!seq]`）は、通常走査（`collect_files` / `collect_code_files` の `Path.glob`）と削除済みパスの判定（`_scope_pattern_to_regex` による impact の dangling / 削除上流検出）とで同じ意味に解釈される。閉じ `]` が無い場合は fnmatch と同様リテラル `[` として扱う。`[z-a]` のような逆順の不正な文字範囲は `re.compile` が `re.error` になるが、クラッシュせず `Path.glob`（fnmatch）と同様「常に非マッチ」として安全に扱う — 根拠: docs/design/codd-coherence-layer.md §4.5.1, packages/codd/scripts/codd.py（`_scope_pattern_to_regex` / `collect_files`）
- [ ] EV-50（異常 / must）: `scope` / `code_scope` / `graph_store` / `impact` / `checks` / `impact.relation_weights` に mapping 以外（文字列・リスト等）を指定した場合、`.get()` / `.items()` 呼び出しによる `AttributeError` でトレースバックを露出させず、`ValueError`（`main()` の設定エラー整形経路）にする。`impact.max_hops` / `impact.corroboration_min_origins` に非有限値（`.inf` / `.nan` 等の YAML 表記）を指定した場合も、`int()` の `OverflowError`（`ValueError` のサブクラスではない）でトレースバックを露出させず `ValueError` にする — 根拠: docs/design/codd-coherence-layer.md §4.6, packages/codd/lib/codd_common.py（`_as_mapping` / `_as_finite_int`）
- [ ] EV-51（境界 / must）: `code_scope.include` / `scope.include` に `../proj/src/**/*.py`（root == proj）のように root 内へ戻ってくる相対 glob を指定しても、containment 判定を通過したパスは root からの相対パスへレキシカルに正規化される（`os.path.normpath` によるドット記法の畳み込みのみで、シンボリックリンクは解決しない）。正規化しないと `src/**/*.py` 等の通常パターンで見つかる同一ファイルと別名の文字列で重複登録されてしまう。root 内部のシンボリックリンクにマッチした場合は、解決先のパスではなくリンク自体の論理パスを登録する（`git diff` が返すパスと一致させるため）。root 外への解決（シンボリックリンクの解決先が root 外）は従来どおり拒否する — 根拠: docs/design/codd-coherence-layer.md §4.3.1, packages/codd/scripts/codd.py（`_glob_relpaths`）
- [ ] EV-52（境界 / should）: code_scope 内のコードファイルは、削除だけでなく **ファイルが残ったまま** `codd:` 注釈の削除や `node_id` 変更で旧コードノードが消失するケースも `impact` の dangling 検出対象になる（`changed_paths` の code_scope 該当分について ref 側の旧注釈を再抽出し、現グラフから消えていれば `deleted_upstream` に含める） — 根拠: docs/design/codd-coherence-layer.md §4.5.1, packages/codd/scripts/codd.py（`compute_impact_result`）
- [ ] EV-53（境界 / should）: drift 検査（EV-10）の最終更新時刻取得は、ノードごとに `git status` / `git log` を個別実行せず、`batch_commit_times()` がリポジトリ全体の 1 回の `git status --porcelain -z` と、対象パスに絞った 1 回の `git log -z --name-only` にまとめて判定する（判定規約は単発の `commit_time()` と同一で、結果は変わらない）。`git log` は `-z` で NUL 区切り取得することで `core.quotePath`（既定 true）によるパスの引用（非 ASCII パスの 8 進エスケープ）を回避し、正しくコミット時刻と紐付ける。`--root` が git リポジトリルート以外（サブディレクトリ）を指す場合は `git rev-parse --show-prefix` で得た prefix を使い、`git status` / `git log` が返すリポジトリルート相対パスを `--root` 相対へ正規化してから突き合わせる — 根拠: docs/design/codd-coherence-layer.md §4.5 (H-3), packages/codd/scripts/codd.py（`batch_commit_times` / `_log_commit_times` / `_repo_root_prefix` / `_check_drift`）

## 5. テストレビュー判断基準（パッケージ固有）

- frontmatter parser のテストは、本文中のコードブロック内 YAML 例を誤って依存として拾わないことを、正常系とは独立したケースで検証しているか（EV-02, M-1）。
- `impact` のスコア計算テストは、設計書の数値例（green_threshold=0.8, amber_threshold=0.4, decay=0.5, corroboration_min_origins=2）から期待値を導出しているか。CLI の現状出力をそのままコピーした期待値になっていないか（EV-15〜17）。
- `checks` レベルの `off` 正規化の挙動は、bare `off`（YAML の boolean `False` と誤読される値）が `normalize_check_level` によって `LEVEL_OFF` へ正規化され、引用符付き `"off"` と同じ結果になることを比較テストで検証しているか（EV-25）。
- status/kind/relation の許容語彙テストは、境界値（未定義語彙・kind 依存の語彙違反）を正常系のついでではなく独立したケースで検証しているか（EV-07, EV-11, EV-18）。
- コード注釈抽出のテストは、Python の「docstring 内は拾う／本文中の文字列リテラルは拾わない」を同一ファイル種別内で対で検証しているか（EV-29）。confidence がコード由来リンクにのみ既定値未満で付与され、doc 由来リンクの confidence（1.0）や JSONL 出力（confidence キー省略）を変えていないことを、既存の doc-only テストと並べて確認しているか（EV-32, EV-34）。
