# Facet-Prompt 検証テスト計画

**作成日**: 2026-03-19
**対象**: facet-prompt (composition-based skill/rule generation)
**目的**: 新規導入・既存導入の両パターンで facet-prompt が正しく機能するか検証する

---

## テスト環境

| 項目 | 値 |
|------|-----|
| ai-orchestra バージョン | develop (9f720aa+) |
| Python バージョン | 3.14.2 |
| テスト用プロジェクト | /private/tmp/claude-501/facet-* |
| 実施日 | 2026-03-21 |
| 実施者 | Claude Code (自動検証) |

---

## 凡例

| ステータス | 意味 |
|-----------|------|
| `-` | 未実施 |
| `PASS` | 合格 |
| `FAIL` | 不合格 |
| `SKIP` | スキップ（理由を備考に記載） |

---

## A. 新規導入パターン

### A1. 基本インストール

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 1 | `orchex install <package>`（未初期化プロジェクト） | 自動 init 実行後にパッケージが `.claude/orchestra.json` に記録 | `PASS` | install 時に自動 init が走ることを確認 |
| 2 | インストール後に `orchex facet build` | `.claude/skills/*/SKILL.md` と `.claude/rules/*.md` が生成 | `PASS` | 25 compositions built |
| 3 | manifest に含まれるが未インストールの composition | スキップされ、生成ファイルなし | `PASS` | Issue #20: `manifest_compositions` + `installed_packages` で判定 |
| 4 | manifest に含まれない composition（グローバル） | パッケージ不問で常にビルドされる | `PASS` | Issue #20: `manifest_compositions` に含まれない → 常にビルド |

### A1b. setup による一括導入

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 1b-1 | `orchex setup essential`（未初期化プロジェクト） | 自動 init → core, agent-routing, quality-gates が一括インストール | `PASS` | init + 3パッケージが順序通りインストール |
| 1b-2 | setup 後の orchestra.json | 3パッケージが `installed_packages` に記録 | `PASS` | agent-routing, core, quality-gates が記録 |
| 1b-3 | setup 後に SessionStart を模擬 | facets 同期 + facet build が実行される | `PASS` | `32 synced, 16 facets built` |
| 1b-4 | `orchex setup essential`（既に導入済みプロジェクト） | インストール済みパッケージがスキップされる | `PASS` | `新規: 0, スキップ: 3` |
| 1b-5 | `orchex setup all`（未初期化プロジェクト） | 全パッケージが依存順に一括インストール | `PASS` | 全10パッケージが依存順にインストール |

### A2. ファセット解決

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 5 | policy の埋め込み | 生成ファイルに `code-quality.md` 等の内容が含まれる | `PASS` | simplify に code-quality 全文が含まれる |
| 6 | output-contract の埋め込み | `tiered-review.md` 等の出力契約が含まれる | `PASS` | review に tiered-review が含まれる |
| 7 | instruction（ファイル参照）の解決 | `instruction: review` → `instructions/review.md` の内容が使われる | `PASS` | ファイル参照が正しく解決 |
| 8 | instruction（インライン）の解決 | composition 内の直接記述がそのまま使われる | `PASS` | simplify のインライン instruction が使用された |

### A3. 生成ファイル構造

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 9 | skill type の SKILL.md | YAML frontmatter + policies + contracts + instruction の順 | `PASS` | `---` で囲まれた frontmatter + 本文 |
| 10 | rule type の .md | frontmatter なし、policies + instruction のみ | `PASS` | coding-principles.md が policy から直接開始 |
| 11 | セクション間の区切り | `\n\n---\n\n` で分離 | `PASS` | |
| 12 | codex ターゲット | `.codex/skills/` と `.codex/rules/` に生成 | `PASS` | |

### A4. SessionStart 自動ビルド

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 13 | 新規セッション開始 | `sync-orchestra.py` が facet build 自動実行 | `PASS` | `.claudeignore updated, 16 facets built` |
| 14 | facets ソースは同期されない | `.claude/facets/` が自動作成されない | `PASS` | orchestra 側を直接参照する設計（案 A） |
| 15 | codex-suggestions パッケージあり | claude + codex 両ターゲットでビルド | `PASS` | 48 facets built（claude 24 + codex 24） |
| 16 | codex-suggestions パッケージなし | claude ターゲットのみビルド | `PASS` | |

