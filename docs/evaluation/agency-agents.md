# agency-agents 評価セット

**パッケージ**: `packages/agency-agents`
**類型**: 主: CLI ツール型（`scripts/vendor-agency-agents.py`）、副: スキル型（取り込んだエージェント定義の配布）
**作成日**: 2026-10-28
**最終レビュー日**: 2026-10-28（新規作成。レビュー指摘（frontmatter の許可リスト化・update の安全化・検出の強化）を反映。人間レビュー待ち）
**情報源**: packages/agency-agents/upstream.yaml（冒頭コメント）, scripts/vendor-agency-agents.py（モジュール docstring）, docs/adr/ADR-20261028-061.md, packages/agency-agents/manifest.json

## 1. 責務定義

`agency-agents` パッケージは、OSS の agency-agents（MIT）のエージェント定義を、`upstream.yaml` で固定した commit から取り込み、Claude Code のサブエージェントとして配布することを保証する。取り込むエージェント・ツール権限・frontmatter の上書きは `upstream.yaml` だけで決まり、生成物（`agents/*.md`・`manifest.json` の `agents`・`LICENSE-agency-agents`）はスクリプトが再生成する。ツール権限は既定で読み取り専用とし、upstream の frontmatter からは権限や実行に関わるキーを持ち込ませない。upstream の更新を取り込むときは、人が読むべき変更に理由付きで印を付け、印がある限り明示の承認（`--yes`）なしに書き換えない。

### Non-Goals

- upstream の本文へのパッチ当て（上書きは frontmatter に限る）
- 配布先プロジェクトごとのエージェントの選択（ai-orchestra 側で一元管理する）
- 配布時のネットワーク越しの取得（生成物をコミットし、配布先では取得しない）
- 言い換えたプロンプトインジェクションの完全な検出（パターン検出は補助。権限の制限と人のレビューで担保する）

## 2. 期待する入出力・副作用

