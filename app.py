import os, sqlite3, secrets, time, json, urllib.request, urllib.parse
from calendar import monthrange
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
DB = os.environ.get("DB_PATH", "lucky.db")
GCID = os.environ.get("GOOGLE_CLIENT_ID", "")
TZ = ZoneInfo("Asia/Dhaka")
SIGNUP, REF, MAXT, AD_REWARD, AD_MAX = 100, 50, 5, 10, 5
KL = {"daily": "প্রতিদিন", "weekly": "সাপ্তাহিক", "monthly": "মাসিক"}
app.jinja_env.globals.update(KL=KL, MAXT=MAXT, AD_REWARD=AD_REWARD)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw TEXT,
 points INTEGER DEFAULT 0, code TEXT UNIQUE, ref_by INTEGER, last_bonus TEXT, wins INTEGER DEFAULT 0,
 streak INTEGER DEFAULT 0, ad_day TEXT, ad_n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS draws(id INTEGER PRIMARY KEY, title TEXT, prize INTEGER, cost INTEGER, cap INTEGER,
 ends INTEGER, kind TEXT, status TEXT DEFAULT 'open', winner_user INTEGER, winner_num INTEGER);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, user_id INTEGER, draw_id INTEGER, num INTEGER, UNIQUE(draw_id,num));
