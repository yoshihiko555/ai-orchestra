# Design Skill — Initial Response

## Phase 判定

ユーザーの入力「設計をお願いしたい」は Phase 1（要件定義）から開始すべきケースです。
また、「新規開発」と明示されているため Phase 0（既存システム調査）はスキップします。

---

## 実際のレスポンステキスト

フィットネス記録アプリの設計を承りました。

新規プロジェクトのため、**Phase 1（要件定義）** から始めます。
まず、アプリの方向性を明確にするためにいくつか確認させてください。

---

**AskUserQuestion ツール呼び出し（1回目）:**

```
AskUserQuestion("""
ターゲットユーザーと開発体制について確認します。

1. メインターゲットは誰ですか？（例: 筋トレ初心者、本格的なアスリート、ダイエット目的の一般ユーザー など）
2. iOS と Android は同時リリース予定ですか？それとも片方を先行させますか？また、開発チームの規模や使用できる技術スタックに制約はありますか？（例: React Native や Flutter で共通化したい、各プラットフォームのネイティブ実装にしたい など）
""")
```

---

## スキル準拠の解説

### Phase 判定の根拠

| 判断要素 | 値 |
|---------|---|
| 開始フェーズ | Phase 1（要件定義） |
| Phase 0 実施 | スキップ（新規プロジェクト） |
| 判定根拠 | 「新規開発」「設計をお願いしたい」→ 曖昧でなく Phase 1 から開始が適切 |

### AskUserQuestion の使用

- スキル指示通り、テキスト出力ではなく **AskUserQuestion ツール** を使用
- 1 回の質問は **2 項目** に絞り（スキルの「2-3 項目まで」制約を遵守）
- ターゲットユーザーと技術スタック（iOS/Android 戦略含む）を最優先で確認

### 質問の粒度と理由

**Q1: ターゲットユーザー**
- 機能優先度と UI/UX 方針に直結する最重要情報
- 例: 初心者向けならシンプルなログ入力、アスリート向けなら詳細な分析機能

**Q2: iOS/Android 戦略と技術スタック**
- ネイティブ（Swift/Kotlin）vs クロスプラットフォーム（React Native/Flutter）で設計が大きく変わる
- iOS Human Interface Guidelines と Android Material Design の準拠方針も異なる
- チーム規模・スキルセットがアーキテクチャ選定に影響

### 次ターン以降の想定質問

**2回目（機能スコープ）:**
- ワークアウト記録の詳細（種目マスタは固定 or カスタム可能か、セット数・レップ数・重量 or 有酸素系メニューも対象か）
- オフライン動作は必要か、データ同期（複数デバイス）は対象スコープか

**3回目（非機能要件）:**
- 認証方式（Apple ID / Google アカウント連携 vs メール認証）
- リリース先（App Store / Google Play）と想定MAU・パフォーマンス要件

### プラットフォーム固有の設計考慮事項

ユーザー回答を受け取った後、以下を設計に反映します：

| 項目 | iOS | Android |
|------|-----|---------|
| 画面設計基準 | Human Interface Guidelines | Material Design 3 |
| HealthKit 連携 | 体重・心拍数のデータ共有が可能 | Health Connect (Android 14+) |
| バックグラウンド処理 | BGTaskScheduler | WorkManager |
| ストレージ | Core Data / SwiftData | Room Database |
| Push 通知 | APNs | FCM |

### Phase 1 完了後の成果物（予定）

- `docs/project-overview.md` — プロジェクト概要・スコープ
- `docs/glossary.md` — 用語集（ワークアウト・セット・レップ・BMI 等）
- `docs/requirements/feature-list.md` — 機能一覧（Must/Should/Could 分類）
- `docs/requirements/functional.md` — 機能要件詳細
- `docs/requirements/non-functional.md` — 非機能要件（パフォーマンス・セキュリティ等）

### Phase 1 受け入れ基準（チェックリスト）

- [ ] `docs/project-overview.md` が作成された
- [ ] `docs/glossary.md` が作成された
- [ ] `docs/requirements/feature-list.md` が作成され、機能に ID が振られている
- [ ] `docs/requirements/functional.md` に Must 機能の要件が記載されている
- [ ] `docs/requirements/non-functional.md` に関連する非機能要件が記載されている
- [ ] 全機能に優先順位（Must/Should/Could）が付けられている
- [ ] スコープ（In/Out）が明確化されている

### 全体の進行計画

```
Phase 1: 要件定義（現在）
  → ターゲット・技術スタック・機能スコープの合意
  → 受け入れ確認後 Phase 2 へ

Phase 2: 基本設計（ネイティブアプリのため全ステップ実施）
  → アーキテクチャ設計（クロスプラットフォーム or ネイティブ）
  → 画面一覧・画面遷移設計
  → API 設計（バックエンドが必要な場合）
  → データモデル設計（ローカル DB + クラウド同期）
  → 受け入れ確認後 Phase 3 へ

Phase 3: 詳細設計（実装者が着手できるレベル）
  → 各画面の詳細仕様（SC-001.md 等）
  → API エンドポイント詳細（API-001.md 等）
  → テーブル定義詳細
```
