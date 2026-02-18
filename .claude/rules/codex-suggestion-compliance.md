# Codex Suggestion Compliance

**PreToolUse hook が `[Codex Suggestion]` を出力した場合、オーケストレーターは必ず従うこと。**

## トリガー

`check-codex-before-write.py`（PreToolUse: Edit|Write）が以下の条件で `[Codex Suggestion]` を出力する:

- `core/` を含むファイルパスへの変更
- `config` や `class` 等のキーワードを含む変更内容
- 大きなコンテンツを含む新規ファイル作成

## 遵守手順

1. **ファイル編集を一旦保留する**
2. **サブエージェント経由で Codex に相談する**:
   ```
   Task(subagent_type="general-purpose", prompt="""
   Codex に以下の変更について相談してください:
   - ファイル: {対象ファイル}
   - 変更内容: {変更の概要}

   codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "..." 2>/dev/null

   結果を要約して返してください。
   """)
   ```
3. **Codex の回答を踏まえて、変更を実行または修正する**

## 例外

以下の場合は Codex 相談をスキップしてよい：

- **実際の typo 修正（1-2文字）またはコメントの追加・修正のみ** に限定される軽微な変更
  - **対象**: スペルミス修正、コメント文言の微修正、フェーズ番号等の参照値更新
  - **非対象**: ロジック変更、新規関数/クラス/セクション追加、ファイル構造の変更、import の追加・変更
- **ユーザーが明示的にスキップを指示** した場合
- **同一セッション内で同じファイルについて既に Codex 相談済み** の場合
- **`cli-tools.yaml` で `codex.enabled: false`** に設定されている場合（hook 自体が提案を抑制する）

## 累積的変更の原則

個々の変更が軽微に見えても、以下の場合は Codex（または implementation agent）に委譲すべき：

- **同一タスクで 3 箇所以上の変更**が予想される場合
- **スキル実行中**（`startproject`, `issue-fix` 等）の実装フェーズでは、原則として implementation agents 経由で実装する
- 「軽微な変更」例外の適用は **単発の修正** に限り、連続的な実装作業には適用しない
