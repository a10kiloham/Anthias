#!/bin/bash

# One-command production install / upgrade for this fork, built FROM
# SOURCE — so fork-only features (first-class playlists, etc.) actually
# end up on the device, which the standard installer's ghcr.io image
# pull can never deliver.
#
# Usage, on the device (Pi 2/3/4/5, x86, or 64-bit ARM SBC):
#
#     ./bin/install_production.sh
#
# or, on a completely fresh device without a clone yet:
#
#     bash <(curl -sL https://raw.githubusercontent.com/a10kiloham/Anthias/master/bin/install_production.sh)
#
# What it does, in order:
#   1. Clones the repo to ~/anthias if it isn't there yet (curl
#      bootstrap case), or syncs your checkout to ~/anthias when run
#      from somewhere else (the path every runtime script expects).
#   2. Runs the standard interactive installer ONCE if the host has
#      never been provisioned (no docker) — that's the supported path
#      for system setup: docker, network manager, ansible host config.
#   3. Builds the anthias-server / viewer / redis images locally from
#      the working tree for this board, tagged exactly where the
#      compose file looks for them.
#   4. Renders docker-compose.yml and (re)starts the stack WITHOUT
#      pulling, so the locally-built images are what runs
#      (MODE=build bin/upgrade_containers.sh).
#
# Safe to re-run any time you pull new code: steps 1-2 no-op, 3-4
# rebuild and restart.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "error: install_production.sh must be run with bash, not sh." >&2
    exit 1
fi

set -euo pipefail

REPOSITORY="https://github.com/a10kiloham/Anthias.git"
ANTHIAS_REPO_DIR="/home/${USER}/anthias"
# Keep in lockstep with bin/install.sh's UV_PIN_VERSION.
UV_PIN_VERSION="0.9.17"

log() { echo -e "\n==> $*"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (it uses sudo where needed)," >&2
    echo "not as root — the stack and repo live under /home/\$USER." >&2
    exit 1
fi

# ---------------------------------------------------------------------
# 1. Make sure the source lives at ~/anthias and run from there.
# ---------------------------------------------------------------------
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [ -z "${SCRIPT_PATH}" ] || [[ "${SCRIPT_PATH}" == /dev/fd/* ]] \
    || [[ "${SCRIPT_PATH}" == /proc/self/fd/* ]]; then
    # curl-pipe bootstrap: no checkout backing this script.
    REPO_ROOT=""
else
    REPO_ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
fi

if [ -z "${REPO_ROOT}" ]; then
    if [ ! -d "${ANTHIAS_REPO_DIR}/.git" ]; then
        log "Cloning ${REPOSITORY} to ${ANTHIAS_REPO_DIR}"
        command -v git >/dev/null || sudo apt-get install -y git
        git clone "${REPOSITORY}" "${ANTHIAS_REPO_DIR}"
    fi
    exec bash "${ANTHIAS_REPO_DIR}/bin/install_production.sh"
elif [ "${REPO_ROOT}" != "${ANTHIAS_REPO_DIR}" ]; then
    log "Syncing ${REPO_ROOT} -> ${ANTHIAS_REPO_DIR}"
    command -v rsync >/dev/null || sudo apt-get install -y rsync
    mkdir -p "${ANTHIAS_REPO_DIR}"
    rsync -a --delete "${REPO_ROOT}/" "${ANTHIAS_REPO_DIR}/"
    exec bash "${ANTHIAS_REPO_DIR}/bin/install_production.sh"
fi

cd "${ANTHIAS_REPO_DIR}"

# ---------------------------------------------------------------------
# 2. One-time system provisioning via the standard installer.
# ---------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    log "Docker not found — running the standard installer once for" \
        "system setup (it will ask a few questions; pick 'latest')."
    bash "${ANTHIAS_REPO_DIR}/bin/install.sh"
fi

# ---------------------------------------------------------------------
# 3. Detect the board. Same decision tree as upgrade_containers.sh —
#    build-target names map 1:1 onto DEVICE_TYPE.
# ---------------------------------------------------------------------
if [ ! -f /proc/device-tree/model ] && [ "$(uname -m)" = "x86_64" ]; then
    DEVICE_TYPE="x86"
elif grep -qF "Raspberry Pi 5" /proc/device-tree/model \
    || grep -qF "Compute Module 5" /proc/device-tree/model; then
    DEVICE_TYPE="pi5"
elif grep -qF "Raspberry Pi 4" /proc/device-tree/model \
    || grep -qF "Compute Module 4" /proc/device-tree/model; then
    DEVICE_TYPE="pi4-64"
elif grep -qF "Raspberry Pi 3" /proc/device-tree/model \
    || grep -qF "Compute Module 3" /proc/device-tree/model; then
    # Userspace arch, not uname -m: 32-bit Pi OS ships a 64-bit kernel.
    if [ "$(dpkg --print-architecture)" = "arm64" ]; then
        DEVICE_TYPE="pi3-64"
    else
        DEVICE_TYPE="pi3"
    fi
elif grep -qF "Raspberry Pi 2" /proc/device-tree/model; then
    DEVICE_TYPE="pi2"
elif [ "$(uname -m)" = "aarch64" ]; then
    DEVICE_TYPE="arm64"
else
    echo "Unsupported device. Anthias supports Pi 2/3/4/5, x86," >&2
    echo "and 64-bit ARM SBCs." >&2
    exit 1
fi
log "Board detected: ${DEVICE_TYPE}"

# ---------------------------------------------------------------------
# 4. Build the images from this working tree.
# ---------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv ${UV_PIN_VERSION}"
    curl -LsSf "https://astral.sh/uv/${UV_PIN_VERSION}/install.sh" | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

# The compose template pins images to ${DOCKER_TAG}-${DEVICE_TYPE}; the
# image builder tags master builds latest-<board> and branch builds
# <branch>-<board>. Match DOCKER_TAG to the checked-out branch so the
# stack runs what we just built, never a pulled upstream image.
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "${GIT_BRANCH}" = "master" ]; then
    export DOCKER_TAG="latest"
else
    export DOCKER_TAG="${GIT_BRANCH}"
fi

log "Building server / viewer / redis images for ${DEVICE_TYPE}" \
    "(tag ${DOCKER_TAG}-${DEVICE_TYPE}; first build takes a while)"
uv run --group docker-image-builder python -m tools.image_builder \
    --build-target "${DEVICE_TYPE}" \
    --service server \
    --service viewer \
    --service redis

# ---------------------------------------------------------------------
# 5. Render compose and (re)start the stack from the local images.
#    MODE=build skips the `docker compose pull` that would otherwise
#    clobber the just-built tags with upstream's images.
# ---------------------------------------------------------------------
log "Starting the stack"
MODE="build" DOCKER_TAG="${DOCKER_TAG}" \
    bash "${ANTHIAS_REPO_DIR}/bin/upgrade_containers.sh"

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Done. Anthias is up — open http://${IP_ADDR:-<device-ip>}/"
