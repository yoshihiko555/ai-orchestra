---
name: design
description: |
  Interactive design skill for software projects — covers requirements definition,
  basic design, and detailed design through dialogue with the user.
  Use this skill when the user wants to define requirements, design architecture,
  plan screens/API/database, or any pre-implementation design work.
  Trigger on: "設計", "要件定義", "基本設計", "詳細設計", "画面設計", "API設計",
  "アーキテクチャ", "design", "requirements", "system design",
  or when the user discusses what to build before implementation.
  This skill produces design documents that feed into /preflight and /startproject.
---

# Design

**ソフトウェア設計を対話的に段階的に進めるスキル。**

> このスキルは対話型ワークフローです。
> ユーザーと会話しながら要件や設計を詰めていきます。
> `EnterPlanMode` ツールは使用しないこと。

### 対話の手段（必須）

ユーザーへの質問には **常に AskUserQuestion ツール** を使用すること。
初回の質問だけでなく、**2ターン目以降の全ての対話ターンでも** AskUserQuestion を使う。
テキストとして質問を出力するのではなく、AskUserQuestion を呼び出してユーザーの回答を待つ。
これにより、対話の区切りが明確になり、ユーザーが確実に回答できる。

```
AskUserQuestion("質問内容をここに記載")
```

1 回の AskUserQuestion で聞く質問は **2-3 項目まで**。それ以上は次のターンに回す。

**なぜ AskUserQuestion が重要か**: 設計は対話の積み重ね。テキスト出力だけでは、ユーザーがどこで回答すべきか不明確になる。AskUserQuestion を使うことで「ここであなたの回答が必要です」という境界が明示される。

## Overview

`/design` は設計フェーズを 3 段階に分けて対話的に進める：

1. **要件定義** — 何を作るか、何を満たすべきか（用語・プロジェクト概要・機能一覧・要件）
2. **基本設計** — どう作るか（アーキテクチャ、画面、API、データモデルの全体像）
3. **詳細設計** — 各コンポーネントの実装レベル仕様（個別設計書として出力）

各フェーズは独立して実行可能。途中から始めることも、特定フェーズだけ実行することもできる。

### フェーズ間の境界

| 境界 | 基準 |
|------|------|
| 要件定義 → 基本設計 | 「何を作るか」が確定した（機能一覧・要件が合意済み） |
| 基本設計 → 詳細設計 | 「どう作るか」の方針が確定した（一覧レベルの設計が合意済み） |

基本設計は「一覧」と「方針」を決めるフェーズ。詳細設計は一覧の各項目を「個別の設計書」に展開するフェーズ。

### 他スキルとの関係

```
/design → 設計ドキュメント（docs/）
    ↓
/preflight → タスク分解（Plans.md）
    ↓
/startproject → 実装
```

## Workflow（俯瞰図）

```
[Phase 0: 既存システム調査]  ← 既存プロジェクトの場合のみ
  成果物: なし（調査結果を口頭共有）
  遷移: ユーザーが調査結果に合意
    ↓
Phase 1: 要件定義（Requirements）
  成果物: project-overview.md, glossary.md, feature-list.md, functional.md, non-functional.md
  遷移: 全成果物作成 + MoSCoW分類 + スコープ確定 + ユーザー合意
    ↓
Phase 2: 基本設計（Basic Design）
  成果物: architecture.md, screen-list.md, api-list.md, er-design.md（該当分のみ）
  遷移: 該当する全一覧ドキュメント作成 + ユーザー合意
    ↓
Phase 3: 詳細設計（Detailed Design）
  成果物: API-001.md, SC-001.md, TBL-001.md, ...（一覧の各項目を個別展開）
  遷移: 全個別設計書作成 + 実装可能レベル + ユーザー合意
    ↓
/preflight → タスク分解（Plans.md 作成）
    ↓
/startproject → 実装開始
```

