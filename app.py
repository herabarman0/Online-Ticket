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
KL = {"daily": "প্রতিদিন", "weekly": "সাপ্তাহিক", "monthly": "মাসিক", "custom": "বিশেষ"}
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

def new_user(email, name, pw_hash, ref_code="", phone=""):
    ref = None
    if ref_code:
        for s in fs.collection("users").where("code", "==", ref_code).limit(1).stream():
            ref = s
    r = fs.collection("users").document()
    r.set({"email": email, "name": name, "pw": pw_hash, "points": SIGNUP, "code": secrets.token_hex(3).upper(),
           "ref_by": ref.id if ref else None, "phone": phone, "created": time.time(), "banned": False, "last_bonus": "", "wins": 0, "tix": 0, "streak": 0, "ad_day": "", "ad_n": 0})
    if ref:
        ref.reference.update({"points": firestore.Increment(REF)})
    return r.id

# ---------- draws (admin-created only; no automatic draws) ----------
SPIN = 60  # seconds of live spin, same moment on every phone

@firestore.transactional
def _finish_tx(tx, dref):
    # at the draw time: pick the winner (stored, hidden until the spin ends)
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "open" or d["ends"] > time.time():
        return
    if d["sold"] > 0:
        num = secrets.randbelow(d["sold"]) + 1
        t = dref.collection("tickets").document(str(num)).get(transaction=tx).to_dict()
        tx.update(dref, {"status": "drawing", "winner_num": num, "winner_user": t["uid"]})
    else:
        tx.update(dref, {"status": "done"})

@firestore.transactional
def _award_tx(tx, dref):
    # after the spin: credit the prize and publish the result
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "drawing" or time.time() < d["ends"] + SPIN:
        return
    uref = fs.collection("users").document(d["winner_user"])
    us = uref.get(transaction=tx)
    if not us.exists:
        tx.update(dref, {"status": "done"})
        return
    u = us.to_dict()
    tx.update(uref, {"points": u["points"] + d["prize"], "wins": u.get("wins", 0) + 1, "unclaimed": u.get("unclaimed", 0) + 1})
    tx.update(dref, {"status": "done"})
    tx.set(fs.collection("results").document(dref.id), {"uid": d["winner_user"], "name": u["name"], "title": d["title"],
           "prize": d["prize"], "winner_num": d["winner_num"], "ends": d["ends"]})

_last = 0.0
def settle(force=False):
    global _last
    now = time.time()
    if not force and now - _last < 20:
        return
    _last = now
    for s in fs.collection("draws").where("status", "in", ["open", "drawing"]).stream():
        d = s.to_dict()
        if d.get("kind") != "custom":
            continue  # old automatic draws are ignored; admin can cancel them
        if d["status"] == "open" and d["ends"] <= now:
            _finish_tx(fs.transaction(), s.reference)
        elif d["status"] == "drawing" and now >= d["ends"] + SPIN:
            _award_tx(fs.transaction(), s.reference)

def open_draws(uid):
    now, out = time.time(), []
    for s in fs.collection("draws").where("status", "==", "open").stream():
        d = snap(s)
        if d["ends"] > now and d.get("kind") == "custom":
            out.append(d)
    out.sort(key=lambda d: d["ends"])
    refs = [fs.collection("draws").document(d["id"]).collection("players").document(uid) for d in out]
    m = {p.reference.parent.parent.id: (p.to_dict() or {}).get("n", 0) for p in fs.get_all(refs)} if refs else {}
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
        t["status"], t["winner_num"], t["title"] = x.get("status", ""), x.get("winner_num"), x.get("title", "")
    return docs

def my_wins(uid):
    # prizes this user won whose details form is not finished yet
    rs = [snap(x) for x in fs.collection("results").where("uid", "==", uid).limit(20).stream()]
    if not rs:
        return []
    cl = {x.id: (x.to_dict() or {}) for x in fs.get_all([fs.collection("claims").document(r["id"]) for r in rs]) if x.exists}
    out = []
    for r in sorted(rs, key=lambda r: -r["ends"]):
        c = cl.get(r["id"])
        if c is None:
            r["state"] = "need"
        elif c.get("status") == "pending":
            r["state"] = "wait"
        else:
            continue
        out.append(r)
    return out

