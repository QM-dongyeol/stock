from flask import Flask, render_template, request, jsonify
import sqlite3
import os
import requests

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'stocks.db')


def fetch_naver_close_price(stock_code):
    try:
        if not stock_code:
            return None
        url = f"https://m.stock.naver.com/api/stock/{stock_code}/basic"
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
        }
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        data = response.json()
        if 'closePrice' not in data:
            return None
        price = data.get('closePrice', 0)
        if isinstance(price, str):
            price = int(price.replace(',', ''))
        else:
            price = int(price)
        return price
    except Exception:
        return None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
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
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id INTEGER NOT NULL,
            dividend_date TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stocks', methods=['GET'])
def get_stocks():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT s.*, 
            GROUP_CONCAT(d.id || '::' || d.dividend_date || '::' || d.amount, '|') as dividends
        FROM stocks s
        LEFT JOIN dividends d ON s.id = d.stock_id
        GROUP BY s.id
        ORDER BY s.created_at DESC
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    stocks = []
    for row in rows:
        stock = {
            'id': row['id'],
            'accountName': row['account_name'],
            'stockName': row['stock_name'],
            'stockCode': row['stock_code'],
            'purchasePrice': row['purchase_price'],
            'shares': row['shares'],
            'totalAmount': row['total_amount'],
            'dividendCycle': row['dividend_cycle'],
            'currentPrice': row['current_price'],
            'sellAmount': row['sell_amount'],
            'isSold': row['is_sold'],
            'dividends': []
        }
        
        if row['dividends']:
            for d in row['dividends'].split('|'):
                parts = d.split('::')
                stock['dividends'].append({
                    'id': int(parts[0]),
                    'date': parts[1],
                    'amount': float(parts[2])
                })
        
        stocks.append(stock)
    
    return jsonify(stocks)

@app.route('/api/stocks', methods=['POST'])
def add_stock():
    data = request.json
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO stocks (account_name, stock_name, stock_code, purchase_price, shares, total_amount, dividend_cycle)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['accountName'],
        data['stockName'],
        data.get('stockCode', ''),
        data['purchasePrice'],
        data['shares'],
        data['totalAmount'],
        data['dividendCycle']
    ))
    
    stock_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return jsonify({'id': stock_id, 'message': '주식이 추가되었습니다.'})

@app.route('/api/stocks/<int:stock_id>', methods=['PUT'])
def update_stock(stock_id):
    data = request.json
    
    conn = get_db()
    cursor = conn.cursor()
    
    if 'isSold' in data:
        cursor.execute('''
            UPDATE stocks
            SET shares = ?, total_amount = ?, sell_amount = ?, is_sold = ?
            WHERE id = ?
        ''', (data['shares'], data['totalAmount'], data.get('sellAmount', 0), data['isSold'], stock_id))
    else:
        cursor.execute('''
            UPDATE stocks 
            SET shares = ?, total_amount = ?, purchase_price = ?
            WHERE id = ?
        ''', (data['shares'], data['totalAmount'], data['purchasePrice'], stock_id))
    
    conn.commit()
    conn.close()
    
    return jsonify({'message': '주식이 업데이트되었습니다.'})

@app.route('/api/stocks/<int:stock_id>', methods=['DELETE'])
def delete_stock(stock_id):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM dividends WHERE stock_id = ?', (stock_id,))
    cursor.execute('DELETE FROM stocks WHERE id = ?', (stock_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({'message': '주식이 삭제되었습니다.'})

@app.route('/api/dividends', methods=['POST'])
def add_dividend():
    data = request.json
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO dividends (stock_id, dividend_date, amount)
        VALUES (?, ?, ?)
    ''', (data['stockId'], data['date'], data['amount']))
    
    dividend_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return jsonify({'id': dividend_id, 'message': '배당금이 추가되었습니다.'})

@app.route('/api/dividends/<int:dividend_id>', methods=['DELETE'])
def delete_dividend(dividend_id):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM dividends WHERE id = ?', (dividend_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({'message': '배당금이 삭제되었습니다.'})

@app.route('/api/export')
def export_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, 'rb') as f:
            return f.read(), 200, {
                'Content-Type': 'application/octet-stream',
                'Content-Disposition': f'attachment; filename=stocks.db'
            }
    return 'Database not found', 404

@app.route('/api/import', methods=['POST'])
def import_db():
    file = request.files['file']
    file.save(DB_PATH)
    return jsonify({'message': '데이터베이스를 가져왔습니다.'})

@app.route('/api/price/<stock_code>', methods=['GET'])
def get_stock_price(stock_code):
    try:
        price = fetch_naver_close_price(stock_code)
        if price is not None:
            return jsonify({
                'name': stock_code,
                'price': price,
                'success': True
            })

        return jsonify({
            'success': False,
            'message': '주식을 찾을 수 없습니다.'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e)
        })

@app.route('/api/stocks/update-prices', methods=['POST'])
def update_stock_prices():
    data = request.json
    stock_id = data.get('stockId')
    current_price = data.get('currentPrice')
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE stocks SET current_price = ? WHERE id = ?', (current_price, stock_id))
    conn.commit()
    conn.close()
    
    return jsonify({'message': '가격이 업데이트되었습니다.'})


@app.route('/api/stocks/refresh-prices', methods=['POST'])
def refresh_all_stock_prices():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('SELECT id, stock_code FROM stocks WHERE stock_code IS NOT NULL AND stock_code != ""')
    rows = cursor.fetchall()

    updated = 0
    for row in rows:
        price = fetch_naver_close_price(row['stock_code'])
        if price is None:
            continue
        cursor.execute('UPDATE stocks SET current_price = ? WHERE id = ?', (price, row['id']))
        updated += 1

    conn.commit()
    conn.close()

    return jsonify({'message': '현재가 갱신 완료', 'updated': updated})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
