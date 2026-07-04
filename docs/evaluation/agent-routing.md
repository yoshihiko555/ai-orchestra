# agent-routing 評価セット

**パッケージ**: `packages/agent-routing`
**類型**: 主: hook 型、副: 設定・エージェント定義の配布（README.md の 3 類型のうちどれにも完全一致しないため、共通チェックリストの「配布ライフサイクル」「後方互換性」で代替評価する。詳細は 4 節参照）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-04（Issue #124 対応: 検証方法（manual/policy review）の明示、EV-11/EV-22/EV-26 の検証状態更新、4節 N/A 理由の具体化を実施。観点数・優先度は変更なし）
**情報源**: docs/reference/packages.md（agent-routing セクション）, .claude/rules/agent-routing-policy.md, .claude/rules/codex-delegation.md, .claude/rules/antigravity-delegation.md, .claude/rules/config-loading.md
**補助参照（構成要素の列挙のみ）**: packages/agent-routing/manifest.json, packages/agent-routing/{hooks,agents,config}/ 配下のファイル名一覧

## 1. 責務定義

`cli-tools.yaml` に基づき、ユーザープロンプトから適切なエージェントを検出して `[Agent Routing]` 提案を行う（`agent-router.py`, UserPromptSubmit hook）。また、28 種のエージェント定義（`agents/*.md`）、ルーティング運用ルール（`orchestra-usage.md`, `agent-routing-policy.md`）、CLI ルーティング設定（`cli-tools.yaml`）の配布元としても機能する。オーケストレーターは hook の提案が出た場合、提案されたエージェントをサブエージェント経由（`Task(subagent_type=...)`）で呼び出すことが期待される。

### Non-Goals

- Codex/Antigravity CLI の具体的な呼び出しコマンド組み立てそのもの（各エージェント/スキルの実行時責務）
- サブエージェント実行結果の要約・コンテキスト共有（`core` パッケージの責務）
- レビュアー選定ロジック（`skill-review-policy.md` の責務）

## 2. 期待する入出力・副作用

