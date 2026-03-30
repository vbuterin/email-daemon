---
name: email-daemon
description: "Use this skill whenever you need to read emails or send an email via the email-daemon HTTP API. Triggers include: reading recent emails, querying messages by sender or subject, sending an email, listing mailbox folders, or any interaction with the local email-daemon running on port 6001."
---

# Email Daemon HTTP API

## Overview

A local HTTP daemon runs on `http://localhost:6001` and exposes five endpoints:
- `GET /messages` — query received emails from the local SQLite store
- `GET /send` — send an email
- `GET /folders` — list all IMAP folders for an account
- `GET /accounts` — list all configured accounts
- `GET /status` — check daemon health, last poll time, and message count

Timestamps/dates use **ISO 8601** format (e.g. `2026-01-15T09:00:00`).

**Important:** Sending to self completes immediately. Sending to anyone else returns a `confirm_url` that **must be shown to the user** — the email is not sent until the user opens that URL in their browser and clicks "Send".

> **Warning:** Message responses can be very long. In many circumstances it is more efficient to save the output to a file and then search or chunk it rather than reading it directly:
> ```bash
> curl "http://localhost:6001/messages" > /tmp/messages.json
> cat /tmp/messages.json | python3 -c "import json,sys; msgs = json.load(sys.stdin)['messages']; [print(m['date_sent'], m['sender'], m['subject']) for m in msgs]"
> ```

---

## Managing Accounts

### List configured accounts
```bash
curl "http://localhost:6001/accounts"
```

### Response format
```json
{
  "count": 2,
  "accounts": [
    {
      "email": "bob@proton.me",
      "imap_host": "127.0.0.1",
      "imap_port": "1143",
      "smtp_host": "127.0.0.1",
      "smtp_port": "1025",
      "poll_folders": "INBOX"
    }
  ]
}
```

### Add an account (CLI, not HTTP)
```bash
sudo -u email-daemon python3 /etc/email-daemon/email_daemon.py add \
  --email you@proton.me --password BRIDGE_PASSWORD \
  --imap-host 127.0.0.1 --imap-port 1143 \
  --smtp-host 127.0.0.1 --smtp-port 1025
```

### Remove an account (CLI, not HTTP)
```bash
sudo -u email-daemon python3 /etc/email-daemon/email_daemon.py remove \
  --email you@proton.me
```

---

## Reading Messages

### All messages (most recent 100, all accounts)
```bash
curl "http://localhost:6001/messages"
```

### All messages for a specific account
```bash
curl --get "http://localhost:6001/messages" --data-urlencode "account=bob@proton.me"
```

### Messages from the last hour
```bash
curl --get "http://localhost:6001/messages" \
  --data-urlencode "since=$(python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())')"
```

### Messages from the last 24 hours
```bash
curl --get "http://localhost:6001/messages" \
  --data-urlencode "since=$(python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=1)).isoformat())')"
```

### Filter by sender (substring match)
```bash
curl --get "http://localhost:6001/messages" --data-urlencode "sender=alice@example.com"
```

### Filter by subject (substring match)
```bash
curl --get "http://localhost:6001/messages" --data-urlencode "subject=invoice"
```

### Filter by folder
```bash
curl "http://localhost:6001/messages?folder=Sent"
```

### Combine filters: account + sender + time window
```bash
curl --get "http://localhost:6001/messages" \
  --data-urlencode "account=bob@proton.me" \
  --data-urlencode "sender=alice@example.com" \
  --data-urlencode "since=2026-03-01T00:00:00"
```

### Limit number of results
```bash
curl "http://localhost:6001/messages?limit=10"
```

### Between two dates
```bash
curl --get "http://localhost:6001/messages" \
  --data-urlencode "since=2026-03-01T00:00:00" \
  --data-urlencode "until=2026-03-31T23:59:59"
```

### Response format
```json
{
  "count": 2,
  "messages": [
    {
      "id": 1,
      "account": "bob@proton.me",
      "uid": "1042",
      "folder": "INBOX",
      "message_id": "<abc123@mail.proton.me>",
      "sender": "Alice <alice@example.com>",
      "recipients": "bob@proton.me",
      "subject": "Hello there",
      "body_plain": "Hi Bob, just checking in...",
      "date_sent": "Sat, 28 Mar 2026 12:34:56 +0000",
      "received_at": "2026-03-28T12:35:10.123456+00:00"
    }
  ]
}
```

