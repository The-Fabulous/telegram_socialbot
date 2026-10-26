# Helper Bot on Oracle Cloud Always Free

These files prepare a deployment; they do not create Oracle resources or publish code.

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
