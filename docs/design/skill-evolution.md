---
codd:
  node_id: "design:skill-evolution"
  kind: design
  status: active
  depends_on:
    - id: "req:skill-evolution"
      relation: derives_from
  owner: ai-orchestra
---

# Skill Evolution（スキル自己改善ループ）設計ドキュメント

**作成日**: 2026-07-01
**ステータス**: active（発火検出・自己申告収集・数値 config は確定済み。オフライン層の実装は Issue #139 で追跡）
**対象**: `feat/5` ブランチ
**関連**: `req:skill-evolution`, `adr:ADR-20260701-032`

> CODD 注記: 本書 → ADR は依存 edge を張らない（ADR-032 が本書を `references` 済み。
> 双方向に張ると `req → design ← adr` のグラフに循環が生じ `codd validate` が error を出すため）。

---

## 1. 背景と目的

スキルは一度書かれると固定化され、実行品質のばらつきが自動で改善されない（`req:skill-evolution`）。
本機能は、スキル実行から **二軸テレメトリ**（自己申告＋機械計測）を収集し、学び（lessons）を
次回実行へ還元しつつ、停止条件付きの反復ループでスキル自体を改善する。

対象は **facet 製スキル（AI Orchestra 管理）＋ 導入先プロジェクト独自の非 facet 製スキル**の両方。
AI Orchestra の既存配布レール（packages / facets / hooks）に載せ、新しい配布機構は作らない。

---

## 2. スコープ

### 2.1 In Scope（本フェーズ）

- 二層アーキテクチャ（オンライン層＝毎回軽量、オフライン層＝反復改善）
- 二軸評価とデータスキーマ（`metrics/<skill>.jsonl` / `lessons/<skill>.md`）
- 自己申告の収集メカニズム（標準化された完了時申告ブロック）
- `[critical]` チェックリストタグ規約
- スキルアダプタ抽象（facet 製/非 facet 製の判別＋反映先解決）
- 改善の反映・昇格（非 facet=lessons / facet=承認付き昇格）
- **A 最小拡張（FT-12）**: `skill-review-policy` への 4 視点網羅オプション追記（Phase 6・軽微）

### 2.2 Out of Scope

| 項目                               | 移管先                            |
| ---------------------------------- | --------------------------------- |
| 成果物セルフレビューの本格自動実行 | `skill-review-policy` / `/review` |
| facet 完全自動昇格（無人）         | 当面しない（人間承認ゲート）      |
| スキル外（エージェント本体）改善   | 対象外                            |

---

## 3. アーキテクチャ

### 3.1 全体像（二層）

```text
                         ┌─────────────────────────────────────────────┐
                         │            スキル実行（毎回）                 │
                         └─────────────────────────────────────────────┘
   [発火前] lessons 注入 ───▶  スキル本体  ───▶ [完了] 自己申告ブロック出力
        ▲ (inject hook)                              │      ＋ 二軸テレメトリ捕捉
        │                                            ▼      (SubagentStop/Stop hook)
        │                            ┌──────────────────────────────┐
        │   ── オンライン層（軽量・反復しない）──▶│ metrics/<skill>.jsonl        │
        │                            │ lessons/<skill>.md (追記)     │
        └────────────────────────────┤  ※保存先は skill-evolution 独立領域 │
                                     └──────────────┬───────────────┘
                                                    │ 起動口（手動/スケジュール/lessons 閾値）
                                                    ▼  ※スキル単位ロックで同時1
                         ┌─────────────────────────────────────────────┐
                         │ オフライン層（mizchi 型・停止条件付き反復）   │
                         │  固定シナリオ runner（並列・新規サブエージェント）│
                         │     ▶ 二軸 judge ▶ 1反復1テーマ改善案         │
                         │     ▶ 停止条件＋3ガード                       │
                         └──────────────┬──────────────────────────────┘
                                        ▼ 改善反映（人間承認ゲート）
                ┌───────────────────────┴───────────────────────┐
                ▼                                                ▼
      非 facet 製スキル                                   facet 製スキル
      lessons / SKILL.md へ diff 反映                     facet 昇格 ＋ `facet build`
```

