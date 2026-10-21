#!/bin/sh
# Minimal `timedatectl status` shim for a systemd-less guest (see Dockerfile.osworld for why:
# this image is supervisord-based, not systemd). Reproduces exactly the multi-line format
# desktop_env.evaluators.metrics.basic_os.is_utc_0 parses (it reads line index 3 verbatim and
# checks it ends with "+0000)") -- driven from /etc/timezone + `date`, which the image's tzdata
# already keeps in sync with any /etc/localtime symlink an agent or task config writes.
if [ "$1" != "status" ] && [ -n "$1" ]; then
    echo "timedatectl-shim: unsupported subcommand '$1'" >&2
    exit 1
fi
TZ_NAME=$(cat /etc/timezone 2>/dev/null || echo "Etc/UTC")
NOW_LOCAL=$(date +"%a %Y-%m-%d %H:%M:%S %Z")
NOW_UTC=$(TZ=UTC date +"%a %Y-%m-%d %H:%M:%S UTC")
TZ_ABBR=$(date +"%Z")
TZ_OFFSET=$(date +"%z")
cat <<EOF
               Local time: $NOW_LOCAL
           Universal time: $NOW_UTC
                 RTC time: $NOW_UTC
                Time zone: $TZ_NAME ($TZ_ABBR, $TZ_OFFSET)
System clock synchronized: yes
              NTP service: inactive
          RTC in local TZ: no
EOF
