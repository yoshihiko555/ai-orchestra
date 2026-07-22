---
codd:
  node_id: "design:meta-harness-guide"
  kind: design
  status: active
  depends_on:
    - id: "design:meta-harness"
      relation: references
    - id: "design:meta-harness-detailed"
      relation: references
    - id: "design:meta-harness-proposer-routing-unlock"
      relation: references
  owner: ai-orchestra
---

# meta-harness 利用者ガイド

**更新日**: 2026-07-22
`orchex meta` サブコマンド群でハーネス構成（facet / config）を計測・評価・提案・昇格するための
利用者向けガイド。内部実装の詳細は設計書（`design:meta-harness` / `design:meta-harness-detailed`）を参照する。

> 各図は生成画像を主表示とし、正確なフロー定義は `<details>` 内の Mermaid ソースを正とする。

---

## 1. これは何か

`meta-harness` は、**ハーネス構成そのもの**（指示書・ポリシー・facet 合成・ルーティング config）を
候補（candidate）として登録し、隔離環境でシナリオ評価して「品質 vs コスト」の Pareto frontier を
求め、勝った候補を PR 経由で本流へ昇格する仕組みである。arXiv:2603.28052 の population ベース
harness 最適化を、この repo の宣言的 facet/config 面に制約して実装している。

- 候補は **宣言的オーバーレイ**（baseline との差分パッチ）として保存される。任意コードは扱わない。
- 評価は **一時 worktree + Docker コンテナ隔離**で実行され、実 HOME・store・他 worktree を汚染しない。
- 実ファイルへの書き込み経路は **`promote`（PR 経由）のみ**。探索ループは実ファイルを直接編集しない。
- **人間の関与は promotion のマージ判断のみ**。ループ自身は auto-merge を行わない。

段階導入は 3 フェーズ:

| フェーズ | 内容 | 主なサブコマンド |
| --- | --- | --- |
| Phase 1（計測基盤） | 候補登録・評価・frontier 算出 | `init` / `register` / `evaluate` / `frontier` / `status` / `purge` |
| Phase 2（提案と昇格） | proposer 起動・PR 昇格 | `propose` / `promote` |
| Phase 3（自動探索） | ガード付き自動ループ | `loop` |

---

## 2. 全体像（図解）

### ① アーキテクチャ俯瞰

store・evaluator・proposer・promoter・loop と、評価を隔離する Docker + credential broker の関係。

![meta-harness アーキテクチャ俯瞰](../assets/meta-harness/meta-harness-architecture-ja.png)

<details>
<summary>Mermaid ソース（正確な関係定義）</summary>

```mermaid
flowchart TD
    subgraph CLI["orchex meta CLI"]
        REG[register] --> STORE
        PROP[propose] --> STORE
        EVAL[evaluate] --> STORE
        FRO[frontier] --> STORE
        PROM[promote]
        LOOP[loop]
    end

    subgraph STORE[".claude/meta-harness/ (store, root worktree 配下)"]
        CAND[candidates/&lt;cand_id&gt;/<br/>overlay + manifest + notes]
        RUNS[runs/&lt;run_id&gt;/<br/>result.json + events.jsonl.gz]
        LEDGER[ledger.jsonl<br/>append-only]
        FRONTIER[frontier.json<br/>Pareto キャッシュ]
    end

    subgraph EVALZONE["隔離評価ゾーン"]
        WT[一時 worktree<br/>candidate.source_commit を --detach]
        DOCKER["Docker コンテナ<br/>候補 claude -p 実行"]
        BROKER["credential broker<br/>dual-homed sidecar"]
    end

    EVAL --> WT --> DOCKER
    DOCKER -- "ANTHROPIC_BASE_URL" --> BROKER
    BROKER -- "実 OAuth 注入" --> ANTH["api.anthropic.com"]
    DOCKER --> RUNS

    LEDGER --> FRONTIER
    PROP -. "store を read して<br/>新候補 overlay 生成" .-> STORE
    PROM -- "PR 生成（人間承認マージ）" --> REPO["facet / config 実ファイル"]
    LOOP -. "propose → evaluate → frontier を反復" .-> CLI
```

</details>

### ② サブコマンドとフェーズの対応

利用者の入口。9 サブコマンドがどのフェーズに属し、何を入出力するか。

![meta-harness サブコマンドとフェーズの対応](../assets/meta-harness/meta-harness-subcommands-phases-ja.png)

<details>
<summary>Mermaid ソース</summary>