### 3.2 オンライン層（毎回・軽量）

スキル発火のたびに動く。**反復はしない**。役割は「計測」と「次回への注入」。

| 処理           | 実装                                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------------- |
| lessons 注入   | スキル発火前に `lessons/<skill>.md` を prompt へ注入（`context-sharing` の inject **ロジックのみ**流用） |
| 自己申告捕捉   | スキル完了時の自己申告ブロック（3.5）を hook が回収                                                      |
| テレメトリ捕捉 | `SubagentStop` / `Stop` hook で機械計測（`tool_uses`・`duration_ms` 等）を収集                           |
| 追記           | `metrics/<skill>.jsonl` に 1 行 append、要約を `lessons/<skill>.md` に追記                               |

> 設計判断は `adr:ADR-20260701-032`（D1 二層構成）。

**保存先の分離（重要）**: `context-sharing` から流用するのは **inject 前後のロジックパターンのみ**。
保存先は `context-sharing` のセッション領域（`session/` / `working-context.json`）を**使わない**。
これらは `cleanup-session-context.py` が SessionEnd で削除するため、lessons を置くと毎セッション消える。
lessons / metrics は **`.claude/skill-evolution/` 配下の独立した永続ストレージ**（正本は config `skill-evolution.yaml` の `storage.dir`）に保持し、
context-sharing の cleanup ロジックには一切触れない。

### 3.3 オフライン層（mizchi 型反復）

明示的な起動でのみ動く深い改善ループ。

| 要素                | 内容                                                                                                                                                     |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 起動口              | 手動コマンド / スケジュール / lessons 閾値（蓄積量がしきい値超）                                                                                         |
| 排他制御            | **同一スキルのオフライン実行は同時 1 インスタンス**。スキル単位ロック（`<skill>.lock`）で起動口の競合を防ぐ                                              |
| 固定シナリオ runner | 評価用の固定シナリオを**新規サブエージェント**で並列ディスパッチ（履歴汚染＝学習バイアス防止）                                                           |
| holdout             | 固定シナリオを **train / holdout に分割**（既定: holdout 30%）。過学習ガードは holdout スコアで判定                                                      |
| 二軸 judge          | 自己申告＋機械計測でスコアリング。**「精度」= judge 総合スコア（0–100）**。主軸は `critical_pass_rate`                                                   |
| 改善案生成          | **1 反復 1 テーマ**に限定（複数同時改変による因果不明を避ける）                                                                                          |
| 停止条件            | 連続 2 回で「新規不明瞭点 0 / 精度 +3pt 以内 / ステップ ±10% / 時間 ±15%」を全達成                                                                       |
| 3 ガード            | ①発散（3 回改善なし）→ **人間へ通知してループ停止**（自動構造変更はしない＝NF-03）/ ②過学習（holdout 15pt 超下落）→ 停止 / ③コスト打ち切り＋最大反復上限 |

- **コスト上限・最大反復上限は数値を config 化**済み（`skill-evolution.yaml` の `offline.max_cost_usd`（既定 5.0）/
  `offline.max_iterations`（既定 10）。holdout 割合・停止しきい値・ガード値も同ファイルが正本）。
- 「精度」の定義は judge 総合スコア。`success` 判定そのものは `[critical]` 全達成（3.6）で別管理。

> 設計判断は `adr:ADR-20260701-032`（D2 二軸評価・停止条件）。出典: mizchi
> 「empirical-prompt-tuning」（参考ノート `digital-garden/.../2026-04-21_ai-self-evaluation-loop-references.md`）。

### 3.4 データスキーマ

#### `metrics/<skill>.jsonl`（1 実行 1 行）

```jsonc
{
  "ts": "2026-07-01T02:40:00+09:00",
  "skill": "issue-fix",
  "run_id": "issue-fix-20260701T024000-<short-uuid>", // hook が発火時に生成（スキル名+時刻+乱数）
  "self_report": { "ambiguities": 0, "discretion_fills": 1, "retries": 0 },
  "machine": {
    "tool_uses": 14,
    "duration_ms": 83200,
    "critical_pass_rate": 1.0,
  },
  "success": true, // [critical] 全達成のときのみ true
}
```

