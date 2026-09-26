@echo off
title Stock news alert
cd /d %~dp0
set PYTHONIOENCODING=utf-8
:loop
python stock_alert.py
if errorlevel 3 if not errorlevel 4 goto :eof
echo [%date% %time%] stock_alert.py exited, restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