```mermaid
flowchart LR
    subgraph P1["Phase 1: 計測基盤"]
        direction TB
        I[init<br/>store 初期化] --> R[register<br/>候補登録]
        R --> E[evaluate<br/>シナリオ評価]
        E --> F[frontier<br/>Pareto 算出]
        S[status<br/>状態表示]
        PU[purge<br/>古世代削除]
    end
    subgraph P2["Phase 2: 提案と昇格"]
        direction TB
        PP[propose<br/>候補提案・登録]
        PM[promote<br/>PR ベース昇格]
    end
    subgraph P3["Phase 3: 自動探索"]
        L[loop<br/>propose/evaluate 自動反復]
    end

    F --> PP
    PP --> E
    F --> PM
    P1 -.-> L
    P2 -.-> L
```

</details>

---

## 3. 候補ライフサイクル

候補の状態は ledger のイベント畳み込みからのみ導出され、`candidate → evaluated → promoted / retired`
の一方向に遷移する。候補・実行結果は immutable で、改訂は新しい `cand_id`（`parent_id` で系譜保持）
として登録する。

![meta-harness 候補ライフサイクル](../assets/meta-harness/meta-harness-candidate-lifecycle-ja.png)

<details>
<summary>Mermaid ソース</summary>

```mermaid
stateDiagram-v2
    [*] --> candidate: register / propose
    candidate --> evaluated: evaluate（シナリオ実行 + judge）
    evaluated --> evaluated: 再評価（新 run_id / N 回反復）
    evaluated --> promoted: promote（PR 生成 → 人間承認マージ）
    evaluated --> retired: frontier 外・劣化・過学習却下
    candidate --> retired: proposer ガード発火（発散/コスト上限）
    retired --> [*]: purge（古世代・retired 削除）
    promoted --> [*]
    note right of promoted
        実ファイルへの反映は promote の
        PR 経由のみ。ループは直接編集しない
    end note
```

</details>

---

## 4. 評価フロー（evaluate）

`orchex meta evaluate` の 1 実行。worktree 確保 → Docker 隔離実行 → self-report → rubric judge →
反復集計 → Pareto 判定 → ledger 記録。evaluator とシナリオは候補 overlay の適用範囲外に固定され、
hash を run metadata に記録・照合して reward hacking を防ぐ。

![meta-harness 評価フロー](../assets/meta-harness/meta-harness-evaluate-flow-ja.png)

<details>
<summary>Mermaid ソース</summary>

```mermaid
flowchart TD
    A[evaluate 起動] --> B{evaluate.lock 取得}
    B -- 失敗 --> BX[exit: 別 evaluate 実行中]
    B -- 成功 --> C[候補 source_commit から<br/>一時 worktree を --detach]
    C --> D[overlay 適用 + facet/context build]
    D --> E[シナリオごとに Docker コンテナ実行<br/>broker 経由で認証]
    E --> F[self-report 収集]
    F --> G[oracle 判定<br/>artifact_exists / json_schema /<br/>command_exit / rubric_judge]
    G --> H{critical checklist<br/>全達成?}
    H -- No --> HX[hard gate: 不合格]
    H -- Yes --> I[品質スカラー + コストベクトル算出]
    I --> J{重要候補?<br/>holdout シナリオ}
    J -- Yes --> K[N 回反復し mean/var/min]
    J -- No --> L[単発スコア]
    K --> M[result.json 保存 + ledger 記録]
    L --> M
    HX --> M
    M --> N[frontier 更新]
    N --> O[worktree 後始末 + lock 解放]
```

</details>

---

## 5. 隔離実行と credential broker

Phase 3 の自動評価では、候補の hooks / skills が評価対象として意図的に実行される。実 HOME や
資格情報の汚染を防ぐため、候補コンテナ内には**資格情報を一切置かず**、コンテナ外の broker だけが
実 OAuth を保持して `api.anthropic.com` へ転送する（ADR-20260712-035）。

![meta-harness credential broker 隔離実行](../assets/meta-harness/meta-harness-broker-isolation-ja.png)

<details>
<summary>Mermaid ソース（シーケンス）</summary>

```mermaid
sequenceDiagram
    participant CLI as orchex meta (host)
    participant BR as broker sidecar<br/>(dual-homed)
    participant C as 候補コンテナ<br/>(internal network)
    participant API as api.anthropic.com

    CLI->>BR: run スコープで起動（実 OAuth を tmpfs へ注入 → 読取後 unlink）
    CLI->>C: docker run --rm（cap-drop / no-new-privileges / read-only rootfs）
    Note over C: ANTHROPIC_BASE_URL=broker<br/>資格情報はコンテナ内に無い
    C->>BR: claude -p / claude --bare のリクエスト
    BR->>API: 実 OAuth ヘッダを注入して転送（egress は api.anthropic.com のみ）
    API-->>BR: 応答
    BR-->>C: 応答中継
    C-->>CLI: result / events（redaction 済み）
    CLI->>C: docker rm -f（run 終了・timeout・中断すべてで全子孫回収）
    CLI->>BR: broker 破棄（能力ごと消滅）
```

