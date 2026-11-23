#!/bin/sh
# System bootstrap for local development. Called only by setup, never by doctor.
set -eu
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"
export PATH
DOCKER=${DOCKER:-docker}
DOCKER_WAIT_SECONDS=${DOCKER_WAIT_SECONDS:-120}
case "$DOCKER_WAIT_SECONDS" in ''|*[!0-9]*) printf 'ERROR: DOCKER_WAIT_SECONDS must be a nonnegative integer.\n' >&2; exit 1 ;; esac
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
installer_tmp=
trap '[ -z "$installer_tmp" ] || rm -f "$installer_tmp"' EXIT HUP INT TERM

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 || fail 'System installation needs sudo or a root shell. Install sudo or rerun the bootstrap with administrator access.'
        sudo "$@" || fail 'Administrator command failed. Resolve sudo authentication/package-manager errors and rerun make setup.'
    fi
}

download() {
    printf 'Downloading installer/dependency from %s\n' "$1"
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 "$1" --output "$2" || fail 'Dependency download failed; no downloaded installer was executed. Check network access and rerun make setup.'
}

compose_usable() {
    "$DOCKER" compose version >/dev/null 2>&1 || return 1
    config_help=$("$DOCKER" compose config --help) || return 1
    case "$config_help" in *--environment*) ;; *) return 1 ;; esac
    up_help=$("$DOCKER" compose up --help) || return 1
    case "$up_help" in *--wait*) return 0 ;; *) return 1 ;; esac
}

team_python_usable() {
    python3 -c 'import curses, fcntl, sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1
}

tmux_usable() {
    tmux -V >/dev/null 2>&1
}

# A complete working installation is never upgraded or restarted.
if make --version >/dev/null 2>&1 && "$DOCKER" --version >/dev/null 2>&1 && compose_usable &&
   "$DOCKER" info >/dev/null 2>&1 && team_python_usable && tmux_usable; then
    printf 'OK: system dependencies already available; no installation needed.\n'
    exit 0
fi

