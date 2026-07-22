---
codd:
  node_id: "design:meta-harness-proposer-routing-unlock"
  kind: design
  status: active
  depends_on:
    - id: "design:meta-harness-detailed"
      relation: refines
    - id: "adr:ADR-20260716-041"
      relation: references
  owner: ai-orchestra
---

# Meta-Harness: proposer への routing-config 解放 — reward hacking 対策設計

- **日付**: 2026-07-16
- **ステータス**: active（凍結決定 5 点はユーザー承認済み 2026-07-16、決定 3 は 2026-07-17 修正承認。Phase A 実装済み 2026-07-18）
- **前提 ADR**: ADR-20260716-041（routing config patch を human 登録候補限定で解放。proposer 解放は本設計の完了を着手条件として deferral）
- **成果物の位置づけ**: 本書は proposer unlock の設計 SSOT。凍結決定は
  `docs/design/meta-harness-detailed.md` §1-8 等へ反映済みで、ADR-20260717-042（accepted）として起票済み。

---

## 1. スコープ

**対象**: proposer 生成候補に `target: routing-config` の config patch（ceiling 3 key:
`agents.*.tool` / `codex.model` / `antigravity.model`）を許可するための reward hacking 対策。

**非対象**: human merge ゲート（promotion PR の手動マージ）の撤廃、ceiling の拡張、
`routing-config` 以外の target への config patch 拡張。

## 2. 前提事実（コード検証済み、2026-07-16。独立した 2 レビュー[security-reviewer / architect]が一致）

| # | 事実 | 含意 |
| --- | --- | --- |
| F1 | `scenarios/routing-config/` の critical checks は全て機械的（materialization 存在 + `assert-routing-config-layer.py` + `packages/agent-routing/tests`）で `rubric_judge` ゼロ。合法 patch は quality がほぼ定数 | frontier で変動する軸は **コストのみ**。「最安 backend へ全切替」patch が構造的に frontier を支配する |
| F2 | `resolve_skill_impacts()`（`skill_targets.py:136`）は `facets/` prefix しか見ないため、routing-config 候補は `impacted_targets=()` となりクロススキル回帰が一切走らない | patch はグローバル影響なのに blast radius が未計測。**human 登録候補にも現に効いている live gap** |
| F3 | `extract_cost()`（`evaluator.py:1423-1445`）はオーケストレーター Claude の `usage` のみ計上。codex/agy サブプロセスのトークンは不可視 | CLI 委譲するほど見かけ上安くなる「トークンロンダリング」が成立 |
| F4 | `codex.model` には allowlist が存在しない（charset regex + YAML round-trip のみ）。`antigravity.model` は `model_allowlist` 空 ⇒ 全拒否の fail-closed 実装済み | ceiling 3 key の防御は非対称。`codex.model` は実質フリーテキスト |
| F5 | judge 隔離（`--bare`、cwd はワークツリー外、`judge.*` は `meta-harness.yaml` の別ファイル）は「コードの形」で成立しているが、これを保証するテストが無い | 将来のリファクタで自己採点が無警告で復活しうる |

## 3. 脅威モデル要約

攻撃カタログ全 12 件（A1〜A12）の詳細は本設計の元となった脅威モデリング結果を §9 の実装 handoff に
引き継ぐ。ここでは対策設計を駆動する分類のみ記す。

**routing-config 解放に固有の攻撃**（facets-only では成立しない）:

- A1 コスト軸盲点によるトークンロンダリング（F3）— High
- A5 自己採点: 候補自身の patch が「どのエンジンで測られるか」を決める — High
- A3/A12 model/tool 値空間の悪用（F4、agent 名リストが frozen 定数でない TOCTOU）— Med
- A7 frontier crowd-out: proposer 候補が human 候補と同一 Pareto プールで競合し駆逐 — Med-High
- A6 `created_by` ゲートの意味崩壊（解放実装の仕方を誤ると provenance 区別が消失）— Med
- A9 judge 不変性のドリフト（F5）— Med

**汎用リスクだが routing-config で影響が拡大するもの**: A2/A4 浅いオラクルへの Goodhart（F1）、
A8 排他チェック（patch XOR overlay）の第 5 入口（proposer 経路）実装漏れ、A11 trace 経由 prompt injection。

**単一障害点（複数防御を同時に壊す仮定）**:

