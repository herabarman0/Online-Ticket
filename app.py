import os, re, hmac, secrets, time, json, urllib.request, urllib.parse
from urllib.parse import urlparse
from calendar import monthrange
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists
from flask import (Flask, g, render_template, request, redirect, url_for, session, flash,
                   jsonify, abort, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, template_folder=".", static_folder=None)  # flat layout: all files in repo root
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

@app.route("/static/<path:filename>", endpoint="static")
def static_files(filename):
    if filename != "style.css":
        abort(404)
    return send_from_directory(app.root_path, "style.css")

GCID = os.environ.get("GOOGLE_CLIENT_ID", "")
TZ = ZoneInfo("Asia/Dhaka")
SIGNUP, REF, MAXT, AD_REWARD, AD_MAX = 100, 50, 5, 10, 5
KL = {"daily": "প্রতিদিন", "weekly": "সাপ্তাহিক", "monthly": "মাসিক"}
KO = {"daily": 0, "weekly": 1, "monthly": 2}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMINS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
app.jinja_env.globals.update(KL=KL, MAXT=MAXT, AD_REWARD=AD_REWARD)

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
@app.template_filter("pad4")
def pad4(v):
    return bn(str(v).zfill(4))
@app.template_filter("when")
def when(ts):
    return bn(datetime.fromtimestamp(ts, TZ).strftime("%d/%m, %I:%M %p"))

def snap(s):
    x = s.to_dict() or {}
    x["id"] = s.id
    return x

def me():
    if "_me" in g:
        return g._me
    u, uid = None, session.get("uid")
    if uid:
        s = fs.collection("users").document(uid).get()
        u = snap(s) if s.exists else None
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

def new_user(email, name, pw_hash, ref_code=""):
    ref = None
    if ref_code:
        for s in fs.collection("users").where("code", "==", ref_code).limit(1).stream():
            ref = s
    r = fs.collection("users").document()
    r.set({"email": email, "name": name, "pw": pw_hash, "points": SIGNUP, "code": secrets.token_hex(3).upper(),
           "ref_by": ref.id if ref else None, "last_bonus": "", "wins": 0, "tix": 0, "streak": 0, "ad_day": "", "ad_n": 0})
    if ref:
        ref.reference.update({"points": firestore.Increment(REF)})
    return r.id

# ---------- draws ----------
@firestore.transactional
def _finish_tx(tx, dref):
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "open":
        return
    if d["sold"] > 0:
        num = secrets.randbelow(d["sold"]) + 1
        t = dref.collection("tickets").document(str(num)).get(transaction=tx).to_dict()
        uref = fs.collection("users").document(t["uid"])
        u = uref.get(transaction=tx).to_dict()
        tx.update(uref, {"points": u["points"] + d["prize"], "wins": u.get("wins", 0) + 1, "unclaimed": u.get("unclaimed", 0) + 1})
        tx.update(dref, {"status": "done", "winner_num": num, "winner_user": t["uid"]})
        tx.set(fs.collection("results").document(dref.id), {"uid": t["uid"], "name": u["name"], "title": d["title"],
               "prize": d["prize"], "winner_num": num, "ends": d["ends"]})
    else:
        tx.update(dref, {"status": "done"})

def ensure(ids, kind, title, prize, cost, cap, end_dt):
    e = int(end_dt.timestamp())
    did = "%s-%d" % (kind, e)
    if did in ids:
        return
    try:
        fs.collection("draws").document(did).create({"kind": kind, "title": title, "prize": prize, "cost": cost,
                                                    "cap": cap, "ends": e, "status": "open", "sold": 0})
    except AlreadyExists:
        pass

