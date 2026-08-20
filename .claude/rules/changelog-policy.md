# Changelog Writing Policy

**CHANGELOG.md のエントリは orchex 利用者の体験に直結する変更だけを、簡潔に記載する。**

CHANGELOG は自動生成ではなく PR ごとに手書きする（`## [Unreleased]` セクション）。
`release-changelog.sh` はこれをバージョンセクションへ切り出すだけで粒度には関与しない。
書き手がこのポリシーに従って取捨選択と粒度を判断する。

## 判定の基準

各変更について「利用者が挙動・使い方を変える必要があるか？」を問う。
**No なら CHANGELOG に載せない。** 迷う内部変更は載せないことを既定とする。

### 載せる（利用者に見える変更）

- CLI コマンド・フラグ・サブコマンドの追加 / 変更 / 削除
- 設定キー（`.claude/config/**`）の追加 / 変更 / デフォルト値変更
- スキル・エージェント・パッケージの利用者から見える挙動変更
- 破壊的変更・後方非互換（最優先で記載）

### 載せない（内部変更）

- テストのみの追加 / 修正、CI 設定
- 挙動が変わらない内部リファクタ・コード構造変更
- PR レビュー対応の経緯、実装の内部原因の説明
- 依存の内部更新（利用者に影響しないもの）
- ドキュメントの内部整理（利用者向け README 等の実利用に関わるものを除く）

## 粒度

- 1 変更 = **見出し（太字の変更点）＋ 1〜2 行の説明**まで。
- 内部実装・レビュー対応・テスト修正を並べる深いサブバレットは書かない。
- `Fixed` は「利用者から見て何が直ったか」を 1 行で書く。内部原因・実装詳細・PR 番号の羅列は書かない（トレースは PR / コミットで足りる）。
- 破壊的変更は行頭に `**BREAKING**` を付けて目立たせる。

## フォーマット

- [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 準拠。
- カテゴリは `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`。
- 新規エントリは `## [Unreleased]` 直下の該当カテゴリに追記する。

## 例外: meta-harness の skill / claude-harness promotion PR

meta-harness の `skill:<slug>` promotion PR と `claude-harness` promotion PR では、promoter が
`## [Unreleased]` / `### Changed` へ 1 行のエントリを自動追記する。`routing-config` promotion PR は
対象外であり、従来どおり人間が追記する。これは「CHANGELOG に何を書くかの判断」を自動化に委譲
するものではなく、あくまでドラフトの下書きである。掲載可否・粒度・文言の最終判断は、promote PR
のチェックリスト（未チェックの `- [ ] CHANGELOG.md \`Unreleased\`: auto-inserted draft entry`）に
従って人間レビューで行う。レビュー時は本ポリシー（判定の基準・粒度・フォーマット）と照合し、
不要なら削除、必要なら文言を整えてからマージする。
