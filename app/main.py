"""Entry point: runs the SMTP listener and the web UI in one asyncio loop."""
import asyncio
import logging
import os
import ssl
import time

import uvicorn

from .auth import ensure_credentials
from .config import Settings
from .db import Database
from .delivery import DeliveryQueue
from .migrations import MigrationError, migrate
from .relays import Relays
from .smtp_server import protocol_factory
from .web import create_app

log = logging.getLogger("smtp-relay")


def load_tls_context() -> ssl.SSLContext | None:
    cert, key = os.environ.get("TLS_CERT_FILE"), os.environ.get("TLS_KEY_FILE")
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        log.info("STARTTLS enabled with %s", cert)
        return ctx
    return None


async def purge_loop(db: Database, settings: Settings) -> None:
    while True:
        days = settings.get("retention_days")
        if days:
            try:
                db.purge(time.time() - days * 86400)
            except Exception:
                log.exception("Cleanup failed")
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "smtp-relay.db")
    notes = []
    for suffix in ("", "-wal", "-shm"):  # database of releases named "MTA-Relay"
        old = os.path.join(data_dir, "mta-relay.db" + suffix)
        if os.path.exists(old) and not os.path.exists(db_path + suffix):
            os.rename(old, db_path + suffix)
            notes.append(f"Renamed {os.path.basename(old)} to {os.path.basename(db_path + suffix)}")
    db = Database(db_path)
    try:
        migrate(db, data_dir, out=lambda line: print(line, flush=True), notes=notes)
    except MigrationError as exc:
        log.critical("%s", exc)
        raise SystemExit(1)
    if db.created:
        log.info("Created a new database at %s", db_path)
    settings = Settings(db)
    reset = os.environ.get("RESET_ADMIN_PASSWORD", "").lower() in ("1", "true", "yes")
    ensure_credentials(settings, reset=reset)
    if reset:
        log.warning("RESET_ADMIN_PASSWORD is set – remove it again, otherwise the password is reset on every start")

    smtp_host = os.environ.get("SMTP_LISTEN_HOST") or None
    smtp_port = int(os.environ.get("SMTP_LISTEN_PORT", "2525"))
    web_port = int(os.environ.get("WEB_PORT", "8080"))

    queue = DeliveryQueue(db, settings)
    loop = asyncio.get_running_loop()
    server = await loop.create_server(protocol_factory(db, settings, queue, load_tls_context()),
                                      host=smtp_host, port=smtp_port)
    log.info("SMTP listening on port %s, web UI on port %s", smtp_port, web_port)
    if not settings.get("setup_completed"):
        log.warning("Setup not completed yet – open the web UI to run the setup wizard")
    elif not Relays(db).all():
        log.warning("No relay configured – all mail will be rejected")

    web = uvicorn.Server(uvicorn.Config(create_app(db, settings, queue), host="0.0.0.0", port=web_port,
                                        log_level="warning", proxy_headers=False))
    tasks = [asyncio.create_task(purge_loop(db, settings)), asyncio.create_task(queue.run())]
    try:
        await web.serve()
    finally:
        for task in tasks:
            task.cancel()
        server.close()


if __name__ == "__main__":
    asyncio.run(main())
