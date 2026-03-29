"""
email_daemon.py — IMAP email polling daemon with HTTP query interface.

Supports multiple email accounts. Accounts are stored as a JSON list in the
SQLite config table under the key 'accounts'. The daemon runs and serves HTTP
regardless of how many accounts are configured (including zero).

Usage
-----
Add an account:
    python email_daemon.py add \
        --imap-host 127.0.0.1 --imap-port 1143 \
        --smtp-host 127.0.0.1 --smtp-port 1025 \
        --email you@proton.me --password yourBridgePassword

List configured accounts:
    python email_daemon.py list

Remove an account:
    python email_daemon.py remove --email you@proton.me

Run the daemon (uses stored accounts):
    python email_daemon.py run

HTTP endpoints (default port 6001)
-----------------------------------
GET /messages
    ?account=  filter by account email address
    ?sender=   filter by From address (substring match)
    ?subject=  filter by subject (substring match)
    ?since=    ISO-8601 datetime, e.g. 2024-01-01T00:00:00
    ?until=    ISO-8601 datetime
    ?folder=   mailbox folder
    ?limit=    max rows to return (default: 100)

GET /send
    ?from=     sender account email (required if multiple accounts)
    ?to=       recipient address (required)
    ?subject=  email subject (default: "(no subject)")
    ?body=     message body (required)

GET /folders
    ?account=  account email address (required if multiple accounts)

GET /accounts
    Lists all configured accounts (passwords omitted).

GET /status
    Returns daemon uptime, last poll time, message count, account summary.
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
            account      TEXT,
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
            UNIQUE(account, uid, folder)
        )
    """)
    db.commit()
    return db


def load_accounts(db: sqlite3.Connection) -> list[dict]:
    row = db.execute("SELECT value FROM config WHERE key='accounts'").fetchone()
    if not row:
        return []
    return json.loads(row[0])


def save_accounts(db: sqlite3.Connection, accounts: list[dict]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('accounts', ?)",
        (json.dumps(accounts),),
    )
    db.commit()


# ── Account management ─────────────────────────────────────────────────────────

def add_account(db: sqlite3.Connection, account: dict) -> None:
    accounts = load_accounts(db)
    accounts = [a for a in accounts if a["email"] != account["email"]]
    accounts.append(account)
    save_accounts(db, accounts)
    print(f"Account saved: {account['email']}")


def remove_account(db: sqlite3.Connection, email: str) -> None:
    accounts = load_accounts(db)
    before = len(accounts)
    accounts = [a for a in accounts if a["email"] != email]
    if len(accounts) == before:
        print(f"Account not found: {email}")
    else:
        save_accounts(db, accounts)
        print(f"Account removed: {email}")


def get_account(accounts: list[dict], email: str | None) -> dict | None:
    if not accounts:
        return None
    if email:
        matches = [a for a in accounts if a["email"] == email]
        return matches[0] if matches else None
    if len(accounts) == 1:
        return accounts[0]
    return None  # ambiguous — caller must specify


# ── Email helpers ──────────────────────────────────────────────────────────────

def _decode_header_value(raw: str | bytes | None) -> str:
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

def _imap_connect(acct: dict) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    host = acct["imap_host"]
    port = int(acct["imap_port"])
    use_ssl = acct.get("imap_ssl", "true").lower() == "true"

    if use_ssl:
        if host in ("127.0.0.1", "localhost"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
        else:
            conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)

    conn.login(acct["email"], acct["password"])
    return conn


# ── Polling ────────────────────────────────────────────────────────────────────

