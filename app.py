from functools import wraps

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import os
import sqlite3
import requests
import tempfile
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "stocks.db")
app.secret_key = os.environ.get("SECRET_KEY", "stock-manager-secret-key")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@kuma.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1234")
MIGRATION_OWNER_EMAIL = os.environ.get("MIGRATION_OWNER_EMAIL", "dongyeol.jung@gmail.com").strip().lower()
MIGRATION_OWNER_PASSWORD = os.environ.get("MIGRATION_OWNER_PASSWORD", "1234")


def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def row_has_key(row, key):
    return hasattr(row, "keys") and key in row.keys()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"message": "로그인이 필요합니다."}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"message": "로그인이 필요합니다."}), 401

        conn = get_db()
        cursor = conn.cursor()
        if USE_POSTGRES:
            cursor.execute("SELECT is_admin, is_active FROM users WHERE id = %s", (session.get("user_id"),))
        else:
            cursor.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (session.get("user_id"),))
        user = cursor.fetchone()
        conn.close()

        if not user or not bool(user["is_active"]):
            session.clear()
            return jsonify({"message": "로그인이 필요합니다."}), 401
        if not bool(user["is_admin"]):
            return jsonify({"message": "관리자 권한이 필요합니다."}), 403
        return view(*args, **kwargs)

    return wrapped


def get_current_user_id():
    return session.get("user_id")


def ensure_sqlite_user_columns(cursor):
    cursor.execute("PRAGMA table_info(stocks)")
    stock_cols = {row[1] for row in cursor.fetchall()}
    if "user_id" not in stock_cols:
        cursor.execute("ALTER TABLE stocks ADD COLUMN user_id INTEGER")

    cursor.execute("PRAGMA table_info(dividends)")
    dividend_cols = {row[1] for row in cursor.fetchall()}
    if "user_id" not in dividend_cols:
        cursor.execute("ALTER TABLE dividends ADD COLUMN user_id INTEGER")