| 構成要素                         | 入力                                                      | 期待する出力                                                                                                                                                                                                                       | 副作用                                                                          |
| -------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| hook `agent-router.py`           | UserPromptSubmit イベント（ユーザープロンプト）           | プロンプトがエージェント検出条件に一致した場合 `[Agent Routing]` 提案（エージェント名 + tool 種別）を追加コンテキストとして出力。一致しない場合の出力仕様は情報源に明記なし（仕様確定・文書化はパッケージ別ギャップ Issue で追跡） | ドキュメント上は明記なし（仕様確定・文書化はパッケージ別ギャップ Issue で追跡） |
| util `route_config.py`           | `cli-tools.yaml`（+ `.local.yaml`）                       | ルーティング設定の読み込み結果・エージェント検出ロジックの判定結果（`agent-router.py` から利用される想定）                                                                                                                         | なし（設定読み込みのみ、ドキュメント上の記載範囲）                              |
| agents（28 定義, `agents/*.md`） | なし（静的定義ファイル）                                  | `Task(subagent_type=...)` から参照可能なエージェント振る舞い定義                                                                                                                                                                   | `.claude/agents/` への sync 配布                                                |
| rule `agent-routing-policy.md`   | なし（静的ドキュメント）                                  | オーケストレーターが従うべきルーティング遵守手順・例外規定                                                                                                                                                                         | `.claude/rules/` への sync 配布                                                 |
| rule `orchestra-usage.md`        | なし（静的ドキュメント）                                  | CLI 言語プロトコル・エージェント一覧・ワークフロー例の提示                                                                                                                                                                         | `.claude/rules/` への sync 配布                                                 |
| config `cli-tools.yaml`          | なし（静的設定。プロジェクト側で `.local.yaml` 上書き可） | `codex`/`antigravity` のモデル・sandbox・flags、`agents.<name>.tool` のルーティング解決に使う値を提供                                                                                                                              | `.claude/config/agent-routing/` への sync 配布                                  |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `agent-router.py` はプロンプトがエージェント検出条件に一致した場合 `[Agent Routing]` 提案を出力する — 根拠: .claude/rules/agent-routing-policy.md
- [ ] EV-02（正常 / must）: `[Agent Routing]` 提案が出た場合、オーケストレーターは提案されたエージェントを `Task(subagent_type=...)` 経由で呼び出し、Codex/Antigravity CLI を直接 Bash で実行しない — 根拠: .claude/rules/agent-routing-policy.md — 検証方法: manual/policy review（hook 出力文字列は自動検証済み。オーケストレーターが Task 経由で呼ぶ遵守自体は運用でのみ確認可能）
- [ ] EV-03（正常 / should）: hook が提案を出さなかった場合、オーケストレーターの判断で直接実行してよい — 根拠: .claude/rules/agent-routing-policy.md — 検証方法: manual/policy review（提案なしケースの hook 出力は自動検証済み。その後の裁量判断は運用）
- [ ] EV-04（正常 / must）: ルーティング解決は (1) `cli-tools.yaml` 読み込み → (2) `cli-tools.local.yaml` があれば上書き適用 → (3) `{tool}.enabled` 確認 → (4) `agents.{name}.tool` で実行先決定 → (5) 一致する tool のみ呼び出す、の順で行う — 根拠: .claude/rules/codex-delegation.md 判定手順, .claude/rules/antigravity-delegation.md 判定手順
- [ ] EV-05（正常 / must）: `agents.<name>.tool == "codex"` の場合は Codex CLI を使用する — 根拠: .claude/rules/codex-delegation.md ルーティング規則表
- [ ] EV-06（正常 / must）: `agents.<name>.tool == "antigravity"` の場合は Antigravity CLI（`agy`）を使用する — 根拠: .claude/rules/antigravity-delegation.md
- [ ] EV-07（正常 / must）: `agents.<name>.tool == "claude-direct"` の場合は外部 CLI を呼ばず Claude で処理する — 根拠: .claude/rules/codex-delegation.md, .claude/rules/antigravity-delegation.md
- [ ] EV-08（正常 / should）: `agents.<name>.tool == "auto"` の場合、深い推論（設計判断・デバッグ・比較検討・レビュー）→ Codex、外部調査・最新ドキュメント確認 → Antigravity、単純編集・明確な単一解・テスト/lint → Claude direct のヒューリスティクスで選択する — 根拠: .claude/rules/codex-delegation.md `tool: auto` ヒューリスティクス表 — 検証方法: manual/policy review（`tool: auto` の選択ロジックはコード実装が存在せず（route_config は auto をそのまま返す）、ヒューリスティクスはルール文書のみ）
- [ ] EV-09（正常 / must）: 設定は `cli-tools.yaml`（ベース）→ `cli-tools.local.yaml`（上書き）の順で解決し、local に未定義のキーはベース値を継続使用する — 根拠: .claude/rules/config-loading.md
- [ ] EV-10（正常 / must）: Codex CLI 呼び出し時は stdin を `< /dev/null` で封じる — 根拠: .claude/rules/codex-delegation.md Non-Interactive 実行
- [ ] EV-11（正常 / should）: Antigravity（`agy -p`）呼び出しは非対話完結のため `< /dev/null` は不要 — 根拠: .claude/rules/antigravity-delegation.md Non-Interactive 実行 — 自動テスト: `build_cli_suggestion()` の antigravity 出力に `< /dev/null` が含まれないことを否定 assert する形で `packages/agent-routing/tests/test_agent_router.py` に追加される（本 PR）
- [ ] EV-12（正常 / must）: Codex/Antigravity への質問は英語、ユーザーへの報告は日本語で行う — 根拠: .claude/rules/orchestra-usage.md CLI Language Policy — 検証方法: manual/policy review（CLI 言語ポリシーは対象コードなし）
- [ ] EV-13（異常 / must）: `codex.enabled == false` の場合、Codex は呼び出されずフォールバック方針に従う — 根拠: .claude/rules/codex-delegation.md
- [ ] EV-14（異常 / must）: `antigravity.enabled == false` の場合、Antigravity 呼び出しは全て無効化され、使用エージェントは自動的に `claude-direct` にフォールバックする — 根拠: .claude/rules/antigravity-delegation.md
- [ ] EV-15（異常 / should）: Codex 呼び出しが長時間無出力の場合、`< /dev/null` の有無確認 → `2>/dev/null` を外した再実行での stderr 確認 → モデル疎通確認、の順でハングを調査する — 根拠: .claude/rules/codex-delegation.md ハング調査プロトコル — 検証方法: manual/policy review（ハング調査手順はランブックであり自動テスト対象コードなし）
- [ ] EV-16（異常 / should）: Antigravity が質問文（`?` で終わる文、"Could you clarify" 等の質問フレーズ）を返した場合、追加コンテキスト付きで最大 2 回までリトライし、3 回目の失敗で報告する — 根拠: .claude/rules/antigravity-delegation.md リトライプロトコル — 検証方法: manual/policy review（Antigravity リトライプロトコルは実装コードが存在せず、ルール文書上の運用手順のみ）
- [ ] EV-17（異常 / should）: `antigravity.model` が `antigravity.model_allowlist` に含まれない場合、実行前に `[WARN] model '<value>' not in allowlist` を出力する（agy は無効な slug でも exit 0 でデフォルトモデルに黙ってフォールバックするため） — 根拠: .claude/rules/antigravity-delegation.md
- [ ] EV-18（境界 / must）: 旧 `gemini.enabled: false`（`.local.yaml` 残存分）は `antigravity.enabled: false` と等価に扱われる（後方互換） — 根拠: .claude/rules/antigravity-delegation.md「旧 gemini 設定からの移行」表
- [ ] EV-19（境界 / must）: 旧 `agents.<name>.tool: gemini` は `agents.<name>.tool: antigravity` と同義に自動読み替えされる（後方互換） — 根拠: .claude/rules/antigravity-delegation.md「旧 gemini 設定からの移行」表
- [ ] EV-20（境界 / should）: 旧 `gemini.model` の値は引き継がれず無視され、`antigravity.model` を明示的に設定する必要がある — 根拠: .claude/rules/antigravity-delegation.md「旧 gemini 設定からの移行」表
- [ ] EV-21（境界 / should）: 「明らかに 1 行で完結する CLI 呼び出し」または「ユーザーが明示的に直接実行を指示した場合」に限り、hook の提案を経ずオーケストレーターが直接実行してよい（例外規定） — 根拠: .claude/rules/agent-routing-policy.md 例外 — 検証方法: manual/policy review（直接実行の例外規定はオーケストレーター裁量）
- [ ] EV-22（境界 / should）: Codex の実行は分析用途（`sandbox.analysis` = read-only）と実装用途（`sandbox.implementation` = workspace-write）で sandbox モードを使い分ける — 根拠: .claude/rules/codex-delegation.md Sandbox モード表 — **自動テスト範囲の限定**: hook（`agent-router.py`）は提案文に `sandbox.analysis` のみを表示し、`sandbox.implementation` との使い分け自体はオーケストレーター運用（.claude/rules/codex-delegation.md）でのみ定義される。自動テストで検証できるのは「hook が analysis のみ表示すること」に限られ、分析/実装の使い分けの実行判断自体は検証方法: manual/policy review

