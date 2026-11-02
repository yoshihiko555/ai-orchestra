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
2. **長時間実行に備える**: 調査は 10〜20 分かかることがある。サブエージェントからの調査・コードベース分析は
   下記「長時間実行と待機」の手順で実行し、`--print-timeout 30m` を付ける（`agy` 1.4 系の `--print-timeout` の
   既定は `0s` = 無制限）
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

### 長時間実行と待機（MUST）

Claude Code は Bash コマンドが `timeout` に達すると、止めずにバックグラウンドへ移して
「Command was moved to the background」と返す。バックグラウンドで起動したサブエージェントでは、`timeout` の
指定に関係なく約 180 秒で移される（Claude Code 2.1.316 で観測。秒数は Claude Code 側で決まり、設定では
変えられない）。`agy` の調査は 10〜20 分かかることがあるため、移されるのは通常の動きである。

- **バックグラウンドへ移されたことは失敗ではない。** リトライも claude-direct へのフォールバックもせず、
  完了を待ってから出力を読む
- **`agy` の結果を判定するまで報告を返さない。** サブエージェントはターンを終えると、その時点の内容が報告として
  呼び出し元に返る。待つ間に WebFetch 等で一次情報を確かめるのは構わない

手順:

1. 出力先のディレクトリを作る（sandbox 内のまま）。シェル変数は Bash 呼び出しをまたいで残らないため、
   表示された絶対パスを以降のコマンドに文字列で書く。**`agy` を実行するたびに（sandbox エラー後の再実行・
   リトライを含む）新しく作る。** 使い回すと前回の `exit` が残り、実行中の `agy` を終わったと誤認する。
   バックグラウンドで動いている `agy` があるうちは、同じ調査をもう一度起動しない:

   ```bash
   mktemp -d "${TMPDIR:-/tmp}/agy.XXXXXX"
   ```

2. `agy` を実行する。Bash の `timeout` は `600000` にし、`run_in_background` は使わない。stdout と stderr を
   ファイルに受け、終了コードを書き出す:

   ```bash
   agy -p "{質問}

   IMPORTANT: Do not ask any clarifying questions. Provide your best answer
   based on the available information." --model <antigravity.model> --print-timeout 30m \
     > '<dir>/out.md' 2> '<dir>/err.log'; echo $? > '<dir>/exit'
   ```

3. 結果が「moved to the background」なら、次のコマンドを数字が表示されるまで繰り返す（1 回 150 秒で終わる。
   Bash の `timeout` は `170000`）。単独の `sleep` コマンドはブロックされるので使わない:

   ```bash
   for i in $(seq 30); do [ -s '<dir>/exit' ] && break; sleep 5; done; cat '<dir>/exit' 2>/dev/null || echo running
   ```

   `--print-timeout 30m` により `agy` は 30 分で止まる。35 分たっても `running` のままなら失敗として扱う。

4. 終了コード・`err.log`・`out.md` を下記「タイムアウト/エラーの検出」で判定してから `out.md` を読む

短い質問を直接呼び出す場合も、バックグラウンドへ移されたら同じく完了を待つ（メインセッションでは完了通知が届く）。

### リトライプロトコル

`agy` がエラー終了した場合、出力が空の場合、または出力に質問が含まれている場合にリトライする。
タイムアウト（`--print-timeout`）はリトライしない。

#### タイムアウト/エラーの検出

- **exit code が非ゼロ**: エラーとみなす
- **`err.log` に `print timeout` を含む**: タイムアウトとみなす。`agy` は exit 0 のまま途中までの出力を返し、
  タイムアウトは stderr にしか出ないため、exit code だけでは判定できない
- **出力が空（0バイト）**: 失敗とみなす
- タイムアウト時はリトライしない（30 分かかった調査を繰り返さない）。`out.md` に途中までの内容があれば使い、
  足りない部分は claude-direct で補って、その旨を報告に書く

#### 質問検出の判定基準

出力に以下のパターンが含まれる場合、Antigravity が質問を返したとみなす:

- `?` で終わる文
- "Could you clarify", "Which", "Please specify", "Can you provide", "Do you want" 等の質問フレーズ

#### リトライ手順

1. 出力を確認し、Antigravity の質問内容を抽出する
2. サブエージェント自身のコンテキストから質問への回答を組み立てる
3. 元のプロンプト + 質問への回答を含めた新プロンプトで再実行する（新しい出力先を作り、「長時間実行と待機」の
   手順で実行・待機する）:

```bash
agy -p "
[Original request]: {元のプロンプト}

[Additional context]: Previously, you asked: '{Antigravity の質問}'
The answer is: {回答}

Please proceed with the analysis without asking further questions.
IMPORTANT: Do not ask any clarifying questions.
" --model <antigravity.model> --print-timeout 30m \
  > '<dir>/out.md' 2> '<dir>/err.log'; echo $? > '<dir>/exit'
```

4. 最大リトライ回数: **2回**（3回目の失敗で失敗として報告）

---

## 呼び出し方法

> **Bash サンドボックスの制約**
> `agy` は sandbox 内で実行する（`sandbox.excludedCommands` に `agy` を入れても、現行の Claude Code では
> sandbox の外に出ない）。認証と推論の通信先（`daily-cloudcode-pa.googleapis.com` / `www.googleapis.com` /
> `antigravity-unleash.goog` / `lh3.googleusercontent.com`）が sandbox のネットワーク許可に無いと、
> `Eligibility check failed: ... Forbidden` で exit 1 になる。その場合は通信先を許可して再実行するか、
> claude-direct にフォールバックする。
> `excludedCommands` に `agy` があると、呼び出しに `allowed_domains` を付けたとき「sandbox の外で動くコマンド」
> として拒否されることがある。その場合は `allowed_domains` を外して再実行し、それが `Eligibility check failed` で
> 失敗したら（sandbox 内で動いた）、`allowed_domains` を付けて再実行する。

### サブエージェント経由（推奨）

大きな出力が予想される場合、コンテキスト保護のためサブエージェント経由で呼び出す：

```
Task(subagent_type="general-purpose", prompt="""
Antigravity でリサーチしてください：

{リサーチ内容}

agy -p "{質問}

IMPORTANT: Do not ask any clarifying questions. Provide your best answer
based on the available information." --model <antigravity.model> --print-timeout 30m

実行と待機: .claude/rules/antigravity-delegation.md の「長時間実行と待機」の手順に従う
（出力はファイルに受ける）。Bash がバックグラウンドへ移しても失敗ではない。完了を待って出力を読み、
agy の結果を判定するまで報告を返さない。
リトライ: エラー終了・空出力・質問検出時は上記「リトライプロトコル」に従う。
sandbox エラーや権限拒否が発生した場合は claude-direct にフォールバックすること。

結果を .claude/docs/research/{topic}.md に保存し、
要点を絞った要約を返してください。
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

| 機能                           | 説明                                                   |
| ------------------------------ | ------------------------------------------------------ |
| Google Search グラウンディング | 最新情報へのアクセス                                   |
| 複数モデル                     | `antigravity.model_allowlist` に登録されたモデルを切替 |

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
