from flask import Flask, render_template, request, jsonify
import os
import sqlite3
import requests
import tempfile

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "stocks.db")


def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_sqlite_columns(cursor):
    cursor.execute("PRAGMA table_info(stocks)")
    cols = {row[1] for row in cursor.fetchall()}
    if "stock_code" not in cols:
        cursor.execute("ALTER TABLE stocks ADD COLUMN stock_code TEXT")
    if "current_price" not in cols:
        cursor.execute("ALTER TABLE stocks ADD COLUMN current_price REAL DEFAULT 0")
    if "sell_amount" not in cols:
        cursor.execute("ALTER TABLE stocks ADD COLUMN sell_amount REAL DEFAULT 0")
    if "is_sold" not in cols:
        cursor.execute("ALTER TABLE stocks ADD COLUMN is_sold INTEGER DEFAULT 0")


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                id SERIAL PRIMARY KEY,
                account_name TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                stock_code TEXT,
                purchase_price DOUBLE PRECISION NOT NULL,
                shares INTEGER NOT NULL,
                total_amount DOUBLE PRECISION NOT NULL,
                dividend_cycle TEXT NOT NULL,
                current_price DOUBLE PRECISION DEFAULT 0,
                sell_amount DOUBLE PRECISION DEFAULT 0,
                is_sold BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dividends (
                id SERIAL PRIMARY KEY,
                stock_id INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
                dividend_date TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                stock_code TEXT,
                purchase_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                total_amount REAL NOT NULL,
                dividend_cycle TEXT NOT NULL,
                current_price REAL DEFAULT 0,
                sell_amount REAL DEFAULT 0,
                is_sold INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dividends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_id INTEGER NOT NULL,
                dividend_date TEXT NOT NULL,
                amount REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
            )
            """
        )
        ensure_sqlite_columns(cursor)

    conn.commit()
    conn.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stocks", methods=["GET"])
def get_stocks():
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT s.*,
                   STRING_AGG((d.id::text || '::' || d.dividend_date || '::' || d.amount::text), '|' ORDER BY d.id) AS dividends
            FROM stocks s
            LEFT JOIN dividends d ON s.id = d.stock_id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """
        )
        rows = cursor.fetchall()
    else:
        cursor.execute(
            """
            SELECT s.*,
                   GROUP_CONCAT(d.id || '::' || d.dividend_date || '::' || d.amount, '|') AS dividends
            FROM stocks s
            LEFT JOIN dividends d ON s.id = d.stock_id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """
        )
        rows = cursor.fetchall()

    conn.close()

    stocks = []
    for row in rows:
        stock = {
            "id": row["id"],
            "accountName": row["account_name"],
            "stockName": row["stock_name"],
            "stockCode": row.get("stock_code", "") if isinstance(row, dict) else row["stock_code"],
            "purchasePrice": row["purchase_price"],
            "shares": row["shares"],
            "totalAmount": row["total_amount"],
            "dividendCycle": row["dividend_cycle"],
            "currentPrice": row["current_price"] or 0,
            "sellAmount": row["sell_amount"] or 0,
            "isSold": bool(row["is_sold"]) if USE_POSTGRES else row["is_sold"],
            "dividends": [],
        }

        dividends_raw = row["dividends"]
        if dividends_raw:
            for d in dividends_raw.split("|"):
                parts = d.split("::")
                if len(parts) == 3:
                    stock["dividends"].append(
                        {
                            "id": int(parts[0]),
                            "date": parts[1],
                            "amount": float(parts[2]),
                        }
                    )

        stocks.append(stock)

    return jsonify(stocks)


