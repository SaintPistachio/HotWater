@echo off
setlocal DisableDelayedExpansion
set "HOTWATER_COMPONENT=%~1"
if /i "%HOTWATER_COMPONENT%"=="relay" goto launch
if /i "%HOTWATER_COMPONENT%"=="gauge" goto launch
>&2 echo Usage: %~nx0 relay^|gauge
exit /b 2

:launch
if not defined HOTWATER_LOG_DIR set "HOTWATER_LOG_DIR=C:\HotWaterRuntime\logs"
if not exist "%HOTWATER_LOG_DIR%\." mkdir "%HOTWATER_LOG_DIR%"
if not exist "%HOTWATER_LOG_DIR%\." exit /b 1
set "HOTWATER_LOG=%HOTWATER_LOG_DIR%\%HOTWATER_COMPONENT%.log"
set "HOTWATER_PYTHON=%~dp0..\.runtime\python313\python.exe"
call :run >> "%HOTWATER_LOG%" 2>&1
exit /b %ERRORLEVEL%

:run
echo [%DATE% %TIME%] Starting HotWater %HOTWATER_COMPONENT% on %COMPUTERNAME%. Log: "%HOTWATER_LOG%"
if /i "%HOTWATER_COMPONENT%"=="relay" (
    call "%~dp0..\run-relay.cmd"
) else (
    "%HOTWATER_PYTHON%" -B -u "%~dp0..\gauge.py"
)
set "HOTWATER_EXIT=%ERRORLEVEL%"
echo [%DATE% %TIME%] HotWater %HOTWATER_COMPONENT% exited with code %HOTWATER_EXIT%.
exit /b %HOTWATER_EXIT%
