# Deploying

Two ways to put Patchbay online: from your own always-on machine through
Tailscale Funnel, or on a cloud VM.

## From your own machine (Tailscale Funnel)

Funnel gives the machine a fixed public HTTPS address
(`https://<machine>.<tailnet>.ts.net`) and forwards it to a local port. It's
free on every Tailscale plan. On macOS it needs the open-source `tailscale`
CLI (Homebrew), not the App Store app.

```bash
cp .env.example .env                  # set LLM_API_KEY; LLM_THINKING=off
docker build -t patchbay-sandbox:latest sandbox/
bash deploy/make-login.sh             # the username and password for the site
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
brew install tailscale && sudo brew services start tailscale
sudo tailscale up                     # sign in in the browser
sudo tailscale funnel --bg 8088       # prints the public address
```

`docker-compose.tunnel.yml` puts the login in front and listens on
`127.0.0.1` only; the tunnel provides HTTPS. Keep the machine on power and
awake (macOS: Battery → Options → prevent automatic sleeping on power adapter
when the display is off; keep the lid open). `sudo tailscale funnel reset`
takes it offline again.

Cloudflare's free quick tunnels don't support Server-Sent Events, which the
live session view uses; a Cloudflare tunnel on your own domain does.

## On a free Oracle Cloud VM

Patchbay needs a machine where it can start Docker containers, so it runs on a
VM rather than a platform like Render. Oracle Cloud's Always Free tier includes
an Arm VM with up to 2 cores and 12 GB of memory, which is enough.

### 1. The VM

1. Sign up at oracle.com/cloud/free. Choose a home region with capacity for
   Arm instances; it can't be changed later.
2. Create an instance: image **Ubuntu 24.04**, shape **VM.Standard.A1.Flex**
   (2 OCPU, 12 GB). Save the SSH private key it offers you.
3. In the instance's subnet → Security List, add ingress rules for TCP ports
   **80** and **443** from `0.0.0.0/0`.
4. Note the instance's public IP.

An Always Free-only account can have idle instances reclaimed. Upgrading the
account to Pay As You Go keeps usage inside the free limits free and avoids
that; set a budget alert.

### 2. A domain name

Let's Encrypt needs a domain. At duckdns.org, sign in and create a subdomain
(e.g. `yourname.duckdns.org`) pointing at the VM's IP.

### 3. Install

```bash
scp -i <key> patchbay-src.zip ubuntu@<ip>:~
ssh -i <key> ubuntu@<ip>
sudo apt-get install -y unzip && unzip patchbay-src.zip && cd patchbay
bash deploy/setup-vm.sh          # Docker, firewall, swap, sandbox image
cp .env.example .env             # set LLM_*, PATCHBAY_DOMAIN, ACME_EMAIL
bash deploy/make-login.sh        # the username and password for the site
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Open `https://<your domain>`; the first request can take a few seconds while
the certificate is issued.

### Operating it

```bash
cd ~/patchbay
alias dc='sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml'
dc ps                    # status
dc logs -f worker        # what the agent is doing
dc up -d --build         # after copying in new code
dc down                  # stop (data volumes are kept)
```

Everything restarts on its own after a reboot. Anyone with the login can run
code in sandboxes on the VM and spend the model key's balance: keep the
password to people you trust, and keep a low balance on the key.