各フェーズの終わりで **受け入れ確認** を行い、ユーザーの明示的な合意を得てから次に進む。
Phase 3 完了後は `/preflight` でタスク分解し、`/startproject` で実装に入る流れになる。

---

## Phase 判定

ユーザーの入力から開始フェーズを判定する：

| ユーザーの意図 | 開始フェーズ |
|--------------|------------|
| 「要件定義から始めたい」「何を作るか整理したい」 | Phase 1 |
| 「要件は決まっている、設計をしたい」 | Phase 2 |
| 「基本設計は終わった、詳細を詰めたい」 | Phase 3 |
| 「設計して」（曖昧） | Phase 1 から |

判定が曖昧な場合は AskUserQuestion で確認する。

既存の `docs/` 配下にドキュメントがある場合は、それを読み込んで前フェーズの成果物として扱う。

---

## Phase 0: 既存システム調査（条件付き）

**既存プロジェクトへの機能追加・改修の場合にのみ実施する。** 新規プロジェクトの場合はスキップして Phase 1 へ進む。

### いつ実施するか

ユーザーの入力に以下のキーワード・文脈が含まれる場合に Phase 0 を実施する：
- 「既存の〜に追加」「機能追加」「改修」「リプレース」
- 既にコードベースが存在するプロジェクト内で `/design` が呼ばれた場合

### 進め方

このスキルは Claude Code 内で実行されるため、プロジェクト配下のコードを直接調査できる。
技術スタックやプロジェクト構造はユーザーに聞くのではなく、**コードから自動で把握する**。

1. **プロジェクト構造の自動調査** — Glob/Grep/Read でディレクトリ構造、主要ファイル、設定ファイル（`package.json`, `pyproject.toml`, `go.mod`, `Gemfile` 等）を読み取る
2. **技術スタックの自動特定** — 設定ファイルから言語・フレームワーク・DB・インフラを把握する。ユーザーに聞くのは自動判別できない情報（インフラ構成、外部サービス連携等）のみ
3. **既存ドキュメントの確認** — `docs/`, `README.md`, 既存の設計書があれば読み込む
4. **既存の設計パターン把握** — コードのアーキテクチャパターン（レイヤード、MVC 等）を分析
5. **調査結果のサマリをユーザーに提示** — AskUserQuestion で認識齟齬がないか確認する

サブエージェントの活用（`cli-tools.yaml` の `agents.researcher.tool` を参照）：

```
# 大規模コードベースの場合は researcher エージェントを活用
Task(subagent_type="researcher", prompt="""
以下のプロジェクトの構造と技術スタックを調査してください：

調査対象:
1. プロジェクト構造（主要ディレクトリ、ファイル構成）
2. 技術スタック（言語、フレームワーク、DB、インフラ）— 設定ファイルから自動検出
3. 既存の設計パターン・アーキテクチャ
4. 関連する既存機能・コード

結果を要約して返してください。
""")
```

### フェーズ完了条件

- 既存システムの技術スタックと構造を把握した
- ユーザーが調査結果の認識に合意した

---

## Phase 1: 要件定義（Requirements）

`references/requirements.md` を読み込んで実行する。

**概要**: ユーザーとの対話を通じて、プロジェクトの全体像・用語・機能要件・非機能要件を整理し、ドキュメント化する。

### 進め方

1. **プロジェクト概要の整理** — 目的、背景、ターゲットユーザー、スコープ
2. **用語の定義** — プロジェクト固有の用語を整理（認識齟齬の防止）
3. **機能一覧の作成** — システムが持つ機能をリストアップし分類
4. **機能要件の洗い出し** — 各機能のユースケース・受け入れ条件を対話で明確化
5. **非機能要件の確認** — パフォーマンス、セキュリティ、可用性等
6. **優先順位付け** — Must/Should/Could（MoSCoW）で分類
7. **ドキュメント作成** — 合意した内容をドキュメント化

### 出力ファイル

