@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Запуск сервера тестирования...
start "" http://localhost:8080/teacher
python server.py --port 8080 --devices 20
pause
