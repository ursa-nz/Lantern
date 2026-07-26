#!/usr/bin/env bash
# Build a shareable Lantern flatpak bundle.
#
# Output: build/nz.ursa.Lantern.flatpak — a single file you can hand to
# anyone, who installs it with `flatpak install --user ./nz.ursa.Lantern.flatpak`.
set -euo pipefail

APP_ID="nz.ursa.Lantern"
MANIFEST="flatpak/${APP_ID}.yaml"
RUNTIME_VERSION="50"
NODE_EXT="org.freedesktop.Sdk.Extension.node22"
NODE_BRANCH="25.08"     # GNOME 50 is still based on freedesktop 25.08

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "${HERE}"

# Build sandbox must be on a filesystem that supports symlinks.  If the
# source tree lives on a cloud-synced mount (e.g. kDrive, Dropbox) that
# blocks symlinks, do the build under ~/.cache and copy out only the
# final bundle.
WORK_ROOT="${HOME}/.cache/lantern-build"
BUILD_DIR="${WORK_ROOT}/builder"
REPO_DIR="${WORK_ROOT}/repo"

# Where flatpak-builder keeps downloaded module sources and its ccache. On a
# desktop this lives under the work root and persists between builds on its own.
# A CI job sets BUILDER_STATE_DIR to a path the runner mounts from the host, so
# the cache outlives the container the job runs in.
STATE_DIR="${BUILDER_STATE_DIR:-${WORK_ROOT}/.flatpak-builder}"
mkdir -p "${WORK_ROOT}" "${STATE_DIR}"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_reset='\033[0m'
log()  { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
warn() { printf "${c_yellow}warn:${c_reset} %s\n" "$*" >&2; }
err()  { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- Tools ----------
have flatpak         || err "flatpak missing. Install: sudo apt install flatpak"
have flatpak-builder || err "flatpak-builder missing. Install: sudo apt install flatpak-builder"

# ---------- Flathub remote ----------
# Ensure flathub at USER scope. Every install below is --user, and a --user
# install cannot resolve a system-scope remote, so a machine that only has
# flathub at system scope fails with "No remote refs found for flathub". Add it
# whenever the user scope lacks it, regardless of a system remote.
if ! flatpak remotes --user --columns=name | grep -qx flathub; then
    log "Adding flathub remote (user scope)..."
    flatpak remote-add --user --if-not-exists flathub \
        https://flathub.org/repo/flathub.flatpakrepo
fi

# ---------- Runtime + SDK ----------
# These are the expensive part, about a gigabyte across the three refs. Every
# call below is user scope, so libflatpak resolves them under FLATPAK_USER_DIR
# when it is set and under ~/.local/share/flatpak when it is not. CI sets it to a
# host path the runner mounts, which is what keeps the pull to the first build.
ensure_installed() {
    local ref="$1"
    if ! flatpak info "${ref}" >/dev/null 2>&1; then
        log "Installing ${ref} from flathub..."
        flatpak install --user -y flathub "${ref}"
    fi
}

ensure_installed "org.gnome.Platform//${RUNTIME_VERSION}"
ensure_installed "org.gnome.Sdk//${RUNTIME_VERSION}"
ensure_installed "${NODE_EXT}//${NODE_BRANCH}"

# ---------- Build ----------
mkdir -p build
BUNDLE="$(pwd)/build/${APP_ID}.flatpak"
MANIFEST_ABS="$(pwd)/${MANIFEST}"   # flatpak-builder resolves sources relative to the manifest

rm -rf "${BUILD_DIR}"

# rofiles-fuse needs /dev/fuse, which isn't exposed in many CI containers
# (even privileged ones). Disable it only when fuse is unavailable so local
# and GitHub builds keep the read-only build-dir protection.
rofiles_args=()
if [ ! -e /dev/fuse ]; then
    warn "/dev/fuse not available — disabling rofiles-fuse for this build"
    rofiles_args+=(--disable-rofiles-fuse)
fi

log "Running flatpak-builder (work dir: ${WORK_ROOT})..."
# No --repo here: the export is a separate step below so it can pass
# --disable-sandbox (flatpak-builder does not forward that flag).
flatpak-builder \
    --force-clean \
    --user \
    --install-deps-from=flathub \
    --state-dir="${STATE_DIR}" \
    "${rofiles_args[@]}" \
    "${BUILD_DIR}" \
    "${MANIFEST_ABS}"

# Export the finished build into the OSTree repo. build-export validates the app
# icon, and by default it does so inside a nested bwrap sandbox that has to
# create a new user namespace — which the kernel refuses in an unprivileged
# container runner ("bwrap: Creating new namespace failed"). That sandbox exists
# to contain a malicious third-party SVG; this icon is our own and trusted, so
# --disable-sandbox validates it directly. This keeps CI's flatpak runner
# unprivileged, and is a no-op on a desktop where the sandbox would have worked.
log "Exporting to repo (icon validator unsandboxed)..."
flatpak build-export --disable-sandbox "${REPO_DIR}" "${BUILD_DIR}"

log "Exporting single-file bundle..."
flatpak build-bundle "${REPO_DIR}" "${BUNDLE}" "${APP_ID}"

log "Done."
echo
echo "Bundle written to:  ${BUNDLE}"
echo "Install locally:    flatpak install --user ${BUNDLE}"
echo "Run:                flatpak run ${APP_ID}"
echo
echo "Share by sending the .flatpak file. Recipient installs with the same command."