### Notes
- `body_html` and `raw_headers` are intentionally omitted from responses to keep output lean
- `sender` and `subject` filters are **substring matches** (case-insensitive via LIKE)
- Results are ordered by `date_sent` descending (newest first)
- `received_at` is when the daemon's poller collected the message (UTC)
- `date_sent` is the RFC 2822 date string from the email header
- `account` filter is optional when only one account is configured

---

## Sending Email

### Send to self (no confirmation required)
When `to` matches the sender account, the email is sent immediately.

```bash
curl --get "http://localhost:6001/send" \
  --data-urlencode "from=bob@proton.me" \
  --data-urlencode "to=bob@proton.me" \
  --data-urlencode "subject=Note to self" \
  --data-urlencode "body=Remember to do the thing."
```

### Response format (sent immediately)
```json
{
  "ok": true,
  "from": "bob@proton.me",
  "to": "bob@proton.me",
  "subject": "Note to self"
}
```

---

### Send to another person — requires user confirmation

```bash
curl --get "http://localhost:6001/send" \
  --data-urlencode "from=bob@proton.me" \
  --data-urlencode "to=alice@example.com" \
  --data-urlencode "subject=Hello" \
  --data-urlencode "body=Hi Alice, this is Bob."
```

### Response format (confirmation required)
```json
{
  "pending": true,
  "confirm_url": "http://localhost:7001/confirm?token=...",
  "from": "bob@proton.me",
  "to": "alice@example.com",
  "subject": "Hello",
  "message": "Open confirm_url in your browser to approve or deny this email."
}
```

> **When you receive a `pending: true` response, you must present the `confirm_url` to the user.** The email has NOT been sent yet. The user must open the URL in their browser and click "Send" to approve, or "Don't send" to cancel. Do not silently discard the URL.

### Notes
- `from=` is optional when only one account is configured
- `subject=` defaults to `(no subject)` if omitted

---

## Listing Folders

### List folders for a specific account
```bash
curl --get "http://localhost:6001/folders" --data-urlencode "account=bob@proton.me"
```

### List folders when only one account is configured (account= optional)
```bash
curl "http://localhost:6001/folders"
```

### Response format
```json
{
  "account": "bob@proton.me",
  "folders": ["INBOX", "Sent", "Drafts", "Trash", "Archive", "Spam"]
}
```

---

## Checking Daemon Status

```bash
curl "http://localhost:6001/status"
```

### Response format
```json
{
  "uptime_seconds": 3600,
  "last_poll": "2026-03-28T23:37:07.163682+00:00",
  "message_count": 142,
  "pending_confirmations": 0,
  "accounts": ["bob@proton.me", "alice@proton.me"]
}
```

---

## Query Parameter Reference

| Parameter | Endpoint  | Type   | Description                                                        |
|-----------|-----------|--------|--------------------------------------------------------------------|
| `account` | /messages | string | Filter by account email (optional if only one account configured)  |
| `sender`  | /messages | string | Substring match against From address                               |
| `subject` | /messages | string | Substring match against subject line                               |
| `since`   | /messages | string | ISO 8601 datetime, inclusive lower bound on date_sent              |
| `until`   | /messages | string | ISO 8601 datetime, inclusive upper bound on date_sent              |
| `folder`  | /messages | string | Exact folder name (default: no filter, all folders)                |
| `limit`   | /messages | int    | Max messages to return (default: 100)                              |
| `from`    | /send     | string | Sender account email (optional if only one account configured)     |
| `to`      | /send     | string | Recipient email address (required)                                 |
| `subject` | /send     | string | Email subject (default: "(no subject)")                            |
| `body`    | /send     | string | Plain-text message body (required)                                 |
| `account` | /folders  | string | Account email (optional if only one account configured)            |

---

## Datetime Quick Reference

```bash
# Now (UTC)
python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())'

# 10 minutes ago
python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())'

# 1 hour ago
python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())'

# 24 hours ago
python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=1)).isoformat())'

# 7 days ago
python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())'

# Start of today (UTC)
python3 -c 'from datetime import datetime, timezone; t = datetime.now(timezone.utc); print(t.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())'
```
