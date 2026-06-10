# Reacher Deployment

## Server

- 2 vCPU
- 2 GB RAM
- 20 GB NVMe
- Ubuntu Server 26.04 LTS
- Docker
- Static IPv4
- Outbound TCP port 25 enabled, or an SMTP-capable SOCKS5 proxy

## API Secret

The API secret is stored locally in `.env` as `RCH__HEADER_SECRET`. The `.env`
file must remain private and must not be committed to Git.

Generate a replacement secret when needed:

```bash
openssl rand -hex 32
```

## Run With Docker

Copy `.env` to the server through an encrypted SSH connection, then restrict
access to the file:

```bash
chmod 600 .env
```

Start Reacher with authentication enabled:

```bash
docker run -d \
  --name reacher \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 \
  reacherhq/backend@sha256:cb73c2ee5bd684e014174aba64316d6cd567260f6c6c97fcda47de3fc95f7266
```

## Worker Mode For Bulk Verification

The local worker stack is defined in `docker-compose.worker.yml`. It runs:

- Reacher HTTP server and worker on `127.0.0.1:8080`
- RabbitMQ on a private Docker network
- PostgreSQL on a private Docker network with persistent storage

Start the stack:

```bash
docker compose --env-file .env -f docker-compose.worker.yml up -d
```

Check service health and Reacher logs:

```bash
docker compose --env-file .env -f docker-compose.worker.yml ps
docker compose --env-file .env -f docker-compose.worker.yml logs reacher
```

Stop the stack without deleting database or queue data:

```bash
docker compose --env-file .env -f docker-compose.worker.yml down
```

Do not add `-v` to the `down` command unless permanent deletion of PostgreSQL
and RabbitMQ data is intended.

Bind port 8080 to localhost and publish the API through an HTTPS reverse proxy
such as Caddy or Nginx.

The image is pinned to the exact digest tested with Reacher `0.11.6`. Upgrade
the digest deliberately after testing a newer image; do not use the floating
`latest` tag in production.

## Call The API

Read the secret into the shell without printing it:

```bash
set -a
. ./.env
set +a
```

Call the authenticated endpoint:

```bash
curl https://verify.example.com/v0/check_email \
  -H "Content-Type: application/json" \
  -H "x-reacher-secret: ${RCH__HEADER_SECRET}" \
  -d '{"to_email":"amar@basisvps.com"}'
```

Requests with a missing or incorrect `x-reacher-secret` header are rejected
when `RCH__HEADER_SECRET` is configured.

## Rotate The Secret

1. Generate a new secret and replace the value in `.env`.
2. Recreate the container with `--env-file .env`.
3. Update API clients with the new secret.
4. Remove the old secret from password managers and deployment systems.
