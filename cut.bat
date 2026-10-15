@echo off
cd /d "%~dp0"
py -3.13 -m app cut "%~1"
pause