1. 「シナリオスイートのスコアはルーティング品質の忠実な代理」— F1 により現状 false
2. 「`total_tokens` は実コスト」— F3 により委譲時 false
3. 「`created_by=="human"` は人間による作成・レビューを意味する」— ラベルであり工程保証ではない
4. 「judge は候補 config から隔離されている」— テストなしのコード形状依存（F5）
5. 「排他チェックは全入口で実施される」— 未実装の proposer 入口では未保証

**Top-5 対処優先順位**: ①A5+A1（報酬信号自体の操作可能性）②A2+A4（浅いオラクル）
③A6（created_by 完全性）④A7+A8（frontier 汚染・入口漏れ）⑤A3+A9（codex allowlist 欠如・judge テスト欠如）。

## 4. 設計原則

1. **報酬チャネルは patch の「品質に対応しない部分」に対して不変であること**: judge の backend/model、
   コスト比較の会計単位、「合格 = スイート合法性のみ」の構造は patch の影響を受けてはならない。
2. **ほぼ定数の品質シグナルでコスト軸の勝ちを買えないこと**（F1 対応の中核）。
3. **blast radius は実測された範囲に限定されること**: グローバル patch は未計測のまま昇格不可。
4. **全ての新ゲートは fail-closed**: 判定不能は拒否（既存設計と一貫）。
5. **攻撃の照準を移すだけの複雑な機構より、安く構造的に健全な機構を優先**する。

## 5. 対策カタログ

### 採用（Phase A 前提）

| ID | 対策 | 塞ぐ攻撃 | アンカー | コスト |
| --- | --- | --- | --- | --- |
| C-1 | `codex.model_allowlist` 新設（antigravity と対称。空/欠落 ⇒ 全拒否） | A3/F4 | `cli-tools.yaml` + `validate_config_patch`（`meta_harness_common.py:~1800`） | S |
| C-2 | **quality 厳密優越ルール**: `target == routing-config` は `quality_mean` の厳密増加がなければ frontier 支配なし（コスト単独勝ち禁止）。C-9 導入後に緩和可 | A1/A2/F1 | `_dominates`（`meta_harness_common.py:~1415`）の target 分岐 | S |
| C-3 | `frontier.cost_axis` を `total_cost_usd` へ**全 target グローバル切替**（凍結決定 2） | A1 の会計非互換部分 | `config/meta-harness.yaml:66-67`（既存 `KNOWN_COST_FIELDS` 利用） | S |
| C-4 | judge 不変性の回帰テスト: (a) ceiling 全エントリが `agent-routing/cli-tools.yaml` のみを指す (b) routing-config suite に `rubric_judge` オラクルが無い、を CI で強制 | A9/F5 | 新規 `test_judge_invariance.py` 相当 | S |
| C-5 | **グローバル blast-radius ゲート**: routing-config 候補の impact を構造的に「全 `skill:*` target + `claude-harness`」とみなしクロススキル回帰を必ず実行（凍結決定 3）。suite を持つ target の解決失敗は昇格ブロッカー、suite 不在 skill は PR 本文へ unverified 警告（§6 決定 3 の 2026-07-17 修正参照）。**human 登録候補にも即適用**（F2 の live gap 修正） | A4/F2 | `candidate_impact_context`（`evaluator.py:~2754`）の target 特例 + `promoter.py` precondition | M |
| C-6 | レート制限: proposer 候補は **1 候補 = 1 key kind**（hard reject。human 候補は対象外 — 凍結決定 4）+ loop 1 回あたり routing-config 提案は 1 件 + overfit 却下後のクールダウン | A7 の物量ヒルクライム | register/propose 時の検証層 + `loop_cli.py` | S |
| C-7 | `created_by` ゲートの **per-key `allowed_created_by` マップ化**: 現行の all-or-nothing（`created_by != "human"` ⇒ 全拒否）を「key kind ごとに許可 originator を定義」へ変更。Phase A では `agents.*.tool` / `antigravity.model` のみ `proposer` を許可 | A6 | `validate_config_patch`（`meta_harness_common.py:1740-1745`） | S |
| C-8 | **行動ベーススイート拡充**（凍結決定 1、Phase A 着手前提）: ルーティング先 agent で実タスクを実行して測る `command_exit` シナリオを train/holdout に最低 1-2 本ずつ追加し、品質軸に patch 依存の実変動を作る | A2/A4/F1 | `scenarios/routing-config/` | M |

