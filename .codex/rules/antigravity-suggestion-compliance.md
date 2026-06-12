# Antigravity Suggestion Compliance

**PreToolUse hook が `[Antigravity Suggestion]` を出力した場合、オーケストレーターは必ず従うこと。**

## トリガー

`suggest-antigravity-research.py`（PreToolUse: WebSearch|WebFetch）が `[Antigravity Suggestion]` を出力する。

## 遵守手順

1. **現在の操作（WebSearch/WebFetch 等）を一旦保留する**
2. **サブエージェント経由で Antigravity にリサーチを依頼する**:

   ```
   Task(subagent_type="general-purpose", prompt="""
   Antigravity でリサーチしてください:
   {リサーチ内容}

   agy -p "..." --model <antigravity.model> 2>/dev/null

   結果を要約して返してください。
   """)
   ```

3. **Antigravity の結果を踏まえて操作を続行する**

## 例外

以下の場合は Antigravity 相談をスキップしてよい：

- **ユーザーが明示的にスキップを指示** した場合
- **同一セッション内で同じトピックについて既に Antigravity 相談済み** の場合
- **`cli-tools.yaml` で `antigravity.enabled: false`** に設定されている場合（hook 自体が提案を抑制する）
