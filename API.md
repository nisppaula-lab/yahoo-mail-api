# Yahoo Mail API

Small REST API for Yahoo Mail through IMAP/SMTP. Designed so an assistant can search compact summaries first and fetch full message bodies only when needed.

## Auth

All routes except `/health` and `/openapi.json` require:

```http
Authorization: Bearer <API_TOKEN>
```

## Environment variables

- `YAHOO_EMAIL`: Yahoo mailbox address
- `YAHOO_APP_PASSWORD`: Yahoo app password for IMAP/SMTP (not the normal account password)
- `API_TOKEN`: long random bearer token used by Muse's custom connector
- `PORT`: default `8080`
- Optional: `IMAP_HOST`, `IMAP_PORT`, `SMTP_HOST`, `SMTP_PORT`

Never commit real values. Keep them only in the host's environment/secret manager.

## Endpoints

### GET /health

Returns `{ "ok": true, "configured": true/false }`. Does not reveal secrets.

### GET /messages/search

Query params:

- `q`: text searched in subject/from/body
- `folder`: default `INBOX`
- `limit`: 1-100, default 20
- `unread`: `true` to return only unread
- `since`, `before`: IMAP dates like `08-Oct-2026`

Returns compact messages: `id`, `folder`, `from`, `to`, `subject`, `date`, `messageId`, `snippet`, `unread`, `flags`, `hasAttachments`.

Search/read uses `BODY.PEEK` and a read-only mailbox selection so messages are not accidentally marked read.

### GET /messages/{folder}/{id}

Fetches one full message: text/html, headers, flags, and attachment metadata. Attachment binary content is not returned.

### POST /messages/{folder}/{id}/read

Marks one message read.

### POST /messages/{folder}/{id}/unread

Marks one message unread.

### POST /messages/send

JSON body:

```json
{ "to": "person@example.com", "subject": "Hello", "text": "Body" }
```

## Local run

```bash
cp .env.example .env
# edit .env locally only
set -a; source .env; set +a
python3 app.py
curl http://localhost:8080/health
curl -H "Authorization: Bearer $API_TOKEN" "http://localhost:8080/messages/search?folder=INBOX&limit=5"
```

## Docker

```bash
docker build -t yahoo-mail-api .
docker run --env-file .env -p 8080:8080 yahoo-mail-api
```

## Muse connector setup

After this API is deployed at a public HTTPS URL, tell Muse the base URL and say: "setup a connector for this Yahoo Mail API." Muse should register a custom connector with auth scheme API key, placement `bearer_header`, and API host set to the deployed hostname. Enter the API token only in Muse's secure connector form, never in chat or in the repo.
