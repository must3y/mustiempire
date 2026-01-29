import eventlet
eventlet.monkey_patch()
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from flask_bcrypt import Bcrypt
import sqlite3, random, time, threading, os
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = 'musti-gizli-key-123'
bcrypt = Bcrypt(app)
# Render ve Socket senkronizasyonu için kritik ayar
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# RULET MOTORU KONTROLÜ
game_thread_started = False

def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                 username TEXT UNIQUE, password TEXT, 
                 balance REAL DEFAULT 1000.0, role TEXT DEFAULT 'user',
                 xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
                 total_won REAL DEFAULT 0, total_lost REAL DEFAULT 0,
                 created_at TEXT, last_claim TEXT DEFAULT '2000-01-01 00:00:00',
                 is_muted INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                 username TEXT, message TEXT, role TEXT, level INTEGER)''')
    conn.commit()
    conn.close()

init_db()

game = {"timer": 15, "active_bets": {"T":[], "Dice":[], "CT":[]}, "history": [], "betting_open": True, "online_users": {}}

def game_loop():
    while True:
        game["betting_open"] = True
        for i in range(15, -1, -1):
            game["timer"] = i
            socketio.emit('timer_update', {"time": i})
            time.sleep(1)
        
        game["betting_open"] = False
        socketio.emit('lock_bets')
        
        target_index = random.randint(30, 60)
        res = "Dice" if (target_index % 15) == 0 else ("T" if (target_index % 15) <= 7 else "CT")
        
        socketio.emit('spin_start', {"result": res, "target_index": target_index})
        time.sleep(7)
        
        multiplier = {"T": 2, "CT": 2, "Dice": 14}
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        for side in ["T", "CT", "Dice"]:
            for bet in game["active_bets"][side]:
                user_sid = next((s for s, u in game["online_users"].items() if u['username'] == bet['user']), None)
                if side == res:
                    win = bet['amount'] * multiplier[res]
                    if user_sid:
                        u = game["online_users"][user_sid]
                        u['balance'] += win
                        u['total_won'] += win
                        socketio.emit('win_event', {"win": win, "new_bal": u['balance']}, to=user_sid)
                    c.execute("UPDATE users SET balance = balance + ?, total_won = total_won + ? WHERE username = ?", (win, win, bet['user']))
                else:
                    if user_sid:
                        game["online_users"][user_sid]['total_lost'] += bet['amount']
                    c.execute("UPDATE users SET total_lost = total_lost + ? WHERE username = ?", (bet['amount'], bet['user']))
        
        conn.commit()
        conn.close()
        game["history"].insert(0, res)
        game["active_bets"] = {"T":[], "Dice":[], "CT":[]}
        socketio.emit('reset_wheel', {"history": game["history"][:10]})

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('login')
def login(d):
    global game_thread_started
    # Biri girdiğinde rulet durmussa calıstır
    if not game_thread_started:
        threading.Thread(target=game_loop, daemon=True).start()
        game_thread_started = True

    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT password, balance, role, xp, level, total_won, total_lost, created_at, last_claim, is_muted FROM users WHERE username = ?", (d['user'],))
    u = c.fetchone()
    if u and bcrypt.check_password_hash(u[0], d['pw']):
        # MUST3Y'E OTOMATİK ADMİNLİK VER
        role = 'admin' if d['user'].lower() == "must3y" else u[2]
        user_data = {
            "username": d['user'], "balance": u[1], "role": role, 
            "xp": u[3], "level": u[4], "total_won": u[5], 
            "total_lost": u[6], "created_at": u[7], "last_claim": u[8], "is_muted": u[9]
        }
        game["online_users"][request.sid] = user_data
        emit('login_success', user_data)
        
        # AKTİF BAHİSLERİ GÖSTER
        for side, bets in game["active_bets"].items():
            for b in bets: emit('new_bet', {"side": side, "bet": b})
        
        # CHAT GEÇMİŞİNİ YÜKLE
        c.execute("SELECT username, message, role, level FROM chat_history ORDER BY id DESC LIMIT 50")
        history = [{"user": r[0], "text": r[1], "role": r[2], "level": r[3]} for r in reversed(c.fetchall())]
        emit('load_chat', history)
    conn.close()

@socketio.on('register')
def reg(d):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    hashed = bcrypt.generate_password_hash(d['pw']).decode('utf-8')
    role = 'admin' if d['user'].lower() == "must3y" else 'user'
    try:
        c.execute("INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)", 
                  (d['user'], hashed, role, datetime.now().strftime("%d.%m.%Y")))
        conn.commit()
        emit('auth_res', {"status": "success", "msg": "Kayıt başarılı!"})
    except:
        emit('auth_res', {"status": "error", "msg": "Bu kullanıcı adı alınmış!"})
    conn.close()

@socketio.on('claim_free')
def free():
    u = game["online_users"].get(request.sid)
    if u:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT last_claim FROM users WHERE username = ?", (u['username'],))
        last_s = c.fetchone()[0]
        # 12 saat kontrolü
        if datetime.now() - datetime.strptime(last_s, "%Y-%m-%d %H:%M:%S") > timedelta(hours=12):
            u['balance'] += 1000
            now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute("UPDATE users SET balance = balance + 1000, last_claim = ? WHERE username = ?", (now_s, u['username']))
            conn.commit()
            emit('free_coin_res', {"status": "success", "new_bal": u['balance']})
        else:
            emit('free_coin_res', {"status": "error", "msg": "12 saatte bir alabilirsin!"})
        conn.close()

@socketio.on('admin_command')
def admin(d):
    u = game["online_users"].get(request.sid)
    if u and u['role'] == 'admin':
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        if d['type'] == 'set_balance':
            c.execute("UPDATE users SET balance = ? WHERE username = ?", (d['val'], d['target']))
        elif d['type'] == 'mute':
            c.execute("UPDATE users SET is_muted = 1 WHERE username = ?", (d['target'],))
        elif d['type'] == 'set_role':
            c.execute("UPDATE users SET role = ? WHERE username = ?", (d['val'], d['target']))
        conn.commit()
        conn.close()
        # Admin panelini güncellemek için online listesini tekrar çekebilirsin
        emit('admin_res', {"msg": "İşlem başarılı!"})

# ADMİN PANELİ İÇİN KULLANICI LİSTESİ
@socketio.on('get_admin_data')
def get_admin():
    u = game["online_users"].get(request.sid)
    if u and u['role'] == 'admin':
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("SELECT username, balance, role, is_muted FROM users")
        all_users = [{"username": r[0], "balance": r[1], "role": r[2], "is_muted": r[3]} for r in c.fetchall()]
        conn.close()
        emit('admin_data_res', all_users)

@socketio.on('get_leaderboard')
def lb():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT username, total_won FROM users ORDER BY total_won DESC LIMIT 10")
    w = [{"user": r[0], "val": r[1]} for r in c.fetchall()]
    c.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    b = [{"user": r[0], "val": r[1]} for r in c.fetchall()]
    conn.close()
    emit('leaderboard_res', {"top_won": w, "top_bal": b})

@socketio.on('send_msg')
def msg(t):
    u = game["online_users"].get(request.sid)
    if u and not u['is_muted']:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute("INSERT INTO chat_history (username, message, role, level) VALUES (?, ?, ?, ?)", 
                  (u['username'], t, u['role'], u['level']))
        conn.commit()
        conn.close()
        socketio.emit('receive_msg', {"user": u['username'], "text": t, "role": u['role'], "level": u['level']})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
