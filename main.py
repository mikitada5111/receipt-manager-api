import io
import json
import os
from dotenv import load_dotenv
import google.generativeai as genai
from fastapi import FastAPI, File, UploadFile, HTTPException
from PIL import Image

# .env ファイルから環境変数を読み込み
load_dotenv()

app = FastAPI()

# 環境変数から API キーを取得
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY が設定されていません。.env ファイルを確認してください。")

genai.configure(api_key=GEMINI_API_KEY)

@app.post("/analyze-receipt")
async def analyze_receipt(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="画像ファイルをアップロードしてください。")

    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))

        model = genai.GenerativeModel('gemini-flash-latest')
        
        prompt = """
        添付されたレシート画像から、店舗名、商品名と価格のリスト、および合計金額を抽出してJSON形式で回答してください。
        
        出力フォーマット（JSONのみ）:
        {
          "store_name": "店舗名",
          "items": [{"name": "商品名", "price": 金額}],
          "total": 合計金額
        }
        
        注意点:
        ・説明文やバックトック（```json など）は一切含めず、純粋なJSON文字列のみを出力してください。
        ・価格・合計金額は数値（integer）で出力してください。
        """

        response = model.generate_content([image, prompt])
        clean_json_str = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json_str)

        return {"status": "success", "filename": file.filename, "data": data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析エラー: {str(e)}")