| 構成要素       | 入力                                                    | 期待する出力                                                                                                          | 副作用                                                                                             |
| -------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `sync`         | `upstream.yaml`、`--source`（ローカルの clone）         | `[sync] N agents @ <sha>`、exit 0。設定・upstream の不備は `[error]` で exit 2                                        | 生成物を一時ファイル経由でまとめて書き、設定から外れた生成物（印のあるファイル）を削除             |
| `sync --check` | 同上                                                    | 一致なら exit 0、食い違いを `[check]` で列挙して exit 1                                                               | なし                                                                                               |
| `update --to`  | `upstream.yaml`、`--source`、進め先の ref、`--yes`      | Markdown の報告（要レビュー・要約のみ・upstream で削除・upstream で追加）。要レビューがあり `--yes` が無ければ exit 1 | 新しい commit で生成に成功した場合だけ、`upstream.yaml` の `commit` 行と生成物をまとめて書き換える |
| `list`         | `upstream.yaml`、`--source`、任意の `--ref` / `--query` | upstream のエージェントを `[x]`（取り込み済み）/ `[ ]` 付きで description と表示                                      | なし                                                                                               |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `sync` が `upstream.yaml` の `agents` ごとに `agents/<slug>.md` を生成し、`manifest.json` の `agents` を同じ集合にする — 根拠: upstream.yaml（冒頭コメント）
- [ ] EV-02（正常 / must）: upstream は `commit` で固定した版を `git show <sha>:<path>` で読み、clone の作業ツリーの未コミット変更や後続の commit を反映しない — 根拠: scripts/vendor-agency-agents.py（docstring）
- [ ] EV-03（正常 / must）: 生成した frontmatter の `name` は upstream のファイル名（slug）になり、`cli-tools.yaml` の `agents` のキーと同じ名前空間になる — 根拠: upstream.yaml（冒頭コメント）
- [ ] EV-04（正常 / must）: `profile` も `tools` も無いエージェントは `Read, Glob, Grep`（readonly）になり、upstream の `tools` は使わない — 根拠: upstream.yaml（冒頭コメント）
- [ ] EV-05（正常 / must）: `profile` は readonly / writer（+Edit, Write）/ implementer（+Bash）に展開され、どのプロファイルにも `Agent` は含まれない。`Agent` は `tools` で明示したときだけ付く — 根拠: upstream.yaml（冒頭コメント）
- [ ] EV-06（正常 / should）: `upstream.yaml` の `frontmatter` で上書きできるのは `description` / `model` / `color` / `emoji` / `vibe` だけ。本文は upstream のまま変えず、生成物であることを示すコメントを frontmatter の直後に置く — 根拠: ADR-20261028-061
- [ ] EV-07（正常 / must）: `sync` は `upstream.yaml` から外れたエージェントの生成物を削除する — 根拠: upstream.yaml（冒頭コメント。付け外しは設定で行う）
- [ ] EV-08（異常 / must）: `sync --check` は、未生成・余分なファイル・手での編集・`manifest.json` / `LICENSE-agency-agents` の食い違いを列挙して exit 1 にし、何も書き込まない。UTF-8 でないファイルがあっても落ちずに食い違いとして報告する — 根拠: scripts/vendor-agency-agents.py（docstring）
- [ ] EV-09（異常 / must）: 次の不備は exit 2 で止まり、何も生成しない: 未知の profile、profile と tools の同時指定、空の tools、上書きできない frontmatter のキー（`name` / `tools` / `permissionMode` / `hooks` / `mcpServers` / 大文字小文字違いを含む）、未知のキー、40 桁の小文字 SHA でない commit、source に無い commit、upstream に無いパス、frontmatter の無い文書、文字列でない description、slug の重複（大文字小文字の違いだけのものを含む） — 根拠: 実装挙動
- [ ] EV-10（境界 / must）: エージェントのパスは英数字・`_`・`-` と `/` 区切りに限り、`..`・絶対パス・空白・拡張子付きは拒否する。`-` で始まる ref は git のオプションとして渡さず拒否する — 根拠: 実装挙動
- [ ] EV-11（異常 / should）: `cli-tools.yaml` の `agents` に無いエージェントは警告を出すが、`sync` は成功する — 根拠: ADR-20261028-061（決定 6）
- [ ] EV-12（正常 / must）: `update --to <ref>` は、取り込み済みエージェントの変更（追加・削除行数付き）と upstream で新しく追加されたエージェントを報告する。書き換えるときは `commit` 行だけを変え、行末のコメントや他のコメントは保つ — 根拠: scripts/vendor-agency-agents.py（docstring）
- [ ] EV-13（異常 / must）: 取り込み済みエージェントが upstream で削除されていたら、`update` は報告して exit 1 にし、`upstream.yaml` も生成物も書き換えない — 根拠: 実装挙動
- [ ] EV-14（正常 / must）: `update` は次の変更を「要レビュー」にして理由と差分全文を出す: `Bash` / `Edit` / `Write` / `Agent` を持つエージェントの変更、description の変更、未対応の frontmatter キーの追加、追加行の危険なパターン（シェル・インタプリタへのパイプ、eval・base64 デコード等、外部 URL、認証情報や `.env` 等の語、指示の上書き（英語・日本語、空白や行継続の揺れを正規化して照合））、不可視の制御文字・HTML コメントの追加、安全に関する記述（never / do not / 禁止 等）の削除 — 根拠: ADR-20261028-061（決定 5）
- [ ] EV-15（正常 / should）: `update --dry-run` は報告だけを出し、何も書き換えない — 根拠: scripts/vendor-agency-agents.py（`--dry-run` の help）
- [ ] EV-16（正常 / must）: `list` は upstream のエージェントを description 付きで一覧し、取り込み済みに `[x]` を付ける。frontmatter の無い文書は出さない。`--query` は大文字小文字を区別せずパス・名前・description で絞り、`--ref` で固定 commit 以外の版を一覧できる — 根拠: scripts/vendor-agency-agents.py（`list` の help）
- [ ] EV-19（異常 / must）: upstream の frontmatter のうち生成物に通すのは `description` / `color` / `emoji` / `vibe` だけで、`permissionMode` / `hooks` / `mcpServers` / `model` / 大文字小文字違いの `Tools` 等は生成物に入れず、捨てたキーを警告する — 根拠: ADR-20261028-061（決定 3）
- [ ] EV-20（異常 / must）: 要レビューの変更があるとき、`update` は `--yes` が無ければ exit 1 で何も書き換えない — 根拠: ADR-20261028-061（決定 5）
- [ ] EV-21（異常 / must）: 新しい commit で生成に失敗する場合（frontmatter が無くなった等）、`update` は `upstream.yaml` も生成物も書き換えない — 根拠: ADR-20261028-061（レビュー指摘への対応）
- [ ] EV-22（異常 / must）: `update` は、進め先が現在の commit の子孫でない場合（巻き戻し・fork の commit）と、`--source` の origin が `upstream.yaml` の `repo` と別のリポジトリの場合に exit 2 で止まる。https と ssh の URL の違いは同じリポジトリとして扱う — 根拠: ADR-20261028-061（レビュー指摘への対応）
- [ ] EV-23（異常 / must）: `sync` は、生成物の印が無いファイル（手で置いたもの）と symlink を削除せず警告にとどめる — 根拠: 実装挙動
- [ ] EV-24（正常 / must）: 生成物の印のコメントに upstream の repo・commit・パス・ライセンス名・著作権表示を入れ、upstream の LICENSE を `LICENSE-agency-agents` として同梱する — 根拠: ADR-20261028-061（MIT の告知）

