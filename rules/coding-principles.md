# Coding Principles

コード品質のための共通ルール。

## シンプルさ優先

- 読みやすいコードを複雑なコードより選ぶ
- 過度な抽象化を避ける
- 「動く」より「理解できる」を優先

## 単一責任

- 1つの関数は1つのことだけ行う
- 1つのクラスは1つの責任だけ持つ
- ファイルは200-400行を目安に（最大800行）

## Early Return

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

## 型ヒント必須

すべての関数に型アノテーションを付ける：

```python
def call_llm(
    prompt: str,
    model: str = "gpt-4",
    max_tokens: int = 1000
) -> str:
    ...
```

## 不変性

既存オブジェクトを変更せず、新しいオブジェクトを作成：

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
