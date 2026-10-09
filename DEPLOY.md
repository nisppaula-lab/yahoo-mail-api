# Deploy guide

## What you need

1. A Yahoo app password for the mailbox.
2. A host that can run a small Python service and keep environment variables secret.
3. A public HTTPS URL for the service.
4. A long random `API_TOKEN` that Muse will store in its secure connector form.

## Generic Docker deploy

```bash
git clone <your-repo-url>
cd yahoo-mail-api
cp .env.example .env
# Fill in real values only on the server/host, never in git.
docker build -t yahoo-mail-api .
docker run --env-file .env -p 8080:8080 yahoo-mail-api
```

Put a reverse proxy or platform HTTPS endpoint in front of it. Confirm:

```bash
curl https://your-domain.example/health
curl -H "Authorization: Bearer <API_TOKEN>" "https://your-domain.example/messages/search?folder=INBOX&limit=5"
```

## Configure in Muse

When the public HTTPS URL works, tell Muse:

> Setup a connector for this Yahoo Mail API: <base URL>

Muse should use:

- provider slug suggestion: `yahoo-mail-api`
- auth scheme: `api_key`
- placement: `bearer_header`
- API host: the deployed hostname only, for example `mail.example.com`

You will enter the API token in Muse's secure connector form. Do not send the token, the Yahoo app password, or a filled `.env` in chat or to GitHub.

## Daily briefing integration

After the connector test succeeds, Muse can switch the Yahoo part of the morning routine to this API: search compact unread/recent summaries first, fetch full bodies only for important items, and mark clear non-important mail as read through the API.