---

## B. 既存導入パターン

### B1. ファセット更新の伝播

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 17 | orchestra 側の policy 変更 → セッション開始 | 変更が参照する全 skill/rule に反映 | `PASS` | orchestra 側を直接参照するため即反映 |
| 18 | orchestra 側の instruction 変更 | 該当 composition のみ再ビルド | `PASS` | review のみ変更、simplify は影響なし |
| 19 | orchestra 側の output-contract 変更 | 参照する composition が再ビルド | `PASS` | review に伝播、simplify は影響なし |
| 20 | ソース未変更（mtime 一致） | 再ビルドがスキップされる | `PASS` | 2回目・3回目の SessionStart で完全スキップ |

### B2. プロジェクトローカル上書き

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 21 | `.claude/facets/policies/code-quality.md` を手動配置 | ローカル版が優先される | `PASS` | `LOCAL POLICY` が生成物に反映 |
| 22 | `.claude/facets/instructions/review.md` を手動配置 | ローカル instruction が使われる | `PASS` | `LOCAL INSTRUCTION` が生成物に反映 |
| 23 | `.claude/facets/compositions/custom.yaml` を手動配置 | orchestra + local がマージされてビルド | `PASS` | local-test がビルドされた |
| 24 | 同名 composition（orchestra と local 両方に存在） | local が優先、重複なし | `PASS` | local simplify.yaml が優先 |

### B3. extract（チューニング後の書き戻し）

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 25 | 生成済み SKILL.md を手動編集 → `facet extract` | instruction がソースに書き戻される | `PASS` | 編集後の内容が instructions/ に書き戻された |
| 26 | policy 部分を手動編集 → extract | policy 変更は instruction に混入しない | `PASS` | LEAK_TEST マーカーが instruction に混入しない |

### B4. パッケージ追加・削除

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 27 | 新パッケージ追加 → セッション開始 | 新 composition が追加ビルドされる | `PASS` | cli-logging 追加後に checkpointing がビルドされた |
| 28 | パッケージ削除 → セッション開始 | 該当 composition が再ビルドされない | `PASS` | cli-logging 削除後に checkpointing が removed された |

### B5. 既存ファイルとの競合

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 29 | facet 導入前の手動 skill が存在（同名） | facet build で上書きされる | `PASS` | 手動 tdd が facet で上書きされた |
| 30 | facet に対応しない手動 skill | 影響を受けない（削除されない） | `PASS` | my-manual は残存 |

---

## C. エッジケース・異常系

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 31 | 存在しない policy を参照する composition | エラーメッセージ、部分ビルドしない | `PASS` | `facet ファイルが見つかりません` + exit 1 |
| 32 | 存在しない instruction を参照する composition | 同上 | `PASS` | `facet ファイルが見つかりません: .../instructions/...` + exit 1 |
| 33 | 空の composition YAML | バリデーションエラー | `PASS` | `composition の形式が不正です` + exit 1 |
| 34 | facets ディレクトリ未存在でビルド | 適切なエラーハンドリング | `PASS` | プロジェクト側に facets/ がなくても orchestra 側から正常ビルド |
| 35 | `facet build` タイムアウト（30秒超過） | graceful failure、stderr 出力 | `SKIP` | 再現困難 |
| 36 | 全 composition 一括ビルド | 全ファイル正常生成、所要時間が妥当 | `PASS` | 24 compositions ビルド成功 |

---

## E. ソース参照・配布フロー

facet ソースの参照先と配布設計に関するテスト。案 A（sync 廃止）導入後に追加。

### E1. ソース参照の正確性

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 37 | facet build が orchestra 側の policies を直接参照 | `.claude/facets/` を経由せず `$AI_ORCHESTRA_DIR/facets/policies/` から読み込む | `PASS` | `.claude/facets/` 未作成で正常ビルド |
| 38 | facet build が orchestra 側の instructions を直接参照 | 同上（instructions） | `PASS` | |
| 39 | facet build が orchestra 側の output-contracts を直接参照 | 同上（output-contracts） | `PASS` | |
| 40 | compositions は orchestra 側のみに存在 | プロジェクト側に compositions が配布されない | `PASS` | `.claude/facets/compositions/` は手動配置のみ |

