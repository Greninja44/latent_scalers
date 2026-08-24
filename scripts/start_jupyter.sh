#!/bin/bash
# Launched from autorun.sh's @reboot cron job, which has no login shell, hence
# the explicit .bashrc source (REACTOR_API_KEY and friends are exported there).
[ -f ~/.bashrc ] && source ~/.bashrc

# Resolved from this script's location, not hardcoded to ~/ugv_rpi.
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

cd "$REPO_ROOT" && exec ./ugv-env/bin/jupyter lab --ip=0.0.0.0 --port=8888 --no-browser
