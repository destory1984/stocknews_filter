@echo off
rem Stop the background stock_alert (restart loop included).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'stock_alert\.(bat|py)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo stopped.
