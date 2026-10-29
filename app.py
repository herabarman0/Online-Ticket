import os, re, hmac, secrets, time, json, urllib.request, urllib.parse
from urllib.parse import urlparse
from calendar import monthrange
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists
from markupsafe import Markup, escape
from flask import (Flask, g, render_template, request, redirect, url_for, session, flash,
                   jsonify, abort, send_from_directory)
from i18n import to_en
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, template_folder=".", static_folder=None)  # flat layout: all files in repo root
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=timedelta(days=180))

@app.route("/static/<path:filename>", endpoint="static")
def static_files(filename):
    if filename not in ("style.css", "desktop.css"):
        abort(404)
    return send_from_directory(app.root_path, filename)

GCID = os.environ.get("GOOGLE_CLIENT_ID", "")
TZ = ZoneInfo("Asia/Dhaka")
SIGNUP, REF, MAXT, AD_REWARD, AD_MAX = 100, 50, 5, 10, 5
TREASURY, FEE_PCT, MIN_SEND, MAX_SEND = 1_000_000_000, 5, 10, 100000  # admin pool, transfer fee %, send limits
KL = {"daily": "প্রতিদিন", "weekly": "সাপ্তাহিক", "monthly": "মাসিক", "custom": "বিশেষ"}
KO = {"daily": 0, "weekly": 1, "monthly": 2}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMINS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
app.jinja_env.globals.update(KL=KL, MAXT=MAXT, AD_REWARD=AD_REWARD, FEE_PCT=FEE_PCT)

_cred = os.environ.get("FIREBASE_CREDENTIALS", "")
if not _cred:
    raise RuntimeError("FIREBASE_CREDENTIALS environment variable is missing")
firebase_admin.initialize_app(credentials.Certificate(json.loads(_cred)))
fs = firestore.client()
DESC = firestore.Query.DESCENDING

BN = "০১২৩৪৫৬৭৮৯"
@app.template_filter("bn")
def bn(v):
    return "".join(BN[int(ch)] if ch.isdigit() else ch for ch in str(v))
@app.template_filter("m")
def money(v):
    return bn("{:,}".format(int(v)))
@app.template_filter("short")
def short(v):
    # admin balance: 100M, 1.5B ... (users always see full numbers)
    v = int(v)
    for n, suf in ((10**12, "T"), (10**9, "B"), (10**6, "M")):
        if abs(v) >= n:
            return ("%.2f" % (v / n)).rstrip("0").rstrip(".") + suf
    return "{:,}".format(v)
@app.template_filter("taka")
def taka(p):
    return bn("{:,}".format(int(int(p) * get_rate() / 1000)))
@app.template_filter("keep")
def keep(v):
    # text written by admins/users: never translated (markers are removed before the page is sent)
    return Markup("\u2063" + str(escape(v)) + "\u2063")
@app.template_filter("pad4")
def pad4(v):
    return bn(str(v).zfill(4))
@app.template_filter("when")
def when(ts):
    return bn(datetime.fromtimestamp(ts, TZ).strftime("%d/%m, %I:%M %p"))

# ---- tiny in-process cache (keeps Firestore reads low; single gunicorn worker) ----
_C = {}
def cget(k, ttl, fn):
    e = _C.get(k)
    if e and time.time() - e[0] < ttl:
        return e[1]
    v = fn()
    _C[k] = (time.time(), v)
    return v

def cdrop(*ks):
    for k in ks:
        if k.endswith("*"):
            for x in [x for x in _C if x.startswith(k[:-1])]:
                _C.pop(x, None)
        else:
            _C.pop(k, None)

def snap(s):
    x = s.to_dict() or {}
    x["id"] = s.id
    return x

def me():
    if "_me" in g:
        return g._me
    u, uid = None, session.get("uid")
    if uid:
        session.permanent = True  # stay signed in (cookie lasts 180 days, renewed on use)
        s = fs.collection("users").document(uid).get()
        u = snap(s) if s.exists else None
        if u and u.get("banned") and u["email"] not in ADMINS:
            session.clear()
            flash("আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে।")
            u = None
    g._me = u
    return u

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not me():
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def today():
    return datetime.now(TZ).date()

def by_email(e):
    for s in fs.collection("users").where("email", "==", e).limit(1).stream():
        return snap(s)

def gen_pid():
    while True:
        p = str(secrets.randbelow(9000000) + 1000000)
        if not list(fs.collection("users").where("pid", "==", p).limit(1).stream()):
            return p

def is_pending(u):
    return u.get("status", "approved") != "approved"  # accounts made before approval existed count as approved

def new_user(email, name, pw_hash, ref_code="", phone="", extra=None, admin=False):
    ref = None
    if ref_code:
        for fld in ("code", "pid"):
            for s in fs.collection("users").where(fld, "==", ref_code).limit(1).stream():
                ref = ref or s
    r = fs.collection("users").document()
    pid = gen_pid()
    d = {"email": email, "name": name, "pw": pw_hash, "points": 0, "code": pid, "pid": pid,
         "ref_by": ref.id if ref else None, "phone": phone, "created": time.time(), "banned": False, "last_bonus": "", "wins": 0,
         "tix": 0, "streak": 0, "ad_day": "", "ad_n": 0, "wl": [], "tk": [], "mine": {}, "refs": 0, "txl": [],
         "status": "approved" if admin else "pending"}
    d.update(extra or {})
    r.set(d)
    if ref:
        ref.reference.update({"refs": firestore.Increment(1)})  # referral points are paid when the admin approves this account
    return r.id

def ensure_pid(u):
    if not u.get("pid") or u.get("code") != u["pid"]:  # referral code is always the user ID
        u["pid"] = u.get("pid") or gen_pid()
        u["code"] = u["pid"]
        fs.collection("users").document(u["id"]).update({"pid": u["pid"], "code": u["code"]})

TREF = fs.collection("config").document("treasury")
def tre_bal():
    s = TREF.get()
    return (s.to_dict() or {}).get("bal", TREASURY) if s.exists else TREASURY

def _bal(s):
    return (s.to_dict() or {}).get("bal", TREASURY) if s.exists else TREASURY

def get_rate():
    # taka value of 1000 points (display only), set by admin
    def load():
        s = TREF.get()
        return (s.to_dict() or {}).get("rate", 100) if s.exists else 100
    return cget("rate", 300, load)

def parse_amt(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmbKMB]?)", en(s).replace(",", "").strip())
    if not m:
        raise ValueError
    return int(float(m.group(1)) * {"": 1, "k": 10**3, "m": 10**6, "b": 10**9}[m.group(2).lower()])

# ---------- draws (admin-created only; no automatic draws) ----------
SPIN = 60  # seconds of live spin, same moment on every phone

PLACE = ["১ম", "২য়", "৩য়"]
RNG = secrets.SystemRandom()

@app.template_filter("prizeline")
def prizeline(d):
    ps = d.get("prizes") or [d["prize"]]
    return " · ".join("%s %s" % (PLACE[i], money(p)) for i, p in enumerate(ps))

