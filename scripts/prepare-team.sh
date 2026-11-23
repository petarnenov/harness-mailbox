#!/bin/sh
# Deploy the published team files into the selected mailbox. Runtime records stay local.
set -eu
app=$(CDPATH= cd -P -- "$(dirname -- "$0")/.." && pwd -P)
root=$(CDPATH= cd -P -- "$1" && pwd -P)
cd "$app/workspace"

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
present() { [ -e "$1" ] || [ -L "$1" ]; }
# Walk the destination one component at a time, before writing through it.
ensure_dir() {
    case "$1" in */*) ensure_dir "${1%/*}" ;; esac
    [ ! -L "$root/$1" ] || fail 'A team installation directory is a symbolic link. Check the selected mailbox.'
    if [ ! -d "$root/$1" ]; then
        mkdir "$root/$1" 2>/dev/null || [ -d "$root/$1" ] || fail 'Cannot prepare a directory for team.'
    fi
}
temp=
trap '[ -z "$temp" ] || rm -f "$temp"' EXIT
copy_file() {
    src=$1 dest=$2 mode=$3
    if present "$dest"; then
        [ -f "$dest" ] || fail 'An expected team file is occupied by another kind of entry.'
        if [ "$mode" = preserve ]; then return; fi
        [ ! -L "$dest" ] || fail 'A team program is a symbolic link. Check the mailbox installation.'
        if cmp -s "$src" "$dest" && { [ ! -x "$src" ] || [ -x "$dest" ]; }; then return; fi
    fi
    temp=$(mktemp "${dest%/*}/.team-setup.XXXXXX")
    cp -p "$src" "$temp"
    if [ "$mode" = preserve ]; then
        ln "$temp" "$dest" 2>/dev/null || [ -f "$dest" ]
        rm -f "$temp"
    else
        mv -f "$temp" "$dest"
    fi
    temp=
}

if [ ! "$root" -ef "$app" ]; then
    if ! present "$root/dashboard"; then ln -s "$app" "$root/dashboard"; fi
    [ "$root/dashboard" -ef "$app" ] || fail 'This mailbox already points to a different dashboard checkout.'
fi

# Only ship package files. The source checkout can also contain caches or local configuration.
ensure_dir copilot-mailbox/extensions/mailbox
for file in team install-team.sh copilot-mailbox/start copilot-mailbox/team copilot-mailbox/team_control.py \
    copilot-mailbox/team_ui_data.py copilot-mailbox/install-windows-command.ps1 copilot-mailbox/plugin.json \
    copilot-mailbox/extensions/mailbox/*.mjs; do
    copy_file "$file" "$root/$file" update
done

copy_file "$app/README.md" "$root/README.md" preserve
for file in AGENTS.md CLAUDE.md templates/*.json .github/agents/*.agent.md \
    .agents/skills/mailbox-*/SKILL.md .agents/skills/mailbox-*/references/*.md .agents/skills/mailbox-*/scripts/*.py; do
    ensure_dir "$(dirname "$file")"
    copy_file "$file" "$root/$file" preserve
done
for host in .claude .github; do
    ensure_dir "$host/skills"
    for skill in mailbox-orchestrator mailbox-register-sessions; do
        link="$root/$host/skills/$skill"
        if ! present "$link"; then ln -s "../../.agents/skills/$skill" "$link"; fi
    done
done
