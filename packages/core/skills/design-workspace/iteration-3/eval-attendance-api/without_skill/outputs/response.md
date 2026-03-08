# 勤怠管理システム API 設計

## 前提・設計方針

- REST API（JSON）
- 認証: JWT Bearer トークン
- タイムゾーン: 全日時は UTC で保存、レスポンスは ISO 8601 形式
- バージョニング: URL パスに `/v1/` を含める

---

## データモデル設計

### テーブル一覧

#### `users` — 社員マスタ

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | 社員 ID |
| `employee_code` | VARCHAR(20) | UNIQUE, NOT NULL | 社員番号 |
| `name` | VARCHAR(100) | NOT NULL | 氏名 |
| `email` | VARCHAR(255) | UNIQUE, NOT NULL | メールアドレス |
| `department_id` | UUID | FK | 所属部署 |
| `role` | ENUM | NOT NULL | `employee` / `manager` / `admin` |
| `created_at` | TIMESTAMP | NOT NULL | 作成日時 |
| `updated_at` | TIMESTAMP | NOT NULL | 更新日時 |

#### `departments` — 部署マスタ

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | 部署 ID |
| `name` | VARCHAR(100) | NOT NULL | 部署名 |
| `manager_id` | UUID | FK (users) | 部署責任者 |

#### `attendance_records` — 勤怠打刻記録

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | レコード ID |
| `user_id` | UUID | FK (users), NOT NULL | 社員 ID |
| `type` | ENUM | NOT NULL | `clock_in` / `clock_out` |
| `timestamp` | TIMESTAMP | NOT NULL | 打刻日時（UTC） |
| `location` | VARCHAR(255) | NULL | 打刻場所（任意） |
| `note` | TEXT | NULL | 備考 |
| `created_at` | TIMESTAMP | NOT NULL | 作成日時 |

インデックス:
- `(user_id, timestamp)` — 特定社員の日次・月次集計に使用
- `(timestamp)` — 全社員の月次レポート生成に使用

#### `monthly_reports` — 月次レポート（集計キャッシュ）

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | レポート ID |
| `user_id` | UUID | FK (users), NOT NULL | 社員 ID |
| `year` | SMALLINT | NOT NULL | 対象年 |
| `month` | SMALLINT | NOT NULL | 対象月 |
| `total_work_minutes` | INTEGER | NOT NULL | 総労働時間（分） |
| `total_overtime_minutes` | INTEGER | NOT NULL | 総残業時間（分） |
| `work_days` | INTEGER | NOT NULL | 出勤日数 |
| `status` | ENUM | NOT NULL | `draft` / `confirmed` |
| `generated_at` | TIMESTAMP | NOT NULL | 生成日時 |
| `confirmed_at` | TIMESTAMP | NULL | 確定日時 |

ユニーク制約: `(user_id, year, month)`

---

## API 設計

### 認証

```
POST /v1/auth/login
POST /v1/auth/logout
POST /v1/auth/refresh
```

---

### 打刻 API

#### 出勤打刻

```
POST /v1/attendance/clock-in
```

**リクエストボディ**

```json
{
  "location": "東京本社",  // 任意
  "note": "テレワーク"      // 任意
}
```

**レスポンス `201 Created`**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "clock_in",
  "timestamp": "2026-03-08T09:00:00Z",
  "location": "東京本社",
  "note": "テレワーク"
}
```

**エラーケース**

| ステータス | 条件 |
|---|---|
| `409 Conflict` | 既に当日の出勤打刻が存在する |
| `401 Unauthorized` | 未認証 |

---

#### 退勤打刻

```
POST /v1/attendance/clock-out
```

**リクエストボディ**

```json
{
  "location": "東京本社",  // 任意
  "note": ""               // 任意
}
```

**レスポンス `201 Created`**

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "type": "clock_out",
  "timestamp": "2026-03-08T18:30:00Z",
  "location": "東京本社"
}
```

**エラーケース**

| ステータス | 条件 |
|---|---|
| `409 Conflict` | 当日の出勤打刻がない状態で退勤しようとした |
| `409 Conflict` | 既に当日の退勤打刻が存在する |

---

### 勤怠一覧 API

#### 自分の勤怠一覧取得

```
GET /v1/attendance?year=2026&month=3
```

**クエリパラメータ**

| パラメータ | 型 | 必須 | 説明 |
|---|---|---|---|
| `year` | integer | 任意 | 対象年（デフォルト: 当年） |
| `month` | integer | 任意 | 対象月（デフォルト: 当月） |
| `date` | string (YYYY-MM-DD) | 任意 | 特定日のみ取得 |

**レスポンス `200 OK`**

