# update-orchestra

<update-orchestra>

プロジェクトの AI Orchestra 設定ファイルを最新テンプレートに同期します。
プロジェクト固有データ（CLAUDE.md, research/, logs/ 等）は一切変更しません。

## 実行手順

### Step 1: 前提チェック

以下を確認:
1. `.claude/settings.json` が存在するか
   - 存在しない場合 → **「`/init-orchestra` を先に実行してください」** と伝えて終了
2. `~/.claude/templates/` ディレクトリが存在するか
   - 存在しない場合 → **「テンプレートディレクトリが見つかりません。ai-orchestra のセットアップを確認してください」** と伝えて終了

### Step 2: バックアップ作成

`.claude/backup/{YYYYMMDD-HHMMSS}/` を作成し、以下をコピー:

```
.claude/backup/{timestamp}/
├── settings.json                    ← .claude/settings.json
├── codex/                           ← .codex/ 配下全体
├── gemini/                          ← .gemini/ 配下全体
└── docs/libraries/_TEMPLATE.md      ← .claude/docs/libraries/_TEMPLATE.md（存在する場合）
```

バックアップの作成を報告する。

### Step 3: `.claude/settings.json` のスマートマージ

テンプレート: `~/.claude/templates/project-settings.json`

#### hooks のマージロジック

**フェーズ（UserPromptSubmit / PreToolUse / PostToolUse）ごとに処理する。**

各フェーズ内のフックエントリは `matcher` で分類される。同じ `matcher` を持つエントリ内の個別フックは `command` フィールドのパスで識別する。

1. **テンプレートにあるがプロジェクトにないフック** → 追加
2. **両方にあるフック**（`command` パスが一致）→ テンプレートの `timeout` 値に更新
3. **プロジェクトにあるがテンプレートにないフック** → そのまま保持（プロジェクト固有）

**具体的な処理手順:**

```
テンプレートのフェーズごとに:
  テンプレートの各エントリ（matcher 単位）に対して:
    プロジェクトに同じ matcher のエントリがあるか？
      ある場合:
        テンプレートの各 hook.command に対して:
          プロジェクトに同じ command があるか？
            ある → timeout をテンプレートの値に更新
            ない → プロジェクトの hooks 配列にこの hook を追加
        プロジェクトにあるがテンプレートにない hook → 保持
      ない場合:
        エントリごと追加

  プロジェクトにあるがテンプレートにないエントリ（matcher が一致しない） → 保持
```

#### permissions のマージロジック

- `allow`: テンプレートの値 ∪ プロジェクトの値（和集合、重複排除）
- `deny`: プロジェクトのものをそのまま保持（テンプレートは空配列のため）

#### マージ結果の報告

マージ完了後、以下を報告:
- 追加したフック数
- timeout を更新したフック数
- 追加した permissions 数
- 保持したプロジェクト固有フック数

### Step 4: `.codex/` の更新（差分確認付き）

テンプレート: `~/.claude/templates/codex/`

対象ファイル:
- `config.toml`
- `AGENTS.md`
- `skills/context-loader/SKILL.md`

**各ファイルについて:**

1. テンプレートとプロジェクトの内容を比較
2. **差分がない場合** → 「変更なし」として記録、スキップ
3. **差分がある場合**:
   - 差分の概要を表示（どの部分が変わるか）
   - `AskUserQuestion` で「更新する / スキップする」をユーザーに確認
   - 「更新する」→ テンプレートの内容で上書き
   - 「スキップする」→ そのまま保持
4. **プロジェクトにファイルが存在しない場合** → テンプレートからコピー（新規追加として報告）

### Step 5: `.gemini/` の更新（差分確認付き）

テンプレート: `~/.claude/templates/gemini/`

対象ファイル:
- `settings.json`
- `GEMINI.md`
- `skills/context-loader/SKILL.md`

Step 4 と同じ方式で処理する。

### Step 6: `.claude/docs/libraries/_TEMPLATE.md` の更新

テンプレート: `~/.claude/templates/project/docs/libraries/_TEMPLATE.md`

- テンプレートと比較して差分があれば更新（確認不要 — テンプレートファイルのため）
- 差分がなければ「変更なし」として記録
- ファイルが存在しない場合はテンプレートからコピー

### Step 7: 更新レポート

以下の形式で最終レポートを出力:

```
## Orchestra 更新完了

### バックアップ
.claude/backup/{timestamp}/

### 更新したファイル
- .claude/settings.json（新規フック N 件追加、timeout 更新 N 件）
- .codex/AGENTS.md
- .gemini/GEMINI.md
- .claude/docs/libraries/_TEMPLATE.md

### スキップしたファイル（ユーザー選択）
- .codex/config.toml（プロジェクト固有設定を維持）

### 変更なし
- .gemini/settings.json（テンプレートと同一）

### 触れていないファイル（安全）
- CLAUDE.md, .claude/docs/DESIGN.md, .claude/docs/research/*
- .claude/docs/libraries/*.md（_TEMPLATE.md 以外）
- .claude/logs/*, .claude/checkpoints/*
- .claude/settings.local.json
```

---

## 絶対に触らないファイル

| ファイル | 理由 |
|---------|------|
| `CLAUDE.md` | プロジェクト固有の指示書 |
| `.claude/docs/DESIGN.md` | プロジェクトの設計決定記録 |
| `.claude/docs/research/*` | Gemini のリサーチ出力 |
| `.claude/docs/libraries/*.md`（`_TEMPLATE.md` 以外） | ライブラリ固有のドキュメント |
| `.claude/logs/*` | CLI ツールログ |
| `.claude/checkpoints/*` | セッションチェックポイント |
| `.claude/settings.local.json` | プロジェクト固有のオーバーライド |

## 重要な注意事項

- バックアップは毎回作成する（上書きしない）
- settings.json のマージは必ずスマートマージを使い、単純な上書きはしない
- .codex/ と .gemini/ のファイルはユーザー確認なしに更新しない
- テンプレートファイル（`_TEMPLATE.md`）のみ確認なしで更新可能
- エラーが発生した場合はバックアップからの復元手順を案内する

</update-orchestra>
