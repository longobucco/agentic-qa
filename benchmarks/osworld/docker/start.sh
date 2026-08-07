#!/bin/sh
set -e

# supervisord autorestarts this script on any crash; a leftover lock/socket from the previous
# Xvfb (orphaned when python crashes after `exec`, or surviving a sandbox stop/start) makes the
# next Xvfb fail to bind :99 -> permanent crash loop. -nolock avoids the lock file entirely.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1280x1024x24 -nolock &
sleep 2
openbox &
sleep 1

# main.py does `from pyxcursor import Xcursor` (sibling import) — must run from its own dir,
# not as `python3 -m desktop_env.server.main` (that would put /app on sys.path, not this dir).
cd /app/desktop_env/server
exec python3 main.py
