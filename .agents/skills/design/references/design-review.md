# 設計レビューゲート（品質ゲート Stage 2）

design スキルの各フェーズ末で実施する、設計ドキュメント専用の自動レビュー手順。
セルフチェック（Stage 1、各 reference 末尾のチェックリスト）を通過した成果物に対して、
レビュアーサブエージェントによる第三者視点の検証を行う。

## ゲートの全体像

```
成果物作成
    ↓
Stage 1: セルフチェック（オーケストレーター自身。エージェント起動なし）
    ↓ 未達項目を修正
Stage 2: 自動レビュー（フェーズ対応のレビュアーサブエージェント）
    ↓ Tiered Output（Critical/High/Medium/Low）
ゲート通過判定（Critical = 0、High 処理済み）
    ↓
受け入れ確認（AskUserQuestion。レビュー結果サマリを添えて）
```

## フェーズ別レビュアー

| フェーズ         | レビュアー                                                | 主な観点                                               |
| ---------------- | --------------------------------------------------------- | ------------------------------------------------------ |
| Phase 1 要件定義 | `requirements`                                            | 網羅性・矛盾・曖昧さ・MoSCoW 妥当性・スコープクリープ  |
| Phase 2 基本設計 | `architecture-reviewer`（+ 条件付き `security-reviewer`） | 要件トレーサビリティ・アーキ妥当性・既存コードとの整合 |
| Phase 3 詳細設計 | `spec-reviewer`                                           | 基本設計との整合・実装可能性                           |

- **Phase 0（影響範囲分析）は専用レビュアーを起動しない。** 既存の受け入れチェックリストを Stage 1 として扱い、成果物（impact-analysis）は Phase 1 レビューのコンテキストに含めて後段で検証する。
- **拡張トラックの成果物**（security-design / test-design / design-system）を生成した場合は、生成したフェーズのレビュー対象に必ず含める（レビュー対象パスに追加する）。
- ルーティングは `cli-tools.yaml` の `agents.{name}.tool` に従う（agent-routing-policy 参照）。
- レビュアーはコードレビュー向けに定義されているため、プロンプトで **「設計ドキュメントのレビューである」ことを必ず明示** し、対象ファイルパスを列挙する（git diff に依存しない）。

### security-reviewer 追加条件（Phase 2）

以下のいずれかに該当する場合、`architecture-reviewer` と並列で `security-reviewer` も起動する:

認証・認可 / 権限モデル / 秘密情報・PII / コンプライアンス要件 / 決済 / テナント分離 / 外部システム連携 / 公開 API

## 設計ドキュメント用の重要度定義

コード用の基準（skill-review-policy）を設計ドキュメント向けに読み替える。レビュアーへのプロンプトにもこの定義を含める。

| 重要度       | 設計ドキュメントでの基準                                                                                                                                                                       |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Critical** | 実現不可能・安全でない設計 / Must 要件との矛盾 / コアフローの欠落 / 未緩和のセキュリティ・データ損失リスク / 移行計画のない後方非互換な API・スキーマ変更 / 推測なしでは実装を開始できない欠落 |
| **High**     | 大きな手戻りを生みうる曖昧さ / 未確定のまま残されたアーキ・API・データ設計判断 / 重要経路の非機能要件未達 / フェーズ間の重大な不整合 / 文書化されていないリスク受容                            |
| **Medium**   | エッジケースの記述漏れ / 軽微なトレーサビリティ欠落 / 曖昧な命名                                                                                                                               |
| **Low**      | 文言・書式・軽微な構成                                                                                                                                                                         |

- **High の対応は 3 択に限定する**: (a) 今すぐ修正 / (b) 理由を明記してリスク受容 / (c) タスク化して先送り（`Plans.md` に `cc:TODO` で登録）。いずれも記録を残す。
- 同種の Medium 指摘が同一ドキュメント内で繰り返される場合は High に格上げする。

## ゲート通過条件

1. **Critical = 0** — 修正後は該当レビュアーで再レビューし、解消を確認する（指摘該当箇所に絞ってよい。構造的な指摘の場合のみフル再レビュー）
2. **High がすべて処理済み** — 修正 / 理由付き受容 / タスク化のいずれか
3. Medium / Low は報告のみ（受け入れ確認に添えてユーザーに提示）

### 受け入れ確認時の提示内容

skill-review-policy の Tiered Review Output Contract に従い、受け入れ確認（AskUserQuestion）の前に以下を提示する:

- レビュー結果サマリ（severity 別件数と Critical/High の対応内容）
- セルフチェックの結果 — **成果物マニフェスト**: 作成済みドキュメント / スキップしたドキュメントと理由 / 未解決課題・前提事項

セルフチェックを「全部チェック済み」の一言で済ませない（形骸化防止）。スキップや未解決を明示することが目的。

