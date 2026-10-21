#!/bin/sh
# Re-execs the real vlc binary (moved to /usr/bin/vlc.real) as vlcuser -- VLC refuses to run at
# all as root ("VLC is not supposed to be run as root. Sorry."). Every task's own official
# launch command still says plain "vlc ..." (e.g. "VLC_VERBOSE=-1 vlc --loop ... file.mp4"), so
# this has to be a drop-in replacement installed at the same path, not a change to how any task
# launches VLC -- see the Dockerfile's own comment above this file's COPY line for the full
# root-cause story and why vlc-wrapper (VLC's own official root-bypass binary) doesn't apply
# here.
#
# VLC_VERBOSE is forwarded explicitly (read from THIS shell's own env, not relying on sudo's
# env_keep configuration) because several official task configs set it inline
# (VLC_VERBOSE=-1 vlc ...) right before invoking vlc in the same shell command.
exec sudo -u vlcuser env \
    DISPLAY="${DISPLAY:-:99}" \
    HOME=/home/user \
    XDG_RUNTIME_DIR=/tmp/runtime-vlcuser \
    VLC_VERBOSE="${VLC_VERBOSE:-}" \
    /usr/bin/vlc.real "$@"
