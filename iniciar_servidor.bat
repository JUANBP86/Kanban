@echo off
cd /d "%~dp0"
set "HOST=0.0.0.0"

echo Este servidor escuchara en la red local.
echo En otro PC conectado a la misma red, abre:
echo http://192.168.0.55:5000
echo Busca la direccion IPv4 de este PC:
ipconfig | findstr /I "IPv4"
echo.

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  python app.py
)

pause
