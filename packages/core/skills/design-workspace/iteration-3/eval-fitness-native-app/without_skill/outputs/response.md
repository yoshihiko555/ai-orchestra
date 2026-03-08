# フィットネス記録アプリ 設計書

iOS / Android 向けクロスプラットフォームネイティブアプリ

---

## 1. 要件整理

### 主要機能

| カテゴリ | 機能 |
|--------|------|
| ワークアウト記録 | 種目・セット数・重量・回数・有酸素時間の記録、履歴閲覧 |
| 体重・体脂肪率トラッキング | 日次入力、グラフ表示、推移確認 |
| 目標設定と進捗表示 | 体重目標・ワークアウト目標の設定、進捗バー・グラフ表示 |

### 非機能要件（想定）

- オフライン対応（ネットワーク不要で記録できること）
- iOS 16+ / Android 10+ 対応
- 将来的にウェアラブルデバイス連携も考慮

---

## 2. アーキテクチャ概要

### クロスプラットフォーム戦略

**React Native（Expo）を採用**

理由:
- iOS/Android を単一コードベースでカバー
- Expo によりビルド・配布フローの簡素化
- ネイティブ UI コンポーネントの利用が可能
- 将来的な Apple Watch / Wear OS 拡張への対応余地

### アーキテクチャパターン: Feature-Sliced Design + MVVM

```
src/
  app/              # エントリポイント・ルーティング設定
  features/         # 機能単位のモジュール（メイン）
    workout/
    body-metrics/
    goals/
    profile/
  shared/           # 共通コンポーネント・ユーティリティ
    ui/
    lib/
    api/
    store/
  entities/         # ドメインエンティティ（型定義・モデル）
```

---

## 3. 機能設計

### 3-1. ワークアウト記録

#### データモデル

```typescript
// ワークアウトセッション
interface WorkoutSession {
  id: string;
  date: string;            // ISO 8601
  name: string;            // 例: "胸の日"
  exercises: Exercise[];
  durationMinutes?: number;
  notes?: string;
  createdAt: string;
  updatedAt: string;
}

// 種目
interface Exercise {
  id: string;
  name: string;            // 例: "ベンチプレス"
  category: ExerciseCategory;  // strength | cardio | flexibility
  sets: WorkoutSet[];
}

// セット（筋トレ）
interface WorkoutSet {
  id: string;
  weight?: number;         // kg
  reps?: number;
  durationSeconds?: number; // 有酸素種目
  distanceKm?: number;
  completed: boolean;
}

type ExerciseCategory = 'strength' | 'cardio' | 'flexibility';
```

#### 主要画面フロー

```
ワークアウト一覧 → ワークアウト開始 → 種目追加 → セット入力 → 完了
                                               ↑
                              種目ライブラリ（カスタム種目追加可）
```

#### 主要ユースケース

- ワークアウト開始（テンプレートから or 空白から）
- 種目ライブラリ検索・追加
- セット記録（重量・回数のインクリメントUI）
- ワークアウト完了・保存
- 過去のワークアウト履歴閲覧
- 前回の重量・回数を自動表示（PR管理）

---

### 3-2. 体重・体脂肪率トラッキング

#### データモデル

```typescript
interface BodyMetricsRecord {
  id: string;
  date: string;          // ISO 8601 (YYYY-MM-DD)
  weightKg: number;
  bodyFatPercent?: number;
  muscleMassKg?: number; // 将来拡張
  notes?: string;
  createdAt: string;
}
```

#### 主要画面フロー

```
ダッシュボード → 体重入力ダイアログ → 保存
              → グラフ画面（週/月/年 切替）
```

#### グラフ要件

- 折れ線グラフ（体重推移・体脂肪率推移）
- 期間フィルタ（直近7日・30日・90日・1年・全期間）
- 移動平均線の重ね表示（ノイズ低減）

---

### 3-3. 目標設定と進捗表示

#### データモデル

```typescript
interface Goal {
  id: string;
  type: GoalType;
  title: string;
  targetValue: number;
  currentValue: number;
  unit: string;
  startDate: string;
  targetDate?: string;
  status: 'active' | 'completed' | 'paused';
  createdAt: string;
}

type GoalType =
  | 'target_weight'         // 目標体重
  | 'target_body_fat'       // 目標体脂肪率
  | 'weekly_workout_count'  // 週あたりワークアウト数
  | 'exercise_max_weight';  // 特定種目の最大重量
```

#### 進捗表示要件

- 進捗バー（現在値 / 目標値）
- 達成率（%）
- 残り日数
- 推移グラフ（目標ラインとの比較）
- 目標達成時の通知（ローカルプッシュ通知）

---

## 4. 技術スタック

### フロントエンド（アプリ）

| 役割 | 採用技術 | 理由 |
|------|---------|------|
| フレームワーク | React Native + Expo SDK 51 | クロスプラットフォーム・高エコシステム |
| 言語 | TypeScript | 型安全・IDE補完 |
| 状態管理 | Zustand | 軽量・シンプル |
| ナビゲーション | Expo Router (file-based) | 直感的・ディープリンク対応 |
| UIライブラリ | React Native Paper | Material Design 準拠 |
| グラフ | Victory Native XL | 高パフォーマンス・Skia ベース |
| フォーム | React Hook Form + Zod | 型安全バリデーション |
| ローカルDB | WatermelonDB | 大量データ対応・オフラインファースト |
| 日付処理 | date-fns | 軽量 |
| アニメーション | React Native Reanimated v3 | 高フレームレート |

### バックエンド（オプション）

オフラインファーストを優先するが、将来的なクラウド同期・バックアップのために設計を考慮する。

