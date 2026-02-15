---
name: review
description: |
  Run code reviews using specialized reviewer agents.
  Supports individual or batch review modes.
metadata:
  short-description: Multi-agent code review
---

# Review Skill

**複数の専門レビュアーによるコードレビューを実行する。**

## Usage

```
/review              # 全レビュアー（6種）を並列実行
/review code         # コードレビューのみ
/review security     # セキュリティレビューのみ
/review performance  # パフォーマンスレビューのみ
/review spec         # 仕様整合性レビューのみ
/review architecture # アーキテクチャレビューのみ
/review ux           # UX/アクセシビリティレビューのみ
/review design       # 設計系レビュー（spec + architecture）
/review impl         # 実装系レビュー（code + security + performance）
```

## Reviewers

| Reviewer | Focus |
|----------|-------|
| `code-reviewer` | 可読性、保守性、バグ検出 |
| `security-reviewer` | 脆弱性、権限、情報漏洩 |
| `performance-reviewer` | 計算量、I/O、最適化 |
| `spec-reviewer` | 設計書との整合性 |
| `architecture-reviewer` | アーキテクチャ妥当性 |
| `ux-reviewer` | UX、アクセシビリティ |

## Execution

### Full Review (No Arguments)

When `/review` is called without arguments:

1. **Identify target**: Ask user or use recent changes (`git diff`)
2. **Launch all reviewers in parallel**:

```
Task(subagent_type="code-reviewer", run_in_background=true, prompt="Review: {target}")
Task(subagent_type="security-reviewer", run_in_background=true, prompt="Review: {target}")
Task(subagent_type="performance-reviewer", run_in_background=true, prompt="Review: {target}")
Task(subagent_type="spec-reviewer", run_in_background=true, prompt="Review: {target}")
Task(subagent_type="architecture-reviewer", run_in_background=true, prompt="Review: {target}")
Task(subagent_type="ux-reviewer", run_in_background=true, prompt="Review: {target}")
```

3. **Aggregate results**: Combine findings by severity
4. **Present summary**: Show consolidated review report

### Individual Review

When `/review {type}` is called:

1. Launch specific reviewer
2. Return detailed findings

## Output Format

```markdown
## Review Summary: {target}

### Critical Issues ({count})
{Aggregated critical findings from all reviewers}

### Important Issues ({count})
{Aggregated important findings}

### Minor Issues ({count})
{Aggregated minor findings}

### By Reviewer

#### Code Review
{Summary from code-reviewer}

#### Security Review
{Summary from security-reviewer}

#### Performance Review
{Summary from performance-reviewer}

#### Spec Compliance
{Summary from spec-reviewer}

#### Architecture
{Summary from architecture-reviewer}

#### UX/Accessibility
{Summary from ux-reviewer}

### Recommendations
1. {Priority 1}
2. {Priority 2}
3. {Priority 3}
```

## Tips

- Use `/review impl` for quick implementation review
- Use `/review design` before major refactoring
- Full `/review` is recommended before releases
- Specify files/features for focused review