_last = 0.0
def settle(force=False):
    global _last
    now = time.time()
    if not force and now - _last < 20:
        return
    _last = now
    ids = set()
    for s in fs.collection("draws").where("status", "==", "open").stream():
        if s.to_dict()["ends"] <= now:
            _finish_tx(fs.transaction(), s.reference)
        else:
            ids.add(s.id)
    n = datetime.now(TZ)
    at = lambda dt, h: dt.replace(hour=h, minute=0, second=0, microsecond=0)
    day = at(n, 22)
    if day <= n:
        day += timedelta(days=1)
    ensure(ids, "daily", "প্রতিদিনের ড্র", 1000, 10, 500, day)
    fri = at(n, 19) + timedelta(days=(4 - n.weekday()) % 7)
    if fri <= n:
        fri += timedelta(days=7)
    ensure(ids, "weekly", "সাপ্তাহিক মেগা ড্র", 5000, 25, 1000, fri)
    mo = at(n.replace(day=monthrange(n.year, n.month)[1]), 20)
    if mo <= n:
        nx = (n.replace(day=1) + timedelta(days=32)).replace(day=1)
        mo = at(nx.replace(day=monthrange(nx.year, nx.month)[1]), 20)
    ensure(ids, "monthly", "মাসিক গ্র্যান্ড ড্র", 20000, 100, 5000, mo)

def open_draws(uid):
    for attempt in (0, 1):
        now, out = time.time(), []
        for s in fs.collection("draws").where("status", "==", "open").stream():
            d = snap(s)
            if d["ends"] > now:
                out.append(d)
        if out:
            break
        settle(True)
    out.sort(key=lambda d: KO.get(d["kind"], 9))
    refs = [fs.collection("draws").document(d["id"]).collection("players").document(uid) for d in out]
    m = {p.reference.parent.parent.id: (p.to_dict() or {}).get("n", 0) for p in fs.get_all(refs)}
    for d in out:
        d["mine"] = m.get(d["id"], 0)
    return out

def my_tickets(uid):
    docs = [s.to_dict() for s in fs.collection("users").document(uid).collection("tickets")
            .order_by("ts", direction=DESC).limit(12).stream()]
    ids = list({t["draw"] for t in docs})
    info = {s.id: (s.to_dict() or {}) for s in fs.get_all([fs.collection("draws").document(i) for i in ids])} if ids else {}
    for t in docs:
        x = info.get(t["draw"], {})
        t["status"], t["winner_num"] = x.get("status", ""), x.get("winner_num")
    return docs

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
        elif by_email(email):
            flash("এই ইমেইলে আগেই অ্যাকাউন্ট আছে। লগইন করুন।")
        else:
            session["uid"] = new_user(email, name, generate_password_hash(pw), session.pop("ref", "") or f.get("ref", "").strip().upper())
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup", gcid=GCID, ref=session.get("ref", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = by_email(request.form.get("email", "").strip().lower())
        if u and u.get("pw") and check_password_hash(u["pw"], request.form.get("pw", "")):
            session["uid"] = u["id"]
            return redirect(url_for("admin" if u["email"] in ADMINS else "home"))
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
    d = open_draws(u["id"])[0]
    ad_left = AD_MAX - (u.get("ad_n", 0) if u.get("ad_day") == today().isoformat() else 0)
    return render_template("home.html", u=u, d=d, tcount=u.get("tix", 0), day=streak_info(u), ad_left=ad_left)

@firestore.transactional
def _bonus_tx(tx, ref_, t, y):
    u = ref_.get(transaction=tx).to_dict()
    if u.get("last_bonus") == t:
        return 0
    cur = u.get("streak", 0) if u.get("last_bonus") == y else 0
    reward = 30 + 10 * (cur % 7)
    tx.update(ref_, {"points": u["points"] + reward, "last_bonus": t, "streak": cur + 1})
    return reward

@app.post("/bonus")
@login_required
def bonus():
    t = today()
    r = _bonus_tx(fs.transaction(), fs.collection("users").document(me()["id"]), t.isoformat(), (t - timedelta(days=1)).isoformat())
    flash("%s পয়েন্ট বোনাস পেয়েছেন।" % bn(r) if r else "আজকের বোনাস নেওয়া হয়ে গেছে।")
    return redirect(url_for("home"))

@firestore.transactional
def _ad_tx(tx, ref_, t):
    u = ref_.get(transaction=tx).to_dict()
    n = u.get("ad_n", 0) if u.get("ad_day") == t else 0
    if n >= AD_MAX:
        return None
    tx.update(ref_, {"points": u["points"] + AD_REWARD, "ad_day": t, "ad_n": n + 1})
    return u["points"] + AD_REWARD, AD_MAX - n - 1

