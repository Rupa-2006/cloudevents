"""Cloud-Based Event Management and Registration System.

23CSC205 Cloud Computing Laboratory mini-project.
Flask application with four modules:
  1. Authentication and access control
  2. Event management (admin CRUD, search, filter, pagination)
  3. Registration and ticketing (capacity, waiting list, QR e-ticket, check-in)
  4. Admin analytics, monitoring, user management, export and backup
"""
import csv
import io
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps

import segno
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from markupsafe import Markup
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATEGORIES = ["Technical", "Workshop", "Cultural", "Sports", "Seminar"]
DEFAULT_ADMIN_PASSWORD = "admin123"   # sample value, flagged at login until changed
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EVENTS_PER_PAGE = 9
LOGS_PER_PAGE = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    category    TEXT NOT NULL,
    venue       TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    capacity    INTEGER NOT NULL CHECK (capacity > 0),
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    details    TEXT,
    ip         TEXT,
    created_at TEXT NOT NULL
);
"""

REGISTRATIONS_TABLE = """
CREATE TABLE registrations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    ticket_code   TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'confirmed'
                  CHECK (status IN ('confirmed', 'waitlisted', 'cancelled')),
    registered_at TEXT NOT NULL,
    checked_in_at TEXT,
    UNIQUE (user_id, event_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_reg_event ON registrations(event_id, status);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_log_action ON activity_log(action, details);
"""

EVENT_SELECT = """
SELECT e.*,
       (SELECT COUNT(*) FROM registrations r
         WHERE r.event_id = e.id AND r.status = 'confirmed') AS booked,
       (SELECT COUNT(*) FROM registrations r
         WHERE r.event_id = e.id AND r.status = 'waitlisted') AS waiting
  FROM events e
"""

TICKET_SELECT = """
SELECT r.*, e.title, e.venue, e.event_date, e.event_time, e.category, u.name, u.email
  FROM registrations r
  JOIN events e ON e.id = r.event_id
  JOIN users u ON u.id = r.user_id
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return date.today().isoformat()


def new_ticket_code():
    return "EVT-" + secrets.token_hex(4).upper()


def password_problem(password):
    """Return why a password is too weak, or None when it is acceptable."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    return None


def csv_safe(value):
    """Stop spreadsheet formula injection: a cell that starts with = + - @
    would be run as a formula by Excel, so it is prefixed with a quote."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def create_app(test_config=None):
    app = Flask(__name__)
    # Twelve-factor style configuration: everything comes from the environment,
    # so the same code runs locally, in a container and on a cloud platform.
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=os.environ.get("DATABASE_PATH",
                                os.path.join(BASE_DIR, "instance", "events.db")),
        CSRF_ENABLED=True,
        SEED_SAMPLE_DATA=True,
        # Brute-force protection: after this many wrong passwords for one
        # account from one IP address, further attempts are blocked.
        LOGIN_MAX_ATTEMPTS=5,
        LOGIN_LOCK_MINUTES=5,
        # Session cookie hardening. HttpOnly keeps scripts from reading the
        # cookie, SameSite stops other sites sending it with their requests,
        # and Secure (set COOKIE_SECURE=1 once hosted on HTTPS) keeps it off
        # unencrypted connections.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=1024 * 1024,      # reject request bodies over 1 MB
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        # No key supplied: generate one once and keep it beside the database,
        # so sessions survive restarts and all worker threads share it.
        key_file = os.path.join(os.path.dirname(app.config["DATABASE"]) or ".",
                                "secret_key")
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        if not os.path.exists(key_file):
            with open(key_file, "w") as fh:
                fh.write(secrets.token_hex(32))
        with open(key_file) as fh:
            app.config["SECRET_KEY"] = fh.read().strip()

    # ------------------------------------------------------------------ db
    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], isolation_level=None,
                                   timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @contextmanager
    def write_tx():
        """One atomic write. BEGIN IMMEDIATE takes the write lock up front, so
        two requests cannot both read "one seat left" and both take it."""
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    def log(action, details=""):
        get_db().execute(
            "INSERT INTO activity_log (user_id, action, details, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session.get("user_id"), action, details, request.remote_addr, now()))

    def init_db():
        folder = os.path.dirname(app.config["DATABASE"])
        if folder:
            os.makedirs(folder, exist_ok=True)
        db = get_db()
        db.execute("PRAGMA journal_mode = WAL")   # readers do not block the writer
        db.executescript(SCHEMA)
        existing = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' "
                              "AND name = 'registrations'").fetchone()
        if existing is None:
            db.executescript(REGISTRATIONS_TABLE)
        elif "waitlisted" not in existing["sql"]:
            migrate_registrations(db)
        db.executescript(INDEXES)
        if app.config["SEED_SAMPLE_DATA"] and \
                db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed(db)

    def migrate_registrations(db):
        """Schema migration for databases created by version 1, which had no
        waiting list or check-in. SQLite cannot alter a CHECK constraint, so
        the table is rebuilt and the rows are copied across."""
        db.executescript(
            "PRAGMA foreign_keys = OFF; BEGIN;"
            "ALTER TABLE registrations RENAME TO registrations_v1;"
            + REGISTRATIONS_TABLE +
            "INSERT INTO registrations (id, user_id, event_id, ticket_code, status, "
            "registered_at) SELECT id, user_id, event_id, ticket_code, status, "
            "registered_at FROM registrations_v1;"
            "DROP TABLE registrations_v1; COMMIT; PRAGMA foreign_keys = ON;")

    def seed(db):
        """Synthetic sample data only - no real personal data."""
        people = [("Admin", "admin@example.com", DEFAULT_ADMIN_PASSWORD, "admin"),
                  ("Asha Verma", "asha@example.com", "student123", "user"),
                  ("Kiran Rao", "kiran@example.com", "student123", "user")]
        for name, email, pw, role in people:
            db.execute("INSERT INTO users (name, email, password_hash, role, created_at) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (name, email, generate_password_hash(pw), role, now()))
        events = [
            ("Cloud Computing Workshop", "Hands-on session on deploying a Flask "
             "application to a public cloud platform.", "Workshop",
             "Seminar Hall A", 7, "10:00", 60),
            ("Capture The Flag 2026", "A beginner friendly cyber security CTF "
             "with web, crypto and forensics challenges.", "Technical",
             "CSE Lab 3", 12, "09:30", 40),
            ("Hackathon: Code for Campus", "24-hour hackathon to build solutions "
             "for everyday campus problems.", "Technical",
             "Innovation Centre", 20, "09:00", 100),
            ("Guest Lecture on Zero Trust Security", "Industry expert talk on "
             "zero trust architecture and identity management.", "Seminar",
             "Auditorium", 5, "14:00", 200),
            ("Annual Cultural Fest", "Music, dance and drama performances by "
             "student clubs.", "Cultural", "Open Air Theatre", 30, "17:00", 500),
            ("Inter-Department Cricket", "Knock-out cricket tournament between "
             "departments. Only two team slots.", "Sports", "Main Ground", 15, "07:00", 2),
            ("Docker and Kubernetes Bootcamp", "Two-day bootcamp on containers, "
             "images and orchestration.", "Workshop", "CSE Lab 1", 25, "09:30", 45),
            ("Ethical Hacking Seminar", "Introduction to penetration testing "
             "methodology and responsible disclosure.", "Seminar",
             "Seminar Hall B", 35, "11:00", 120),
            ("Badminton Singles Open", "Open singles tournament for students "
             "and staff.", "Sports", "Indoor Stadium", 40, "16:00", 32),
            ("Project Expo", "Showcase of final-year and mini projects with "
             "industry judges.", "Technical", "Main Block Foyer", 45, "10:00", 150),
            ("Freshers Orientation", "Welcome session and campus tour for "
             "first-year students.", "Cultural", "Auditorium", -10, "10:00", 300),
        ]
        for title, desc, cat, venue, days, time_, cap in events:
            db.execute(
                "INSERT INTO events (title, description, category, venue, event_date, "
                "event_time, capacity, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (title, desc, cat, venue,
                 (date.today() + timedelta(days=days)).isoformat(), time_, cap, now()))
        for user_id, event_id in [(2, 1), (2, 2), (3, 1), (3, 4), (2, 6)]:
            db.execute("INSERT INTO registrations (user_id, event_id, ticket_code, "
                       "status, registered_at) VALUES (?, ?, ?, 'confirmed', ?)",
                       (user_id, event_id, new_ticket_code(), now()))

    # ------------------------------------------------------- request hooks
    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        if session.get("user_id"):
            g.user = get_db().execute("SELECT * FROM users WHERE id = ?",
                                      (session["user_id"],)).fetchone()
            if g.user is None:
                session.clear()
        if request.method == "POST" and app.config["CSRF_ENABLED"]:
            token = session.get("csrf_token")
            if not token or not secrets.compare_digest(
                    token, request.form.get("csrf_token", "")):
                abort(400, "Invalid or missing CSRF token.")

    @app.after_request
    def security_headers(resp):
        # Only our own scripts, styles and images may load; no inline script.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if g.get("user") is not None:
            # Never let a shared computer's browser cache a logged-in page.
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject_helpers():
        def csrf_token():
            if "csrf_token" not in session:
                session["csrf_token"] = secrets.token_hex(16)
            return session["csrf_token"]

        def page_url(page):
            args = {**(request.view_args or {}), **request.args.to_dict(), "page": page}
            return url_for(request.endpoint, **args)

        return {"csrf_token": csrf_token, "page_url": page_url,
                "current_user": g.get("user"), "categories": CATEGORIES,
                "today": today()}

    # ---------------------------------------------- display formatting
    def as_date(value):
        return datetime.strptime(value, "%Y-%m-%d").date()

    @app.template_filter("nice_date")
    def nice_date(value):                       # 2026-11-21 -> Sat, 21 Nov 2026
        d = as_date(value)
        return f"{d:%a}, {d.day} {d:%b %Y}"

    @app.template_filter("mon")
    def month_abbr(value):                      # 2026-11-21 -> NOV
        return f"{as_date(value):%b}".upper()

    @app.template_filter("day")
    def day_of_month(value):                    # 2026-11-21 -> 21
        return str(as_date(value).day)

    @app.template_filter("nice_time")
    def nice_time(value):                       # 14:00 -> 2:00 PM
        return datetime.strptime(value, "%H:%M").strftime("%I:%M %p").lstrip("0")

    @app.template_filter("relative_day")
    def relative_day(value):                    # how far away an event is
        days = (as_date(value) - date.today()).days
        if days < 0:
            return "Ended"
        if days == 0:
            return "Today"
        if days == 1:
            return "Tomorrow"
        if days < 14:
            return f"In {days} days"
        return f"In {days // 7} weeks"

    @app.template_filter("initials")
    def initials(name):                         # Asha Verma -> AV
        return "".join(part[0] for part in (name or "").split()[:2]).upper() or "?"

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def admin_required(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] != "admin":
                abort(403)
            return view(*args, **kwargs)
        return wrapped

    # ---------------------------------------------------------- helpers
    def paginate(sql, params, per_page):
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        db = get_db()
        total = db.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        pages = max(1, -(-total // per_page))
        page = min(page, pages)
        rows = db.execute(sql + " LIMIT ? OFFSET ?",
                          [*params, per_page, (page - 1) * per_page]).fetchall()
        return rows, {"page": page, "pages": pages, "total": total}

    def get_event_or_404(event_id):
        event = get_db().execute(EVENT_SELECT + " WHERE e.id = ?",
                                 (event_id,)).fetchone()
        if event is None:
            abort(404)
        return event

    def waitlist_position(reg):
        return get_db().execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id = ? "
            "AND status = 'waitlisted' AND (registered_at < ? OR "
            "(registered_at = ? AND id <= ?))",
            (reg["event_id"], reg["registered_at"], reg["registered_at"],
             reg["id"])).fetchone()[0]

    def promote_waitlist(event_id):
        """Give freed seats to the people who have waited longest."""
        db, promoted = get_db(), 0
        while True:
            event = db.execute(EVENT_SELECT + " WHERE e.id = ?", (event_id,)).fetchone()
            if event is None or event["booked"] >= event["capacity"] \
                    or event["event_date"] < today():
                break
            nxt = db.execute(
                "SELECT r.id, u.email FROM registrations r JOIN users u ON u.id = r.user_id "
                "WHERE r.event_id = ? AND r.status = 'waitlisted' "
                "ORDER BY r.registered_at, r.id LIMIT 1", (event_id,)).fetchone()
            if nxt is None:
                break
            db.execute("UPDATE registrations SET status = 'confirmed' WHERE id = ?",
                       (nxt["id"],))
            log("waitlist_promoted", f"{event['title']}: {nxt['email']}")
            promoted += 1
        return promoted

    # ------------------------------------------- module 1: authentication
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            name = " ".join(request.form.get("name", "").split())
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            errors = []
            if not 2 <= len(name) <= 80:
                errors.append("Name must be 2 to 80 characters.")
            if not EMAIL_RE.match(email) or len(email) > 254:
                errors.append("Enter a valid email address.")
            if password_problem(password):
                errors.append(password_problem(password))
            if password != request.form.get("confirm", ""):
                errors.append("Passwords do not match.")
            db = get_db()
            if not errors and db.execute("SELECT 1 FROM users WHERE email = ?",
                                         (email,)).fetchone():
                errors.append("An account with this email already exists.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("register.html", name=name, email=email), 400
            db.execute("INSERT INTO users (name, email, password_hash, role, created_at) "
                       "VALUES (?, ?, ?, 'user', ?)",
                       (name, email, generate_password_hash(password), now()))
            log("user_registered", email)
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        return render_template("register.html", name="", email="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            db = get_db()
            # Count wrong passwords for this account from this IP address
            # inside the lock window, since the last successful login.
            window_start = (datetime.now() - timedelta(
                minutes=app.config["LOGIN_LOCK_MINUTES"])).strftime("%Y-%m-%d %H:%M:%S")
            last_ok = db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM activity_log "
                "WHERE action = 'login_success' AND details = ?", (email,)).fetchone()[0]
            failures = db.execute(
                "SELECT COUNT(*) FROM activity_log WHERE action = 'login_failed' "
                "AND details = ? AND ip = ? AND id > ? AND created_at >= ?",
                (email, request.remote_addr, last_ok, window_start)).fetchone()[0]
            if failures >= app.config["LOGIN_MAX_ATTEMPTS"]:
                log("login_blocked", email)
                flash("Too many failed attempts. Try again in "
                      f"{app.config['LOGIN_LOCK_MINUTES']} minutes.", "error")
                return render_template("login.html", email=email), 429
            user = db.execute("SELECT * FROM users WHERE email = ?",
                              (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()                 # new session: no session fixation
                session.permanent = True        # expires after 8 hours
                session["user_id"] = user["id"]
                log("login_success", email)
                flash(f"Welcome, {user['name']}.", "success")
                if user["role"] == "admin" and password == DEFAULT_ADMIN_PASSWORD:
                    flash("You are using the default sample admin password. "
                          "Change it from the account page.", "warning")
                nxt = request.args.get("next", "")
                # Only same-site paths are allowed, so the login link cannot
                # be abused to bounce users to another website.
                if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                    nxt = url_for("admin_dashboard" if user["role"] == "admin"
                                  else "index")
                return redirect(nxt)
            log("login_failed", email)
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email), 401
        return render_template("login.html", email="")

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            current = request.form.get("current", "")
            new = request.form.get("new", "")
            errors = []
            if not check_password_hash(g.user["password_hash"], current):
                errors.append("Current password is incorrect.")
            if password_problem(new):
                errors.append(password_problem(new).replace("Password", "New password"))
            elif new == current:
                errors.append("New password must be different from the current one.")
            if new != request.form.get("confirm", ""):
                errors.append("New passwords do not match.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("change_password.html"), 400
            get_db().execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new), g.user["id"]))
            log("password_changed", g.user["email"])
            flash("Password changed.", "success")
            return redirect(url_for("index"))
        return render_template("change_password.html")

    @app.route("/account/email", methods=["GET", "POST"])
    @login_required
    def change_email():
        if request.method == "POST":
            new = request.form.get("email", "").strip().lower()
            errors = []
            if not check_password_hash(g.user["password_hash"],
                                       request.form.get("password", "")):
                errors.append("Password is incorrect.")
            if not EMAIL_RE.match(new) or len(new) > 254:
                errors.append("Enter a valid email address.")
            elif new == g.user["email"]:
                errors.append("That is already your email address.")
            elif get_db().execute("SELECT 1 FROM users WHERE email = ?",
                                  (new,)).fetchone():
                errors.append("Another account already uses this email.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("change_email.html", email=new), 400
            get_db().execute("UPDATE users SET email = ? WHERE id = ?",
                             (new, g.user["id"]))
            log("email_changed", f"{g.user['email']} -> {new}")
            flash(f"Email changed. Use {new} to log in from now on.", "success")
            return redirect(url_for("index"))
        return render_template("change_email.html", email="")

    @app.route("/logout", methods=["POST"])
    def logout():
        log("logout")
        session.clear()
        flash("You have been logged out.", "success")
        return redirect(url_for("index"))

    # ---------------------------------------------- module 2: event browse
    @app.route("/")
    def index():
        q = request.args.get("q", "").strip()[:100]
        category = request.args.get("category", "")
        when = request.args.get("when", "upcoming")
        sql, params = EVENT_SELECT + " WHERE 1 = 1", []
        if q:
            sql += " AND (e.title LIKE ? OR e.description LIKE ? OR e.venue LIKE ?)"
            params += [f"%{q}%"] * 3
        if category in CATEGORIES:
            sql += " AND e.category = ?"
            params.append(category)
        if when == "upcoming":
            sql += " AND e.event_date >= ?"
            params.append(today())
        elif when == "past":
            sql += " AND e.event_date < ?"
            params.append(today())
        sql += " ORDER BY e.event_date " + ("DESC" if when == "past" else "ASC") \
               + ", e.event_time, e.id"
        events, pager = paginate(sql, params, EVENTS_PER_PAGE)
        return render_template("index.html", events=events, pager=pager, q=q,
                               category=category, when=when)

    @app.route("/events/<int:event_id>")
    def event_detail(event_id):
        event = get_event_or_404(event_id)
        registration, position = None, None
        if g.user:
            registration = get_db().execute(
                "SELECT * FROM registrations WHERE user_id = ? AND event_id = ?",
                (g.user["id"], event_id)).fetchone()
            if registration and registration["status"] == "waitlisted":
                position = waitlist_position(registration)
        return render_template("event_detail.html", event=event,
                               registration=registration, position=position)

    # ----------------------------------- module 3: registration & ticketing
    @app.route("/events/<int:event_id>/register", methods=["POST"])
    @login_required
    def register_for_event(event_id):
        with write_tx() as db:
            event = get_event_or_404(event_id)
            existing = db.execute(
                "SELECT * FROM registrations WHERE user_id = ? AND event_id = ?",
                (g.user["id"], event_id)).fetchone()
            if event["event_date"] < today():
                outcome = ("error", "Registration is closed: this event is over.")
            elif existing and existing["status"] == "confirmed":
                outcome = ("error", "You are already registered for this event.")
            elif existing and existing["status"] == "waitlisted":
                outcome = ("error", "You are already on the waiting list.")
            else:
                status = "confirmed" if event["booked"] < event["capacity"] \
                    else "waitlisted"
                if existing:
                    db.execute("UPDATE registrations SET status = ?, registered_at = ?, "
                               "checked_in_at = NULL WHERE id = ?",
                               (status, now(), existing["id"]))
                else:
                    db.execute("INSERT INTO registrations (user_id, event_id, "
                               "ticket_code, status, registered_at) VALUES (?, ?, ?, ?, ?)",
                               (g.user["id"], event_id, new_ticket_code(), status, now()))
                if status == "confirmed":
                    log("event_registered", event["title"])
                    outcome = ("success", "Registration confirmed. Your e-ticket is ready.")
                else:
                    log("waitlist_joined", event["title"])
                    outcome = ("warning", "This event is full, so you were added to the "
                               f"waiting list at position {event['waiting'] + 1}. You get "
                               "a seat automatically if someone cancels.")
        flash(outcome[1], outcome[0])
        if outcome[0] == "error":
            return redirect(url_for("event_detail", event_id=event_id))
        return redirect(url_for("my_registrations"))

    @app.route("/registrations/<int:reg_id>/cancel", methods=["POST"])
    @login_required
    def cancel_registration(reg_id):
        with write_tx() as db:
            reg = db.execute("SELECT r.*, e.title FROM registrations r "
                             "JOIN events e ON e.id = r.event_id WHERE r.id = ?",
                             (reg_id,)).fetchone()
            if reg is None:
                abort(404)
            if reg["user_id"] != g.user["id"] and g.user["role"] != "admin":
                abort(403)
            if reg["status"] == "cancelled":
                message = ("error", "This registration is already cancelled.")
            else:
                db.execute("UPDATE registrations SET status = 'cancelled', "
                           "checked_in_at = NULL WHERE id = ?", (reg_id,))
                log("registration_cancelled" if reg["status"] == "confirmed"
                    else "waitlist_left", reg["title"])
                promote_waitlist(reg["event_id"])
                message = ("success", "Registration cancelled."
                           if reg["status"] == "confirmed"
                           else "You left the waiting list.")
        flash(*reversed(message))
        if g.user["role"] == "admin" and reg["user_id"] != g.user["id"]:
            return redirect(url_for("admin_event_registrations",
                                    event_id=reg["event_id"]))
        return redirect(url_for("my_registrations"))

    @app.route("/my-registrations")
    @login_required
    def my_registrations():
        regs = get_db().execute(
            "SELECT r.*, e.title, e.venue, e.event_date, e.event_time, e.category "
            "FROM registrations r JOIN events e ON e.id = r.event_id "
            "WHERE r.user_id = ? ORDER BY e.event_date, e.event_time",
            (g.user["id"],)).fetchall()
        positions = {r["id"]: waitlist_position(r) for r in regs
                     if r["status"] == "waitlisted"}
        return render_template("my_registrations.html", regs=regs, positions=positions)

    @app.route("/ticket/<code>")
    @login_required
    def ticket(code):
        t = get_db().execute(TICKET_SELECT + " WHERE r.ticket_code = ?",
                             (code,)).fetchone()
        if t is None:
            abort(404)
        if t["user_id"] != g.user["id"] and g.user["role"] != "admin":
            abort(403)
        qr = None
        if t["status"] == "confirmed":
            # The QR code holds the staff check-in link for this ticket. The
            # link is admin-only, so scanning it gives attendees no access.
            link = url_for("admin_checkin_ticket", code=code, _external=True)
            qr = Markup(segno.make(link, error="m").svg_inline(scale=4, border=2))
        return render_template("ticket.html", t=t, qr=qr)

    # ------------------------------------------------- module 4: admin side
    def read_event_form():
        f = request.form
        data = {k: f.get(k, "").strip() for k in
                ("title", "description", "category", "venue",
                 "event_date", "event_time", "capacity")}
        errors = []
        if not 3 <= len(data["title"]) <= 120:
            errors.append("Title must be 3 to 120 characters.")
        if not 1 <= len(data["description"]) <= 2000:
            errors.append("Description is required (up to 2000 characters).")
        if data["category"] not in CATEGORIES:
            errors.append("Choose a valid category.")
        if not 1 <= len(data["venue"]) <= 120:
            errors.append("Venue is required (up to 120 characters).")
        try:
            datetime.strptime(data["event_date"], "%Y-%m-%d")
        except ValueError:
            errors.append("Enter a valid date.")
        try:
            datetime.strptime(data["event_time"], "%H:%M")
        except ValueError:
            errors.append("Enter a valid time.")
        try:
            cap = int(data["capacity"])
            if not 1 <= cap <= 100000:
                raise ValueError
        except ValueError:
            errors.append("Capacity must be a whole number from 1 to 100000.")
        return data, errors

    @app.route("/admin")
    @admin_required
    def admin_dashboard():
        db = get_db()

        def one(sql, *params):
            return db.execute(sql, params).fetchone()[0]

        stats = {
            "events": one("SELECT COUNT(*) FROM events"),
            "upcoming": one("SELECT COUNT(*) FROM events WHERE event_date >= ?", today()),
            "users": one("SELECT COUNT(*) FROM users WHERE role = 'user'"),
            "confirmed": one("SELECT COUNT(*) FROM registrations WHERE status = 'confirmed'"),
            "waiting": one("SELECT COUNT(*) FROM registrations WHERE status = 'waitlisted'"),
            "cancelled": one("SELECT COUNT(*) FROM registrations WHERE status = 'cancelled'"),
            "checked_in": one("SELECT COUNT(*) FROM registrations "
                              "WHERE checked_in_at IS NOT NULL"),
            "seats": one("SELECT COALESCE(SUM(capacity), 0) FROM events"),
        }
        events = db.execute(EVENT_SELECT + " ORDER BY e.event_date, e.id").fetchall()
        by_category = db.execute(
            "SELECT e.category, COUNT(r.id) AS n FROM events e "
            "LEFT JOIN registrations r ON r.event_id = e.id AND r.status = 'confirmed' "
            "GROUP BY e.category ORDER BY n DESC, e.category").fetchall()
        recent = db.execute(
            "SELECT l.*, u.name FROM activity_log l LEFT JOIN users u ON u.id = l.user_id "
            "ORDER BY l.id DESC LIMIT 8").fetchall()
        return render_template("admin_dashboard.html", stats=stats, events=events,
                               by_category=by_category, recent=recent,
                               max_cat=max([c["n"] for c in by_category] + [1]))

    @app.route("/admin/events/new", methods=["GET", "POST"])
    @admin_required
    def admin_event_new():
        if request.method == "POST":
            data, errors = read_event_form()
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("admin_event_form.html", event=data,
                                       editing=False), 400
            get_db().execute(
                "INSERT INTO events (title, description, category, venue, event_date, "
                "event_time, capacity, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (data["title"], data["description"], data["category"], data["venue"],
                 data["event_date"], data["event_time"], int(data["capacity"]),
                 g.user["id"], now()))
            log("event_created", data["title"])
            flash("Event created.", "success")
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_event_form.html", event={}, editing=False)

    @app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_event_edit(event_id):
        event = get_event_or_404(event_id)
        if request.method == "POST":
            data, errors = read_event_form()
            if not errors and int(data["capacity"]) < event["booked"]:
                errors.append(f"Capacity cannot be below the {event['booked']} "
                              "seats already booked.")
            if errors:
                for e in errors:
                    flash(e, "error")
                data["id"] = event_id
                return render_template("admin_event_form.html", event=data,
                                       editing=True), 400
            with write_tx() as db:
                db.execute(
                    "UPDATE events SET title = ?, description = ?, category = ?, "
                    "venue = ?, event_date = ?, event_time = ?, capacity = ? WHERE id = ?",
                    (data["title"], data["description"], data["category"], data["venue"],
                     data["event_date"], data["event_time"], int(data["capacity"]),
                     event_id))
                log("event_updated", data["title"])
                promoted = promote_waitlist(event_id)   # extra seats go to the queue
            flash("Event updated." + (f" {promoted} waiting-list "
                                      f"{'person was' if promoted == 1 else 'people were'} "
                                      "given a seat." if promoted else ""), "success")
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_event_form.html", event=event, editing=True)

    @app.route("/admin/events/<int:event_id>/delete", methods=["POST"])
    @admin_required
    def admin_event_delete(event_id):
        event = get_event_or_404(event_id)
        get_db().execute("DELETE FROM events WHERE id = ?", (event_id,))
        log("event_deleted", event["title"])
        flash("Event deleted along with its registrations.", "success")
        return redirect(url_for("admin_dashboard"))

    def event_registrations(event_id):
        return get_db().execute(
            "SELECT r.*, u.name, u.email FROM registrations r "
            "JOIN users u ON u.id = r.user_id WHERE r.event_id = ? "
            "ORDER BY CASE r.status WHEN 'confirmed' THEN 0 WHEN 'waitlisted' THEN 1 "
            "ELSE 2 END, r.registered_at, r.id", (event_id,)).fetchall()

    @app.route("/admin/events/<int:event_id>/registrations")
    @admin_required
    def admin_event_registrations(event_id):
        return render_template("admin_registrations.html",
                               event=get_event_or_404(event_id),
                               regs=event_registrations(event_id))

    @app.route("/admin/events/<int:event_id>/export.csv")
    @admin_required
    def admin_event_export(event_id):
        event = get_event_or_404(event_id)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Ticket", "Name", "Email", "Status", "Registered at",
                         "Checked in at"])
        for r in event_registrations(event_id):
            writer.writerow([csv_safe(r[k]) for k in
                             ("ticket_code", "name", "email", "status",
                              "registered_at", "checked_in_at")])
        log("registrations_exported", event["title"])
        return Response(out.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition":
                f"attachment; filename=event_{event_id}_registrations.csv"})

    @app.route("/admin/checkin")
    @admin_required
    def admin_checkin():
        code = request.args.get("code", "").strip().upper()
        if code:
            return redirect(url_for("admin_checkin_ticket", code=code))
        return render_template("admin_checkin.html", t=None)

    @app.route("/admin/checkin/<code>", methods=["GET", "POST"])
    @admin_required
    def admin_checkin_ticket(code):
        db = get_db()
        t = db.execute(TICKET_SELECT + " WHERE r.ticket_code = ?", (code,)).fetchone()
        if t is None:
            flash("No ticket found with that code.", "error")
            return redirect(url_for("admin_checkin"))
        if request.method == "POST":
            if t["status"] != "confirmed":
                flash("Only a confirmed registration can be checked in.", "error")
            elif t["checked_in_at"]:
                flash(f"Already checked in at {t['checked_in_at']}.", "error")
            else:
                db.execute("UPDATE registrations SET checked_in_at = ? WHERE id = ?",
                           (now(), t["id"]))
                log("attendee_checked_in", f"{t['title']}: {t['email']}")
                flash(f"{t['name']} is checked in.", "success")
            return redirect(url_for("admin_checkin_ticket", code=code))
        return render_template("admin_checkin.html", t=t)

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = get_db().execute(
            "SELECT u.*, (SELECT COUNT(*) FROM registrations r WHERE r.user_id = u.id "
            "AND r.status = 'confirmed') AS regs FROM users u ORDER BY u.id").fetchall()
        return render_template("admin_users.html", users=users)

    def get_other_user_or_error(user_id):
        user = get_db().execute("SELECT * FROM users WHERE id = ?",
                                (user_id,)).fetchone()
        if user is None:
            abort(404)
        if user["id"] == g.user["id"]:
            # An admin cannot demote or delete themselves, which also
            # guarantees the system always keeps at least one admin.
            flash("You cannot change your own account here.", "error")
            return None
        return user

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_user_role(user_id):
        user = get_other_user_or_error(user_id)
        if user:
            role = "user" if user["role"] == "admin" else "admin"
            get_db().execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            log("role_changed", f"{user['email']} -> {role}")
            flash(f"{user['name']} is now {'an admin' if role == 'admin' else 'a user'}.",
                  "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_user_delete(user_id):
        user = get_other_user_or_error(user_id)
        if user:
            with write_tx() as db:
                freed = [r["event_id"] for r in db.execute(
                    "SELECT event_id FROM registrations WHERE user_id = ? "
                    "AND status = 'confirmed'", (user_id,))]
                db.execute("UPDATE events SET created_by = NULL WHERE created_by = ?",
                           (user_id,))
                db.execute("DELETE FROM users WHERE id = ?", (user_id,))
                log("user_deleted", user["email"])
                for event_id in freed:
                    promote_waitlist(event_id)
            flash(f"{user['name']} and their registrations were deleted.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/logs")
    @admin_required
    def admin_logs():
        action = request.args.get("action", "")
        sql, params = ("SELECT l.*, u.name FROM activity_log l "
                       "LEFT JOIN users u ON u.id = l.user_id"), []
        if action:
            sql += " WHERE l.action = ?"
            params.append(action)
        logs, pager = paginate(sql + " ORDER BY l.id DESC", params, LOGS_PER_PAGE)
        actions = [r[0] for r in get_db().execute(
            "SELECT DISTINCT action FROM activity_log ORDER BY action")]
        return render_template("admin_logs.html", logs=logs, pager=pager,
                               actions=actions, action=action)

    @app.route("/admin/backup")
    @admin_required
    def admin_backup():
        """Consistent online snapshot of the database (backup concept)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            target = sqlite3.connect(path)
            get_db().backup(target)
            target.close()
            with open(path, "rb") as fh:
                data = fh.read()
        finally:
            os.remove(path)
        log("database_backup", f"{len(data)} bytes")
        name = "events_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db"
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                         mimetype="application/octet-stream")

    # ------------------------------------- cloud service endpoints (JSON)
    @app.route("/health")
    def health():
        """Health probe used by load balancers / container platforms."""
        try:
            get_db().execute("SELECT 1 FROM users LIMIT 1")
            return jsonify(status="ok", database="up", time=now())
        except sqlite3.Error:
            return jsonify(status="error", database="down"), 503

    @app.route("/api/events")
    def api_events():
        rows = get_db().execute(EVENT_SELECT + " WHERE e.event_date >= ? "
                                "ORDER BY e.event_date, e.event_time, e.id",
                                (today(),)).fetchall()
        return jsonify([{
            "id": r["id"], "title": r["title"], "category": r["category"],
            "venue": r["venue"], "date": r["event_date"], "time": r["event_time"],
            "capacity": r["capacity"], "seats_left": r["capacity"] - r["booked"],
            "waiting_list": r["waiting"],
        } for r in rows])

    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        messages = {400: "The request could not be understood",
                    403: "You do not have access to this page",
                    404: "Page not found",
                    405: "That action is not allowed here",
                    413: "The submitted data is too large"}
        return render_template("error.html", code=err.code,
                               message=messages.get(err.code, err.name)), err.code

    @app.errorhandler(500)
    def handle_server_error(_err):
        return render_template("error.html", code=500,
                               message="Something went wrong on our side"), 500

    with app.app_context():
        init_db()
    return app


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve     # production-grade server, all platforms
    except ImportError:
        application.run(host=host, port=port)
    else:
        proxy = {}
        if os.environ.get("TRUST_PROXY") == "1":
            # Behind a cloud load balancer the visitor's real address and the
            # https scheme arrive in X-Forwarded-* headers. Trust exactly one
            # proxy hop; without this setting those headers are discarded, so
            # nobody can fake their address to dodge the login lockout.
            proxy = dict(trusted_proxy="*", trusted_proxy_count=1,
                         trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto",
                                                "x-forwarded-host"})
        print(f"CloudEvents is running at http://{host}:{port}  (Ctrl+C to stop)",
              flush=True)
        serve(application, host=host, port=port, threads=8, **proxy)
