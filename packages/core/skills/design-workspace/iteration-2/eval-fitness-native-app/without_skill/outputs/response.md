# フィットネス記録アプリ 設計書

## 1. 概要

iOS / Android 向けのクロスプラットフォームフィットネス記録アプリ。
主な機能: ワークアウト記録、体重・体脂肪率トラッキング、目標設定と進捗表示。

---

## 2. 技術スタック選定

### フロントエンド（モバイル）

| 項目 | 選定 | 理由 |
|------|------|------|
| フレームワーク | **React Native + Expo** | iOS/Android を単一コードベースでカバー。Expo により OTA アップデートや端末センサー連携が容易 |
| 言語 | TypeScript | 型安全性と開発体験 |
| 状態管理 | Zustand | 軽量かつシンプル。Redux より学習コストが低い |
| ナビゲーション | React Navigation v7 | React Native デファクトスタンダード |
| UI コンポーネント | React Native Paper / NativeWind (Tailwind) | Material Design 準拠 + ユーティリティ CSS |
| グラフ | Victory Native | 体重・体脂肪グラフ描画 |

### バックエンド

| 項目 | 選定 | 理由 |
|------|------|------|
| 言語 | Python 3.12 | 開発速度・エコシステム |
| フレームワーク | FastAPI | 高速・自動 OpenAPI ドキュメント生成 |
| ORM | SQLAlchemy 2.x + Alembic | マイグレーション管理 |
| DB | PostgreSQL 16 | リレーショナルデータに適合 |
| 認証 | JWT (access + refresh token) | ステートレス認証 |
| ファイルストレージ | AWS S3 / Cloudflare R2 | プロフィール画像等 |

### インフラ

| 項目 | 選定 |
|------|------|
| コンテナ | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| ホスティング | Render / Railway（初期）→ AWS ECS（スケール時） |
| プッシュ通知 | Expo Push Notifications → APNs / FCM |

---

## 3. アーキテクチャ概要

```
┌─────────────────────────────────────────────────┐
│              Mobile App (React Native)           │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Workout  │  │ Body     │  │ Goal /        │  │
│  │ Tracker  │  │ Metrics  │  │ Progress      │  │
│  └────┬─────┘  └────┬─────┘  └──────┬────────┘  │
│       └─────────────┴───────────────┘            │
│                   Zustand Store                  │
│                   API Client (Axios)             │
└────────────────────────┬────────────────────────┘
                         │ HTTPS / REST API
┌────────────────────────▼────────────────────────┐
│              FastAPI Backend                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Auth     │  │ Workout  │  │ Body /        │  │
│  │ Router   │  │ Router   │  │ Goal Router   │  │
│  └────┬─────┘  └────┬─────┘  └──────┬────────┘  │
│       └─────────────┴───────────────┘            │
│                  Service Layer                   │
│                  Repository Layer                │
└────────────────────────┬────────────────────────┘
                         │
┌────────────────────────▼────────────────────────┐
│               PostgreSQL Database                │
└─────────────────────────────────────────────────┘
```

**レイヤー構成（バックエンド）:**

```
api/
  routers/        # エンドポイント定義（薄い層）
  services/       # ビジネスロジック
  repositories/   # DB アクセス（SQLAlchemy）
  schemas/        # Pydantic モデル（入出力）
  models/         # SQLAlchemy ORM モデル
  core/           # 設定・認証・依存性注入
```

---

## 4. データモデル設計

### ER 図（主要テーブル）

```
users
  id (PK)
  email (unique)
  hashed_password
  display_name
  profile_image_url
  created_at

goals
  id (PK)
  user_id (FK → users)
  type  ENUM(weight, body_fat, workout_frequency, custom)
  target_value
  unit
  deadline
  created_at

workout_sessions
  id (PK)
  user_id (FK → users)
  title
  started_at
  ended_at
  notes

exercises
  id (PK)
  name
  category  ENUM(chest, back, legs, shoulders, arms, core, cardio)
  is_custom  BOOLEAN

workout_sets
  id (PK)
  session_id (FK → workout_sessions)
  exercise_id (FK → exercises)
  set_number
  reps
  weight_kg
  duration_sec  (有酸素系)
  distance_m    (有酸素系)

body_metrics
  id (PK)
  user_id (FK → users)
  recorded_at
  weight_kg
  body_fat_pct
  notes
```

### インデックス方針

- `workout_sessions(user_id, started_at DESC)` — 一覧取得
- `body_metrics(user_id, recorded_at DESC)` — グラフ描画
- `workout_sets(session_id)` — セッション詳細

---

## 5. API 設計（主要エンドポイント）

### 認証

```
POST /auth/register    ユーザー登録
POST /auth/login       ログイン（JWT 発行）
POST /auth/refresh     アクセストークン更新
POST /auth/logout      ログアウト（リフレッシュトークン無効化）
```

### ワークアウト

```
GET    /workouts                 セッション一覧（ページネーション）
POST   /workouts                 セッション作成
GET    /workouts/{id}            セッション詳細
PUT    /workouts/{id}            セッション更新
DELETE /workouts/{id}            セッション削除
POST   /workouts/{id}/sets       セット追加
PUT    /workouts/{id}/sets/{set_id}   セット更新
DELETE /workouts/{id}/sets/{set_id}   セット削除
```

