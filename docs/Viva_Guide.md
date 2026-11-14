# Viva Guide

How the project works, where each thing lives in the code, and answers to the
questions an examiner is most likely to ask. Read `app.py` alongside this.

## 1. The project in one minute

"It is a web application for managing college events. Users sign up, browse events and
register. If an event is full they join a waiting list and get a seat automatically
when someone cancels. Every confirmed booking has an e-ticket with a QR code, which
staff scan to check the person in. Admins create events, manage users, and watch a
dashboard and an activity log. It is written in Python with Flask and SQLite, it is
configured only through environment variables, and it ships with a Dockerfile and a
Render blueprint so it can run on a cloud platform."

## 2. How a request travels

1. The browser sends a request to the **Waitress** server, which passes it to **Flask**.
2. `load_user_and_check_csrf` runs first: it reads the session cookie, loads the
   logged-in user, and for every POST checks the CSRF token.
3. Flask calls the matching **route function** (for example `register_for_event`).
4. The function reads or writes the **SQLite** database and records an entry in
   `activity_log`.
5. It returns a page built from a **Jinja2 template**, or a redirect.
6. `security_headers` adds the security headers to the response.

## 3. Where things are in `app.py`

| Part | What to look for |
|------|------------------|
| Top of file | `SCHEMA`, `REGISTRATIONS_TABLE`, `INDEXES` – the database design |
| `create_app` | Configuration from environment variables |
| `get_db`, `write_tx` | One connection per request; `write_tx` is the locked transaction |
| `init_db`, `migrate_registrations`, `seed` | Create tables, upgrade an old database, add sample data |
| `load_user_and_check_csrf`, `security_headers` | Runs before and after every request |
| `login_required`, `admin_required` | Decorators that enforce access control |
| `promote_waitlist` | Gives freed seats to the longest-waiting people |
| `register`, `login`, `change_password`, `logout` | Module 1 |
| `index`, `event_detail` | Module 2 (public side) |
| `register_for_event`, `cancel_registration`, `ticket` | Module 3 |
| `admin_*` functions | Module 4 and admin event CRUD |
| `health`, `api_events` | Cloud service endpoints |

Other files: `templates/` (pages), `static/style.css`, `static/app.js` (confirm
dialogs and the print button), `tests/test_app.py` (44 tests).

## 4. Likely questions and answers

### Cloud

**What makes this a cloud application?**
It is delivered as a service through the browser (SaaS). It keeps no state in the
server process: the session is in a signed cookie and the data is in a database on a
separate volume, so the server can be stopped, replaced or copied. All settings come
from environment variables. It has a health endpoint for a load balancer, a container
image definition, and a platform blueprint.

**Which service model is it?**
The application itself is SaaS for its users. It is designed to be deployed on a PaaS
such as Render, where the platform provides the servers, HTTPS and scaling.

**Is it deployed?**
Yes, at https://cloudevents.onrender.com on Render's free plan. The code is on GitHub
(Rupa-2006/cloudevents); Render reads `render.yaml`, installs the requirements and starts
the app. Every push to the `main` branch redeploys it automatically, which is continuous
deployment.

**Why does the live site take a while to open sometimes?**
The free plan puts the service to sleep after about 15 minutes without visitors, and
the first request wakes it up. For the same reason the free plan has no permanent disk,
so data resets to the sample data when the service restarts.

**Why SQLite and not a cloud database?**
It needs no server or paid service, which suits a mini-project and the "no paid
charges" rule. Its limit is one application instance. For several servers the same
SQL would move to a managed PostgreSQL service.

**What is the `/health` endpoint for?**
A load balancer or container platform calls it regularly. If it does not answer 200
the platform restarts the instance or stops sending it traffic.

**How would you scale it?**
Move the database to a managed service, then run several copies of the app behind a
load balancer. That works because the app holds no state in memory.

**What does the Dockerfile do?**
It starts from a small Python image, installs the dependencies, copies the code,
switches to a non-root user, declares the port and a health check, and starts the app.

**How is backup done?**
The admin can download a snapshot made with SQLite's online backup API, which gives a
consistent copy even while the app is in use. Restoring is replacing the file.

### Security

**How are passwords stored?**
As salted hashes made by Werkzeug's `generate_password_hash` (scrypt). The original
password cannot be read back; login re-hashes the entered password and compares.

**How do you stop brute-force login?**
Every failed login is logged with the account and IP address. Before checking a
password the app counts failures for that account from that address in the last five
minutes since the last success. At five it refuses with HTTP 429.

**Why count per account and IP, not just per account?**
Counting only per account would let an attacker lock a real user out on purpose.

**What is CSRF and how is it prevented?**
Another website tricking your browser into sending a request to this site while you
are logged in. Every form contains a random token stored in the session; a POST
without the right token is rejected. The session cookie is also `SameSite=Lax`.

**What is XSS and how is it prevented?**
Injecting script into a page other users see. Jinja2 escapes every value it prints,
and the Content-Security-Policy header tells the browser to run only scripts loaded
from this site, never inline ones.

**How is SQL injection prevented?**
Every query uses `?` placeholders with the values passed separately, so input is
never treated as SQL.

**What do the cookie flags do?**
`HttpOnly`: JavaScript cannot read the cookie. `SameSite=Lax`: other sites cannot send
it with their form posts. `Secure`: it is only sent over HTTPS (enabled with
`COOKIE_SECURE=1`).

**How is access control enforced?**
`login_required` and `admin_required` wrap the route functions. Ticket and cancel
routes also check that the booking belongs to the logged-in user.

**What is CSV injection?**
A cell starting with `=` is run as a formula by Excel. Since names come from users,
`csv_safe` puts a quote in front of such cells in the export.

**Could a user fake the QR code to get in?**
The QR code is only a link to the admin check-in page for that ticket. Only an admin
can open it, the ticket code is random, and a ticket can be checked in once.

### Design and code

**How do you stop two people taking the last seat?**
`write_tx` runs `BEGIN IMMEDIATE`, which takes the database write lock before the
seats are counted. The second request waits, then sees the event is full and goes on
the waiting list.

**How does the waiting list work?**
A registration has a status: confirmed, waitlisted or cancelled. `promote_waitlist`
runs after a cancellation, a capacity increase or a user deletion, and confirms
waitlisted rows in order of `registered_at` while seats remain.

**What is the migration for?**
The first version had no waiting list. SQLite cannot change a CHECK constraint, so on
start-up the app detects the old table, creates the new one, copies the rows and drops
the old one, all in one transaction.

**Why is there a UNIQUE constraint on (user_id, event_id)?**
So a user can never have two bookings for one event, even if the code had a bug.
Re-registering after a cancellation updates the same row.

**Why Waitress?**
Flask's built-in server is for development only. Waitress is a production WSGI server
that runs the same way on Windows and Linux.

**How did you test it?**
44 automated tests with pytest. Each test gets a fresh temporary database. They cover
normal use, validation errors, access control and each security measure. Run
`python -m pytest -v`.

**What are the limitations?**
One server because of SQLite; no email notifications or password reset by email; not
yet published to a public URL.

## 5. Things to try before the viva

- Run the app and follow the demonstration script in the report, start to finish.
- Run the tests and read three of them, so you can explain how one works.
- Change `EVENTS_PER_PAGE` in `app.py`, restart, and see the effect.
- Open the browser's developer tools (F12 → Network), load a page, and find the
  `Content-Security-Policy` and `Set-Cookie` headers.
- Enter a wrong password five times and find the `login_blocked` row in the activity log.