- **`run_id`**: 発火検出 hook が「スキル名 + ISO 時刻 + 短い乱数」で一意生成し、完了側 hook と
  自己申告ブロックが同じ `run_id` を引き継ぐ。これで judge が特定実行を参照できる。

#### `lessons/<skill>.md`（人間可読・注入対象）

```markdown
# Lessons: <skill>

## [critical] チェックリスト

- [ ] <スキルごとの最小成功条件>

## 学び（新しい順）

- 2026-07-01: <短い学び・再発防止策>
```

- **肥大化管理（NF-01）**: 注入対象の lessons は**最大行数を上限**とし（既定: 直近 N 件）、
  超過分は要約圧縮して `lessons/<skill>.archive.md` へ退避する。無制限追記は注入コンテキストを
  膨らませ NF-01（レイテンシ・コンテキスト増を避ける）に違反するため必須。
- 保存先は `.claude/skill-evolution/` 配下のプロジェクトローカル領域（3.2 の分離方針）。

### 3.5 自己申告の収集メカニズム

機械計測（`tool_uses` 等）と違い、自己申告（不明瞭点・裁量補完・再試行）は**実行主体の LLM が
能動的に出力しないと取得できない**。これを安定取得するため、以下を標準化する。

- **標準化された自己申告ブロック**: スキルの**完了フェーズ**に、決まったフォーマット（構造化）で
  `run_id` と 3 項目（`ambiguities` / `discretion_fills` / `retries`）を出力する小ブロックを定義する。
- **提供方法**: skill-evolution パッケージが**共通テンプレート**として申告ブロックを提供し、
  スキルはそれを末尾に差し込む。完了側 hook がブロックをパースして `metrics` に格納する。
- **一律注入（実装済み）**: `inject-lessons.py`（PreToolUse: Skill）がスキル発火時に自己申告ブロックの
  テンプレートと `run_id` を全スキルへ一律注入するため、スキル個別の改修（テンプレート差し込み等）は
  不要。段階適用は行わない。
- **欠落時のフォールバック**: 申告ブロックが無い実行は `self_report` を `null` とし、機械計測のみで
  記録する（`success` 判定は `[critical]` 達成で成立するため自己申告欠落でも破綻しない）。

> この決定（D2 自己申告軸の実装可否）は確定済み。

### 3.6 `[critical]` チェックリストタグ規約

- スキルごとに「最小成功条件」を `[critical]` タグ付きチェックリストとして定義する。
- **全 `[critical]` 達成で初めて `success=true`**。未達は定量スコアが高くても失敗扱い。
- 規約の正本は `lessons/<skill>.md` の冒頭ブロック（または SKILL.md の専用セクション）。

### 3.7 スキルアダプタ抽象（facet 製/非 facet 製の判別）

facet 製/非 facet 製で「改善の反映先」が異なるため、判別と解決を 1 箇所に閉じる。

**判別の実装方法（誤判定を避ける）**:

- **正本は `manifest.json` の `skills` リスト照合**。AI Orchestra パッケージが管理するスキル名集合に
  含まれれば facet 製、含まれなければ非 facet 製とする。
- `facets/` ディレクトリの有無での判定は**しない**（導入先には `facets/` が無く、全スキルを
  非 facet と誤判定するため）。導入先では同梱の manifest（`.claude/orchestra.json` / パッケージ
  manifest）に基づき判別する。
- 補助案: facet build 生成物の frontmatter に `generated_by: facet` マーカーを付与（採用時は
  `facets/compositions/` 出力テンプレ変更が必要で ADR-026 との整合確認を要する）。

