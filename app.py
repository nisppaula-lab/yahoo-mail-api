import base64
import email
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from collections import defaultdict, deque
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

YAHOO_EMAIL = os.getenv("YAHOO_EMAIL", "")
YAHOO_APP_PASSWORD = os.getenv("YAHOO_APP_PASSWORD", "")
API_TOKEN = os.getenv("API_TOKEN", "")
IMAP_HOST = os.getenv("IMAP_HOST", "imap.mail.yahoo.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.yahoo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "1048576"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))

_hits = defaultdict(deque)

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

def require_config():
    missing = [name for name, value in {
        "YAHOO_EMAIL": YAHOO_EMAIL,
        "YAHOO_APP_PASSWORD": YAHOO_APP_PASSWORD,
        "API_TOKEN": API_TOKEN,
    }.items() if not value]
    if missing:
        raise ApiError(500, "Server is missing required environment variables: " + ", ".join(missing))

def constant_time_equal(a, b):
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode(), b.encode()):
        result |= x ^ y
    return result == 0

def check_rate_limit(client_ip):
    now = time.time()
    bucket = _hits[client_ip]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        raise ApiError(429, "Rate limit exceeded")
    bucket.append(now)

def decode_mime(value):
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)

def clean_text(value, limit=500):
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:limit]

def extract_text(msg, max_chars=20000):
    text_body = ""
    html_body = ""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            if disp == "attachment" or filename:
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "filename": decode_mime(filename) if filename else None,
                    "contentType": ctype,
                    "size": len(payload),
                })
                continue
            if ctype == "text/plain" and not text_body:
                text_body = part.get_content() if hasattr(part, "get_content") else decode_payload(part)
            elif ctype == "text/html" and not html_body:
                html_body = part.get_content() if hasattr(part, "get_content") else decode_payload(part)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html_body = msg.get_content() if hasattr(msg, "get_content") else decode_payload(msg)
        else:
            text_body = msg.get_content() if hasattr(msg, "get_content") else decode_payload(msg)
    return clean_text(text_body, max_chars), html_body[:max_chars], attachments

def decode_payload(part):
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")

def parse_message(raw, uid, folder, include_full=False):
    msg = email.message_from_bytes(raw, policy=default)
    text_body, html_body, attachments = extract_text(msg, max_chars=50000 if include_full else 4000)
    snippet_source = text_body or re.sub(r"<[^>]+>", " ", html_body)
    item = {
        "id": str(uid),
        "folder": folder,
        "from": decode_mime(msg.get("From")),
        "to": decode_mime(msg.get("To")),
        "subject": decode_mime(msg.get("Subject")),
        "date": format_date(msg.get("Date")),
        "messageId": msg.get("Message-ID"),
        "snippet": clean_text(snippet_source, 300),
        "hasAttachments": bool(attachments),
    }
    if include_full:
        item.update({
            "text": text_body,
            "html": html_body or None,
            "attachments": attachments,
            "headers": {k: decode_mime(v) for k, v in msg.items()},
        })
    return item

def format_date(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except Exception:
        return str(value)

def imap_connect(readonly=True):
    require_config()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=30)
    client.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
    return client

def select_folder(client, folder, readonly=True):
    typ, data = client.select(folder, readonly=readonly)
    if typ != "OK":
        raise ApiError(404, f"Folder not found or not selectable: {folder}")

