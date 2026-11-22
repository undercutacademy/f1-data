@echo off
rem One-time F1 TV sign-in for the laptop ingester (FastF1 >= 3.9).
rem Since Oct 2026 livetiming.formula1.com only serves F1 TV subscribers.
rem FastF1 asks for your F1 account email and password here and stores only the
rem resulting token (in %LOCALAPPDATA%\fastf1). ingest.bat reuses that token.
rem When it expires, run this again - or set F1_USERNAME / F1_PASSWORD as user
rem environment variables so the ingester can renew it on its own.
cd /d "%~dp0"
uv run --with "fastf1==3.9.2" python -m fastf1 auth f1tv --authenticate
echo.
uv run --with "fastf1==3.9.2" python -m fastf1 auth f1tv --status
echo.
pause
