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
- [ ] `data/` readable by the server's account only: Plugboard creates it with mode `0700` and its SQLite stores `0600`, but a `data/` that already exists keeps its mode (`chmod 700 data`).
- [ ] Optional: `PLUGBOARD_ADMIN_MFA_REQUIRED=1` after enrolling a TOTP secret.
- [ ] Optional: `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` to restrict where provider requests may go.
- [ ] Optional: `ALERT_WEBHOOK_URL` for provider and budget alerts.
- [ ] `FORWARDED_ALLOW_IPS` lists the reverse proxy's address as Plugboard sees it (see below).
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
- A health check calls `/health` every 30 seconds. Container and deployment probes stay on `/health`: `/health/ready` answers `503` while the instance cannot serve (usage database not writable, CLI of an active provider missing, client auth off outside dev mode), which is for a load balancer to route traffic away, not a reason to restart the container ([health endpoints](monitoring.md#health-endpoints)).
- **Auto-login does not apply in Docker**: requests reach the server from the Docker network, so sign in with `ADMIN_KEY`.
- **CLI subscriptions in a container** (`full` image only): sign in from inside the container (`docker compose exec <service> claude`, `codex login`, `gemini`) and keep `/home/app` on a volume, otherwise the login is lost when the container is recreated.
- **Ollama on the host** (optional, off by default: set `OLLAMA_ENABLED=true`): `localhost` inside the container is the container itself. Set `OLLAMA_URL` to an address the container can reach, for example `http://host.docker.internal:11434` on Docker Desktop. Local models are free for budgeted keys only on a loopback or private IP literal: with a DNS name, use the host's private IP or set a `manual` 0 in the `ollama` price space ([pricing](pricing.md#how-a-model-finds-its-price)). When the host only lets the gateway in through a proxy that asks for a bearer token, set `PLUGBOARD_OLLAMA_TOKEN` too.

### CLI versions

The `full` image installs exact releases of the provider CLIs, never `latest`. The versions live in one place only, the `ARG` lines of the `full` stage in the `Dockerfile`: `GEMINI_CLI_VERSION`, `CLAUDE_CODE_VERSION` and `CODEX_VERSION` for the npm packages, and `AGY_URL` with its `AGY_SHA512` checksum for the Antigravity CLI (`agy`). To upgrade a CLI, edit its line in the `Dockerfile`, rebuild the `full` image and redeploy. Compose files and deployment scripts do not pass these versions again (`tests/test_cli_versions_single_source.py` refuses a second copy), so every deployment builds the same releases. A one-off local build may still override one with `docker build --target full --build-arg CODEX_VERSION=<version> .`. The private OVH host installs the same Codex, Claude Code and `agy` releases natively with `deploy/ovh/install-host-tools.sh`, which carries one checksum per release: when the host tools follow, bump that script in the same change; the same test keeps it aligned with the `Dockerfile`.

The UAT gateway's configuration (Compose file, seccomp and AppArmor profiles, deployer entry, env variable names without values) is versioned in `deploy/uat/`; see [deploy/uat/README.md](../../deploy/uat/README.md). Its deployer entry carries no `build_args` either.

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
3. **Client addresses.** The per-IP limit (`RATE_LIMIT`) and the admin-login lockout use the address in `X-Forwarded-For` only when the connection comes from an address listed in `FORWARDED_ALLOW_IPS` (default `127.0.0.1,::1`): a proxy on the same host that forwards to `127.0.0.1:8765` works as is. In Docker, the proxy arrives from the bridge gateway (`docker network inspect`): list that address, or every client shares one counter and one lockout. The proxy must set or append `X-Forwarded-For`. Never list `*` or a `/0` network: any client could then choose its address (the boot log warns). Forwarded headers also disable dashboard auto-login, which is intended.

Caddy (automatic HTTPS):

```
llm.example.com {
    reverse_proxy 127.0.0.1:8765 {
        transport http {
            read_timeout 900s
        }
    }
}
```

Do not add `flush_interval -1`. Plugboard does not need it: each of its streams is `text/event-stream` or has no `Content-Length`, and Caddy sends such a response to the client at once without it. From Caddy 2.6 to 2.8 it also did harm: with it, Caddy did not cancel the request to Plugboard when the client hung up ([Caddy pull request 4952](https://github.com/caddyserver/caddy/pull/4952), reverted in Caddy 2.9.0). Plugboard then did not see the client leave during a request that is not streamed, or during a stream before its first byte, so it could neither stop the provider call nor free the chat slot.

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

Keep the `Host` header as the examples do (Caddy keeps it by default). Over plain HTTP to a non-local address, browsers send no `Sec-Fetch-Site`, and Plugboard checks the dashboard's changes by comparing their `Origin` with `Host` ([details](monitoring.md#changes-made-with-a-dashboard-session)).

## Outbound network

Protected providers (catalog presets, built-in presets with an HTTP(S) URL, and custom endpoints with **Public addresses only** ticked) connect to the internet directly: they ignore `HTTPS_PROXY` and `HTTP_PROXY`, because Plugboard's egress guard checks itself every address they connect to ([Public addresses only](providers-and-subscriptions.md#public-addresses-only)).

- The server needs direct outbound HTTPS access to these providers' hosts. Behind a mandatory proxy they cannot connect: add each one as a custom endpoint to the same URL with the box left unticked, which keeps using `HTTPS_PROXY`.
- Custom endpoints with the box left unticked and Ollama are unchanged.
- The egress guard is not a firewall. As defence in depth, filter the server's outbound traffic too: allow only the provider hosts you use and block private ranges and the cloud metadata address `169.254.169.254`. `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` also limits the hosts a provider base URL may name.

## Platform deployments

`railway.toml` builds the Dockerfile on Railway and checks `/health`. On Railway or any similar platform: set the variables of the checklist as secrets, attach a persistent volume at `/app/data` (and at `/home/app` if you sign in to CLIs inside the container), and keep one replica. `scripts/setup_railway_providers.py` creates API-key providers on a remote instance without opening a shell (see [Providers](providers-and-subscriptions.md#add-providers-from-env-or-scripts)).

## Backups and upgrades

- Back up `data/` (and `ENCRYPTION_KEY` if it is set in the environment, or `data/.encryption_key` if it was generated). Without the encryption key, stored provider credentials cannot be decrypted: if the server starts with another key, its boot log names every stored secret it cannot decrypt (critical level), the providers concerned are turned off until the right key is restored or their key is entered again (audit `provider.secret_unreadable`, alert `provider_secret_unreadable`), and their stored key is never sent.
- To upgrade: back up `data/`, pull the new version, run `make setup` (or rebuild the image), restart, and check `/health`. To go back, follow the [rollback runbook](../runbooks/rollback-release.md).
- To move the install to another machine, follow the [move runbook](../runbooks/move-install.md).

## MCP server

The MCP server is a separate process; see [MCP server](mcp.md). In SSE or HTTP mode it requires `PLUGBOARD_MCP_TOKEN` and listens on `127.0.0.1:MCP_PORT` (8766). Never expose it, through a reverse proxy or a non-loopback `MCP_HOST`, without that token: every caller would act with its client key. When remote clients need it, keep the token secret, serve it over TLS, and list the public name in `PLUGBOARD_MCP_ALLOWED_HOSTS` and `PLUGBOARD_MCP_ALLOWED_ORIGINS` ([network transports](mcp.md#network-transports-sse-http)). The admin key is never used in these modes.
