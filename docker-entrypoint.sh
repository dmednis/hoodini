#!/bin/sh
set -eu
chown -R 999:999 /data
chmod 0700 /data
exec setpriv --reuid=999 --regid=999 --clear-groups hoodini "$@"