```json
{
  "user_id": "user-uuid",
  "year": 2026,
  "month": 3,
  "records": [
    {
      "date": "2026-03-04",
      "clock_in": {
        "id": "record-uuid-1",
        "timestamp": "2026-03-04T09:05:00Z",
        "location": "東京本社"
      },
      "clock_out": {
        "id": "record-uuid-2",
        "timestamp": "2026-03-04T18:32:00Z",
        "location": "東京本社"
      },
      "work_minutes": 567,
      "overtime_minutes": 27
    }
  ],
  "summary": {
    "work_days": 15,
    "total_work_minutes": 7200,
    "total_overtime_minutes": 180
  }
}
```

---

#### 管理者向け: 部下の勤怠一覧取得

```
GET /v1/users/{user_id}/attendance?year=2026&month=3
```

- 権限: `manager`（自分の部署の社員のみ）または `admin`
- レスポンス形式は上記と同一

---

### 月次レポート API

#### 月次レポート生成（または再生成）

```
POST /v1/reports/monthly
```

**リクエストボディ**

```json
{
  "year": 2026,
  "month": 3,
  "user_id": "user-uuid"  // 管理者が他ユーザー向けに生成する場合のみ指定
}
```

**レスポンス `202 Accepted`**（非同期処理の場合）

```json
{
  "report_id": "report-uuid",
  "status": "generating",
  "estimated_at": "2026-03-08T10:01:00Z"
}
```

> 集計対象データが少ない場合は同期処理（`201 Created` + レポート本体）でも可。
> スケール・要件に応じて選択する。

---

#### 月次レポート取得

```
GET /v1/reports/monthly?year=2026&month=3&user_id=user-uuid
```

**クエリパラメータ**

| パラメータ | 型 | 必須 | 説明 |
|---|---|---|---|
| `year` | integer | 必須 | 対象年 |
| `month` | integer | 必須 | 対象月 |
| `user_id` | string | 任意 | 管理者が他ユーザーのレポートを参照する場合 |

**レスポンス `200 OK`**

```json
{
  "report_id": "report-uuid",
  "user_id": "user-uuid",
  "year": 2026,
  "month": 3,
  "status": "confirmed",
  "generated_at": "2026-03-08T10:01:30Z",
  "confirmed_at": "2026-03-10T09:00:00Z",
  "summary": {
    "work_days": 20,
    "total_work_minutes": 9600,
    "total_overtime_minutes": 300
  },
  "daily_records": [
    {
      "date": "2026-03-04",
      "work_minutes": 487,
      "overtime_minutes": 0
    }
  ]
}
```

---

#### レポートの確定

```
PATCH /v1/reports/monthly/{report_id}/confirm
```

- 権限: `manager` または `admin`
- `status` を `draft` → `confirmed` に変更

**レスポンス `200 OK`**

```json
{
  "report_id": "report-uuid",
  "status": "confirmed",
  "confirmed_at": "2026-03-10T09:00:00Z"
}
```

---

## エンドポイント一覧

| メソッド | パス | 説明 | 権限 |
|---|---|---|---|
| POST | `/v1/auth/login` | ログイン | 全員 |
| POST | `/v1/auth/logout` | ログアウト | 認証済み |
| POST | `/v1/auth/refresh` | トークン更新 | 認証済み |
| POST | `/v1/attendance/clock-in` | 出勤打刻 | employee 以上 |
| POST | `/v1/attendance/clock-out` | 退勤打刻 | employee 以上 |
| GET | `/v1/attendance` | 自分の勤怠一覧 | employee 以上 |
| GET | `/v1/users/{user_id}/attendance` | 指定ユーザーの勤怠一覧 | manager / admin |
| POST | `/v1/reports/monthly` | 月次レポート生成 | employee 以上 |
| GET | `/v1/reports/monthly` | 月次レポート取得 | employee 以上 |
| PATCH | `/v1/reports/monthly/{report_id}/confirm` | レポート確定 | manager / admin |

---

## 設計上の検討事項（次フェーズで要確認）

- **打刻修正フロー**: 打刻ミスの修正を誰が承認するか（本人申請 + 管理者承認のワークフローが必要か）
- **残業時間の定義**: 法定労働時間（8時間）超えで計算するか、所定労働時間（会社設定）超えで計算するか
- **月次レポートの同期/非同期**: データ量が多い場合は非同期処理（ジョブキュー）を検討
- **打刻の二重防止**: 短時間での連続打刻（操作ミス）をどう扱うか（冪等キー or 最小間隔制限）
- **タイムゾーン管理**: 拠点が複数ある場合、社員ごとのタイムゾーン設定が必要か
