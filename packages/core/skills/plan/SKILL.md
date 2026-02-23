---
name: plan
description: |
  Create a preflight implementation plan before coding.
  Clarifies scope, risks, and task breakdown, then reflects them in Plans.md.
disable-model-invocation: true
---

# Plan (Preflight)

`/plan` は **実装前のプリフライト** です。
`/startproject` の前段で「何を作るか / どこまで作るか / 何が危険か」を確定します。

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

### 1. Requirements

- 目的（何を達成するか）
- スコープ in/out
- 受け入れ条件
- 制約（技術、時間、依存）

### 2. Current State

- 既存実装と関連ファイル
- 既存テストと不足テスト
- 既存ルール/パターン

### 3. Plan Breakdown

- 小さく検証可能なステップへ分解
- 依存順序を明示
- 高リスク項目を先に実施

### 4. Plans.md 反映

`Plans.md` をタスクの SSOT として更新する。

- 未作成なら `/task-state init`
- フェーズ追加が必要なら `/task-state add-phase ...`
- 主要タスクを `cc:TODO` で登録

### 5. Exit 条件

- Open Questions が 0、または保留理由付きで明示
- 実装ステップに抜けがない
- `Plans.md` が次フェーズで実行可能な状態

## 出力フォーマット

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
- 必要に応じて設計相談は Codex、調査は Gemini を利用