def last_result():
    for x in fs.collection("results").order_by("ends", direction=DESC).limit(1).stream():
        r = snap(x)
        r["mname"] = mask_name(r.get("name", ""))
        return r

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
        elif not re.fullmatch(r"01\d{9}", norm_phone(f.get("phone", ""))):
            flash("সঠিক মোবাইল নম্বর দিন (যেমন ০১৭XXXXXXXX)।")
        elif email in ADMINS:
            flash("এই ইমেইলে অ্যাকাউন্ট খুলতে হবে না, সরাসরি লগইন করুন।")
        elif by_email(email):
            flash("এই ইমেইলে আগেই অ্যাকাউন্ট আছে। লগইন করুন।")
        else:
            session["uid"] = new_user(email, name, generate_password_hash(pw), session.pop("ref", "") or f.get("ref", "").strip().upper(), norm_phone(f.get("phone", "")))
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup", gcid=GCID, ref=session.get("ref", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw = request.form.get("email", "").strip().lower(), request.form.get("pw", "")
        if email in ADMINS:
            # admin signs in with the ADMIN_PASSWORD set in Render's Environment
            if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                u = by_email(email)
                session["uid"] = u["id"] if u else new_user(email, "Admin", None)
                return redirect(url_for("admin"))
            time.sleep(1)
        else:
            u = by_email(email)
            if u and u.get("pw") and check_password_hash(u["pw"], pw):
                if u.get("banned"):
                    flash("আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে। অ্যাডমিনের সাথে যোগাযোগ করুন।")
                    return render_template("auth.html", mode="login", gcid=GCID, ref="")
                session["uid"] = u["id"]
                fs.collection("users").document(u["id"]).update({"last_login": time.time()})
                return redirect(url_for("home"))
        flash("ইমেইল বা পাসওয়ার্ড ঠিক নয়।")
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
    ds = open_draws(u["id"])
    d = ds[0] if ds else None
    live_d = None
    for x in fs.collection("draws").where("status", "==", "drawing").limit(3).stream():
        if (x.to_dict() or {}).get("kind") == "custom":
            live_d = snap(x)
    if not live_d and d and d["ends"] - time.time() < 600:
        live_d = d
    last = None if (d or live_d) else last_result()
    ad_left = AD_MAX - (u.get("ad_n", 0) if u.get("ad_day") == today().isoformat() else 0)
    return render_template("home.html", u=u, d=d, tcount=u.get("tix", 0), day=streak_info(u), ad_left=ad_left,
                           live_d=live_d, last=last, wins=my_wins(u["id"]), now=time.time())

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
    cur = next((r for r in rows if r["id"] == sel), rows[0] if rows else None)
    return render_template("ticket.html", u=u, rows=rows, cur=cur, mytix=my_tickets(u["id"]),
                           last=(None if rows else last_result()), wins=my_wins(u["id"]), now=time.time())

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

# ---------- live draw ----------
def mask_name(n):
    return " ".join((w[:2] + "***") for w in (n or "").split())

@app.route("/live/<did>")
@login_required
def live(did):
    s_ = fs.collection("draws").document(did).get()
    if not s_.exists:
        abort(404)
    return render_template("live.html", u=me(), d=snap(s_))

@app.route("/live/<did>/state")
@login_required
def live_state(did):
    dref = fs.collection("draws").document(did)
    s_ = dref.get()
    if not s_.exists:
        abort(404)
    d, now = s_.to_dict(), time.time()
    if d["status"] == "open" and d["ends"] <= now:
        _finish_tx(fs.transaction(), dref)
        d = dref.get().to_dict()
    if d["status"] == "drawing" and now >= d["ends"] + SPIN:
        _award_tx(fs.transaction(), dref)
        d = dref.get().to_dict()
    out = dict(now=now, ends=d["ends"], status=d["status"], sold=d["sold"], title=d["title"], prize=d["prize"], spin=SPIN)
    if d["status"] in ("drawing", "done") and d.get("winner_num"):
        out["winner_num"] = d["winner_num"]
    if d["status"] == "done" and d.get("winner_user"):
        r = fs.collection("results").document(did).get()
        out["winner"] = mask_name((r.to_dict() or {}).get("name", ""))
        out["mine"] = d["winner_user"] == me()["id"]
    return jsonify(out)

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
    return dict(admin_email=u["email"] if u else "")

def cnt(q):
    try:
        return q.count().get()[0][0].value
    except Exception:
        return None

def alog(action, **kw):
    fs.collection("adminlog").document().set(dict(kw, action=action, admin=me()["email"], ts=time.time()))

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
              claims=cnt(fs.collection("claims").where("status", "==", "pending")))
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
        rows = [snap(s) for s in U.order_by(col, direction=DESC).limit(50).stream()]
        if col == "points":
            for i, r in enumerate(rows[:3]):
                r["rank"] = i + 1
    return render_template("a_users.html", rows=rows, q=q, f=flt, s=srt)

