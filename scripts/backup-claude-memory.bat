@echo off
REM Windows launcher for backup-claude-memory.sh
REM Called by Task Scheduler (Claude Memory Backup, daily 04:00 local)
"C:\Program Files\Git\bin\bash.exe" -lc "/d/IAMINE.org/scripts/backup-claude-memory.sh"