</details>

> **fail-closed**: Docker daemon 不在・イメージ pin 不一致・broker 起動失敗は、非隔離実行へ
> 降格せず **run error** とする（ADR-033 の「名前を設定しただけでは利用可能扱いにしない」を踏襲）。

---

## 6. 探索ループ（loop, Phase 3）

`orchex meta loop` は propose → evaluate → frontier 更新を自動反復する。3 ガード
（発散 / 過学習 / コスト上限）と反復上限で停止し、**promotion は含まない**（昇格は人間が別途 `promote`）。

![meta-harness 探索ループ](../assets/meta-harness/meta-harness-loop-ja.png)

<details>
<summary>Mermaid ソース</summary>

```mermaid
flowchart TD
    A[loop 開始 / 再開<br/>ledger からループ状態を復元] --> B{停止条件チェック}
    B -- 反復上限到達 --> Z[停止]
    B -- 3 回連続で改善なし --> Z2[発散 → 人間通知して停止]
    B -- コスト上限超過 --> Z3[停止]
    B -- 継続 --> C[propose: 1 候補 overlay 生成]
    C --> D[register]
    D --> E[evaluate: train シナリオ]
    E --> H{frontier 入り?}
    H -- No --> I[ledger にイテレーション記録]
    H -- Yes --> HO[frontier 入り時のみ<br/>holdout も評価]
    HO --> F{holdout が baseline から<br/>15pt 超下落?}
    F -- Yes --> G[過学習 → 候補却下 retired]
    F -- No --> I
    G --> I
    I --> B
```

</details>

`routing-config` を対象にする場合、proposer が patch できるキーは per-key の `created_by`
allowlist で制限される（`agents.*.tool` / `antigravity.model` は proposer 可、`codex.model` は
human 限定。ADR-041 → 042、`design:meta-harness-proposer-routing-unlock` 参照）。

---

## 7. 使い方（コマンド早見表）

> config（モデル・sandbox・budget 等）は `packages/meta-harness/config/meta-harness.yaml` と
> `.local` 上書きで解決する（`config-loading.md`）。プレースホルダは config 値で置換する。

```bash
# Phase 1: 計測基盤
orchex meta init                                          # store 一式を初期化
orchex meta register --overlay <dir> --target claude-harness  # 候補を登録（--overlay/--target 必須）
orchex meta evaluate --candidate <cand_id>                # 候補をシナリオ評価（--candidate 必須, Docker 隔離）
orchex meta frontier                                     # Pareto frontier レポート（--target 省略時は既定）
orchex meta status                                       # population / frontier の状態表示
orchex meta purge --keep-generations N                   # 古い世代・retired 候補を削除

# Phase 2: 提案と昇格
orchex meta propose --target claude-harness              # 候補 overlay を提案・登録（--target 必須）
orchex meta promote <cand_id>                            # frontier 候補を PR ベースで昇格（cand_id は位置引数）

# Phase 3: 自動探索
orchex meta loop                                         # propose/evaluate の自動反復（--resume <loop_id> で再開）
```

target の指定（own / skill / routing-config）や各サブコマンドの全フラグは
`orchex meta <sub> --help` と `design:meta-harness-detailed` §6（CLI 仕様）を参照する。

---

## 8. 関連ドキュメント

| ドキュメント | 内容 |
| --- | --- |
| `design:meta-harness` | 基本設計（概要・ストア・evaluator・proposer・promotion・段階導入） |
| `design:meta-harness-detailed` | 詳細設計（schema §1 / evaluator §2 / スコアリング §3 / proposer §11 / promotion §12 / loop §13） |
| `design:meta-harness-proposer-routing-unlock` | routing-config を proposer に段階解放する reward hacking 対策 |
| `req:meta-harness` | 要件定義（FT / 非機能 / 受け入れ基準） |
| `docs/evaluation/meta-harness.md` | 評価セット（EV-NN 観点） |
| ADR-032 / 035 / 041 / 042 / 044 | 機構導入 / Docker 隔離 / routing 解放 / 予算 latch 中立化 |
