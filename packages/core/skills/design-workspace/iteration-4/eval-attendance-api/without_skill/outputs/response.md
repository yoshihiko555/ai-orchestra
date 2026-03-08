# 勤怠管理システム API 設計 & データモデル設計

## 概要

出退勤打刻・勤怠一覧取得・月次レポート生成の3機能を中心とした REST API の基本設計をまとめます。

---

## 1. データモデル設計

### ER 図（概念）

```
employees (社員)
  └─< attendances (勤怠打刻)
  └─< monthly_reports (月次レポート)
```

---

### テーブル定義

#### `employees`（社員）

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | 社員ID |
| `employee_code` | VARCHAR(20) | UNIQUE, NOT NULL | 社員番号 |
| `name` | VARCHAR(100) | NOT NULL | 氏名 |
| `email` | VARCHAR(255) | UNIQUE, NOT NULL | メールアドレス |
| `department_id` | UUID | FK → departments | 所属部署 |
| `created_at` | TIMESTAMP | NOT NULL | 登録日時 |
| `updated_at` | TIMESTAMP | NOT NULL | 更新日時 |

---

#### `attendances`（勤怠打刻）

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | 打刻ID |
| `employee_id` | UUID | FK → employees, NOT NULL | 社員ID |
| `clock_in_at` | TIMESTAMP | NULL | 出勤打刻日時 |
| `clock_out_at` | TIMESTAMP | NULL | 退勤打刻日時 |
| `work_date` | DATE | NOT NULL | 勤務日（打刻の基準日） |
| `status` | ENUM | NOT NULL | `working` / `finished` / `absent` |
| `note` | TEXT | NULL | 備考（修正理由等） |
| `created_at` | TIMESTAMP | NOT NULL | 作成日時 |
| `updated_at` | TIMESTAMP | NOT NULL | 更新日時 |

- `(employee_id, work_date)` に UNIQUE 制約（1日1レコード）
- `clock_out_at >= clock_in_at` のチェック制約

---

#### `monthly_reports`（月次レポート）

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | レポートID |
| `employee_id` | UUID | FK → employees, NOT NULL | 社員ID |
| `year` | INT | NOT NULL | 対象年 |
| `month` | INT | NOT NULL | 対象月（1-12） |
| `total_work_days` | INT | NOT NULL | 総勤務日数 |
| `total_work_minutes` | INT | NOT NULL | 総勤務時間（分） |
| `overtime_minutes` | INT | NOT NULL | 残業時間（分） |
| `absent_days` | INT | NOT NULL | 欠勤日数 |
| `generated_at` | TIMESTAMP | NOT NULL | レポート生成日時 |
| `status` | ENUM | NOT NULL | `draft` / `confirmed` |

- `(employee_id, year, month)` に UNIQUE 制約

---

#### `departments`（部署）※参照テーブル

| カラム名 | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | UUID | PK | 部署ID |
| `name` | VARCHAR(100) | NOT NULL | 部署名 |
| `created_at` | TIMESTAMP | NOT NULL | 作成日時 |

---

## 2. API 設計

### ベース URL

```
https://api.example.com/v1
```

### 認証

- Bearer Token（JWT）をすべてのエンドポイントで必須
- `Authorization: Bearer <token>` ヘッダー

---

### 2-1. 出退勤打刻

#### POST `/attendances/clock-in` — 出勤打刻

**説明**: ログイン中の社員が出勤打刻を行う。

**リクエスト**
```json
{
  "work_date": "2026-03-08",   // 省略時はサーバー側の現在日付
  "note": "テレワーク"          // 任意
}
```

**レスポンス** `201 Created`
```json
{
  "id": "att-uuid-xxxx",
  "employee_id": "emp-uuid-xxxx",
  "work_date": "2026-03-08",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": null,
  "status": "working",
  "note": "テレワーク",
  "created_at": "2026-03-08T09:00:01+09:00"
}
```

**エラーケース**