### E2. SessionStart で .claude/facets/ が自動作成されない

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 41 | setup → SessionStart 後 | `.claude/facets/` ディレクトリが存在しない | `PASS` | sync 廃止により自動作成されない |
| 42 | 複数回 SessionStart 後 | `.claude/facets/` が作成されないまま | `PASS` | |

### E3. ローカル上書きは手動配置のみ

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 43 | `.claude/facets/policies/` を手動作成して上書き | ローカル版が優先される | `PASS` | B2-21 と同一。手動配置で正常動作 |
| 44 | ローカル上書きが SessionStart で消されない | 手動配置したファイルが保持される | `PASS` | sync 廃止により上書きリスクなし |
| 45 | ローカル上書き後の mtime 検知 | ローカルファイルの変更で再ビルドがトリガーされる | `PASS` | `build_facets` が `.claude/facets/` の mtime もチェック |

### E4. install と facet build の関係（manifest-SSOT: Issue #20 で変更）

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 46 | install 時に skills の静的コピーが行われない | `sync_packages()` が skills カテゴリをスキップ | `PASS` | Issue #20: skills は facet build に完全委譲。`packages/*/skills/` は削除済み |
| 47 | 初回 SessionStart で facet build が skills を生成 | `.claude/skills/*/SKILL.md` が facet build で生成される | `PASS` | 静的版が存在しないため「上書き」ではなく「新規生成」 |
| 48 | 2回目 SessionStart で facet 管理ファイルが再コピーされない | sync がスキップする | `PASS` | `_collect_facet_managed_paths()` でスキップ |

---

## F. Knowledge & Scripts（Issue #23）

facet composition の knowledge / scripts 機能に関するテスト。

### F1. Knowledge の配布

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 49 | composition に knowledge 定義 → facet build | `.claude/skills/{name}/references/*.md` が生成される | `-` | |
| 50 | 複数 knowledge エントリ | 全ファイルが references/ に配置される | `-` | |
| 51 | knowledge ファイルがプロジェクトローカルに存在 | ローカル版が優先される | `-` | |
| 52 | 存在しない knowledge を参照 | エラーメッセージ + exit 1 | `-` | |
| 53 | composition から knowledge エントリを削除 → rebuild | 古い references ファイルが削除される | `-` | |

### F2. Scripts の配布

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 54 | composition に scripts 定義 → facet build | `.claude/skills/{name}/scripts/*` が生成される | `-` | |
| 55 | scripts ファイルがプロジェクトローカルに存在 | ローカル版が優先される | `-` | |
| 56 | 存在しない script を参照 | エラーメッセージ + exit 1 | `-` | |
| 57 | composition から scripts エントリを削除 → rebuild | 古い scripts ファイルが削除される | `-` | |

### F3. SKILL.md への参照リンク

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 58 | knowledge 定義あり → SKILL.md 生成 | `## Additional resources` セクションが SKILL.md 末尾に含まれる | `-` | |
| 59 | knowledge 定義なし → SKILL.md 生成 | `## Additional resources` セクションが含まれない | `-` | |
| 60 | extract_one で instruction 書き戻し | `## Additional resources` が instruction に混入しない | `-` | |

### F4. リソース整合性

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 61 | 全 composition の knowledge 宣言 vs 実ファイル | 全エントリに対応する `facets/knowledge/{name}.md` が存在 | `-` | |
| 62 | 全 composition の scripts 宣言 vs 実ファイル | 全エントリに対応する `facets/scripts/{name}` が存在 | `-` | |
| 63 | パッケージ uninstall → orphan cleanup | references/ と scripts/ も削除される | `-` | |