platform=$(uname -s)
case "$platform" in
    Darwin)
        brew_cmd=$(command -v brew || true)
        if [ -z "$brew_cmd" ]; then
            for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
                if [ -x "$candidate" ]; then brew_cmd=$candidate; break; fi
            done
        fi
        if ! make --version >/dev/null 2>&1 || ! "$DOCKER" --version >/dev/null 2>&1 ||
           ! compose_usable || ! team_python_usable || ! tmux_usable; then
            if [ -z "$brew_cmd" ]; then
                command -v curl >/dev/null 2>&1 || fail 'macOS curl is unavailable; restore the system curl before bootstrapping Homebrew.'
                installer_tmp=$(mktemp "${TMPDIR:-/tmp}/mailbox-homebrew.XXXXXX")
                download https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh "$installer_tmp"
                /bin/bash "$installer_tmp" || fail 'Homebrew installation did not complete. Finish its administrator/Command Line Tools prompts, then rerun the bootstrap.'
                for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
                    if [ -x "$candidate" ]; then brew_cmd=$candidate; break; fi
                done
                [ -n "$brew_cmd" ] || fail 'Homebrew installed outside a supported prefix; add brew to PATH and rerun setup.'
            fi
            if ! team_python_usable; then
                "$brew_cmd" install python || fail 'Homebrew could not install Python for team. Check Homebrew and rerun make setup.'
            fi
            if ! tmux_usable; then
                "$brew_cmd" install tmux || fail 'Homebrew could not install tmux. Check Homebrew and rerun make setup.'
            fi
            if ! make --version >/dev/null 2>&1; then
                "$brew_cmd" install make
                make_prefix=$("$brew_cmd" --prefix make)
                PATH="$make_prefix/libexec/gnubin:$PATH"
                export PATH
                make --version >/dev/null 2>&1 || fail 'Installed GNU Make is not executable. Check Homebrew make and its libexec/gnubin directory.'
                printf 'GNU Make installed. For subsequent commands, add Homebrew opt/make/libexec/gnubin to your shell PATH (see README).\n'
            fi
            if ! "$DOCKER" --version >/dev/null 2>&1 || ! compose_usable; then
                if "$brew_cmd" list --cask docker-desktop >/dev/null 2>&1; then
                    "$brew_cmd" upgrade --cask docker-desktop
                else
                    "$brew_cmd" install --cask docker-desktop
                fi
            fi
        fi
        if ! "$DOCKER" info >/dev/null 2>&1; then
            open -a "${DOCKER_DESKTOP_APP:-/Applications/Docker.app}" || fail 'Docker Desktop could not start. Open Docker Desktop, finish first-run setup, then rerun make setup.'
        fi
        ;;
    Linux)
        # A distribution without Docker Desktop follows the native Linux installer.
        if [ -n "${WSL_DISTRO_NAME:-}" ] && ! command -v dockerd >/dev/null 2>&1 &&
           { command -v docker.exe >/dev/null 2>&1 || [ -d /mnt/wsl/docker-desktop ]; } &&
           { ! "$DOCKER" --version >/dev/null 2>&1 || ! compose_usable || ! "$DOCKER" info >/dev/null 2>&1; }; then
            fail 'Docker is unavailable in WSL. Start Docker Desktop on Windows, enable WSL 2 integration for this distribution, then rerun make setup.'
        fi
        if command -v apt-get >/dev/null 2>&1; then
            package_manager=apt
        elif command -v dnf >/dev/null 2>&1; then
            package_manager=dnf
        else
            fail 'Automatic Linux bootstrap supports apt-get or dnf. Install Make, curl, Python 3 with curses, tmux, Docker Engine and Compose for your distribution, then rerun make setup.'
        fi
        packages=
        make --version >/dev/null 2>&1 || packages="$packages make"
        command -v curl >/dev/null 2>&1 || packages="$packages curl ca-certificates"
        if ! team_python_usable; then
            if [ "$package_manager" = apt ]; then
                packages="$packages python3 libpython3-stdlib"
            else
                packages="$packages python3 python3-libs"
            fi
        fi
        tmux_usable || packages="$packages tmux"
        if [ -n "$packages" ]; then
            if [ "$package_manager" = apt ]; then
                as_root apt-get update
                # Intentional splitting: packages contains only the fixed names above.
                as_root apt-get install -y --no-install-recommends $packages
            else
                as_root dnf install -y $packages
            fi
        fi
        if ! "$DOCKER" --version >/dev/null 2>&1; then
            installer_tmp=$(mktemp "${TMPDIR:-/tmp}/mailbox-docker.XXXXXX")
            download https://get.docker.com "$installer_tmp"
            as_root sh "$installer_tmp"
        fi
        if ! compose_usable; then
            # Install only the user's missing/old plugin; preserve the existing engine.
            case "$(uname -m)" in
                x86_64|aarch64|armv7|ppc64le|s390x) architecture=$(uname -m) ;;
                *) fail 'Unsupported Compose CPU architecture. Install the matching Compose plugin manually and rerun setup.' ;;
            esac
            plugin_dir="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins"
            mkdir -p "$plugin_dir"
            installer_tmp=$(mktemp "$plugin_dir/.compose-download.XXXXXX")
            download "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$architecture" "$installer_tmp"
            chmod 755 "$installer_tmp"
            "$installer_tmp" version >/dev/null || fail 'Downloaded Compose plugin failed validation; existing plugin was preserved.'
            mv "$installer_tmp" "$plugin_dir/docker-compose"
            installer_tmp=
        fi
        if ! "$DOCKER" info >/dev/null 2>&1; then
            command -v systemctl >/dev/null 2>&1 || fail 'Docker is installed, but no systemctl is available to start it. Start Docker Engine for this environment and rerun setup.'
            if systemctl is-active --quiet docker; then
                fail 'Docker is running but inaccessible to this user/context. Configure your Docker access, then rerun make setup; socket permissions were not changed.'
            fi
            as_root systemctl start docker
        fi
        ;;
    *) fail 'Automatic bootstrap supports macOS and Linux. On Windows use WSL2, then rerun setup.' ;;
esac

waited=0
until "$DOCKER" info >/dev/null 2>&1; do
    [ "$waited" -lt "$DOCKER_WAIT_SECONDS" ] || fail 'Docker is not ready. Finish Docker Desktop first-run prompts or check Linux daemon access, then rerun make setup.'
    printf 'Waiting for Docker (%s/%s seconds)...\n' "$waited" "$DOCKER_WAIT_SECONDS"
    sleep 2
    waited=$((waited + 2))
done
make --version >/dev/null 2>&1 || fail 'Make is not executable. Finish its system installation and rerun setup.'
compose_usable || fail 'Compose installation is still unavailable/incompatible; check the installer output and rerun make setup.'
team_python_usable || fail 'The python3 on PATH needs Python 3.8 or newer with curses and fcntl. Check your shell PATH and rerun make setup.'
tmux_usable || fail 'tmux is not executable on PATH. Check your shell PATH and rerun make setup.'
printf 'System dependencies are ready.\n'
