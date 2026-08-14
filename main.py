import os
import json
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import jwt
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr

app = FastAPI()
password_hash = PasswordHash.recommended()

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set")


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    store_name TEXT,
                    total INTEGER NOT NULL DEFAULT 0,
                    receipt_date DATE,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id BIGSERIAL PRIMARY KEY,
                    receipt_id BIGINT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_receipts_user_date ON receipts(user_id, receipt_date DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_items_receipt_id ON items(receipt_id)")
        conn.commit()


init_db()


class ItemSchema(BaseModel):
    name: str
    price: int
    category: str


class ReceiptAnalysisSchema(BaseModel):
    store_name: Optional[str] = "不明"
    total: int
    receipt_date: Optional[str] = None
    items: List[ItemSchema]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ReceiptUpdateItem(BaseModel):
    name: str
    price: int
    category: str


class ReceiptUpdate(BaseModel):
    store_name: str
    total: int
    receipt_date: str
    items: List[ReceiptUpdateItem]


def create_token(user_id: int, email: str):
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ログインが必要です")

    try:
        payload = jwt.decode(
            authorization[7:],
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
        )
        return {"id": int(payload["sub"]), "email": payload["email"]}
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ログイン情報が無効です")


@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/register")
async def register(payload: RegisterRequest):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上にしてください")

    email = payload.email.lower()
    hashed = password_hash.hash(payload.password)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                    (email, hashed),
                )
                user_id = cur.fetchone()["id"]
            conn.commit()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="このメールアドレスは既に登録されています")

    return {
        "status": "success",
        "token": create_token(user_id, email),
        "email": email,
    }


@app.post("/api/login")
async def login(payload: LoginRequest):
    email = payload.email.lower()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash FROM users WHERE email = %s",
                (email,),
            )
            user = cur.fetchone()

    if not user or not password_hash.verify(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="メールアドレスまたはパスワードが違います")

    return {
        "status": "success",
        "token": create_token(user["id"], user["email"]),
        "email": user["email"],
    }


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"]}


@app.post("/analyze-receipt")
async def analyze_receipt(
    file: UploadFile = File(...),
    user=Depends(current_user),
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="画像ファイルを選択してください")

    contents = await file.read()
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = (
        "レシートの画像を解析し、以下の情報を抽出してください。\n"
        "1. 店舗名 (store_name)\n"
        "2. 合計金額 (total)\n"
        "3. レシートに記載されている購入日付 (receipt_date)。"
        "必ず YYYY-MM-DD 形式で返してください。"
        "西暦が書かれていない場合は今年の西暦を推測してください。"
        "日付を読み取れない場合は null にしてください。\n"
        "4. 各購入品目の名称、価格、最も適したカテゴリを抽出してください。"
    )

    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[
                types.Part.from_bytes(
                    data=contents,
                    mime_type=file.content_type,
                ),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptAnalysisSchema,
            ),
        )

        data = json.loads(response.text)
        receipt_date = data.get("receipt_date") or datetime.now().strftime("%Y-%m-%d")

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO receipts (user_id, store_name, total, receipt_date)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user["id"],
                        data.get("store_name", "不明"),
                        data.get("total", 0),
                        receipt_date,
                    ),
                )
                receipt_id = cur.fetchone()["id"]

                for item in data.get("items", []):
                    cur.execute(
                        """
                        INSERT INTO items (receipt_id, name, price, category)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            receipt_id,
                            item.get("name", ""),
                            item.get("price", 0),
                            item.get("category", "その他"),
                        ),
                    )

            conn.commit()

        data["receipt_date"] = receipt_date
        data["id"] = receipt_id

        return {"status": "success", "data": data}

    except Exception as e:
        print(f"Error during analysis: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


@app.get("/analytics")
async def get_analytics(
    period_type: str = "all",
    value: str = "",
    user=Depends(current_user),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT EXTRACT(YEAR FROM receipt_date)::int AS year
                FROM receipts
                WHERE user_id = %s AND receipt_date IS NOT NULL
                ORDER BY year DESC
                """,
                (user["id"],),
            )
            years = [str(row["year"]) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT TO_CHAR(receipt_date, 'YYYY-MM') AS month
                FROM receipts
                WHERE user_id = %s AND receipt_date IS NOT NULL
                ORDER BY month DESC
                """,
                (user["id"],),
            )
            months = [row["month"] for row in cur.fetchall()]

            where_clause = "WHERE r.user_id = %s"
            params = [user["id"]]

            if period_type == "year" and value:
                where_clause += " AND EXTRACT(YEAR FROM r.receipt_date)::int = %s"
                params.append(int(value))

            elif period_type == "month" and value:
                where_clause += " AND TO_CHAR(r.receipt_date, 'YYYY-MM') = %s"
                params.append(value)

            elif period_type == "week" and value:
                try:
                    selected = date.fromisoformat(value)
                except ValueError:
                    raise HTTPException(status_code=400, detail="日付形式が不正です")

                monday = selected - timedelta(days=selected.weekday())
                sunday = monday + timedelta(days=6)

                where_clause += " AND r.receipt_date BETWEEN %s AND %s"
                params.extend([monday, sunday])

            cur.execute(
                f"""
                SELECT i.category, COALESCE(SUM(i.price), 0) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                {where_clause}
                GROUP BY i.category
                ORDER BY total DESC
                """,
                params,
            )

            category_data = [
                {
                    "category": row["category"],
                    "total": int(row["total"]),
                }
                for row in cur.fetchall()
            ]

    return {
        "by_category": category_data,
        "available_years": years,
        "available_months": months,
    }


@app.get("/receipts")
async def get_receipts(user=Depends(current_user)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, store_name, total, receipt_date
                FROM receipts
                WHERE user_id = %s
                ORDER BY receipt_date DESC NULLS LAST, id DESC
                LIMIT 20
                """,
                (user["id"],),
            )
            receipts = cur.fetchall()

    return {"receipts": receipts}


