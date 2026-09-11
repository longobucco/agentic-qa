#!/bin/sh
set -e

# supervisord autorestarts this script on any crash; a leftover lock/socket from the previous
# Xvfb (orphaned when python crashes after `exec`, or surviving a sandbox stop/start) makes the
# next Xvfb fail to bind :99 -> permanent crash loop. -nolock avoids the lock file entirely.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1280x1024x24 -nolock &
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

# dconf has no writable backend without a session daemon here, so a failure is expected and
# harmless -- the env vars above already carry the same switch. Never fail the guest over it.
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

openbox &
sleep 1

# main.py does `from pyxcursor import Xcursor` (sibling import) — must run from its own dir,
# not as `python3 -m desktop_env.server.main` (that would put /app on sys.path, not this dir).
cd /app/desktop_env/server
exec python3 main.py
