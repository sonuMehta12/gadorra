# Deploying to the Azure VM

This is how the API is actually deployed for the client, as of September 2026.

```
Internet ──HTTPS──> Application Gateway ──HTTP :80──> nginx ──> API 127.0.0.1:8000
                    (api.gpet.org.in,                  (VM: vm-gradorra-api-prod-prep-001,
                     TLS cert from Key Vault)               10.10.2.4, Ubuntu 24.04)
                                                                  │
                                                                  ▼
                                               Azure Database for PostgreSQL
                                               psql-gradorra-prod-001 (Postgres 17, TLS)

You ──RDP──> Windows jump box ──SSH──> API VM
```

- **TLS ends at the gateway.** Nothing on the VM handles certificates.
- **nginx on :80** belongs to the client's infra team. Their site config is
  `/etc/nginx/sites-available/gpet-api` and proxies to `127.0.0.1:8000`.
- **The API** runs in Docker from `docker-compose.azure.yml` and listens on
  loopback only, so nginx is the one way in.
- **The database** is Azure's managed Postgres, not a container. Backups and
  failover are Azure's.
- **Front end:** Next.js on another VM (`10.10.1.8:3000`).

Hostnames, IPs and credentials come from the info file the infra team left on
the jump box. Ask them if it is missing.

## Deploying a new version

The usual case. On the API VM:

```bash
cd ~/gradorra && bash deploy/deploy.sh
```

It pulls `main`, rebuilds, restarts, and waits until `/health` reports ok,
printing the API logs if it does not. It refuses to run if a required secret in
`.env` is empty, or if `.env` names an external database without
`COMPOSE_FILE=docker-compose.azure.yml`.

Check afterwards:

```bash
curl http://localhost/health                 # through nginx
docker ps --format '{{.Names}}  {{.Ports}}'  # one container, 127.0.0.1:8000
```

## Getting onto the VM

1. **Remote Desktop to the jump box.** On a Mac, install Windows App with
   `brew install --cask windows-app` (no App Store account needed). Add PC →
   the jump box's public IP → the user from the info file.
2. **SSH to the API VM** from PowerShell on the jump box:
   ```powershell
   ssh gradorra@10.10.2.4
   ```
   or with PuTTY and the `.ppk` key from the infra team.

Paste one line at a time. Multi-line pastes through Remote Desktop run together
and garble commands. In PowerShell, right-click pastes.

## Setting up a fresh VM from scratch

Only needed for a new or rebuilt VM.

### 1. The database

Create the application database once, from the VM:

```bash
psql "host=psql-gradorra-prod-001.postgres.database.azure.com port=5432 dbname=postgres user=gradorradbadmin sslmode=require"
```
```sql
CREATE DATABASE gradorra;
\q
```

SQL statements end in `;`. Without it psql waits for more input (the prompt turns
into `postgres->`); press Ctrl+C and type it again. Backslash commands such as
`\q` take no semicolon.

The tables and the 75 districts are created by the API on its first start.

### 2. GitHub access

The repo, `003aja/gradorra-gpet-api`, is private. The VM already has a key
(`~/.ssh/id_ed25519`) on the repo owner's GitHub account, and `~/.ssh/config`
points GitHub at it:

```
Host github.com
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

Check with `ssh -T git@github.com`; it should greet `003aja`.

That key can reach everything the owner's account can. A read-only **deploy key**
is the better long-term choice: `~/.ssh/gradorra_deploy.pub` is already on the VM
for that. The repo owner adds it under Settings → Deploy keys with write access
off, then `IdentityFile` switches to `~/.ssh/gradorra_deploy`.

### 3. Docker and the checkout

```bash
git clone git@github.com:003aja/gradorra-gpet-api.git ~/gradorra
cd ~/gradorra && bash deploy/setup_vm.sh
```

`setup_vm.sh` installs Docker from Docker's own apt repository, adds you to the
`docker` group, and creates `.env` with a random `JWT_SECRET`. **Log out and SSH
back in once** afterwards, or run `newgrp docker`; until then Docker says
`permission denied`.

### 4. `.env`

```bash
nano ~/gradorra/.env
```

In nano, Ctrl+W finds a line, Ctrl+O then Enter saves, Ctrl+X quits.

| Variable | Value |
| --- | --- |
| `COMPOSE_FILE` | `docker-compose.azure.yml` -- **add this line**; without it the Docker-Postgres setup runs instead |
| `DATABASE_URL` | `postgresql://gradorradbadmin:<password>@psql-gradorra-prod-001.postgres.database.azure.com:5432/gradorra?sslmode=require` |
| `APP_ENV` | `production` |
| `WHATSAPP_TOKEN` | the permanent system-user token |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | the **test** pair until the client says go live |
| `RAZORPAY_WEBHOOK_SECRET` | from the Razorpay dashboard, same mode as the keys |
| `CORS_ORIGINS` | the front end's public domain(s), comma separated -- not `*` |

