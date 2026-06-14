# Antigravity Delegation Rule

**Antigravity CLI (`agy`) は大規模リサーチを担当する専門家。**

> **Note**: モデル名・オプションは `.claude/config/agent-routing/cli-tools.yaml` で一元管理。
> `.claude/config/agent-routing/cli-tools.local.yaml` が存在する場合はそちらの値を優先する（詳細は `config-loading.md` 参照）。
> 以下のコマンド例中の `<antigravity.model>` 等のプレースホルダーは、config ファイルの値で置換して使用する。

## 判定手順（MUST）

1. `.claude/config/agent-routing/cli-tools.yaml` を読み込む
2. `.claude/config/agent-routing/cli-tools.local.yaml` があれば上書きを適用する
3. `antigravity.enabled` を確認する
4. 対象エージェントの `agents.<name>.tool` で実行先を決定する
5. `tool == antigravity` のときだけ Antigravity CLI を呼び出す

## いつ Antigravity を使うか

- ルーティング解決結果が `tool: antigravity` のとき
- `tool: auto` で調査・外部情報取得が必要なとき

### `tool: auto` のトリガー目安

| 場面             | トリガー（日本語）              | トリガー（英語）                     |
| ---------------- | ------------------------------- | ------------------------------------ |
| リサーチ         | 「調べて」「リサーチ」「調査」  | "research", "investigate", "look up" |
| ドキュメント     | 「ドキュメント」「最新」「API」 | "documentation", "latest", "API"     |
| コードベース分析 | 「全体を理解」「構造」          | "entire codebase", "structure"       |

## Non-Interactive 実行

`agy -p` モードは stdin を待たないため、`< /dev/null` は不要。
以下を必ず守ること。

### 基本ルール

1. **`< /dev/null` は不要**: `agy -p` は非対話完結。stdin を封じる必要はない
2. **タイムアウトを設定**: Bash の timeout パラメータに `300000`（5分、`agy` の `--print-timeout` デフォルトと同じ）を推奨
3. **プロンプトに「質問するな」指示を含める**:
   プロンプト末尾に以下を追加:
   `"IMPORTANT: Do not ask any clarifying questions. Provide your best answer based on the available information. If you need assumptions, state them."`
4. **model allowlist チェック**: `antigravity.model` が `antigravity.model_allowlist` に含まれない場合は実行前に `[WARN] model '<value>' not in allowlist` を出力すること（agy は無効な slug でも exit 0 でデフォルトモデルに黙ってフォールバックするため）

### コマンドパターン

```bash
# 基本リサーチ（< /dev/null 不要）
agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions. Provide your best answer
based on the available information." --model <antigravity.model> 2>/dev/null

# コードベース分析（--add-dir でディレクトリを追加）
agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null
```

> **マルチモーダル（ファイル stdin）について**: `agy` では stdin からのファイル渡しは非サポート扱い。
> ファイルを参照させる場合は `--add-dir <ディレクトリ>` でファイルのあるディレクトリを追加すること。

### リトライプロトコル

Antigravity がタイムアウトした場合、または出力に質問が含まれている場合にリトライする。

#### タイムアウト/エラーの検出

- **exit code が非ゼロ**: タイムアウトまたはエラーとみなす
- **出力が空（0バイト）**: タイムアウトとみなす
- `2>/dev/null` で stderr を破棄しているため、**exit code で判定する**ことを前提とする

#### 質問検出の判定基準

出力に以下のパターンが含まれる場合、Antigravity が質問を返したとみなす:

- `?` で終わる文
- "Could you clarify", "Which", "Please specify", "Can you provide", "Do you want" 等の質問フレーズ

#### リトライ手順

1. 出力を確認し、Antigravity の質問内容を抽出する
2. サブエージェント自身のコンテキストから質問への回答を組み立てる
3. 元のプロンプト + 質問への回答を含めた新プロンプトで再実行する:

```bash
agy -p "
[Original request]: {元のプロンプト}

[Additional context]: Previously, you asked: '{Antigravity の質問}'
The answer is: {回答}

Please proceed with the analysis without asking further questions.
IMPORTANT: Do not ask any clarifying questions.
" --model <antigravity.model> 2>/dev/null
```

4. 最大リトライ回数: **2回**（3回目のタイムアウトで失敗として報告）

---

## 呼び出し方法

> **Bash サンドボックスの制約**
> Antigravity CLI は認証 + macOS システム API を使用するため、sandbox 内では動作しない場合がある。
> ただし `sandbox.excludedCommands` に `agy` が設定済みなら sandbox 内でも実行可能。

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Antigravity でリサーチしてください：

{リサーチ内容}

agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions. Provide your best answer
based on the available information." --model <antigravity.model> 2>/dev/null

タイムアウト: Bash timeout パラメータに 300000 を指定すること。
リトライ: タイムアウトや質問検出時は上記「リトライプロトコル」に従う。
sandbox エラーや権限拒否が発生した場合は claude-direct にフォールバックすること。

結果を .claude/docs/research/{topic}.md に保存し、
要約を返してください（5-7ポイント）。
""")
```

### 直接呼び出し（短い質問のみ）

```bash
# config の antigravity.model を --model フラグに展開して使う

# リサーチ
agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null

# コードベース分析
agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null
```

## Antigravity の強み

| 機能                           | 説明                                                     |
| ------------------------------ | -------------------------------------------------------- |
| 大規模コンテキスト             | コードベース全体を一度に分析可能                         |
| Google Search グラウンディング | 最新情報へのアクセス                                     |
| 複数モデル                     | Gemini 3.5 Flash / 3.1 Pro / Claude 4.6 / GPT-OSS の切替 |

## 無効化

`.claude/config/agent-routing/cli-tools.yaml`（または `.local.yaml`）で `antigravity.enabled: false` を設定すると、Antigravity CLI の呼び出しが全て無効化される。
無効時は Antigravity を使用するエージェントが自動的に `claude-direct`（Claude Code 自身の能力）にフォールバックする。

```yaml
# .claude/config/agent-routing/cli-tools.local.yaml
antigravity:
  enabled: false
```

> **後方互換**: `.local.yaml` に旧 `gemini.enabled: false` が残っている場合も有効として尊重される。

## `tool: auto` 時の使い分け目安

| タスク           | 推奨                          |
| ---------------- | ----------------------------- |
| 設計判断         | Codex 候補                    |
| デバッグ         | Codex 候補                    |
| コード実装       | `agents.<target>.tool` で解決 |
| ライブラリ調査   | Antigravity 候補              |
| コードベース理解 | Antigravity 候補              |
| ドキュメント検索 | Antigravity 候補              |

---

## 旧 gemini 設定からの移行

`cli-tools.yaml` の `gemini:` キーは `antigravity:` に統合された。移行時の読み替えエイリアスは以下の通り。

| 旧形式                               | 新形式                                   | 挙動                                                                    |
| ------------------------------------ | ---------------------------------------- | ----------------------------------------------------------------------- |
| トップレベル `gemini.enabled: false` | `antigravity.enabled: false` と等価      | enabled 値のみ反映される                                                |
| `agents.<name>.tool: gemini`         | `agents.<name>.tool: antigravity` と同義 | 自動読み替え（antigravity として処理）                                  |
| `gemini.model`                       | **引き継がない**                         | Gemini CLI 固有値のため無視。`antigravity.model` を明示的に設定すること |
