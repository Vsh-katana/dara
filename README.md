# Dara Finance Server

A separate, multi-user self-hosted edition of Dara. Each account receives its own SQLite database file, so transaction, account, budget, and settings data are isolated by user. The server build contains no workbook seed, local finance database, or previous user's transactions.

## What it includes

- Individual username and password accounts with PBKDF2 password hashes, expiring opaque sessions, `HttpOnly`/`Secure`/`SameSite=Strict` cookies, CSRF checks, origin checks, and login/signup rate limits.
- One database per account under a persistent Docker volume. Registration creates a blank finance workspace; it does not import the desktop workbook.
- Same bilingual Persian/English interface, Jalali/Gregorian dates, accounting ledger, reports, accounts, budgets, and exports; automatic, light, and dark modes with six accent colors and teal as the default.
- Docker Compose with the application bound only inside the Compose network and Caddy at the public edge.
- Automatic HTTPS and certificate renewal using Let's Encrypt.

## One-line GitHub installer

After publishing the contents of this directory to a GitHub repository, install with:

```sh
curl -fsSL https://raw.githubusercontent.com/Vsh-katana/dara/main/install.sh | sudo bash -s -- --repo Vsh-katana/dara --domain finance.example.com
```

Replace `finance.example.com` with a domain you control. The script supports Debian 12 or newer and Ubuntu 22.04 or newer. It installs Docker Engine and Compose from Docker's official apt repository when missing, creates a high-entropy session secret file, builds the application, and starts the services.

## Before running the installer

1. Point the domain's A record (and, if used, AAAA record) at the server.
2. Ensure inbound TCP ports 80 and 443 are reachable from the internet; Caddy uses them for HTTP-01 validation and HTTPS traffic. The app container itself is not published to the host.
3. Run the one-line installer as shown. Caddy requests the production Let's Encrypt certificate and renews it automatically.
4. Open `https://finance.example.com` and create an account. Each person can register with their own username and password.

The installer enables public registration by default and caps the server at 500 accounts. Edit `ALLOW_SIGNUP` and `MAX_ACCOUNTS` in `/opt/dara-finance/.env` to change these limits. To pause new account creation, set `ALLOW_SIGNUP=0` and run `docker compose up -d` from that directory. Existing users can continue to sign in.

## Operations

```sh
cd /opt/dara-finance
docker compose ps
docker compose logs -f app caddy
git pull --ff-only
docker compose up -d --build
```

The `.secrets/session_secret` file is kept outside the image and mounted as a Compose secret. Docker's named volume `dara_data` holds the authentication database and per-user databases. `caddy_data` holds the ACME account and certificate state. Back up these volumes and `.secrets/session_secret` before upgrades or moving the server. Do not publish the app's port directly or copy the desktop `data/` directory into this deployment.

The installer expects this server deployment as the root of the GitHub repository. See `compose.yaml`, `Dockerfile`, and `Caddyfile` before installing; the installer is intentionally readable and accepts an explicit repository and ref.
