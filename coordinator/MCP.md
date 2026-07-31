# Mailcheck MCP server

The coordinator exposes its email-verification workflow as an **MCP server** so
coding agents — Claude Code (CLI **and** the claude.ai web app) and Codex — can
verify emails directly, without a browser or any local install.

- **Endpoint:** `https://email-verifier.revengineer.ai/mcp` (streamable-HTTP)
- **Auth:** OAuth 2.0 (PKCE + Dynamic Client Registration). You sign in **once**
  with Google; the client stores a refresh token and never prompts again.
- **No local Python / API key** is needed on any client — the server is hosted
  as part of the coordinator, and all clients point at the one URL.

## Tools

| Tool | What it does |
|---|---|
| `verify_emails(emails, retry_delay_minutes=1)` | Create a job from a list of addresses → `{job_id, accepted, rejected}` |
| `verify_csv(path, retry_delay_minutes=1)` | Create a job from a local CSV (first column = emails) |
| `list_jobs(limit=50, offset=0)` | List jobs, newest first |
| `get_job(job_id)` | Status/progress; poll until `completed`/`failed` |
| `get_results(job_id, status="all", sort="default", limit=200, offset=0)` | Trimmed results (email, is_reachable, accepts_mail, smtp_deliverable, catch_all) |
| `download_csv(job_id, out_path)` | Write a flat results CSV and return the path |
| `verify_and_wait(emails, retry_delay_minutes=1, poll_interval_seconds=5, timeout_seconds=1800)` | Create + poll to completion + return a per-category summary (the easy one-shot) |

Each address resolves to `safe` / `risky` / `invalid` / `unknown`; `unknown`
results are auto-retried on a different server per `retry_delay_minutes` (1–15).

## Connect a client

The first connection opens a Google sign-in in your browser; after that it is
silent (a stored refresh token is reused, and survives redeploys).

### Claude Code — web app (claude.ai)

Settings → **Connectors** → **Add custom connector** → paste
`https://email-verifier.revengineer.ai/mcp` → complete the Google consent once.

### Claude Code — CLI

```bash
claude mcp add --transport http mailcheck https://email-verifier.revengineer.ai/mcp
```

Then, in a session, run `/mcp` and choose **Authenticate** the first time.

### Codex

Add a remote MCP server to `~/.codex/config.toml` and authenticate once:

```toml
[mcp_servers.mailcheck]
url = "https://email-verifier.revengineer.ai/mcp"
```

```bash
codex mcp login mailcheck
```

> Requires a Codex version with remote (streamable-HTTP) MCP + OAuth support. If
> your build only supports stdio MCP servers, upgrade Codex.

## Try it

Ask the agent:

> Verify amar@basisvps.com and jane@example.com and tell me which are deliverable.

The agent calls `verify_and_wait`, waits for the job, and reports the
per-category counts.

## Server configuration (operators)

The MCP server is part of the coordinator app; no separate process. Relevant env
vars (in addition to the coordinator's usual settings):

| Var | Default | Purpose |
|---|---|---|
| `MCP_PUBLIC_URL` | `https://email-verifier.revengineer.ai` | Public base URL; used to build the OAuth issuer + resource identifiers and discovery documents. **Must match how clients reach the server.** |
| `MCP_DISABLE_AUTH` | *(unset)* | Set to `1` to serve `/mcp` without OAuth — **local testing only**. |

OAuth clients, authorization codes, and access/refresh tokens are persisted in
the coordinator's SQLite database (`oauth_*` tables) so tokens survive restarts.
The login step reuses the coordinator's existing Google sign-in
(`GOOGLE_CLIENT_ID`, `GOOGLE_ALLOWED_DOMAINS`); in `AUTH_MODE=development` the
authorize step auto-completes as the local developer.

Discovery + endpoints served at the site root:
`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource/mcp`, `/authorize`, `/token`,
`/register`, `/revoke`, and the MCP endpoint `/mcp`.

Requires Python **3.10+** (the deployed image is 3.13); `mcp[cli]` is pinned in
`backend/requirements.txt`.
