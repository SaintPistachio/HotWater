@echo off
rem Start the iCOMM local relay (ha-icomm-local) in the foreground.
rem Press Ctrl+C to stop it.
setlocal
set "HW=%~dp0."
set "CERT_DIR=%HW%\certs"
set "DATA_DIR=%HW%\data"
set "QUEUE_FILE=%HW%\data\pending.json"
set "TRUSTED_PEERS=192.168.50.1"
set "LISTEN_PORT=8444"
set "STATE_PORT=8124"
set "STATE_HOST=127.0.0.1"
rem Standard write and readback verified on the EE12-50H55DVF through the local relay.
set "HOTWATER_STANDARD_WRITE_PAIR=0001"
set "HOTWATER_STANDARD_READ_PAIR=0001"
if not defined HOTWATER_PYTHON set "HOTWATER_PYTHON=%HW%\.venv\Scripts\python.exe"
cd /d "%HW%\relay-src\relay" || exit /b 1
"%HOTWATER_PYTHON%" -B -u icomm_hub.py
exit /b %ERRORLEVEL%