| HTTP Status | エラーコード | 説明 |
|---|---|---|
| `409 Conflict` | `ALREADY_CLOCKED_IN` | 当日既に出勤打刻済み |
| `400 Bad Request` | `INVALID_DATE` | 不正な日付形式 |

---

#### POST `/attendances/clock-out` — 退勤打刻

**説明**: 出勤打刻済みの社員が退勤打刻を行う。

**リクエスト**
```json
{
  "work_date": "2026-03-08",   // 省略時はサーバー側の現在日付
  "note": "定時退社"            // 任意
}
```

**レスポンス** `200 OK`
```json
{
  "id": "att-uuid-xxxx",
  "employee_id": "emp-uuid-xxxx",
  "work_date": "2026-03-08",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": "2026-03-08T18:00:00+09:00",
  "status": "finished",
  "work_minutes": 540,
  "note": "定時退社",
  "updated_at": "2026-03-08T18:00:01+09:00"
}
```

**エラーケース**

| HTTP Status | エラーコード | 説明 |
|---|---|---|
| `404 Not Found` | `NOT_CLOCKED_IN` | 出勤打刻がない |
| `409 Conflict` | `ALREADY_CLOCKED_OUT` | 既に退勤打刻済み |

---

#### GET `/attendances/today` — 当日の打刻状況確認

**説明**: ログイン中の社員の当日打刻状況を取得する。

**レスポンス** `200 OK`
```json
{
  "work_date": "2026-03-08",
  "status": "working",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": null,
  "work_minutes": null
}
```

---

### 2-2. 勤怠一覧取得

#### GET `/attendances` — 勤怠一覧

**説明**: 指定期間の勤怠記録を一覧取得する。

**クエリパラメータ**

| パラメータ | 型 | 必須 | 説明 |
|---|---|---|---|
| `employee_id` | UUID | 任意 | 特定社員に絞り込み（管理者のみ指定可） |
| `from` | DATE | 任意 | 開始日（例: `2026-03-01`） |
| `to` | DATE | 任意 | 終了日（例: `2026-03-31`） |
| `status` | STRING | 任意 | `working` / `finished` / `absent` |
| `page` | INT | 任意 | ページ番号（デフォルト: 1） |
| `per_page` | INT | 任意 | 1ページあたりの件数（デフォルト: 20、最大: 100） |

**レスポンス** `200 OK`
```json
{
  "data": [
    {
      "id": "att-uuid-xxxx",
      "employee_id": "emp-uuid-xxxx",
      "employee_name": "山田 太郎",
      "work_date": "2026-03-08",
      "clock_in_at": "2026-03-08T09:00:00+09:00",
      "clock_out_at": "2026-03-08T18:00:00+09:00",
      "work_minutes": 540,
      "status": "finished",
      "note": null
    }
  ],
  "pagination": {
    "total": 23,
    "page": 1,
    "per_page": 20,
    "total_pages": 2
  }
}
```

---

#### GET `/attendances/{attendance_id}` — 勤怠詳細

**レスポンス** `200 OK`
```json
{
  "id": "att-uuid-xxxx",
  "employee_id": "emp-uuid-xxxx",
  "employee_name": "山田 太郎",
  "work_date": "2026-03-08",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": "2026-03-08T18:00:00+09:00",
  "work_minutes": 540,
  "status": "finished",
  "note": null,
  "created_at": "2026-03-08T09:00:01+09:00",
  "updated_at": "2026-03-08T18:00:01+09:00"
}
```

---

### 2-3. 月次レポート生成

#### POST `/reports/monthly` — 月次レポート生成（非同期）

**説明**: 指定した社員・年月の月次レポートを生成する。処理は非同期で行い、ジョブIDを返す。

**リクエスト**
```json
{
  "employee_id": "emp-uuid-xxxx",  // 省略時はログイン中の社員
  "year": 2026,
  "month": 3
}
```

**レスポンス** `202 Accepted`
```json
{
  "job_id": "job-uuid-xxxx",
  "status": "pending",
  "employee_id": "emp-uuid-xxxx",
  "year": 2026,
  "month": 3,
  "created_at": "2026-03-08T18:00:00+09:00"
}
```

