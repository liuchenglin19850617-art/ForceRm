@echo off
setlocal EnableExtensions
set "TASK=ForceRm AutoClear"
set "SCRIPT=%~dp0autoForceClear.pyw"

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
  if not defined PYW set "PYW=%%i"
)

if /i "%~1"=="/remove" goto REMOVE
if /i "%~1"=="/run" goto RUN

if not exist "%SCRIPT%" (
  echo Missing entry: %SCRIPT%
  exit /b 1
)
if not defined PYW (
  echo pythonw.exe was not found in PATH.
  exit /b 1
)

schtasks /create /tn "%TASK%" /tr "\"%PYW%\" \"%SCRIPT%\"" /sc onlogon /rl highest /f >nul 2>&1
if not errorlevel 1 goto REGISTERED
schtasks /create /tn "%TASK%" /tr "\"%PYW%\" \"%SCRIPT%\"" /sc onlogon /rl limited /f >nul 2>&1
if errorlevel 1 (
  echo Failed to register the scheduled task.
  exit /b 1
)

:REGISTERED
echo Task:       %TASK%
echo Interpreter: %PYW%
echo Entry:       %SCRIPT%
echo Trigger:     at logon, no console window
exit /b 0

:RUN
if not defined PYW (
  echo pythonw.exe was not found in PATH.
  exit /b 1
)
start "" "%PYW%" "%SCRIPT%"
exit /b 0

:REMOVE
schtasks /delete /tn "%TASK%" /f >nul 2>&1
echo Removed: %TASK%
exit /b 0
