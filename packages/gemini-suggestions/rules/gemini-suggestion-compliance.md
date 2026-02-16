# Gemini Suggestion Compliance

**PreToolUse hook が `[Gemini Suggestion]` を出力した場合、オーケストレーターは必ず従うこと。**

## トリガー

`suggest-gemini-research.py`（PreToolUse: WebSearch|WebFetch）が `[Gemini Suggestion]` を出力する。

## 遵守手順

1. **現在の操作（WebSearch/WebFetch 等）を一旦保留する**
2. **サブエージェント経由で Gemini にリサーチを依頼する**:
   ```
   Task(subagent_type="general-purpose", prompt="""
   Gemini でリサーチしてください:
   {リサーチ内容}

   gemini -m <gemini.model> -p "..." 2>/dev/null

   結果を要約して返してください。
   """)
   ```
3. **Gemini の結果を踏まえて操作を続行する**

## 例外

以下の場合は Gemini 相談をスキップしてよい：

- **ユーザーが明示的にスキップを指示** した場合
- **同一セッション内で同じトピックについて既に Gemini 相談済み** の場合
