#!/usr/bin/env bash
# GridBot in a Git Bash window -- the Startup-folder launcher.
# Same behaviour as run.bat: restart on crash, never start a twin.
#   0 = clean stop, 3 = already running from this folder, 2 = could not start.
cd /d/GridBot || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

while true; do
  python -X utf8 gridbot.py "$@"
  rc=$?
  case $rc in
    0) exit 0 ;;
    3) echo "GridBot is already running from this folder."; sleep 8; exit 0 ;;
    2) echo "GridBot could not start: see the message above and gridbot.log"
       read -rp "Press Enter to close. "; exit 2 ;;
  esac
  echo "GridBot exited with code $rc -- restarting in 15s. Close this window to stop it."
  sleep 15
done
