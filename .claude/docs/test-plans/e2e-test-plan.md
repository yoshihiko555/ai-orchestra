# AI Orchestra E2E テスト計画

**作成日**: 2026-03-21
**対象**: orchex CLI + sync-orchestra.py のE2Eフロー
**目的**: ユニットテストでカバーできない統合フローの検証

---

## テスト環境

| 項目 | 値 |
|------|-----|
| ai-orchestra バージョン | |
| Python バージョン | |
| テスト用プロジェクト | /private/tmp/claude-501/e2e-* |
| 実施日 | |
| 実施者 | |

---

## 凡例

| ステータス | 意味 |
|-----------|------|
| `-` | 未実施 |
| `PASS` | 合格 |
| `FAIL` | 不合格 |
| `SKIP` | スキップ（理由を備考に記載） |

---

## 1. パッケージ導入フロー（install / uninstall）

### 1.1 install 基本

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 1 | 未初期化プロジェクトに `orchex install core` | 自動 init → `.claude/` 構造作成 → パッケージインストール | `PASS` | 自動初期化を確認 |
| 2 | install 後の `.claude/orchestra.json` | `installed_packages` にパッケージ名が記録 | `PASS` | |
| 3 | install 後の `.claude/settings.local.json` | manifest の hooks が登録されている | `PASS` | 5イベント, 7 hooks |
| 4 | install 後の config ファイル | `.claude/config/{pkg}/` にベース設定がコピーされている | `PASS` | task-memory.yaml |
| 5 | install + SessionStart 後の skills 生成 | facet build で `.claude/skills/` が生成される | `PASS` | Issue #20: skills は facet build に委譲。install 直後ではなく SessionStart 後に生成 |
| 6 | `$AI_ORCHESTRA_DIR` 環境変数 | `~/.claude/settings.json` に設定されている | `PASS` | |

### 1.2 install 依存解決

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 7 | 依存パッケージ未インストールで install | 警告メッセージが表示される | `PASS` | `警告: 依存パッケージが未インストール` |
| 8 | 依存パッケージインストール済みで install | 警告なしで正常完了 | `PASS` | |
| 9 | 同じパッケージを2回 install | 冪等（エラーにならず、状態が一貫） | `PASS` | 重複なし確認 |

### 1.3 uninstall

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 10 | `orchex uninstall <pkg>` | `orchestra.json` から除外される | `PASS` | |
| 11 | uninstall 後の hooks | `settings.local.json` から該当 hooks が除去される | `PASS` | |
| 12 | uninstall 後に他パッケージの hooks | 残存している（誤削除されない） | `PASS` | core + agent-routing の hooks 残存 |
| 13 | 未インストールパッケージを uninstall | エラーメッセージが表示される | `PASS` | exit 1 |

### 1.4 enable / disable

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 14 | `orchex disable <pkg>` | hooks が無効化される（settings.local.json から除去） | `PASS` | |
| 15 | disable 後の `orchestra.json` | `installed_packages` には残っている（uninstall ではない） | `PASS` | |
| 16 | `orchex enable <pkg>` | hooks が再登録される | `PASS` | |
| 17 | 未インストールパッケージを enable | エラーメッセージ | `PASS` | exit 1 |

---

## 2. setup フロー（一括導入）

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 18 | `orchex setup essential`（未初期化） | init → core, agent-routing, quality-gates が依存順にインストール | `PASS` | |
| 19 | setup 後の `orchestra.json` | 3パッケージが記録 | `PASS` | |
| 20 | setup 後の hooks | 全パッケージの hooks が登録 | `PASS` | |
| 21 | `orchex setup essential`（既導入） | 既存パッケージはスキップ、新規のみインストール | `PASS` | `スキップ: 3` |
| 22 | `orchex setup all`（未初期化） | 全パッケージが依存順にインストール | `PASS` | 10 パッケージ |
| 23 | setup 後に SessionStart | sync + facet build が正常実行 | `PASS` | 16 facets built |

---

## 3. SessionStart 統合フロー（sync-orchestra.py）

### 3.1 ファイル同期

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 24 | 初回 SessionStart | manifest 記載の全ファイルが `.claude/` にコピー | `PASS` | |
| 25 | 2回目 SessionStart（変更なし） | sync 0、facet build スキップ（出力なし） | `PASS` | パッケージハッシュ導入後に修正確認 |
| 26 | orchestra 側でファイル更新後の SessionStart | 更新ファイルのみコピー（mtime 差分検知） | `PASS` | `1 synced` |
| 27 | `*.local.yaml` がプロジェクト側に存在 | sync で上書き・削除されない | `PASS` | |
| 28 | `*.local.json` がプロジェクト側に存在 | sync で上書き・削除されない | `PASS` | |