**実装規律（対策と同格の必須事項)**:

- proposer の routing-config 入口は必ず `register_candidate` と同一の検証パス（排他チェック含む）を
  経由すること。第 5 入口での排他チェック再実装・省略は禁止（A8）。
- proposer の変更メニューと登録前 `validate_config_patch` は候補の `source_commit` にある
  agent-routing config を共通参照する。その後 `register_candidate` が現 checkout の config で
  独立再検証するため、stale parent では source / 現 checkout の両方を通過した patch だけを登録する
  fail-closed な二重ゲートとなる。
- `propose_cli.py:45,136` / `loop_cli.py:277-279` の拒否解除は、C-1〜C-8 全マージ後の
  最後のコミットで行う（fail-closed の解除は最後）。

### Phase B で導入

| ID | 対策 | 内容 | コスト |
| --- | --- | --- | --- |
| C-9 | **ペア/反実仮想評価**: 同一評価バッチで同一シナリオ集合を baseline routing と patched routing の両方で実行し、絶対値でなく **差分**がノイズ閾値を超えることを要求。導入後に C-2 の制限を緩和し、コスト軸の改善にもクレジットを戻す | 報酬信号の根本修正（A1/A5 の root cause） | L |

### 不採用（理由付き）

- **shadow mode / 検疫期間**: promotion は既に human merge の PR であり、手動ゲートの上に手動ゲートを
  重ねるだけ。ADR-041 は `.local.yaml` の promotion コミットを明示的に禁止しており、新アーティファクト種別が
  必要になる割に C-2 + C-5 + human merge が塞がない隙間を塞がない。Phase C（human merge 撤廃）を
  検討する時点で再評価。
- **ledger の暗号学的ハッシュチェーン**: `created_by` は既に 3 層（register 引数 vs manifest、
  manifest vs 追記専用 ledger イベント、promote 時再検証）でクロスチェック済み。ハッシュチェーンが守るのは
  「ledger ファイルへの直接書込権限を持つ攻撃者」であり、LLM proposer の reward hacking とは別の脅威モデル。
  Issue #211（loop-harness Docker 完全隔離）で実行環境の信頼が変わる際に再評価。

## 6. 凍結決定（ユーザー承認 2026-07-16。実装時に再審議しない）

1. **スイート拡充が Phase A の着手前提**（C-8）。安全網（C-2）のみでの先行解放はしない。
2. **cost_axis は `total_cost_usd` へ全 target グローバル切替**（C-3）。既存 skill-target の frontier 序列が
   変わりうる挙動変更であることを設計・CHANGELOG に明記する。per-target override は実装しない。
3. **blast radius は全スキル + claude-harness のグローバル扱い**（C-5）。精密スコープ（patch が触る agent 名
   からの逆引き）は評価コストが実運用で問題化した場合の将来最適化とする。
   **修正（2026-07-17、ユーザー承認済み）**: 「suite 不在（unverified）= 昇格ブロッカー」は、現状カバレッジ
   （composition 22 件 vs スイート 2 件）では全 routing-config 候補（human 含む）を恒久的に昇格不能にすることが
   着手前レビューで判明。suite を持つ target の解決・実行失敗のみブロッカーとし、suite 不在 skill は
   promote PR 本文への unverified 警告列挙（skill-target と同じ既存挙動）に変更する。human merge が最終判断。
   カバレッジ拡大に伴い保護は自動的に広がる。残り 20 スキルのスイート整備は別 Issue で追跡する。
4. **1 候補 = 1 key kind 制限は proposer 候補のみ hard reject**（C-6）。human 候補は PR レビューで
   説明責任が担保されるため対象外。
5. **Phase A で proposer に解放するのは `agents.*.tool` と `antigravity.model` のみ。`codex.model` は
   C-1 の allowlist 整備後も Phase B まで human 限定**（防御の対称性が確認されるまで）。

## 7. 段階的解放計画

### Phase A — 最小解放

- **解放対象**: `agents.*.tool`, `antigravity.model`（`created_by: proposer`）。
- **前提（全て先行マージ）**: C-1〜C-8 + per-key created_by マップ（C-7）+ 実装規律 2 点。
- **entry criteria**: 前提全件が `docs/evaluation/meta-harness.md` + `.checks.yaml` で突合済み。
  敵対的ドライラン（「全 agent を最安 tool へ切替」する手作り patch）が C-2 により frontier に
  載らないことを実測確認。
