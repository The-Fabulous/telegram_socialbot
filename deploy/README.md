# Helper Bot on Oracle Cloud Always Free

These files prepare a deployment; they do not create Oracle resources or publish code.

## GitHub Pages frontend

The repository's Pages site uses **Deploy from a branch**, with `main` and
`/(root)` as its publishing source. The root `index.html` is a Jekyll entry
point that includes `webapp/index.html`, so the existing UI is published at
`https://the-fabulous.github.io/telegram_socialbot/` without copying its source.
The original `/telegram_socialbot/webapp/` URL remains available.
Do not add `.nojekyll`: the root entry point requires Jekyll's `include_relative`.
`_config.yml` excludes server code, database files and deployment tooling from
the generated static site.

Pages publishes HTML, CSS and JavaScript only. Its frontend sends API requests
to `https://telegram-socialbot-im20.onrender.com`. The Render-hosted frontend
and local copies continue to use their own origin. Change `API_BASE_URL` in
`webapp/index.html` if the backend moves.

The backend accepts the Pages origin `https://the-fabulous.github.io` by
default. Additional frontend origins can be configured as a comma-separated
`ALLOWED_ORIGINS` environment variable (origins only, without URL paths).
Unapproved origins are rejected before the API handler runs.

Pages requests a bearer session on registration/login, stores it in
`sessionStorage` for the current tab, and sends it in the `Authorization`
header. This avoids third-party-cookie restrictions between github.io and
onrender.com. Reloading the tab restores the session; logout revokes it on
the backend and removes the stored token. Closing the tab normally clears
the browser's session storage. No password or model API key is stored there.
When opened directly on Render, the UI uses the existing HttpOnly cookie.

## Render service

Deploy `main` as a Python Web Service with an empty root directory:

- Build command: `pip install -r requirements.txt`.
- Start command: `python bot.py`.
- Set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` for your model provider.
- Render supplies `PORT` and `RENDER_EXTERNAL_URL` automatically.
- Optionally set `WEBAPP_URL` to the public frontend URL for Telegram buttons.

After pushing a change, both Pages and Render must finish deploying it. Verify
that an `OPTIONS /api/auth/me` request with the Pages `Origin` returns 204 and
`Access-Control-Allow-Origin: https://the-fabulous.github.io`, then verify login
and logout from Pages.

The current application uses SQLite. A Free Render service has no persistent
disk: local accounts and messages can be lost on redeploy, restart or spin-down.
For durable accounts, mount a persistent disk and set `DB_PATH` to a path on
that disk, or complete the integration of an external database. A database
backup must never be served as a static asset or committed to Git.

After a Pages deployment, verify that the root URL returns HTTP 200 and the
Social Bridge HTML (not unrendered Liquid), and that `/bot.py` and
`/data.db.bak-v1` under the project URL return HTTP 404.

## Target layout

- Ubuntu 24.04 Ampere A1 or AMD Micro VM.
- Caddy is the only public process on ports 80/443.
- `helper-bot.service` binds one Python process to `127.0.0.1:8080`.
- Firestore remains the durable data store. No SQLite file is deployed.
- Secrets live in `/etc/helper-bot`, outside Git and outside release archives.
- Releases live in `/opt/helper-bot/releases/<git-sha>`; `/opt/helper-bot/current` is an atomic symlink.

Python 3.12 is the production baseline. On an ARM VM, verify that every dependency installs from a wheel or builds successfully before enabling deployment.

## One-time server preparation

Run these steps manually after creating the VM and DNS record:

1. Create an unprivileged `helperbot` account and `/opt/helper-bot/releases` owned by it.
2. Install Python 3.12, `python3.12-venv`, Caddy, `curl`, `tar`, and OpenSSH.
3. Copy `helper-bot.service` to `/etc/systemd/system/`.
4. Copy the two shell scripts to `/usr/local/sbin/`, owner `root:root`, mode `0755`.
5. Copy `.env.example` to `/etc/helper-bot/helper-bot.env`, owner `root:helperbot`, mode `0640`, and fill in the existing project values.
6. Put the existing Firebase service-account JSON at `/etc/helper-bot/firebase-service-account.json`, owner `root:helperbot`, mode `0640`. Prefer a dedicated service account with only the Firestore permissions required by this application.
7. Replace `chat.example.com` in `Caddyfile`, install it as `/etc/caddy/Caddyfile`, and validate with `caddy validate --config /etc/caddy/Caddyfile`.
8. Allow the deployment SSH user to run only these commands without a password:

   ```sudoers
   deploy ALL=(root) NOPASSWD: /usr/local/sbin/helper-bot-deploy *, /usr/local/sbin/helper-bot-rollback *
   ```

9. Open inbound TCP 80/443 in the Oracle security list and host firewall. Keep 8080 private.
10. Enable Caddy and Helper Bot only after the first release is installed.

