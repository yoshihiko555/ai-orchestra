# 勤怠管理システム API 設計（基本設計）

## 概要

出退勤打刻・勤怠一覧取得・月次レポート生成の3機能を中心とした REST API の基本設計です。

---

## 1. データモデル設計

### Employee（従業員）

| カラム名 | 型 | 説明 |
|---|---|---|
| `id` | UUID | 主キー |
| `employee_code` | VARCHAR(20) | 社員番号（ユニーク） |
| `name` | VARCHAR(100) | 氏名 |
| `email` | VARCHAR(255) | メールアドレス（ユニーク） |
| `department_id` | UUID | 部署 ID（FK） |
| `employment_type` | ENUM | 雇用形態: `full_time`, `part_time`, `contract` |
| `created_at` | TIMESTAMP | 作成日時 |
| `updated_at` | TIMESTAMP | 更新日時 |

### Department（部署）

| カラム名 | 型 | 説明 |
|---|---|---|
| `id` | UUID | 主キー |
| `name` | VARCHAR(100) | 部署名 |
| `created_at` | TIMESTAMP | 作成日時 |

### AttendanceRecord（勤怠打刻レコード）

| カラム名 | 型 | 説明 |
|---|---|---|
| `id` | UUID | 主キー |
| `employee_id` | UUID | 従業員 ID（FK） |
| `date` | DATE | 打刻日 |
| `clock_in_at` | TIMESTAMP | 出勤打刻日時 |
| `clock_out_at` | TIMESTAMP | 退勤打刻日時（NULL = 未打刻） |
| `status` | ENUM | `present`, `absent`, `late`, `early_leave` |
| `note` | TEXT | 備考 |
| `created_at` | TIMESTAMP | 作成日時 |
| `updated_at` | TIMESTAMP | 更新日時 |

**制約:**
- `(employee_id, date)` に UNIQUE 制約（1日1レコード）
- `clock_out_at > clock_in_at` のチェック制約

### MonthlyReport（月次レポート）

| カラム名 | 型 | 説明 |
|---|---|---|
| `id` | UUID | 主キー |
| `employee_id` | UUID | 従業員 ID（FK） |
| `year` | SMALLINT | 対象年 |
| `month` | SMALLINT | 対象月 |
| `total_working_days` | INT | 出勤日数 |
| `total_working_minutes` | INT | 総勤務時間（分） |
| `overtime_minutes` | INT | 残業時間（分） |
| `absent_days` | INT | 欠勤日数 |
| `late_count` | INT | 遅刻回数 |
| `generated_at` | TIMESTAMP | 生成日時 |

**制約:**
- `(employee_id, year, month)` に UNIQUE 制約

---

## 2. API エンドポイント設計

### ベース URL

```
/api/v1
```

### 認証

全エンドポイントで Bearer トークン（JWT）を要求:
```
Authorization: Bearer <token>
```

---

### 2-1. 打刻 API

#### 出勤打刻

```
POST /api/v1/attendance/clock-in
```

**リクエストボディ:**
```json
{
  "employee_id": "uuid",
  "note": "テレワーク"   // optional
}
```

**レスポンス: 201 Created**
```json
{
  "id": "uuid",
  "employee_id": "uuid",
  "date": "2026-03-08",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": null,
  "status": "present",
  "note": "テレワーク"
}
```

**エラーケース:**
- `409 Conflict`: 当日すでに出勤打刻済み

---

#### 退勤打刻

```
POST /api/v1/attendance/clock-out
```

**リクエストボディ:**
```json
{
  "employee_id": "uuid",
  "note": ""   // optional
}
```

**レスポンス: 200 OK**
```json
{
  "id": "uuid",
  "employee_id": "uuid",
  "date": "2026-03-08",
  "clock_in_at": "2026-03-08T09:00:00+09:00",
  "clock_out_at": "2026-03-08T18:00:00+09:00",
  "status": "present",
  "working_minutes": 480
}
```

**エラーケース:**
- `404 Not Found`: 当日の出勤打刻が存在しない
- `409 Conflict`: すでに退勤打刻済み

---

### 2-2. 勤怠一覧取得 API

#### 自分の勤怠一覧

```
GET /api/v1/attendance
```