### 体組成トラッキング

```
GET    /body-metrics             記録一覧（期間フィルタ）
POST   /body-metrics             記録追加
DELETE /body-metrics/{id}        記録削除
GET    /body-metrics/summary     週/月集計サマリー
```

### 目標・進捗

```
GET    /goals                    目標一覧
POST   /goals                    目標設定
PUT    /goals/{id}               目標更新
DELETE /goals/{id}               目標削除
GET    /goals/{id}/progress      進捗計算（達成率・推移）
```

---

## 6. モバイルアプリ画面構成

### ナビゲーション構造

```
App
├── Auth Stack
│   ├── LoginScreen
│   └── RegisterScreen
└── Main Tab Navigator
    ├── HomeScreen（ダッシュボード）
    ├── Workout Stack
    │   ├── WorkoutListScreen
    │   ├── WorkoutSessionScreen（記録中）
    │   └── WorkoutDetailScreen
    ├── BodyMetricsStack
    │   ├── BodyMetricsScreen（グラフ + 一覧）
    │   └── AddBodyMetricScreen
    ├── GoalsScreen（目標 + 進捗）
    └── ProfileScreen
```

### 主要画面の説明

| 画面 | 主要コンポーネント |
|------|-------------------|
| ホーム | 今週のワークアウト回数、最新体重、目標達成率カード |
| ワークアウト記録 | エクササイズ検索、セット追加（レップ数・重量入力）、タイマー |
| 体組成グラフ | 折れ線グラフ（体重・体脂肪率）、期間セレクター（1W / 1M / 3M / 1Y）|
| 目標設定 | 目標種別選択、数値入力、期日設定、進捗バー表示 |

---

## 7. 認証フロー

```
1. ユーザーがメール + パスワードでログイン
2. サーバーが access_token (15分) + refresh_token (30日) を返す
3. クライアントは Secure Storage (Expo SecureStore) にトークンを保存
4. API リクエスト時に Authorization: Bearer <access_token> を付与
5. 401 受信時、refresh_token で自動更新（Axios インターセプター）
6. refresh_token も期限切れの場合、ログイン画面へリダイレクト
```

---

## 8. オフライン対応方針

フィットネスアプリはジム圏外での使用も想定されるため、基本的なオフライン対応を実装する。

- **ローカル DB**: WatermelonDB（React Native 向け高性能 DB）
- **同期戦略**: バックグラウンドで差分同期（接続回復時に自動 push/pull）
- **競合解決**: `updated_at` タイムスタンプによる last-write-wins

---

## 9. セキュリティ考慮点

- パスワード: bcrypt ハッシュ（salt rounds: 12）
- HTTPS 強制（HSTS）
- JWT: RS256 署名（非対称鍵）
- refresh token: DB に保存し、ログアウト時に無効化（Rotation 方式）
- レートリミット: FastAPI + slowapi（ログイン: 5回/分）
- 入力バリデーション: Pydantic の strict モード
- CORS: モバイルアプリオリジンのみ許可

---

## 10. 開発フェーズ計画

### Phase 1（MVP）: 4週間

- [ ] 認証（登録・ログイン）
- [ ] ワークアウト記録（セッション・セット管理）
- [ ] 体重記録（シンプルなグラフ）
- [ ] 基本的な目標設定

### Phase 2（強化）: 3週間

- [ ] 体脂肪率トラッキング + 詳細グラフ
- [ ] 目標進捗の詳細表示
- [ ] エクササイズライブラリ（カスタム追加）
- [ ] プッシュ通知（ワークアウトリマインダー）

### Phase 3（拡張）: 3週間

- [ ] オフライン対応（WatermelonDB 同期）
- [ ] Apple HealthKit / Google Fit 連携
- [ ] ソーシャル機能（フレンドと進捗共有）
- [ ] App Store / Google Play ストア申請

---

## 11. 主な技術的リスクと対策

| リスク | 対策 |
|--------|------|
| iOS / Android 挙動差異 | Expo マネージドワークフロー採用でネイティブ差異を吸収。差異が大きい場合は Platform.OS で分岐 |
| HealthKit / Google Fit 連携の複雑さ | Phase 3 以降に先送り。MVP では手動入力のみ |
| オフライン同期の競合 | 初期は last-write-wins。複雑化した場合は CRDT 導入を検討 |
| 体重データの単位（kg/lbs）| ユーザー設定で切り替え可能にし、DB は kg で正規化保存 |

---

## 12. 次のステップ（推奨アクション）

1. **要件確認**: 対象ユーザー層（初心者 vs アスリート）と初期リリーススコープの確定
2. **API 詳細設計**: OpenAPI スキーマ（`/docs`）を先行作成しフロントとの契約を固める
3. **DB マイグレーション環境構築**: Alembic + Docker Compose で即時セットアップ
4. **UI モックアップ**: Figma でメイン画面のワイヤーフレーム作成（ワークアウト記録 / グラフ画面を優先）
5. **CI パイプライン**: GitHub Actions で lint・型チェック・テストを自動実行