---

#### GET `/reports/monthly/{job_id}/status` — レポート生成ステータス確認

**レスポンス** `200 OK`（生成中）
```json
{
  "job_id": "job-uuid-xxxx",
  "status": "processing"
}
```

**レスポンス** `200 OK`（完了）
```json
{
  "job_id": "job-uuid-xxxx",
  "status": "completed",
  "report_id": "report-uuid-xxxx"
}
```

---

#### GET `/reports/monthly/{report_id}` — 月次レポート取得

**レスポンス** `200 OK`
```json
{
  "id": "report-uuid-xxxx",
  "employee_id": "emp-uuid-xxxx",
  "employee_name": "山田 太郎",
  "year": 2026,
  "month": 3,
  "total_work_days": 20,
  "total_work_minutes": 9600,
  "total_work_hours": 160.0,
  "overtime_minutes": 600,
  "overtime_hours": 10.0,
  "absent_days": 1,
  "status": "confirmed",
  "generated_at": "2026-03-08T18:05:00+09:00",
  "daily_summary": [
    {
      "work_date": "2026-03-01",
      "clock_in_at": "2026-03-01T09:00:00+09:00",
      "clock_out_at": "2026-03-01T18:00:00+09:00",
      "work_minutes": 480,
      "overtime_minutes": 0,
      "status": "finished"
    }
  ]
}
```

---

#### GET `/reports/monthly` — 月次レポート一覧

**クエリパラメータ**

| パラメータ | 型 | 必須 | 説明 |
|---|---|---|---|
| `employee_id` | UUID | 任意 | 特定社員に絞り込み |
| `year` | INT | 任意 | 対象年 |
| `month` | INT | 任意 | 対象月 |
| `status` | STRING | 任意 | `draft` / `confirmed` |

---

## 3. エラーレスポンス共通フォーマット

```json
{
  "error": {
    "code": "ALREADY_CLOCKED_IN",
    "message": "本日はすでに出勤打刻済みです。",
    "details": {}
  }
}
```

---

## 4. 設計上の考慮事項

### タイムゾーン
- すべての日時は ISO 8601 形式（`+09:00` を付与）でやり取りする
- DB 保存は UTC、レスポンス時に JST 変換

### 権限モデル（ロールの想定）
| ロール | 自分の打刻 | 他社員の打刻参照 | レポート生成 |
|---|---|---|---|
| `employee`（一般社員） | 可 | 不可 | 自分のみ |
| `manager`（管理者） | 可 | 可 | 部下分 |
| `admin`（システム管理者） | 可 | 可 | 全社員分 |

### 月次レポートの生成ロジック（想定）
1. 対象月の `attendances` レコードを集計
2. 勤務時間 = `clock_out_at - clock_in_at`（休憩時間は別途設定で除外）
3. 法定労働時間（8時間/日）を超えた分を残業時間として計上
4. `work_date` が `absent` のレコードを欠勤日としてカウント

### 将来の拡張ポイント
- 打刻修正申請ワークフロー（`/attendances/{id}/correction-requests`）
- 休暇管理との連携（有給・代休等）
- 勤務形態対応（フレックス・シフト）
- CSV / PDF エクスポート（`GET /reports/monthly/{id}/export?format=csv`）

---

## 5. エンドポイント一覧（サマリ）

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/attendances/clock-in` | 出勤打刻 |
| `POST` | `/attendances/clock-out` | 退勤打刻 |
| `GET` | `/attendances/today` | 当日打刻状況 |
| `GET` | `/attendances` | 勤怠一覧 |
| `GET` | `/attendances/{id}` | 勤怠詳細 |
| `POST` | `/reports/monthly` | 月次レポート生成（非同期） |
| `GET` | `/reports/monthly/{job_id}/status` | 生成ジョブ状態確認 |
| `GET` | `/reports/monthly/{id}` | 月次レポート取得 |
| `GET` | `/reports/monthly` | 月次レポート一覧 |