- **bootstrap**: `propose` は同一 target の引用可能な non-holdout run が無ければ exit 2 で停止する。
  最初に human が現在値を再指定する no-op の `routing-config` baseline 候補を register し、train evaluate
  を完了して `frontier --rebuild --target routing-config` を実行する。この baseline の実
  `run_completed(holdout=false)` が proposer の `based_on_runs` 候補と parent frontier を同時に供給する。
  ゼロシナリオ phase は evaluator 上 vacuous pass になり得るが `run_completed` を生成しないため、bootstrap
  の代替にはならない。routing-config の train suite を実行して citable run を作ることを必須とする。
- **exit criteria（→ Phase B）**: proposer 駆動の探索 20 loop rounds 以上で (a) コスト単独優越の
  インシデント 0 件 (b) blast-radius ゲート失敗 0 件 (c) human レビューで「合法性でなく実改善」と
  判断された昇格が 1 件以上。

### Phase B — 完全解放

- **解放対象**: `codex.model` を追加。C-9（ペア評価）導入後、paired baseline の下で C-2 の
  コスト軸制限を緩和。
- **前提**: C-9 の実装と、Phase A で蓄積した候補履歴に対するバックテスト
  （「C-9 なら C-2 が見逃したものを検出できたか」）。

### Phase C — スコープ外（記録のみ）

human merge ゲートの撤廃・無人 loop での routing-config 探索は本設計の対象外。着手する場合は
shadow mode と provenance 強化（不採用 2 件）を前提から再設計する。

## 8. 恒久的に human-only の領域（non-goals）

- `CONFIG_PATCH_ALLOWLIST_CEILING`（コード定数）の変更
- `antigravity.model_allowlist` / 新設 `codex.model_allowlist` の編集（メニュー自体は信頼境界）
- `judge.*` 設定（backend/model/effort）— いかなる候補からも patch 不可
- promotion PR のマージ
- 新しい allowlist key kind の追加（追加ごとに個別の脅威モデリングを要する。例: `codex.flags` は
  安全フラグ無効化の経路になりうる）

## 9. 実装前 spike 結果（2026-07-17）

1. **Spike A — `total_cost_usd` の捕捉範囲**: codex/agy サブプロセスの支出は**捕捉しない**。
   `extract_cost()` が読むのは scenario を駆動する Claude Code の result event（`usage` /
   `total_cost_usd`）だけである。broker の `estimated_cost_usd` は同じ run の broker を経由して
   `api.anthropic.com` へ送られた scenario と `claude-bare` judge の usage を積算し、evaluator は
   CLI 申告値との大きい方を保存する。一方、codex/agy は各 CLI の独自 OAuth と endpoint を使い、
   Anthropic broker を通らないため broker metrics に現れない。したがって C-3 は Anthropic 経路の
   会計を改善するが F3 を完全には解消せず、Phase A のコスト単独勝ちを防ぐ実質的な防壁は C-2 とする。
2. **Spike B — agent 名 SSOT の TOCTOU**: `_load_known_agent_names()` の SSOT は、渡された
   `schema_dir` から sibling package として解決した
   `packages/agent-routing/config/cli-tools.yaml` の `agents` mapping keys である。register / evaluate /
   promote は共通 validator を再実行するが、現行 promote preflight と PR 直前再検証はいずれも
   developer checkout の `_SCHEMA_DIR` を渡しており、`origin/main` から作る promotion worktree の
   agent 名を再読込していない。このため evaluate 後に agent 名集合が変わると、developer checkout と
   promotion base の差による TOCTOU が成立する。凍結判断とは矛盾しないが、M2 では validator の参照元を
   promotion base に固定する最小修正が必要である。
3. **Spike C — suite 不在 target の promotion semantics**: skill composition は 22 件、scenario suite は
   2 件（`skill:handoff` / `skill:issue-create`、各 train + holdout）で、想定値と一致した。suite 不在の
   20 skill は `unverified_impacts` に記録され、promote PR 本文へ warning として全件列挙されるため
   promotion blocker ではない。suite が存在する target の suite 解決失敗は評価を成立させず、実行結果の
   fail/error・run 不足・hash 不一致は promotion precondition を通らない。これは §6 決定 3 の
   2026-07-17 修正版（suite 不在は警告、suite-bearing target の失敗は hard gate）と一致する。
