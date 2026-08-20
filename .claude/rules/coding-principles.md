# Code Quality Policy

**コード品質のための共通ルール。スキル・ルールの両方から参照される。**

## シンプルさ優先

- 読みやすいコードを複雑なコードより選ぶ
- 過度な抽象化を避ける
- 「動く」より「理解できる」を優先

### 明確な小規模変更の fast path

変更先と仕様が明示された小規模タスクでは、複合 shell より Read・Write・Edit などの単純な専用ツールを優先する。ツールの成功結果が書き込んだ内容を保証している場合、同じ内容を再読込・再検証しない。拒否された任意の追加検証を別コマンドで再試行せず、受け入れ条件に必要な最小の検証だけを行う。ただし、リポジトリや上位の指示で必須化された検証（テスト・lint 等）はこの省略の対象外であり、常に実行する。実行できない場合は省略せず理由を報告する。

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