def quote_imap(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

def search_uids(client, folder, params):
    criteria = []
    if params.get("unread", ["false"])[0].lower() == "true":
        criteria.append("UNSEEN")
    since = params.get("since", [None])[0]
    before = params.get("before", [None])[0]
    if since:
        criteria.append(f"SINCE {since}")
    if before:
        criteria.append(f"BEFORE {before}")
    q = params.get("q", [""])[0].strip()
    if q:
        safe = quote_imap(q)
        criteria.append(f"(OR OR SUBJECT {safe} FROM {safe} TEXT {safe})")
    if not criteria:
        criteria = ["ALL"]
    typ, data = client.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise ApiError(502, "IMAP search failed")
    if not data or not data[0]:
        return []
    return data[0].decode().split()

def fetch_flags(client, uid):
    typ, data = client.uid("FETCH", uid, "(FLAGS)")
    if typ != "OK" or not data:
        return []
    raw = data[0].decode(errors="ignore") if isinstance(data[0], bytes) else str(data[0])
    m = re.search(r"FLAGS \((.*?)\)", raw)
    return m.group(1).split() if m else []

def fetch_raw(client, uid, full=False):
    limit = "" if full else "<0.65536>"
    typ, data = client.uid("FETCH", uid, f"(BODY.PEEK[]{limit} FLAGS INTERNALDATE RFC822.SIZE)")
    if typ != "OK" or not data:
        raise ApiError(502, "IMAP fetch failed")
    raw = b""
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            raw = part[1]
            break
    return raw

def list_messages(params):
    folder = params.get("folder", ["INBOX"])[0] or "INBOX"
    limit = min(max(int(params.get("limit", ["20"])[0]), 1), 100)
    client = imap_connect(readonly=True)
    try:
        select_folder(client, folder, readonly=True)
        uids = search_uids(client, folder, params)
        chosen = list(reversed(uids[-limit:]))
        results = []
        for uid in chosen:
            raw = fetch_raw(client, uid, full=False)
            item = parse_message(raw, uid, folder, include_full=False)
            flags = fetch_flags(client, uid)
            item["unread"] = "\\Seen" not in flags
            item["flags"] = flags
            results.append(item)
        return {"folder": folder, "count": len(results), "messages": results}
    finally:
        try:
            client.logout()
        except Exception:
            pass

def get_message(folder, uid):
    client = imap_connect(readonly=True)
    try:
        select_folder(client, folder, readonly=True)
        raw = fetch_raw(client, uid, full=True)
        item = parse_message(raw, uid, folder, include_full=True)
        flags = fetch_flags(client, uid)
        item["unread"] = "\\Seen" not in flags
        item["flags"] = flags
        return item
    finally:
        try:
            client.logout()
        except Exception:
            pass

def set_seen(folder, uid, seen=True):
    client = imap_connect(readonly=False)
    try:
        select_folder(client, folder, readonly=False)
        flag = "+FLAGS.SILENT" if seen else "-FLAGS.SILENT"
        typ, data = client.uid("STORE", uid, flag, "(\\Seen)")
        if typ != "OK":
            raise ApiError(502, "Failed to update message flags")
        return {"id": str(uid), "folder": folder, "unread": not seen}
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass

def send_message(payload):
    to = payload.get("to")
    subject = payload.get("subject", "")
    text = payload.get("text", "")
    if not to or not isinstance(to, str):
        raise ApiError(400, "Field 'to' is required")
    if len(text.encode()) > MAX_BODY_BYTES:
        raise ApiError(413, "Message body too large")
    msg = EmailMessage()
    msg["From"] = YAHOO_EMAIL
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
        server.login(YAHOO_EMAIL, YAHOO_APP_PASSWORD)
        server.send_message(msg)
    return {"sent": True, "to": to, "subject": subject}

OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Yahoo Mail API", "version": "1.0.0"},
    "servers": [{"url": "/"}],
    "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearerAuth": []}],
    "paths": {
        "/health": {"get": {"summary": "Health check", "security": [], "responses": {"200": {"description": "OK"}}}},
        "/messages/search": {"get": {"summary": "Search compact message summaries", "parameters": [
            {"name": "q", "in": "query", "schema": {"type": "string"}},
            {"name": "folder", "in": "query", "schema": {"type": "string", "default": "INBOX"}},
            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20, "maximum": 100}},
            {"name": "unread", "in": "query", "schema": {"type": "boolean"}},
            {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "IMAP date, e.g. 08-Oct-2026"},
            {"name": "before", "in": "query", "schema": {"type": "string"}, "description": "IMAP date, e.g. 09-Oct-2026"},
        ], "responses": {"200": {"description": "Compact message list"}}}},
        "/messages/{folder}/{id}": {"get": {"summary": "Fetch one full message", "parameters": [
            {"name": "folder", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ], "responses": {"200": {"description": "Full message"}}}},
        "/messages/{folder}/{id}/read": {"post": {"summary": "Mark read", "responses": {"200": {"description": "Updated"}}}},
        "/messages/{folder}/{id}/unread": {"post": {"summary": "Mark unread", "responses": {"200": {"description": "Updated"}}}},
        "/messages/send": {"post": {"summary": "Send email", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["to"], "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "text": {"type": "string"}}}}}}, "responses": {"200": {"description": "Sent"}}}},
    },
}

class Handler(BaseHTTPRequestHandler):
    server_version = "YahooMailAPI/1.0"

    def log_message(self, fmt, *args):
        # Do not log bodies, headers, tokens, or message content.
        print(f"{self.client_address[0]} {self.command} {self.path.split('?')[0]}")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            raise ApiError(400, "Invalid JSON")

    def authorize(self):
        if not API_TOKEN:
            raise ApiError(500, "API_TOKEN is not configured")
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise ApiError(401, "Missing Bearer token")
        token = auth.removeprefix("Bearer ").strip()
        if not constant_time_equal(token, API_TOKEN):
            raise ApiError(401, "Invalid token")

    def handle_route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        try:
            check_rate_limit(self.client_address[0])
            if method == "GET" and path == "/health":
                return self.send_json(200, {"ok": True, "configured": bool(YAHOO_EMAIL and YAHOO_APP_PASSWORD and API_TOKEN)})
            if method == "GET" and path == "/openapi.json":
                return self.send_json(200, OPENAPI)
            self.authorize()
            if method == "GET" and path == "/messages/search":
                return self.send_json(200, list_messages(params))
            m = re.match(r"^/messages/([^/]+)/([^/]+)$", path)
            if method == "GET" and m:
                return self.send_json(200, get_message(m.group(1), m.group(2)))
            m = re.match(r"^/messages/([^/]+)/([^/]+)/(read|unread)$", path)
            if method == "POST" and m:
                return self.send_json(200, set_seen(m.group(1), m.group(2), seen=(m.group(3) == "read")))
            if method == "POST" and path == "/messages/send":
                return self.send_json(200, send_message(self.read_json()))
            raise ApiError(404, "Route not found")
        except ApiError as e:
            return self.send_json(e.status, {"error": e.message})
        except imaplib.IMAP4.error as e:
            return self.send_json(502, {"error": f"IMAP error: {str(e)[:160]}"})
        except Exception as e:
            return self.send_json(500, {"error": f"Internal error: {str(e)[:160]}"})

    def do_GET(self):
        self.handle_route("GET")

    def do_POST(self):
        self.handle_route("POST")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