| ファイル | 内容 |
|---------|------|
| `docs/project-overview.md` | プロジェクト概要（目的・背景・スコープ） |
| `docs/glossary.md` | 用語集 |
| `docs/requirements/feature-list.md` | 機能一覧 |
| `docs/requirements/functional.md` | 機能要件の詳細 |
| `docs/requirements/non-functional.md` | 非機能要件の詳細 |

### フェーズ完了条件（受け入れチェックリスト）

以下の全項目を満たした上で、AskUserQuestion でユーザーに受け入れ確認を行う：

- [ ] `docs/project-overview.md` が作成された
- [ ] `docs/glossary.md` が作成された（用語が少なくても骨格は作る）
- [ ] `docs/requirements/feature-list.md` が作成され、機能に ID が振られている
- [ ] `docs/requirements/functional.md` に Must 機能の要件が記載されている
- [ ] `docs/requirements/non-functional.md` に関連する非機能要件が記載されている
- [ ] 全機能に優先順位（Must/Should/Could）が付けられている
- [ ] スコープ（In/Out）が明確化されている

**受け入れ確認の聞き方（例）：**

```
AskUserQuestion("Phase 1（要件定義）の成果物が揃いました。以下の内容で次の基本設計フェーズに進んでよろしいですか？

作成したドキュメント:
- docs/project-overview.md（プロジェクト概要）
- docs/glossary.md（用語集）
- docs/requirements/feature-list.md（機能一覧: N件）
- docs/requirements/functional.md（機能要件詳細）
- docs/requirements/non-functional.md（非機能要件）

不足している点や修正したい箇所があれば教えてください。")
```

---

## Phase 2: 基本設計（Basic Design）

`references/basic-design.md` を読み込んで実行する。

**概要**: 要件に基づいてシステムの全体像を設計する。このフェーズでは「一覧」と「方針」を決める。個別の詳細仕様は Phase 3 で扱う。

### 進め方

プロジェクトに応じて必要なステップを選択する：

1. **アーキテクチャ設計** — システム構成、技術スタック選定、コンポーネント分割
2. **画面設計** — 画面一覧、画面遷移の整理
3. **API 設計** — エンドポイント一覧、認証方式、共通仕様
4. **データモデル設計** — エンティティ一覧、リレーション、主要テーブル定義

各サブステップでユーザーと対話しながら進める。
必要に応じてサブエージェント（`architect`, `api-designer`, `data-modeler`）を活用する。

### 出力ファイル

| ファイル | 内容 | 必須度 |
|---------|------|--------|
| `docs/architecture/architecture.md` | アーキテクチャ設計 | ほぼ必須 |
| `docs/screens/screen-list.md` | 画面一覧 | UI ありの場合 |
| `docs/screens/screen-transitions.md` | 画面遷移 | UI ありの場合 |
| `docs/api/api-list.md` | API エンドポイント一覧 | API ありの場合 |
| `docs/database/er-design.md` | データモデル設計（テーブル定義含む） | DB ありの場合 |

プロジェクトの種類に応じて不要なものは省略する：

| プロジェクト種類 | 推奨ステップ |
|----------------|------------|
| Web アプリ（フルスタック） | 全ステップ |
| ネイティブアプリ | アーキテクチャ + 画面 + API + データモデル |
| API サーバーのみ | アーキテクチャ + API + データモデル |
| CLI ツール | アーキテクチャのみ（必要に応じてデータモデル） |

### フェーズ完了条件（受け入れチェックリスト）

以下のうち、プロジェクトに該当する項目を満たした上で、AskUserQuestion でユーザーに受け入れ確認を行う：

- [ ] `docs/architecture/architecture.md` が作成された（技術スタック・構成図を含む）
- [ ] 画面一覧（`screen-list.md`）と画面遷移（`screen-transitions.md`）が作成された（UI ありの場合）
- [ ] API 一覧（`api-list.md`）が作成され、エンドポイントに ID が振られている（API ありの場合）
- [ ] データモデル（`er-design.md`）が作成され、テーブル定義を含んでいる（DB ありの場合）
- [ ] 各ドキュメントの内容にユーザーが合意した

