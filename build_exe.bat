@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Собирает dist\KFC.exe — программа целиком в одном файле, Python на компьютере пользователя не нужен.
if not exist .venv\Scripts\python.exe (
  echo Создаю окружение для сборки...
  python -m venv .venv || goto :fail
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller==6.22.3 || goto :fail
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name KFC ^
  --icon "%~dp0kfc.ico" --version-file "%~dp0packaging\version_info.txt" ^
  --add-data "%~dp0web;web" --add-data "%~dp0config.toml;." --add-data "%~dp0kfc.ico;." ^
  --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" "%~dp0app.py" || goto :fail
echo.
echo Готово: dist\KFC.exe
exit /b 0
:fail
echo.
echo Сборка не удалась, подробности выше.
exit /b 1
