# MTA-Relay

A small SMTP relay (smarthost client) in a Docker container. It accepts mail from devices and services on
your local network (NAS, printers, Home Assistant, Proxmox, cron jobs …) and forwards it with
authentication through **mailbox.org** or any other SMTP provider. The web UI is built for **maximum
diagnostics**: every message and every line of both SMTP conversations is recorded.

![Mail history](docs/screenshots/history.png)

## Features

- **SMTP for the LAN** on container port 2525, mapped to e.g. port 25 on the host
- **Access control**
  - allowed client networks (CIDR); other IPs are refused as soon as they connect
  - **allowed senders**: `service@domain.com`, `@domain.com` for a whole domain, or `*` for anyone
  - optional allowed recipients
- **Relaying** via SSL/TLS (465) or STARTTLS (587) with authentication
- **Delivery queue with automatic retries** (store-and-forward)
  - Every message is stored in the database first and delivered right away.
  - Permanent rejections from the relay (5xx) are returned to the client directly.
  - On temporary failures (relay unreachable, timeout, 4xx) the message is still accepted with `250` and
    retried later; by default after 1, 5, 15 and 30 minutes, then every 60 minutes.
  - If the relay temporarily rejects only some recipients, only those are retried.
  - Delivery is given up after the maximum queue lifetime (default 48 h).
  - Optionally, a notification goes to an admin address when delivery finally fails.
  - The queue survives container restarts; interrupted deliveries are resumed.
- **Sender rewriting**: the From header (and optionally the envelope sender) is replaced with an address
  your provider accepts.
  - The display name can be kept, or replaced by the original address or a fixed name.
  - Reply-To can be set to the original sender.
  - The original From header is preserved as `X-Original-From`.
- **Web UI**
  - **Mail history** with time, client IP, reverse DNS and HELO, sender, recipients, subject, size and
    status; search and filters
  - **Message details** with all headers, text and sandboxed HTML preview, attachment list, raw message
    and `.eml` download
  - **Full SMTP conversation client → MTA-Relay** with millisecond timestamps
  - **Full SMTP conversation MTA-Relay → relay server** for every delivery attempt, including TLS version,
    cipher, certificate and rewrite notes. Credentials are masked.
  - **Queue** with attempts, next attempt and last error; actions “Deliver now”, “Remove”,
    “Deliver all now” and “Pause queue”
  - **All SMTP sessions**, including those without mail: refused IPs, port scans, aborted connections
  - **Resend** for finished messages
  - **Diagnostics**: connection and login test against the relay with full transcript, test message
  - **Settings** that take effect immediately, without a restart
  - Auto-refresh and dark mode
- History in SQLite with configurable retention. Keeping message content permanently can be turned off; it
  is then only kept until delivery is finished.

## Screenshots

**Message details** with delivery state and both SMTP conversations. Here, one recipient was accepted,
the other was temporarily refused and is queued for a retry:

![Message details](docs/screenshots/message.png)

**Queue**:

![Queue](docs/screenshots/queue.png)

**SMTP sessions**:

![SMTP sessions](docs/screenshots/sessions.png)

**Settings**:

![Settings](docs/screenshots/settings.png)

## Quick start

Prebuilt images for amd64 and arm64 are published to the GitHub Container Registry as
`ghcr.io/commifreak/mta-relay`.

1. Get `docker-compose.yml` and `.env.example` from this repository, e.g. by cloning it:

   ```bash
   git clone git@github.com:Commifreak/smtp-relay.git
   cd smtp-relay
   cp .env.example .env      # enter relay credentials and a web UI password
   ```

2. While the image is private, log in to the registry once on the Docker host. Use a GitHub personal
   access token (classic) with the `read:packages` scope:

   ```bash
   echo <TOKEN> | docker login ghcr.io -u <github-user> --password-stdin
   ```

3. Start it:

   ```bash
   docker compose up -d
   ```

- SMTP: `<docker-host>:25`
- Web UI: `http://<docker-host>:8080` (user and password from `WEBUI_USER` / `WEBUI_PASSWORD`)

Then open **Diagnostics → Test connection** to check the login at your provider, and maintain the allowed
senders under **Settings**.

Test from the LAN:

```bash
swaks --server <docker-host>:25 --from nas@example.com --to you@example.com --header "Subject: Test"
```

