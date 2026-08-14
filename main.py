import os
import sqlite3
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY が設定されていません。")

app = FastAPI()
client = genai.Client(api_key=GEMINI_API_KEY)
DB_PATH = "receipts.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT,
            total_amount INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            name TEXT,
            price INTEGER,
            category TEXT,
            FOREIGN KEY (receipt_id) REFERENCES receipts (id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

init_db()

class Item(BaseModel):
    name: str = Field(description="商品名")
    price: int = Field(description="価格（数値のみ）")
    category: str = Field(description="カテゴリ（食費, 日用品, 衣服, 交通費, 趣味・娯楽, その他 のいずれか）")

class ReceiptData(BaseModel):
    store_name: str = Field(description="店舗名", default="")
    items: list[Item] = Field(description="購入商品のリスト")
    total: int = Field(description="合計金額（数値のみ）", default=0)

@app.get("/")
def read_index():
    return FileResponse("index.html")

@app.post("/analyze-receipt")
async def analyze_receipt(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="画像ファイルをアップロードしてください。")

    try:
        contents = await file.read()
        response = client.models.generate_content(
            model='gemini-flash-latest',
            contents=[
                types.Part.from_bytes(data=contents, mime_type=file.content_type),
                "レシート画像から店舗名、商品リスト、および合計金額を抽出し、各商品のカテゴリを推測して分類してください。"
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptData,
                temperature=0.0
            )
        )
        receipt_info: ReceiptData = response.parsed

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO receipts (store_name, total_amount) VALUES (?, ?)", (receipt_info.store_name, receipt_info.total))
        receipt_id = cursor.lastrowid

        for item in receipt_info.items:
            cursor.execute("INSERT INTO receipt_items (receipt_id, name, price, category) VALUES (?, ?, ?, ?)", (receipt_id, item.name, item.price, item.category))

        conn.commit()
        conn.close()
        return {"status": "success", "receipt_id": receipt_id, "data": receipt_info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析エラー: {str(e)}")

@app.get("/receipts")
def get_receipts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, store_name, total_amount, created_at FROM receipts ORDER BY id DESC")
    receipts = cursor.fetchall()
    
    result = []
    for r in receipts:
        r_id, store, total, created_at = r
        cursor.execute("SELECT name, price, category FROM receipt_items WHERE receipt_id = ?", (r_id,))
        items = [{"name": row[0], "price": row[1], "category": row[2]} for row in cursor.fetchall()]
        result.append({"id": r_id, "store_name": store, "total": total, "created_at": created_at, "items": items})
        
    conn.close()
    return {"receipts": result}

@app.delete("/receipts/{receipt_id}")
def delete_receipt(receipt_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (receipt_id,))
    cursor.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "deleted_id": receipt_id}

# 📊 指定日が含まれる週（月曜〜日曜）を判定して集計するAPI
@app.get("/analytics")
def get_analytics(period_type: str = "all", value: str = ""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    where_clause = ""
    params = []

    if period_type == "year" and value:
        where_clause = "WHERE STRFTIME('%Y', receipts.created_at) = ?"
        params.append(value)
    elif period_type == "month" and value:
        where_clause = "WHERE STRFTIME('%Y-%m', receipts.created_at) = ?"
        params.append(value)
    elif period_type == "week" and value:
        # 指定日が含まれる週の「月曜日」と「日曜日」の範囲を算出
        where_clause = """
            WHERE DATE(receipts.created_at) >= DATE(?, 'weekday 0', '-6 days')
              AND DATE(receipts.created_at) <= DATE(?, 'weekday 0')
        """
        params.extend([value, value])

    query = f"""
        SELECT receipt_items.category, SUM(receipt_items.price) AS total
        FROM receipt_items
        JOIN receipts ON receipts.id = receipt_items.receipt_id
        {where_clause}
        GROUP BY receipt_items.category
        ORDER BY total DESC
    """
    cursor.execute(query, params)
    category_summary = [{"category": row[0] or "未分類", "total": row[1]} for row in cursor.fetchall()]

    cursor.execute("SELECT DISTINCT STRFTIME('%Y', created_at) FROM receipts ORDER BY 1 DESC")
    available_years = [row[0] for row in cursor.fetchall() if row[0]]

    cursor.execute("SELECT DISTINCT STRFTIME('%Y-%m', created_at) FROM receipts ORDER BY 1 DESC")
    available_months = [row[0] for row in cursor.fetchall() if row[0]]

    conn.close()
    return {
        "by_category": category_summary,
        "available_years": available_years,
        "available_months": available_months
    }