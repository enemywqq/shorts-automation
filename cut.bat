@echo off
cd /d "%~dp0"
py -3.13 -m app cut --ask "%~1"
pause
