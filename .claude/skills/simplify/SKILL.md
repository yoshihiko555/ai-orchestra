---
name: simplify
description: Simplify and refactor code while preserving functionality and library
  constraints.
disable-model-invocation: true
---

# Code Quality Policy

**コード品質のための共通ルール。スキル・ルールの両方から参照される。**

## シンプルさ優先

- 読みやすいコードを複雑なコードより選ぶ
- 過度な抽象化を避ける
- 「動く」より「理解できる」を優先

## 単一責任

- 1つの関数は1つのことだけ行う
- 1つのクラスは1つの責任だけ持つ
- ファイルは200-400行を目安に（最大800行）
- 関数は20行以下を目標にする

## Early Return

ネストを浅く保つために Early Return を使う:

```python
# Bad: 深いネスト
def process(value):
    if value is not None:
        if value > 0:
            return do_something(value)
    return None

# Good: Early return
def process(value):
    if value is None:
        return None
    if value <= 0:
        return None
    return do_something(value)
```

ネスト深度は **2以下** を目標とする。

## 型ヒント必須

すべての関数に型アノテーションを付ける:

```python
def call_llm(
    prompt: str,
    model: str = "gpt-4",
    max_tokens: int = 1000
) -> str:
    ...
```

## 不変性

既存オブジェクトを変更せず、新しいオブジェクトを作成:

```python
# Bad: 既存オブジェクトの変更
data["new_key"] = value

# Good: 新しいオブジェクトの作成
new_data = {**data, "new_key": value}
```

## 命名規則

- **変数/関数**: snake_case（英語）
- **クラス**: PascalCase（英語）
- **定数**: UPPER_SNAKE_CASE（英語）
- **意味のある名前**: `user_count` > `x`
- 名前だけで意図が伝わるようにする（コメント不要なレベル）

## マジックナンバー禁止

```python
# Bad
if retry_count > 3:
    ...

# Good
MAX_RETRIES = 3
if retry_count > MAX_RETRIES:
    ...
```

## セキュリティ

- APIキー・パスワードをハードコードしない
- 機密情報をログに出力しない
- `.env` ファイルをコミットしない
- 外部入力は必ずバリデーション

---

# Simplify Code

Simplify and refactor $ARGUMENTS.

## Steps

### 1. Analyze Target Code

- Read the file(s) to understand current structure
- Identify complexity hotspots (deep nesting, long functions)
- List functions/classes to simplify

### 2. Check Library Constraints

- Identify libraries used in target code
- Check constraints in `docs/libraries/`
- Web search for unclear library behaviors

### 3. Plan Refactoring

For each complexity issue:
- What change to make
- Why it improves readability
- Verify it doesn't break library usage

### 4. Execute Refactoring

Apply changes following these patterns:

**Early Return:**
```python
# Before
def process(value):
    if value is not None:
        if value > 0:
            return do_something(value)
    return None

# After
def process(value):
    if value is None:
        return None
    if value <= 0:
        return None
    return do_something(value)
```

**Extract Function:**
```python
# Before
def main():
    # 50 lines of mixed concerns
    ...

# After
def main():
    data = load_data()
    result = process_data(data)
    save_result(result)
```

### 5. Verify with Tests

```bash
uv run pytest -v
```

## Notes

- Always preserve library features/constraints
- Web search for unclear points
- Don't change behavior (refactoring only)
- Run tests after each significant change

### 運用補足（v2）

- `/review` で保守性課題が出た場合に実行候補として扱う