The database password contains `@`, which must be written `%40` in the URL.
Otherwise everything after it is read as the host. Encode `#` as `%23` and `%` as
`%25` the same way.

The WhatsApp IDs, template names and fees are already correct in the file.
Leave `AUTO_INIT_DB` alone: the compose file turns it on.

### 5. nginx

The infra team installs nginx and owns its config. If you set up nginx yourself
on a VM that has none, `deploy/nginx/gradorra-api.conf` is a working site config:

```bash
sudo apt-get install -y nginx
sudo cp deploy/nginx/gradorra-api.conf /etc/nginx/sites-available/gradorra-api
sudo ln -sf /etc/nginx/sites-available/gradorra-api /etc/nginx/sites-enabled/gradorra-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Use only one site that claims `default_server` on port 80. With both
`gpet-api` and `gradorra-api` enabled, `nginx -t` fails with *a duplicate
default server*. Keep the infra team's and remove the other link:

```bash
sudo rm /etc/nginx/sites-enabled/gradorra-api
```

### 6. Deploy and check

```bash
bash deploy/deploy.sh
curl http://localhost/health
psql "host=psql-gradorra-prod-001.postgres.database.azure.com port=5432 dbname=gradorra user=gradorradbadmin sslmode=require" -c "select count(*) from districts;"
```

The last one should print **75**. That proves the API reached the Azure database
and not some other one.

## The gateway

Configured by the infra team, not from the VM:

- listener for **`api.gpet.org.in`** on 443 with a certificate that covers it,
  including the intermediate certificate (a missing chain shows up as Chrome
  refusing the site while other browsers accept it)
- backend pool **`10.10.2.4`, port 80**
- health probe **`/health`** over HTTP on port 80
- a DNS A record for `api.gpet.org.in` pointing at the gateway's public IP

Once it answers from outside (`https://api.gpet.org.in/health`), the front end's
base URL is **`https://api.gpet.org.in/api/v1`** and the reference is
`https://api.gpet.org.in/docs`.

## When something is wrong

```bash
docker compose -f docker-compose.azure.yml ps
docker compose -f docker-compose.azure.yml logs -f api
curl http://localhost:8000/health     # the API directly, skipping nginx
curl http://localhost/health          # through nginx
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

| Symptom | Usually |
| --- | --- |
| Gateway shows **502** | backend pool or probe not on port 80 / `/health`, or the VM's network security group blocks the gateway |
| `:8000` works, `localhost/health` fails | nginx: check `sudo nginx -t` and its error log |
| `/health` shows `database_error` | wrong `DATABASE_URL`, an unencoded `@` in the password, or the database's firewall |
| A `postgres` container appears in `docker ps` | `COMPOSE_FILE` is missing from `.env` |
| `permission denied` on `docker` | log out and back in after `setup_vm.sh` |
| OTP returns 502 | the WhatsApp token in `.env` |

## Loose ends

- A `gradorra_pgdata` Docker volume on the VM is left from a deploy that ran
  the wrong compose file. Nothing depends on it; remove it with
  `docker volume rm gradorra_pgdata` once you are sure.
- The API connects as the database admin user. Before launch, create a user with
  rights on the `gradorra` database only and use that in `DATABASE_URL`.
- The rate limiter reads the client's IP from `X-Forwarded-For`. Check its format
  in the nginx access log once real traffic comes through the gateway. The
  gateway can append a port, which breaks per-IP limiting.
