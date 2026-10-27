# MTA-Relay

Ein kleiner SMTP-Relay (Smarthost-Client) als Docker-Container. Er nimmt Mails von Geräten und Diensten im
lokalen Netz an (NAS, Drucker, Home Assistant, Proxmox, Cronjobs …) und leitet sie authentifiziert über
**mailbox.org** (oder einen anderen SMTP-Anbieter) weiter. Das WebUI ist auf **maximale Diagnose** ausgelegt.

## Funktionen

- **SMTP-Annahme** im LAN (Container-Port 2525, auf dem Host z. B. auf Port 25 gemappt)
- **Zugriffskontrolle**
  - erlaubte Client-Netze (CIDR) – andere IPs werden schon beim Verbindungsaufbau abgewiesen
  - **erlaubte Absender** (`dienst@domain.de`, `@domain.de` für eine ganze Domain, `*` für alle)
  - optional erlaubte Empfänger
- **Weiterleitung** an das Relay per SSL/TLS (465) oder STARTTLS (587) mit Anmeldung
- **Synchrone Zustellung**: Der Client bekommt direkt die Antwort des Relays.
  - Hat mailbox.org die Mail angenommen, antwortet MTA-Relay mit `250`.
  - Bei einem temporären Fehler antwortet es mit `451`, der Client versucht es dann später erneut.
  - Bei einer endgültigen Ablehnung antwortet es mit `5xx` und dem Original-Fehlertext.
- **Absender umschreiben**: Der From-Header (und optional der Envelope-Absender) bekommt eine Adresse, die
  mailbox.org akzeptiert.
  - Der Anzeigename lässt sich übernehmen, durch die Originaladresse oder einen festen Namen ersetzen.
  - Optional wird Reply-To auf den Originalabsender gesetzt.
  - Der Original-From bleibt als `X-Original-From` erhalten.
- **WebUI**
  - **Mail-Historie** mit Zeit, Client-IP, Reverse-DNS und HELO, Absender, Empfänger, Betreff, Größe und Status, dazu Suche und Filter
  - **Detailansicht** mit allen Headern, Text- und HTML-Vorschau (abgeschottet), Anhangsliste, Rohdaten und `.eml`-Download
  - **Kompletter SMTP-Dialog Client → MTA-Relay** mit Zeitstempeln in Millisekunden
  - **Kompletter SMTP-Dialog MTA-Relay → mailbox.org**, inklusive TLS-Version, Cipher, Zertifikat und Rewrite-Hinweisen. Zugangsdaten werden maskiert.
  - **Alle SMTP-Sitzungen**, auch ohne Mail: abgewiesene IPs, Portscans, abgebrochene Verbindungen
  - **Erneut senden** für gespeicherte Mails
  - **Diagnose**: Verbindungs- und Login-Test zum Relay mit Protokoll, außerdem Testmail versenden
  - **Einstellungen**: werden sofort und ohne Neustart wirksam
  - Auto-Refresh und Dark Mode
- Historie in SQLite mit konfigurierbarer Aufbewahrungsdauer; Mail-Inhalte zu speichern lässt sich abschalten

## Schnellstart

```bash
git clone git@github.com:Commifreak/smtp-relay.git
cd smtp-relay
cp .env.example .env      # Zugangsdaten und WebUI-Passwort eintragen
docker compose up -d --build
```

- SMTP: `<docker-host>:25`
- WebUI: `http://<docker-host>:8080` (Benutzer/Passwort aus `WEBUI_USER` / `WEBUI_PASSWORD`)

Danach im WebUI unter **Diagnose → Verbindung testen** prüfen, ob die Anmeldung bei mailbox.org klappt, und
unter **Einstellungen** die erlaubten Absender pflegen.

Test aus dem LAN:

```bash
swaks --server <docker-host>:25 --from nas@example.de --to du@mailbox.org --header "Subject: Test"
```

## mailbox.org

