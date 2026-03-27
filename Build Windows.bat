@echo off
echo ========================================
echo   NovaStar Monitor — Windows Build
echo ========================================
echo.

cd src

echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Building executable...
pyinstaller novastar_monitor.spec --clean

echo.
echo ========================================
echo   Build complete!
echo   Output: src\dist\NovaStar Monitor\
echo ========================================
pause
