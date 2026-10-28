# CODD Frontmatter Policy

**CODD 管理下のドキュメントが守るフロントマター記法ルール。**

`scan` / `validate`（`packages/codd`）はこの記法を正本（SSOT）として依存グラフを構築する。
依存宣言は外部ファイルではなく、各ドキュメント先頭の `codd:` ブロック1箇所に書く。

## 記法

各対象ドキュメントの**先頭**に YAML frontmatter を置き、`codd:` ブロックを埋め込む:

```yaml
---
codd:
  node_id: "design:codd-coherence-layer" # 一意ID。形式は <kind>:<file-slug>
  kind: design # requirement|design|adr|plan|rule|instruction
  status: draft # kind ごとに語彙が異なる（下表）
  depends_on:
    - id: "req:coherence-guardrail" # 参照先 node_id（実在必須）
      relation: derives_from # derives_from|refines|implements|references|supersedes
      synced: "61ba71d" # 任意。確認済みの上流コミット SHA（`codd ack` が書く）
      anchor: "API-010" # 任意。上流のこの文字列を含む行だけに依存する
  owner: ai-orchestra # 任意。責任主体
---
```

- parser は**先頭ブロックのみ**を読む。本文中のコードブロック内 `---` や YAML 例は無視される。
- **1 ファイル = 1 ノード**。集約ファイル（FT 群を含む等）もファイル全体で 1 ノードとする。

## node_id 体系

`node_id` は `<kind>:<file-slug>`（file-slug = 拡張子を除いたファイル名 or 安定スラッグ）。

| kind        | node_id 例                  | 由来                                                              |
| ----------- | --------------------------- | ----------------------------------------------------------------- |
| requirement | `req:feature-list`          | `docs/requirements/*.md`                                          |
| design      | `design:architecture`       | `docs/architecture/*.md`                                          |
| adr         | `adr:ADR-20260624-010`      | `docs/adr/ADR-*.md`                                               |
| plan        | `plan:codd-coherence-layer` | `.claude/plans/*.md`（既定の scope 外。`codd.local.yaml` で足す） |
| rule        | `rule:config-loading`       | `.claude/rules/*.md`                                              |
| instruction | `instruction:claude-md`     | `templates/context/*.md`                                          |
| code        | `code:codd-scan`            | `code_scope.include` 内のソースファイル（opt-in）                 |
| test        | `test:test-codd-cli`        | `code_scope.include` 内のテストファイル（opt-in）                 |

> `code` / `test` は 1行の軽量注釈（`codd:<key> <value>`）で宣言する。詳細は
> `docs/design/codd-coherence-layer.md` §4.3.1 を参照。

## status 語彙（kind 依存）

| kind                                                           | status 語彙                                                        |
| -------------------------------------------------------------- | ------------------------------------------------------------------ |
| adr                                                            | `proposed` / `accepted` / `rejected` / `superseded` / `deprecated` |
| requirement / design / plan / rule / instruction / code / test | `draft` / `active` / `deprecated`                                  |

## relation（関係種別）

| relation       | 意味                 | 典型的な向き         |
| -------------- | -------------------- | -------------------- |
| `derives_from` | 上流から派生         | design → requirement |
| `refines`      | 詳細化               | 詳細設計 → 基本設計  |
| `implements`   | 実装関係             | plan → design        |
| `references`   | 参照（弱い依存）     | 任意 → 任意          |
| `supersedes`   | 置換（旧版を無効化） | 新 ADR → 旧 ADR      |

## 追従の確認（synced / anchor）

`validate` の drift は「下流が上流に追従すべき」エッジ（relation が `derives_from` / `refines` / `implements` で、下流が ADR 以外。`codd.yaml` の `drift:` で変更可）で、上流に基準時刻（下流の最終コミット時刻と `synced` のコミット時刻の大きい方）より新しい実質変更（空白・空行・Markdown 表の区切り行だけの変更を除く。未コミットの変更を含む）があるときに出る。

| キー     | 意味                                                                                                                                                                         | 書き方                                                                                                                                                                              |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `synced` | この上流コミットまで中身を確認した、という印。基準時刻が「下流の最終コミット時刻」と「synced のコミット時刻」の大きい方になり、それ以降に実質変更が無ければ drift にならない | 手で書かず `codd ack <node_id>`（drift 中のエッジ）/ `codd ack <node_id> --all`（drift 対象の全エッジ）で付ける。手で書くなら 7〜40 桁の 16 進 SHA を引用符で囲む                   |
| `anchor` | 上流ファイルのうち、この文字列を含む行だけに依存する（一覧 → 個票のように 1 ファイルの一部だけを参照する場合）                                                               | 空でない文字列を手で書く（例: API の ID、見出し）。大文字小文字を区別する部分一致で、実質変更の追加行・削除行に照合する。前後の空白は取り除かれ、空文字や文字列以外の値は未指定扱い |

- どちらも任意。無いときは従来どおりファイル全体・下流の最終コミット時刻で判定する。
- `synced` は上流の実質変更を見て「追従不要」と判断したときに付ける。中身を見ずに付けない（`ack` にリポジトリ全体の一括更新が無いのはそのため）。
- `synced` が解決できない SHA（squash merge で消えた等）のときは未指定として扱われ、drift メッセージにその旨が出る。`codd ack` で付け直す。
- `synced` に解決されるのは 7〜40 桁の 16 進 SHA だけ。`HEAD` やブランチ名・タグ名を書いても解決されず、drift を消す手段にはならない。引用符なしの数字だけの SHA は YAML が数値として読むので必ず引用符で囲む。
- 上流の変更内容を確認できない場合（diff が binary 判定される、履歴を取得できない）は drift になる（確認できない変更は追従漏れ側に倒す）。
- `codd ack` は、上流に未コミットの変更があると最後のコミットまでを `synced` に書いて WARN を出す。コミットの無い上流、コード注釈ノード、flow style の `depends_on` 項目は SKIP として報告する（SKIP があっても終了コードは 0。未知・重複の node_id、上流履歴の取得失敗、ack 中のファイル変更、書き込み失敗は exit 1 で書き込まない）。
- コード注釈（`codd:<key> <value>` の 1 行形式）には `synced` / `anchor` を書けない。

## 運用ルール

- `depends_on.id` は実在する node_id を指すこと（リンク切れは `validate` で error）。
- 同一 node_id を複数ファイルで使わないこと（重複は error）。
- 依存は循環させないこと（循環は error）。
- `roots`（requirement / instruction）以外で参照ゼロのノードは孤立 warning になる。
- 上流ノードを更新したら、下流ノードも追従して更新する。中身を見て追従不要なら `codd ack <node_id>` で `synced` を付けて drift を消す（上記）。
- 検査レベルに `off` を書く場合、YAML 1.1 仕様では bare `off` が boolean `False` と解釈されるが、`normalize_check_level`（`packages/codd/lib/codd_common.py`）が `False` を含め `off` の大文字小文字違いも自動的に `LEVEL_OFF` へ正規化するため、引用符なしの bare `off` と引用符付き `"off"` はどちらも同じ扱いになる。
