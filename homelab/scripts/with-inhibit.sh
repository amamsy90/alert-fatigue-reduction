#!/usr/bin/env bash
# Hold a systemd idle/sleep inhibit for the command that follows.
# Closing the laptop lid can still suspend the machine: logind's lid switch
# is separate from this lock. Collection state is on disk, so the next
# ./scripts/up.sh continues from the last fault and the last scrape.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: with-inhibit.sh command..." >&2
  exit 2
fi

if command -v systemd-inhibit >/dev/null 2>&1; then
  if systemd-inhibit --what=idle:sleep --who=homelab --why="home lab collection" -- true; then
    exec systemd-inhibit --what=idle:sleep --who=homelab --why="home lab collection" -- "$@"
  fi
  echo "warning: systemd-inhibit refused idle:sleep; the laptop may still suspend" >&2
else
  echo "warning: systemd-inhibit is missing; the laptop may sleep and pause collection" >&2
fi
exec "$@"
