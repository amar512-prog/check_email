# Mailcheck Coordinator

Mailcheck is a central coordinator and React frontend for distributing CSV email verification jobs to one or more Reacher workers without exposing their secrets to the browser.

## Local development

The existing Reacher worker must be available at `http://127.0.0.1:8080`.

```bash
cp .env.example .env
# Put the existing RCH__HEADER_SECRET into REACHER_SERVERS_JSON.

python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

cd frontend
npm install
npm run build
cd ..

set -a
source .env
set +a
.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Development mode displays a local login button. Reacher secrets remain in the backend environment.

When running the coordinator through Docker Compose, use `http://host.docker.internal:8080` for a Reacher container published on the host. Use `http://127.0.0.1:8080` when running the coordinator natively as shown above.

## Google login

1. Create an OAuth 2.0 **Web application** client in Google Cloud Console.
2. Add the production origin, for example `https://mailcheck.example.com`, to **Authorized JavaScript origins**.
3. Set `AUTH_MODE=google`, `GOOGLE_CLIENT_ID`, and `SESSION_SECURE=true`.
4. Optionally restrict accounts with `GOOGLE_ALLOWED_DOMAINS=example.com`.

Google returns an ID credential to the browser. The coordinator verifies it server-side and stores only a signed, HTTP-only session cookie. No Google client secret is required for this sign-in flow.

## Four Reacher servers

Configure every endpoint in `REACHER_SERVERS_JSON`:

```json
[
  {"name":"reacher-1","url":"https://reacher-1.example.com","secret":"...","emails_per_minute":50},
  {"name":"reacher-2","url":"https://reacher-2.example.com","secret":"...","emails_per_minute":50},
  {"name":"reacher-3","url":"https://reacher-3.example.com","secret":"...","emails_per_minute":50},
  {"name":"reacher-4","url":"https://reacher-4.example.com","secret":"...","emails_per_minute":50}
]
```

Mailcheck distributes addresses evenly, creates batches equal to each server's per-minute allocation, runs servers in parallel, and waits `REACHER_PACING_SECONDS` between batches. The frontend receives sanitized server status only.

## Production notes

- Serve the coordinator over HTTPS and keep `SESSION_SECURE=true`.
- Keep `.env` readable only by the service account.
- Do not put Reacher secrets in Vite variables, frontend source, browser storage, or HTML.
- Back up the coordinator SQLite volume and each Reacher server's PostgreSQL volume.
- Keep PostgreSQL and RabbitMQ private; expose only each authenticated Reacher HTTPS API.
