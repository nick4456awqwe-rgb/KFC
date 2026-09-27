@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python не найден. Установите Python 3.11 или новее с https://www.python.org/downloads/
  echo При установке отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
python -c "import requests, bs4, lxml, openpyxl" 2>nul || (
  echo Первый запуск: устанавливаю нужные библиотеки...
  python -m pip install -r requirements.txt || (pause & exit /b 1)
)
start "" pythonw app.py