@app.post("/ad")
@login_required
def ad():
    # TODO: replace with a server-side reward callback from your ad network (AdMob SSV / Unity) before going live
    r = _ad_tx(fs.transaction(), fs.collection("users").document(me()["id"]), today().isoformat())
    if not r:
        return jsonify(ok=False, msg="আজকের সীমা শেষ"), 429
    return jsonify(ok=True, points=r[0], left=r[1])

@app.route("/ticket")
@login_required
def ticket():
    settle()
    u = me()
    rows = open_draws(u["id"])
    sel = request.args.get("d")
    cur = next((r for r in rows if r["id"] == sel), rows[0])
    return render_template("ticket.html", u=u, rows=rows, cur=cur, mytix=my_tickets(u["id"]))

class Fail(Exception):
    pass

@firestore.transactional
def _buy_tx(tx, uref, dref, pref, q, now):
    u = uref.get(transaction=tx).to_dict()
    ds = dref.get(transaction=tx)
    p = pref.get(transaction=tx)
    if not ds.exists:
        raise Fail("এই ড্র আর চালু নেই।")
    d = ds.to_dict()
    mine = (p.to_dict() or {}).get("n", 0) if p.exists else 0
    if d["status"] != "open" or d["ends"] <= now:
        raise Fail("এই ড্র আর চালু নেই।")
    if mine + q > MAXT or d["sold"] + q > d["cap"]:
        raise Fail("টিকিটের সীমা পেরিয়ে যাবে।")
    cost = q * d["cost"]
    if u["points"] < cost:
        raise Fail("পয়েন্ট কম আছে। বোনাস নিন বা রেফার করুন।")
    for i in range(q):
        num = d["sold"] + 1 + i
        tx.set(dref.collection("tickets").document(str(num)), {"uid": uref.id, "num": num})
        tx.set(uref.collection("tickets").document("%s_%d" % (dref.id, num)),
               {"draw": dref.id, "kind": d["kind"], "num": num, "ts": time.time()})
    tx.update(dref, {"sold": d["sold"] + q})
    tx.set(pref, {"n": mine + q})
    tx.update(uref, {"points": u["points"] - cost, "tix": u.get("tix", 0) + q})

@app.post("/buy/<did>")
@login_required
def buy(did):
    q = max(1, min(MAXT, request.form.get("q", 1, type=int)))
    uref = fs.collection("users").document(me()["id"])
    dref = fs.collection("draws").document(did)
    try:
        _buy_tx(fs.transaction(), uref, dref, dref.collection("players").document(uref.id), q, time.time())
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
    rows = [snap(s) for s in fs.collection("users").order_by(col, direction=DESC).limit(25).stream()]
    return render_template("board.html", u=me(), rows=rows, by=by)

@app.route("/winners")
@login_required
def winners():
    u = me()
    rows = [snap(s) for s in fs.collection("results").order_by("ends", direction=DESC).limit(30).stream()]
    for w in rows:
        w["claim"] = None
        if w["uid"] == u["id"]:
            c = fs.collection("claims").document(w["id"]).get()
            w["claim"] = (c.to_dict() or {}).get("status", "pending") if c.exists else None
    return render_template("winners.html", u=u, rows=rows)

@app.route("/profile")
@login_required
def profile():
    u = me()
    n_ref = len(list(fs.collection("users").where("ref_by", "==", u["id"]).limit(100).stream()))
    return render_template("profile.html", u=u, n_ref=n_ref, REF=REF, SIGNUP=SIGNUP, link=url_for("ref", code=u["code"], _external=True), is_admin=u["email"] in ADMINS)


# ---------- prize claim (winner details) ----------
PHONE = re.compile(r"^(?:\+?88)?01\d{9}$")
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
def _claim_tx(tx, rref, cref, uref, data, uid):
    r = rref.get(transaction=tx)
    if not r.exists or r.to_dict()["uid"] != uid:
        raise Fail("এই পুরস্কার আপনার নয়।")
    if cref.get(transaction=tx).exists:
        raise Fail("তথ্য আগেই জমা দিয়েছেন।")
    u, rd = uref.get(transaction=tx).to_dict(), r.to_dict()
    tx.set(cref, dict(data, uid=uid, email=u["email"], title=rd["title"], prize=rd["prize"],
                      winner_num=rd["winner_num"], status="pending", ts=time.time()))
    tx.update(uref, {"unclaimed": max(0, u.get("unclaimed", 0) - 1)})

