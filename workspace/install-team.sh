#!/bin/sh
# Install the mailbox's interactive team command for the current user.
set -eu
fail() { printf '%s\n' "$1" >&2; exit 1; }

mailbox=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
launcher="$mailbox/team"
bin="$HOME/.local/bin"
link="$bin/team"

for command_path in "$launcher" "$mailbox/copilot-mailbox/team" "$mailbox/copilot-mailbox/start"; do
    [ -f "$command_path" ] && [ -x "$command_path" ] ||
        fail 'A launcher is missing or is not executable. Check team and copilot-mailbox in your installation.'
done
[ -f "$mailbox/copilot-mailbox/team_ui_data.py" ] &&
    [ -f "$mailbox/copilot-mailbox/team_control.py" ] && [ -f "$mailbox/projects.json" ] ||
    fail 'Run the installer from the mailbox directory containing team and projects.json.'
python3 -c 'import curses, fcntl' >/dev/null 2>&1 ||
    fail 'team requires Python 3 with curses on Linux, macOS, or WSL.'

# Keep an existing command (including a dangling link) owned by another tool.
existing=$(command -v team || :)
if [ -n "$existing" ] && ! [ "$existing" -ef "$launcher" ]; then
    fail 'Another team command is already on your PATH.'
fi
mkdir -p "$bin" 2>/dev/null || fail 'Cannot access your local bin directory.'
if [ -e "$link" ] || [ -L "$link" ]; then
    [ "$link" -ef "$launcher" ] || fail 'The path ~/.local/bin/team is already in use.'
else
    ln -s "$launcher" "$link" 2>/dev/null || fail 'Cannot create the symlink for team.'
fi

# Add the user bin to fresh interactive shells when the caller does not have it yet.
path_rule='export PATH="$HOME/.local/bin:$PATH"'
add_path() {
    profile=$1
    if ! grep -Fqx "$path_rule" "$profile" 2>/dev/null; then
        (printf '\n%s\n' "$path_rule" >> "$profile") 2>/dev/null ||
            fail 'Cannot update your shell profile.'
    fi
}
case ":$PATH:" in
    *":$bin:"*) ;;
    *)
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
            fish) "$login_shell" -c 'fish_add_path "$HOME/.local/bin"' 2>/dev/null ||
                fail 'Add ~/.local/bin to your fish PATH.' ;;
            sh|dash|ksh|'') add_path "$HOME/.profile" ;;
            *) printf '%s\n' 'Add ~/.local/bin to your shell PATH.' ;;
        esac ;;
esac

if [ -n "${WSL_DISTRO_NAME:-}" ]; then
    # WSLENV exports only the selected distribution name to the Windows installer.
    windows_installer="$mailbox/copilot-mailbox/install-windows-command.ps1"
    command -v powershell.exe >/dev/null 2>&1 && [ -r "$windows_installer" ] ||
        fail 'To use team from Windows, enable WSL interoperability and make powershell.exe available on your PATH.'
    HARNESS_TEAM_DISTRO="$WSL_DISTRO_NAME" WSLENV="HARNESS_TEAM_DISTRO/w${WSLENV:+:$WSLENV}" \
        powershell.exe -NoLogo -NoProfile -NonInteractive -Command - < "$windows_installer" >/dev/null 2>&1 ||
        fail 'Windows could not register team. Check your user PATH for an existing team command.'
fi
printf '%s\n' 'team is installed. Open a new terminal and run team.'
