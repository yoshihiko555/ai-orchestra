---
name: plan
description: |
  Create a preflight implementation plan through interactive dialogue with the user.
  Clarifies scope, risks, and task breakdown, then reflects them in Plans.md.
  Use this skill when the user wants to plan before coding, discuss requirements,
  or prepare an implementation roadmap. Trigger on: "計画", "プラン", "設計相談",
  "何を作るか整理", "要件整理", or any pre-implementation planning request.
disable-model-invocation: true
---

# Plan (Preflight)

`/plan` は **実装前のプリフライト** です。
`/startproject` の前段で「何を作るか / どこまで作るか / 何が危険か」を確定します。

> **重要: このスキルは対話型ワークフローです。**
> `EnterPlanMode` ツールを絶対に使わないでください。
> Plan mode に切り替えるのではなく、通常モードのまま AskUserQuestion で対話を進めます。

## 役割

1. 要件とスコープの確定
2. リスクと未確定事項の可視化
3. 実装ステップへの分解
4. `Plans.md`（SSOT）への反映

## いつ使うか

### 必須（中〜大規模）

- 新機能追加
- 複数ファイルにまたがる変更
- 設計変更や依存追加を含む変更

### 省略可（小規模）

- typo 修正
- コメント修正のみ
- 明確な 1 ファイル軽微変更

## プロセス

このスキルは **4 つのフェーズを順番に** 進める。
各フェーズの終わりで AskUserQuestion を使いユーザーの確認を取ってから次に進む。
EnterPlanMode は使わず、通常の会話として進行すること。

### Phase 1: Requirements（対話）

AskUserQuestion を活用してユーザーから以下を聞き出す:

- 目的（何を達成するか）
- スコープ in/out
- 受け入れ条件
- 制約（技術、時間、依存）

不明点があれば追加質問する。全ての要件が明確になるまでこのフェーズに留まる。

**フェーズ完了条件**: ユーザーが要件に合意した

### Phase 2: Current State（調査）

サブエージェントやツールを使い、関連するコードベースを調査する:

- 既存実装と関連ファイル
- 既存テストと不足テスト
- 既存ルール/パターン
- 必要に応じて `cli-tools.yaml` の `agents.<target>.tool` を確認し、`tool: auto` なら調査は Gemini 候補、設計判断は Codex 候補を使う

調査結果をユーザーに要約して報告し、認識齟齬がないか確認する。

**フェーズ完了条件**: ユーザーが現状認識に合意した

### Phase 3: Plan Breakdown（分解・提示）

調査結果をもとに実装計画を組み立てる:

- 小さく検証可能なステップへ分解
- 依存順序を明示
- 高リスク項目を先に実施
- リスクと軽減策を列挙

以下の出力フォーマットでユーザーに提示し、フィードバックを受ける。

**フェーズ完了条件**: ユーザーが実装計画に合意した

### Phase 4: Plans.md 反映

ユーザーの合意を得た計画を `Plans.md` に反映する:

- まず既存の `.claude/Plans.md` を確認する
- 未作成なら `/task-state init` で新規作成
- 既存の場合は既存フェーズ・タスクを保持したまま追記する
- フェーズ追加が必要なら `/task-state add-phase ...`
- 主要タスクを `cc:TODO` で登録

反映完了後、Plans.md の内容をユーザーに報告する。

### Exit 条件

- Open Questions が 0、または保留理由付きで明示
- 実装ステップに抜けがない
- `Plans.md` が次フェーズで実行可能な状態

## 出力フォーマット（Phase 3 で使用）

```markdown
## Preflight Plan: {Title}

### Goal
{1-2 sentences}

### Scope
- In: {list}
- Out: {list}

### Acceptance Criteria
- {criteria}

### Implementation Steps
1. [ ] {step}
2. [ ] {step}

### Risks
- {risk}: {mitigation}

### Open Questions
- {question or "none"}

### Plans.md Updates
- {phase/task entries to add or update}
```

## Notes

- `/plan` は **実装を行わない**（プリフライト専用）
- 実装本線は `/startproject` で行う
- **EnterPlanMode / ExitPlanMode ツールは使用禁止** — 通常モードで対話を続ける