| 判別        | 反映先解決                                                       |
| ----------- | ---------------------------------------------------------------- |
| facet 製    | facet ソース → `facet build`（人間承認）。生成物は直接編集しない |
| 非 facet 製 | lessons 蓄積＋注入、SKILL.md への diff 提示（人間承認）          |
| 判別不能    | 安全側：lessons 蓄積のみ（生成物・facet ソースは触らない）       |

> 設計判断は `adr:ADR-20260701-032`（D3 反映先の塩梅）。
> 誤判定の影響: 「facet→非 facet」誤判定は FT-11 未達（改善が配布されない）、「非 facet→facet」
> 誤判定は導入先スキルを facet build 対象にしてしまう危険。manifest 照合でこれを防ぐ。

### 3.8 スキル発火検出（spike 実施済み・方式確定）

二層アーキの前提。spike の結論: **発火検出は実現可能**。採用方式を以下に確定する。

**採用（候補1）: `PreToolUse` / `PostToolUse` で Skill ツールを捕捉**

- **本リポジトリで実証済み**: `packages/audit/hooks/audit-route.py` が既に本番で
  `tool_name.lower() == "skill"` を検出し、`tool_input.skill`（または `skill_name`）から
  スキル名を取得している。→ Skill ツールは tool 系 hook に乗り、発火（PreToolUse）と
  完了（PostToolUse）の両側でスキル名付きで捕捉できる。
- Task/Agent 経由のスキルは `tool_input.subagent_type` で捕捉（audit-route に前例あり）。
- slash 起動（`/skill-name`）も Skill ツール呼び出しに展開されるため、候補1で拾える。

**完了境界**:

| スキルの実行形態                    | 完了検知                                                                                  |
| ----------------------------------- | ----------------------------------------------------------------------------------------- |
| メインループ内実行                  | `Stop`（`capture-skill-stop.py`）。PostToolUse は起動直後に発火するため完了検知に使えない |
| `context: fork`（サブエージェント） | `SubagentStop`（＋ `SubagentStart` で開始側）                                             |
| セッション全体の区切り              | `Stop`                                                                                    |

- **実測結果**: メインループでは Skill ツールが起動メッセージを返した時点で即完了扱いとなり、
  `PostToolUse`（`tool_name == "Skill"`）は起動直後にしか発火しないことが判明した。そのため
  `PostToolUse` は完了検知に使えず、完了側は `Stop` hook（`capture-skill-stop.py`）が transcript
  から `[skill-self-report]` ブロックを抽出し、`run_id` で pending エントリと突合して記録する。
  `PostToolUse` は tool_response に自己申告が含まれる場合のみ即時記録する経路として残す。

**不採用・代替**:

- 候補2（`UserPromptSubmit` で `/skill` 正規表現）は**不採用**。公式 docs によると slash は
  `UserPromptExpansion` で展開後に `UserPromptSubmit` へ渡るため literal を取りこぼす。ただし
  候補1が slash 起動も拾うため不要。literal が必要になった場合のみ `UserPromptExpansion` を使う。
- **縮退方針を本実装した**（2026-07-03）: 実測でメインループの `PostToolUse` が起動直後にしか
  発火せず完了検知に使えないことが判明したため、`Stop` hook（`capture-skill-stop.py`）＋
  `run_id` 突合による完了検知を本実装として採用した。

> 出典: in-repo 実証（`audit-route.py`）＋ Claude Code hooks-guide（`UserPromptExpansion` /
> `SubagentStart` / `SubagentStop` の存在を確認）＋ 実測（`capture-skill-stop.py`）。
> 発火検出・自己申告フォーマットとも確定済み（8 節参照）。

### 3.9 既存基盤の流用

| 流用元                                           | 用途                                                            |
| ------------------------------------------------ | --------------------------------------------------------------- |
| `context-sharing`（inject/capture **ロジック**） | lessons 注入・捕捉の**パターンのみ**（保存先は流用しない・3.2） |
| `quality-gates`（post-implementation-review.py） | 完了後フックの配線パターン                                      |
| `task-state` / `codd`                            | データのプロジェクトローカル保持の作法                          |

---

## 4. 改善の反映・昇格

