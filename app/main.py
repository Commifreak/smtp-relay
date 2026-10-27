"""Entry point: runs the SMTP listener and the web UI in one asyncio loop."""
import asyncio
import logging
import os
import ssl
import time

import uvicorn

from .config import Settings
from .db import Database
from .smtp_server import protocol_factory
from .web import create_app

log = logging.getLogger("mta-relay")


def load_tls_context() -> ssl.SSLContext | None:
    cert, key = os.environ.get("TLS_CERT_FILE"), os.environ.get("TLS_KEY_FILE")
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        log.info("STARTTLS aktiviert mit %s", cert)
        return ctx
    return None


async def purge_loop(db: Database, settings: Settings) -> None:
    while True:
        days = settings.get("retention_days")
        if days:
            try:
                db.purge(time.time() - days * 86400)
            except Exception:
                log.exception("Bereinigung fehlgeschlagen")
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    db = Database(os.path.join(data_dir, "mta-relay.db"))
    settings = Settings(db)

    smtp_host = os.environ.get("SMTP_LISTEN_HOST") or None
    smtp_port = int(os.environ.get("SMTP_LISTEN_PORT", "2525"))
    web_port = int(os.environ.get("WEB_PORT", "8080"))

    loop = asyncio.get_running_loop()
    server = await loop.create_server(protocol_factory(db, settings, load_tls_context()),
                                      host=smtp_host, port=smtp_port)
    log.info("SMTP lauscht auf Port %s, WebUI auf Port %s", smtp_port, web_port)
    if not settings.get("allowed_senders"):
        log.warning("Keine erlaubten Absender konfiguriert – es werden alle Mails abgelehnt")

    web = uvicorn.Server(uvicorn.Config(create_app(db, settings), host="0.0.0.0", port=web_port,
                                        log_level="warning", proxy_headers=True))
    purge = asyncio.create_task(purge_loop(db, settings))
    try:
        await web.serve()
    finally:
        purge.cancel()
        server.close()


if __name__ == "__main__":
    asyncio.run(main())
