#!/bin/sh
# Install the mailbox's interactive team command for the current user.
set -eu
fail() { printf '%s\n' "$1" >&2; exit 1; }

package=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
mailbox=$package
check=0
if [ "${1:-}" = --check ]; then
    check=1
    if [ -n "${2:-}" ]; then mailbox=$2; fi
fi
launcher="$mailbox/team"
bin="$HOME/.local/bin"
link="$bin/team"

for command_path in "$launcher" "$mailbox/copilot-mailbox/team" "$mailbox/copilot-mailbox/start"; do
    [ -f "$command_path" ] && [ -x "$command_path" ] ||
        fail 'A team launcher is missing or is not executable. Run make setup.'
done
[ -f "$mailbox/copilot-mailbox/team_ui_data.py" ] &&
    [ -f "$mailbox/copilot-mailbox/team_control.py" ] && [ -f "$mailbox/projects.json" ] ||
    fail 'The team installation is incomplete. Run make setup.'
python3 -c 'import curses, fcntl, sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 ||
    fail 'team requires Python 3.8 or later with curses. Run make setup.'
python3 -B - "$package/copilot-mailbox" "$mailbox" 2>/dev/null <<'PYTHON' ||
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from team_ui_data import projects

projects(Path(sys.argv[2]))
PYTHON
    fail 'Cannot read projects.json as a team registry. Check its format and project IDs.'

windows_command() {
    # During diagnosis the installed copy may predate support for read-only checks.
    windows_installer="$package/copilot-mailbox/install-windows-command.ps1"
    command -v powershell.exe >/dev/null 2>&1 && [ -r "$windows_installer" ] ||
        fail 'To use team from Windows, enable WSL interoperability and make powershell.exe available on your PATH.'
    wsl_user=$(id -un 2>/dev/null) || fail 'Cannot identify the Linux account that owns team.'
    HARNESS_TEAM_DISTRO="$WSL_DISTRO_NAME" HARNESS_TEAM_CHECK="$check" HARNESS_TEAM_USER="$wsl_user" \
        WSLENV="HARNESS_TEAM_DISTRO/w:HARNESS_TEAM_USER/w:HARNESS_TEAM_CHECK/w${WSLENV:+:$WSLENV}" \
        powershell.exe -NoLogo -NoProfile -NonInteractive -Command - < "$windows_installer" >/dev/null 2>&1 ||
        fail 'Windows team command or user PATH is unavailable or in conflict. Run make setup and reopen the terminal.'
}

# Keep an existing command (including a dangling link) owned by another tool.
existing=$(command -v team || :)
if [ -n "$existing" ] && ! [ "$existing" -ef "$launcher" ]; then
    fail 'Another team command is already on your PATH.'
fi
if [ "$check" = 1 ]; then
    [ "$link" -ef "$launcher" ] || fail 'The team command is missing or belongs to another mailbox. Run make setup.'
    [ -n "$existing" ] || fail 'team is installed outside the current PATH. Open a new terminal after make setup.'
    if [ -n "${WSL_DISTRO_NAME:-}" ]; then windows_command; fi
    printf '%s\n' 'OK: team launcher and PATH'
    exit 0
fi
mkdir -p "$bin" 2>/dev/null || fail 'Cannot access your local bin directory.'
# A link whose target is gone (a moved or removed checkout) holds no command, so replacing it loses nothing.
if [ -L "$link" ] && [ ! -e "$link" ]; then
    ln -sfn "$launcher" "$link" 2>/dev/null || fail 'Cannot replace the broken team symlink.'
elif [ -e "$link" ] || [ -L "$link" ]; then
    [ "$link" -ef "$launcher" ] || fail 'The path ~/.local/bin/team is already in use.'
else
    ln -s "$launcher" "$link" 2>/dev/null || fail 'Cannot create the symlink for team.'
fi

# Add the user bin to fresh interactive shells when the caller does not have it yet.
path_rule='export PATH="$HOME/.local/bin:$PATH"'
shell_path=${TEAM_SHELL_PATH:-$PATH}
brew_bin=
for program in tmux python3; do
    program_path=$(command -v "$program" || :)
    case "$program_path" in
        /opt/homebrew/bin/*|/usr/local/bin/*)
            case ":$shell_path:" in
                *":${program_path%/*}:"*) ;;
                *) brew_bin=${program_path%/*} ;;
            esac ;;
    esac
done
if [ -n "$brew_bin" ]; then path_rule="export PATH=\"\$HOME/.local/bin:\$PATH:$brew_bin\""; fi
add_path() {
    profile=$1
    if ! grep -Fqx "$path_rule" "$profile" 2>/dev/null; then
        (printf '\n%s\n' "$path_rule" >> "$profile") 2>/dev/null ||
            fail 'Cannot update your shell profile.'
    fi
}
case ":$shell_path:" in
    *":$bin:"*) configure_path=$brew_bin ;;
    *) configure_path=1 ;;
esac
if [ -n "$configure_path" ]; then
    login_shell=${SHELL:-sh}
    case "${login_shell##*/}" in
        zsh) add_path "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash)
            add_path "$HOME/.bashrc"
            for profile in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
                if [ -e "$profile" ] || [ "$profile" = "$HOME/.profile" ]; then
                    add_path "$profile"
                    break
                fi
            done ;;
        fish) if [ -n "$brew_bin" ]; then
                  "$login_shell" -c "fish_add_path --append $brew_bin" 2>/dev/null || fail 'Cannot add Homebrew to your fish PATH.'
              fi
              "$login_shell" -c 'fish_add_path "$HOME/.local/bin"' 2>/dev/null ||
            fail 'Add ~/.local/bin to your fish PATH.' ;;
        sh|dash|ksh|'') add_path "$HOME/.profile" ;;
        *) printf '%s\n' 'Add ~/.local/bin to your shell PATH.' ;;
    esac
fi

if [ -n "${WSL_DISTRO_NAME:-}" ]; then
    windows_command
fi
printf '%s\n' 'team is installed. Open a new terminal and run team.'
