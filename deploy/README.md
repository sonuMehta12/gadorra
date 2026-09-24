# Deploying to the Azure VM

The layout the client set up:

```
Internet ──HTTPS──> Application Gateway ──HTTP :8000──> Linux VM (private)
                    (TLS cert from Key Vault)            ├─ api  (this repo, Docker)
                                                         └─ db   (Postgres, Docker, not exposed)

You ──RDP──> Windows jump box (public IP) ──SSH──> Linux VM
```

TLS ends at the gateway, so nothing on the VM deals with certificates. The API
listens on plain HTTP port 8000 and Postgres is reachable only from the API
container.

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

```bash
curl -fsSL https://raw.githubusercontent.com/sonuMehta12/gadorra/main/deploy/setup_vm.sh -o setup_vm.sh
bash setup_vm.sh
```

This installs Docker from Docker's own apt repository, clones the repo into
`~/gradorra`, and creates `.env` with a random database password and JWT secret
already filled in. If the repo is made private, clone it by hand with a GitHub
token instead.

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
