# antigravity-suggestions 評価セット

**パッケージ**: `packages/antigravity-suggestions`
**類型**: hook 型
**作成日**: 2026-07-03
**最終レビュー日**: —（未レビュー）
**情報源**: docs/reference/packages.md（antigravity-suggestions セクション）, .claude/rules/antigravity-suggestion-compliance.md, .claude/rules/antigravity-delegation.md（補助・後方互換の根拠のみ）, packages/antigravity-suggestions/manifest.json（構成要素列挙のみ）, packages/antigravity-suggestions/hooks/suggest-antigravity-research.py（構成要素列挙のみ、期待値導出には未使用）

## 1. 責務定義

WebSearch/WebFetch ツールの実行前に、Antigravity CLI（`agy`）でのリサーチを代替提案する PreToolUse hook。hook 自体は `[Antigravity Suggestion]` という提案テキストを出力するのみで、実行をブロックしない前提のもとに設計されている。オーケストレーター（Claude Code）はこの提案を検知した場合、`.claude/rules/antigravity-suggestion-compliance.md` の遵守手順（保留 → サブエージェント経由で Antigravity に委譲 → 結果を踏まえて続行）に従うことが期待仕様となる。

### Non-Goals

- Antigravity CLI（`agy`）自体の呼び出し実行（それはサブエージェント/オーケストレーターの責務であり、hook はテキスト提案を出すのみ）
- WebSearch/WebFetch 以外のツール呼び出しへの提案（matcher が `WebSearch|WebFetch` に限定）
- 提案の強制ブロック（hook が exit code で WebSearch/WebFetch を強制停止する仕様はドキュメントに記載がない。遵守はルールベースの運用契約）

## 2. 期待する入出力・副作用

| 構成要素                                  | 入力                                                                       | 期待する出力                                                                            | 副作用                                             |
| ----------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `suggest-antigravity-research.py`（hook） | PreToolUse イベント（`tool_name` が `WebSearch` または `WebFetch`）        | `[Antigravity Suggestion]` を含む提案テキスト（追加コンテキストとして注入、非ブロック） | なし（ファイル書き込み等はドキュメントに記載なし） |
| `antigravity-system`（skill）             | ユーザー/エージェントからの Antigravity CLI 利用に関する自然言語リクエスト | Antigravity CLI 利用ガイダンス                                                          | なし                                               |

## 3. 評価観点

- [ ] EV-01（正常 / must）: WebSearch ツール呼び出し前に PreToolUse hook が発火し `[Antigravity Suggestion]` を出力する — 根拠: docs/reference/packages.md
- [ ] EV-02（正常 / must）: WebFetch ツール呼び出し前に PreToolUse hook が発火し `[Antigravity Suggestion]` を出力する — 根拠: docs/reference/packages.md
- [ ] EV-03（異常 / must）: `antigravity.enabled: false` のとき、hook 自体が提案を抑制する（発火しない） — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-04（異常 / should）: `.local.yaml` に旧 `gemini.enabled: false` が残っている場合も有効な無効化設定として尊重され、提案が抑制される — 根拠: .claude/rules/antigravity-delegation.md
- [ ] EV-05（正常 / must）: `[Antigravity Suggestion]` を検知したオーケストレーターは、進行中の WebSearch/WebFetch 操作を一旦保留する — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-06（正常 / must）: オーケストレーターはサブエージェント経由（`Task(subagent_type="general-purpose", ...)`）で Antigravity にリサーチを依頼する — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-07（正常 / should）: Antigravity の結果を踏まえて、保留していた WebSearch/WebFetch 操作を続行する — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-08（境界 / should）: ユーザーが明示的にスキップを指示した場合、Antigravity 相談をスキップしてよい — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-09（境界 / should）: 同一セッション内で同じトピックについて既に Antigravity 相談済みの場合、再度の相談をスキップしてよい — 根拠: .claude/rules/antigravity-suggestion-compliance.md
- [ ] EV-10（境界 / must）: hook の matcher は `WebSearch|WebFetch` に限定され、それ以外のツール呼び出し（Read/Bash/Edit 等）では発火しない — 根拠: packages/antigravity-suggestions/manifest.json, docs/reference/packages.md

## 4. 類型別観点

- [ ] EV-11（正常 / must）: hook は WebSearch/WebFetch の実行自体をブロックせず、`[Antigravity Suggestion]` を含む追加コンテキストを注入する形で提案する（遵守は Claude 側のルールベース運用であり、hook が exit code で強制ブロックする仕様ではない） — 根拠: docs/reference/packages.md（「を出力」という記述）+ .claude/rules/antigravity-suggestion-compliance.md（手順が「保留」「続行」という Claude 側の運用として記述され、hook 側の強制停止としては記述されていない）
- [ ] EV-12（境界 / should）: EV-11 の非ブロック方針の帰結として、hook は正常系の exit code（0 等の非ブロックコード）で終了し WebSearch/WebFetch 自体の実行を妨げない — 根拠: 同上（ドキュメントからの論理的導出）
- （config 駆動）EV-03 / EV-04 で担保済み。新規 ID は割り当てない
- N/A: fail-safe 方針（hook 内部エラー時の fail-open/fail-closed の選択） — ドキュメントに記載なし
- N/A: 冪等性（同一イベントでの重複発火抑制） — ドキュメントに記載なし。EV-09 はオーケストレーター側の「相談スキップ」判断であり、hook 自体の重複発火制御ではないため代替不可
- N/A: 秘匿情報（提案文言への秘密情報混入防止） — ドキュメントに記載なし
- N/A: 性能（同期 hook が WebSearch/WebFetch 実行を著しく遅延させないか） — ドキュメントに記載なし

## 5. テストレビュー判断基準（パッケージ固有）

- EV-05〜EV-09 は「hook の出力」ではなく「hook 出力を受けたオーケストレーターの振る舞い」を規定する観点である。hook 単体のユニットテストでは検証できないため、統合テストまたは手順ドキュメントとの突合で確認すること。
- EV-03 / EV-04 の抑制テストでは、`antigravity.enabled` と旧 `gemini.enabled` の両キーが同時に設定された場合の優先順位をテストの期待値に持ち込まないこと（ドキュメントからは優先順位が確定できない。情報源に明記なし。仕様確定・文書化はパッケージ別ギャップ Issue で追跡）。
