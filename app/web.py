"""Web UI: mail history, SMTP transcripts, settings and diagnostics."""
import json
import os
import secrets
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import DEFAULTS, LIST_KEYS, Settings
from .db import Database
from .relay import deliver, resend, test_connection
from .rewrite import header_meta, message_parts

BASE = Path(__file__).parent
PAGE_SIZE = 100
STATUSES = ["relayed", "partial", "deferred", "failed", "rejected", "relaying", "received"]
STATUS_LABELS = {"relayed": "zugestellt", "partial": "teilweise", "deferred": "temporär fehlgeschlagen",
                 "failed": "fehlgeschlagen", "rejected": "abgelehnt", "relaying": "wird zugestellt",
                 "received": "angenommen"}
KIND_LABELS = {"inbound": "Eingehend", "outbound": "Relay", "test": "Verbindungstest"}


def fmt_ts(ts, with_ms=False):
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    if with_ms:
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def fmt_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def create_app(db: Database, settings: Settings) -> FastAPI:
    app = FastAPI(title="MTA-Relay", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    env = templates.env
    env.filters["ts"] = fmt_ts
    env.filters["size"] = fmt_size
    env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    env.globals.update(STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS, version=__version__)

    user = os.environ.get("WEBUI_USER", "admin")
    password = os.environ.get("WEBUI_PASSWORD", "")
    basic = HTTPBasic(auto_error=False)
    env.globals["auth_disabled"] = not password

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
        if password:
            ok = creds is not None and \
                secrets.compare_digest(creds.username.encode(), user.encode()) and \
                secrets.compare_digest(creds.password.encode(), password.encode())
            if not ok:
                raise HTTPException(401, "Anmeldung erforderlich", {"WWW-Authenticate": 'Basic realm="MTA-Relay"'})
        if request.method == "POST":
            # Basic CSRF protection: POSTs must originate from this UI.
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "Ungültige Herkunft der Anfrage")

    def render(request, name, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

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
                        f"from_header, subject, size, status, detail FROM messages {clause} "
                        f"ORDER BY id DESC LIMIT ? OFFSET ?", (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
        since = time.time() - 86400
        stats = {r["status"]: r["n"] for r in db.query(
            "SELECT status, COUNT(*) AS n FROM messages WHERE received_at >= ? GROUP BY status", (since,))}
        return render(request, "messages.html", rows=rows[:PAGE_SIZE], has_next=len(rows) > PAGE_SIZE,
                      page=page, q=q, status=status, statuses=STATUSES, stats=stats,
                      qs=lambda **kw: urlencode({"q": q, "status": status, **kw}),
                      no_senders=not settings.get("allowed_senders"))

    @app.get("/messages/{message_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def message_detail(request: Request, message_id: int):
        msg = db.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not msg:
            raise HTTPException(404, "Nachricht nicht gefunden")
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
                      inbound=inbound, outbound=outbound)

    @app.get("/messages/{message_id}/eml", dependencies=[Depends(auth)])
    def message_eml(message_id: int):
        msg = db.query_one("SELECT raw FROM messages WHERE id = ?", (message_id,))
        if not msg or not msg["raw"]:
            raise HTTPException(404, "Kein Inhalt gespeichert")
        return Response(msg["raw"], media_type="message/rfc822",
                        headers={"Content-Disposition": f'attachment; filename="message-{message_id}.eml"'})

    @app.post("/messages/{message_id}/resend", dependencies=[Depends(auth)])
    async def message_resend(message_id: int):
        result = await resend(db, settings.snapshot(), message_id)
        if result is None:
            raise HTTPException(400, "Kein Inhalt gespeichert – erneutes Senden nicht möglich")
        return RedirectResponse(f"/messages/{message_id}#outbound", status_code=303)

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
            raise HTTPException(404, "Sitzung nicht gefunden")
        s["lines"] = db.transcript(session_id)
        msgs = db.query("SELECT id, mail_from, subject, status FROM messages WHERE session_id = ? OR id = ?",
                        (session_id, s["message_id"] or -1))
        return render(request, "session.html", s=s, msgs=msgs)

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_page(request: Request, saved: int = 0):
        return render(request, "settings.html", s=settings.snapshot(), errors=[], saved=saved)

    @app.post("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def settings_save(request: Request):
        form = await request.form()
        values = {}
        for key, default in DEFAULTS.items():
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
            return render(request, "settings.html", s=current, errors=errors, saved=0)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/clear-history", dependencies=[Depends(auth)])
    def clear_history():
        db.clear_history()
        return RedirectResponse("/", status_code=303)

    @app.get("/tools", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def tools(request: Request):
        s = settings.snapshot()
        return render(request, "tools.html", s=s, default_from=s["rewrite_from_address"] or s["relay_username"])

    @app.post("/tools/test-connection", dependencies=[Depends(auth)])
    async def tools_test_connection():
        session_id, _, _ = await test_connection(db, settings.snapshot())
        return RedirectResponse(f"/sessions/{session_id}", status_code=303)

    @app.post("/tools/send-test", dependencies=[Depends(auth)])
    async def tools_send_test(request: Request):
        form = await request.form()
        sender = str(form.get("from", "")).strip()
        rcpt = str(form.get("to", "")).strip()
        if not sender or not rcpt:
            raise HTTPException(400, "Absender und Empfänger erforderlich")
        s = settings.snapshot()
        m = EmailMessage()
        m["From"] = sender
        m["To"] = rcpt
        m["Subject"] = str(form.get("subject") or "MTA-Relay Testmail")
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        m.set_content(str(form.get("body") or "Dies ist eine Testmail von MTA-Relay."))
        raw = m.as_bytes(policy=m.policy.clone(linesep="\r\n"))
        message_id = db.insert_message(client_ip="WebUI", client_name="Testmail", mail_from=sender,
                                       rcpt_to=json.dumps([rcpt]), size=len(raw), status="received",
                                       raw=raw if s["store_content"] else None, **header_meta(raw))
        await deliver(db, s, message_id, raw, sender, [rcpt])
        return RedirectResponse(f"/messages/{message_id}", status_code=303)

    return app