## 4. 類型別観点

<!-- README.md の類型別チェックリストのうち該当するものを具体化。該当しない項目は N/A として理由を明示する -->

### hook 型（`agent-router.py`）

- [ ] EV-23（正常 / should）: stdin/stdout 契約 — UserPromptSubmit イベント（プロンプト文字列）を入力とし、エージェント/tool 一致検出時のみ `[Agent Routing]` 形式の追加コンテキストを出力する（EV-01 と同一根拠）。ただし厳密な JSON 入出力スキーマは情報源に定義がない — 根拠: .claude/rules/agent-routing-policy.md（提案時の出力形式のみ）。スキーマ全体は情報源に明記なし（仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。非検出時の挙動: 現状実装では、エージェント・researcher フォールバックのいずれにも一致しない場合、標準出力に何も出力せず exit 0 する（副作用なし）（`agent-router.py` で確認、仕様として未文書化）
- N/A: exit code 規約 — 指定情報源（packages.md / 4 ルールファイル）に `agent-router.py` の exit code 規約の記載がなく、実装（hooks/agent-router.py）を根拠にすることは本タスクの情報源制約上できないため評価対象外（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。現状実装では、正常時・非検出時・例外時（fail-open、stderr にメッセージ出力）のすべてで exit 0 となり、非ゼロ exit のコードパスは存在しない（`agent-router.py` で確認、仕様として未文書化）
- N/A: fail-safe 方針 — 同上の理由でドキュメントに記載がなく評価対象外
- [ ] EV-24（正常 / must）: config 駆動 — `codex.enabled`/`antigravity.enabled` フラグと `cli-tools.local.yaml` 上書きにより、対応する CLI 連携を無効化できる（EV-13/EV-14/EV-09 と同一根拠のため新規チェックボックスは追加せず、当該 EV を参照する）
- N/A: 冪等性 — 指定情報源に同一プロンプトでの再実行時の二重提案抑止に関する記載がなく評価対象外（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。現状実装では、hook はステートレスであり同一プロンプトを再実行すると毎回同一の提案を出力する（二重提案抑止機構なし）（`agent-router.py` で確認、仕様として未文書化）
- N/A: 秘匿情報 — 指定情報源に `agent-router.py` のログ・注入コンテキストにおけるマスキング仕様の記載がなく評価対象外
- N/A: 性能 — 指定情報源に hook 実行時間の目標値・許容遅延の記載がなく評価対象外

