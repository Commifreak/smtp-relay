"""Header handling: metadata extraction, From rewriting and trace headers.

Headers are edited on the raw bytes so the message body is passed on unchanged.
"""
import email.policy
import time
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid


def split_message(raw: bytes) -> tuple[list[bytes], bytes]:
    """Split into header fields (each incl. folded continuation lines and line ending) and the rest."""
    lines = raw.splitlines(keepends=True)
    fields: list[bytes] = []
    i = 0
    for i, line in enumerate(lines):
        if line in (b"\r\n", b"\n"):
            break
        if line[:1] in (b" ", b"\t") and fields:
            fields[-1] += line
        elif b":" in line:
            fields.append(line)
        else:
            # Not a header line: treat everything from here on as body.
            return fields, b"".join(lines[i:])
    else:
        return fields, b""
    return fields, b"".join(lines[i:])


def field_name(field: bytes) -> str:
    return field.split(b":", 1)[0].decode("ascii", "replace").strip().lower()


def field_value(field: bytes) -> bytes:
    return field.split(b":", 1)[1].strip()


def decode_value(value: bytes | str) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = " ".join(value.split())
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def header_meta(raw: bytes) -> dict:
    """Decoded header values for display in the history."""
    meta = {"from_header": None, "to_header": None, "subject": None, "message_id_header": None}
    try:
        msg = BytesHeaderParser(policy=email.policy.default).parsebytes(raw)
        for key, header in (("from_header", "From"), ("to_header", "To"), ("subject", "Subject"),
                            ("message_id_header", "Message-ID")):
            try:
                value = msg.get(header)
                meta[key] = str(value) if value is not None else None
            except Exception:
                fields, _ = split_message(raw)
                for f in fields:
                    if field_name(f) == header.lower():
                        meta[key] = decode_value(field_value(f))
                        break
    except Exception:
        pass
    return meta


def message_parts(raw: bytes) -> dict:
    """Plain text, HTML and attachment list for the detail view."""
    result = {"text": None, "html": None, "attachments": [], "headers": [], "error": None}
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(raw)
        result["headers"] = [(k, str(v)) for k, v in msg.items()]
        body = msg.get_body(preferencelist=("plain",))
        if body is not None:
            result["text"] = body.get_content()
        html = msg.get_body(preferencelist=("html",))
        if html is not None:
            result["html"] = html.get_content()
        for part in msg.iter_attachments():
            payload = part.get_payload(decode=True) or b""
            result["attachments"].append((part.get_filename() or "(unnamed)",
                                          part.get_content_type(), len(payload)))
    except Exception as exc:  # malformed messages must not break the UI
        result["error"] = str(exc)
    return result


def _fmt(field: str, value: str) -> bytes:
    return f"{field}: {value}\r\n".encode("utf-8")


def prepare(raw: bytes, mail_from: str, settings: dict, trace: dict | None = None) -> tuple[bytes, str, list[str]]:
    """Apply rewriting/trace headers. Returns (message bytes, envelope sender, notes)."""
    fields, body = split_message(raw)
    notes: list[str] = []
    envelope_from = mail_from
    names = [field_name(f) for f in fields]

    if settings.get("rewrite_enabled") and settings.get("rewrite_from_address"):
        new_addr = settings["rewrite_from_address"]
        orig_raw = None
        orig_name, orig_addr = "", mail_from
        if "from" in names:
            idx = names.index("from")
            orig_raw = field_value(fields[idx])
            parsed = getaddresses([decode_value(orig_raw)])
            if parsed and parsed[0][1]:
                orig_name, orig_addr = parsed[0]

        mode = settings.get("rewrite_name_mode", "keep")
        if mode == "keep":
            name = orig_name
        elif mode == "address":
            name = orig_addr
        elif mode == "fixed":
            name = settings.get("rewrite_fixed_name", "")
        else:
            name = ""
        new_from = formataddr((name, new_addr)) if name else new_addr

        extra = []
        if orig_raw is not None:
            fields[idx] = _fmt("From", new_from)
            extra.append(b"X-Original-From: " + orig_raw + b"\r\n")
            if settings.get("rewrite_set_reply_to") and "reply-to" not in names:
                extra.append(b"Reply-To: " + orig_raw + b"\r\n")
        else:
            fields.append(_fmt("From", new_from))
        fields.extend(extra)
        notes.append(f"From rewritten: {decode_value(orig_raw) if orig_raw else '(missing)'} → {new_from}")
        if settings.get("rewrite_envelope"):
            envelope_from = new_addr
            if new_addr != mail_from:
                notes.append(f"Envelope sender rewritten: {mail_from} → {new_addr}")

    if settings.get("add_missing_headers"):
        if "date" not in names:
            fields.append(_fmt("Date", formatdate(localtime=True)))
            notes.append("Added missing Date header")
        if "message-id" not in names:
            domain = envelope_from.rsplit("@", 1)[-1] if "@" in envelope_from else settings.get("smtp_hostname")
            fields.append(_fmt("Message-ID", make_msgid(domain=domain)))
            notes.append("Added missing Message-ID header")

    if settings.get("add_received_header") and trace:
        received = (f"Received: from {trace.get('helo') or 'unknown'} "
                    f"({trace.get('client_name') or 'unknown'} [{trace.get('client_ip')}])\r\n"
                    f"\tby {settings.get('smtp_hostname')} (smtp-relay) with ESMTP id {trace.get('id')}\r\n"
                    f"\tfor <{trace.get('rcpt')}>; {formatdate(time.time(), localtime=True)}\r\n")
        fields.insert(0, received.encode("utf-8", "replace"))

    for i, f in enumerate(fields):
        if not f.endswith(b"\n"):
            fields[i] = f + b"\r\n"
    if not body.startswith((b"\r\n", b"\n")):
        body = b"\r\n" + body
    return b"".join(fields) + body, envelope_from, notes