@app.route("/api/stocks", methods=["POST"])
def add_stock():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            INSERT INTO stocks (account_name, stock_name, stock_code, purchase_price, shares, total_amount, dividend_cycle)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                data["accountName"],
                data["stockName"],
                data.get("stockCode", ""),
                data["purchasePrice"],
                data["shares"],
                data["totalAmount"],
                data["dividendCycle"],
            ),
        )
        stock_id = cursor.fetchone()["id"]
    else:
        cursor.execute(
            """
            INSERT INTO stocks (account_name, stock_name, stock_code, purchase_price, shares, total_amount, dividend_cycle)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["accountName"],
                data["stockName"],
                data.get("stockCode", ""),
                data["purchasePrice"],
                data["shares"],
                data["totalAmount"],
                data["dividendCycle"],
            ),
        )
        stock_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return jsonify({"id": stock_id, "message": "주식이 추가되었습니다."})


@app.route("/api/stocks/<int:stock_id>", methods=["PUT"])
def update_stock(stock_id):
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    if "isSold" in data:
        if USE_POSTGRES:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = %s, total_amount = %s, sell_amount = %s, is_sold = %s
                WHERE id = %s
                """,
                (data["shares"], data["totalAmount"], data.get("sellAmount", 0), bool(data["isSold"]), stock_id),
            )
        else:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = ?, total_amount = ?, sell_amount = ?, is_sold = ?
                WHERE id = ?
                """,
                (data["shares"], data["totalAmount"], data.get("sellAmount", 0), data["isSold"], stock_id),
            )
    else:
        if USE_POSTGRES:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = %s, total_amount = %s, purchase_price = %s
                WHERE id = %s
                """,
                (data["shares"], data["totalAmount"], data["purchasePrice"], stock_id),
            )
        else:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = ?, total_amount = ?, purchase_price = ?
                WHERE id = ?
                """,
                (data["shares"], data["totalAmount"], data["purchasePrice"], stock_id),
            )

    conn.commit()
    conn.close()
    return jsonify({"message": "주식이 업데이트되었습니다."})


@app.route("/api/stocks/<int:stock_id>", methods=["DELETE"])
def delete_stock(stock_id):
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM dividends WHERE stock_id = %s", (stock_id,))
        cursor.execute("DELETE FROM stocks WHERE id = %s", (stock_id,))
    else:
        cursor.execute("DELETE FROM dividends WHERE stock_id = ?", (stock_id,))
        cursor.execute("DELETE FROM stocks WHERE id = ?", (stock_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "주식이 삭제되었습니다."})


@app.route("/api/dividends", methods=["POST"])
def add_dividend():
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO dividends (stock_id, dividend_date, amount) VALUES (%s, %s, %s) RETURNING id",
            (data["stockId"], data["date"], data["amount"]),
        )
        dividend_id = cursor.fetchone()["id"]
    else:
        cursor.execute(
            "INSERT INTO dividends (stock_id, dividend_date, amount) VALUES (?, ?, ?)",
            (data["stockId"], data["date"], data["amount"]),
        )
        dividend_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": dividend_id, "message": "배당금이 추가되었습니다."})


@app.route("/api/dividends/<int:dividend_id>", methods=["DELETE"])
def delete_dividend(dividend_id):
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM dividends WHERE id = %s", (dividend_id,))
    else:
        cursor.execute("DELETE FROM dividends WHERE id = ?", (dividend_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "배당금이 삭제되었습니다."})


@app.route("/api/export")
def export_db():
    if USE_POSTGRES:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, account_name, stock_name, stock_code, purchase_price, shares,
                   total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
            FROM stocks
            ORDER BY id
            """
        )
        stocks = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, stock_id, dividend_date, amount, created_at
            FROM dividends
            ORDER BY id
            """
        )
        dividends = cursor.fetchall()
        conn.close()

        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp_path = tmp_file.name
        tmp_file.close()

        try:
            sconn = sqlite3.connect(tmp_path)
            sc = sconn.cursor()
            sc.execute(
                """
                CREATE TABLE IF NOT EXISTS stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    stock_code TEXT,
                    purchase_price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    total_amount REAL NOT NULL,
                    dividend_cycle TEXT NOT NULL,
                    current_price REAL DEFAULT 0,
                    sell_amount REAL DEFAULT 0,
                    is_sold INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            sc.execute(
                """
                CREATE TABLE IF NOT EXISTS dividends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_id INTEGER NOT NULL,
                    dividend_date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
                )
                """
            )

            for row in stocks:
                sc.execute(
                    """
                    INSERT INTO stocks (
                        id, account_name, stock_name, stock_code, purchase_price, shares,
                        total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["account_name"],
                        row["stock_name"],
                        row["stock_code"],
                        row["purchase_price"],
                        row["shares"],
                        row["total_amount"],
                        row["dividend_cycle"],
                        row["current_price"] or 0,
                        row["sell_amount"] or 0,
                        1 if row["is_sold"] else 0,
                        str(row["created_at"]) if row["created_at"] is not None else None,
                    ),
                )

            for row in dividends:
                sc.execute(
                    """
                    INSERT INTO dividends (id, stock_id, dividend_date, amount, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["stock_id"],
                        row["dividend_date"],
                        row["amount"],
                        str(row["created_at"]) if row["created_at"] is not None else None,
                    ),
                )

            sconn.commit()
            sconn.close()

            with open(tmp_path, "rb") as f:
                data = f.read()

            return data, 200, {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename=stocks.db",
            }
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            return f.read(), 200, {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename=stocks.db",
            }
    return "Database not found", 404


