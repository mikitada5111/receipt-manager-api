import os
import json
import sqlite3
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# --- データベース初期化 ---
DB_NAME = "receipts.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # receipt_date カラムが存在しない場合はテーブル作成時に追加
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT,
            total INTEGER,
            receipt_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            name TEXT,
            price INTEGER,
            category TEXT,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
        )
    ''')
    
    # 既存DBの場合、receipt_date カラムが無ければ追加するマイグレーション処理
    cursor.execute("PRAGMA table_info(receipts)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'receipt_date' not in columns:
        cursor.execute("ALTER TABLE receipts ADD COLUMN receipt_date TEXT")
        # 既存データの receipt_date を created_at の日付で更新
        cursor.execute("UPDATE receipts SET receipt_date = DATE(created_at) WHERE receipt_date IS NULL")

    conn.commit()
    conn.close()

init_db()

# --- Pydantic スキーマ定義 ---
class ItemSchema(BaseModel):
    name: str
    price: int
    category: str

class ReceiptAnalysisSchema(BaseModel):
    store_name: Optional[str] = "不明"
    total: int
    receipt_date: Optional[str] = None  # レシートに記載されている日付 (YYYY-MM-DD)
    items: List[ItemSchema]

# --- Gemini API 設定 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/analyze-receipt")
async def analyze_receipt(file: UploadFile = File(...)):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")

    contents = await file.read()
    
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = (
        "レシートの画像を解析し、以下の情報を抽出してください。\n"
        "1. 店舗名 (store_name)\n"
        "2. 合計金額 (total)\n"
        "3. レシートに記載されている購入日付 (receipt_date)。必ず 'YYYY-MM-DD' 形式（例: 2024-03-15）で抽出してください。西暦が書かれていない場合は、今年の西暦と推測してください。日付が読み取れない場合は null にしてください。\n"
        "4. 各購入品目の名称 (name)、価格 (price)、および最も適したカテゴリ (category - 食費, 日用品, 交通費, 衣服, 美容・健康, 娯楽, その他 など)。"
    )

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(
                    data=contents,
                    mime_type=file.content_type or "image/jpeg",
                ),
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptAnalysisSchema,
            ),
        )

        data = json.loads(response.text)
        
        # レシート日付の確定（解析できなかった場合は本日日付）
        r_date = data.get("receipt_date")
        if not r_date:
            r_date = datetime.now().strftime("%Y-%m-%d")

        # DBに保存
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute(
            "INSERT INTO receipts (store_name, total, receipt_date) VALUES (?, ?, ?)",
            (data.get("store_name", "不明"), data.get("total", 0), r_date)
        )
        receipt_id = cursor.lastrowid

        for item in data.get("items", []):
            cursor.execute(
                "INSERT INTO items (receipt_id, name, price, category) VALUES (?, ?, ?, ?)",
                (receipt_id, item.get("name"), item.get("price"), item.get("category"))
            )

        conn.commit()
        conn.close()

        data["receipt_date"] = r_date
        return {"status": "success", "data": data}

    except Exception as e:
        print(f"Error during analysis: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/analytics")
async def get_analytics(period_type: str = "all", value: str = ""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 利用可能な年・月一覧を取得 (receipt_date 基準)
    cursor.execute("SELECT DISTINCT strftime('%Y', receipt_date) FROM receipts WHERE receipt_date IS NOT NULL ORDER BY 1 DESC")
    years = [r[0] for r in cursor.fetchall() if r[0]]

    cursor.execute("SELECT DISTINCT strftime('%Y-%m', receipt_date) FROM receipts WHERE receipt_date IS NOT NULL ORDER BY 1 DESC")
    months = [r[0] for r in cursor.fetchall() if r[0]]

    # フィルタリング条件の構築
    where_clause = ""
    params = []

    if period_type == "year" and value:
        where_clause = "WHERE strftime('%Y', r.receipt_date) = ?"
        params.append(value)
    elif period_type == "month" and value:
        where_clause = "WHERE strftime('%Y-%m', r.receipt_date) = ?"
        params.append(value)
    elif period_type == "week" and value:
        where_clause = "WHERE r.receipt_date BETWEEN date(?, 'weekday 1', '-7 days') AND date(?, 'weekday 1', '-1 days')"
        params.extend([value, value])

    query = f'''
        SELECT i.category, SUM(i.price) as total
        FROM items i
        JOIN receipts r ON i.receipt_id = r.id
        {where_clause}
        GROUP BY i.category
        ORDER BY total DESC
    '''
    cursor.execute(query, params)
    category_data = [{"category": row[0], "total": row[1]} for row[1] in cursor.fetchall()]

    conn.close()

    return {
        "by_category": category_data,
        "available_years": years,
        "available_months": months
    }

@app.get("/receipts")
async def get_receipts():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # receipt_date を優先表示（無ければ created_at の日付）
    cursor.execute("""
        SELECT id, store_name, total, COALESCE(receipt_date, DATE(created_at)) as receipt_date 
        FROM receipts 
        ORDER BY receipt_date DESC, id DESC 
        LIMIT 20
    """)
    receipts = [
        {"id": row[0], "store_name": row[1], "total": row[2], "receipt_date": row[3]}
        for row in cursor.fetchall()
    ]
    conn.close()
    return {"receipts": receipts}

@app.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}