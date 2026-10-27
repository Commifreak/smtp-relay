"""Web UI: login, setup wizard, mail history, SMTP transcripts, settings and diagnostics."""
import asyncio
import json
import logging
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import (MIN_PASSWORD_LENGTH, SESSION_COOKIE, SESSION_LIFETIME, Sessions, check_login, proxy_user,
                   set_credentials, verify_password)
from .config import DEFAULTS, LIST_KEYS, Settings, normalize_ip
from .db import Database
from .delivery import DeliveryQueue
from .relay import test_connection
from .relays import BOOL_FIELDS as RELAY_BOOL_FIELDS, FIELDS as RELAY_FIELDS, NEW_RELAY, Relays, as_settings
from .rewrite import message_parts

log = logging.getLogger("smtp-relay.web")

BASE = Path(__file__).parent
PAGE_SIZE = 100
STATUSES = ["relayed", "partial", "queued", "failed", "rejected", "relaying"]
STATUS_LABELS = {"relayed": "relayed", "partial": "partial", "queued": "queued",
                 "failed": "failed", "rejected": "rejected", "relaying": "relaying",
                 # statuses of the first release, still present in older histories
                 "deferred": "deferred", "received": "received"}
KIND_LABELS = {"inbound": "Inbound", "outbound": "Relay", "test": "Connection test"}
SETUP_STEPS = [("account", "Account"), ("relay", "Relay server"), ("senders", "Senders"), ("options", "Options")]
PROXY_KEYS = ("proxy_auth_enabled", "proxy_auth_header", "proxy_auth_trusted_networks", "proxy_auth_logout_url")


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def safe_next(url: str | None) -> str:
    """Only allow local redirect targets after login."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"


def fmt_ts(ts, with_ms=False):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    if with_ms:
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def create_app(db: Database, settings: Settings, queue: DeliveryQueue) -> FastAPI:
    app = FastAPI(title="smtp-relay", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    env = templates.env
    env.filters["ts"] = fmt_ts
    env.filters["size"] = fmt_size
    env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    env.globals.update(STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS, version=__version__,
                       queue_count=queue.queued_count)

    web_sessions = Sessions(db)
    relays = queue.relays

    @app.exception_handler(Redirect)
    async def handle_redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    def current_user(request: Request) -> tuple[str | None, str]:
        """(user name, source) where source is 'proxy' or 'local'."""
        peer = normalize_ip(request.client.host) if request.client else ""
        user = proxy_user(settings, peer, request.headers)
        if user:
            return user, "proxy"
        if web_sessions.valid(request.cookies.get(SESSION_COOKIE)):
            return settings.get("auth_username"), "local"
        return None, ""

    def check_origin(request: Request):
        if request.method == "POST":
            # Basic CSRF protection: POSTs must originate from this UI.
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "Invalid request origin")

    def login_required(request: Request):
        check_origin(request)
        user, source = current_user(request)
        if not user:
            if request.method == "GET":
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                raise Redirect("/login?" + urlencode({"next": target}))
            raise HTTPException(401, "Login required")
        request.state.user, request.state.auth_source = user, source

    def auth(request: Request):
        login_required(request)
        if not settings.get("setup_completed"):
            raise Redirect("/setup")

    def setup_auth(request: Request):
        login_required(request)
        if settings.get("setup_completed"):
            raise Redirect("/")

    def is_https(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"

    def render(request, name, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if current_user(request)[0]:
            raise Redirect(safe_next(next))
        return render(request, "login.html", bare=True, next=safe_next(next), error=None, username="",
                      initial=bool(settings.get("auth_initial_password")))

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        check_origin(request)
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        target = safe_next(str(form.get("next", "/")))
        if not check_login(settings, username, password):
            peer = request.client.host if request.client else "?"
            log.warning("Failed login for user %r from %s", username, peer)
            await asyncio.sleep(1)  # slow down password guessing
            return render(request, "login.html", bare=True, next=target, error="Invalid username or password",
                          username=username, initial=bool(settings.get("auth_initial_password")))
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(SESSION_COOKIE, web_sessions.create(), max_age=SESSION_LIFETIME, httponly=True,
                            samesite="lax", secure=is_https(request), path="/")
        return response

    @app.post("/logout")
    def logout(request: Request):
        check_origin(request)
        _, source = current_user(request)
        web_sessions.delete(request.cookies.get(SESSION_COOKIE))
        target = "/login"
        if source == "proxy" and settings.get("proxy_auth_logout_url"):
            target = settings.get("proxy_auth_logout_url")
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    def render_setup(request, step, **ctx):
        names = [k for k, _ in SETUP_STEPS]
        ctx.setdefault("errors", [])
        return render(request, "setup.html", bare=True, step=step, steps=SETUP_STEPS, index=names.index(step),
                      s={**settings.snapshot(), **ctx.pop("values", {})},
                      r={**(relays.first() or NEW_RELAY), **ctx.pop("relay_values", {})}, min_password=MIN_PASSWORD_LENGTH,
                      initial=bool(settings.get("auth_initial_password")), **ctx)

    @app.get("/setup", dependencies=[Depends(setup_auth)])
    def setup_start():
        raise Redirect("/setup/account")

    @app.get("/setup/{step}", response_class=HTMLResponse, dependencies=[Depends(setup_auth)])
    def setup_page(request: Request, step: str):
        if step not in dict(SETUP_STEPS):
            raise Redirect("/setup/account")
        return render_setup(request, step)

    @app.post("/setup/account", response_class=HTMLResponse, dependencies=[Depends(setup_auth)])
    async def setup_account(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))
        password, password2 = str(form.get("password", "")), str(form.get("password2", ""))
        values = {k: form.get(k, "") for k in PROXY_KEYS}
        values["proxy_auth_enabled"] = "proxy_auth_enabled" in form
        errors = []
        initial = settings.get("auth_initial_password")
        if password or initial:
            if password != password2:
                errors.append("The passwords do not match")
            elif initial and password == initial:
                errors.append("Please choose your own password instead of the generated one")
            else:
                errors = set_credentials(settings, username, password)
        elif username.strip() != settings.get("auth_username"):
            errors.append("Enter a password to change the username")
        if not errors:
            errors = settings.update(values)
        if errors:
            values["proxy_auth_trusted_networks"] = str(values["proxy_auth_trusted_networks"]).splitlines()
            return render_setup(request, "account", errors=errors, values={"auth_username": username, **values})
        web_sessions.delete_others(request.cookies.get(SESSION_COOKIE))
        raise Redirect("/setup/relay")

    async def run_connection_test(relay: dict) -> tuple[int, bool, str]:
        return await test_connection(db, {**settings.snapshot(), **as_settings(relay)})

    @app.post("/setup/relay", response_class=HTMLResponse, dependencies=[Depends(setup_auth)])
    async def setup_relay(request: Request):
        form = await request.form()
        values = {k: str(form.get(k, "")) for k in ("name", "host", "port", "security", "username", "password")}
        values["tls_verify"] = "tls_verify" in form
        values["name"] = values["name"].strip() or values["host"].strip()
        relay = relays.first()
        relay_id, errors = relays.save(values, relay["id"] if relay else None, require_senders=False)
        if errors:
            values.pop("password")
            return render_setup(request, "relay", errors=errors, relay_values=values)
        if form.get("action") == "test":
            session_id, ok, detail = await run_connection_test(relays.get(relay_id))
            # Shown inline: the sessions page is not reachable before the setup is completed.
            session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
            session["lines"] = db.transcript(session_id)
            return render_setup(request, "relay", test={"ok": ok, "detail": detail, "session": session})
        raise Redirect("/setup/senders")

    @app.post("/setup/senders", response_class=HTMLResponse, dependencies=[Depends(setup_auth)])
    async def setup_senders(request: Request):
        relay = relays.first()
        if relay is None:
            raise Redirect("/setup/relay")
        form = await request.form()
        senders, networks = str(form.get("senders", "")), str(form.get("allowed_networks", ""))
        _, errors = relays.save({"senders": senders}, relay["id"])
        errors += settings.update({"allowed_networks": networks})
        if errors:
            return render_setup(request, "senders", errors=errors, values={"allowed_networks": networks.splitlines()},
                                relay_values={"senders": senders.splitlines()})
        raise Redirect("/setup/options")

    @app.post("/setup/options", response_class=HTMLResponse, dependencies=[Depends(setup_auth)])
    async def setup_options(request: Request):
        relay = relays.first()
        if relay is None:
            raise Redirect("/setup/relay")
        form = await request.form()
        rewrite = {k: str(form.get(k, "")) for k in ("rewrite_from_address", "rewrite_name_mode")}
        rewrite["rewrite_enabled"] = "rewrite_enabled" in form
        notify = str(form.get("notify_address", ""))
        _, errors = relays.save(rewrite, relay["id"])
        errors += settings.update({"notify_address": notify})
        if errors:
            return render_setup(request, "options", errors=errors, values={"notify_address": notify},
                                relay_values=rewrite)
        settings.set_internal(setup_completed=True)
        log.info("Setup completed")
        raise Redirect("/")

    def relay_form(form) -> dict:
        values = {k: str(form[k]) for k in RELAY_FIELDS if k not in RELAY_BOOL_FIELDS and k in form}
        values.update({k: k in form for k in RELAY_BOOL_FIELDS})
        return values

    def render_relay(request, relay: dict, relay_id: int | None, errors=()):
        return render(request, "relay_edit.html", r=relay, relay_id=relay_id, errors=list(errors))

    @app.get("/relays", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def relay_list(request: Request, saved: int = 0, deleted: int = 0):
        since = time.time() - 86400
        usage = {r["relay_name"]: r["n"] for r in db.query(
            "SELECT relay_name, COUNT(*) AS n FROM messages WHERE received_at >= ? AND relay_name IS NOT NULL "
            "GROUP BY relay_name", (since,))}
        return render(request, "relays.html", relays=relays.all(), usage=usage, saved=saved, deleted=deleted)

    @app.get("/relays/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def relay_new(request: Request):
        return render_relay(request, dict(NEW_RELAY), None)

    @app.post("/relays/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def relay_create(request: Request):
        values = relay_form(await request.form())
        relay_id, errors = relays.save(values)
        if errors:
            return render_relay(request, {**NEW_RELAY, **values, "senders": values.get("senders", "").splitlines()},
                                None, errors)
        raise Redirect("/relays?saved=1")

    @app.get("/relays/{relay_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def relay_edit(request: Request, relay_id: int):
        relay = relays.get(relay_id)
        if relay is None:
            raise HTTPException(404, "Relay not found")
        return render_relay(request, relay, relay_id)

    @app.post("/relays/{relay_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def relay_update(request: Request, relay_id: int):
        relay = relays.get(relay_id)
        if relay is None:
            raise HTTPException(404, "Relay not found")
        values = relay_form(await request.form())
        _, errors = relays.save(values, relay_id)
        if errors:
            return render_relay(request, {**relay, **values, "senders": values.get("senders", "").splitlines()},
                                relay_id, errors)
        raise Redirect("/relays?saved=1")

    @app.post("/relays/{relay_id}/delete", dependencies=[Depends(auth)])
    async def relay_delete(relay_id: int):
        relays.delete(relay_id)
        raise Redirect("/relays?deleted=1")

    @app.post("/relays/{relay_id}/test", dependencies=[Depends(auth)])
    async def relay_test(relay_id: int):
        relay = relays.get(relay_id)
        if relay is None:
            raise HTTPException(404, "Relay not found")
        session_id, _, _ = await run_connection_test(relay)
        raise Redirect(f"/sessions/{session_id}")

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def messages(request: Request, q: str = "", status: str = "", page: int = 1):
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("(mail_from LIKE ? OR rcpt_to LIKE ? OR subject LIKE ? OR from_header LIKE ? "
                         "OR to_header LIKE ? OR client_ip LIKE ? OR client_name LIKE ?)")
            params += [f"%{q}%"] * 7
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        page = max(page, 1)
        rows = db.query(f"SELECT id, received_at, client_ip, client_name, helo, mail_from, rcpt_to, "
                        f"from_header, subject, size, status, detail, relay_name FROM messages {clause} "
                        f"ORDER BY id DESC LIMIT ? OFFSET ?", (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
        since = time.time() - 86400
        stats = {r["status"]: r["n"] for r in db.query(
            "SELECT status, COUNT(*) AS n FROM messages WHERE received_at >= ? GROUP BY status", (since,))}
        return render(request, "messages.html", rows=rows[:PAGE_SIZE], has_next=len(rows) > PAGE_SIZE,
                      page=page, q=q, status=status, statuses=STATUSES, stats=stats,
                      qs=lambda **kw: urlencode({"q": q, "status": status, **kw}),
                      no_relays=not relays.all())

    @app.get("/messages/{message_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def message_detail(request: Request, message_id: int):
        msg = db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not msg:
            raise HTTPException(404, "Message not found")
        parts = message_parts(msg["raw"]) if msg["raw"] else None
        raw_text = msg["raw"].decode("utf-8", "replace") if msg["raw"] else None
        inbound = None
        if msg["session_id"]:
            inbound = db.query_one("SELECT * FROM sessions WHERE id = ?", (msg["session_id"],))
            if inbound:
                inbound["lines"] = db.transcript(inbound["id"])
        outbound = db.query("SELECT * FROM sessions WHERE message_id = ? ORDER BY id", (message_id,))
        for s in outbound:
            s["lines"] = db.transcript(s["id"])
        return render(request, "message.html", msg=msg, parts=parts, raw_text=raw_text,
                      inbound=inbound, outbound=outbound, queue_paused=settings.get("queue_paused"))

    @app.get("/messages/{message_id}/eml", dependencies=[Depends(auth)])
    def message_eml(message_id: int):
        msg = db.query_one("SELECT raw FROM messages WHERE id = ?", (message_id,))
        if not msg or not msg["raw"]:
            raise HTTPException(404, "No content stored")
        return Response(msg["raw"], media_type="message/rfc822",
                        headers={"Content-Disposition": f'attachment; filename="message-{message_id}.eml"'})

    @app.post("/messages/{message_id}/resend", dependencies=[Depends(auth)])
    async def message_resend(message_id: int):
        result = await queue.resend(message_id)
        if result is None:
            raise HTTPException(400, "Cannot resend: no content stored "
                                     "or message still being delivered")
        return RedirectResponse(f"/messages/{message_id}#outbound", status_code=303)

    @app.get("/queue", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def queue_page(request: Request):
        rows = db.query("SELECT id, received_at, queued_at, client_ip, client_name, mail_from, rcpt_to, "
                        "pending_rcpts, subject, size, status, detail, attempts, last_attempt_at, "
                        "next_attempt_at, is_notification FROM messages "
                        "WHERE status IN ('queued', 'relaying') ORDER BY next_attempt_at IS NULL, next_attempt_at")
        s = settings.snapshot()
        return render(request, "queue.html", rows=rows, paused=s["queue_paused"], s=s, now=time.time())

    @app.post("/queue/{message_id}/deliver", dependencies=[Depends(auth)])
    async def queue_deliver(message_id: int):
        await queue.deliver_now(message_id)
        return RedirectResponse(f"/messages/{message_id}#outbound", status_code=303)

    @app.post("/queue/{message_id}/remove", dependencies=[Depends(auth)])
    async def queue_remove(message_id: int):
        queue.remove(message_id)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/queue/deliver-all", dependencies=[Depends(auth)])
    async def queue_deliver_all():
        db.execute("UPDATE messages SET next_attempt_at = ? WHERE status = 'queued'", (time.time(),))
        queue.wake()
        return RedirectResponse("/queue", status_code=303)

    @app.post("/queue/pause", dependencies=[Depends(auth)])
    async def queue_pause(request: Request):
        form = await request.form()
        settings.update({"queue_paused": form.get("paused") == "1"})
        queue.wake()
        return RedirectResponse("/queue", status_code=303)

    @app.get("/sessions", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def sessions(request: Request, kind: str = "", q: str = "", page: int = 1):
        where, params = [], []
        if kind:
            where.append("s.kind = ?")
            params.append(kind)
        if q:
            where.append("(s.peer LIKE ? OR s.peer_name LIKE ? OR s.helo LIKE ? OR s.result LIKE ?)")
            params += [f"%{q}%"] * 4
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        page = max(page, 1)
        rows = db.query(
            f"SELECT s.*, (SELECT COUNT(*) FROM transcript t WHERE t.session_id = s.id) AS line_count, "
            f"(SELECT GROUP_CONCAT(m.id) FROM messages m WHERE m.session_id = s.id) AS message_ids "
            f"FROM sessions s {clause} ORDER BY s.id DESC LIMIT ? OFFSET ?",
            (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
        return render(request, "sessions.html", rows=rows[:PAGE_SIZE], has_next=len(rows) > PAGE_SIZE,
                      page=page, kind=kind, q=q, qs=lambda **kw: urlencode({"kind": kind, "q": q, **kw}))

    @app.get("/sessions/{session_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def session_detail(request: Request, session_id: int):
        s = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not s:
            raise HTTPException(404, "Session not found")
        s["lines"] = db.transcript(session_id)
        msgs = db.query("SELECT id, mail_from, subject, status FROM messages WHERE session_id = ? OR id = ?",
                        (session_id, s["message_id"] or -1))
        return render(request, "session.html", s=s, msgs=msgs)

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_page(request: Request, saved: int = 0, account: int = 0):
        return render(request, "settings.html", s=settings.snapshot(), errors=[], saved=saved,
                      account_saved=account, account_errors=[], min_password=MIN_PASSWORD_LENGTH)

    @app.post("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def settings_save(request: Request):
        form = await request.form()
        values = {}
        for key, default in DEFAULTS.items():
            if key == "queue_paused":
                continue  # toggled on the queue page
            if isinstance(default, bool):
                values[key] = key in form
            elif key in form:
                values[key] = form[key]
        if not values.get("relay_password"):
            values.pop("relay_password", None)  # empty field keeps the stored password
        if form.get("clear_password"):
            values["relay_password"] = ""
        errors = settings.update(values)
        if errors:
            current = {**settings.snapshot(), **{k: v for k, v in values.items() if k not in LIST_KEYS}}
            for key in LIST_KEYS:
                current[key] = str(values.get(key, "")).splitlines()
            return render(request, "settings.html", s=current, errors=errors, saved=0, account_errors=[],
                          min_password=MIN_PASSWORD_LENGTH)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/account", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def settings_account(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))
        password, password2 = str(form.get("new_password", "")), str(form.get("new_password2", ""))
        errors = []
        if not verify_password(str(form.get("current_password", "")), settings.get("auth_password_hash")):
            errors.append("Current password is wrong")
        elif password != password2:
            errors.append("The new passwords do not match")
        else:
            errors = set_credentials(settings, username, password)
        if errors:
            return render(request, "settings.html", s=settings.snapshot(), errors=[], saved=0,
                          account_errors=errors, min_password=MIN_PASSWORD_LENGTH)
        web_sessions.delete_others(request.cookies.get(SESSION_COOKIE))
        return RedirectResponse("/settings?account=1#account", status_code=303)

    @app.post("/settings/clear-history", dependencies=[Depends(auth)])
    def clear_history():
        db.clear_history()
        return RedirectResponse("/", status_code=303)

    @app.get("/tools", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def tools(request: Request):
        all_relays = relays.all()
        first = all_relays[0] if all_relays else NEW_RELAY
        default_from = (first["rewrite_from_address"] if first["rewrite_enabled"] else "") or first["username"]
        return render(request, "tools.html", s=settings.snapshot(), relays=all_relays, default_from=default_from,
                      default_to=first["username"])

    @app.post("/tools/send-test", dependencies=[Depends(auth)])
    async def tools_send_test(request: Request):
        form = await request.form()
        sender = str(form.get("from", "")).strip()
        rcpt = str(form.get("to", "")).strip()
        if not sender or not rcpt:
            raise HTTPException(400, "Sender and recipient are required")
        m = EmailMessage()
        m["From"] = sender
        m["To"] = rcpt
        m["Subject"] = str(form.get("subject") or "smtp-relay test message")
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        m.set_content(str(form.get("body") or "This is a test message from smtp-relay."))
        raw = m.as_bytes(policy=m.policy.clone(linesep="\r\n"))
        relay_id = int(form["relay_id"]) if str(form.get("relay_id", "")).isdigit() else None
        message_id = queue.accept(raw=raw, mail_from=sender, rcpts=[rcpt], client_ip="WebUI",
                                  client_name="Test message", relay_id=relay_id)
        await queue.attempt(message_id)
        return RedirectResponse(f"/messages/{message_id}", status_code=303)

    return app