## 4. 類型別観点

- [ ] EV-17（CLI ツール型 / 出力の安定性 / should）: `manifest.json` は prettier（printWidth 80）と同じ形で書かれ、hook が整形しても `--check` が食い違いを出さない — 根拠: 実装挙動
- [ ] EV-18（共通 / 生成物の同期 / must）: コミットされた生成物が `upstream.yaml` と一致している（`sync --check` が exit 0。clone の無い CI ではエージェントの集合・`name`・`tools`・frontmatter のキー・印のコメント・LICENSE を確かめる） — 根拠: upstream.yaml（冒頭コメント）
- [ ] EV-25（CLI ツール型 / 破壊的操作の安全策 / should）: 生成物は一時ファイルに書き終えてから置き換え、途中で失敗しても一部だけ更新された状態を残さない。git の呼び出しには時間制限を付け、clone の設定によるフック・fsmonitor の実行を無効にする — 根拠: 実装挙動
- N/A: 設定レイヤリング（`*.local.*`）— `upstream.yaml` は ai-orchestra 側の開発用正本で配布されず、配布先での上書きを持たない
- N/A: スキル型の対話規約・非対話完結性・フォールバック — スキルを持たず、取り込んだエージェントは外部 CLI を呼ばない（`cli-tools.yaml` で `claude-direct`）
- N/A: 機械可読出力（JSON 等）— 出力は人が読む報告のみ

## 5. テストレビュー判断基準（パッケージ固有）

- upstream は一時ディレクトリに作った実 git リポジトリで再現し、`git` 呼び出しをモックしない（EV-02 の固定 commit 読み出しを空洞化させないため）
- 危険なパターン（EV-14）は、レビューで実際にすり抜けた書き方（行継続・空白の揺れ・日本語・スキームなしの URL）を含めて、追加行での検出と、削除行だけでは検出しないことの両方を確かめる
- 権限の持ち込み（EV-19）は、実際に Claude Code が解釈するキー（`permissionMode` / `hooks` / `mcpServers`）で確かめる