### 3.2 Stale file cleanup

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 29 | manifest からファイルを削除 → SessionStart | プロジェクト側の該当ファイルが削除される | `PASS` | `1 removed` |
| 30 | stale cleanup で `*.local.yaml` | 削除されない | `PASS` | |
| 31 | パッケージ uninstall でファイル削除 | uninstall コマンドが skills/rules を直接削除、SessionStart で facet 再生成 | `PASS` | Issue #20: uninstall が composition 名形式で直接削除 |

### 3.3 Hook 同期

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 32 | manifest に hook 追加 → SessionStart | `settings.local.json` に hook が追加される | `PASS` | install で hooks 30→32 |
| 33 | manifest から hook 削除 → SessionStart | `settings.local.json` から hook が除去される | `PASS` | uninstall で 32→30 |
| 34 | 手動追加した hook（orchestra 管理外） | 削除されない | `PASS` | manual-hook 残存 |

### 3.4 Agent model patching

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 35 | `cli-tools.yaml` の model 変更 → SessionStart | agents/*.md の frontmatter が更新される | `PASS` | model: sonnet 確認 |
| 36 | `cli-tools.local.yaml` で model 上書き | local の値が優先される | `PASS` | `1 agent models patched` |

### 3.5 .claudeignore 生成

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 37 | SessionStart でテンプレートから生成 | `.claudeignore` が生成される | `PASS` | |
| 38 | `.claudeignore.local` 存在時 | テンプレート + local がマージされる | `PASS` | `my-custom-pattern` マージ確認 |
| 39 | `.claudeignore` 変更なし時 | 上書きされない（内容ベース差分チェック） | `PASS` | mtime 不変確認 |

---

## 4. Config loading フロー

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 40 | `cli-tools.yaml` のみ存在 | ベース値が使用される | `PASS` | codex.model: gpt-5.3-codex |
| 41 | `cli-tools.yaml` + `cli-tools.local.yaml` | local のキーで上書き、未定義キーはベース | `PASS` | |
| 42 | local でネストされたキーの一部を上書き | deep merge が正しく動作 | `PASS` | model 上書き + sandbox 維持 |
| 43 | local で `codex.enabled: false` | Codex CLI の呼び出しが全て無効化 | `PASS` | |
| 44 | local で `gemini.enabled: false` | Gemini CLI の呼び出しが全て無効化 | `PASS` | |

---

## 5. Facet-prompt フロー

**既存テスト計画を参照**: `facet-prompt-test-plan.md`（53 項目）

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 45 | facet build 基本フロー | 全 composition が正常ビルド | `PASS` | facet-prompt-test-plan A1-A3 |
| 46 | SessionStart 自動ビルド | mtime 検知 → 差分ビルド → スキップ | `PASS` | facet-prompt-test-plan A4, B1-20 |
| 47 | ローカル上書き | 手動配置の `.claude/facets/` が優先 | `PASS` | facet-prompt-test-plan B2 |
| 48 | extract | instruction の書き戻し | `PASS` | facet-prompt-test-plan B3 |
| 49 | facet build で knowledge が references/ に配布 | `.claude/skills/design/references/requirements.md` 等が存在 | `-` | Issue #23 |
| 50 | facet build で scripts が scripts/ に配布 | `.claude/skills/checkpointing/scripts/checkpoint.py` が存在 | `-` | Issue #23 |
| 51 | SKILL.md に Additional resources リンク | design の SKILL.md に references リンクが含まれる | `-` | Issue #23 |

---

## 6. Context 管理フロー

### 6.1 context build / check / sync

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 49 | `orchex context build` | `templates/context/*.md` → `templates/project/CLAUDE.md` 等が生成 | `PASS` | |
| 50 | `orchex context check` | ソースと生成物が一致していれば OK | `PASS` | `context check: OK` |
| 51 | ソース変更後に `context check` | 不一致を検出 | `PASS` | `context check: NG` + exit 1 |
| 52 | `orchex context sync --project <dir>` | CLAUDE.md, AGENTS.md, GEMINI.md がプロジェクトにコピー | `PASS` | |

### 6.2 セッション内コンテキスト共有

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 53 | SessionStart で context dir 初期化 | `.claude/context/session/meta.json` 作成 | `PASS` | e2e テスト追加（test_e2e_context_taskstate.py） |
| 54 | サブエージェント完了後 | `session/entries/` に結果が書き出される | `PASS` | e2e + live 検証。tool_name 不一致バグ修正済み |
| 55 | 次のサブエージェント起動時 | 前回の結果が prompt に注入される | `PASS` | e2e テスト追加 |
| 56 | SessionEnd | `session/` ディレクトリが削除される | `PASS` | e2e テスト追加 |

---

## 7. Cocoindex MCP プロキシ

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 57 | `proxy.enabled: true` で SessionStart | プロキシが起動、PID ファイル作成 | `SKIP` | cocoindex + uvx 必要 |
| 58 | 2回目の SessionStart | 起動済みならスキップ（冪等） | `SKIP` | cocoindex + uvx 必要 |
| 59 | `orchex proxy stop` | プロキシ停止、PID ファイル削除 | `SKIP` | cocoindex + uvx 必要 |
| 60 | .mcp.json にサーバー定義が書き出される | Claude Code が MCP 接続可能 | `SKIP` | cocoindex + uvx 必要 |
| 61 | `proxy.enabled: false`（または未設定） | プロキシ起動しない | `SKIP` | cocoindex + uvx 必要 |

---

## 8. エージェントルーティング

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 62 | 日本語プロンプトでエージェント提案 | `[Agent Routing]` が正しいエージェントを提案 | `PASS` | backend-python-dev 提案 |
| 63 | 英語プロンプトでエージェント提案 | 同上 | `PASS` | researcher 提案 |
| 64 | `tool: auto` のエージェント | タスク種別に応じた CLI 選択が提案される | `PASS` | |
| 65 | `tool: codex` のエージェント | Codex CLI 使用が提案される | `PASS` | |
| 66 | `tool: claude-direct` のエージェント | 外部 CLI なしの直接処理が提案される | `PASS` | |
| 67 | `codex.enabled: false` 時 | Codex 提案が抑制される | `PASS` | |

---

## 9. 横断シナリオ（ライフサイクルE2E）

### 9.1 新規プロジェクト導入 → 運用 → パッケージ追加

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 68 | setup essential → SessionStart → facet build 確認 → パッケージ追加 → SessionStart | 全フローがエラーなく完了 | `PASS` | 16 facets → codex-suggestions 追加 → 38 facets |
| 69 | 上記の後に2回目 SessionStart | 出力なし（完全スキップ） | `PASS` | |

### 9.2 orchestra 側の更新 → プロジェクト反映

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 70 | facet policy 変更 → SessionStart | 変更が全参照 skill に伝播 | `PASS` | simplify + coding-principles に伝播確認 |
| 71 | 新しい agent .md 追加 → SessionStart | manifest に追加されていれば同期される | `PASS` | manifest に未記載のファイルは同期されない（仕様通り） |
| 72 | config 値変更 → SessionStart | `.claude/config/` のベース値が更新、local は保持 | `PASS` | base 更新 + local.yaml 保持を確認 |

### 9.3 パッケージ削除 → クリーンアップ

| # | テスト内容 | 期待結果 | 結果 | 備考 |
|---|----------|---------|------|------|
| 73 | uninstall → SessionStart | 該当パッケージの skills/agents/rules/hooks が全て除去 | `PASS` | **バグ発見→修正**: orchestra.json の mtime を build_facets に追加。修正後は codex-system / codex-delegation が正しく削除 |
| 74 | uninstall 後に残パッケージが正常動作 | 他パッケージの hooks/skills に影響なし | `PASS` | review, simplify, agent-router hooks 残存確認 |
| 75 | uninstall 後に references/ と scripts/ がクリーンアップ | orphan skill の references/ scripts/ も削除される | `-` | Issue #23 |

---

## テスト結果サマリー

| カテゴリ | 合計 | PASS | FAIL | SKIP |
|---------|------|------|------|------|
| 1. install / uninstall | 17 | 17 | 0 | 0 |
| 2. setup | 6 | 6 | 0 | 0 |
| 3. SessionStart sync | 16 | 16 | 0 | 0 |
| 4. Config loading | 5 | 5 | 0 | 0 |
| 5. Facet-prompt（参照） | 7 | 4 | 0 | 0 |
| 6. Context 管理 | 8 | 8 | 0 | 0 |
| 7. Cocoindex proxy | 5 | 0 | 0 | 5 |
| 8. エージェントルーティング | 6 | 6 | 0 | 0 |
| 9. 横断シナリオ | 8 | 7 | 0 | 0 |
| **合計** | **78** | **69** | **0** | **5** |

**SKIP 理由**:
- 7. Cocoindex proxy #57-61: cocoindex パッケージと uvx ランタイムが必要

## 発見した問題

| # | 関連テスト | 重要度 | 内容 | 対応 |
|---|----------|--------|------|------|
| 1 | #73 | Medium | **解決済み**: パッケージ uninstall 後、`build_facets` の mtime スキップが `orchestra.json` の変更を考慮しないため、不要な facet 生成物が削除されない | `sync-orchestra.py`: `installed_packages` の内容ハッシュで変更検知 |
| 2 | #25 | Medium | **解決済み**: 初回 SessionStart で `.claudeignore` 更新 → `orchestra.json` 再保存 → 次回 facet build の mtime スキップが無効化 | `sync-orchestra.py`: mtime ではなくパッケージハッシュ（`.facet-packages-hash`）で判定に変更 |
| 3 | #54 | High | **解決済み**: capture-task-result / inject-shared-context が動作しない。Claude Code は `tool_name="Agent"` を送るが hook が `"Task"` を期待していた | 4 hook スクリプト + 3 manifest + settings.local.json の matcher を修正。e2e テスト 39 件追加 |