### 共通（全類型）

- 後方互換性: 旧 `gemini` キー体系（`enabled` / `tool` 値）が新 `antigravity` キー体系と等価に扱われ、既存設定を壊さない → EV-18, EV-19 で担保（新規 ID は追加しない）
- [ ] EV-25（境界 / should）: 配布ライフサイクル — sync 時に `*.local.yaml` / `*.local.json` は同期・削除の対象外として保持され、ベース設定ファイルの更新で上書き・削除されない — 根拠: .claude/rules/config-loading.md
- N/A: 生成物の同期（テンプレート/facet 由来かの確認） — 指定情報源（packages.md / 4 ルールファイル）に `packages/agent-routing/{agents,rules,config}` が templates 由来の build 生成物か直接配布正本かの記載がなく判断不可（情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。現状実装では、`packages/agent-routing/agents/` と `config/` は packages 直下が直接配布正本であり `sync-orchestra.py` がそのままコピー同期する。一方 manifest.json の `rules`（`["orchestra-usage", "agent-routing-policy"]`）が指す実体は `facets/instructions/` が正本で facet build により `.claude/rules/` へ直接生成される（packages 配下に `rules/` ディレクトリは存在しない）という非対称な構成になっている（manifest.json / facets 構成で確認、仕様として未文書化）
- [ ] EV-26（正常 / should）: ドキュメント整合 — `packages/agent-routing/manifest.json` の `agents` 配列（28 件）が `docs/reference/packages.md` のエージェント一覧表（Planning 3 + Design 5 + Implementation 3 + AI/ML 4 + Test/Debug 2 + Review 6 + Docs 1 + Utility 4 = 28）と一致する — 根拠: docs/reference/packages.md, packages/agent-routing/manifest.json（ファイル名列挙による突合） — 自動テスト: `manifest.json` の `agents` 配列と `docs/reference/packages.md` 一覧表の突合テストが `tests/unit/test_agent_routing_consistency.py` に新設される（本 PR）。なお `.claude/rules/orchestra-usage.md` 側の一覧表（25 件）にも本 PR で 3 エージェント（specialized-mcp-builder, support-executive-summary-generator, testing-reality-checker）が追記される

## 5. テストレビュー判断基準（パッケージ固有）

- ルーティング分岐（EV-05〜EV-08）のテストは、`cli-tools.yaml` の値をコピーした期待値ではなく、ルール文書（`codex-delegation.md` / `antigravity-delegation.md`）の規則表から独立に期待値を導出しているか確認する。
- 後方互換観点（EV-18〜EV-20）は、`.local.yaml` に旧 `gemini` キーのみが残存するケース・新旧混在するケースの両方を独立したテストケースとして検証する（正常系の副産物にしない）。
- hook の exit code / fail-safe / 冪等性 / 秘匿情報 / 性能（4 節で N/A とした項目）についてテストが存在する場合、その期待値の根拠がドキュメントか実装かを明示させ、実装追認になっていないか重点確認する。
