#!/usr/bin/env python3
"""
Trip Planner server (general purpose, multi-user, multi-trip).

Auth
  POST /api/register  {email,password,name}      -> {ok, token, user}
  POST /api/login     {email,password}           -> {ok, token, user}
  POST /api/logout                               -> {ok}
  GET  /api/me                                   -> {ok, user}

Trips
  GET  /api/trips                                -> trips I own or joined (+ my role)
  POST /api/trips      {title,start_date,end_date,days}  -> {ok, trip_id}
  POST /api/trips/rename {trip_id,title,...}
  POST /api/trips/delete {trip_id}                (owner only)
  POST /api/trips/invite {trip_id,email,role}     role=editor|viewer (owner only)
  POST /api/trips/share  {trip_id,role}           -> {ok, share_token}  read-only link
  GET  /api/join?t=<share_token>                  -> joins (viewer) and returns trip_id

Data (per trip; role checked)
  GET  /api/bootstrap?trip=X
  GET  /api/state?trip=X
  POST /api/set                {trip,item,status}
  POST /api/item               {trip,...}
  POST /api/item/delete        {trip,id}
  POST /api/item/move          {trip,id,dir}
  POST /api/photo/analyze      {image}
  POST /api/photo/commit       {trip,image,day,time,title,sub,kind,place_id}
  POST /api/photo/attach       {trip,image,item_id}
  POST /api/photo/delete       {trip,photo_id}
  POST /api/poi                {trip,...}
  GET  /api/photo/<file>

Auth is a bearer token in the Authorization header, or ?token= for GETs.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA = os.environ.get("TRIPER_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB = os.path.join(DATA, "trips.db")
PHOTOS = os.path.join(DATA, "photos")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901

VISION_URL = os.environ.get("VISION_URL", "http://192.168.1.202:8103/v1/chat/completions")
VISION_KEY = os.environ.get("VISION_KEY", "076f99fbd807bc1adb380d73d8cc4974d14ae4935cc1a1c8")
VISION_MODEL = os.environ.get("VISION_MODEL", "vision-model-default")

STATUSES = ["", "done", "partial", "skip"]
ROLES = {"owner": 3, "editor": 2, "viewer": 1}
ROLE_LABEL = {3: "owner", 2: "editor", 1: "viewer"}

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def schema_sql():
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        return f.read()


SCHEMA = schema_sql()


def db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ------------------------------------------------------------------ auth utils

def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return h.hex(), salt


def new_token():
    return secrets.token_urlsafe(32)


def user_public(row):
    return {"id": row["id"], "email": row["email"], "name": row["name"] or row["email"]}


def role_of(conn, trip, user_id):
    """Return numeric role for user on trip (0 = none)."""
    if not trip:
        return 0
    t = conn.execute("SELECT owner FROM trips WHERE id=?", (trip,)).fetchone()
    if t is None:
        return 0
    if user_id and t["owner"] == user_id:
        return 3
    if user_id:
        m = conn.execute("SELECT role FROM members WHERE trip=? AND user=?",
                         (trip, user_id)).fetchone()
        if m:
            return ROLES.get(m["role"], 0)
    return 0


def require(conn, trip, user_id, need):
    r = role_of(conn, trip, user_id)
    if r < need:
        return None
    return r


def bump(conn, trip):
    conn.execute("UPDATE trips SET rev=rev+1, updated=? WHERE id=?", (time.time(), trip))
    conn.commit()
    row = conn.execute("SELECT rev FROM trips WHERE id=?", (trip,)).fetchone()
    return row["rev"] if row else 0


# ------------------------------------------------------------------ vision

def analyze_photo(raw, mime, hint=""):
    b64 = base64.b64encode(raw).decode()
    prompt = (
        "You are helping build a travel itinerary from a photo (menu, receipt, shop sign, "
        "station board, ticket). Reply with STRICT JSON only, no prose:\n"
        'Schema: {"is_food":bool,"shop_name":"","what":"","location_guess":"","time_guess":"",'
        '"kind":"","title":"","sub":""}\n'
        "kind: pick the single best of food, sight, shop, move, run, rest, gear, warn, star, "
        "event, stay, other. title: short line for an itinerary (max 30 chars). "
        "sub: one or two lines with price/hours/notes. If you cannot tell the location, "
        'leave location_guess empty. User hint: ' + (hint or "(none)")
    )
    body = json.dumps({
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
        "temperature": 0.2,
        "max_tokens": 600,
    }).encode()
    req = urllib.request.Request(VISION_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + VISION_KEY,
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    txt = data["choices"][0]["message"]["content"].strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        parsed = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        parsed = json.loads(m.group(0)) if m else {}
    return parsed, txt


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "TripPlanner/2.0"
    protocol_version = "HTTP/1.1"

    # ---------- plumbing
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _token(self, qs=None):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if qs:
            t = qs.get("token")
            if t:
                return t[0]
        return None

    def _me(self, conn, qs=None):
        tok = self._token(qs)
        if not tok:
            return None
        row = conn.execute("SELECT user,expires FROM sessions WHERE token=?", (tok,)).fetchone()
        if row is None or row["expires"] < time.time():
            return None
        return conn.execute("SELECT * FROM users WHERE id=?", (row["user"],)).fetchone()

    # ---------- GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._static("app.html")
        if path in ("/login", "/login.html"):
            return self._static("login.html")

        conn = db()
        try:
            if path == "/api/me":
                me = self._me(conn, qs)
                if me is None:
                    return self._json(401, {"ok": False, "error": "auth"})
                return self._json(200, {"ok": True, "user": user_public(me)})

            if path == "/api/trips":
                me = self._me(conn, qs)
                if me is None:
                    return self._json(401, {"ok": False, "error": "auth"})
                rows = conn.execute(
                    "SELECT t.*, CASE WHEN t.owner=? THEN 'owner' ELSE COALESCE(m.role,'') END AS my_role "
                    "FROM trips t LEFT JOIN members m ON m.trip=t.id AND m.user=? "
                    "WHERE t.owner=? OR m.user=? ORDER BY t.updated DESC",
                    (me["id"], me["id"], me["id"], me["id"])).fetchall()
                trips = []
                for r in rows:
                    n_items = conn.execute("SELECT COUNT(*) c FROM items WHERE trip=?", (r["id"],)).fetchone()["c"]
                    trips.append({"id": r["id"], "title": r["title"], "start_date": r["start_date"],
                                  "end_date": r["end_date"], "days": r["days"], "role": r["my_role"],
                                  "items": n_items, "rev": r["rev"],
                                  "share_token": r["share_token"] if r["my_role"] == "owner" else None,
                                  "updated": r["updated"]})
                return self._json(200, {"ok": True, "trips": trips})

            if path == "/api/trips/members":
                trip = (qs.get("trip") or [""])[0]
                me = self._me(conn, qs)
                if me is None or role_of(conn, trip, me["id"]) < 1:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                rows = conn.execute(
                    "SELECT m.user, m.role, m.created, u.email, u.name FROM members m "
                    "JOIN users u ON u.id=m.user WHERE m.trip=? ORDER BY m.created", (trip,)).fetchall()
                owner = conn.execute("SELECT u.email,u.name FROM trips t JOIN users u ON u.id=t.owner "
                                     "WHERE t.id=?", (trip,)).fetchone()
                inv = conn.execute("SELECT email,role FROM invites WHERE trip=?", (trip,)).fetchall()
                return self._json(200, {"ok": True,
                                        "owner": dict(owner) if owner else None,
                                        "members": [dict(r) for r in rows],
                                        "invites": [dict(r) for r in inv]})

            if path == "/api/join":
                tok = (qs.get("t") or [""])[0]
                t = conn.execute("SELECT * FROM trips WHERE share_token=?", (tok,)).fetchone()
                if t is None:
                    return self._json(404, {"ok": False, "error": "bad link"})
                me = self._me(conn, qs)
                if me is not None and t["owner"] != me["id"]:
                    conn.execute("INSERT OR IGNORE INTO members(trip,user,role,created) VALUES(?,?,?,?)",
                                 (t["id"], me["id"], t["share_role"] or "viewer", time.time()))
                    conn.commit()
                return self._json(200, {"ok": True, "trip_id": t["id"], "title": t["title"],
                                        "role": t["share_role"] or "viewer",
                                        "logged_in": me is not None})

            if path.startswith("/api/bootstrap"):
                trip = (qs.get("trip") or [""])[0]
                me = self._me(conn, qs)
                r = role_of(conn, trip, me["id"] if me else None)
                if r < 1:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                t = conn.execute("SELECT * FROM trips WHERE id=?", (trip,)).fetchone()
                items = [dict(x) for x in conn.execute(
                    "SELECT id,day,sort,time,title,sub,kind,place_id,photo FROM items "
                    "WHERE trip=? ORDER BY day,sort,id", (trip,))]
                marks = {x["item"]: x["status"] for x in conn.execute(
                    "SELECT item,status FROM marks WHERE trip=? AND status<>''", (trip,))}
                pois = [dict(x) for x in conn.execute(
                    "SELECT * FROM pois WHERE trip=? OR trip IS NULL ORDER BY group_key,name", (trip,))]
                photos = [dict(x) for x in conn.execute(
                    "SELECT id,trip,file,caption,day,item_id,created FROM photos "
                    "WHERE trip=? ORDER BY created DESC LIMIT 300", (trip,))]
                return self._json(200, {"ok": True, "rev": t["rev"], "role": ROLE_LABEL[r],
                                        "can_edit": r >= 2, "trip": {
                                            "id": t["id"], "title": t["title"],
                                            "start_date": t["start_date"], "end_date": t["end_date"],
                                            "days": t["days"], "share_token": t["share_token"] if r >= 3 else None},
                                        "items": items, "marks": marks, "pois": pois, "photos": photos})

            if path.startswith("/api/state"):
                trip = (qs.get("trip") or [""])[0]
                me = self._me(conn, qs)
                if role_of(conn, trip, me["id"] if me else None) < 1:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                t = conn.execute("SELECT rev FROM trips WHERE id=?", (trip,)).fetchone()
                return self._json(200, {"ok": True, "rev": t["rev"],
                                        "marks": {x["item"]: x["status"] for x in conn.execute(
                                            "SELECT item,status FROM marks WHERE trip=? AND status<>''",
                                            (trip,))}})

            if path.startswith("/api/photo/"):
                fname = os.path.basename(path)
                fpath = os.path.join(PHOTOS, fname)
                if os.path.isfile(fpath):
                    ext = os.path.splitext(fname)[1].lower()
                    ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                             ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
                    with open(fpath, "rb") as f:
                        return self._send(200, f.read(), ctype)
                return self._send(404, "no photo", "text/plain")

            return self._static(os.path.basename(path))
        finally:
            conn.close()

    def _static(self, name):
        fpath = os.path.join(STATIC, name)
        if os.path.isfile(fpath):
            ext = os.path.splitext(name)[1].lower()
            ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                     ".js": "application/javascript", ".png": "image/png",
                     ".jpg": "image/jpeg", ".json": "application/json"}.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                return self._send(200, f.read(), ctype)
        self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- POST
    def do_POST(self):
        path = urlparse(self.path).path
        d = self._body()
        conn = db()
        try:
            # ---------- auth
            if path == "/api/register":
                email = (d.get("email") or "").strip().lower()
                pw = d.get("password") or ""
                if "@" not in email or len(pw) < 6:
                    return self._json(400, {"ok": False, "error": "email 或密碼太短（最少 6 位）"})
                if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    return self._json(409, {"ok": False, "error": "email 已註冊"})
                pw_hash, salt = hash_pw(pw)
                uid = "u-" + uuid.uuid4().hex[:10]
                now = time.time()
                conn.execute("INSERT INTO users(id,email,name,pw_hash,pw_salt,created,last_login) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (uid, email, d.get("name") or "", pw_hash, salt, now, now))
                # auto-accept pending invites
                for inv in conn.execute("SELECT trip,role FROM invites WHERE email=?", (email,)).fetchall():
                    conn.execute("INSERT OR IGNORE INTO members(trip,user,role,created) VALUES(?,?,?,?)",
                                 (inv["trip"], uid, inv["role"], now))
                    conn.execute("DELETE FROM invites WHERE trip=? AND email=?", (inv["trip"], email))
                tok = new_token()
                conn.execute("INSERT INTO sessions(token,user,created,expires,ua) VALUES(?,?,?,?,?)",
                             (tok, uid, now, now + 60 * 60 * 24 * 60, self.headers.get("User-Agent", "")[:200]))
                conn.commit()
                return self._json(200, {"ok": True, "token": tok,
                                        "user": {"id": uid, "email": email, "name": d.get("name") or email}})

            if path == "/api/login":
                email = (d.get("email") or "").strip().lower()
                pw = d.get("password") or ""
                u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if u is None:
                    return self._json(401, {"ok": False, "error": "帳戶或密碼錯"})
                h, _ = hash_pw(pw, u["pw_salt"])
                if not hmac.compare_digest(h, u["pw_hash"]):
                    return self._json(401, {"ok": False, "error": "帳戶或密碼錯"})
                now = time.time()
                tok = new_token()
                conn.execute("INSERT INTO sessions(token,user,created,expires,ua) VALUES(?,?,?,?,?)",
                             (tok, u["id"], now, now + 60 * 60 * 24 * 60, self.headers.get("User-Agent", "")[:200]))
                conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
                conn.commit()
                return self._json(200, {"ok": True, "token": tok, "user": user_public(u)})

            if path == "/api/logout":
                tok = self._token()
                if tok:
                    conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
                    conn.commit()
                return self._json(200, {"ok": True})

            # ---------- trips
            if path == "/api/trips":
                me = self._me(conn)
                if me is None:
                    return self._json(401, {"ok": False, "error": "auth"})
                title = (d.get("title") or "新旅程").strip()[:120]
                days = int(d.get("days") or 3)
                days = max(1, min(30, days))
                tid = "t-" + uuid.uuid4().hex[:10]
                now = time.time()
                conn.execute("INSERT INTO trips(id,title,owner,start_date,end_date,days,share_token,"
                             "share_role,rev,created,updated) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                             (tid, title, me["id"], d.get("start_date"), d.get("end_date"), days,
                              secrets.token_urlsafe(8), "viewer", now, now))
                conn.commit()
                return self._json(200, {"ok": True, "trip_id": tid})

            if path == "/api/trips/rename":
                me = self._me(conn)
                trip = str(d.get("trip_id") or d.get("trip") or "")
                if me is None or require(conn, trip, me["id"], 2) is None:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                sets, vals = [], []
                for k in ("title", "start_date", "end_date"):
                    if k in d:
                        sets.append(f"{k}=?")
                        vals.append(d[k])
                if "days" in d:
                    sets.append("days=?")
                    vals.append(max(1, min(30, int(d["days"]))))
                if sets:
                    conn.execute(f"UPDATE trips SET {','.join(sets)}, updated=? WHERE id=?",
                                 (*vals, time.time(), trip))
                    conn.commit()
                return self._json(200, {"ok": True, "rev": bump(conn, trip)})

            if path == "/api/trips/delete":
                me = self._me(conn)
                trip = str(d.get("trip_id") or d.get("trip") or "")
                if me is None or require(conn, trip, me["id"], 3) is None:
                    return self._json(403, {"ok": False, "error": "只有 owner 可以刪"})
                for tb in ("items", "marks", "members", "invites", "pois"):
                    conn.execute(f"DELETE FROM {tb} WHERE trip=?", (trip,))
                files = [r["file"] for r in conn.execute("SELECT file FROM photos WHERE trip=?", (trip,))]
                conn.execute("DELETE FROM photos WHERE trip=?", (trip,))
                conn.execute("DELETE FROM trips WHERE id=?", (trip,))
                conn.commit()
                for f in files:
                    try:
                        os.remove(os.path.join(PHOTOS, f))
                    except OSError:
                        pass
                return self._json(200, {"ok": True})

            if path == "/api/trips/invite":
                me = self._me(conn)
                trip = str(d.get("trip_id") or "")
                if me is None or require(conn, trip, me["id"], 3) is None:
                    return self._json(403, {"ok": False, "error": "只有 owner 可以邀請"})
                email = (d.get("email") or "").strip().lower()
                role = d.get("role") or "editor"
                if "@" not in email or role not in ("editor", "viewer"):
                    return self._json(400, {"ok": False, "error": "email/role 唔啱"})
                u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                if u:
                    conn.execute("INSERT INTO members(trip,user,role,created) VALUES(?,?,?,?) "
                                 "ON CONFLICT(trip,user) DO UPDATE SET role=excluded.role",
                                 (trip, u["id"], role, time.time()))
                else:
                    conn.execute("INSERT INTO invites(id,trip,email,role,created) VALUES(?,?,?,?,?) "
                                 "ON CONFLICT(trip,email) DO UPDATE SET role=excluded.role",
                                 ("inv-" + uuid.uuid4().hex[:8], trip, email, role, time.time()))
                conn.commit()
                return self._json(200, {"ok": True})

            if path == "/api/trips/share":
                me = self._me(conn)
                trip = str(d.get("trip_id") or "")
                if me is None or require(conn, trip, me["id"], 3) is None:
                    return self._json(403, {"ok": False, "error": "只有 owner 可以分享"})
                role = d.get("role") or "viewer"
                tok = secrets.token_urlsafe(8)
                conn.execute("UPDATE trips SET share_token=?, share_role=? WHERE id=?", (tok, role, trip))
                conn.commit()
                return self._json(200, {"ok": True, "share_token": tok, "role": role})

            # ---------- data (needs editor)
            trip = str(d.get("trip") or "")
            me = self._me(conn)

            if path == "/api/set":
                if me is None or require(conn, trip, me["id"], 2) is None:
                    return self._json(403, {"ok": False, "error": "forbidden"})
                item = str(d.get("item") or "")
                status = d.get("status", "")
                if status not in STATUSES or not item:
                    return self._json(400, {"ok": False, "error": "bad request"})
                conn.execute(
                    "INSERT INTO marks(trip,item,status,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(trip,item) DO UPDATE SET status=excluded.status,updated=excluded.updated",
                    (trip, item, status, time.time()))
                conn.commit()
                return self._json(200, {"ok": True, "rev": bump(conn, trip)})

            if path in ("/api/item", "/api/item/delete", "/api/item/move",
                        "/api/photo/commit", "/api/photo/attach", "/api/photo/delete", "/api/poi"):
                if me is None or require(conn, trip, me["id"], 2) is None:
                    return self._json(403, {"ok": False, "error": "forbidden"})

            if path == "/api/item":
                iid = str(d.get("id") or ("it-" + uuid.uuid4().hex[:8]))
                day = str(d.get("day") or "d1")
                now = time.time()
                exists = conn.execute("SELECT 1 FROM items WHERE trip=? AND id=?", (trip, iid)).fetchone()
                sort = d.get("sort")
                if sort is None:
                    mx = conn.execute("SELECT COALESCE(MAX(sort),0) FROM items WHERE trip=? AND day=?",
                                      (trip, day)).fetchone()[0]
                    sort = mx + 10
                if exists:
                    conn.execute(
                        "UPDATE items SET day=?,sort=?,time=?,title=?,sub=?,kind=?,place_id=?,"
                        "photo=COALESCE(?,photo),updated=? WHERE trip=? AND id=?",
                        (day, float(sort), d.get("time", ""), d.get("title", ""), d.get("sub", ""),
                         d.get("kind", ""), d.get("place_id") or None, d.get("photo") or None,
                         now, trip, iid))
                else:
                    conn.execute(
                        "INSERT INTO items(trip,id,day,sort,time,title,sub,kind,place_id,photo,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (trip, iid, day, float(sort), d.get("time", ""), d.get("title", ""),
                         d.get("sub", ""), d.get("kind", ""), d.get("place_id") or None,
                         d.get("photo") or None, now, now))
                conn.commit()
                return self._json(200, {"ok": True, "id": iid, "rev": bump(conn, trip)})

            if path == "/api/item/delete":
                iid = str(d.get("id") or "")
                conn.execute("DELETE FROM items WHERE trip=? AND id=?", (trip, iid))
                conn.execute("DELETE FROM marks WHERE trip=? AND item=?", (trip, iid))
                conn.commit()
                return self._json(200, {"ok": True, "rev": bump(conn, trip)})

            if path == "/api/item/move":
                iid = str(d.get("id") or "")
                direction = d.get("dir")
                row = conn.execute("SELECT day,sort FROM items WHERE trip=? AND id=?", (trip, iid)).fetchone()
                if row is None:
                    return self._json(404, {"ok": False, "error": "no item"})
                day, sort = row["day"], row["sort"]
                if direction == "up":
                    prev = conn.execute("SELECT id,sort FROM items WHERE trip=? AND day=? AND sort<? "
                                        "ORDER BY sort DESC LIMIT 1", (trip, day, sort)).fetchone()
                    if prev:
                        conn.execute("UPDATE items SET sort=? WHERE trip=? AND id=?", (prev["sort"], trip, prev["id"]))
                        conn.execute("UPDATE items SET sort=? WHERE trip=? AND id=?", (sort, trip, iid))
                elif direction == "down":
                    nxt = conn.execute("SELECT id,sort FROM items WHERE trip=? AND day=? AND sort>? "
                                       "ORDER BY sort ASC LIMIT 1", (trip, day, sort)).fetchone()
                    if nxt:
                        conn.execute("UPDATE items SET sort=? WHERE trip=? AND id=?", (nxt["sort"], trip, nxt["id"]))
                        conn.execute("UPDATE items SET sort=? WHERE trip=? AND id=?", (sort, trip, iid))
                conn.commit()
                return self._json(200, {"ok": True, "rev": bump(conn, trip)})

            if path == "/api/photo/analyze":
                if me is None:
                    return self._json(401, {"ok": False, "error": "auth"})
                img = d.get("image") or ""
                if "," in img:
                    img = img.split(",", 1)[1]
                try:
                    raw = base64.b64decode(img)
                except Exception:
                    return self._json(400, {"ok": False, "error": "bad image"})
                if len(raw) > 12 * 1024 * 1024:
                    return self._json(413, {"ok": False, "error": "too large"})
                try:
                    parsed, rawtxt = analyze_photo(raw, d.get("mime") or "image/jpeg", d.get("hint") or "")
                except Exception as e:
                    return self._json(502, {"ok": False, "error": "vision failed: " + str(e)[:200]})
                return self._json(200, {"ok": True, "suggestion": parsed, "raw": rawtxt})

            if path in ("/api/photo/commit", "/api/photo/attach"):
                img = d.get("image") or ""
                if "," in img:
                    img = img.split(",", 1)[1]
                try:
                    raw = base64.b64decode(img)
                except Exception:
                    return self._json(400, {"ok": False, "error": "bad image"})
                os.makedirs(PHOTOS, exist_ok=True)
                pid = "ph-" + uuid.uuid4().hex[:10]
                ext = ".png" if (d.get("mime") or "").endswith("png") else ".jpg"
                fname = pid + ext
                with open(os.path.join(PHOTOS, fname), "wb") as f:
                    f.write(raw)
                now = time.time()
                day = str(d.get("day") or "d1")
                if path.endswith("attach"):
                    iid = str(d.get("item_id") or "")
                    if not iid:
                        return self._json(400, {"ok": False, "error": "no item_id"})
                    conn.execute("INSERT INTO photos(id,trip,file,caption,vision,day,item_id,created) "
                                 "VALUES(?,?,?,?,?,?,?,?)",
                                 (pid, trip, fname, d.get("caption", ""), d.get("raw", ""),
                                  d.get("day", ""), iid, now))
                    row = conn.execute("SELECT photo FROM items WHERE trip=? AND id=?", (trip, iid)).fetchone()
                    if row is not None and not row["photo"]:
                        conn.execute("UPDATE items SET photo=?,updated=? WHERE trip=? AND id=?",
                                     (fname, now, trip, iid))
                    conn.commit()
                    return self._json(200, {"ok": True, "photo": fname, "id": pid, "rev": bump(conn, trip)})

                iid = str(d.get("item_id") or ("it-" + uuid.uuid4().hex[:8]))
                mx = conn.execute("SELECT COALESCE(MAX(sort),0) FROM items WHERE trip=? AND day=?",
                                  (trip, day)).fetchone()[0]
                exists = conn.execute("SELECT 1 FROM items WHERE trip=? AND id=?", (trip, iid)).fetchone()
                if exists:
                    conn.execute("UPDATE items SET day=?,sort=?,time=?,title=?,sub=?,kind=?,place_id=?,"
                                 "photo=?,updated=? WHERE trip=? AND id=?",
                                 (day, float(d.get("sort", mx + 10)), d.get("time", ""), d.get("title", ""),
                                  d.get("sub", ""), d.get("kind", ""), d.get("place_id") or None,
                                  fname, now, trip, iid))
                else:
                    conn.execute("INSERT INTO items(trip,id,day,sort,time,title,sub,kind,place_id,photo,"
                                 "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (trip, iid, day, float(d.get("sort", mx + 10)), d.get("time", ""),
                                  d.get("title", ""), d.get("sub", ""), d.get("kind", ""),
                                  d.get("place_id") or None, fname, now, now))
                conn.execute("INSERT INTO photos(id,trip,file,caption,vision,day,item_id,created) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (pid, trip, fname, d.get("title", ""), d.get("raw", ""), day, iid, now))
                conn.commit()
                return self._json(200, {"ok": True, "id": iid, "photo": fname, "rev": bump(conn, trip)})

            if path == "/api/photo/delete":
                pid = str(d.get("photo_id") or "")
                row = conn.execute("SELECT file,item_id FROM photos WHERE trip=? AND id=?", (trip, pid)).fetchone()
                if row is None:
                    return self._json(404, {"ok": False, "error": "no photo"})
                fname, iid = row["file"], row["item_id"]
                conn.execute("DELETE FROM photos WHERE trip=? AND id=?", (trip, pid))
                it = conn.execute("SELECT photo FROM items WHERE trip=? AND id=?", (trip, iid)).fetchone()
                if it is not None and it["photo"] == fname:
                    nxt = conn.execute("SELECT file FROM photos WHERE trip=? AND item_id=? "
                                       "ORDER BY created DESC LIMIT 1", (trip, iid)).fetchone()
                    conn.execute("UPDATE items SET photo=?,updated=? WHERE trip=? AND id=?",
                                 (nxt["file"] if nxt else None, time.time(), trip, iid))
                conn.commit()
                try:
                    os.remove(os.path.join(PHOTOS, fname))
                except OSError:
                    pass
                return self._json(200, {"ok": True, "rev": bump(conn, trip)})

            if path == "/api/poi":
                pid = str(d.get("id") or ("poi-" + uuid.uuid4().hex[:8]))
                conn.execute(
                    "INSERT INTO pois(id,trip,name,name_en,addr,lat,lng,kind,hours,note,group_key,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,name_en=excluded.name_en,"
                    "addr=excluded.addr,lat=excluded.lat,lng=excluded.lng,kind=excluded.kind,"
                    "hours=excluded.hours,note=excluded.note,group_key=excluded.group_key,"
                    "updated=excluded.updated",
                    (pid, trip, d.get("name", ""), d.get("name_en", ""), d.get("addr", ""),
                     d.get("lat"), d.get("lng"), d.get("kind", ""), d.get("hours", ""),
                     d.get("note", ""), d.get("group_key", "trip"), time.time()))
                conn.commit()
                return self._json(200, {"ok": True, "id": pid, "rev": bump(conn, trip)})

            return self._json(404, {"ok": False, "error": "not found"})
        finally:
            conn.close()

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(PHOTOS, exist_ok=True)
    os.makedirs(STATIC, exist_ok=True)
    conn = db()
    conn.close()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"trip app serving on 0.0.0.0:{PORT}  data={DATA}", flush=True)
    srv.serve_forever()
