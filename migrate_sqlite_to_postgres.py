import os
import sqlite3
import psycopg
from pwdlib import PasswordHash

SQLITE_DB = os.getenv("SQLITE_DB", "receipts.db")
DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATION_EMAIL = os.getenv("MIGRATION_EMAIL")
MIGRATION_PASSWORD = os.getenv("MIGRATION_PASSWORD")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
if not MIGRATION_EMAIL or not MIGRATION_PASSWORD:
    raise RuntimeError("MIGRATION_EMAIL and MIGRATION_PASSWORD are required")

password_hash = PasswordHash.recommended()

sqlite_conn = sqlite3.connect(SQLITE_DB)
sqlite_conn.row_factory = sqlite3.Row

pg_conn = psycopg.connect(DATABASE_URL)

try:
    with pg_conn.cursor() as cur:
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

        cur.execute("SELECT id FROM users WHERE email = %s", (MIGRATION_EMAIL.lower(),))
        user = cur.fetchone()

        if user:
            user_id = user[0]
        else:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (MIGRATION_EMAIL.lower(), password_hash.hash(MIGRATION_PASSWORD)),
            )
            user_id = cur.fetchone()[0]

        old_receipts = sqlite_conn.execute("""
            SELECT id, store_name, total,
                   COALESCE(receipt_date, DATE(created_at)) AS receipt_date,
                   created_at
            FROM receipts
            ORDER BY id
        """).fetchall()

        receipt_map = {}

        for r in old_receipts:
            cur.execute("""
                INSERT INTO receipts
                    (user_id, store_name, total, receipt_date, created_at)
                VALUES
                    (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                user_id,
                r["store_name"],
                r["total"] or 0,
                r["receipt_date"],
                r["created_at"],
            ))
            new_id = cur.fetchone()[0]
            receipt_map[r["id"]] = new_id

        old_items = sqlite_conn.execute("""
            SELECT receipt_id, name, price, category
            FROM items
            ORDER BY id
        """).fetchall()

        for item in old_items:
            new_receipt_id = receipt_map.get(item["receipt_id"])
            if new_receipt_id is None:
                continue

            cur.execute("""
                INSERT INTO items
                    (receipt_id, name, price, category)
                VALUES
                    (%s, %s, %s, %s)
            """, (
                new_receipt_id,
                item["name"] or "",
                item["price"] or 0,
                item["category"] or "その他",
            ))

    pg_conn.commit()
    print(f"Migration complete: {len(old_receipts)} receipts migrated.")

finally:
    sqlite_conn.close()
    pg_conn.close()
