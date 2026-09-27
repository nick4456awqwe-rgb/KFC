@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Проверка программы (без интернета, около 30 секунд)...
echo.
python -m unittest discover -s tests -t tests -v
echo.
if "%1"=="speed" (
  echo Замер скорости на живом сайте МЭТС...
  python tests\bench_speed.py
)
pause