@firestore.transactional
def _finish_tx(tx, dref):
    # at the draw time: pick up to 3 different winners (stored, hidden until the spin ends)
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "open" or d["ends"] > time.time():
        return
    ts = TREF.get(transaction=tx)
    prizes = d.get("prizes") or [d["prize"]]
    ws, seen = [], set()
    if d["sold"] > 0:
        for num in RNG.sample(range(1, d["sold"] + 1), min(d["sold"], 60)):
            t = dref.collection("tickets").document(str(num)).get(transaction=tx).to_dict()
            if t and t["uid"] not in seen:
                seen.add(t["uid"])
                ws.append({"place": len(ws) + 1, "num": num, "uid": t["uid"], "prize": prizes[len(ws)]})
                if len(ws) == len(prizes):
                    break
    upd = {"status": "drawing" if ws else "done", "winners": ws}
    if ws:
        upd.update(winner_num=ws[0]["num"], winner_user=ws[0]["uid"])
    unused = (sum(prizes) - sum(w["prize"] for w in ws)) if d.get("reserved") else 0
    if unused > 0:  # prizes nobody won go back to the admin balance
        tx.set(TREF, {"bal": _bal(ts) + unused}, merge=True)
    tx.update(dref, upd)

@firestore.transactional
def _award_tx(tx, dref):
    # after the spin: credit every winner and publish the results
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "drawing" or time.time() < d["ends"] + SPIN:
        return
    legacy = "winners" not in d
    ws = d.get("winners") or [{"place": 1, "num": d["winner_num"], "uid": d["winner_user"], "prize": d["prize"]}]
    got = [(w, fs.collection("users").document(w["uid"])) for w in ws]
    got = [(w, r, r.get(transaction=tx)) for w, r in got]
    out = []
    for w, uref, us in got:
        if not us.exists:
            continue
        u = us.to_dict()
        rid = dref.id if legacy else "%s_%d" % (dref.id, w["place"])
        upd = {"points": u["points"] + w["prize"], "wins": u.get("wins", 0) + 1}
        if "wl" in u or u.get("wins", 0) == 0:  # pending-prize list lives on the user doc (no extra reads later)
            upd["wl"] = (u.get("wl") or []) + [{"id": rid, "title": d["title"], "prize": w["prize"], "winner_num": w["num"],
                                                "ends": d["ends"], "state": "need", "place": w["place"]}]
        tx.update(uref, upd)
        tx.set(fs.collection("results").document(rid), {"uid": w["uid"], "name": u["name"], "title": d["title"], "prize": w["prize"],
                                                        "winner_num": w["num"], "ends": d["ends"], "place": w["place"]})
        out.append(dict(w, name=u["name"]))
    tx.update(dref, {"status": "done", "winners": out})

def active():
    # open / drawing admin draws: ONE query per 30s for the whole site
    def load():
        return [snap(s) for s in fs.collection("draws").where("status", "in", ["open", "drawing"]).stream()
                if (s.to_dict() or {}).get("kind") == "custom"]
    return cget("act", 30, load)

def draw_get(did):
    e = _C.get("dr:" + did)
    if e and (e[1] and e[1]["status"] in ("done", "cancelled") or time.time() - e[0] < 15):
        return e[1]
    s = fs.collection("draws").document(did).get()
    v = snap(s) if s.exists else None
    _C["dr:" + did] = (time.time(), v)
    return v

def changed(did=None):
    cdrop("act", "last", "res", "board:*")
    if did:
        cdrop("dr:" + did)

def settle(force=False):
    now, ch = time.time(), False
    for d in active():
        dref = fs.collection("draws").document(d["id"])
        if d["status"] == "open" and d["ends"] <= now:
            _finish_tx(fs.transaction(), dref)
            ch = True
        elif d["status"] == "drawing" and now >= d["ends"] + SPIN:
            _award_tx(fs.transaction(), dref)
            ch = True
        if ch:
            changed(d["id"])

def open_draws(u):
    now, mine = time.time(), u.get("mine") or {}
    out = [dict(d) for d in active() if d["status"] == "open" and d["ends"] > now]
    out.sort(key=lambda d: d["ends"])
    for d in out:
        d["mine"] = mine.get(d["id"], 0)
    return out

def my_tickets(u):
    out = []
    for t in (u.get("tk") or [])[:12]:
        t = dict(t)
        x = draw_get(t["draw"]) or {}
        t["status"], t["winner_num"] = x.get("status", ""), x.get("winner_num")
        t["title"] = x.get("title") or t.get("title", "")
        out.append(t)
    return out

def my_wins(u):
    # prizes this user won whose details are not yet delivered (stored on the user doc: zero extra reads)
    if "wl" not in u:
        wl = []
        if u.get("wins", 0) > 0:  # one-time migration for accounts made before this change
            rs = [snap(x) for x in fs.collection("results").where("uid", "==", u["id"]).limit(20).stream()]
            cl = {x.id: (x.to_dict() or {}) for x in fs.get_all([fs.collection("claims").document(r["id"]) for r in rs]) if x.exists} if rs else {}
            for r in sorted(rs, key=lambda r: -r["ends"]):
                c = cl.get(r["id"])
                if c is None:
                    wl.append(dict(id=r["id"], title=r["title"], prize=r["prize"], winner_num=r["winner_num"], ends=r["ends"], state="need"))
                elif c.get("status") == "pending":
                    wl.append(dict(id=r["id"], title=r["title"], prize=r["prize"], winner_num=r["winner_num"], ends=r["ends"], state="wait"))
        fs.collection("users").document(u["id"]).update({"wl": wl})
        u["wl"] = wl
    return sorted(u["wl"], key=lambda r: -r["ends"])

def last_result():
    def load():
        rs = [snap(x) for x in fs.collection("results").order_by("ends", direction=DESC).limit(3).stream()]
        if rs:
            top = [r for r in rs if r["ends"] == rs[0]["ends"]]
            r = min(top, key=lambda r: r.get("place", 1))
            r["mname"] = mask_name(r.get("name", ""))
            return r
    return cget("last", 60, load)

# ---------- auth ----------
@app.route("/lang/<code>")
def set_lang(code):
    session["lang"] = "en" if code == "en" else "bn"
    session.permanent = True
    back = urlparse(request.referrer or "")
    return redirect(back.path or "/" if back.netloc == request.host else "/")

@app.after_request
def _translate(resp):
    # English version: translate rendered user pages (admin pages stay Bengali)
    if resp.mimetype == "text/html" and not request.path.startswith("/admin") and resp.status_code < 400:
        h = resp.get_data(as_text=True)
        resp.set_data(to_en(h) if session.get("lang") == "en" else h.replace("\u2063", ""))
    return resp

