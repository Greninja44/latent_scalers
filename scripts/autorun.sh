#!/bin/bash

if [ -n "$SUDO_USER" ] || [ -n "$SUDO_UID" ]; then
    echo "This script was executed with sudo."
    echo "Use './scripts/autorun.sh' instead of 'sudo ./scripts/autorun.sh'"
    echo "Exiting..."
    exit 1
fi

# Resolved from this script's location rather than hardcoded to ~/ugv_rpi
# (upstream's directory name), so the repo can be cloned anywhere.
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

# python -u: without it the app's stdout is block-buffered into the log file,
# so [Reactor] lines and tracebacks stay invisible for minutes. An empty log is
# then not evidence that nothing happened.
cron_job1="@reboot XDG_RUNTIME_DIR=/run/user/$(id -u) $REPO_ROOT/ugv-env/bin/python -u $REPO_ROOT/app.py >> ~/ugv.log 2>&1"

cron_job2="@reboot /bin/bash $REPO_ROOT/scripts/start_jupyter.sh >> ~/jupyter_log.log 2>&1"

# Check if the first cron job already exists in the user's crontab
if crontab -l 2>/dev/null | grep -qF "$cron_job1"; then
    echo "First cron job is already set, no changes made."
else
    # Add the first cron job for the user
    (crontab -l 2>/dev/null; echo "$cron_job1") | crontab -
    echo "First cron job added successfully."
fi

# Check if the second cron job already exists in the user's crontab
if crontab -l 2>/dev/null | grep -qF "$cron_job2"; then
    echo "Second cron job is already set, no changes made."
else
    # Add the second cron job for the user
    (crontab -l 2>/dev/null; echo "$cron_job2") | crontab -
    echo "Second cron job added successfully."
fi

"$REPO_ROOT/ugv-env/bin/jupyter" notebook --generate-config
CONFIG_FILE=/home/$(logname)/.jupyter/jupyter_notebook_config.py
if [ -f "$CONFIG_FILE" ]; then
    echo "c.NotebookApp.token = ''" >> $CONFIG_FILE
    echo "c.NotebookApp.password = ''" >> $CONFIG_FILE
    echo "JupyterLab: password/token = ''."
else
    echo "run jupyter notebook --generate-config failed."
fi

echo "Now you can use the command below to reboot."

echo "sudo reboot"