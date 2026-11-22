@echo off
rem One-click F1 data ingest: fetch new sessions, validate, push, purge CDN.
rem Double-click to run manually, or let Task Scheduler call it with --max-wait 0.
cd /d "%~dp0"
rem FastF1 3.9+ is required: livetiming needs an F1 TV token (run f1tv-signin.bat once).
uv run --with "fastf1==3.9.2" python scripts\local_ingest.py %*
set RC=%ERRORLEVEL%
if "%1"=="" pause
exit /b %RC%