| 役割 | 採用技術 |
|------|---------|
| API | FastAPI (Python) または Hono (TypeScript on Cloudflare Workers) |
| DB | PostgreSQL |
| 認証 | Supabase Auth |
| ストレージ同期 | Supabase Realtime + 差分同期 |

**フェーズ1はバックエンドなし（ローカルのみ）で実装し、フェーズ2でクラウド同期を追加する戦略を推奨。**

---

## 5. データ永続化設計

### ローカルDB（WatermelonDB）

```
WatermelonDB (SQLite ベース)
  └── workoutSessions     テーブル
  └── exercises           テーブル
  └── workoutSets         テーブル
  └── bodyMetrics         テーブル
  └── goals               テーブル
  └── exerciseLibrary     テーブル（プリセット種目）
```

### 同期戦略（フェーズ2）

- WatermelonDB の組み込み同期プロトコルを利用
- サーバー側に `updated_at` + `is_deleted` でソフトデリート
- コンフリクト解決: Last-Write-Wins（フィットネスデータの性質上、最新が優先）

---

## 6. 画面構成

### タブ構成

```
[ホーム] [ワークアウト] [記録] [目標] [プロフィール]
```

### 画面一覧

| 画面 | 説明 |
|------|------|
| ホーム (Dashboard) | 今日のサマリー、直近体重、目標進捗プレビュー |
| ワークアウト一覧 | 過去セッション一覧・カレンダービュー |
| ワークアウト実行 | リアルタイム記録・タイマー・セット入力 |
| 種目ライブラリ | プリセット種目一覧・カスタム追加 |
| 体重記録 | 入力フォーム + 推移グラフ |
| 目標一覧 | アクティブ目標と進捗 |
| 目標作成/編集 | ウィザード形式 |
| プロフィール | 身長・単位設定・データエクスポート |

---

## 7. 非機能要件への対応

### オフライン対応

- WatermelonDB によりローカル完結
- ネットワーク不要でフル機能利用可能
- クラウド同期はバックグラウンドで行い、失敗してもアプリを止めない

### パフォーマンス

- グラフ描画に Victory Native XL（Skia ベース）を採用し、大量データでもスムーズ
- WatermelonDB のレイジーローディングで初期起動を高速化
- 過去ワークアウト一覧は FlatList + ページネーション

### アクセシビリティ

- React Native Paper のアクセシビリティプロパティを活用
- Dynamic Type（iOS）/ フォントスケール（Android）対応
- カラーコントラスト比 4.5:1 以上

### セキュリティ

- ローカルDBは Expo SecureStore でマスターキー管理
- クラウド同期時は HTTPS + JWT
- 生体認証ロック（オプション）

---

## 8. 開発フェーズ計画

### フェーズ1（MVP）: 約6週間

- [x] プロジェクトセットアップ（Expo + TypeScript + ESLint）
- [x] ローカルDB設計・WatermelonDB セットアップ
- [x] ワークアウト記録機能（CRUD）
- [x] 体重・体脂肪率入力と推移グラフ
- [x] 基本的な目標設定と進捗表示
- [x] ダッシュボード画面

### フェーズ2: 約4週間

- [ ] クラウド同期（Supabase）
- [ ] Apple HealthKit / Google Fit 連携
- [ ] データエクスポート（CSV / PDF）
- [ ] プッシュ通知（目標達成・ワークアウトリマインダー）
- [ ] ウィジェット（iOS 14+ / Android）

### フェーズ3: 約3週間

- [ ] ソーシャル機能（友達と進捗共有）
- [ ] Apple Watch / Wear OS 対応
- [ ] AI による分析・アドバイス機能

---

## 9. ディレクトリ構成（詳細）

```
fitness-app/
  app/                          # Expo Router pages
    (tabs)/
      index.tsx                 # ホーム
      workout.tsx               # ワークアウト
      metrics.tsx               # 体重・体脂肪
      goals.tsx                 # 目標
      profile.tsx               # プロフィール
    workout/
      [id].tsx                  # セッション詳細
      new.tsx                   # 新規ワークアウト
    goals/
      new.tsx
      [id].tsx
  src/
    features/
      workout/
        components/             # WorkoutCard, SetRow, ExercisePicker ...
        hooks/                  # useWorkoutSession, useExerciseLibrary ...
        store/                  # Zustand slice
        types.ts
      body-metrics/
        components/             # WeightChart, MetricsInputModal ...
        hooks/
        store/
        types.ts
      goals/
        components/             # GoalCard, ProgressBar ...
        hooks/
        store/
        types.ts
    shared/
      ui/                       # Button, Card, Modal, TextInput ...
      lib/
        db/                     # WatermelonDB models & schema
        notifications/
        healthKit/
      store/
        index.ts                # Root store
  assets/
  __tests__/
```

---

## 10. 主要なリスクと対策

| リスク | 影響 | 対策 |
|--------|------|------|
| WatermelonDB の学習コスト | 中 | 初期にサンプル実装でチームが慣熟する |
| グラフライブラリの描画性能 | 中 | Victory Native XL + データ間引き処理 |
| iOS/Android の UI 差異 | 低〜中 | Platform.OS で分岐、E2Eテストで両プラットフォーム確認 |
| クラウド同期コンフリクト | 中（フェーズ2） | LWW ポリシー明確化、フェーズ1はオフラインのみで回避 |
| App Store / Play Store 審査 | 低 | ヘルスデータ取扱いポリシーを事前に整備 |

---

## まとめ

- **React Native + Expo** でiOS/Androidを単一コードベースでカバー
- **WatermelonDB** でオフラインファーストを実現し、フェーズ2でクラウド同期を追加
- **Feature-Sliced Design** でスケーラブルなディレクトリ構成
- フェーズ1（MVP）を6週間で完成させ、段階的に機能拡張する戦略を推奨