@app.route("/claim/<rid>", methods=["GET", "POST"])
@login_required
def claim(rid):
    u = me()
    r = fs.collection("results").document(rid).get()
    if not r.exists or r.to_dict()["uid"] != u["id"]:
        abort(404)
    if request.method == "POST":
        f = request.form
        name, addr = f.get("name", "").strip()[:60], f.get("address", "").strip()[:200]
        phone, fb = re.sub(r"[\s-]", "", en(f.get("phone", ""))), fb_link(f.get("fb", ""))
        try:
            age = int(en(f.get("age", "")).strip())
        except ValueError:
            age = None
        err = None
        if len(name) < 3 or len(addr) < 10:
            err = "পুরো নাম ও পুরো ঠিকানা লিখুন।"
        elif not PHONE.match(phone):
            err = "সঠিক মোবাইল নম্বর দিন (যেমন ০১৭XXXXXXXX)।"
        elif age is None or age < 18 or age > 100:
            err = "পুরস্কার নিতে বয়স কমপক্ষে ১৮ হতে হবে।"
        elif not fb:
            err = "সঠিক ফেসবুক প্রোফাইল লিংক দিন।"
        elif not f.get("ok"):
            err = "তথ্য ব্যবহারের সম্মতি দিন।"
        if err:
            flash(err)
        else:
            try:
                _claim_tx(fs.transaction(), fs.collection("results").document(rid), fs.collection("claims").document(rid),
                          fs.collection("users").document(u["id"]), dict(name=name, phone=phone, address=addr, age=age, fb=fb), u["id"])
                flash("তথ্য জমা হয়েছে। অ্যাডমিন শীঘ্রই যোগাযোগ করবেন।")
                return redirect(url_for("winners"))
            except Fail as e:
                flash(str(e))
    return render_template("claim.html", u=u, r=r.to_dict(), f=request.form)

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

@app.route("/admin")
@admin_required
def admin():
    q = request.args.get("q", "").strip().lower()
    if q:
        x = by_email(q)
        users = [x] if x else []
    else:
        users = [snap(s) for s in fs.collection("users").order_by("points", direction=DESC).limit(30).stream()]
    claims = [snap(s) for s in fs.collection("claims").where("status", "==", "pending").limit(50).stream()]
    return render_template("admin.html", u=me(), users=users, claims=claims, q=q)

@firestore.transactional
def _adj_tx(tx, uref, delta, lref, log):
    s = uref.get(transaction=tx)
    if not s.exists:
        raise Fail("ইউজার পাওয়া যায়নি।")
    p = s.to_dict()["points"]
    new = max(0, p + delta)
    tx.update(uref, {"points": new})
    tx.set(lref, dict(log, before=p, after=new))
    return new

@app.post("/admin/points")
@admin_required
def admin_points():
    f, a = request.form, me()
    try:
        delta = int(en(f.get("delta", "")).strip())
        assert 0 < abs(delta) <= 1000000
        uid = f.get("uid", "")
        new = _adj_tx(fs.transaction(), fs.collection("users").document(uid), delta, fs.collection("adjustments").document(),
                      dict(uid=uid, delta=delta, reason=f.get("reason", "")[:80], admin=a["email"], ts=time.time()))
        flash("পয়েন্ট বদলেছে, এখন %s।" % bn(new))
    except (ValueError, AssertionError):
        flash("সঠিক সংখ্যা দিন (যেমন 50 বা -50)।")
    except Fail as e:
        flash(str(e))
    return redirect(url_for("admin", q=f.get("q", "")))

@app.post("/admin/sent/<rid>")
@admin_required
def admin_sent(rid):
    fs.collection("claims").document(rid).update({"status": "sent", "sent_ts": time.time()})
    flash("পাঠানো হয়েছে হিসেবে চিহ্নিত।")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