Never copy `OPENAI_API_KEY`, `BOT_TOKEN`, Firebase JSON, `.env`, or user databases into GitHub. The external model key must be placed on the server only with its owner's permission.

## GitHub Actions secrets

Configure a `production` GitHub Environment, restrict it to the `main` branch, and require a reviewer approval before deployments. Add:

- `DEPLOY_HOST`: VM hostname or IP;
- `DEPLOY_USER`: restricted SSH deployment account;
- `DEPLOY_SSH_KEY`: dedicated private deployment key;
- `DEPLOY_HOST_KEY`: pinned `known_hosts` line collected through a trusted channel.

Application and Firebase secrets are intentionally absent from GitHub Actions. The workflow uploads only source code, then the root-owned server script creates a release, installs dependencies, compiles, tests, switches the symlink, restarts one service, and checks `/healthz`.

## Verification

After a release, check:

```bash
systemctl status helper-bot --no-pager
curl --fail https://chat.example.com/healthz
journalctl -u helper-bot --since '-10 minutes' --no-pager
```

Then verify web registration/login, Telegram Mini App login, private chat ownership, AI and human messages, unread state, profile/settings, logout, and persistence across `systemctl restart helper-bot`.

The process lock and localhost port bind reject a second application process. Do not configure Gunicorn workers, systemd template instances, or multiple Oracle replicas.

## Rollback

Automatic rollback occurs when a new release does not pass its local health check. Manual rollback selects the previous retained release:

```bash
sudo /usr/local/sbin/helper-bot-rollback
```

To select a retained release explicitly:

```bash
sudo /usr/local/sbin/helper-bot-rollback <full-40-character-git-sha>
```

Rollback changes application code and its isolated virtual environment. Firestore data is not rolled back. Schema-changing releases therefore need a separate forward-compatible data migration plan.

## Firestore operation budget

With the default six-hour session and user cache, browser polling performs zero Firestore operations on cache hits. Telegram Mini App requests also cache the Telegram-ID-to-user mapping, avoiding a Firestore transaction on every 2.5-second poll. Assuming one active session per user, continuous activity causes about one session read and one user read every six hours: about 8 reads/user/day. Login adds roughly two reads and one session write. Logout adds one delete. Normal message costs are action-dependent:

- AI exchange: approximately three writes (user message, chat state, assistant message).
- Human message: approximately three writes (two message copies and peer unread state).
- Reading an unread chat: one chat-state write.
- Settings or profile update: one write.

Authentication-only estimates, excluding startup restoration and user actions:

| Simultaneously active users | Authentication reads/day | Writes/day from polling |
|---:|---:|---:|
| 10 | about 80 | 0 |
| 100 | about 800 | 0 |
| 1,000 | about 8,000 | 0 |

The current browser sends about 24 API polls/minute per active user (10/100/1,000 users means about 240/2,400/24,000 HTTP requests/minute). These are served from the single process's in-memory state and have zero steady-state Firestore reads/writes, subject to the cache-expiry reads above. This is a Firestore estimate only, not a capacity claim for one aiohttp process.

Every process start additionally reads one catalog-version document, every user document, every chat document, and at most `MESSAGE_LOAD_LIMIT` recent messages per chat. Message history is loaded in one bounded descending query per chat and kept in process memory; the 2.5-second UI poll never queries Firestore for messages. For `U` users, `C` chats, and `M_i` stored messages per chat, restart reads are approximately:

```text
1 + U + C + sum(max(1, min(M_i, MESSAGE_LOAD_LIMIT)))
```

This startup term, rather than polling, is the main Firestore scaling limit. For example, 1,000 users with one 20-message chat each add about 22,001 reads on restart; with one 80-message chat each, about 82,001 reads. Ten 80-message chats per user would exceed the free daily quota by a large margin. The app currently requires global in-memory chat state for matchmaking, so eliminating the `O(users + chats + recent messages)` cold-start read requires a separate architectural change: lazy per-user chat loading and a durable matchmaking queue. That migration is not part of this behavior-preserving production preparation.

The Firestore free quota is 50,000 reads, 20,000 writes, and 20,000 deletes per day. Message volume can exceed the write quota even when authentication stays below the read quota, so production must monitor the Firebase Usage dashboard.

## Git-history finding

The current Git history contains `data.db`, `data.db-shm`, `data.db-wal`, and `data.db.bak-v1` blobs in commits `9fd4000` and `1b7ab33`. A pattern scan of historical text files found no Telegram bot tokens, OpenAI API keys, or private-key headers; database blobs were not searched for or displayed. `.gitignore` now prevents new untracked database files, and the deployment archive excludes all `*.db*` files, but already tracked files and historical blobs remain present and may contain personal data. No history was rewritten and no files were removed from Git's index.

Before making the repository public, treat those database blobs as exposed personal data. A history rewrite and forced update would require explicit approval, a backup, coordination with every clone, and rotation/revocation of any credentials found during a dedicated secret scan.
