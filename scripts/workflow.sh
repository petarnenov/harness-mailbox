#!/bin/sh
# All mutations are confined to setup/build/service targets; doctor is read-only.
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_DIR"
TEAM_SHELL_PATH=$PATH
export TEAM_SHELL_PATH
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"
export PATH
DOCKER=${DOCKER:-docker}
PROJECT_NAME=${PROJECT_NAME:-mailbox-dashboard}
# Normal startup selects the reader from the mounted mailbox, never the caller's environment.
DASHBOARD_USER=65534:65534
export DASHBOARD_USER
NODE_IMAGE=${NODE_IMAGE:-node:22-alpine}
TOOLS_IMAGE="${PROJECT_NAME}-tools:local"
SCRIPT_TESTS=workspace/.agents/skills/mailbox-orchestrator/scripts

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
compose() { "$DOCKER" compose --project-name "$PROJECT_NAME" --project-directory "$APP_DIR" --file "$APP_DIR/compose.yaml" "$@"; }

dashboard_state() {
    # A second checkout can share the Compose name, and 'compose run' creates
    # one-off containers for this service without publishing the dashboard port.
    details=$("$DOCKER" ps --all --filter "label=com.docker.compose.project=$PROJECT_NAME" \
        --filter label=com.docker.compose.service=dashboard --filter label=com.docker.compose.oneoff=False \
        --format '{{.ID}} {{.State}} {{.Label "com.docker.compose.project.working_dir"}}') || return 1
    if [ -n "$details" ]; then
        container=${details%% *}
        state_and_owner=${details#* }
        [ "${state_and_owner#* }" -ef "$APP_DIR" ] || fail 'Dashboard container belongs to another Compose directory.'
        if [ -n "${team_root:-}" ]; then
            # The client path label works across WSL/Docker Desktop VM boundaries.
            # Older containers can still be recognized by their existing bind mount.
            mount=$("$DOCKER" inspect --format '{{with index .Config.Labels "io.harness-mailbox.root"}}{{.}}{{else}}{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}{{end}}' "$container") || return 1
            [ "$mount" -ef "$team_root" ] || fail 'The dashboard is configured for another mailbox.'
        fi
        actual_user=$("$DOCKER" inspect --format '{{.Config.User}}' "$container" 2>/dev/null) || fail 'Cannot verify the dashboard reader identity.'
        if [ "$actual_user" != "$DASHBOARD_USER" ]; then
            printf '%s\n' 'replace-reader'
        else
            printf '%s\n' "${state_and_owner%% *}"
        fi
    fi
}

doctor_deps() {
    make --version >/dev/null 2>&1 || fail 'Make is missing or cannot execute. Run sh scripts/install-system.sh and follow its PATH instructions.'
    printf 'OK: Make\n'
    command -v "$DOCKER" >/dev/null 2>&1 || fail 'Docker CLI is missing. Install Docker Desktop (macOS/Windows) or Docker Engine + Compose plugin (Linux), then run make setup.'
    "$DOCKER" --version >/dev/null 2>&1 || fail 'Docker CLI cannot execute. Repair the Docker installation, then run make setup.'
    printf 'OK: Docker CLI\n'
    "$DOCKER" compose version >/dev/null 2>&1 || fail 'Docker Compose plugin is missing. Install/upgrade Docker Desktop or the Compose plugin, then run make setup.'
    config_help=$(compose config --help)
    case "$config_help" in *--environment*) ;; *) fail 'Docker Compose is too old: config --environment is required. Upgrade Compose, then run make doctor.' ;; esac
    up_help=$(compose up --help)
    case "$up_help" in *--wait*) ;; *) fail 'Docker Compose is too old: up --wait is required. Upgrade Compose, then run make doctor.' ;; esac
    printf 'OK: Docker Compose\n'
    "$DOCKER" info >/dev/null 2>&1 || fail 'Docker daemon is unavailable. Start Docker Desktop or Docker Engine and check your Docker permissions, then run make doctor.'
    printf 'OK: Docker daemon\n'
}

