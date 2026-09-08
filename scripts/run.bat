@echo off
REM ===========================================================================
REM  Start Inventory Autopilot in the foreground.
REM
REM  Double-click this file, or run it from cmd. Press Ctrl+C to stop it.
REM
REM  This is for MANUAL testing -- Part 1 and Part 2 of SETUP.md, before the
REM  Scheduled Task in Step 11 takes over. Once that task is running, the port
REM  is already in use and this will refuse to start; stop the task first:
REM      Stop-ScheduledTask -TaskName InventoryAutopilot
REM
REM  WHY THE cd ON THE FIRST LINE: this file lives in scripts\ but the
REM  application has to run from the REPOSITORY ROOT, because app/config.py
REM  reads ".env" as a path relative to the working directory. Started from
REM  anywhere else the app reports MASTER_KEY as missing while the file sits
REM  there looking perfectly correct. Moving the scripts into scripts\ broke
REM  all six of them in exactly this way once already, so every script here
REM  resolves paths from the PARENT of its own directory.
REM ===========================================================================

setlocal
cd /d "%~dp0.."

REM Python 3.14 colours tracebacks, and some of those colours are unreadable on
REM the default console -- an error message can come out completely blank. That
REM cost a real debugging round trip on the first deployment. Off, always.
set PYTHON_COLORS=0

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   ERROR: no Python environment found at .venv
    echo   Run Step 4 of SETUP.md first ^(Set up Python for this project^).
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo   ERROR: .env is missing, so there are no settings to start with.
    echo   Run Step 5 of SETUP.md first ^(scripts\setup-env.ps1^).
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   Inventory Autopilot - running in the foreground
echo  ============================================================
echo   Dashboard : http://127.0.0.1:8000
echo   Stop it   : press Ctrl+C, then answer Y to
echo               "Terminate batch job (Y/N)?"
echo.
echo   Closing this window also stops it. That is expected while
echo   testing; Step 11 of SETUP.md is what makes it permanent.
echo  ============================================================
echo.

.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

REM Deliberately pause on exit. Without it the window vanishes the instant
REM anything goes wrong and takes the reason with it -- which is the single
REM most annoying way to debug a start-up failure.
echo.
echo  ------------------------------------------------------------
echo   The application has stopped.
echo.
echo   If that was not you pressing Ctrl+C, the reason is above.
echo   A common one: "address already in use" means it is already
echo   running, most likely as the InventoryAutopilot Scheduled Task.
echo  ------------------------------------------------------------
pause