## フェーズ間ドリフトプロトコル

後続フェーズでの変更が上流ドキュメントと矛盾する場合（例: 詳細設計中に API 仕様を変えた）:

1. **上流ドキュメント（要件・基本設計）を先に更新する**
2. 上流の該当ゲートを再実行する（変更箇所に絞ってよい）
3. codd フロントマターの `depends_on` を更新し、`/codd-validate` で整合を確認する

下流だけ直して上流を放置しない。上流が正、が原則（要件そのものが誤りだった場合はユーザーに確認してから上流を直す）。

## レビュアープロンプトテンプレート

共通ルール:

- 設計ドキュメントのレビューであり、コードレビューではないことを明示する
- レビュー対象のファイルパスを明示的に列挙する
- 上流ドキュメント（トレース先）と Phase 0 の impact-analysis をコンテキストとして渡す
- 重要度定義（上表）をプロンプトに含める
- 出力は Tiered Output（各指摘に file / セクションを明記）、報告は日本語

### Phase 1: 要件レビュー（requirements）

```
Task(subagent_type="requirements", prompt="""
You are REVIEWING requirements DESIGN DOCUMENTS, not code. Act as a critical reviewer, not an extractor.

Documents to review:
- docs/project-overview.md
- docs/glossary.md
- docs/requirements/feature-list.md
- docs/requirements/functional.md
- docs/requirements/non-functional.md
{拡張トラック成果物があれば追加（例: docs/security/threat-model.md）}

Context (for consistency checks, not review targets):
- .claude/docs/impact-analysis/{date}_{slug}.md — existing-code impact analysis

Review perspectives:
1. Coverage — does the feature list cover the stated project goals? Any missing core flows?
2. Contradictions — between functional requirements, and against the impact analysis
3. Ambiguity — acceptance criteria that are not verifiable ("fast", "easy to use", etc.)
4. MoSCoW validity — Must inflation, priorities inconsistent with the stated goal
5. Scope creep — features without ties to the project goal
6. Traceability — every Must feature has functional requirements

Severity definitions (design documents):
{設計ドキュメント用の重要度定義を貼り付け}

Report findings in tiered format (Critical/High/Medium/Low) with file and section for each finding.
Report in Japanese.
""")
```

### Phase 2: 基本設計レビュー（architecture-reviewer）

```
Task(subagent_type="architecture-reviewer", prompt="""
You are REVIEWING basic-design DESIGN DOCUMENTS, not code.

Documents to review:
- docs/architecture/architecture.md
- docs/screens/screen-list.md, docs/screens/screen-transitions.md（UI ありの場合）
- docs/api/api-list.md（API ありの場合）
- docs/database/er-design.md（DB ありの場合）
{拡張トラック成果物があれば追加}

Context (upstream, for traceability checks):
- docs/requirements/feature-list.md, functional.md, non-functional.md
- .claude/docs/impact-analysis/{date}_{slug}.md

Review perspectives:
1. Requirement traceability — every Must feature maps to screens/APIs/data model
2. Architectural soundness — component boundaries, technology choices with rationale
3. Fit with existing code — consistency with patterns found in the impact analysis
4. Cross-document consistency — screens reference existing APIs, ER supports the API payloads
5. NFR coverage — non-functional requirements addressed by the architecture
6. Unresolved decisions — architectural choices left ambiguous that block Phase 3

Severity definitions (design documents):
{設計ドキュメント用の重要度定義を貼り付け}

Report findings in tiered format with file and section. Report in Japanese.
""")
```

security-reviewer を追加起動する場合は同じ対象パスで、認証・認可設計 / データ保護 / 脅威と対策の対応 / 監査ログを観点にする。

### Phase 3: 詳細設計レビュー（spec-reviewer）

```
Task(subagent_type="spec-reviewer", prompt="""
You are REVIEWING detailed-design DESIGN DOCUMENTS against the basic design.
This is NOT implementation-vs-spec review — compare DETAILED design vs BASIC design, and judge implementability.

Documents to review:
- docs/api/API-*.md, docs/screens/SC-*.md, docs/database/TBL-*.md（該当分）
{拡張トラック成果物があれば追加}

Context (upstream):
- docs/api/api-list.md, docs/screens/screen-list.md, docs/database/er-design.md
- docs/requirements/feature-list.md

Review perspectives:
1. Coverage — every item in the basic-design lists has a corresponding detailed document
2. Consistency — no contradictions with basic design (IDs, paths, data types)
3. Implementability — inputs/outputs, error responses, validation are specified; an implementer can start WITHOUT guessing
4. Completeness — error cases and edge cases described, not only happy paths

Severity definitions (design documents):
{設計ドキュメント用の重要度定義を貼り付け}

Report findings in tiered format with file and section. Report in Japanese.
""")
```