@app.route("/api/import", methods=["POST"])
def import_db():
    if USE_POSTGRES:
        file = request.files.get("file")
        if file is None:
            return jsonify({"message": "파일이 없습니다."}), 400

        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp_path = tmp_file.name
        tmp_file.close()
        file.save(tmp_path)

        try:
            sconn = sqlite3.connect(tmp_path)
            sconn.row_factory = sqlite3.Row
            sc = sconn.cursor()

            sc.execute(
                """
                SELECT id, account_name, stock_name, stock_code, purchase_price, shares,
                       total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                FROM stocks
                ORDER BY id
                """
            )
            sqlite_stocks = sc.fetchall()

            sc.execute(
                """
                SELECT id, stock_id, dividend_date, amount, created_at
                FROM dividends
                ORDER BY id
                """
            )
            sqlite_dividends = sc.fetchall()
            sconn.close()

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute("DELETE FROM dividends")
            cursor.execute("DELETE FROM stocks")

            id_map = {}
            for row in sqlite_stocks:
                cursor.execute(
                    """
                    INSERT INTO stocks (
                        account_name, stock_name, stock_code, purchase_price, shares,
                        total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        row["account_name"],
                        row["stock_name"],
                        row["stock_code"] if "stock_code" in row.keys() else "",
                        row["purchase_price"],
                        row["shares"],
                        row["total_amount"],
                        row["dividend_cycle"],
                        row["current_price"] if "current_price" in row.keys() else 0,
                        row["sell_amount"] if "sell_amount" in row.keys() else 0,
                        bool(row["is_sold"]) if "is_sold" in row.keys() else False,
                        row["created_at"] if "created_at" in row.keys() else None,
                    ),
                )
                new_id = cursor.fetchone()["id"]
                id_map[row["id"]] = new_id

            for row in sqlite_dividends:
                mapped_stock_id = id_map.get(row["stock_id"])
                if mapped_stock_id is None:
                    continue
                cursor.execute(
                    """
                    INSERT INTO dividends (stock_id, dividend_date, amount, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        mapped_stock_id,
                        row["dividend_date"],
                        row["amount"],
                        row["created_at"] if "created_at" in row.keys() else None,
                    ),
                )

            conn.commit()
            conn.close()

            return jsonify({"message": "데이터베이스를 가져왔습니다."})
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    file = request.files["file"]
    file.save(DB_PATH)
    return jsonify({"message": "데이터베이스를 가져왔습니다."})


def fetch_naver_close_price(stock_code):
    try:
        if not stock_code:
            return None
        url = f"https://m.stock.naver.com/api/stock/{stock_code}/basic"
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        }
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        data = response.json()
        if "closePrice" not in data:
            return None
        price = data.get("closePrice", 0)
        if isinstance(price, str):
            price = int(price.replace(",", ""))
        else:
            price = int(price)
        return price
    except Exception:
        return None


@app.route("/api/price/<stock_code>", methods=["GET"])
def get_stock_price(stock_code):
    price = fetch_naver_close_price(stock_code)
    if price is None:
        return jsonify({"success": False, "message": "주식을 찾을 수 없습니다."})
    return jsonify({"name": stock_code, "price": price, "success": True})


@app.route("/api/stocks/update-prices", methods=["POST"])
def update_stock_prices():
    data = request.json
    stock_id = data.get("stockId")
    current_price = data.get("currentPrice")

    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE stocks SET current_price = %s WHERE id = %s", (current_price, stock_id))
    else:
        cursor.execute("UPDATE stocks SET current_price = ? WHERE id = ?", (current_price, stock_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "가격이 업데이트되었습니다."})


@app.route("/api/stocks/refresh-prices", methods=["POST"])
def refresh_all_stock_prices():
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, stock_code FROM stocks WHERE stock_code IS NOT NULL AND stock_code != ''")
    else:
        cursor.execute("SELECT id, stock_code FROM stocks WHERE stock_code IS NOT NULL AND stock_code != ''")
    rows = cursor.fetchall()

    updated = 0
    for row in rows:
        row_id = row["id"]
        stock_code = row["stock_code"]
        price = fetch_naver_close_price(stock_code)
        if price is None:
            continue
        if USE_POSTGRES:
            cursor.execute("UPDATE stocks SET current_price = %s WHERE id = %s", (price, row_id))
        else:
            cursor.execute("UPDATE stocks SET current_price = ? WHERE id = ?", (price, row_id))
        updated += 1

    conn.commit()
    conn.close()
    return jsonify({"message": "현재가 갱신 완료", "updated": updated})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