doctor_config() {
    compose config --quiet >/dev/null 2>&1 || fail 'Invalid Compose configuration. Run make setup or set MAILBOX_ROOT to your existing mailbox directory.'
    # Compose parses .env safely and applies its normal environment precedence.
    # Do not source .env or print the complete environment (it may contain secrets).
    data_root=$(compose config --environment | sed -n 's/^MAILBOX_ROOT=//p')
    [ -n "$data_root" ] || fail 'MAILBOX_ROOT is empty. Run make setup or supply an existing mailbox directory.'
    case "$data_root" in /*) ;; *) fail 'MAILBOX_ROOT must be an absolute directory path.' ;; esac
    [ -d "$data_root" ] || fail 'MAILBOX_ROOT does not point to an existing directory. Correct .env or supply MAILBOX_ROOT explicitly.'
    [ -f "$data_root/projects.json" ] || fail 'MAILBOX_ROOT has no projects.json. Select the mailbox workspace, or use make demo for synthetic data.'
    printf 'OK: mailbox directory and projects.json\nOK: Compose configuration\n'
}

# Only a dedicated data directory can be read as its owner. The alternative
# layouts also mount a checkout or credentials and keep the image's nobody ID.
select_dashboard_user() {
    mailbox_dir=$(CDPATH= cd -- "$data_root" 2>/dev/null && pwd -P 2>/dev/null) || fail 'Cannot locate the dashboard mailbox.'
    checkout_dir=$(pwd -P)
    [ "$mailbox_dir" != / ] || return 0
    case "$checkout_dir/" in "$mailbox_dir/"*) return ;; esac
    [ ! -e "$mailbox_dir/.git" ] || return 0
    if [ -d "$mailbox_dir/dashboard" ]; then
        dashboard_dir=$(CDPATH= cd -- "$mailbox_dir/dashboard" 2>/dev/null && pwd -P 2>/dev/null) || return 0
        case "$dashboard_dir/" in "$mailbox_dir/"*) return 0 ;; esac
    fi
    for sensitive in "$mailbox_dir/.env" "$mailbox_dir/.env.local"; do
        if [ -L "$sensitive" ]; then
            # The standalone installation can link its token to the sibling
            # checkout. That target stays outside the container's bind mount.
            [ "$sensitive" = "$mailbox_dir/.env.local" ] || return 0
            [ "$(readlink "$sensitive" 2>/dev/null)" = '../.env.local' ] && [ ! -L "$mailbox_dir/../.env.local" ] || return 0
        elif [ -e "$sensitive" ]; then
            return 0
        fi
    done
    # GNU/Linux (including WSL) and BSD/macOS stat use different format flags.
    owner=$(stat -Lc '%u:%g' "$mailbox_dir" 2>/dev/null || stat -Lf '%u:%g' "$mailbox_dir" 2>/dev/null) || fail 'Cannot identify the dashboard mailbox owner.'
    case "$owner" in 0:*|*:0) return ;; esac
    DASHBOARD_USER=$owner
}

# A mailbox either lives in this checkout or holds it as a subdirectory.
detect_mailbox_root() {
    for candidate in "$APP_DIR" "$APP_DIR/.."; do
        if [ -f "$candidate/projects.json" ]; then
            (CDPATH= cd -- "$candidate" && pwd)
            return
        fi
    done
}

prepare_config() {
    if [ ! -e .env ]; then
        data_root=${MAILBOX_ROOT:-$(detect_mailbox_root)}
        if [ -z "$data_root" ]; then
            data_root="$APP_DIR/mailbox-data"
            [ ! -L "$data_root" ] || fail 'The default mailbox-data path is a symbolic link. Select the intended mailbox explicitly.'
            mkdir -p "$data_root" || fail 'Cannot create the local mailbox directory.'
            if [ ! -e "$data_root/projects.json" ]; then
                registry_tmp=$(mktemp "$data_root/.projects.XXXXXX")
                printf '%s\n' '{"projects":[]}' > "$registry_tmp"
                chmod 644 "$registry_tmp"
                ln "$registry_tmp" "$data_root/projects.json" 2>/dev/null || [ -f "$data_root/projects.json" ]
                rm -f "$registry_tmp"
            fi
        fi
        case "$data_root" in /*) ;; *) fail 'MAILBOX_ROOT must be an absolute directory path.' ;; esac
        [ -f "$data_root/projects.json" ] || fail 'Selected mailbox directory has no projects.json; .env was not created.'
        # Compose single quotes preserve literal dollars and hashes in paths.
        escaped_root=$(printf '%s' "$data_root" | sed "s/'/\\\\'/g")
        config_tmp=$(mktemp "$APP_DIR/.env.setup.XXXXXX")
        printf "MAILBOX_ROOT='%s'\n" "$escaped_root" > "$config_tmp"
        # Hard-link publication is atomic and cannot replace a concurrent .env.
        if ln "$config_tmp" "$APP_DIR/.env" 2>/dev/null; then
            printf 'Created .env for the selected mailbox.\n'
        elif [ ! -f "$APP_DIR/.env" ]; then
            rm -f "$config_tmp"
            fail 'Cannot create .env; no existing configuration was replaced.'
        fi
        rm -f "$config_tmp"
    fi
    [ -f .env ] || fail '.env is not a regular configuration file.'
    if [ -f "$APP_DIR/../projects.json" ] && [ ! -e "$APP_DIR/../Makefile" ]; then
        # Install the workspace entrypoint without overwriting another Makefile.
        entry_tmp=$(mktemp "$APP_DIR/../.make.setup.XXXXXX")
        cp "$APP_DIR/workspace.Makefile" "$entry_tmp"
        if ! ln "$entry_tmp" "$APP_DIR/../Makefile" 2>/dev/null && [ ! -f "$APP_DIR/../Makefile" ]; then
            rm -f "$entry_tmp"
            fail 'Cannot install workspace Makefile.'
        fi
        rm -f "$entry_tmp"
    fi
}

build_tools() {
    "$DOCKER" build --file "$APP_DIR/Dockerfile.tools" --tag "$TOOLS_IMAGE" "$APP_DIR"
}

run_python() {
    "$DOCKER" run --rm --network none --read-only --tmpfs /tmp:rw,exec,nosuid,nodev \
        --mount "type=bind,source=$APP_DIR,target=/workspace,readonly" \
        --workdir /workspace "$TOOLS_IMAGE" python -B "$@"
}

case "${1:-help}" in
    doctor-deps) doctor_deps ;;
    doctor)
        status=0
        if ! (PATH=$TEAM_SHELL_PATH; command -v team) >/dev/null 2>&1; then
            printf '%s\n' 'ERROR: team is missing from PATH. Run make setup, then open a new terminal.' >&2
            status=1
        fi
        if ! PATH=$TEAM_SHELL_PATH tmux -V >/dev/null 2>&1; then
            printf '%s\n' 'ERROR: tmux is missing. Run make setup.' >&2
            status=1
        fi
        if ! PATH=$TEAM_SHELL_PATH python3 -c 'import curses, fcntl, sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
            printf '%s\n' 'ERROR: team needs Python 3.8 or later with curses. Run make setup.' >&2
            status=1
        fi
        (doctor_deps) || status=1
        # Compose can validate the selected mailbox even while its daemon is stopped.
        if command -v "$DOCKER" >/dev/null 2>&1; then
            (doctor_config
             [ -f "$data_root/install-team.sh" ] || fail 'The team launcher is missing from the mailbox. Run make setup.'
             PATH=$TEAM_SHELL_PATH sh "$APP_DIR/workspace/install-team.sh" --check "$data_root") || status=1
        fi
        exit "$status" ;;
    setup)
        sh scripts/install-system.sh
        doctor_deps
        prepare_config
        doctor_config
        sh scripts/prepare-team.sh "$data_root"
        sh "$data_root/install-team.sh"
        build_tools
        "$DOCKER" pull "$NODE_IMAGE"
        compose build
        printf 'Setup complete. Open a new terminal and run team.\n'
        ;;
    build) doctor_deps; doctor_config; compose build ;;
    up) doctor_deps; doctor_config; select_dashboard_user; compose up -d --build --wait ;;
    ensure-dashboard)
        team_root=${2:-}
        if [ -n "$team_root" ]; then
            MAILBOX_ROOT=$team_root
            export MAILBOX_ROOT
        elif [ ! -e .env ] && [ -z "${MAILBOX_ROOT:-}" ]; then
            MAILBOX_ROOT=$(detect_mailbox_root)
            export MAILBOX_ROOT
        fi
        doctor_config
        team_root=$data_root
        MAILBOX_ROOT=$data_root
        export MAILBOX_ROOT
        select_dashboard_user
        state=$(dashboard_state)
        case "$state" in
            running) exit 0 ;;
            replace-reader) recreate=--force-recreate ;;
            *) recreate=--no-recreate ;;
        esac
        compose up -d "$recreate" --no-deps --wait dashboard
        state=$(dashboard_state)
        [ "$state" = running ] || fail 'Dashboard service did not start.'
        ;;
    restart) doctor_deps; doctor_config; select_dashboard_user; compose up -d --build --force-recreate --wait ;;
    down) doctor_deps; compose down ;;
    status) doctor_deps; compose ps ;;
    logs) doctor_deps; compose logs --follow --tail 100 dashboard ;;
    health)
        doctor_deps
        health_probe="import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read().decode())"
        health_reply=$(compose exec -T dashboard python -c "$health_probe" 2>/dev/null) || fail 'Dashboard health check failed.'
        printf '%s\n' "$health_reply"
        ;;
    demo)
        doctor_deps
        (export MAILBOX_ROOT="$APP_DIR/demo"; doctor_config; select_dashboard_user; compose up -d --build --wait)
        printf 'Demo fixture mounted; select Explore demo in the dashboard. Run make up to restore live data.\n'
        ;;
    tools) doctor_deps; build_tools ;;
    test)
        doctor_deps
        run_python -m unittest discover -s tests -v
        run_python -m unittest discover -s "$SCRIPT_TESTS" -v
        "$DOCKER" run --rm --network none --read-only --tmpfs /tmp:rw,exec,nosuid,nodev \
            --mount "type=bind,source=$APP_DIR,target=/workspace,readonly" \
            --workdir /workspace "$NODE_IMAGE" sh -c 'node --test tests/js/*.test.mjs'
        ;;
    lint)
        doctor_deps
        sh -n scripts/workflow.sh
        sh -n scripts/install-system.sh
        sh -n scripts/prepare-team.sh
        sh -n workspace/install-team.sh
        "$DOCKER" run --rm --network none --read-only --tmpfs /tmp:rw,exec,nosuid,nodev \
            --mount "type=bind,source=$APP_DIR,target=/workspace,readonly" \
            --workdir /workspace "$NODE_IMAGE" sh -c 'node --check static/app.js && node --check static/theme.js'
        ;;
    coverage)
        doctor_deps
        mkdir -p "$APP_DIR/.coverage-output"
        "$DOCKER" run --rm --network none --read-only --tmpfs /tmp:rw,exec,nosuid,nodev \
            --user "$(id -u):$(id -g)" --env COVERAGE_FILE=/coverage/.coverage \
            --env SCRIPT_TESTS="$SCRIPT_TESTS" \
            --mount "type=bind,source=$APP_DIR,target=/workspace,readonly" \
            --mount "type=bind,source=$APP_DIR/.coverage-output,target=/coverage" \
            --workdir /workspace "$TOOLS_IMAGE" sh -c \
            'measure="python -B -m coverage run --branch --source=server,dependency_graph,dispatch_gate,delivery_process,$SCRIPT_TESTS,workspace/copilot-mailbox,workspace/.agents/skills/mailbox-register-sessions/scripts --omit=*/test_*.py" && $measure -m unittest discover -s tests -v && $measure --append -m unittest discover -s "$SCRIPT_TESTS" -v && python -B -m coverage report -m && python -B -m coverage json -o /coverage/coverage.json && python -B -m coverage html -d /coverage/html'
        ;;
    *) fail 'Unknown command. Run make help.' ;;
esac