def ensure_default_user_and_migrate(cursor):
    admin_hash = generate_password_hash(ADMIN_PASSWORD)
    owner_hash = generate_password_hash(MIGRATION_OWNER_PASSWORD)

    if USE_POSTGRES:
        cursor.execute("SELECT id FROM users WHERE email = %s", (MIGRATION_OWNER_EMAIL,))
        owner = cursor.fetchone()
        if owner:
            owner_id = owner["id"]
            cursor.execute(
                "UPDATE users SET password_hash = %s, is_admin = FALSE, is_active = TRUE WHERE id = %s",
                (owner_hash, owner_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (%s, %s, FALSE, TRUE) RETURNING id",
                (MIGRATION_OWNER_EMAIL, owner_hash),
            )
            owner_id = cursor.fetchone()["id"]

        cursor.execute("SELECT id FROM users WHERE email = %s", (ADMIN_EMAIL,))
        admin = cursor.fetchone()
        if admin:
            admin_id = admin["id"]
            cursor.execute(
                "UPDATE users SET password_hash = %s, is_admin = TRUE, is_active = TRUE WHERE id = %s",
                (admin_hash, admin_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (%s, %s, TRUE, TRUE) RETURNING id",
                (ADMIN_EMAIL, admin_hash),
            )
            admin_id = cursor.fetchone()["id"]

        cursor.execute("UPDATE stocks SET user_id = %s WHERE user_id IS NULL", (owner_id,))
        cursor.execute("UPDATE dividends SET user_id = %s WHERE user_id IS NULL", (owner_id,))
        cursor.execute("UPDATE dividends d SET user_id = s.user_id FROM stocks s WHERE d.stock_id = s.id AND d.user_id IS NULL")
        return

    cursor.execute("SELECT id FROM users WHERE email = ?", (MIGRATION_OWNER_EMAIL,))
    owner = cursor.fetchone()
    if owner:
        owner_id = owner["id"]
        cursor.execute(
            "UPDATE users SET password_hash = ?, is_admin = 0, is_active = 1 WHERE id = ?",
            (owner_hash, owner_id),
        )
    else:
        cursor.execute(
            "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (?, ?, 0, 1)",
            (MIGRATION_OWNER_EMAIL, owner_hash),
        )
        owner_id = cursor.lastrowid

    cursor.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,))
    admin = cursor.fetchone()
    if admin:
        admin_id = admin["id"]
        cursor.execute(
            "UPDATE users SET password_hash = ?, is_admin = 1, is_active = 1 WHERE id = ?",
            (admin_hash, admin_id),
        )
    else:
        cursor.execute(
            "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (?, ?, 1, 1)",
            (ADMIN_EMAIL, admin_hash),
        )
        admin_id = cursor.lastrowid

    cursor.execute("UPDATE stocks SET user_id = ? WHERE user_id IS NULL", (owner_id,))
    cursor.execute("UPDATE dividends SET user_id = ? WHERE user_id IS NULL", (owner_id,))
    cursor.execute(
        """
        UPDATE dividends
        SET user_id = (SELECT user_id FROM stocks WHERE stocks.id = dividends.stock_id)
        WHERE user_id IS NULL
        """
    )


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
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
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
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                dividend_date TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        cursor.execute("ALTER TABLE dividends ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stocks_user_id ON stocks(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dividends_user_id ON dividends(user_id)")
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
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
                user_id INTEGER,
                dividend_date TEXT NOT NULL,
                amount REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
            )
            """
        )
        ensure_sqlite_columns(cursor)
        ensure_sqlite_user_columns(cursor)

    ensure_default_user_and_migrate(cursor)

    conn.commit()
    conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if "user_id" in session:
            return redirect(url_for("index"))
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        if request.is_json:
            return jsonify({"message": "이메일과 비밀번호를 입력해주세요."}), 400
        return render_template("login.html", error="이메일과 비밀번호를 입력해주세요."), 400

    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, email, password_hash, is_active FROM users WHERE email = %s", (email,))
    else:
        cursor.execute("SELECT id, email, password_hash, is_active FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()
    conn.close()

    valid_user = user and bool(user["is_active"]) and check_password_hash(user["password_hash"], password)
    if not valid_user:
        if request.is_json:
            return jsonify({"message": "로그인 정보가 올바르지 않습니다."}), 401
        return render_template("login.html", error="로그인 정보가 올바르지 않습니다."), 401

    session["user_id"] = user["id"]
    session["user_email"] = user["email"]

    if request.is_json:
        return jsonify({"message": "로그인 성공"})
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("SELECT id, email, is_admin, is_active FROM users WHERE id = %s", (user_id,))
    else:
        cursor.execute("SELECT id, email, is_admin, is_active FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()

    if not user or not bool(user["is_active"]):
        session.clear()
        return jsonify({"message": "로그인이 필요합니다."}), 401

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "isAdmin": bool(user["is_admin"]),
    })


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_list_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, is_admin, is_active, created_at FROM users ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    users = []
    for row in rows:
        users.append(
            {
                "id": row["id"],
                "email": row["email"],
                "isAdmin": bool(row["is_admin"]),
                "isActive": bool(row["is_active"]),
                "createdAt": str(row["created_at"]) if row_has_key(row, "created_at") and row["created_at"] is not None else "",
            }
        )
    return jsonify(users)


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    is_admin = bool(data.get("isAdmin", False))

    if not email or not password:
        return jsonify({"message": "이메일과 비밀번호를 입력해주세요."}), 400

    conn = get_db()
    cursor = conn.cursor()
    password_hash = generate_password_hash(password)
    try:
        if USE_POSTGRES:
            cursor.execute(
                "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (%s, %s, %s, TRUE)",
                (email, password_hash, is_admin),
            )
        else:
            cursor.execute(
                "INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (?, ?, ?, 1)",
                (email, password_hash, 1 if is_admin else 0),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({"message": "이미 존재하는 이메일입니다."}), 400

    conn.close()
    return jsonify({"message": "사용자가 생성되었습니다."})


@app.route("/api/admin/users/<int:target_user_id>", methods=["PUT"])
@admin_required
def admin_update_user(target_user_id):
    data = request.json or {}
    new_password = data.get("password")
    is_active = data.get("isActive")
    is_admin = data.get("isAdmin")

    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT id, email FROM users WHERE id = %s", (target_user_id,))
    else:
        cursor.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,))
    target = cursor.fetchone()
    if not target:
        conn.close()
        return jsonify({"message": "사용자를 찾을 수 없습니다."}), 404

    if str(target["email"]).lower() == ADMIN_EMAIL:
        is_active = True
        is_admin = True

    updates = []
    params = []

    if new_password:
        updates.append("password_hash = %s" if USE_POSTGRES else "password_hash = ?")
        params.append(generate_password_hash(new_password))
    if is_active is not None:
        updates.append("is_active = %s" if USE_POSTGRES else "is_active = ?")
        params.append(bool(is_active) if USE_POSTGRES else (1 if bool(is_active) else 0))
    if is_admin is not None:
        updates.append("is_admin = %s" if USE_POSTGRES else "is_admin = ?")
        params.append(bool(is_admin) if USE_POSTGRES else (1 if bool(is_admin) else 0))

    if updates:
        params.append(target_user_id)
        if USE_POSTGRES:
            cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", tuple(params))
        else:
            cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
        conn.commit()

    conn.close()
    return jsonify({"message": "사용자 정보가 업데이트되었습니다."})


@app.route("/api/admin/users/<int:target_user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(target_user_id):
    current_user_id = session.get("user_id")

    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT id, email FROM users WHERE id = %s", (target_user_id,))
    else:
        cursor.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,))
    target = cursor.fetchone()

    if not target:
        conn.close()
        return jsonify({"message": "사용자를 찾을 수 없습니다."}), 404

    target_email = str(target["email"]).lower()
    if target_email == ADMIN_EMAIL:
        conn.close()
        return jsonify({"message": "기본 관리자 계정은 삭제할 수 없습니다."}), 400

    if int(target["id"]) == int(current_user_id):
        conn.close()
        return jsonify({"message": "현재 로그인한 계정은 삭제할 수 없습니다."}), 400

    if USE_POSTGRES:
        cursor.execute("DELETE FROM dividends WHERE user_id = %s", (target_user_id,))
        cursor.execute("DELETE FROM stocks WHERE user_id = %s", (target_user_id,))
        cursor.execute("DELETE FROM users WHERE id = %s", (target_user_id,))
    else:
        cursor.execute("DELETE FROM dividends WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM stocks WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM users WHERE id = ?", (target_user_id,))

    conn.commit()
    conn.close()
    return jsonify({"message": "사용자가 삭제되었습니다."})


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/stocks", methods=["GET"])
@login_required
def get_stocks():
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT s.*,
                   STRING_AGG((d.id::text || '::' || d.dividend_date || '::' || d.amount::text), '|' ORDER BY d.id) AS dividends
            FROM stocks s
            LEFT JOIN dividends d ON s.id = d.stock_id AND d.user_id = %s
            WHERE s.user_id = %s
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """,
            (user_id, user_id),
        )
        rows = cursor.fetchall()
    else:
        cursor.execute(
            """
            SELECT s.*,
                   GROUP_CONCAT(d.id || '::' || d.dividend_date || '::' || d.amount, '|') AS dividends
            FROM stocks s
            LEFT JOIN dividends d ON s.id = d.stock_id AND d.user_id = ?
            WHERE s.user_id = ?
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """,
            (user_id, user_id),
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
@login_required
def add_stock():
    user_id = get_current_user_id()
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            INSERT INTO stocks (user_id, account_name, stock_name, stock_code, purchase_price, shares, total_amount, dividend_cycle)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
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
            INSERT INTO stocks (user_id, account_name, stock_name, stock_code, purchase_price, shares, total_amount, dividend_cycle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
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
@login_required
def update_stock(stock_id):
    user_id = get_current_user_id()
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    if "isSold" in data:
        if USE_POSTGRES:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = %s, total_amount = %s, sell_amount = %s, is_sold = %s
                WHERE id = %s AND user_id = %s
                """,
                (data["shares"], data["totalAmount"], data.get("sellAmount", 0), bool(data["isSold"]), stock_id, user_id),
            )
        else:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = ?, total_amount = ?, sell_amount = ?, is_sold = ?
                WHERE id = ? AND user_id = ?
                """,
                (data["shares"], data["totalAmount"], data.get("sellAmount", 0), data["isSold"], stock_id, user_id),
            )
    else:
        if USE_POSTGRES:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = %s, total_amount = %s, purchase_price = %s
                WHERE id = %s AND user_id = %s
                """,
                (data["shares"], data["totalAmount"], data["purchasePrice"], stock_id, user_id),
            )
        else:
            cursor.execute(
                """
                UPDATE stocks
                SET shares = ?, total_amount = ?, purchase_price = ?
                WHERE id = ? AND user_id = ?
                """,
                (data["shares"], data["totalAmount"], data["purchasePrice"], stock_id, user_id),
            )

    conn.commit()
    conn.close()
    return jsonify({"message": "주식이 업데이트되었습니다."})


@app.route("/api/stocks/<int:stock_id>", methods=["DELETE"])
@login_required
def delete_stock(stock_id):
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM dividends WHERE stock_id = %s AND user_id = %s", (stock_id, user_id))
        cursor.execute("DELETE FROM stocks WHERE id = %s AND user_id = %s", (stock_id, user_id))
    else:
        cursor.execute("DELETE FROM dividends WHERE stock_id = ? AND user_id = ?", (stock_id, user_id))
        cursor.execute("DELETE FROM stocks WHERE id = ? AND user_id = ?", (stock_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "주식이 삭제되었습니다."})


@app.route("/api/dividends", methods=["POST"])
@login_required
def add_dividend():
    user_id = get_current_user_id()
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute("SELECT id FROM stocks WHERE id = %s AND user_id = %s", (data["stockId"], user_id))
    else:
        cursor.execute("SELECT id FROM stocks WHERE id = ? AND user_id = ?", (data["stockId"], user_id))
    owned_stock = cursor.fetchone()
    if not owned_stock:
        conn.close()
        return jsonify({"message": "유효하지 않은 주식입니다."}), 403

    if USE_POSTGRES:
        cursor.execute(
            "INSERT INTO dividends (stock_id, user_id, dividend_date, amount) VALUES (%s, %s, %s, %s) RETURNING id",
            (data["stockId"], user_id, data["date"], data["amount"]),
        )
        dividend_id = cursor.fetchone()["id"]
    else:
        cursor.execute(
            "INSERT INTO dividends (stock_id, user_id, dividend_date, amount) VALUES (?, ?, ?, ?)",
            (data["stockId"], user_id, data["date"], data["amount"]),
        )
        dividend_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": dividend_id, "message": "배당금이 추가되었습니다."})


@app.route("/api/dividends/<int:dividend_id>", methods=["DELETE"])
@login_required
def delete_dividend(dividend_id):
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("DELETE FROM dividends WHERE id = %s AND user_id = %s", (dividend_id, user_id))
    else:
        cursor.execute("DELETE FROM dividends WHERE id = ? AND user_id = ?", (dividend_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "배당금이 삭제되었습니다."})


@app.route("/api/export")
@login_required
def export_db():
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT id, account_name, stock_name, stock_code, purchase_price, shares,
                   total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
            FROM stocks
            WHERE user_id = %s
            ORDER BY id
            """,
            (user_id,),
        )
        stocks = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, stock_id, dividend_date, amount, created_at
            FROM dividends
            WHERE user_id = %s
            ORDER BY id
            """,
            (user_id,),
        )
        dividends = cursor.fetchall()
    else:
        cursor.execute(
            """
            SELECT id, account_name, stock_name, stock_code, purchase_price, shares,
                   total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
            FROM stocks
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        )
        stocks = cursor.fetchall()
        cursor.execute(
            """
            SELECT id, stock_id, dividend_date, amount, created_at
            FROM dividends
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
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

        id_map = {}
        next_stock_id = 1
        next_dividend_id = 1

        for row in stocks:
            old_id = row["id"]
            id_map[old_id] = next_stock_id
            sc.execute(
                """
                INSERT INTO stocks (
                    id, account_name, stock_name, stock_code, purchase_price, shares,
                    total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_stock_id,
                    row["account_name"],
                    row["stock_name"],
                    row["stock_code"] if row_has_key(row, "stock_code") else "",
                    row["purchase_price"],
                    row["shares"],
                    row["total_amount"],
                    row["dividend_cycle"],
                    row["current_price"] if row_has_key(row, "current_price") and row["current_price"] is not None else 0,
                    row["sell_amount"] if row_has_key(row, "sell_amount") and row["sell_amount"] is not None else 0,
                    1 if row_has_key(row, "is_sold") and row["is_sold"] else 0,
                    str(row["created_at"]) if row_has_key(row, "created_at") and row["created_at"] is not None else None,
                ),
            )
            next_stock_id += 1

        for row in dividends:
            mapped_stock_id = id_map.get(row["stock_id"])
            if mapped_stock_id is None:
                continue
            sc.execute(
                """
                INSERT INTO dividends (id, stock_id, dividend_date, amount, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    next_dividend_id,
                    mapped_stock_id,
                    row["dividend_date"],
                    row["amount"],
                    str(row["created_at"]) if row_has_key(row, "created_at") and row["created_at"] is not None else None,
                ),
            )
            next_dividend_id += 1

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


@app.route("/api/import", methods=["POST"])
@login_required
def import_db():
    user_id = get_current_user_id()
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

        if USE_POSTGRES:
            cursor.execute("DELETE FROM dividends WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM stocks WHERE user_id = %s", (user_id,))
        else:
            cursor.execute("DELETE FROM dividends WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM stocks WHERE user_id = ?", (user_id,))

        id_map = {}
        for row in sqlite_stocks:
            if USE_POSTGRES:
                cursor.execute(
                    """
                    INSERT INTO stocks (
                        user_id, account_name, stock_name, stock_code, purchase_price, shares,
                        total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        row["account_name"],
                        row["stock_name"],
                        row["stock_code"] if row_has_key(row, "stock_code") else "",
                        row["purchase_price"],
                        row["shares"],
                        row["total_amount"],
                        row["dividend_cycle"],
                        row["current_price"] if row_has_key(row, "current_price") else 0,
                        row["sell_amount"] if row_has_key(row, "sell_amount") else 0,
                        bool(row["is_sold"]) if row_has_key(row, "is_sold") else False,
                        row["created_at"] if row_has_key(row, "created_at") else None,
                    ),
                )
                new_id = cursor.fetchone()["id"]
            else:
                cursor.execute(
                    """
                    INSERT INTO stocks (
                        user_id, account_name, stock_name, stock_code, purchase_price, shares,
                        total_amount, dividend_cycle, current_price, sell_amount, is_sold, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        row["account_name"],
                        row["stock_name"],
                        row["stock_code"] if row_has_key(row, "stock_code") else "",
                        row["purchase_price"],
                        row["shares"],
                        row["total_amount"],
                        row["dividend_cycle"],
                        row["current_price"] if row_has_key(row, "current_price") else 0,
                        row["sell_amount"] if row_has_key(row, "sell_amount") else 0,
                        int(row["is_sold"]) if row_has_key(row, "is_sold") else 0,
                        row["created_at"] if row_has_key(row, "created_at") else None,
                    ),
                )
                new_id = cursor.lastrowid
            id_map[row["id"]] = new_id

        for row in sqlite_dividends:
            mapped_stock_id = id_map.get(row["stock_id"])
            if mapped_stock_id is None:
                continue
            if USE_POSTGRES:
                cursor.execute(
                    """
                    INSERT INTO dividends (stock_id, user_id, dividend_date, amount, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        mapped_stock_id,
                        user_id,
                        row["dividend_date"],
                        row["amount"],
                        row["created_at"] if row_has_key(row, "created_at") else None,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO dividends (stock_id, user_id, dividend_date, amount, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        mapped_stock_id,
                        user_id,
                        row["dividend_date"],
                        row["amount"],
                        row["created_at"] if row_has_key(row, "created_at") else None,
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
@login_required
def get_stock_price(stock_code):
    price = fetch_naver_close_price(stock_code)
    if price is None:
        return jsonify({"success": False, "message": "주식을 찾을 수 없습니다."})
    return jsonify({"name": stock_code, "price": price, "success": True})


@app.route("/api/stocks/update-prices", methods=["POST"])
@login_required
def update_stock_prices():
    user_id = get_current_user_id()
    data = request.json
    stock_id = data.get("stockId")
    current_price = data.get("currentPrice")

    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute("UPDATE stocks SET current_price = %s WHERE id = %s AND user_id = %s", (current_price, stock_id, user_id))
    else:
        cursor.execute("UPDATE stocks SET current_price = ? WHERE id = ? AND user_id = ?", (current_price, stock_id, user_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "가격이 업데이트되었습니다."})


@app.route("/api/stocks/refresh-prices", methods=["POST"])
@login_required
def refresh_all_stock_prices():
    user_id = get_current_user_id()
    conn = get_db()
    cursor = conn.cursor()
    if USE_POSTGRES:
        cursor.execute(
            "SELECT id, stock_code FROM stocks WHERE user_id = %s AND stock_code IS NOT NULL AND stock_code != ''",
            (user_id,),
        )
    else:
        cursor.execute(
            "SELECT id, stock_code FROM stocks WHERE user_id = ? AND stock_code IS NOT NULL AND stock_code != ''",
            (user_id,),
        )
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