@app.route("/admin/user/<uid>")
@admin_required
def admin_user(uid):
    x = get_user(uid)
    uref = fs.collection("users").document(uid)
    tix = [s.to_dict() for s in uref.collection("tickets").order_by("ts", direction=DESC).limit(100).stream()]
    ids = list({t["draw"] for t in tix})
    info = {s.id: (s.to_dict() or {}) for s in fs.get_all([fs.collection("draws").document(i) for i in ids])} if ids else {}
    for t in tix:
        d = info.get(t["draw"], {})
        t["title"], t["status"] = d.get("title", "(মুছে গেছে)"), d.get("status", "")
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
                           refs=refs, by=by, hist=hist)

@firestore.transactional
def _pts_tx(tx, uref, mode, amt, reason, admin_email):
    s = uref.get(transaction=tx)
    if not s.exists:
        raise Fail("ইউজার পাওয়া যায়নি।")
    p = s.to_dict()["points"]
    new = max(0, p + amt if mode == "add" else amt)
    tx.update(uref, {"points": new})
    tx.set(fs.collection("adminlog").document(), dict(action="points", uid=uref.id, mode=mode, amount=amt, before=p,
                                                      after=new, reason=reason, admin=admin_email, ts=time.time()))
    return new

@app.post("/admin/user/<uid>/points")
@admin_required
def admin_points(uid):
    f = request.form
    try:
        amt, mode = int(en(f.get("amount", "")).strip()), f.get("mode", "add")
        assert mode in ("add", "set") and abs(amt) <= 10 ** 7
        new = _pts_tx(fs.transaction(), fs.collection("users").document(uid), mode, amt, f.get("reason", "")[:80], me()["email"])
        flash("পয়েন্ট এখন %s।" % bn(new))
    except (ValueError, AssertionError):
        flash("সঠিক সংখ্যা দিন।")
    except Fail as e:
        flash(str(e))
    return redirect(url_for("admin_user", uid=uid))

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
    for s in uref.collection("tickets").limit(500).stream():
        s.reference.delete()
    uref.delete()
    alog("delete", uid=uid, email=x["email"], name=x["name"])
    flash("ইউজার ডিলিট হয়েছে।")
    return redirect(url_for("admin_users"))

@app.route("/admin/draws")
@admin_required
def admin_draws():
    rows = [snap(s) for s in fs.collection("draws").order_by("ends", direction=DESC).limit(30).stream()]
    return render_template("a_draws.html", rows=rows)

@app.post("/admin/draws/new")
@admin_required
def admin_draw_new():
    f = request.form
    try:
        title, prize, cost, cap = f["title"].strip()[:60], int(en(f["prize"])), int(en(f["cost"])), int(en(f["cap"]))
        end = datetime.strptime(f["ends"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
        assert title and prize > 0 and cost > 0 and 1 <= cap <= 100000 and end.timestamp() > time.time()
    except (KeyError, ValueError, AssertionError):
        flash("সব ঘর সঠিকভাবে পূরণ করুন, শেষ সময় ভবিষ্যতের হতে হবে।")
        return redirect(url_for("admin_draws"))
    e = int(end.timestamp())
    fs.collection("draws").document("custom-%d" % e).set(dict(kind="custom", title=title, prize=prize, cost=cost, cap=cap,
                                                              ends=e, status="open", sold=0))
    alog("draw_new", title=title, prize=prize)
    flash("নতুন ড্র তৈরি হয়েছে।")
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
    alog("draw_live", draw=did)
    flash("১৫ সেকেন্ড পরে লাইভ ড্র শুরু হবে।")
    return redirect(url_for("live", did=did))

@firestore.transactional
def _cancel_tx(tx, dref):
    d = dref.get(transaction=tx).to_dict()
    if not d or d["status"] != "open":
        return None
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
        if uref.get().exists:
            uref.update({"points": firestore.Increment(len(nums) * d["cost"]), "tix": firestore.Increment(-len(nums))})
            for n in nums:
                uref.collection("tickets").document("%s_%d" % (did, n)).delete()
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
    fs.collection("claims").document(rid).update({"status": "sent", "sent_ts": time.time()})
    alog("claim_sent", claim=rid)
    flash("পাঠানো হয়েছে হিসেবে চিহ্নিত।")
    return redirect(url_for("admin_claims"))

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
