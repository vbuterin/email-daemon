"""
email_daemon.py — IMAP email polling daemon with HTTP query interface.

Supports multiple email accounts. Accounts are stored as a JSON list in the
SQLite config table under the key 'accounts'. The daemon runs and serves HTTP
regardless of how many accounts are configured (including zero).

Two HTTP servers run:
  Port 6001 — main API (safe to expose to untrusted software)
  Port 7001 — confirmation UI (human-facing only; keep away from untrusted software)

When /send is called with a recipient different from the sender, the email is
held pending and a confirmation URL on port 7001 is returned. The human must
open that URL in a browser and click "Send" to approve.

Usage
-----
Add an account:
    python email_daemon.py add \\
        --imap-host 127.0.0.1 --imap-port 1143 \\
        --smtp-host 127.0.0.1 --smtp-port 1025 \\
        --email you@proton.me --password yourBridgePassword

List configured accounts:
    python email_daemon.py list

Remove an account:
    python email_daemon.py remove --email you@proton.me

Run the daemon (uses stored accounts):
    python email_daemon.py run

HTTP endpoints (port 6001)
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
    Returns immediately. If to != from, returns a confirm_url instead of sending.

GET /folders
    ?account=  account email address (required if multiple accounts)

GET /accounts
    Lists all configured accounts (passwords omitted).

GET /status
    Returns daemon uptime, last poll time, message count, account summary.

Confirmation endpoints (port 7001 — human-facing only)
-------------------------------------------------------
GET /confirm?token=...   Show confirmation page
GET /approve?token=...   Send the email
GET /deny?token=...      Cancel the email
"""

import asyncio
import email as email_lib
import html as html_lib
import imaplib
import json
import os
import secrets
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
CONFIRM_PORT = 7001  # human-facing confirmation UI — keep this port away from untrusted software
POLL_INTERVAL = 60  # seconds

_start_time = datetime.now(timezone.utc)
_last_poll: datetime | None = None

# In-memory store of pending outbound emails awaiting confirmation
# { token: { acct, to, subject, body, created_at } }
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

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
    plain, body_html = "", ""
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
            elif ct == "text/html" and not body_html:
                body_html = text
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload:
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                body_html = text
            else:
                plain = text
    return plain, body_html


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
            plain, body_html = _get_body(msg)
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
                    "body_html": body_html,
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


