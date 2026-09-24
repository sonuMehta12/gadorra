# Deploying to the Azure VM

The layout the client set up:

```
Internet ──HTTPS──> Application Gateway ──HTTP :80──> nginx ──> API :8000 (Linux VM, private)
                    (TLS cert from Key Vault)            ├─ api  (this repo, Docker)
                                                         └─ db   (Postgres, Docker, not exposed)

You ──RDP──> Windows jump box (public IP) ──SSH──> Linux VM
```

TLS ends at the gateway, so nothing on the VM deals with certificates. The API
listens on plain HTTP port 8000 and Postgres is reachable only from the API
container.

## The client's actual setup (found on the VM)

- API VM: `vm-gradorra-api-prod-prep-001`, Ubuntu 24.04
- Database: **Azure Database for PostgreSQL**, `10.10.3.4:5432`, Postgres 17,
  admin user `gradorradbadmin`, TLS required. Use `docker-compose.azure.yml` --
  it runs only the API and does not start a db container.
- Front end: Next.js on another VM, `10.10.1.8:3000`

### Azure database, one time

From the API VM, create the application database (you were in the default
`postgres` database):

```bash
psql "host=10.10.3.4 port=5432 dbname=postgres user=gradorradbadmin sslmode=require"
```
```sql
CREATE DATABASE gradorra;
\q
```

Then in `.env`:

```
COMPOSE_FILE=docker-compose.azure.yml
DATABASE_URL=postgresql://gradorradbadmin:<password>@10.10.3.4:5432/gradorra?sslmode=require
```

If the password contains `@ : / # ? %` or spaces, URL-encode them (`@` → `%40`,
`#` → `%23`, `%` → `%25`), or the URL is misread. `AUTO_INIT_DB` builds the
tables and the 75 districts on first start.

## What you need first

From the client's infra person:

- the Linux VM's **private IP**
- its **SSH username**, and a password or key
- whether the database stays **in Docker on the VM** (what these files do) or
  moves to **Azure Database for PostgreSQL**
- the gateway's **domain**, and confirmation that its backend points at the VM on
  **port 8000** with health probe path **`/health`**

## 1. Get onto the jump box

On a Mac, install **Windows App** (formerly Microsoft Remote Desktop) from the
App Store. Add PC → the jump box's public IP → sign in as the user you were given.

## 2. From the jump box, get onto the Linux VM

Open **PowerShell** on the jump box:

```powershell
ssh <linux-user>@<linux-private-ip>
```

Windows 10/11 and Server 2019+ ship with the `ssh` command. If it is missing,
use PuTTY.

## 3. One-time setup on the VM

The repo (`003aja/gradorra-gpet-api`) is **private**, so the VM needs its own
read-only key to clone it.

**a. Make a deploy key on the VM**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gradorra_deploy -N "" -C "vm-gradorra-api-prod-prep-001"
printf 'Host github.com\n  IdentityFile ~/.ssh/gradorra_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
cat ~/.ssh/gradorra_deploy.pub
```

**b. Add it to the repo.** Copy the line that starts with `ssh-ed25519`. On
GitHub: the repo → **Settings → Deploy keys → Add deploy key** → paste it, title
it after the VM, and leave **Allow write access unticked**. This needs admin on
the repo; if you do not have it, send the line to the repo owner.

**c. Clone and set up**

```bash
ssh -T git@github.com            # answer "yes" once; it should greet the repo
git clone git@github.com:003aja/gradorra-gpet-api.git ~/gradorra
cd ~/gradorra && bash deploy/setup_vm.sh
```

`setup_vm.sh` installs Docker from Docker's own apt repository and creates `.env`
with a random JWT secret (and a database password, used only by the
Docker-Postgres setup).

**Log out and SSH back in once**, so your user picks up the `docker` group.

## 4. Fill in the secrets

```bash
cd ~/gradorra
nano .env
```

| Variable | Value |
| --- | --- |
| `WHATSAPP_TOKEN` | the permanent system-user token |
| `WHATSAPP_PHONE_NUMBER_ID` | `1355994020929287` |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | `1290804439764703` |
| `OTP_CHANNEL` | `whatsapp_template` |
| `WHATSAPP_PROVIDER` | `meta` |
| `WHATSAPP_AUTH_TEMPLATE_BUTTON` | `true` |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | test pair on dev, live pair only on prod |
| `RAZORPAY_WEBHOOK_SECRET` | from the Razorpay dashboard |
| `CORS_ORIGINS` | the front end's domain(s), comma separated -- never `*` on prod |
| `APP_ENV` | `production` |

Leave `DATABASE_URL` alone: `docker-compose.prod.yml` points it at the db
container. `POSTGRES_PASSWORD` and `JWT_SECRET` were generated in step 3.

Save in nano with Ctrl+O, Enter, Ctrl+X.

## 5. Deploy

```bash
bash deploy/deploy.sh
```

It refuses to start if a required secret is empty, pulls the latest code,
rebuilds, restarts, and waits for `/health` to report ok. On failure it prints
the last API logs.

Check from the VM:

```bash
curl http://localhost:8000/health
```

and from outside, once the gateway points at it:

```
https://<gateway-domain>/health
https://<gateway-domain>/docs
```

## 6. nginx in front (the gateway talks to port 80)

The Application Gateway sends traffic to the VM on port 80, so nginx sits there
and proxies to the API, which listens only on `127.0.0.1:8000`.

```bash
sudo apt-get install -y nginx
cd ~/gradorra && git pull
sudo cp deploy/nginx/gradorra-api.conf /etc/nginx/sites-available/gradorra-api
sudo ln -sf /etc/nginx/sites-available/gradorra-api /etc/nginx/sites-enabled/gradorra-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
bash deploy/deploy.sh
curl http://localhost/health
```

`deploy.sh` is re-run so the API rebinds to loopback. The gateway's backend
should point at the VM's private IP on **port 80**, health probe path `/health`.

## Every later deploy

```bash
cd ~/gradorra && bash deploy/deploy.sh
```

Data lives in the `pgdata` Docker volume and survives rebuilds and restarts.

## When something is wrong

```bash
docker compose -f docker-compose.prod.yml ps               # both containers Up?
docker compose -f docker-compose.prod.yml logs -f api      # live API logs
docker compose -f docker-compose.prod.yml logs --tail 50 db
curl http://localhost:8000/health                          # reports database_error when it cannot connect
```

| Symptom | Usually |
| --- | --- |
| Gateway shows **502** | backend pool or probe not pointing at VM port 8000 / `/health`, or the VM's network security group blocks the gateway |
| `/health` shows `database_error` | the db container is down -- check its logs |
| Works from the VM, not from outside | gateway or NSG, not the app |
| OTP returns 502 | WhatsApp token wrong in `.env` |

## Backups

The database is a Docker volume on one VM. Take a dump before anything risky,
and put this on a daily cron before launch:

```bash
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U gradorra gradorra | gzip > ~/backup-$(date +%F).sql.gz
```

For launch, Azure Database for PostgreSQL gives managed backups and failover.
Moving to it is a `DATABASE_URL` change plus dropping the `db` service.
