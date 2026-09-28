#!/usr/bin/env bash
# Adds the site login to .env as PATCHBAY_AUTH (username + hashed password).
set -euo pipefail
cd "$(dirname "$0")/.."
read -rp "username: " user
read -rsp "password: " pass; echo
line="PATCHBAY_AUTH='$user:$(openssl passwd -apr1 "$pass")'"
touch .env
grep -v '^PATCHBAY_AUTH=' .env > .env.tmp || true
echo "$line" >> .env.tmp && mv .env.tmp .env
echo "saved to .env; restart with the prod compose command to apply"