# ── Main API server (port 6001) ────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] {' '.join(str(a) for a in args)}")

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

            # Sending to self: send immediately
            if to.strip().lower() == acct["email"].strip().lower():
                try:
                    send_email(acct, to, subject, body)
                    self.send_json({"ok": True, "from": acct["email"], "to": to, "subject": subject})
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
            else:
                # Sending to someone else: require human confirmation
                token = secrets.token_urlsafe(32)
                with _pending_lock:
                    _pending[token] = {
                        "acct": acct,
                        "to": to,
                        "subject": subject,
                        "body": body,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                confirm_url = f"http://localhost:{CONFIRM_PORT}/confirm?token={token}"
                print(f"[{datetime.now().isoformat()}] Confirmation required: {confirm_url}")
                self.send_json({
                    "pending": True,
                    "confirm_url": confirm_url,
                    "from": acct["email"],
                    "to": to,
                    "subject": subject,
                    "message": "Open confirm_url in your browser to approve or deny this email.",
                })

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
            with _pending_lock:
                pending_count = len(_pending)
            self.send_json(
                {
                    "uptime_seconds": int(uptime),
                    "last_poll": _last_poll.isoformat() if _last_poll else None,
                    "message_count": count,
                    "pending_confirmations": pending_count,
                    "accounts": [a["email"] for a in accounts],
                }
            )

        else:
            self.send_json({"error": "Not found"}, 404)


# ── Confirmation server (port 7001, human-facing) ──────────────────────────────

def _page(title: str, body_content: str) -> str:
    e = html_lib.escape
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{e(title)}</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 4em auto; padding: 0 1em; color: #111; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.5em 0; }}
  td {{ padding: 10px 12px; vertical-align: top; }}
  tr:nth-child(even) {{ background: #f5f5f5; }}
  .label {{ font-weight: bold; width: 80px; }}
  .body-text {{ white-space: pre-wrap; font-family: monospace; font-size: 0.9em; }}
  .actions {{ display: flex; gap: 1em; margin-top: 2em; }}
  a.btn {{ padding: 12px 28px; text-decoration: none; border-radius: 6px; font-size: 1.05em; color: white; }}
  a.send {{ background: #2563eb; }}
  a.deny {{ background: #dc2626; }}
  .meta {{ color: #888; font-size: 0.85em; margin-top: 2em; }}
</style>
</head><body>
{body_content}
</body></html>"""


class ConfirmHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat()}] confirm: {' '.join(str(a) for a in args)}")

    def send_html(self, content: str, status: int = 200) -> None:
        data = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        token = qs.get("token", [None])[0]
        e = html_lib.escape

        if parsed.path == "/confirm":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.get(token)
            if not pending:
                self.send_html(_page("Not found", "<h2>&#x274C; Not found</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            body_content = f"""
<h2>&#x2709;&#xFE0F; Confirm outbound email</h2>
<table>
  <tr><td class="label">From</td><td>{e(pending['acct']['email'])}</td></tr>
  <tr><td class="label">To</td><td>{e(pending['to'])}</td></tr>
  <tr><td class="label">Subject</td><td>{e(pending['subject'])}</td></tr>
  <tr><td class="label">Body</td><td class="body-text">{e(pending['body'])}</td></tr>
</table>
<div class="actions">
  <a class="btn send" href="/approve?token={e(token)}">&#x2714; Send</a>
  <a class="btn deny" href="/deny?token={e(token)}">&#x2716; Don't send</a>
</div>
<p class="meta">Requested at {e(pending['created_at'])}</p>"""
            self.send_html(_page("Confirm send", body_content))

        elif parsed.path == "/approve":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.pop(token, None)
            if not pending:
                self.send_html(_page("Already handled", "<h2>&#x274C; Already handled</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            try:
                send_email(pending["acct"], pending["to"], pending["subject"], pending["body"])
                print(f"[{datetime.now().isoformat()}] Confirmed and sent to {pending['to']}")
                body_content = f"<h2>&#x2705; Email sent</h2><p>Sent to <strong>{e(pending['to'])}</strong> &mdash; <em>{e(pending['subject'])}</em></p>"
                self.send_html(_page("Sent", body_content))
            except Exception as ex:
                body_content = f"<h2>&#x274C; Send failed</h2><pre>{e(str(ex))}</pre>"
                self.send_html(_page("Error", body_content), 500)

        elif parsed.path == "/deny":
            if not token:
                self.send_html(_page("Error", "<h2>Missing token</h2>"), 400)
                return
            with _pending_lock:
                pending = _pending.pop(token, None)
            if not pending:
                self.send_html(_page("Already handled", "<h2>&#x274C; Already handled</h2><p>This link is invalid or has already been used.</p>"), 404)
                return
            print(f"[{datetime.now().isoformat()}] Denied send to {pending['to']}")
            body_content = f"<h2>&#x1F6AB; Cancelled</h2><p>The email to <strong>{e(pending['to'])}</strong> was not sent.</p>"
            self.send_html(_page("Cancelled", body_content))

        else:
            self.send_html(_page("Not found", "<h1>Not found</h1>"), 404)


def run_server() -> None:
    print(f"API server listening on http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def run_confirm_server() -> None:
    print(f"Confirmation server listening on http://localhost:{CONFIRM_PORT} (human-facing)")
    HTTPServer(("127.0.0.1", CONFIRM_PORT), ConfirmHandler).serve_forever()


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
        threading.Thread(target=run_confirm_server, daemon=True).start()
        asyncio.run(poll_loop(interval=interval))
