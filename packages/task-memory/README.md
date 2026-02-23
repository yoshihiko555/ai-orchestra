# task-memory

`Plans.md` を SSOT (Single Source of Truth) として扱い、セッション開始時にタスク状態を自動サマリー表示するパッケージです。

## できること

- SessionStart で `.claude/Plans.md` を読み取り
- 状態ごとの件数とタスク一覧をサマリー表示
- 優先順で表示:
  - `WIP`
  - `TODO`
  - `blocked`

## クイックスタート

1. Plans を初期化する

```bash
/task-state init
```

2. タスクを更新する

```bash
/task-state update "API 実装" --status wip
/task-state update "決済連携" --status blocked --reason "外部API仕様待ち"
/task-state update "API 実装" --status done
```

3. セッション開始時に自動サマリーを確認する  
`show_summary_on_start: true` のとき、SessionStart でサマリーが出力されます。

## コマンド一覧 (`task-state` スキル)

```bash
/task-state
/task-state init
/task-state update "タスク名" --status todo|wip|done|blocked [--reason "..."]
/task-state add-phase "Phase 2: 実装" --tasks "タスク1" "タスク2"
/task-state decision "YYYY-MM-DD: 設計判断"
```

## Plans.md の最小例

```markdown
# Plans

## Project: my-app

### Phase 1: 実装 `cc:WIP`

- `cc:WIP` 商品一覧 API
- `cc:TODO` 注文 API
- `cc:blocked` 決済 API — 理由: 外部決済サービス契約待ち
```

## 設定

デフォルト設定は `packages/task-memory/config/task-memory.yaml` です。  
プロジェクト側で上書きする場合は以下を作成します。

- `.claude/config/task-memory/task-memory.yaml`
- 必要なら `.claude/config/task-memory/task-memory.local.yaml`

主な設定:

```yaml
plans_file: ".claude/Plans.md"
show_summary_on_start: true
max_display_tasks: 20  # サマリー表示の合計上限。0 は無制限
markers:
  todo: "cc:TODO"
  wip: "cc:WIP"
  done: "cc:done"
  blocked: "cc:blocked"
```

## 表示ルール

- `max_display_tasks` は「全セクション合計の表示上限」です
- `0` は無制限表示です
- 表示は `WIP -> TODO -> blocked` の順で上限を消費します
- `blocked` が上限で1件も表示できない場合は、`Blocked: (上限のため N 件省略)` を表示します

## marker カスタマイズの注意

- `markers` の値は重複不可です
- 重複がある場合は stderr に警告を出し、デフォルト marker にフォールバックします
- 例:
  - `todo` と `wip` を同じ値にしない

## 注意点

- タスク行は `- \`marker\` タスク名` 形式で記述してください
- マーカー後のタスク名が空の行は集計対象外です
- `blocked` 理由は `— 理由: ...` 形式で記述するとサマリーに表示されます

## テスト

```bash
pytest -q packages/task-memory/tests
```
