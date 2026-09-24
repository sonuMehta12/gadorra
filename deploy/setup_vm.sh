#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu VM: Docker, the repo, and a .env to fill in.
#   bash setup_vm.sh
set -euo pipefail

# The repo is private: clone over SSH with a deploy key (see deploy/README.md).
REPO_URL="${REPO_URL:-git@github.com:003aja/gradorra-gpet-api.git}"
# Run from inside a checkout and it uses that checkout; otherwise it clones to ~/gradorra.
SCRIPT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
APP_DIR="${APP_DIR:-${SCRIPT_REPO:-$HOME/gradorra}}"

echo "==> installing Docker from Docker's official apt repository"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"

echo "==> fetching the code into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  # a strong database password and JWT secret, generated here so nobody has to invent one
  printf '\nPOSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|" .env
  echo "==> created .env -- fill in the secrets before deploying (see deploy/README.md)"
fi

echo
echo "Done. Log out and back in once (so the docker group applies), then:"
echo "  cd $APP_DIR && nano .env && bash deploy/deploy.sh"
