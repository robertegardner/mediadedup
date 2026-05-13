#!/usr/bin/env bash
#
# host-setup.sh — Prepare a fresh Ubuntu host for the mediadedup stack.
#
# Installs:
#   • Docker Engine + Compose v2 (from Docker's official APT repo, NOT snap)
#   • NVIDIA Container Toolkit (from NVIDIA's official APT repo)
#
# Does NOT install:
#   • The NVIDIA driver itself — verify yours with `nvidia-smi` before running.
#   • CUDA toolkit on the host — not required, the toolkit injects everything
#     the container needs at runtime.
#
# Usage:
#   chmod +x host-setup.sh
#   sudo ./host-setup.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

# ---- 0. Sanity checks -------------------------------------------------------

. /etc/os-release
if [[ "$ID" != "ubuntu" ]]; then
    echo "Warning: this script is tested on Ubuntu only (detected: $ID $VERSION_ID)" >&2
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "ERROR: nvidia-smi not found on host." >&2
    echo "Install the NVIDIA driver first, e.g.:" >&2
    echo "    sudo ubuntu-drivers autoinstall && sudo reboot" >&2
    exit 1
fi
echo "→ Host driver:"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | sed 's/^/    /'

# ---- 1. Remove conflicting packages -----------------------------------------
# The Snap-installed Docker is sandboxed and CANNOT see /dev/nvidia*. This is
# the #1 reason GPU passthrough silently fails. Purge any prior install.

echo "→ Removing any conflicting Docker packages..."
if command -v snap >/dev/null 2>&1; then
    snap remove --purge docker 2>/dev/null || true
fi
apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# ---- 2. Install Docker Engine from the official repo -----------------------

echo "→ Installing Docker Engine from docker.com APT repo..."
apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Use the host's actual codename (jammy / noble / etc).
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

systemctl enable --now docker

# ---- 3. Install the NVIDIA Container Toolkit --------------------------------

echo "→ Installing NVIDIA Container Toolkit..."
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y nvidia-container-toolkit

# Wire the runtime into Docker's daemon.json and restart.
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# ---- 4. Add the invoking user to the docker group ---------------------------

# When run via sudo, $SUDO_USER is the original user.
TARGET_USER="${SUDO_USER:-${USER}}"
if [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
    usermod -aG docker "$TARGET_USER"
    echo "→ Added '$TARGET_USER' to the docker group."
    echo "  Log out and back in (or run \`newgrp docker\`) for this to take effect."
fi

# ---- 5. Verification --------------------------------------------------------

echo
echo "════════════════════════════════════════════════════════════════════════"
echo " Verifying GPU passthrough into a container..."
echo "════════════════════════════════════════════════════════════════════════"
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi

cat <<'EOF'

════════════════════════════════════════════════════════════════════════
 Host setup complete.
════════════════════════════════════════════════════════════════════════

Next steps:
  1. Mount your NFS shares on the host (rw) — see README §"NFS mounts".
  2. cp .env.example .env   &&   $EDITOR .env
  3. docker compose build
  4. docker compose up -d postgres redis worker web
  5. docker compose --profile tools run --rm doctor   (verify GPU + DB)
  6. docker compose --profile tools run --rm scanner  (discover + queue)
  7. Wait for the queue to drain (visible at http://<host>:8088)
  8. docker compose --profile tools run --rm matcher  (cluster duplicates)

If your shell can't run docker without sudo, log out and back in.
EOF
