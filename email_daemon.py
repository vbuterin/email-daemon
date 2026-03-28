"""
email_daemon.py — IMAP email polling daemon with HTTP query interface.

Mirrors the architecture of signal_daemon.py:
  - Polls an IMAP mailbox on a configurable interval
  - Stores messages in a local SQLite database
  - Exposes a small HTTP API for querying and sending

Supports any IMAP/SMTP provider. For Protonmail, use Protonmail Bridge
(runs locally on 127.0.0.1:1143 IMAP / 127.0.0.1:1025 SMTP).

Usage
-----
First run (saves credentials):
    python email_daemon.py --imap-host 127.0.0.1 --imap-port 1143 \
                           --smtp-host 127.0.0.1 --smtp-port 1025 \
                           --email you@proton.me --password yourBridgePassword

Subsequent runs (uses stored config):
    python email_daemon.py

HTTP endpoints (default port 6001)
-----------------------------------
GET /messages
    ?sender=   filter by From address (substring match)
    ?subject=  filter by subject (substring match)
    ?since=    ISO-8601 datetime, e.g. 2024-01-01T00:00:00
    ?until=    ISO-8601 datetime
    ?folder=   mailbox folder (default: INBOX)
    ?limit=    max rows to return (default: 100)

GET /send
    ?to=       recipient address (required)
    ?subject=  email subject (required)
    ?body=     message body (required)

GET /folders
    Lists all mailbox folders on the IMAP server.

GET /status
    Returns daemon uptime, last poll time, message count, config summary.
"""

import asyncio
import email as email_lib
import imaplib
import json
import os
import smtplib
import sqlite3
import ssl
import sys
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import argparse

# ── Constants ──────────────────────────────────────────────────────────────────

DAEMON_DIR = os.path.expanduser("~/.email_daemon")
DB_PATH = os.path.join(DAEMON_DIR, "messages.db")
PORT = 6001
POLL_INTERVAL = 60  # seconds

_start_time = datetime.now(timezone.utc)
_last_poll: datetime | None = None

# ── Database ───────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    os.makedirs(DAEMON_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY,
            uid          TEXT,
            folder       TEXT,
            message_id   TEXT,
            sender       TEXT,
            recipients   TEXT,
            subject      TEXT,
            body_plain   TEXT,
            body_html    TEXT,
            date_sent    TEXT,
            received_at  TEXT,
            raw_headers  TEXT,
            UNIQUE(uid, folder)
        )
    """)
    db.commit()
    return db


def get_config(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT key, value FROM config").fetchall()
    return {k: v for k, v in rows}


def set_config(db: sqlite3.Connection, **kwargs) -> None:
    for k, v in kwargs.items():
        db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v))
        )
    db.commit()


# ── Email helpers ──────────────────────────────────────────────────────────────

def _decode_header_value(raw: str | bytes | None) -> str:
    """Decode a (possibly encoded) email header value to a plain string."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _get_body(msg: email_lib.message.Message) -> tuple[str, str]:
    """Extract (plain_text, html) bodies from a Message object."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload:
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text
    return plain, html


# ── IMAP connection ────────────────────────────────────────────────────────────

def _imap_connect(cfg: dict) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    host = cfg["imap_host"]
    port = int(cfg["imap_port"])
    use_ssl = cfg.get("imap_ssl", "true").lower() == "true"

    if use_ssl:
        # Protonmail Bridge uses a self-signed cert — disable verification for localhost
        if host in ("127.0.0.1", "localhost"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        else:
            conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)

    conn.login(cfg["email"], cfg["password"])
    return conn


# ── Polling ────────────────────────────────────────────────────────────────────

def fetch_new_messages(cfg: dict, folder: str = "INBOX") -> list[dict]:
    """Fetch all unseen messages from the given folder."""
    conn = _imap_connect(cfg)
    try:
        conn.select(f'"{folder}"')
        # Search for ALL messages; track by UID so we don't re-import
        _, data = conn.uid("search", None, "ALL")
        uids = data[0].split() if data[0] else []

        db = sqlite3.connect(DB_PATH)
        known = {
            row[0]
            for row in db.execute(
                "SELECT uid FROM messages WHERE folder = ?", (folder,)
            ).fetchall()
        }
        db.close()

        new_uids = [u for u in uids if u.decode() not in known]
        messages = []

        for uid_bytes in new_uids:
            uid = uid_bytes.decode()
            _, msg_data = conn.uid("fetch", uid_bytes, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            plain, html = _get_body(msg)
            recipients = _decode_header_value(msg.get("To", ""))
            messages.append(
                {
                    "uid": uid,
                    "folder": folder,
                    "message_id": msg.get("Message-ID", ""),
                    "sender": _decode_header_value(msg.get("From", "")),
                    "recipients": recipients,
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "body_plain": plain,
                    "body_html": html,
                    "date_sent": msg.get("Date", ""),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "raw_headers": str(msg.items()),
                }
            )
        return messages
    finally:
        conn.logout()


def store_messages(db: sqlite3.Connection, messages: list[dict]) -> int:
    count = 0
    for m in messages:
        try:
            db.execute(
                """INSERT OR IGNORE INTO messages
                   (uid, folder, message_id, sender, recipients, subject,
                    body_plain, body_html, date_sent, received_at, raw_headers)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["uid"], m["folder"], m["message_id"], m["sender"],
                    m["recipients"], m["subject"], m["body_plain"], m["body_html"],
                    m["date_sent"], m["received_at"], m["raw_headers"],
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                count += 1
        except sqlite3.Error as e:
            print(f"DB error: {e}")
    db.commit()
    return count


