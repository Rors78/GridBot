' GridBot boot launcher: run.bat with NO window, detached from any console.
' Run by the Scheduled Task "GridBot" at logon (schtasks /run /tn GridBot to
' test). Replaces the old Startup shortcut that opened a Git Bash tab: close
' that tab and the bot died with it (journal audit 2026-09-24).
' A second start is harmless: gridbot.py's instance lock exits with code 3.
CreateObject("WScript.Shell").Run """D:\GridBot\run.bat""", 0, False
