# Deploy the API with Docker Compose and Caddy

Clone the public backend repo onto the VPS, configure its private `.env`, and run
Compose. The frontend remains on Vercel and talks directly to the API over HTTPS.

- `api`: Python 3.14, locked dependencies, the spaCy model, and FFmpeg. Runs as a
  non-root user with one worker, a read-only filesystem, temporary storage in
  memory, a health check, and automatic restarts.
- `caddy`: publishes only TCP 80 and 443, obtains and renews HTTPS certificates,
  and forwards requests to `api:8000` on the private Docker network.

The API port is never published. Docker-published ports can bypass UFW, so keep
that property when modifying Compose. Caddy uses HTTP/1.1 and HTTP/2; UDP 443 is
not required. No host Python, uv, FFmpeg, Nginx, or Certbot installation is needed.

## 1. Install Docker on the VPS

Run as `ubuntu` in your SSH session:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update

sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

These use Docker's official Ubuntu repository. If Docker is already installed,
check its version and avoid mixing distro packages with Docker CE packages.
Use `sudo docker` below; joining the `docker` group grants root-equivalent access
and is not required for this workflow.

Keep the UFW rules for TCP 22, 80, and 443. Check for existing processes using
80 or 443 with `sudo ss -lntp`; stop a previous host web server before starting
Caddy if it owns those ports. Do not stop SSH.

## 2. Clone the public backend repository

The Docker changes must first be committed and pushed from your development
machine. Then, on the VPS:

```bash
git clone https://github.com/nilu2039/hush-mark.git ~/hush-mark-api
cd ~/hush-mark-api
```

All backend deployment files are in this repository. No frontend checkout is
needed. If this directory already contains a clone, inspect `git status` and
use `git pull --ff-only` instead of cloning over it.

## 3. Configure runtime secrets and domains

```bash
umask 077
cp .env.example .env
chmod 600 .env
nano .env
```

Set these values in the VPS editor:

```dotenv
OPENAI_API_KEY=YOUR_REAL_KEY
CORS_ALLOWED_ORIGINS=https://hush-mark.nilanjan.xyz
API_DOMAIN=hush-mark-api.nilanjan.xyz
```

The key is passed only to the API container at runtime. Git and the Docker build
context exclude `.env`; it is not built into the image or passed to Caddy. Do
not paste it into chat or commit it. Avoid displaying the full output of
`docker compose config`, which includes environment values; use `--quiet`.

Keep the API domain's A record pointing to `15.235.245.44`, with Cloudflare set
to **DNS only**. Add an AAAA record only if the VPS IPv6 address is configured
and reachable. Caddy needs inbound TCP 80 and 443 to serve HTTPS and obtain its
certificate.

## 4. Build and start

```bash
sudo docker compose config --quiet
sudo docker compose build api
sudo docker compose up -d --wait --wait-timeout 180
sudo docker compose ps
sudo docker compose logs --tail 50 caddy
```

The first build downloads the large NLP model. Dependencies and the model are
installed during the build; the host does not need Python or FFmpeg. Compose
starts Caddy after the API health check passes. Certificate issuance may take
a little longer than the container startup itself.

Verify the API and CORS:

```bash
curl --fail --silent --show-error https://hush-mark-api.nilanjan.xyz/openapi.json -o /dev/null
curl -i -X OPTIONS https://hush-mark-api.nilanjan.xyz/v1/analyze \
  -H 'Origin: https://hush-mark.nilanjan.xyz' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type'
```

The preflight should return HTTP 200 with
`Access-Control-Allow-Origin: https://hush-mark.nilanjan.xyz`. `docker compose ps`
must show no published port for `api`; port 8000 is reachable only inside the
Docker network. Uvicorn trusts forwarded client-IP headers in that private
network; Caddy replaces the incoming `X-Forwarded-For` with the actual peer IP.
Keep the API unpublished so external callers cannot spoof that trust boundary.
If you enable Cloudflare's proxy later, configure Caddy's trusted proxy ranges
before relying on per-client IP limits.

Compose limits API POST requests to 10 per client IP per 60-second window.
CORS preflights and health checks are exempt. Responses use HTTP 429 with the
normal safe JSON error shape and a `Retry-After` header. To change the quota,
set `RATE_LIMIT_PER_MINUTE` in `.env` and recreate the service. Counters live only
in this single worker's memory, expire when requests arrive, and reset on
restart. This is not authentication or a global spending cap.

Caddy accepts requests up to 30 MB; the API still limits audio to 25 MiB. The API
has a 2.5 GiB memory limit and a 512 MiB temporary filesystem; very long recordings
can exhaust temporary space during export. Measure representative recordings
before raising these limits on a 4 GB VPS. Request access logs and core dumps
are disabled, and Docker logs are bounded. No uploads or analyses have persistent
volumes. Caddy's named volumes retain certificate state across updates.

In Vercel, set the build-time variable
`NEXT_PUBLIC_API_BASE_URL=https://hush-mark-api.nilanjan.xyz/v1`. Point the frontend
domain to the exact record Vercel requests; only the API domain points to this
VPS. Test synthetic text, file upload, and audio review/export from the frontend.
Real analysis incurs OpenAI usage.

## Updates and rollback

After a tested backend commit is pushed, run on the VPS:

```bash
cd ~/hush-mark-api
git rev-parse HEAD
git pull --ff-only
sudo docker compose build api
sudo docker compose up -d --wait --wait-timeout 180
sudo docker compose ps
```

Record the first commit hash as your rollback point. The old container remains
running during the build; recreation causes a short interruption. If a new
version fails, check out that previous commit, rebuild, and run `up` again. If
the directory has local modifications, inspect them rather than resetting it.
The ignored `.env` and certificate volumes stay outside Git updates.

For a proxy-only configuration change, validate and reload without recreating
the API:

```bash
sudo docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Use `sudo docker compose up -d` after environment changes; `restart` alone does
not apply them. Inspect `sudo docker compose logs --tail 100` and
`sudo docker stats --no-stream` for failures and resource usage. Never add
submitted text, uploads, or transcripts to logs. Back up `.env` and Caddy's
certificate volume securely. Do not use `docker compose down -v` during routine
updates, because it deletes certificate state.

References: [Docker on Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[Caddy with Compose](https://caddyserver.com/docs/running#docker-compose),
[Docker and UFW](https://docs.docker.com/engine/network/packet-filtering-firewalls/#docker-and-ufw),
[uv container builds](https://docs.astral.sh/uv/guides/integration/docker/).
