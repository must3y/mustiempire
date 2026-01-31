import eventlet
eventlet.monkey_patch()
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from flask_bcrypt import Bcrypt
import sqlite3, random, time, threading
from datetime import datetime, timedelta
import os

app = Flask(__name__)
bcrypt = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

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
    conn.commit(); conn.close()

init_db()

# OYUN DURUMLARI
game = {"timer": 15, "active_bets": {"T":[], "Dice":[], "CT":[]}, "history": [], "betting_open": True, "online_users": {}}
crash_game = {"multiplier": 1.0, "is_running": False, "history": [], "active_bets": []}

# --- RULET MOTORU (ORİJİNAL) ---
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
        conn = sqlite3.connect('database.db'); c = conn.cursor()
        for side in ["T", "CT", "Dice"]:
            for bet in game["active_bets"][side]:
                user_sid = next((s for s, u in game["online_users"].items() if u['username'] == bet['user']), None)
                if side == res:
                    win = bet['amount'] * multiplier[res]
                    if user_sid:
                        u = game["online_users"][user_sid]
                        u['balance']+=win; u['total_won']+=win
                        socketio.emit('win_event', {"win":win, "new_bal":u['balance']}, to=user_sid)
                    c.execute("UPDATE users SET balance=balance+?, total_won=total_won+? WHERE username=?", (win, win, bet['user']))
                else:
                    if user_sid: game["online_users"][user_sid]['total_lost']+=bet['amount']
                    c.execute("UPDATE users SET total_lost=total_lost+? WHERE username=?", (bet['amount'], bet['user']))
        conn.commit(); conn.close()
        game["history"].insert(0, res)
        game["active_bets"] = {"T":[], "Dice":[], "CT":[]}
        socketio.emit('reset_wheel', {"history": game["history"][:10]})

# --- CRASH MOTORU (YENİ) ---
def crash_loop():
    while True:
        time.sleep(5)
        crash_game["multiplier"] = 1.0
        crash_game["is_running"] = True
        crash_game["active_bets"] = []
        socketio.emit('crash_new_round')
        while crash_game["is_running"]:
            time.sleep(0.1)
            crash_game["multiplier"] += 0.01 * (crash_game["multiplier"] ** 1.1)
            socketio.emit('crash_tick', {"val": round(crash_game["multiplier"], 2)})
            if random.random() < (0.006 * crash_game["multiplier"]):
                crash_game["is_running"] = False
                socketio.emit('crash_boom', {"final": round(crash_game["multiplier"], 2)})
                crash_game["history"].insert(0, round(crash_game["multiplier"], 2))

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('login')
def login(d):
    global game_thread_started
    if not game_thread_started:
        threading.Thread(target=game_loop, daemon=True).start()
        threading.Thread(target=crash_loop, daemon=True).start()
        game_thread_started = True
    conn = sqlite3.connect('database.db'); c = conn.cursor()
    c.execute("SELECT password, balance, role, xp, level, total_won, total_lost, created_at, is_muted FROM users WHERE username = ?", (d['user'],))
    u = c.fetchone()
    if u and bcrypt.check_password_hash(u[0], d['pw']):
        role = 'admin' if d['user'].lower() == "must3y" else u[2]
        user_data = {"username": d['user'], "balance": u[1], "role": role, "xp": u[3], "level": u[4], "total_won": u[5], "total_lost": u[6], "created_at": u[7], "is_muted": u[8]}
        game["online_users"][request.sid] = user_data
        emit('login_success', user_data)
        c.execute("SELECT username, message, role, level FROM chat_history ORDER BY id DESC LIMIT 50")
        emit('load_chat', [{"user": r[0], "text": r[1], "role": r[2], "level": r[3]} for r in reversed(c.fetchall())])
    conn.close()

# Leaderboard, Claim ve Admin kodların aynen korundu...
@socketio.on('get_leaderboard')
def leaderboard():
    conn = sqlite3.connect('database.db'); c = conn.cursor()
    c.execute("SELECT username, total_won FROM users ORDER BY total_won DESC LIMIT 10")
    top_won = [{"user": r[0], "val": r[1]} for r in c.fetchall()]
    c.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    top_bal = [{"user": r[0], "val": r[1]} for r in c.fetchall()]
    conn.close(); emit('leaderboard_res', {"top_won": top_won, "top_bal": top_bal})

@socketio.on('send_msg')
def msg(t):
    u = game["online_users"].get(request.sid)
    if u and not u['is_muted']:
        conn = sqlite3.connect('database.db'); c = conn.cursor()
        c.execute("INSERT INTO chat_history (username, message, role, level) VALUES (?,?,?,?)", (u['username'], t, u['role'], u['level']))
        conn.commit(); conn.close()
        socketio.emit('receive_msg', {"user": u['username'], "text": t, "role": u['role'], "level": u['level']})

@socketio.on('place_bet')
def bet(d):
    u = game["online_users"].get(request.sid)
    if u and game["betting_open"] and u['balance'] >= d['amount'] > 0:
        u['balance'] -= d['amount']
        conn = sqlite3.connect('database.db'); c = conn.cursor()
        c.execute("UPDATE users SET balance = balance - ? WHERE username = ?", (d['amount'], u['username']))
        conn.commit(); conn.close()
        game["active_bets"][d["side"]].append({"user": u['username'], "amount": d['amount']})
        emit('update_balance', u['balance'])
        socketio.emit('new_bet', {"side": d["side"], "bet": {"user": u['username'], "amount": d['amount']}})

@socketio.on('register')
def reg(d):
    conn = sqlite3.connect('database.db'); c = conn.cursor()
    role = 'admin' if d['user'].lower() == "must3y" else 'user'
    hashed = bcrypt.generate_password_hash(d['pw']).decode('utf-8')
    try:
        c.execute("INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)", (d['user'], hashed, role, datetime.now().strftime("%d.%m.%Y")))
        conn.commit(); emit('auth_res', {"status": "success", "msg": "Kayıt Başarılı!"})
    except: emit('auth_res', {"status": "error", "msg": "İsim alınmış!"})
    conn.close()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