def list_folders(cfg: dict) -> list[str]:
    conn = _imap_connect(cfg)
    try:
        _, folder_list = conn.list()
        folders = []
        for item in folder_list:
            if item:
                parts = item.decode().split('"')
                name = parts[-2] if len(parts) >= 2 else item.decode().split()[-1]
                folders.append(name)
        return folders
    finally:
        conn.logout()


async def poll_loop(cfg: dict, interval: int = POLL_INTERVAL) -> None:
    global _last_poll
    folders_to_poll = cfg.get("poll_folders", "INBOX").split(",")
    while True:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Polling mailbox…")
        db = sqlite3.connect(DB_PATH)
        total_new = 0
        for folder in folders_to_poll:
            folder = folder.strip()
            try:
                msgs = fetch_new_messages(cfg, folder=folder)
                n = store_messages(db, msgs)
                total_new += n
                if n:
                    print(f"  {folder}: {n} new message(s)")
            except Exception as e:
                print(f"  Error polling {folder}: {e}")
        db.close()
        _last_poll = datetime.now(timezone.utc)
        print(f"  Total new: {total_new}")
        await asyncio.sleep(interval)


# ── Sending ────────────────────────────────────────────────────────────────────

def send_email(cfg: dict, to: str, subject: str, body: str) -> None:
    host = cfg["smtp_host"]
    port = int(cfg["smtp_port"])
    use_ssl = cfg.get("smtp_ssl", "false").lower() == "true"
    use_tls = cfg.get("smtp_tls", "true").lower() == "true"

    msg = MIMEMultipart("alternative")
    msg["From"] = cfg["email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if use_ssl:
        if host in ("127.0.0.1", "localhost"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            smtp = smtplib.SMTP_SSL(host, port, context=ctx)
        else:
            smtp = smtplib.SMTP_SSL(host, port)
    else:
        smtp = smtplib.SMTP(host, port)
        if use_tls:
            smtp.starttls()

    with smtp:
        smtp.login(cfg["email"], cfg["password"])
        smtp.sendmail(cfg["email"], to, msg.as_string())


# ── HTTP query ─────────────────────────────────────────────────────────────────

def query_messages(
    sender: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    until: str | None = None,
    folder: str | None = None,
    limit: int = 100,
) -> list[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    clauses: list[str] = []
    params: list = []

    if sender:
        clauses.append("sender LIKE ?")
        params.append(f"%{sender}%")
    if subject:
        clauses.append("subject LIKE ?")
        params.append(f"%{subject}%")
    if folder:
        clauses.append("folder = ?")
        params.append(folder)
    if since:
        clauses.append("date_sent >= ?")
        params.append(since)
    if until:
        clauses.append("date_sent <= ?")
        params.append(until)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM messages {where} ORDER BY date_sent DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    db.close()
    # Omit raw HTML and headers by default to keep responses lean
    result = []
    for r in rows:
        d = dict(r)
        d.pop("body_html", None)
        d.pop("raw_headers", None)
        result.append(d)
    return result


# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def __init__(self, cfg: dict, *args, **kwargs):
        self.cfg = cfg
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] {args[0]} {args[1]} {args[2]}")

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        def first(key: str) -> str | None:
            return qs[key][0] if key in qs else None

        if parsed.path == "/messages":
            try:
                limit = int(first("limit") or 100)
            except ValueError:
                self.send_json({"error": "'limit' must be an integer"}, 400)
                return
            messages = query_messages(
                sender=first("sender"),
                subject=first("subject"),
                since=first("since"),
                until=first("until"),
                folder=first("folder"),
                limit=limit,
            )
            self.send_json({"count": len(messages), "messages": messages})

        elif parsed.path == "/send":
            to = first("to")
            subject = first("subject") or "(no subject)"
            body = first("body")
            if not to or not body:
                self.send_json({"error": "missing 'to' or 'body' parameter"}, 400)
                return
            try:
                send_email(self.cfg, to, subject, body)
                self.send_json({"ok": True, "to": to, "subject": subject})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif parsed.path == "/folders":
            try:
                folders = list_folders(self.cfg)
                self.send_json({"folders": folders})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif parsed.path == "/status":
            db = sqlite3.connect(DB_PATH)
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            db.close()
            uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
            self.send_json(
                {
                    "uptime_seconds": int(uptime),
                    "last_poll": _last_poll.isoformat() if _last_poll else None,
                    "message_count": count,
                    "imap_host": self.cfg.get("imap_host"),
                    "imap_port": self.cfg.get("imap_port"),
                    "email": self.cfg.get("email"),
                    "poll_folders": self.cfg.get("poll_folders", "INBOX"),
                }
            )

        else:
            self.send_json({"error": "Not found"}, 404)


def run_server(cfg: dict) -> None:
    def make_handler(*args, **kwargs):
        return Handler(cfg, *args, **kwargs)

    print(f"HTTP server listening on http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), make_handler).serve_forever()


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="IMAP email polling daemon")
    p.add_argument("--imap-host", help="IMAP server hostname (e.g. 127.0.0.1 for Bridge)")
    p.add_argument("--imap-port", type=int, help="IMAP port (Bridge default: 1143)")
    p.add_argument("--imap-ssl", choices=["true", "false"], help="Use SSL for IMAP? (default: true)")
    p.add_argument("--smtp-host", help="SMTP server hostname")
    p.add_argument("--smtp-port", type=int, help="SMTP port (Bridge default: 1025)")
    p.add_argument("--smtp-ssl", choices=["true", "false"], help="Use SSL for SMTP? (default: false)")
    p.add_argument("--smtp-tls", choices=["true", "false"], help="Use STARTTLS for SMTP? (default: true)")
    p.add_argument("--email", help="Your email address")
    p.add_argument("--password", help="IMAP/SMTP password (Bridge password, not Protonmail login)")
    p.add_argument("--poll-folders", default="INBOX", help="Comma-separated folders to poll (default: INBOX)")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL, help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    p.add_argument("--port", type=int, default=PORT, help=f"HTTP server port (default: {PORT})")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    db = init_db()
    cfg = get_config(db)

    # Merge any CLI args into stored config
    cli_overrides = {
        "imap_host":     args.imap_host,
        "imap_port":     str(args.imap_port) if args.imap_port else None,
        "imap_ssl":      args.imap_ssl,
        "smtp_host":     args.smtp_host,
        "smtp_port":     str(args.smtp_port) if args.smtp_port else None,
        "smtp_ssl":      args.smtp_ssl,
        "smtp_tls":      args.smtp_tls,
        "email":         args.email,
        "password":      args.password,
        "poll_folders":  args.poll_folders,
    }
    updates = {k: v for k, v in cli_overrides.items() if v is not None}
    if updates:
        set_config(db, **updates)
        cfg.update(updates)
        print(f"Config updated: {', '.join(k for k in updates if k != 'password')}")

    db.close()

    required = ["imap_host", "imap_port", "smtp_host", "smtp_port", "email", "password"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"Error: missing config: {', '.join(missing)}")
        print("Run with --help for usage.")
        sys.exit(1)

    print(f"Using account: {cfg['email']}")
    print(f"IMAP: {cfg['imap_host']}:{cfg['imap_port']}  SMTP: {cfg['smtp_host']}:{cfg['smtp_port']}")
    print(f"Polling folders: {cfg.get('poll_folders', 'INBOX')} every {args.interval}s")

    threading.Thread(
        target=run_server,
        args=(cfg,),
        daemon=True,
    ).start()

    asyncio.run(poll_loop(cfg, interval=args.interval))
