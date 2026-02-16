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

- **typo/コメント追加** など、設計判断を伴わない軽微な変更
- **ユーザーが明示的にスキップを指示** した場合
- **同一セッション内で同じファイルについて既に Codex 相談済み** の場合
