# Tiered Review Output Contract

**レビュー系スキルの段階別出力形式。**

## フォーマット

```markdown
## Review Summary

**レビュアー**: {選定されたレビュアー一覧}
**変更ファイル**: {ファイル数} files, {追加行数} insertions(+), {削除行数} deletions(-)

### Critical ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 影響 + 修正案}
  ```{lang}
  {コードスニペット}
  ```

### High ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**
  {問題の説明 + 修正案}

### Medium ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}

### Low ({count})
- [{reviewer}] `{file}:{line}` - {1行サマリ}
```

## Refuted Findings セクション（指摘検証フェーズを実施した場合のみ）

指摘検証フェーズ（例: `/review` の Phase 3.5）を実施したスキルでは、上記フォーマット末尾に以下を追加する:

```markdown
### Refuted Findings ({count})
- [{reviewer}] `{file}:{line}` - **{Issue}**（元 severity: {Critical|High}）
  {反証理由（finding-verifier の verdict 根拠）}
```

- `verdict: refuted` となった指摘を、反証理由を添えて掲載する（除外の透明性確保のため）
- severity 格下げ（`effective_severity`）が適用された指摘は、格下げ後の severity セクションに掲載し「元 severity: {original} → 検証後: {effective}」を付記する
- 指摘検証フェーズを持たないスキル、検証を無効化した場合（`verify_findings: false`）、または該当指摘がない場合はこのセクションを出力しない

## 重要度の定義

| 重要度 | 基準 | 対応 |
|--------|------|------|
| **Critical** | セキュリティ脆弱性、データ損失リスク、本番障害の可能性 | 必ず修正してから次に進む |
| **High** | バグの可能性、設計上の問題、パフォーマンス劣化 | ユーザーに確認（AskUserQuestion） |
| **Medium** | コード品質、可読性、軽微な改善 | 報告のみ。修正は任意 |
| **Low** | スタイル、命名、コメント改善 | 報告のみ。修正は任意 |

## 集約ルール

### 重複指摘の統合

複数レビュアーが同一ファイル・同一箇所を指摘した場合:

- severity が最も高いものを採用する
- 他のレビュアー名を `[{reviewer1}, {reviewer2}]` で併記する
- 異なる観点の指摘（例: security と performance）は別エントリとして残す

### 詳細度

- **Critical / High**: 詳細な説明 + 影響範囲 + 修正案（コードスニペット付き）
- **Medium / Low**: 1行サマリのみ