### F5. 後方互換性

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 64 | knowledge/scripts 未定義の composition | 従来通りビルド成功、余計なディレクトリなし | `-` | |
| 65 | packages/*/skills/ が存在しない | facet build が正常動作（パッケージ側に依存しない） | `-` | |

---

## D. テスト手順詳細

### D1. 新規プロジェクトのセットアップ

```bash
# 1. テスト用ディレクトリ作成
mkdir -p /tmp/facet-test-new && cd /tmp/facet-test-new
git init

# 2. パッケージインストール（未初期化なら自動で init が実行される）
orchex install core --project .
orchex install quality-gates --project .

# 3. または setup で一括導入（推奨）
orchex setup essential --project .

# 4. 生成結果の確認（facet build は次回 SessionStart で自動実行）
orchex facet build --project .  # 手動実行する場合
ls -la .claude/skills/
ls -la .claude/rules/
cat .claude/skills/simplify/SKILL.md
```

### D2. 既存プロジェクトでの更新テスト

```bash
# 1. orchestra 側で policy を変更
# facets/policies/code-quality.md に1行追加

# 2. SessionStart（または手動ビルド）で反映
# orchestra 側を直接参照するため、sync 不要
orchex facet build --project <既存プロジェクト>

# 3. 変更が伝播したか確認
grep "追加した行" <既存プロジェクト>/.claude/skills/simplify/SKILL.md
```

### D3. ローカル上書きテスト

```bash
# 1. プロジェクト側に facets ディレクトリを手動作成
mkdir -p .claude/facets/policies
echo "# Local Override" > .claude/facets/policies/code-quality.md

# 2. ビルド
orchex facet build --project .

# 3. ローカル版が使われたか確認
head -5 .claude/skills/simplify/SKILL.md

# 4. クリーンアップ
rm -rf .claude/facets
```

### D4. extract テスト

```bash
# 1. 生成済みファイルの instruction 部分を編集
# .claude/skills/simplify/SKILL.md の instruction セクションを変更

# 2. extract で書き戻し
orchex facet extract --name simplify --project .

# 3. ソースに反映されたか確認
cat facets/instructions/simplify.md
```

---

## テスト結果サマリー

| カテゴリ | 合計 | PASS | FAIL | SKIP |
|---------|------|------|------|------|
| A. 新規導入（A1 + A1b） | 21 | 21 | 0 | 0 |
| B. 既存導入 | 14 | 14 | 0 | 0 |
| C. エッジケース | 6 | 5 | 0 | 1 |
| E. ソース参照・配布フロー | 12 | 12 | 0 | 0 |
| F. Knowledge & Scripts | 17 | 0 | 0 | 0 |
| **合計** | **70** | **52** | **0** | **1** |

## 発見した問題（解決済み）

| # | 関連テスト | 重要度 | 内容 | 対応 |
|---|----------|--------|------|------|
| 1 | B1-20 | Low | **解決済み**: checkpointing の sync → facet build 削除 → 再 sync の無限ループ。`_collect_facet_managed_paths()` で sync スキップ | `sync-orchestra.py` |
| 2 | C-32 | Low | **解決済み**: `policies` フィールドを省略可能に変更 | `orchestra-manager.py` |
| 3 | B1-20 | Medium | **解決済み**: facet 管理ファイルをパッケージ同期から除外し、毎セッションのフルリビルドを解消 | `sync-orchestra.py` |
| 4 | E1/E2 | Medium | **解決済み（設計変更）**: facets 同期（`.claude/facets/` への自動コピー）を廃止。orchestra 側を直接参照する設計に変更（案 A）。ローカル上書きは手動配置のみ | `sync-orchestra.py` |

## 設計メモ

### facet ソースの参照先（案 A）

- **compositions**: `$AI_ORCHESTRA_DIR/facets/compositions/` のみ（プロジェクトに配布しない）
- **policies / instructions / output-contracts**: `$AI_ORCHESTRA_DIR/facets/` を直接参照
- **ローカル上書き**: `.claude/facets/` に手動配置で優先される（`resolve_facet` がローカル → orchestra の順で解決）
- **将来（案 B）**: `*.local.md` 規則で config と一貫した上書きの仕組みを導入予定

### knowledge / scripts の管理（Issue #23）

- **ソース**: `facets/knowledge/*.md` と `facets/scripts/*`
- **定義**: `facets/compositions/*.yaml` の `knowledge:` / `scripts:` キーで宣言
- **配布先**: `.claude/skills/{name}/references/` と `.claude/skills/{name}/scripts/`（Claude Code 公式推奨に従う）
- **解決順序**: プロジェクトローカル `.claude/facets/knowledge/` → orchestra `facets/knowledge/`
- **SKILL.md 連携**: knowledge 定義時に `## Additional resources` セクションをSKILL.mdに自動挿入
- **クリーンアップ**: ビルド前に references/ と scripts/ をクリアして再配置。orphan 時は shutil.rmtree で削除