---

## Phase 3: 詳細設計（Detailed Design）

`references/detailed-design.md` を読み込んで実行する。

**概要**: 基本設計の一覧から各項目を個別の設計書に展開する。実装者がこのドキュメントだけで実装を始められるレベルの詳細を記述する。

### 進め方

基本設計で作成した一覧ドキュメントをもとに、各項目の詳細設計書を作成する：

1. **API 詳細設計** — 各エンドポイントごとに個別ファイル（`API-001.md` 等）
2. **画面詳細設計** — 各画面ごとに個別ファイル（`SC-001.md` 等）
3. **データモデル詳細** — テーブル定義の詳細（カラム、インデックス、制約）
4. **コンポーネント詳細** — 主要コンポーネントの内部設計

### 出力ファイル

一覧から個別ファイルに展開する形式：

```
docs/
├── api/
│   ├── api-list.md          ← Phase 2 で作成済み（一覧）
│   ├── API-001.md           ← 個別エンドポイント詳細設計
│   ├── API-002.md
│   └── ...
├── screens/
│   ├── screen-list.md       ← Phase 2 で作成済み（一覧）
│   ├── SC-001.md            ← 個別画面詳細設計
│   ├── SC-002.md
│   └── ...
└── database/
    ├── er-design.md          ← Phase 2 で作成済み（全体像）
    ├── TBL-001.md            ← 個別テーブル詳細（必要に応じて）
    └── ...
```

ID 採番は一覧ドキュメントの ID と対応させる。

### フェーズ完了条件（受け入れチェックリスト）

以下を満たした上で、AskUserQuestion でユーザーに受け入れ確認を行う：

- [ ] 基本設計の一覧に対応する個別設計書（`API-{ID}.md`, `SC-{ID}.md` 等）が作成された
- [ ] 各設計書に実装に必要な情報（型定義、バリデーション、状態管理等）が記載されている
- [ ] 「実装者がこのドキュメントだけで実装を始められるか？」の基準を満たしている
- [ ] 各設計書の内容にユーザーが合意した

---

## 拡張設計トラック（オプション）

Phase 1-3 の基本フローに加えて、プロジェクトの性質に応じて追加の設計トラックを実施できる。
これらは独立した Phase ではなく、Phase 2 または Phase 3 の中で必要に応じて実施する。

| トラック | reference ファイル | 主な実施タイミング | トリガー |
|---------|------------------|------------------|---------|
| デザインシステム | `references/design-system.md` | Phase 2-3（画面設計と並行） | UI を持つプロジェクトで画面数が多い、またはユーザーが希望 |
| テスト設計 | `references/test-design.md` | Phase 2-3（設計と並行） | 機能数が多い、品質要件が厳しい、またはユーザーが希望 |
| セキュリティ設計 | `references/security-design.md` | Phase 1-2（要件・基本設計と並行） | 機密データを扱う、権限モデルが複雑、またはユーザーが希望 |

### いつ提案するか

以下の状況を検出した場合、対応する拡張トラックの実施を AskUserQuestion で提案する：

- **デザインシステム**: 画面一覧（`screen-list.md`）作成時に画面数が 10 以上、または複数のフロントエンド開発者が想定される場合
- **テスト設計**: 機能一覧（`feature-list.md`）で Must 機能が 10 以上、または非機能要件で品質基準が明示されている場合
- **セキュリティ設計**: 個人情報・決済情報等の機密データを扱う、またはマルチテナント・複雑な権限モデルが必要な場合
- ユーザーが各トラックに関連するキーワード（「デザインシステム」「テスト計画」「セキュリティ設計」「認証設計」等）を使った場合は、提案ではなく直接 reference を読み込んで開始する

### 実施方法