@app.route("/r/<code>")
def ref(code):
    session["ref"] = code.upper()[:12]
    return redirect(url_for("signup"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    f = request.form
    if request.method == "POST":
        name, email, pw = f.get("name", "").strip()[:40], f.get("email", "").strip().lower(), f.get("pw", "")
        phone, addr, fb = norm_phone(f.get("phone", "")), f.get("address", "").strip()[:200], fb_link(f.get("fb", ""))
        try:
            age = int(en(f.get("age", "")).strip())
        except ValueError:
            age = None
        if len(name) < 3 or "@" not in email or len(pw) < 6:
            flash("পুরো নাম, সঠিক ইমেইল ও কমপক্ষে ৬ অক্ষরের পাসওয়ার্ড দিন।")
        elif not re.fullmatch(r"01\d{9}", phone):
            flash("সঠিক মোবাইল নম্বর দিন (যেমন ০১৭XXXXXXXX)।")
        elif len(addr) < 10:
            flash("পুরো ঠিকানা লিখুন (গ্রাম/এলাকা, থানা, জেলা সহ)।")
        elif age is None or age < 18 or age > 100:
            flash("অ্যাকাউন্ট খুলতে বয়স কমপক্ষে ১৮ হতে হবে।")
        elif not fb:
            flash("সঠিক ফেসবুক প্রোফাইল লিংক দিন (facebook.com/...)।")
        elif not f.get("ok"):
            flash("সম্মতি বক্সে টিক দিন।")
        elif email in ADMINS:
            flash("এই ইমেইলে অ্যাকাউন্ট খুলতে হবে না, সরাসরি লগইন করুন।")
        elif by_email(email):
            flash("এই ইমেইলে আগেই অ্যাকাউন্ট আছে। লগইন করুন।")
        else:
            session["uid"] = new_user(email, name, generate_password_hash(pw), session.pop("ref", "") or f.get("ref", "").strip().upper(),
                                      phone, dict(addr=addr, age=age, fb=fb))
            flash("অ্যাকাউন্ট খোলা হয়েছে। অ্যাডমিন অ্যাপ্রুভ করলে ১০০ পয়েন্ট বোনাস পাবেন।")
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup", gcid=GCID, ref=session.get("ref", ""), f=f)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw = request.form.get("email", "").strip().lower(), request.form.get("pw", "")
        if email in ADMINS:
            # admin signs in with the ADMIN_PASSWORD set in Render's Environment
            if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                u = by_email(email)
                session["uid"] = u["id"] if u else new_user(email, "Admin", None, admin=True)
                return redirect(url_for("admin"))
            time.sleep(1)
        else:
            u = by_email(email)
            if u and u.get("pw") and check_password_hash(u["pw"], pw):
                if u.get("banned"):
                    flash("আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে। অ্যাডমিনের সাথে যোগাযোগ করুন।")
                    return render_template("auth.html", mode="login", gcid=GCID, ref="", f={})
                session["uid"] = u["id"]
                fs.collection("users").document(u["id"]).update({"last_login": time.time()})
                return redirect(url_for("home"))
        flash("ইমেইল বা পাসওয়ার্ড ঠিক নয়।")
    return render_template("auth.html", mode="login", gcid=GCID, ref="", f={})

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
    u = by_email(email)
    session["uid"] = u["id"] if u else new_user(email, i.get("name") or email.split("@")[0], None, session.pop("ref", ""))
    return jsonify(ok=True)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- app pages ----------
def streak_info(u):
    t = today()
    last = u.get("last_bonus") or ""
    cur = u.get("streak", 0) if last in (t.isoformat(), (t - timedelta(days=1)).isoformat()) else 0
    claimed = last == t.isoformat()
    done = ((cur - 1) % 7 + 1) if (claimed and cur) else cur % 7
    return dict(claimed=claimed, done=done, reward=30 + 10 * done)

@app.route("/")
@login_required
def home():
    settle()
    u = me()
    ds = open_draws(u)
    d = ds[0] if ds else None
    live_d = next((dict(x) for x in active() if x["status"] == "drawing"), None)
    if not live_d and d and d["ends"] - time.time() < 600:
        live_d = d
    last = None if (d or live_d) else last_result()
    ad_left = AD_MAX - (u.get("ad_n", 0) if u.get("ad_day") == today().isoformat() else 0)
    return render_template("home.html", u=u, d=d, tcount=u.get("tix", 0), day=streak_info(u), ad_left=ad_left,
                           live_d=live_d, last=last, wins=my_wins(u), now=time.time(), notes=notices_now())

@firestore.transactional
def _bonus_tx(tx, ref_, t, y):
    u = ref_.get(transaction=tx).to_dict()
    ts = TREF.get(transaction=tx)
    if u.get("last_bonus") == t:
        return 0
    cur = u.get("streak", 0) if u.get("last_bonus") == y else 0
    reward = 30 + 10 * (cur % 7)
    bal = _bal(ts)
    if bal < reward:
        return -1
    tx.update(ref_, {"points": u["points"] + reward, "last_bonus": t, "streak": cur + 1})
    tx.set(TREF, {"bal": bal - reward}, merge=True)  # every point comes from the admin balance
    return reward

@app.post("/bonus")
@login_required
def bonus():
    if is_pending(me()):
        flash("অ্যাডমিন অ্যাপ্রুভ করার পর বোনাস নিতে পারবেন।")
        return redirect(url_for("home"))
    t = today()
    r = _bonus_tx(fs.transaction(), fs.collection("users").document(me()["id"]), t.isoformat(), (t - timedelta(days=1)).isoformat())
    flash("এখন বোনাস দেওয়া যাচ্ছে না, পরে চেষ্টা করুন।" if r == -1 else ("%s পয়েন্ট বোনাস পেয়েছেন।" % bn(r) if r else "আজকের বোনাস নেওয়া হয়ে গেছে।"))
    return redirect(url_for("home"))

@firestore.transactional
def _ad_tx(tx, ref_, t):
    u = ref_.get(transaction=tx).to_dict()
    ts = TREF.get(transaction=tx)
    n = u.get("ad_n", 0) if u.get("ad_day") == t else 0
    if n >= AD_MAX:
        return None
    bal = _bal(ts)
    if bal < AD_REWARD:
        return "empty"
    tx.update(ref_, {"points": u["points"] + AD_REWARD, "ad_day": t, "ad_n": n + 1})
    tx.set(TREF, {"bal": bal - AD_REWARD}, merge=True)
    return u["points"] + AD_REWARD, AD_MAX - n - 1

@app.post("/ad")
@login_required
def ad():
    # TODO: replace with a server-side reward callback from your ad network (AdMob SSV / Unity) before going live
    if is_pending(me()):
        return jsonify(ok=False, msg="অ্যাকাউন্ট অ্যাপ্রুভ হয়নি"), 403
    r = _ad_tx(fs.transaction(), fs.collection("users").document(me()["id"]), today().isoformat())
    if r == "empty":
        return jsonify(ok=False, msg="এখন পুরস্কার নেই"), 503
    if not r:
        return jsonify(ok=False, msg="আজকের সীমা শেষ"), 429
    return jsonify(ok=True, points=r[0], left=r[1])

@app.route("/ticket")
@login_required
def ticket():
    settle()
    u = me()
    rows = open_draws(u)
    sel = request.args.get("d")
    cur = next((r for r in rows if r["id"] == sel), rows[0] if rows else None)
    return render_template("ticket.html", u=u, rows=rows, cur=cur, mytix=my_tickets(u),
                           last=(None if rows else last_result()), wins=my_wins(u), now=time.time())

class Fail(Exception):
    pass

@firestore.transactional
def _buy_tx(tx, uref, dref, q, now, keep):
    u = uref.get(transaction=tx).to_dict()
    ds = dref.get(transaction=tx)
    ts = TREF.get(transaction=tx)
    if not ds.exists:
        raise Fail("এই ড্র আর চালু নেই।")
    d = ds.to_dict()
    mine = dict(u.get("mine") or {})
    have = mine.get(dref.id, 0)
    if d["status"] != "open" or d["ends"] <= now:
        raise Fail("এই ড্র আর চালু নেই।")
    if have + q > MAXT or d["sold"] + q > d["cap"]:
        raise Fail("টিকিটের সীমা পেরিয়ে যাবে।")
    cost = q * d["cost"]
    if u["points"] < cost:
        raise Fail("পয়েন্ট কম আছে। বোনাস নিন বা রেফার করুন।")
    tk = list(u.get("tk") or [])
    for i in range(q):
        num = d["sold"] + 1 + i
        tx.set(dref.collection("tickets").document(str(num)), {"uid": uref.id, "num": num})
        tk.insert(0, {"draw": dref.id, "kind": d["kind"], "num": num, "ts": now, "title": d["title"]})
    mine = {k: v for k, v in mine.items() if k in keep}
    mine[dref.id] = have + q
    tx.update(dref, {"sold": d["sold"] + q})
    tx.set(TREF, {"bal": _bal(ts) + cost}, merge=True)  # ticket money goes to the admin balance
    tx.update(uref, {"points": u["points"] - cost, "tix": u.get("tix", 0) + q, "tk": tk[:60], "mine": mine})

@app.post("/buy/<did>")
@login_required
def buy(did):
    if is_pending(me()):
        flash("অ্যাডমিন অ্যাপ্রুভ করার পর টিকিট কিনতে পারবেন।")
        return redirect(url_for("ticket", d=did))
    q = max(1, min(MAXT, request.form.get("q", 1, type=int)))
    uref = fs.collection("users").document(me()["id"])
    dref = fs.collection("draws").document(did)
    try:
        _buy_tx(fs.transaction(), uref, dref, q, time.time(), {x["id"] for x in active()})
        cdrop("act", "dr:" + did)
        flash("%sটি টিকিট পেয়েছেন।" % bn(q))
    except Fail as e:
        flash(str(e))
    return redirect(url_for("ticket", d=did))

@app.route("/board")
@login_required
def board():
    by = request.args.get("by", "points")
    by = by if by in ("points", "tickets", "wins") else "points"
    col = {"points": "points", "tickets": "tix", "wins": "wins"}[by]
    rows = cget("board:" + by, 60, lambda: [snap(s) for s in fs.collection("users").order_by(col, direction=DESC).limit(25).stream()])
    return render_template("board.html", u=me(), rows=rows, by=by)

@app.route("/winners")
@login_required
def winners():
    u = me()
    rows = [dict(r) for r in cget("res", 30, lambda: [snap(s) for s in fs.collection("results").order_by("ends", direction=DESC).limit(30).stream()])]
    st = {w["id"]: w["state"] for w in my_wins(u)}
    for w in rows:
        w["claim"] = None
        if w["uid"] == u["id"]:
            w["claim"] = {"need": None, "wait": "pending"}.get(st.get(w["id"], "sent"), "sent")
    return render_template("winners.html", u=u, rows=rows)

@app.route("/profile")
@login_required
def profile():
    u = me()
    ensure_pid(u)
    n_ref = u["refs"] if "refs" in u else (cnt(fs.collection("users").where("ref_by", "==", u["id"])) or 0)
    return render_template("profile.html", u=u, n_ref=n_ref, REF=REF, SIGNUP=SIGNUP, link=url_for("ref", code=u["code"], _external=True), is_admin=u["email"] in ADMINS, pend=is_pending(u))


@app.post("/profile/info")
@login_required
def profile_info():
    f, u = request.form, me()
    addr, fb = f.get("address", "").strip()[:200], fb_link(f.get("fb", ""))
    try:
        age = int(en(f.get("age", "")).strip())
    except ValueError:
        age = None
    if len(addr) < 10:
        flash("পুরো ঠিকানা লিখুন (গ্রাম/এলাকা, থানা, জেলা সহ)।")
    elif age is None or age < 18 or age > 100:
        flash("বয়স কমপক্ষে ১৮ হতে হবে।")
    elif not fb:
        flash("সঠিক ফেসবুক প্রোফাইল লিংক দিন।")
    else:
        fs.collection("users").document(u["id"]).update({"addr": addr, "age": age, "fb": fb})
        flash("তথ্য সেভ হয়েছে।")
    return redirect(url_for("profile"))

# ---------- wallet: send / receive points by user ID ----------
@firestore.transactional
def _send_tx(tx, sref, rref, amt, fee):
    s_, r_, t_ = sref.get(transaction=tx), rref.get(transaction=tx), TREF.get(transaction=tx)
    if not s_.exists or not r_.exists:
        raise Fail("ইউজার পাওয়া যায়নি।")
    s, r = s_.to_dict(), r_.to_dict()
    if is_pending(s):
        raise Fail("অ্যাডমিন অ্যাপ্রুভ করার পর পয়েন্ট পাঠাতে পারবেন।")
    if is_pending(r) or r.get("banned"):
        raise Fail("এই আইডিতে এখন পয়েন্ট পাঠানো যাবে না।")
    if s["points"] < amt + fee:
        raise Fail("পয়েন্ট কম আছে। পাঠাতে লাগবে %s (ফি সহ)।" % bn(amt + fee))
    bal = (t_.to_dict() or {}).get("bal", TREASURY) if t_.exists else TREASURY
    now = time.time()
    tx.update(sref, {"points": s["points"] - amt - fee, "txl": ([{"t": "out", "name": r["name"], "pid": r.get("pid", ""), "amt": amt, "fee": fee, "ts": now}] + (s.get("txl") or []))[:30]})
    tx.update(rref, {"points": r["points"] + amt, "txl": ([{"t": "in", "name": s["name"], "pid": s.get("pid", ""), "amt": amt, "fee": 0, "ts": now}] + (r.get("txl") or []))[:30]})
    tx.set(TREF, {"bal": bal + fee}, merge=True)  # the fee goes back to the admin pool
    return r["name"]

@app.route("/wallet", methods=["GET", "POST"])
@login_required
def wallet():
    u = me()
    ensure_pid(u)
    tab = "recv" if request.args.get("t") == "recv" else "send"
    if request.method == "POST":
        pid = en(request.form.get("pid", "")).strip()
        try:
            amt = int(en(request.form.get("amount", "")).strip())
            assert MIN_SEND <= amt <= MAX_SEND
        except (ValueError, AssertionError):
            flash("পরিমাণ কমপক্ষে %s ও সর্বোচ্চ %s পয়েন্ট হতে হবে।" % (bn(MIN_SEND), bn(MAX_SEND)))
            return redirect(url_for("wallet"))
        fee = max(1, -(-amt * FEE_PCT // 100))
        rs = list(fs.collection("users").where("pid", "==", pid).limit(1).stream()) if pid else []
        if not rs:
            flash("এই আইডির কোনো ইউজার পাওয়া যায়নি।")
        elif rs[0].id == u["id"]:
            flash("নিজেকে পাঠানো যায় না।")
        else:
            try:
                nm = _send_tx(fs.transaction(), fs.collection("users").document(u["id"]), rs[0].reference, amt, fee)
                flash("%s-কে %s পয়েন্ট পাঠানো হয়েছে (ফি %s)।" % (nm, bn(amt), bn(fee)))
            except Fail as e:
                flash(str(e))
        return redirect(url_for("wallet"))
    return render_template("wallet.html", u=u, tab=tab, txl=u.get("txl") or [], MIN_SEND=MIN_SEND, pend=is_pending(u))

# ---------- prize claim (winner details) ----------
PHONE = re.compile(r"^(?:\+?88)?01\d{9}$")
def norm_phone(s):
    p = re.sub(r"[\s-]", "", en(s))
    if p.startswith("+88"):
        p = p[3:]
    elif p.startswith("88") and len(p) == 13:
        p = p[2:]
    return p

def en(s):
    return (s or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))

def fb_link(s):
    s = (s or "").strip()
    if not s.lower().startswith("http"):
        s = "https://" + s
    p = urlparse(s)
    h = (p.hostname or "").lower()
    if not (h in ("facebook.com", "fb.com", "fb.me") or h.endswith(".facebook.com") or h.endswith(".fb.com")):
        return None
    return "https://" + h + p.path + (("?" + p.query) if p.query else "")

@firestore.transactional
def _claim_tx(tx, cref, uref, data, rid):
    if cref.get(transaction=tx).exists:
        raise Fail("তথ্য আগেই জমা দিয়েছেন।")
    u = uref.get(transaction=tx).to_dict()
    wl = [dict(x) for x in (u.get("wl") or [])]
    w = next((x for x in wl if x["id"] == rid), None)
    if not w:
        raise Fail("এই পুরস্কার আপনার নয়।")
    w["state"] = "wait"
    tx.set(cref, dict(data, uid=uref.id, email=u["email"], title=w["title"] + (" (%s পুরস্কার)" % PLACE[w["place"] - 1] if w.get("place", 1) > 1 else ""), prize=w["prize"],
                      winner_num=w["winner_num"], status="pending", ts=time.time()))
    tx.update(uref, {"wl": wl})

@app.route("/claim/<rid>", methods=["GET", "POST"])
@login_required
def claim(rid):
    u = me()
    r = next((x for x in my_wins(u) if x["id"] == rid), None)
    if not r:
        abort(404)
    if r["state"] != "need":
        flash("তথ্য আগেই জমা দিয়েছেন।")
        return redirect(url_for("home"))
    err = None
    if request.method == "POST":
        f = request.form
        name, addr = f.get("name", "").strip()[:60], f.get("address", "").strip()[:200]
        phone, fb = norm_phone(f.get("phone", "")), fb_link(f.get("fb", ""))
        try:
            age = int(en(f.get("age", "")).strip())
        except ValueError:
            age = None
        if len(name) < 3:
            err = "পুরো নাম লিখুন (কমপক্ষে ৩ অক্ষর)।"
        elif not re.fullmatch(r"01\d{9}", phone):
            err = "সঠিক মোবাইল নম্বর দিন (১১ সংখ্যা, যেমন ০১৭XXXXXXXX)।"
        elif len(addr) < 10:
            err = "পুরো ঠিকানা লিখুন (গ্রাম/এলাকা, থানা, জেলা সহ)।"
        elif age is None or age < 18 or age > 100:
            err = "পুরস্কার নিতে বয়স কমপক্ষে ১৮ হতে হবে।"
        elif not fb:
            err = "সঠিক ফেসবুক প্রোফাইল লিংক দিন (facebook.com/... )।"
        elif not f.get("ok"):
            err = "নিচের সম্মতি বক্সে টিক দিন।"
        else:
            try:
                _claim_tx(fs.transaction(), fs.collection("claims").document(rid), fs.collection("users").document(u["id"]),
                          dict(name=name, phone=phone, address=addr, age=age, fb=fb), rid)
                flash("তথ্য জমা হয়েছে। অ্যাডমিন শীঘ্রই যোগাযোগ করবেন।")
                return redirect(url_for("home"))
            except Fail as e:
                err = str(e)
            except Exception:
                app.logger.exception("claim failed")
                err = "সার্ভারে সমস্যা হয়েছে, একটু পরে আবার চেষ্টা করুন।"
    pre = request.form if request.method == "POST" else dict(name=u.get("name", ""), phone=u.get("phone", ""), address=u.get("addr", ""),
                                                          age=u.get("age", ""), fb=u.get("fb", ""))
    return render_template("claim.html", u=u, r=r, f=pre, err=err)

# ---------- live draw ----------
def mask_name(n):
    return " ".join((w[:2] + "***") for w in (n or "").split())

@app.route("/live/<did>")
@login_required
def live(did):
    d = draw_get(did)
    if not d:
        abort(404)
    return render_template("live.html", u=me(), d=d)

@app.route("/live/<did>/state")
def live_state(did):
    if not session.get("uid"):  # session cookie is enough here: no user read per poll
        abort(401)
    d, now = draw_get(did), time.time()
    if not d:
        abort(404)
    dref = fs.collection("draws").document(did)
    if d["status"] == "open" and d["ends"] <= now:
        _finish_tx(fs.transaction(), dref)
        changed(did)
        d = draw_get(did)
    if d["status"] == "drawing" and now >= d["ends"] + SPIN:
        _award_tx(fs.transaction(), dref)
        changed(did)
        d = draw_get(did)
    out = dict(now=now, ends=d["ends"], status=d["status"], sold=d["sold"], title=d["title"], prize=d["prize"], spin=SPIN)
    if d["status"] in ("drawing", "done") and d.get("winner_num"):
        out["winner_num"] = d["winner_num"]
    out["prizes"] = d.get("prizes") or [d["prize"]]
    if d["status"] == "done" and d.get("winner_user"):
        ws = d.get("winners")
        if not ws:  # older single-winner draws: read the name once, then cached
            nm = cget("rn:" + did, 3600, lambda: (fs.collection("results").document(did).get().to_dict() or {}).get("name", ""))
            ws = [{"place": 1, "num": d["winner_num"], "uid": d["winner_user"], "prize": d["prize"], "name": nm}]
        out["winners"] = [{"place": w["place"], "num": w["num"], "prize": w["prize"], "name": mask_name(w.get("name", "")),
                           "mine": w["uid"] == session["uid"]} for w in ws]
        out["winner"] = out["winners"][0]["name"]
        out["mine"] = any(w["mine"] for w in out["winners"])
    return jsonify(out)

# ---------- site notices (admin -> home page) ----------
def notices_all():
    def load():
        s = fs.collection("config").document("site").get()
        return (s.to_dict() or {}).get("notices", []) if s.exists else []
    return cget("notices", 120, load)

def notices_now():
    now = time.time()
    return [n for n in notices_all() if not n.get("exp") or n["exp"] > now]

# ---------- admin ----------
def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u:
            return redirect(url_for("login"))
        if u["email"] not in ADMINS:
            abort(404)
        return f(*a, **k)
    return w

@app.context_processor
def _inj():
    u = me()
    return dict(admin_email=u["email"] if u else "", lang=session.get("lang", "bn"))

def cnt(q):
    try:
        return q.count().get()[0][0].value
    except Exception:
        return None

def alog(action, **kw):
    fs.collection("adminlog").document().set(dict(kw, action=action, admin=me()["email"], ts=time.time(),
                                                  expire_at=datetime.utcnow() + timedelta(days=90)))  # Firestore TTL policy can delete these automatically

def get_user(uid):
    s = fs.collection("users").document(uid).get()
    if not s.exists:
        abort(404)
    return snap(s)

def users_by_ids(ids):
    ids = list(set(ids))
    if not ids:
        return {}
    return {s.id: (s.to_dict() or {}) for s in fs.get_all([fs.collection("users").document(i) for i in ids])}

@app.route("/admin")
@admin_required
def admin():
    U = fs.collection("users")
    day0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    st = dict(users=cnt(U), banned=cnt(U.where("banned", "==", True)), new=cnt(U.where("created", ">=", day0)),
              claims=cnt(fs.collection("claims").where("status", "==", "pending")),
              pend=cnt(U.where("status", "==", "pending")), bal=tre_bal(), rate=get_rate())
    draws = sorted((snap(s) for s in fs.collection("draws").where("status", "==", "open").stream()), key=lambda d: d["ends"])
    top = [snap(s) for s in U.order_by("points", direction=DESC).limit(5).stream()]
    new = [snap(s) for s in U.order_by("created", direction=DESC).limit(5).stream()]
    wins = [snap(s) for s in fs.collection("results").order_by("ends", direction=DESC).limit(5).stream()]
    return render_template("a_dash.html", st=st, draws=draws, top=top, new=new, wins=wins)

@app.route("/admin/users")
@admin_required
def admin_users():
    q, flt, srt = request.args.get("q", "").strip(), request.args.get("f", ""), request.args.get("s", "points")
    col = {"points": "points", "tickets": "tix", "wins": "wins", "new": "created"}.get(srt, "points")
    U = fs.collection("users")
    if q:
        if "@" in q:
            x = by_email(q.lower())
            rows = [x] if x else []
        elif re.fullmatch(r"[\d+\s-]{6,}", en(q)):
            rows = [snap(s) for s in U.where("phone", "==", norm_phone(q)).limit(20).stream()]
        else:
            rows = [snap(s) for s in U.where("name", ">=", q).where("name", "<=", q + "\uf8ff").limit(40).stream()]
    elif flt == "banned":
        rows = [snap(s) for s in U.where("banned", "==", True).limit(50).stream()]
    else:
        PAGE = 20  # small pages keep reads low; "আরও দেখুন" loads the next ones
        qq = U.order_by(col, direction=DESC)
        after = request.args.get("after", "")
        if after:
            sn = U.document(after).get()
            if sn.exists:
                qq = qq.start_after(sn)
        rows = [snap(s) for s in qq.limit(PAGE + 1).stream()]
        more = rows[PAGE - 1]["id"] if len(rows) > PAGE else ""
        rows = rows[:PAGE]
        if col == "points" and not after:
            for i, r in enumerate(rows[:3]):
                r["rank"] = i + 1
        return render_template("a_users.html", rows=rows, q=q, f=flt, s=srt, more=more)
    return render_template("a_users.html", rows=rows, q=q, f=flt, s=srt, more="")

@app.route("/admin/user/<uid>")
@admin_required
def admin_user(uid):
    x = get_user(uid)
    uref = fs.collection("users").document(uid)
    tix = [dict(t) for t in (x.get("tk") or [])]
    for t in tix:
        d = draw_get(t["draw"]) or {}
        t["title"], t["status"] = d.get("title") or t.get("title") or "(মুছে গেছে)", d.get("status", "")
        t["won"] = d.get("winner_user") == uid and d.get("winner_num") == t["num"]
    wins = sorted((snap(s) for s in fs.collection("results").where("uid", "==", uid).limit(50).stream()), key=lambda w: -w["ends"])
    cl = {s.id: s.to_dict() for s in fs.get_all([fs.collection("claims").document(w["id"]) for w in wins])} if wins else {}
    for w in wins:
        w["claim"] = cl.get(w["id"])
    U = fs.collection("users")
    higher = cnt(U.where("points", ">", x["points"]))
    refs = cnt(U.where("ref_by", "==", uid))
    by = None
    if x.get("ref_by"):
        b = U.document(x["ref_by"]).get()
        by = b.to_dict() if b.exists else None
    hist = sorted((snap(s) for s in fs.collection("adminlog").where("uid", "==", uid).limit(30).stream()), key=lambda h: -h["ts"])
    return render_template("a_user.html", x=x, tix=tix, wins=wins, rank=(higher + 1 if higher is not None else None),
                           refs=refs, by=by, hist=hist, bal=tre_bal())

@firestore.transactional
def _pts_tx(tx, uref, amt, reason, admin_email):
    s, t = uref.get(transaction=tx), TREF.get(transaction=tx)
    if not s.exists:
        raise Fail("ইউজার পাওয়া যায়নি।")
    bal = _bal(t)
    if amt > bal:
        raise Fail("অ্যাডমিন ব্যালেন্সে এত পয়েন্ট নেই (আছে %s)।" % short(bal))
    p = s.to_dict()["points"]
    tx.update(uref, {"points": p + amt})
    tx.set(TREF, {"bal": bal - amt}, merge=True)
    tx.set(fs.collection("adminlog").document(), dict(action="points", uid=uref.id, mode="add", amount=amt, before=p,
                                                      after=p + amt, reason=reason, admin=admin_email, ts=time.time()))
    return p + amt

@app.post("/admin/user/<uid>/points")
@admin_required
def admin_points(uid):
    f = request.form
    try:
        amt = parse_amt(f.get("amount", ""))
        assert 0 < amt <= 10 ** 9
        new = _pts_tx(fs.transaction(), fs.collection("users").document(uid), amt, f.get("reason", "")[:80], me()["email"])
        flash("%s পয়েন্ট যোগ হয়েছে, এখন %s।" % (bn(amt), bn(new)))
    except (ValueError, AssertionError):
        flash("১ বা তার বেশি সঠিক সংখ্যা দিন (পয়েন্ট কমানো যায় না)।")
    except Fail as e:
        flash(str(e))
    return redirect(url_for("admin_user", uid=uid))

# ---- admin balance & point value ----
@firestore.transactional
def _tre_tx(tx, op, amt):
    bal = _bal(TREF.get(transaction=tx))
    new = bal + amt if op == "add" else bal - amt
    if new < 0:
        raise Fail("ব্যালেন্স ০-এর নিচে নামানো যায় না (আছে %s)।" % short(bal))
    tx.set(TREF, {"bal": new}, merge=True)
    return new

@app.post("/admin/treasury")
@admin_required
def admin_treasury():
    op = "sub" if request.form.get("op") == "sub" else "add"
    try:
        amt = parse_amt(request.form.get("amount", ""))
        assert 0 < amt <= 10 ** 13
        new = _tre_tx(fs.transaction(), op, amt)
        alog("treasury", reason=("+" if op == "add" else "-") + str(amt))
        flash("অ্যাডমিন ব্যালেন্স এখন %s।" % short(new))
    except (ValueError, AssertionError):
        flash("সঠিক সংখ্যা দিন (যেমন 500000, 100M, 1B)।")
    except Fail as e:
        flash(str(e))
    return redirect(url_for("admin"))

@app.post("/admin/rate")
@admin_required
def admin_rate():
    try:
        v = int(en(request.form.get("rate", "")).strip())
        assert 1 <= v <= 10 ** 6
        TREF.set({"rate": v}, merge=True)
        cdrop("rate")
        alog("rate", reason="১০০০ পয়েন্ট = %d টাকা" % v)
        flash("এখন ১০০০ পয়েন্টের মূল্য %s টাকা।" % bn(v))
    except (ValueError, AssertionError):
        flash("সঠিক সংখ্যা দিন।")
    return redirect(url_for("admin"))

# ---- approvals ----
@firestore.transactional
def _approve_tx(tx, uref):
    us, ts = uref.get(transaction=tx), TREF.get(transaction=tx)
    if not us.exists:
        raise Fail("ইউজার পাওয়া যায়নি।")
    u = us.to_dict()
    if not is_pending(u):
        raise Fail("আগেই অ্যাপ্রুভ করা হয়েছে।")
    bal = _bal(ts)
    rref = fs.collection("users").document(u["ref_by"]) if u.get("ref_by") else None
    rs = rref.get(transaction=tx) if rref else None
    pay_ref = rs is not None and rs.exists
    need = SIGNUP + (REF if pay_ref else 0)
    if bal < need:
        raise Fail("অ্যাডমিন ব্যালেন্সে বোনাসের পয়েন্ট নেই।")
    tx.update(uref, {"status": "approved", "points": u["points"] + SIGNUP, "approved_ts": time.time()})
    tx.set(TREF, {"bal": bal - need}, merge=True)
    if pay_ref:
        tx.update(rref, {"points": rs.to_dict()["points"] + REF})

@app.route("/admin/approvals")
@admin_required
def admin_approvals():
    rows = sorted((snap(s) for s in fs.collection("users").where("status", "==", "pending").limit(50).stream()), key=lambda x: x.get("created", 0))
    return render_template("a_approvals.html", rows=rows, SIGNUP=SIGNUP)

@app.post("/admin/user/<uid>/approve")
@admin_required
def admin_approve(uid):
    try:
        _approve_tx(fs.transaction(), fs.collection("users").document(uid))
        alog("approve", uid=uid, reason="+%d" % SIGNUP)
        cdrop("board:*")
        flash("অ্যাপ্রুভ হয়েছে, %s পয়েন্ট বোনাস গেছে।" % bn(SIGNUP))
    except Fail as e:
        flash(str(e))
    return redirect(url_for("admin_approvals"))

@app.post("/admin/user/<uid>/reject")
@admin_required
def admin_reject(uid):
    x = get_user(uid)
    if x["email"] in ADMINS or not is_pending(x):
        flash("এই অ্যাকাউন্ট বাতিল করা যাবে না।")
    else:
        fs.collection("users").document(uid).delete()
        alog("reject", uid=uid, email=x["email"], name=x["name"])
        flash("আবেদন বাতিল করে অ্যাকাউন্ট মুছে দেওয়া হয়েছে।")
    return redirect(url_for("admin_approvals"))

@app.post("/admin/user/<uid>/ban")
@admin_required
def admin_ban(uid):
    x = get_user(uid)
    if x["email"] in ADMINS:
        flash("অ্যাডমিনকে ব্যান করা যায় না।")
    else:
        ban, why = request.form.get("do") == "ban", request.form.get("reason", "")[:80]
        fs.collection("users").document(uid).update({"banned": ban, "ban_reason": why if ban else "", "ban_ts": time.time()})
        alog("ban" if ban else "unban", uid=uid, reason=why)
        flash("ইউজার ব্যান করা হয়েছে।" if ban else "ইউজারকে আনব্যান করা হয়েছে।")
    return redirect(url_for("admin_user", uid=uid))

@app.post("/admin/user/<uid>/edit")
@admin_required
def admin_edit(uid):
    f = request.form
    get_user(uid)
    name, phone = f.get("name", "").strip()[:60], norm_phone(f.get("phone", ""))
    if len(name) < 2 or (phone and not re.fullmatch(r"01\d{9}", phone)):
        flash("নাম বা মোবাইল নম্বর সঠিক নয়।")
    else:
        fs.collection("users").document(uid).update({"name": name, "phone": phone})
        alog("edit", uid=uid, name=name, phone=phone)
        flash("তথ্য সেভ হয়েছে।")
    return redirect(url_for("admin_user", uid=uid))

@app.post("/admin/user/<uid>/delete")
@admin_required
def admin_delete(uid):
    x = get_user(uid)
    if x["email"] in ADMINS or request.form.get("ok", "").strip() != "DELETE":
        flash("ডিলিট হয়নি। নিশ্চিত করতে DELETE লিখুন (অ্যাডমিন ডিলিট করা যায় না)।")
        return redirect(url_for("admin_user", uid=uid))
    uref = fs.collection("users").document(uid)
    uref.delete()
    changed()
    alog("delete", uid=uid, email=x["email"], name=x["name"])
    flash("ইউজার ডিলিট হয়েছে।")
    return redirect(url_for("admin_users"))

@app.route("/admin/draws")
@admin_required
def admin_draws():
    rows = [snap(s) for s in fs.collection("draws").order_by("ends", direction=DESC).limit(30).stream()]
    return render_template("a_draws.html", rows=rows)

@firestore.transactional
def _new_draw_tx(tx, dref, data, total):
    ds, ts = dref.get(transaction=tx), TREF.get(transaction=tx)
    if ds.exists:
        raise Fail("এই সময়ে আরেকটি ড্র আছে, সময় একটু বদলান।")
    bal = _bal(ts)
    if bal < total:
        raise Fail("অ্যাডমিন ব্যালেন্সে পুরস্কারের পয়েন্ট নেই (আছে %s)।" % short(bal))
    tx.set(TREF, {"bal": bal - total}, merge=True)  # prizes are reserved from the admin balance up front
    tx.set(dref, data)

@app.post("/admin/draws/new")
@admin_required
def admin_draw_new():
    f = request.form
    try:
        title, cost, cap = f["title"].strip()[:60], parse_amt(f["cost"]), int(en(f["cap"]))
        prizes = [parse_amt(f["prize"])] + [parse_amt(f[k]) for k in ("prize2", "prize3") if f.get(k, "").strip()]
        end = datetime.strptime(f["ends"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
        assert title and all(p > 0 for p in prizes) and cost > 0 and 1 <= cap <= 100000 and end.timestamp() > time.time()
        if len(prizes) == 2 and not f.get("prize2", "").strip():
            raise ValueError
    except (KeyError, ValueError, AssertionError):
        flash("সব ঘর সঠিকভাবে পূরণ করুন, শেষ সময় ভবিষ্যতের হতে হবে।")
        return redirect(url_for("admin_draws"))
    e = int(end.timestamp())
    try:
        _new_draw_tx(fs.transaction(), fs.collection("draws").document("custom-%d" % e),
                     dict(kind="custom", title=title, prize=prizes[0], prizes=prizes, reserved=sum(prizes), cost=cost, cap=cap,
                          ends=e, status="open", sold=0), sum(prizes))
    except Fail as x:
        flash(str(x))
        return redirect(url_for("admin_draws"))
    changed()
    alog("draw_new", title=title, prize=sum(prizes))
    flash("নতুন ড্র তৈরি হয়েছে, পুরস্কারের %s পয়েন্ট অ্যাডমিন ব্যালেন্স থেকে আলাদা করা হয়েছে।" % bn(sum(prizes)))
    return redirect(url_for("admin_draws"))

@app.route("/admin/draw/<did>")
@admin_required
def admin_draw(did):
    s = fs.collection("draws").document(did).get()
    if not s.exists:
        abort(404)
    d = snap(s)
    tix = [t.to_dict() for t in s.reference.collection("tickets").order_by("num").limit(200).stream()]
    um = users_by_ids([t["uid"] for t in tix])
    for t in tix:
        t["user"] = um.get(t["uid"]) or {}
    return render_template("a_draw.html", d=d, tix=tix)

@app.post("/admin/draw/<did>/finish")
@admin_required
def admin_finish(did):
    dref = fs.collection("draws").document(did)
    s_ = dref.get()
    if not s_.exists or s_.to_dict()["status"] != "open":
        flash("এই ড্র আর চালু নেই।")
        return redirect(url_for("admin_draw", did=did))
    dref.update({"ends": int(time.time()) + 15})
    changed(did)
    alog("draw_live", draw=did)
    flash("১৫ সেকেন্ড পরে লাইভ ড্র শুরু হবে।")
    return redirect(url_for("live", did=did))

@firestore.transactional
def _cancel_tx(tx, dref):
    d = dref.get(transaction=tx).to_dict()
    ts = TREF.get(transaction=tx)
    if not d or d["status"] != "open":
        return None
    # reserved prizes come back to the admin balance, ticket money goes back to the buyers
    tx.set(TREF, {"bal": max(0, _bal(ts) + d.get("reserved", 0) - d["sold"] * d["cost"])}, merge=True)
    tx.update(dref, {"status": "cancelled"})
    return d

@app.post("/admin/draw/<did>/cancel")
@admin_required
def admin_cancel(did):
    dref = fs.collection("draws").document(did)
    d = _cancel_tx(fs.transaction(), dref)
    if not d:
        flash("এই ড্র বাতিল করা যায় না।")
        return redirect(url_for("admin_draw", did=did))
    per = {}
    for t in dref.collection("tickets").stream():
        x = t.to_dict()
        per.setdefault(x["uid"], []).append(x["num"])
    for uid, nums in per.items():
        uref = fs.collection("users").document(uid)
        us = uref.get()
        if not us.exists:
            continue
        u = us.to_dict()
        upd = {"points": firestore.Increment(len(nums) * d["cost"]), "tix": firestore.Increment(-len(nums))}
        if "tk" in u:
            upd["tk"] = [t for t in u["tk"] if t.get("draw") != did]
        if "mine" in u:
            upd["mine"] = {k: v for k, v in u["mine"].items() if k != did}
        uref.update(upd)
    changed(did)
    alog("draw_cancel", draw=did, refunded=sum(len(v) for v in per.values()) * d["cost"])
    flash("ড্র বাতিল হয়েছে, সবার পয়েন্ট ফেরত দেওয়া হয়েছে।")
    return redirect(url_for("admin_draws"))

@app.route("/admin/claims")
@admin_required
def admin_claims():
    pend = [snap(s) for s in fs.collection("claims").where("status", "==", "pending").limit(50).stream()]
    sent = sorted((snap(s) for s in fs.collection("claims").where("status", "==", "sent").limit(30).stream()),
                  key=lambda c: -c.get("sent_ts", 0))
    return render_template("a_claims.html", pend=pend, sent=sent)

@app.post("/admin/claim/<rid>/sent")
@admin_required
def admin_sent(rid):
    cref = fs.collection("claims").document(rid)
    c = cref.get().to_dict() or {}
    cref.update({"status": "sent", "sent_ts": time.time()})
    if c.get("uid"):  # winner no longer needs the "waiting" card
        uref = fs.collection("users").document(c["uid"])
        us = uref.get()
        if us.exists and "wl" in us.to_dict():
            uref.update({"wl": [x for x in us.to_dict()["wl"] if x["id"] != rid]})
    alog("claim_sent", claim=rid)
    flash("পাঠানো হয়েছে হিসেবে চিহ্নিত।")
    return redirect(url_for("admin_claims"))

@app.route("/admin/notices")
@admin_required
def admin_notices():
    return render_template("a_notices.html", rows=notices_all(), now=time.time())

@app.post("/admin/notices/add")
@admin_required
def admin_notice_add():
    f = request.form
    text, link = f.get("text", "").strip()[:300], f.get("link", "").strip()[:300]
    if len(text) < 3:
        flash("নোটিশের লেখা দিন।")
        return redirect(url_for("admin_notices"))
    if link and not (link.startswith("https://") or link.startswith("http://") or link.startswith("/")):
        link = ""
    hrs = f.get("hours", type=int) or 0
    n = dict(id=secrets.token_hex(4), text=text, link=link, level="imp" if f.get("level") == "imp" else "info",
             ts=time.time(), exp=(time.time() + hrs * 3600) if hrs > 0 else 0)
    rows = [x for x in notices_all() if not x.get("exp") or x["exp"] > time.time()]
    rows = ([n] + rows)[:8]
    fs.collection("config").document("site").set({"notices": rows}, merge=True)
    cdrop("notices")
    alog("notice_add", text=text[:60])
    flash("নোটিশ হোম পেজে দেখানো শুরু হয়েছে।")
    return redirect(url_for("admin_notices"))

@app.post("/admin/notices/<nid>/del")
@admin_required
def admin_notice_del(nid):
    rows = [x for x in notices_all() if x["id"] != nid]
    fs.collection("config").document("site").set({"notices": rows}, merge=True)
    cdrop("notices")
    alog("notice_del", notice=nid)
    flash("নোটিশ সরানো হয়েছে।")
    return redirect(url_for("admin_notices"))

@app.post("/admin/logs/clear")
@admin_required
def admin_logs_clear():
    days = request.form.get("days", type=int) or 30
    old = list(fs.collection("adminlog").where("ts", "<", time.time() - max(1, days) * 86400).limit(400).stream())
    for s in old:
        s.reference.delete()
    flash("%s টি পুরোনো লগ মুছেছে।" % bn(len(old)) + (" আবার চাপুন, আরও আছে।" if len(old) == 400 else ""))
    return redirect(url_for("admin_logs"))

@app.route("/admin/logs")
@admin_required
def admin_logs():
    rows = [snap(s) for s in fs.collection("adminlog").order_by("ts", direction=DESC).limit(60).stream()]
    um = users_by_ids([r["uid"] for r in rows if r.get("uid")])
    for r in rows:
        r["uname"] = (um.get(r.get("uid")) or {}).get("name", "") if r.get("uid") else ""
    return render_template("a_logs.html", rows=rows)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