@app.get("/receipts/{receipt_id}")
async def get_receipt(
    receipt_id: int,
    user=Depends(current_user),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, store_name, total, receipt_date
                FROM receipts
                WHERE id = %s AND user_id = %s
                """,
                (receipt_id, user["id"]),
            )
            receipt = cur.fetchone()

            if not receipt:
                raise HTTPException(status_code=404, detail="レシートが見つかりません")

            cur.execute(
                """
                SELECT id, name, price, category
                FROM items
                WHERE receipt_id = %s
                ORDER BY id
                """,
                (receipt_id,),
            )
            receipt["items"] = cur.fetchall()

    return receipt


@app.put("/receipts/{receipt_id}")
async def update_receipt(
    receipt_id: int,
    payload: ReceiptUpdate,
    user=Depends(current_user),
):
    try:
        date.fromisoformat(payload.receipt_date)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="購入日は YYYY-MM-DD 形式で入力してください",
        )

    if payload.total < 0:
        raise HTTPException(status_code=400, detail="合計金額は0以上にしてください")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE receipts
                SET store_name = %s, total = %s, receipt_date = %s
                WHERE id = %s AND user_id = %s
                """,
                (
                    payload.store_name,
                    payload.total,
                    payload.receipt_date,
                    receipt_id,
                    user["id"],
                ),
            )

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="レシートが見つかりません")

            cur.execute(
                "DELETE FROM items WHERE receipt_id = %s",
                (receipt_id,),
            )

            for item in payload.items:
                cur.execute(
                    """
                    INSERT INTO items (receipt_id, name, price, category)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        receipt_id,
                        item.name,
                        item.price,
                        item.category,
                    ),
                )

        conn.commit()

    return {"status": "success"}


@app.delete("/receipts/{receipt_id}")
async def delete_receipt(
    receipt_id: int,
    user=Depends(current_user),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM receipts
                WHERE id = %s AND user_id = %s
                """,
                (receipt_id, user["id"]),
            )

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="レシートが見つかりません")

        conn.commit()

    return {"status": "success"}
