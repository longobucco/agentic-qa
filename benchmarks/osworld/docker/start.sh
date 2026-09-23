#!/bin/sh
set -e

# supervisord autorestarts this script on any crash; a leftover lock/socket from the previous
# Xvfb (orphaned when python crashes after `exec`, or surviving a sandbox stop/start) makes the
# next Xvfb fail to bind :99 -> permanent crash loop. -nolock avoids the lock file entirely.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
# This image runs everything as root ($HOME=/root by default -- there is no `user` account,
# confirmed live via whoami/ps aux). ~870 of 871 vm_file-type evaluator paths in
# data/osworld_verified.jsonl hardcode /home/user/... literally (see Dockerfile.osworld's own
# VS Code fix comment), and apps that resolve their config dir via $HOME (LibreOffice, VLC) need
# $HOME to actually BE /home/user for their state to land where the evaluator looks. Exporting
# this before anything else starts means every process inherits it, same ordering discipline as
# the D-Bus session bus below.
export HOME=/home/user
mkdir -p "$HOME/Desktop" "$HOME/Downloads" "$HOME/.config"
# vlcuser (Dockerfile.osworld -- VLC refuses to run as root) needs write access under the same
# $HOME tree every other app's config/media lives under, including whatever a later task's own
# config step creates here as root (e.g. downloaded media files VLC then needs to read). The
# build-time chmod only covers what existed at build time; each fresh sandbox boot re-applies it
# to whatever this specific task's config just created.
chmod -R o+rwX "$HOME"
# gsettings persistence without a real session/dconf-service: this container has no session bus
# wired for dconf's default backend (see the toolkit-accessibility gsettings call below, which is
# already known to no-op for the same reason). keyfile is glib's own documented backend for
# exactly this -- gsettings set/get read and write a plain file
# ($HOME/.config/glib-2.0/settings/keyfile) instead of round-tripping through dconf-service, so a
# value set in one process is visible to a get in a LATER, separate process (confirmed root cause
# of task 3ce045a0: text-scaling-factor set successfully, read back correctly in the SAME shell,
# but invisible to the evaluator's later, separate `gsettings get` call).
export GSETTINGS_BACKEND=keyfile
Xvfb :99 -screen 0 1920x1080x24 -nolock &
sleep 2

# --- AT-SPI accessibility bus -------------------------------------------------
# Without this block the guest's /accessibility route returns a self-closing
# `<desktop-frame/>` with zero children on EVERY call -- confirmed across all 456 a11y_tree
# captures on disk, every app, every campaign (docs/grounding-harness-plan.md Section 8). The
# image already installs at-spi2-core and python3-pyatspi, so the dependency was never the
# problem: pyatspi reaches the desktop through the accessibility bus, which is bootstrapped from
# a D-Bus SESSION bus, and this container had none. `dbus-run-session`/openbox alone does not
# supply one to the Flask server's own process, which is what has to see it.
#
# Started before openbox and the apps so every later process inherits the address: a toolkit
# bridge only registers at widget-construction time, so an app launched before the bus exists
# stays invisible to AT-SPI for its whole lifetime even after the bus comes up.
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID
fi

# The toolkit side of the same story. GTK3 loads its ATK bridge only when accessibility is
# switched on (gsettings, or this env var as the no-GNOME-session equivalent); GTK2 needs the
# module list; Qt needs its own opt-in. NO_AT_BRIDGE=0 is explicit rather than assumed, because
# several base images ship it set to 1 to silence bridge warnings -- which disables the bridge.
export GTK_MODULES="${GTK_MODULES:+$GTK_MODULES:}gail:atk-bridge"
export GTK_A11Y=atspi
export NO_AT_BRIDGE=0
export QT_ACCESSIBILITY=1
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
# LibreOffice exposes AT-SPI only through its gtk3 VCL backend; with the generic X11 plugin it
# reports nothing at all, which would leave the three libreoffice_* app families -- the largest
# slice of the task set -- blind even with a working bus. libreoffice-gtk3 is installed for this.
export SAL_USE_VCLPLUGIN=gtk3

# GSETTINGS_BACKEND=keyfile (exported above) gives this a writable backend without a session
# daemon, so this now typically succeeds rather than silently failing -- the env vars above
# already carry the same switch either way, so this call staying belt-and-suspenders (|| true)
# costs nothing if it ever doesn't.
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

# These two live in /usr/libexec on Ubuntu 22.04 and in /usr/lib/at-spi2-core on older releases,
# and a hardcoded path that misses is exactly the kind of thing that fails silently here: the
# bus would simply never come up and /accessibility would keep returning an empty tree, looking
# identical to the bug this block is fixing. Resolve, and say so in the log when not found.
find_atspi() {
    for p in "/usr/libexec/$1" "/usr/lib/at-spi2-core/$1" "/usr/lib/$1"; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    command -v "$1" 2>/dev/null && return 0
    return 1
}

# --launch-immediately: otherwise the bus is only created on D-Bus activation by the first
# client, and the registry may not be up when the first /accessibility call arrives.
if launcher="$(find_atspi at-spi-bus-launcher)"; then
    "$launcher" --launch-immediately &
    sleep 1
else
    echo "WARNING: at-spi-bus-launcher not found -- /accessibility will return an empty tree" >&2
fi
if registryd="$(find_atspi at-spi2-registryd)"; then
    "$registryd" &
    sleep 1
else
    echo "WARNING: at-spi2-registryd not found -- /accessibility will return an empty tree" >&2
fi

# reads /etc/xdg/openbox/rc.xml by default (see docker/openbox-rc.xml -- Ctrl+Alt+T -> xterm)
openbox &
sleep 1

# main.py does `from pyxcursor import Xcursor` (sibling import) — must run from its own dir,
# not as `python3 -m desktop_env.server.main` (that would put /app on sys.path, not this dir).
cd /app/desktop_env/server
exec python3 main.py
