import os
import sqlite3
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# .env から環境変数を読み込み
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY が設定されていません。.env ファイルを確認してください。")

app = FastAPI()
client = genai.Client(api_key=GEMINI_API_KEY)

# DBファイル名
DB_PATH = "receipts.db"

# データベースの初期化（起動時に自動作成）
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # レシート親テーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT,
            total_amount INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 品目子テーブル（カテゴリ列付き）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            name TEXT,
            price INTEGER,
            category TEXT,
            FOREIGN KEY (receipt_id) REFERENCES receipts (id)
        )
    """)
    conn.commit()
    conn.close()

# アプリ起動時にDBを作成
init_db()

# --- Gemini用レスポンス構造の定義 ---
class Item(BaseModel):
    name: str = Field(description="商品名")
    price: int = Field(description="価格（数値のみ）")
    category: str = Field(description="カテゴリ（食費, 日用品, 衣服, 交通費, 趣味・娯楽, その他 のいずれか）")

class ReceiptData(BaseModel):
    store_name: str = Field(description="店舗名", default="")
    items: list[Item] = Field(description="購入商品のリスト")
    total: int = Field(description="合計金額（数値のみ）", default=0)

# --- 1. レシート解析＆保存 API ---
@app.post("/analyze-receipt")
async def analyze_receipt(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="画像ファイルをアップロードしてください。")

    try:
        contents = await file.read()

        # Geminiによる読み取り＆カテゴリ自動付与
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

        # SQLiteデータベースへ保存
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO receipts (store_name, total_amount) VALUES (?, ?)",
            (receipt_info.store_name, receipt_info.total)
        )
        receipt_id = cursor.lastrowid

        for item in receipt_info.items:
            cursor.execute(
                "INSERT INTO receipt_items (receipt_id, name, price, category) VALUES (?, ?, ?, ?)",
                (receipt_id, item.name, item.price, item.category)
            )

        conn.commit()
        conn.close()

        return {
            "status": "success",
            "receipt_id": receipt_id,
            "filename": file.filename,
            "data": receipt_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析エラー: {str(e)}")

# --- 2. 全レシート一覧取得 API ---
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
        
        result.append({
            "id": r_id,
            "store_name": store,
            "total": total,
            "created_at": created_at,
            "items": items
        })
        
    conn.close()
    return {"receipts": result}

# --- 3. 出費集計 API（日ごと・月ごと・カテゴリごと） ---
@app.get("/analytics")
def get_analytics():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ① 日ごとの出費集計（YYYY-MM-DD単位）
    cursor.execute("""
        SELECT DATE(created_at) AS date, SUM(total_amount) AS total
        FROM receipts
        GROUP BY DATE(created_at)
        ORDER BY date DESC
    """)
    daily_summary = [{"date": row[0], "total": row[1]} for row in cursor.fetchall()]

    # ② 月ごとの出費集計（YYYY-MM単位）
    cursor.execute("""
        SELECT STRFTIME('%Y-%m', created_at) AS month, SUM(total_amount) AS total
        FROM receipts
        GROUP BY STRFTIME('%Y-%m', created_at)
        ORDER BY month DESC
    """)
    monthly_summary = [{"month": row[0], "total": row[1]} for row in cursor.fetchall()]

    # ③ カテゴリごとの出費集計
    cursor.execute("""
        SELECT category, SUM(price) AS total
        FROM receipt_items
        GROUP BY category
        ORDER BY total DESC
    """)
    category_summary = [{"category": row[0] or "未分類", "total": row[1]} for row in cursor.fetchall()]

    conn.close()

    return {
        "daily": daily_summary,
        "monthly": monthly_summary,
        "by_category": category_summary
    }