### Updates and image tags

```bash
docker compose pull && docker compose up -d
```

| Tag         | Contents                                                                 |
|-------------|--------------------------------------------------------------------------|
| `1`         | Latest 1.x release – **default in `docker-compose.yml`**, never jumps to 2.x |
| `1.1`       | Latest 1.1.x release (bug fixes only)                                    |
| `1.1.0`     | Exactly this release                                                     |
| `latest`    | Latest build of the `main` branch (may contain unreleased changes)       |
| `sha-…`     | A specific commit                                                        |

Pinning the major version means a future 2.0 with breaking changes will not be installed automatically;
switch to `:2` deliberately after reading the release notes.

### Building from source

Replace the `image:` line in `docker-compose.yml` with `build: .` and run `docker compose up -d --build`.

## mailbox.org

| Setting         | Value                                   |
|-----------------|-----------------------------------------|
| Host            | `smtp.mailbox.org`                      |
| Port / mode     | `465` / SSL/TLS or `587` / STARTTLS     |
| Username        | your mailbox.org address                |
| Password        | preferably a dedicated app password     |

mailbox.org only accepts sender addresses that belong to your account: primary address, alias or your own
verified domain. For services with arbitrary senders (`root@nas.local` …), enable **Sender rewriting** and
enter one of your own addresses as the new sender address. With “Original address as display name” you can
still see at a glance which service sent the message.

## Configuration

All functional settings are managed in the web UI and stored in the volume (`/data/mta-relay.db`). The
environment variables in `.env` (see `.env.example`) only provide **initial values on the very first start**.

These variables are only read from the environment:

| Variable           | Default         | Meaning                                              |
|--------------------|-----------------|------------------------------------------------------|
| `WEBUI_USER`       | `admin`         | Web UI username                                      |
| `WEBUI_PASSWORD`   | –               | Web UI password (without it the UI is open!)         |
| `SMTP_LISTEN_PORT` | `2525`          | SMTP port inside the container                       |
| `WEB_PORT`         | `8080`          | Web UI port inside the container                     |
| `TZ`               | `Europe/Berlin` | Time zone used for display                           |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | – | Enables STARTTLS for inbound connections           |
| `LOG_LEVEL`        | `INFO`          | Log level of the container logs                      |

### Client IP and Docker

The network check and the history use the source IP of the connection. In the default bridge mode with port
mapping, the real client IP is usually preserved for IPv4 (iptables DNAT). If the web UI only shows the
gateway IP of the Docker network (e.g. `172.17.0.1`), use one of these options:

- `network_mode: host` in `docker-compose.yml` (then set `SMTP_LISTEN_PORT=25` and remove `ports:`;
  port 25 then needs root or `cap_add: [NET_BIND_SERVICE]`)
- or `"userland-proxy": false` in `/etc/docker/daemon.json`

## Security notes

- Do **not** expose the SMTP port to the internet. Together with the allowed networks and senders this
  keeps the service from becoming an open relay.
- Set `WEBUI_PASSWORD`. The web UI shows message content and holds the relay password, which is stored in
  the database in the volume.
- If message content should not be kept, disable “Keep message content”. While a message is queued, its
  content is still stored. Metadata and SMTP transcripts are kept, but “Resend” is then not available.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
DATA_DIR=./data WEBUI_PASSWORD=test python -m app.main
```

Layout:

- `app/smtp_server.py`: SMTP listener (aiosmtpd), policy checks and recording of the inbound conversation
- `app/delivery.py`: queue, delivery attempts, retries and failure notifications
- `app/relay.py`: SMTP connection to the relay (smtplib) with recording of the outbound conversation
- `app/rewrite.py`: header rewriting on the raw message, so the body stays untouched
- `app/web.py` and `app/templates/`: web UI (FastAPI and Jinja2, no external browser dependencies)
- `app/db.py`: SQLite (settings, sessions, transcript lines, messages)

The GitHub Action runs the tests on every push and pull request. Pushes to `main` publish the `latest`
image; pushing a version tag publishes the release tags:

```bash
git tag v1.2.0 && git push origin v1.2.0   # -> images 1.2.0, 1.2 and 1
```

Keep `__version__` in `app/__init__.py` in sync with the tag.