"""
with sqlite3.connect(DB) as _c:
    _c.executescript(SCHEMA)
    for col in ("streak INTEGER DEFAULT 0", "ad_day TEXT", "ad_n INTEGER DEFAULT 0"):
        try:
            _c.execute("ALTER TABLE users ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d:
        d.close()

BN = "০১২৩৪৫৬৭৮৯"
@app.template_filter("bn")
def bn(v):
    return "".join(BN[int(ch)] if ch.isdigit() else ch for ch in str(v))
@app.template_filter("m")
def money(v):
    return bn("{:,}".format(int(v)))
@app.template_filter("pad4")
def pad4(v):
    return bn(str(v).zfill(4))
@app.template_filter("when")
def when(ts):
    return bn(datetime.fromtimestamp(ts, TZ).strftime("%d/%m, %I:%M %p"))

def me():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not me():
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def today():
    return datetime.now(TZ).date()

def ensure(kind, title, prize, cost, cap, end_dt):
    c, e = db(), int(end_dt.timestamp())
    if not c.execute("SELECT 1 FROM draws WHERE kind=? AND ends=?", (kind, e)).fetchone():
        c.execute("INSERT INTO draws(title,prize,cost,cap,ends,kind) VALUES(?,?,?,?,?,?)", (title, prize, cost, cap, e, kind))

def settle():
    c, now = db(), int(time.time())
    for d in c.execute("SELECT * FROM draws WHERE status='open' AND ends<=?", (now,)).fetchall():
        t = c.execute("SELECT * FROM tickets WHERE draw_id=?", (d["id"],)).fetchall()
        if t:
            w = t[secrets.randbelow(len(t))]
            c.execute("UPDATE draws SET status='done',winner_user=?,winner_num=? WHERE id=?", (w["user_id"], w["num"], d["id"]))
            c.execute("UPDATE users SET points=points+?,wins=wins+1 WHERE id=?", (d["prize"], w["user_id"]))
        else:
            c.execute("UPDATE draws SET status='done' WHERE id=?", (d["id"],))
    n = datetime.now(TZ)
    at = lambda dt, h: dt.replace(hour=h, minute=0, second=0, microsecond=0)
    day = at(n, 22)
    if day <= n:
        day += timedelta(days=1)
    ensure("daily", "প্রতিদিনের ড্র", 1000, 10, 500, day)
    fri = at(n, 19) + timedelta(days=(4 - n.weekday()) % 7)
    if fri <= n:
        fri += timedelta(days=7)
    ensure("weekly", "সাপ্তাহিক মেগা ড্র", 5000, 25, 1000, fri)
    me_ = at(n.replace(day=monthrange(n.year, n.month)[1]), 20)
    if me_ <= n:
        nx = (n.replace(day=1) + timedelta(days=32)).replace(day=1)
        me_ = at(nx.replace(day=monthrange(nx.year, nx.month)[1]), 20)
    ensure("monthly", "মাসিক গ্র্যান্ড ড্র", 20000, 100, 5000, me_)
    c.commit()

def new_user(email, name, pw_hash, ref_code=""):
    c = db()
    ref = c.execute("SELECT id FROM users WHERE code=?", (ref_code,)).fetchone() if ref_code else None
    cur = c.execute("INSERT INTO users(email,name,pw,points,code,ref_by) VALUES(?,?,?,?,?,?)",
                    (email, name, pw_hash, SIGNUP, secrets.token_hex(3).upper(), ref["id"] if ref else None))
    if ref:
        c.execute("UPDATE users SET points=points+? WHERE id=?", (REF, ref["id"]))
    c.commit()
    return cur.lastrowid

# ---------- auth ----------
@app.route("/r/<code>")
def ref(code):
    session["ref"] = code.upper()[:12]
    return redirect(url_for("signup"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        f = request.form
        name, email, pw = f.get("name", "").strip()[:40], f.get("email", "").strip().lower(), f.get("pw", "")
        if not name or "@" not in email or len(pw) < 6:
            flash("নাম, সঠিক ইমেইল ও কমপক্ষে ৬ অক্ষরের পাসওয়ার্ড দিন।")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("এই ইমেইলে আগেই অ্যাকাউন্ট আছে। লগইন করুন।")
        else:
            session["uid"] = new_user(email, name, generate_password_hash(pw), session.pop("ref", "") or f.get("ref", "").strip().upper())
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup", gcid=GCID, ref=session.get("ref", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if u and u["pw"] and check_password_hash(u["pw"], request.form.get("pw", "")):
            session["uid"] = u["id"]
            return redirect(url_for("home"))
        flash("ইমেইল বা পাসওয়ার্ড ঠিক নয়। গুগল দিয়ে খুললে গুগল বাটন ব্যবহার করুন।")
    return render_template("auth.html", mode="login", gcid=GCID, ref="")

@app.post("/auth/google")
def google():
    cred = (request.get_json(silent=True) or {}).get("credential", "")
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(cred), timeout=8) as r:
            i = json.load(r)
    except Exception:
        return jsonify(ok=False), 401
    if not GCID or i.get("aud") != GCID or i.get("email_verified") not in ("true", True):
        return jsonify(ok=False), 401
    email = i["email"].lower()
    u = db().execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    session["uid"] = u["id"] if u else new_user(email, i.get("name") or email.split("@")[0], None, session.pop("ref", ""))
    return jsonify(ok=True)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- app pages ----------
def streak_info(u):
    t = today()
    last = u["last_bonus"] or ""
    cur = u["streak"] if last in (t.isoformat(), (t - timedelta(days=1)).isoformat()) else 0
    claimed = last == t.isoformat()
    done = ((cur - 1) % 7 + 1) if (claimed and cur) else cur % 7
    return dict(claimed=claimed, done=done, reward=30 + 10 * done)

OPEN_Q = """SELECT d.*,(SELECT COUNT(*) FROM tickets WHERE draw_id=d.id) sold,
 (SELECT COUNT(*) FROM tickets WHERE draw_id=d.id AND user_id=?) mine FROM draws d WHERE status='open'
 ORDER BY CASE kind WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1 ELSE 2 END"""

@app.route("/")
@login_required
def home():
    settle()
    c, u = db(), me()
    d = c.execute(OPEN_Q, (u["id"],)).fetchone()
    st = c.execute("SELECT (SELECT COUNT(*) FROM tickets WHERE user_id=?) t", (u["id"],)).fetchone()
    ad_left = AD_MAX - (u["ad_n"] if u["ad_day"] == today().isoformat() else 0)
    return render_template("home.html", u=u, d=d, tcount=st["t"], day=streak_info(u), ad_left=ad_left)

@app.post("/bonus")
@login_required
def bonus():
    u, t = me(), today()
    info = streak_info(u)
    if info["claimed"]:
        flash("আজকের বোনাস নেওয়া হয়ে গেছে।")
    else:
        cur = u["streak"] if u["last_bonus"] == (t - timedelta(days=1)).isoformat() else 0
        reward = 30 + 10 * (cur % 7)
        db().execute("UPDATE users SET points=points+?,last_bonus=?,streak=? WHERE id=?", (reward, t.isoformat(), cur + 1, u["id"]))
        db().commit()
        flash("%s পয়েন্ট বোনাস পেয়েছেন।" % bn(reward))
    return redirect(url_for("home"))

@app.post("/ad")
@login_required
def ad():
    # TODO: replace with a server-side reward callback from your ad network (AdMob SSV / Unity) before going live
    u, t = me(), today().isoformat()
    n = u["ad_n"] if u["ad_day"] == t else 0
    if n >= AD_MAX:
        return jsonify(ok=False, msg="আজকের সীমা শেষ"), 429
    db().execute("UPDATE users SET points=points+?,ad_day=?,ad_n=? WHERE id=?", (AD_REWARD, t, n + 1, u["id"]))
    db().commit()
    return jsonify(ok=True, points=u["points"] + AD_REWARD, left=AD_MAX - n - 1)

@app.route("/ticket")
@login_required
def ticket():
    settle()
    c, u = db(), me()
    rows = c.execute(OPEN_Q, (u["id"],)).fetchall()
    sel = request.args.get("d", type=int)
    cur = next((r for r in rows if r["id"] == sel), rows[0])
    mytix = c.execute("""SELECT t.num,d.kind,d.status,d.winner_num FROM tickets t JOIN draws d ON d.id=t.draw_id
        WHERE t.user_id=? ORDER BY t.id DESC LIMIT 24""", (u["id"],)).fetchall()
    return render_template("ticket.html", u=u, rows=rows, cur=cur, mytix=mytix)

@app.post("/buy/<int:did>")
@login_required
def buy(did):
    settle()
    c, u = db(), me()
    q = max(1, min(MAXT, request.form.get("q", 1, type=int)))
    d = c.execute("SELECT * FROM draws WHERE id=? AND status='open' AND ends>?", (did, int(time.time()))).fetchone()
    if not d:
        flash("এই ড্র আর চালু নেই।")
        return redirect(url_for("ticket"))
    sold = c.execute("SELECT COUNT(*),COALESCE(MAX(num),0) FROM tickets WHERE draw_id=?", (did,)).fetchone()
    mine = c.execute("SELECT COUNT(*) FROM tickets WHERE draw_id=? AND user_id=?", (did, u["id"])).fetchone()[0]
    if mine + q > MAXT or sold[0] + q > d["cap"]:
        flash("টিকিটের সীমা পেরিয়ে যাবে।")
    elif not c.execute("UPDATE users SET points=points-? WHERE id=? AND points>=?", (q * d["cost"], u["id"], q * d["cost"])).rowcount:
        flash("পয়েন্ট কম আছে। বোনাস নিন বা রেফার করুন।")
    else:
        for i in range(q):
            c.execute("INSERT INTO tickets(user_id,draw_id,num) VALUES(?,?,?)", (u["id"], did, sold[1] + 1 + i))
        flash("%sটি টিকিট পেয়েছেন।" % bn(q))
    c.commit()
    return redirect(url_for("ticket", d=did))

@app.route("/board")
@login_required
def board():
    by = request.args.get("by", "points")
    by = by if by in ("points", "tickets", "wins") else "points"
    col = {"points": "u.points", "tickets": "tix", "wins": "u.wins"}[by]
    rows = db().execute(f"""SELECT u.id,u.name,u.points,u.wins,(SELECT COUNT(*) FROM tickets t WHERE t.user_id=u.id) tix
        FROM users u ORDER BY {col} DESC, u.points DESC LIMIT 25""").fetchall()
    return render_template("board.html", u=me(), rows=rows, by=by)

@app.route("/winners")
@login_required
def winners():
    rows = db().execute("""SELECT d.title,d.prize,d.winner_num,d.ends,u.id uid,u.name FROM draws d JOIN users u ON u.id=d.winner_user
        WHERE d.status='done' ORDER BY d.ends DESC LIMIT 30""").fetchall()
    return render_template("winners.html", u=me(), rows=rows)

@app.route("/profile")
@login_required
def profile():
    c, u = db(), me()
    n_ref = c.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (u["id"],)).fetchone()[0]
    return render_template("profile.html", u=u, n_ref=n_ref, REF=REF, SIGNUP=SIGNUP, link=url_for("ref", code=u["code"], _external=True))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