def fetch_new_messages(acct: dict, folder: str = "INBOX") -> list[dict]:
    conn = _imap_connect(acct)
    try:
        conn.select(f'"{folder}"')
        _, data = conn.uid("search", None, "ALL")
        uids = data[0].split() if data[0] else []

        db = sqlite3.connect(DB_PATH)
        known = {
            row[0]
            for row in db.execute(
                "SELECT uid FROM messages WHERE account = ? AND folder = ?",
                (acct["email"], folder),
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
            messages.append(
                {
                    "account": acct["email"],
                    "uid": uid,
                    "folder": folder,
                    "message_id": msg.get("Message-ID", ""),
                    "sender": _decode_header_value(msg.get("From", "")),
                    "recipients": _decode_header_value(msg.get("To", "")),
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
                   (account, uid, folder, message_id, sender, recipients, subject,
                    body_plain, body_html, date_sent, received_at, raw_headers)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["account"], m["uid"], m["folder"], m["message_id"],
                    m["sender"], m["recipients"], m["subject"], m["body_plain"],
                    m["body_html"], m["date_sent"], m["received_at"], m["raw_headers"],
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                count += 1
        except sqlite3.Error as e:
            print(f"DB error: {e}")
    db.commit()
    return count


def list_folders(acct: dict) -> list[str]:
    conn = _imap_connect(acct)
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


async def poll_loop(interval: int = POLL_INTERVAL) -> None:
    global _last_poll
    while True:
        db = sqlite3.connect(DB_PATH)
        accounts = load_accounts(db)
        db.close()

        if not accounts:
            print(f"[{datetime.now(timezone.utc).isoformat()}] No accounts configured — skipping poll.")
            await asyncio.sleep(interval)
            continue

        print(f"[{datetime.now(timezone.utc).isoformat()}] Polling {len(accounts)} account(s)…")
        for acct in accounts:
            folders_to_poll = acct.get("poll_folders", "INBOX").split(",")
            db = sqlite3.connect(DB_PATH)
            for folder in folders_to_poll:
                folder = folder.strip()
                try:
                    msgs = fetch_new_messages(acct, folder=folder)
                    n = store_messages(db, msgs)
                    if n:
                        print(f"  {acct['email']} / {folder}: {n} new message(s)")
                except Exception as e:
                    print(f"  Error polling {acct['email']} / {folder}: {e}")
            db.close()

        _last_poll = datetime.now(timezone.utc)
        await asyncio.sleep(interval)


# ── Sending ────────────────────────────────────────────────────────────────────

def send_email(acct: dict, to: str, subject: str, body: str) -> None:
    host = acct["smtp_host"]
    port = int(acct["smtp_port"])
    use_ssl = acct.get("smtp_ssl", "false").lower() == "true"
    use_tls = acct.get("smtp_tls", "true").lower() == "true"

    msg = MIMEMultipart("alternative")
    msg["From"] = acct["email"]
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
        smtp.login(acct["email"], acct["password"])
        smtp.sendmail(acct["email"], to, msg.as_string())


# ── HTTP query ─────────────────────────────────────────────────────────────────

def query_messages(
    account: str | None = None,
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

    if account:
        clauses.append("account = ?")
        params.append(account)
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
    result = []
    for r in rows:
        d = dict(r)
        d.pop("body_html", None)
        d.pop("raw_headers", None)
        result.append(d)
    return result


# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
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
                account=first("account"),
                sender=first("sender"),
                subject=first("subject"),
                since=first("since"),
                until=first("until"),
                folder=first("folder"),
                limit=limit,
            )
            self.send_json({"count": len(messages), "messages": messages})

        elif parsed.path == "/send":
            db = sqlite3.connect(DB_PATH)
            accounts = load_accounts(db)
            db.close()
            acct = get_account(accounts, first("from"))
            if not acct:
                self.send_json(
                    {"error": "specify ?from= (or add an account first)" if not accounts
                     else "specify ?from= to disambiguate between multiple accounts"},
                    400,
                )
                return
            to = first("to")
            body = first("body")
            if not to or not body:
                self.send_json({"error": "missing 'to' or 'body' parameter"}, 400)
                return
            subject = first("subject") or "(no subject)"
            try:
                send_email(acct, to, subject, body)
                self.send_json({"ok": True, "from": acct["email"], "to": to, "subject": subject})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif parsed.path == "/folders":
            db = sqlite3.connect(DB_PATH)
            accounts = load_accounts(db)
            db.close()
            acct = get_account(accounts, first("account"))
            if not acct:
                self.send_json(
                    {"error": "specify ?account= (or add an account first)" if not accounts
                     else "specify ?account= to disambiguate between multiple accounts"},
                    400,
                )
                return
            try:
                folders = list_folders(acct)
                self.send_json({"account": acct["email"], "folders": folders})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif parsed.path == "/accounts":
            db = sqlite3.connect(DB_PATH)
            accounts = load_accounts(db)
            db.close()
            safe = [{k: v for k, v in a.items() if k != "password"} for a in accounts]
            self.send_json({"count": len(safe), "accounts": safe})

        elif parsed.path == "/status":
            db = sqlite3.connect(DB_PATH)
            accounts = load_accounts(db)
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            db.close()
            uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
            self.send_json(
                {
                    "uptime_seconds": int(uptime),
                    "last_poll": _last_poll.isoformat() if _last_poll else None,
                    "message_count": count,
                    "accounts": [a["email"] for a in accounts],
                }
            )

        else:
            self.send_json({"error": "Not found"}, 404)


def run_server() -> None:
    print(f"HTTP server listening on http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="IMAP email polling daemon")
    sub = p.add_subparsers(dest="command")

    # run (default)
    run_p = sub.add_parser("run", help="Start the daemon (default)")
    run_p.add_argument("--interval", type=int, default=POLL_INTERVAL)
    run_p.add_argument("--port", type=int, default=PORT)

    # add
    add_p = sub.add_parser("add", help="Add or update an account")
    add_p.add_argument("--email", required=True)
    add_p.add_argument("--password", required=True)
    add_p.add_argument("--imap-host", required=True)
    add_p.add_argument("--imap-port", type=int, required=True)
    add_p.add_argument("--imap-ssl", choices=["true", "false"], default="true")
    add_p.add_argument("--smtp-host", required=True)
    add_p.add_argument("--smtp-port", type=int, required=True)
    add_p.add_argument("--smtp-ssl", choices=["true", "false"], default="false")
    add_p.add_argument("--smtp-tls", choices=["true", "false"], default="true")
    add_p.add_argument("--poll-folders", default="INBOX")

    # remove
    rm_p = sub.add_parser("remove", help="Remove an account")
    rm_p.add_argument("--email", required=True)

    # list
    sub.add_parser("list", help="List configured accounts")

    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    db = init_db()

    if args.command == "add":
        add_account(db, {
            "email":        args.email,
            "password":     args.password,
            "imap_host":    args.imap_host,
            "imap_port":    str(args.imap_port),
            "imap_ssl":     args.imap_ssl,
            "smtp_host":    args.smtp_host,
            "smtp_port":    str(args.smtp_port),
            "smtp_ssl":     args.smtp_ssl,
            "smtp_tls":     args.smtp_tls,
            "poll_folders": args.poll_folders,
        })
        db.close()

    elif args.command == "remove":
        remove_account(db, args.email)
        db.close()

    elif args.command == "list":
        accounts = load_accounts(db)
        db.close()
        if not accounts:
            print("No accounts configured.")
        for a in accounts:
            print(f"  {a['email']}  IMAP {a['imap_host']}:{a['imap_port']}  SMTP {a['smtp_host']}:{a['smtp_port']}  folders={a.get('poll_folders','INBOX')}")

    else:  # run or no subcommand
        interval = getattr(args, "interval", POLL_INTERVAL)
        db.close()
        threading.Thread(target=run_server, daemon=True).start()
        asyncio.run(poll_loop(interval=interval))
