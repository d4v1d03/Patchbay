#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu VM, run from the unpacked repo:
#   bash deploy/setup-vm.sh
# Installs Docker, opens ports 80/443 in the VM's own firewall, adds swap and
# builds the sandbox image. Tested on Oracle Cloud's Ampere (ARM) Ubuntu images.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"

# Oracle's Ubuntu images reject everything except SSH in iptables
if ! sudo iptables -C INPUT -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT 2>/dev/null; then
  sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT
  sudo netfilter-persistent save 2>/dev/null || sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
fi

# npm installs inside sandboxes can spike memory; swap keeps the VM responsive
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo docker build -t patchbay-sandbox:latest sandbox/
echo "done. Next: create .env (see deploy/README.md), then:"
echo "  sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