**クエリパラメータ:**

| パラメータ | 型 | 必須 | 説明 |
|---|---|---|---|
| `from` | DATE (YYYY-MM-DD) | 任意 | 取得開始日（デフォルト: 当月1日） |
| `to` | DATE (YYYY-MM-DD) | 任意 | 取得終了日（デフォルト: 今日） |
| `status` | string | 任意 | フィルタ: `present` / `absent` / `late` / `early_leave` |
| `page` | int | 任意 | ページ番号（デフォルト: 1） |
| `per_page` | int | 任意 | 1ページあたり件数（デフォルト: 31、最大: 100） |

**レスポンス: 200 OK**
```json
{
  "data": [
    {
      "id": "uuid",
      "date": "2026-03-08",
      "clock_in_at": "2026-03-08T09:00:00+09:00",
      "clock_out_at": "2026-03-08T18:00:00+09:00",
      "status": "present",
      "working_minutes": 480,
      "note": ""
    }
  ],
  "pagination": {
    "page": 1,
    "per_page": 31,
    "total": 8,
    "total_pages": 1
  }
}
```

---

#### 管理者用: 全従業員の勤怠一覧

```
GET /api/v1/attendance/employees/{employee_id}
```

クエリパラメータは上記と同様。管理者ロール必須。

---

### 2-3. 月次レポート API

#### 月次レポート生成（または再生成）

```
POST /api/v1/reports/monthly
```

**リクエストボディ:**
```json
{
  "employee_id": "uuid",
  "year": 2026,
  "month": 3
}
```

**処理:**
- 指定月の `AttendanceRecord` を集計して `MonthlyReport` を生成（upsert）
- 非同期処理の場合は `202 Accepted` を返し、`report_id` でポーリング可能にする

**レスポンス: 200 OK（同期処理の場合）**
```json
{
  "id": "uuid",
  "employee_id": "uuid",
  "year": 2026,
  "month": 3,
  "total_working_days": 20,
  "total_working_minutes": 9600,
  "overtime_minutes": 480,
  "absent_days": 1,
  "late_count": 0,
  "generated_at": "2026-03-08T12:00:00+09:00"
}
```

---

#### 月次レポート取得

```
GET /api/v1/reports/monthly/{employee_id}?year=2026&month=3
```

**レスポンス: 200 OK**（上記と同形式）

**エラーケース:**
- `404 Not Found`: レポートが未生成

---

#### 月次レポート一覧（管理者用）

```
GET /api/v1/reports/monthly?year=2026&month=3&department_id=uuid
```

部署・月での絞り込みができる一覧。管理者ロール必須。

---

## 3. 共通設計方針

### エラーレスポンス形式

```json
{
  "error": {
    "code": "ALREADY_CLOCKED_IN",
    "message": "当日すでに出勤打刻済みです",
    "details": {}
  }
}
```

### タイムゾーン

- 打刻日時はすべて **ISO 8601 + JST オフセット (`+09:00`)** で返す
- DB 内部は UTC で保存し、API レスポンス時に変換する

### ステータス遷移（打刻）

```
未打刻
  └─ clock-in → 出勤中
       └─ clock-out → 退勤済み
```

---

## 4. 今後の検討事項

- **打刻修正 API**: `PATCH /api/v1/attendance/{id}`（管理者による承認フロー含む）
- **休暇申請との連携**: 有給・特別休暇との紐付け
- **非同期レポート生成**: 月次レポートの生成をジョブキュー化（データ量が多い場合）
- **CSV/PDF エクスポート**: `GET /api/v1/reports/monthly/{employee_id}/export?format=csv`
- **WebSocket / SSE**: リアルタイムの打刻状況通知

---

## 5. 設計判断メモ

| 判断 | 理由 |
|---|---|
| 1日1レコード（upsert ではなく出退勤を1レコードに格納） | 「打刻修正」や「勤怠集計」がシンプルになる |
| 打刻時刻をサーバー側で記録 | クライアント時刻操作による不正防止 |
| 月次レポートを別テーブルに保存 | 過去レポートの再計算なしに参照可能、監査証跡になる |
| ページネーションのデフォルト per_page=31 | 月単位での取得が主なユースケースのため |
