# Cloud-Based Event Management and Registration System

Mini-project for **23CSC205 – Cloud Computing Laboratory** (B.Tech III Year I Semester, CSE Cyber Security).
Student: **Rupa Sree Sompalli** · Roll No. **24691A37D7**

A Flask web application where users browse events, register, join a waiting list when an
event is full, and receive a QR e-ticket, while administrators manage events and users,
check attendees in, and monitor the service from a dashboard.

## Modules

| # | Module | What it does |
|---|--------|--------------|
| 1 | Authentication and access control | Sign up, log in, change email and password, hashed passwords, user/admin roles, login lockout |
| 2 | Event management | Admin create / edit / delete events; public search, filters and pagination |
| 3 | Registration and ticketing | Seat-limited booking, automatic waiting list, cancel and re-book, QR e-ticket, staff check-in |
| 4 | Admin analytics and monitoring | Dashboard, user management, activity log, CSV export, database backup |

Service endpoints: `GET /health` (health probe) and `GET /api/events` (JSON REST API).

The interface is built on a small CSS design system (`static/style.css`): colour-coded
event categories, light and dark themes that follow the device setting, a responsive
layout from phones to desktops, and visible keyboard focus throughout. It uses no
external fonts or libraries.

## Run locally (Windows)

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python app.py
```

Open <http://localhost:5000>. The database is created with sample data on first start.

### Sample accounts (synthetic data)

| Role  | Email             | Password   |
|-------|-------------------|------------|
| Admin | admin@example.com | admin123   |
| User  | asha@example.com  | student123 |
| User  | kiran@example.com | student123 |

To reset the data, stop the app and delete `instance/events.db`.

## Run the test cases

```bash
.venv\Scripts\python -m pytest -v
```

43 automated test cases cover all four modules and the security measures.

## Security

| Threat | Defence |
|--------|---------|
| Stolen password database | Only salted hashes are stored |
| Password guessing | Lockout for 5 minutes after 5 failures per account and IP address |
| Weak passwords | 8+ characters with a letter and a number; default admin password is flagged at login |
| SQL injection | Every query uses bound parameters |
| Cross-site scripting | Auto-escaped templates and a Content-Security-Policy with no inline scripts |
| Cross-site request forgery | Token in every form, checked on every POST; SameSite cookie |
| Session theft | HttpOnly cookie, Secure on HTTPS, new session at login, 8-hour expiry |
| Clickjacking | Framing is denied |
| Broken access control | Role check on every admin route; users reach only their own bookings |
| Open redirect | Login returns only to same-site paths |
| CSV formula injection | Exported cells starting with `= + - @` are neutralised |
| Last-seat race condition | Seat check and booking run in one locked transaction |

## Configuration

Everything is set through environment variables, so the same code runs anywhere.

| Variable | Purpose | Default |
|----------|---------|---------|
| `HOST` | Address to listen on (`0.0.0.0` in a container or on a cloud host) | `127.0.0.1` |
| `PORT` | Port to listen on | `5000` |
| `DATABASE_PATH` | Location of the SQLite file (point at a persistent disk) | `instance/events.db` |
| `SECRET_KEY` | Session signing key | generated once and saved next to the database |
| `COOKIE_SECURE` | `1` on HTTPS hosting so the session cookie is never sent over plain HTTP | off |
| `TRUST_PROXY` | `1` behind a cloud load balancer, to read the visitor's real address | off |

## Deploy to the cloud

**Render (free plan)** – push this folder to a GitHub repository, then in Render choose
*New → Blueprint* and pick the repository. `render.yaml` sets the build and start
commands, the health check and the security settings. Stay on the free plan: do not add
paid services without faculty approval. The free plan has no persistent disk, so data
added after deployment is reset when the service restarts (the sample data comes back).

**Docker**

```bash
docker build -t cloudevents .
```

```bash
docker run -p 8000:8000 -v cloudevents-data:/data cloudevents
```

Status: **live at <https://cloudevents.onrender.com>** on Render's free plan, deployed
from the GitHub repository with `render.yaml`. Pushing to the `main` branch redeploys it
automatically. The Docker image has not been built, because Docker is not installed on
the development machine.

## Project layout

```
app.py                  application (routes, database, seed data)
templates/              Jinja2 pages
static/                 style.css (design system), app.js, favicon.svg
tests/test_app.py       43 automated test cases
Dockerfile, Procfile,
render.yaml             cloud deployment
docs/Project_Report.docx  report for submission (with screenshots)
docs/Project_Report.md    same report as Markdown
docs/Viva_Guide.md        code walkthrough and likely viva questions
docs/architecture.png     architecture diagram
docs/screenshots/         screenshots used in the report
```