```text
改善案（1反復1テーマ）─▶ 人間承認ゲート ─┬─ 非 facet: lessons/SKILL.md へ diff 反映
                                          └─ facet:   facet ソース更新 ─▶ `facet build` ─▶ 配布
```

- **無人での破壊的変更はしない**（NF-03）。承認なしにファイルへは書き込まない。
- facet 完全自動昇格は当面しない（`adr:ADR-20260701-032` D3）。

---

## 5. フェーズ計画（概要）

詳細タスクは `.claude/Plans.md`（`plan:skill-evolution`）を SSOT とする。

- **Phase 1**: 設計ドキュメント化（本書）＋発火検出 spike（3.8 のマトリクス確定）
- **Phase 2**: 基盤実装（パッケージ scaffold / スキーマ / 自己申告ブロック / `[critical]` タグ / アダプタ）
- **Phase 3**: オンライン層（テレメトリ収集・lessons 追記/圧縮・注入）
- **Phase 4**: オフライン層（runner / holdout 分割 / judge / 停止条件・3 ガード / 排他ロック）
- **Phase 5**: 反映・昇格（非 facet / facet）
- **Phase 6**: A 最小拡張（`skill-review-policy` 4 視点）
- **Phase 7**: テスト・ドキュメント・統合

---

## 6. リスクと対策

| リスク                           | 対策                                                               |
| -------------------------------- | ------------------------------------------------------------------ |
| 発火検出が hook で取れない       | 3.8 の成否マトリクスで失敗パターンごとの縮退方針を事前確定         |
| 自己申告が取得できない/不安定    | 標準化申告ブロック（3.5）。欠落時は機械計測のみで `null` 記録      |
| lessons がセッション終了で消える | 保存先を skill-evolution 独立領域に分離、cleanup に触らない（3.2） |
| オフラインループの暴走・過学習   | 停止条件＋3 ガード（数値 config 化）＋ holdout 判定（3.3）         |
| 起動口の同時実行で書込競合       | スキル単位ロックで同時 1 インスタンス（3.3）                       |
| lessons 肥大化で NF-01 違反      | 注入行数上限＋要約圧縮＋アーカイブ退避（3.4）                      |
| 改善の破壊的反映                 | 人間承認ゲート必須。承認まで書き込まない                           |
| facet/非 facet 判別ミス          | manifest 照合で判定、判別不能時は lessons のみに安全側（3.7）      |

---

## 7. 決定事項

- **D1**: ループは二層構成（オンライン軽量収集 ＋ オフライン反復改善）。
- **D2**: 評価は二軸（自己申告＋機械計測）。`[critical]` 全達成で初めて成功。
- **D3**: 改善反映は塩梅（非 facet=lessons / facet=承認付き昇格）。facet 自動昇格は当面しない。
- **D4**: A（成果物セルフレビュー）は最小拡張に縮小（`skill-review-policy` への追記のみ）。
- **D5**: 既存配布レール（packages/facets/hooks）に載せ、新規配布機構は作らない。
- **D6**: 発火検出方式・自己申告収集は spike / 方針確定で詰めるまで draft とする。

> D1〜D3 の採否理由は `adr:ADR-20260701-032` に記録。

---

## 8. 未解決事項の解消状況

- 発火検出の確定方式 → 3.8 で確定（メインループ完了側は `Stop` hook `capture-skill-stop.py`）。
- コスト上限・最大反復上限・lessons 注入行数・holdout 割合の具体値 →
  `config/skill-evolution.yaml` に既定値を定義済み（`offline.*` / `lessons.*` /
  `pending.stale_after_seconds`）。
- lessons 閾値（オフライン起動口のトリガー値） → `trigger.lessons_threshold: 20`。
- 自己申告ブロックの具体フォーマットと段階適用 → `[skill-self-report]` JSON ブロックとして実装済み。
  `inject-lessons.py` の一律注入により、既存スキルへの段階適用は不要になった。

オフライン層（runner / judge / 改善案生成 / 反映・昇格）の実装は未着手。Issue #139 で追跡する。
