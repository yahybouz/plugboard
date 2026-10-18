# Deployment

A local install (`HOST=127.0.0.1`, dashboard auto-login) is meant for one person on one machine. As soon as other machines can reach Plugboard, treat it as a **deployed instance**: it must listen on a network interface, ask for `ADMIN_KEY`, and sit behind TLS.

Plugboard keeps its state in local files (JSON and SQLite under `data/`), so run **a single instance** per data directory.

## Server checklist

In the server's `.env` (or the platform's secret store):

```bash
HOST=0.0.0.0
PLUGBOARD_LOCAL_AUTO_LOGIN=0
ADMIN_KEY=<openssl rand -hex 24>
API_KEY=<openssl rand -hex 24>
ENCRYPTION_KEY=<a long random value, backed up outside the server>
CORS_ORIGINS=https://your-app.example.com      # only if browsers call Plugboard directly
```

Then:

- [ ] TLS in front of Plugboard (reverse proxy, below); port 8765 not reachable from the internet.
- [ ] `data/` on persistent storage and backed up (it holds encrypted credentials, key hashes, usage and pricing history).
- [ ] Optional: `PLUGBOARD_ADMIN_MFA_REQUIRED=1` after enrolling a TOTP secret.
- [ ] Optional: `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` to restrict where provider requests may go.
- [ ] Optional: `ALERT_WEBHOOK_URL` for provider and budget alerts.
- [ ] `RATE_LIMIT` adapted to the reverse proxy (see below).
- [ ] Do not expose subscription-backed providers to other people (see [the risks](providers-and-subscriptions.md#risks-of-using-subscriptions)).

## Docker Compose

```bash
make init            # or create .env yourself, see the checklist
docker compose up -d
curl -s http://127.0.0.1:8765/health
```

The compose file publishes the service on `127.0.0.1:8765` only, so it is reachable from the host and from a reverse proxy running on the host, not from the network. Check `docker-compose.yml` for the volumes it mounts, and keep `data/` on one of them.

About the image:

- `docker compose` builds the slim `api` image: `python:3.12-slim`, HTTP providers only, no Node.js or provider CLIs. For CLI subscriptions build the `full` target instead (`make docker-build-full`, or `target: full` in `docker-compose.yml`), which adds Node.js, the GitHub CLI and the `gemini`, `claude` and `codex` CLIs. Both run as the unprivileged user `app` (UID 1000) with its home in `/home/app` and the application in `/app`; state goes to `/app/data`.
- Inside the container the server listens on `0.0.0.0` (`HOST` is set in the image). Do not pass `HOST=127.0.0.1` to the container: the published port would stop answering.
- A health check calls `/health` every 30 seconds.
- **Auto-login does not apply in Docker**: requests reach the server from the Docker network, so sign in with `ADMIN_KEY`.
- **CLI subscriptions in a container** (`full` image only): sign in from inside the container (`docker compose exec <service> claude`, `codex login`, `gemini`) and keep `/home/app` on a volume, otherwise the login is lost when the container is recreated.
- **Ollama on the host**: `localhost` inside the container is the container itself. Set `OLLAMA_URL` to an address the container can reach, for example `http://host.docker.internal:11434` on Docker Desktop.

## Without Docker (systemd)

```bash
git clone <repository-url> /opt/plugboard
cd /opt/plugboard
make setup
make init            # then edit .env for a server, see the checklist
```

```ini
# /etc/systemd/system/plugboard.service
[Unit]
Description=Plugboard LLM gateway
After=network-online.target

[Service]
Type=simple
User=plugboard
WorkingDirectory=/opt/plugboard
ExecStart=/opt/plugboard/.venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now plugboard
journalctl -u plugboard -f
```

`run.py` reads `.env` from the working directory. CLI-based providers run as the service user, so sign in to the CLIs as that user.

## Behind a reverse proxy

Terminate TLS in a reverse proxy and forward to `127.0.0.1:8765`. Three things matter:

1. **Long timeouts.** A request to a slow model or a CLI provider can take minutes (Plugboard allows up to 900 seconds). Raise the proxy's read timeout.
2. **No buffering for streams.** Streaming responses are server-sent events; Plugboard sends `X-Accel-Buffering: no`, which nginx honours.
3. **Client addresses.** Plugboard does not read forwarded-for headers: the per-IP limit (`RATE_LIMIT`) sees the proxy's address for every request. Raise `RATE_LIMIT`, or set it to `0` and rate-limit at the proxy; per-key RPM limits keep working. Forwarded headers also disable dashboard auto-login, which is intended.

Caddy (automatic HTTPS):

```
llm.example.com {
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
        transport http {
            read_timeout 900s
        }
    }
}
```

nginx:

```nginx
server {
    listen 443 ssl;
    server_name llm.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # Required: the forwarded headers make Plugboard treat the request as
        # proxied, so local auto-login never applies to it.
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900s;
        proxy_buffering off;
    }
}
```

When the proxy runs on the same machine as a loopback-bound Plugboard, also set `PLUGBOARD_LOCAL_AUTO_LOGIN=0`: a proxy that forwards no `X-Forwarded-*` header makes remote requests look local.

With HTTPS in place, the admin session cookie is marked `Secure` automatically (`COOKIE_SECURE=auto`).

## Platform deployments

`railway.toml` builds the Dockerfile on Railway and checks `/health`. On Railway or any similar platform: set the variables of the checklist as secrets, attach a persistent volume at `/app/data` (and at `/home/app` if you sign in to CLIs inside the container), and keep one replica. `scripts/setup_railway_providers.py` creates API-key providers on a remote instance without opening a shell (see [Providers](providers-and-subscriptions.md#add-providers-from-env-or-scripts)).

## Backups and upgrades

- Back up `data/` (and `ENCRYPTION_KEY` if it is set in the environment, or `data/.encryption_key` if it was generated). Without the encryption key, stored provider credentials cannot be decrypted.
- To upgrade: back up `data/`, pull the new version, run `make setup` (or rebuild the image), restart, and check `/health`. To go back, follow the [rollback runbook](../runbooks/rollback-release.md).

## MCP server

The MCP server is a separate process; see [MCP server](mcp.md). In SSE or HTTP mode it listens on `MCP_PORT` (8766); put it behind the same reverse proxy if remote clients need it.
