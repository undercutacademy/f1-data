@echo off
rem One-click F1 data ingest: fetch new sessions, validate, push, purge CDN.
rem Double-click to run manually, or let Task Scheduler call it with --max-wait 0.
cd /d "%~dp0"
uv run --with "fastf1==3.8.3" python scripts\local_ingest.py %*
set RC=%ERRORLEVEL%
if "%1"=="" pause
exit /b %RC%