1. 該当する `references/*.md` を読み込む
2. reference の手順に従い、ユーザーと対話しながら設計を進める
3. 成果物は `docs/` 配下の対応ディレクトリに出力する

拡張トラックの実施は任意。ユーザーが不要と判断すればスキップしてよい。

---

## Agent Routing

設計作業でサブエージェントを活用する際は、`cli-tools.yaml` の `agents.{name}.tool` を参照する。

| エージェント | 用途 |
|------------|------|
| `architect` | アーキテクチャレビュー・設計判断 |
| `api-designer` | API 設計の詳細化 |
| `data-modeler` | データモデル設計 |
| `researcher` | 技術調査・ライブラリ選定 |
| `requirements` | 要件の整理・分析（補助） |

使い方の例：

```
Task(subagent_type="architect", prompt="""
以下の要件に対するアーキテクチャを提案してください：

要件: {要件の概要}
技術制約: {制約}

提案内容:
1. システム構成
2. 技術スタック
3. トレードオフ分析
""")
```

---

## 対話のガイドライン

このスキルは対話が中心。以下を意識する：

- **一度に聞きすぎない** — 1 回の質問は 2-3 項目まで
- **具体例を示す** — 抽象的な質問より「例えばこういう機能ですか？」
- **中間成果物を見せる** — 要件リストや設計案を途中で提示してフィードバックを得る
- **判断を押しつけない** — 選択肢を提示してユーザーに選んでもらう
- **前フェーズの成果物を参照する** — `docs/` のファイルを読んで文脈を維持する
- **Web とネイティブアプリの違いを考慮する** — プラットフォームに応じた設計項目を提示する

---

## 出力ディレクトリ

全ての成果物は `docs/` 配下に配置する：

```
docs/
├── project-overview.md       ← プロジェクト概要
├── glossary.md               ← 用語集
├── requirements/             ← 要件定義
│   ├── feature-list.md       ← 機能一覧
│   ├── functional.md         ← 機能要件詳細
│   └── non-functional.md     ← 非機能要件詳細
├── architecture/             ← アーキテクチャ
│   └── architecture.md
├── screens/                  ← 画面設計
│   ├── screen-list.md        ← 画面一覧
│   ├── screen-transitions.md ← 画面遷移
│   ├── SC-001.md             ← 個別画面詳細（Phase 3）
│   └── ...
├── api/                      ← API 設計
│   ├── api-list.md           ← エンドポイント一覧
│   ├── API-001.md            ← 個別API詳細（Phase 3）
│   └── ...
├── database/                 ← データモデル
│   ├── er-design.md          ← ER 設計（テーブル定義含む）
│   └── ...
├── design-system/            ← デザインシステム（拡張トラック）
│   ├── principles.md         ← デザイン原則
│   ├── tokens.md             ← Design Tokens
│   ├── component-list.md     ← コンポーネント一覧
│   ├── CMP-001.md            ← 個別コンポーネント詳細
│   └── ...
├── testing/                  ← テスト設計（拡張トラック）
│   ├── test-strategy.md      ← テスト戦略
│   ├── test-plan.md          ← テスト計画
│   └── api-test-plan.md      ← API テスト計画
└── security/                 ← セキュリティ設計（拡張トラック）
    ├── security-requirements.md ← セキュリティ要件
    ├── threat-model.md       ← 脅威モデル
    ├── auth-design.md        ← 認証・認可設計
    └── data-protection.md    ← データ保護設計
```

ディレクトリ構成は固定ではない。プロジェクトに応じて追加・省略してよい。

---

## Tips

- 全フェーズを一度にやる必要はない — 要件定義だけで止めてもよい
- 設計ドキュメントは `/preflight` や `/startproject` への入力になる
- 大きなプロジェクトではフェーズごとにセッションを分けることを推奨
- 既存プロジェクトに新機能を追加する場合は、Phase 0（既存システム調査）から始める
- 用語集はプロジェクト初期に作成し、設計中に継続的に更新する
