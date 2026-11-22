@echo off
rem Start the hot water relay and the phone gauge, each in its own window.
set "HW=C:\HotWater"
start "Hot water relay" /d "%HW%" cmd /k "%HW%\run-relay.cmd"
start "Hot water gauge" /d "%HW%" cmd /k ""%HW%\.venv\Scripts\python.exe" gauge.py"
