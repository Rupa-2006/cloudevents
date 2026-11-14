"""Automated test cases. Run with:  python -m pytest -v

Seeded sample data used by the tests:
  users   1 admin, 2 Asha, 3 Kiran
  events  1 Cloud Computing Workshop (60 seats, Asha + Kiran booked)
          2 Capture The Flag (Asha booked)      3 Hackathon (empty)
          4 Guest Lecture (Kiran booked)        5 Cultural Fest (empty)
          6 Cricket (2 seats, Asha booked)      7-10 more upcoming events
          11 Freshers Orientation (already over)
  registrations  1 Asha/event 1, 2 Asha/event 2, 3 Kiran/event 1,
                 4 Kiran/event 4, 5 Asha/event 6
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app, csv_safe, password_problem  # noqa: E402

ADMIN = ("admin@example.com", "admin123")
USER = ("asha@example.com", "student123")
USER2 = ("kiran@example.com", "student123")
LATE = ("late@example.com", "secret12")


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test",
                       "CSRF_ENABLED": False,
                       "DATABASE": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def query(app):
    def run(sql, *params):
        con = sqlite3.connect(app.config["DATABASE"])
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    return run


def login(client, creds):
    return client.post("/login", data={"email": creds[0], "password": creds[1]})


def logout(client):
    return client.post("/logout")


def signup(client, creds=LATE, name="Late User"):
    return client.post("/register", data={"name": name, "email": creds[0],
                                          "password": creds[1], "confirm": creds[1]})


def new_event(**over):
    data = {"title": "Test Event", "description": "A test", "category": "Technical",
            "venue": "Lab 1", "event_date": "2099-01-01", "event_time": "10:00",
            "capacity": "5"}
    data.update(over)
    return data


def fill_cricket_and_waitlist_late_user(client):
    """Kiran takes the last cricket seat, then a new user joins the waiting list."""
    login(client, USER2)
    client.post("/events/6/register")
    logout(client)
    signup(client)
    login(client, LATE)
    response = client.post("/events/6/register", follow_redirects=True)
    logout(client)
    return response


# ---- Module 1: authentication and access control -------------------------
def test_tc01_home_page_lists_seeded_events(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Cloud Computing Workshop" in r.data


def test_tc02_signup_then_login(client):
    assert signup(client).status_code == 302
    assert login(client, LATE).status_code == 302


def test_tc03_signup_validation(client):
    r = client.post("/register", data={"name": "X", "email": "not-an-email",
                                       "password": "123", "confirm": "456"})
    assert r.status_code == 400
    assert b"valid email" in r.data and b"at least 8" in r.data
    assert b"do not match" in r.data


def test_tc04_duplicate_email_rejected(client):
    r = signup(client, (USER[0], "secret12"), "Asha")
    assert r.status_code == 400 and b"already exists" in r.data


def test_tc05_wrong_password_rejected(client):
    r = login(client, (USER[0], "wrong"))
    assert r.status_code == 401 and b"Invalid email or password" in r.data


def test_tc06_admin_pages_blocked_for_guests_and_users(client):
    assert client.get("/admin").status_code == 302          # guest -> login page
    login(client, USER)
    for path in ("/admin", "/admin/users", "/admin/logs", "/admin/backup",
                 "/admin/checkin", "/admin/events/new", "/admin/events/1/export.csv"):
        assert client.get(path).status_code == 403, path
    assert client.post("/admin/events/1/delete").status_code == 403
    assert client.post("/admin/users/3/role").status_code == 403
    assert client.post("/admin/users/3/delete").status_code == 403


def test_tc07_passwords_are_stored_hashed(query):
    stored = query("SELECT password_hash FROM users WHERE email = ?", ADMIN[0])[0][0]
    assert "admin123" not in stored


# ---- Module 2: event management ------------------------------------------
def test_tc08_search_and_category_filter(client):
    r = client.get("/?q=hackathon")
    assert b"Hackathon" in r.data and b"Cultural Fest" not in r.data
    r = client.get("/?category=Sports")
    assert b"Cricket" in r.data and b"Hackathon" not in r.data


def test_tc09_admin_creates_updates_deletes_event(client):
    login(client, ADMIN)
    assert client.post("/admin/events/new", data=new_event()).status_code == 302
    assert b"Test Event" in client.get("/?q=Test+Event").data
    assert client.post("/admin/events/12/edit",
                       data=new_event(title="Renamed Event")).status_code == 302
    assert b"Renamed Event" in client.get("/events/12").data
    assert client.post("/admin/events/12/delete").status_code == 302
    assert client.get("/events/12").status_code == 404


def test_tc10_event_form_validation(client):
    login(client, ADMIN)
    r = client.post("/admin/events/new",
                    data=new_event(title="", capacity="-3", event_date="bad"))
    assert r.status_code == 400
    assert b"Title must be" in r.data and b"Capacity must be" in r.data


def test_tc11_capacity_cannot_drop_below_bookings(client):
    login(client, ADMIN)   # event 1 is seeded with 2 confirmed bookings
    r = client.post("/admin/events/1/edit", data=new_event(capacity="1"))
    assert r.status_code == 400 and b"already booked" in r.data


# ---- Module 3: registration and ticketing --------------------------------
def test_tc12_register_view_ticket_and_reject_duplicate(client, query):
    login(client, USER)
    r = client.post("/events/3/register", follow_redirects=True)
    assert b"Registration confirmed" in r.data and b"EVT-" in r.data
    r = client.post("/events/3/register", follow_redirects=True)
    assert b"already registered" in r.data
    code = query("SELECT ticket_code FROM registrations WHERE user_id = 2 "
                 "AND event_id = 3")[0][0]
    assert client.get(f"/ticket/{code}").status_code == 200


def test_tc13_registration_requires_login(client):
    r = client.post("/events/3/register")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_tc14_full_event_puts_user_on_waiting_list(client, query):
    r = fill_cricket_and_waitlist_late_user(client)
    assert b"waiting list at position 1" in r.data
    assert query("SELECT status FROM registrations WHERE event_id = 6 "
                 "ORDER BY id") == [("confirmed",), ("confirmed",), ("waitlisted",)]


def test_tc15_cancel_frees_seat_and_allows_rebooking(client):
    login(client, USER)   # registration id 1 = Asha, event 1
    r = client.post("/registrations/1/cancel", follow_redirects=True)
    assert b"Registration cancelled" in r.data
    assert b"1 booked of 60" in client.get("/events/1").data
    r = client.post("/events/1/register", follow_redirects=True)
    assert b"Registration confirmed" in r.data


def test_tc16_user_cannot_cancel_or_view_others_booking(client, query):
    login(client, USER2)
    assert client.post("/registrations/1/cancel").status_code == 403
    code = query("SELECT ticket_code FROM registrations WHERE id = 1")[0][0]
    assert client.get(f"/ticket/{code}").status_code == 403


# ---- Module 4: admin analytics, monitoring, backup, API ------------------
def test_tc17_dashboard_export_logs_and_backup(client):
    login(client, ADMIN)
    assert b"Confirmed bookings" in client.get("/admin").data
    r = client.get("/admin/events/1/export.csv")
    assert r.mimetype == "text/csv" and b"asha@example.com" in r.data
    assert b"Checked in at" in r.data
    assert b"login_success" in client.get("/admin/logs").data
    r = client.get("/admin/backup")
    assert r.data.startswith(b"SQLite format 3")


def test_tc18_health_and_json_api(client):
    assert client.get("/health").get_json()["status"] == "ok"
    events = client.get("/api/events").get_json()
    assert len(events) == 10                       # the past event is excluded
    assert {"seats_left", "waiting_list"} <= set(events[0])


def test_tc19_csrf_token_required_when_enabled(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "test",
                      "DATABASE": str(tmp_path / "csrf.db")})
    r = app.test_client().post("/login", data={"email": ADMIN[0],
                                               "password": ADMIN[1]})
    assert r.status_code == 400


# ---- Security hardening ---------------------------------------------------
def test_tc20_login_blocked_after_five_wrong_passwords(client):
    for _ in range(5):
        assert login(client, (USER[0], "wrong")).status_code == 401
    r = login(client, USER)                      # correct password, but locked
    assert r.status_code == 429 and b"Too many failed attempts" in r.data
    assert login(client, USER2).status_code == 302   # other accounts unaffected


def test_tc21_successful_login_resets_failure_count(client):
    for _ in range(4):
        login(client, (USER[0], "wrong"))
    assert login(client, USER).status_code == 302
    logout(client)
    for _ in range(4):
        login(client, (USER[0], "wrong"))
    assert login(client, USER).status_code == 302


def test_tc22_change_password(client):
    assert client.get("/account/password").status_code == 302   # guest -> login
    login(client, USER)
    r = client.post("/account/password", data={"current": "wrong",
                                               "new": "newpass1", "confirm": "newpass1"})
    assert r.status_code == 400 and b"Current password is incorrect" in r.data
    r = client.post("/account/password", data={"current": USER[1],
                                               "new": "newpass1", "confirm": "other"})
    assert r.status_code == 400 and b"do not match" in r.data
    r = client.post("/account/password", data={"current": USER[1],
                                               "new": "newpass1", "confirm": "newpass1"})
    assert r.status_code == 302
    logout(client)
    assert login(client, USER).status_code == 401            # old password fails
    assert login(client, (USER[0], "newpass1")).status_code == 302


def test_tc23_default_admin_password_warning(client):
    r = client.post("/login", data={"email": ADMIN[0], "password": ADMIN[1]},
                    follow_redirects=True)
    assert b"default sample admin password" in r.data


def test_tc24_session_cookie_flags(client, tmp_path):
    cookie = login(client, USER).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    secure_app = create_app({"TESTING": True, "SECRET_KEY": "test",
                             "CSRF_ENABLED": False, "SESSION_COOKIE_SECURE": True,
                             "DATABASE": str(tmp_path / "secure.db")})
    cookie = login(secure_app.test_client(), USER).headers["Set-Cookie"]
    assert "Secure" in cookie


def test_tc25_weak_passwords_rejected(client):
    assert password_problem("short1") and password_problem("lettersonly")
    assert password_problem("12345678") and password_problem("goodpass1") is None
    r = signup(client, ("weak@example.com", "password"))
    assert r.status_code == 400 and b"one letter and one number" in r.data


def test_tc26_login_redirect_stays_on_this_site(client):
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example"):
        r = client.post("/login?next=" + bad, data={"email": USER[0],
                                                    "password": USER[1]})
        assert "evil.example" not in r.headers["Location"], bad
        logout(client)
    r = client.post("/login?next=/my-registrations",
                    data={"email": USER[0], "password": USER[1]})
    assert r.headers["Location"].endswith("/my-registrations")


def test_tc27_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert b"onclick" not in r.data and b"style=" not in r.data   # nothing inline
    login(client, USER)
    assert client.get("/my-registrations").headers["Cache-Control"] == "no-store"


def test_tc28_csv_export_neutralises_formulas(client):
    assert csv_safe("=1+1") == "'=1+1" and csv_safe("Asha") == "Asha"
    signup(client, LATE, "=HYPERLINK(\"http://evil.example\")")
    login(client, LATE)
    client.post("/events/3/register")
    logout(client)
    login(client, ADMIN)
    data = client.get("/admin/events/3/export.csv").data.decode()
    assert "'=HYPERLINK" in data and "\n=HYPERLINK" not in data


def test_tc29_html_in_names_is_escaped(client):
    signup(client, LATE, "<script>alert(1)</script>")
    login(client, LATE)
    page = client.get("/").data
    assert b"<script>alert(1)</script>" not in page
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ---- Waiting list ---------------------------------------------------------
def test_tc30_cancellation_promotes_waiting_list(client, query):
    fill_cricket_and_waitlist_late_user(client)
    login(client, USER)
    client.post("/registrations/5/cancel")           # Asha gives up her seat
    assert query("SELECT status FROM registrations WHERE event_id = 6 "
                 "ORDER BY id") == [("cancelled",), ("confirmed",), ("confirmed",)]
    assert query("SELECT COUNT(*) FROM activity_log "
                 "WHERE action = 'waitlist_promoted'")[0][0] == 1


def test_tc31_extra_capacity_promotes_waiting_list(client, query):
    fill_cricket_and_waitlist_late_user(client)
    login(client, ADMIN)
    r = client.post("/admin/events/6/edit", follow_redirects=True, data=new_event(
        title="Inter-Department Cricket", category="Sports", capacity="3"))
    assert b"1 waiting-list person was given a seat" in r.data
    assert query("SELECT COUNT(*) FROM registrations WHERE event_id = 6 "
                 "AND status = 'confirmed'")[0][0] == 3


def test_tc32_leave_waiting_list_and_position_shown(client):
    fill_cricket_and_waitlist_late_user(client)
    login(client, LATE)
    assert b"number <strong>1</strong> on the waiting list" in \
        client.get("/events/6").data
    assert b"waitlisted #1" in client.get("/my-registrations").data
    reg_id = 7     # 6 is Kiran's cricket seat, 7 is the late user's waiting-list entry
    r = client.post(f"/registrations/{reg_id}/cancel", follow_redirects=True)
    assert b"You left the waiting list" in r.data
    assert b"already on the waiting list" not in r.data


def test_tc33_cannot_register_for_past_event(client):
    login(client, USER)
    r = client.post("/events/11/register", follow_redirects=True)
    assert b"this event is over" in r.data
    assert b"Freshers Orientation" in client.get("/?when=past").data
    assert b"Freshers Orientation" not in client.get("/").data


# ---- QR ticket and check-in ----------------------------------------------
def test_tc34_ticket_qr_only_for_valid_tickets(client, query):
    code = query("SELECT ticket_code FROM registrations WHERE id = 1")[0][0]
    login(client, USER)
    page = client.get(f"/ticket/{code}").data
    assert b'class="qr"' in page and b"<svg" in page
    client.post("/registrations/1/cancel")
    page = client.get(f"/ticket/{code}").data
    assert b'class="qr"' not in page and b"not valid for entry" in page


def test_tc35_admin_checks_in_ticket_once(client, query):
    code = query("SELECT ticket_code FROM registrations WHERE id = 1")[0][0]
    login(client, ADMIN)
    r = client.get("/admin/checkin?code=" + code.lower())
    assert r.headers["Location"].endswith("/admin/checkin/" + code)
    r = client.post(f"/admin/checkin/{code}", follow_redirects=True)
    assert b"Asha Verma is checked in" in r.data
    r = client.post(f"/admin/checkin/{code}", follow_redirects=True)
    assert b"Already checked in" in r.data
    r = client.get("/admin/checkin/EVT-00000000", follow_redirects=True)
    assert b"No ticket found" in r.data
    assert query("SELECT COUNT(*) FROM registrations "
                 "WHERE checked_in_at IS NOT NULL")[0][0] == 1


def test_tc36_cancelled_ticket_cannot_be_checked_in(client, query):
    code = query("SELECT ticket_code FROM registrations WHERE id = 1")[0][0]
    login(client, USER)
    client.post("/registrations/1/cancel")
    logout(client)
    login(client, ADMIN)
    r = client.post(f"/admin/checkin/{code}", follow_redirects=True)
    assert b"Only a confirmed registration" in r.data


# ---- User management, pagination, errors, migration ----------------------
def test_tc37_admin_promotes_demotes_and_deletes_users(client, query):
    login(client, ADMIN)
    r = client.post("/admin/users/3/role", follow_redirects=True)
    assert b"Kiran Rao is now an admin" in r.data
    client.post("/admin/users/3/role")
    assert query("SELECT role FROM users WHERE id = 3")[0][0] == "user"
    r = client.post("/admin/users/1/delete", follow_redirects=True)   # yourself
    assert b"cannot change your own account" in r.data
    r = client.post("/admin/users/2/delete", follow_redirects=True)
    assert b"Asha Verma and their registrations were deleted" in r.data
    assert query("SELECT COUNT(*) FROM registrations WHERE user_id = 2")[0][0] == 0
    assert client.post("/admin/users/999/role").status_code == 404


def test_tc38_deleting_user_promotes_waiting_list(client, query):
    fill_cricket_and_waitlist_late_user(client)
    login(client, ADMIN)
    client.post("/admin/users/2/delete")              # Asha held a cricket seat
    assert query("SELECT status FROM registrations WHERE event_id = 6 "
                 "ORDER BY id") == [("confirmed",), ("confirmed",)]


def test_tc39_pagination(client):
    first = client.get("/").data
    assert b"Page 1 of 2" in first and b"Project Expo" not in first
    assert b"Project Expo" in client.get("/?page=2").data
    assert b"Page 2 of 2" in client.get("/?page=999").data     # clamped
    assert client.get("/?page=abc").status_code == 200
    login(client, ADMIN)
    r = client.get("/admin/logs?action=login_success")
    assert r.status_code == 200 and b"login_success" in r.data


def test_tc40_friendly_error_pages(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and b"Page not found" in r.data
    r = client.get("/logout")
    assert r.status_code == 405 and b"not allowed here" in r.data
    assert client.get("/events/999").status_code == 404


def test_tc42_change_email(client, query):
    assert client.get("/account/email").status_code == 302          # guest -> login
    login(client, ADMIN)
    r = client.post("/account/email", data={"email": "new.admin@example.com",
                                            "password": "wrong"})
    assert r.status_code == 400 and b"Password is incorrect" in r.data
    r = client.post("/account/email", data={"email": "not-an-email",
                                            "password": ADMIN[1]})
    assert r.status_code == 400 and b"valid email" in r.data
    r = client.post("/account/email", data={"email": USER[0], "password": ADMIN[1]})
    assert r.status_code == 400 and b"already uses this email" in r.data
    r = client.post("/account/email", data={"email": " New.Admin@Example.com ",
                                            "password": ADMIN[1]})
    assert r.status_code == 302
    assert query("SELECT email, role FROM users WHERE id = 1") == \
        [("new.admin@example.com", "admin")]
    logout(client)
    assert login(client, ADMIN).status_code == 401                   # old email
    r = login(client, ("new.admin@example.com", ADMIN[1]))
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin")


def test_tc43_admin_tools_on_event_page(client):
    assert b"Admin tools" not in client.get("/events/1").data       # guest
    login(client, USER)
    assert b"Admin tools" not in client.get("/events/1").data       # student
    logout(client)
    login(client, ADMIN)
    page = client.get("/events/1").data
    assert b"Admin tools" in page
    assert b'href="/admin/events/1/edit"' in page
    assert b'href="/admin/events/1/registrations"' in page
    assert b'action="/admin/events/1/delete"' in page


def test_tc44_times_use_indian_time_zone():
    from datetime import datetime, timedelta, timezone
    import app as app_module
    assert str(app_module.APP_TZ) == "Asia/Kolkata"
    expected = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    shown = datetime.strptime(app_module.now(), "%Y-%m-%d %H:%M:%S")
    assert abs((shown - expected.replace(tzinfo=None)).total_seconds()) < 5
    assert app_module.today() == expected.date().isoformat()


def test_tc41_version_1_database_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            description TEXT NOT NULL, category TEXT NOT NULL, venue TEXT NOT NULL,
            event_date TEXT NOT NULL, event_time TEXT NOT NULL,
            capacity INTEGER NOT NULL, created_by INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE registrations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            ticket_code TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('confirmed', 'cancelled')),
            registered_at TEXT NOT NULL, UNIQUE (user_id, event_id));
        INSERT INTO users VALUES (1, 'Old', 'old@example.com', 'x', 'user', '2026-01-01');
        INSERT INTO events VALUES (1, 'Old event', 'd', 'Technical', 'v',
            '2099-01-01', '10:00', 5, 1, '2026-01-01');
        INSERT INTO registrations VALUES (7, 1, 1, 'EVT-OLD00001', 'confirmed',
            '2026-01-02');
    """)
    con.close()
    create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": path})
    con = sqlite3.connect(path)
    assert con.execute("SELECT id, ticket_code, status, checked_in_at "
                       "FROM registrations").fetchall() == \
        [(7, "EVT-OLD00001", "confirmed", None)]
    con.execute("UPDATE registrations SET status = 'waitlisted'")   # now allowed
    assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1   # not re-seeded
    con.close()