| Einstellung     | Wert                                    |
|-----------------|-----------------------------------------|
| Host            | `smtp.mailbox.org`                      |
| Port / Modus    | `465` / SSL/TLS oder `587` / STARTTLS   |
| Benutzer        | deine mailbox.org-Adresse               |
| Passwort        | am besten ein eigenes App-Passwort      |

mailbox.org nimmt nur Absenderadressen an, die zum Konto gehören: Hauptadresse, Alias oder eigene,
verifizierte Domain. Für Dienste mit beliebigen Absendern (`root@nas.local` …) aktivierst du **Absender
umschreiben** und trägst als neue Absenderadresse eine eigene Adresse ein. Mit „Originaladresse als
Anzeigename“ siehst du im Postfach trotzdem sofort, welcher Dienst geschrieben hat.

## Konfiguration

Alle fachlichen Einstellungen werden im WebUI gepflegt und im Volume (`/data/mta-relay.db`) gespeichert.
Die Umgebungsvariablen in `.env` (siehe `.env.example`) dienen nur als **Startwerte beim ersten Start**.

Diese Variablen gelten nur über die Umgebung:

| Variable           | Standard        | Bedeutung                                            |
|--------------------|-----------------|------------------------------------------------------|
| `WEBUI_USER`       | `admin`         | Benutzername WebUI                                   |
| `WEBUI_PASSWORD`   | –               | Passwort WebUI (ohne Passwort ist das UI offen!)     |
| `SMTP_LISTEN_PORT` | `2525`          | SMTP-Port im Container                               |
| `WEB_PORT`         | `8080`          | WebUI-Port im Container                              |
| `TZ`               | `Europe/Berlin` | Zeitzone für die Anzeige                             |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | – | Aktiviert STARTTLS für eingehende Verbindungen     |
| `LOG_LEVEL`        | `INFO`          | Log-Level der Container-Logs                         |

### Client-IP und Docker

Die Netz-Prüfung und die Historie verwenden die Quell-IP der Verbindung. Im Standard-Bridge-Modus mit
Port-Mapping bleibt die echte Client-IP für IPv4 normalerweise erhalten (iptables-DNAT). Siehst du im WebUI
nur die Gateway-IP des Docker-Netzes (z. B. `172.17.0.1`), hilft eine dieser Varianten:

- `network_mode: host` in der `docker-compose.yml` (dann `SMTP_LISTEN_PORT=25` setzen und `ports:` entfernen;
  Port 25 braucht dann Root oder `cap_add: [NET_BIND_SERVICE]`)
- oder `"userland-proxy": false` in `/etc/docker/daemon.json`

## Sicherheitshinweise

- Den SMTP-Port **nicht** ins Internet freigeben. Zusammen mit den erlaubten Netzen und Absendern verhindert
  das, dass der Dienst zum offenen Relay wird.
- `WEBUI_PASSWORD` setzen. Das WebUI zeigt Mail-Inhalte und enthält das Relay-Passwort, das in der
  Datenbank im Volume gespeichert ist.
- Wenn Inhalte nicht gespeichert werden sollen, „Mail-Inhalte speichern“ deaktivieren. Metadaten und
  SMTP-Protokolle bleiben erhalten, „Erneut senden“ ist dann aber nicht möglich.

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest -q
DATA_DIR=./data WEBUI_PASSWORD=test python -m app.main
```

Aufbau:

- `app/smtp_server.py`: SMTP-Annahme (aiosmtpd), Policy und Mitschnitt des eingehenden Dialogs
- `app/relay.py`: Zustellung per smtplib mit Mitschnitt des ausgehenden Dialogs
- `app/rewrite.py`: Header-Rewrite direkt auf den Rohdaten, damit der Body unverändert bleibt
- `app/web.py` und `app/templates/`: WebUI (FastAPI und Jinja2, ohne externe Abhängigkeiten im Browser)
- `app/db.py`: SQLite (Einstellungen, Sitzungen, Protokollzeilen, Nachrichten)

Die GitHub-Action testet jeden Push und baut ein Multi-Arch-Image (amd64/arm64) nach
`ghcr.io/<owner>/mta-relay`.
