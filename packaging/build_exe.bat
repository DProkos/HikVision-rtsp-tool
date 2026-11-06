@echo off
chcp 65001 >nul
REM Διπλό κλικ για να φτιαχτεί το HikRTSPViewer.exe (χρειάζεται Python στα Windows)
cd /d "%~dp0\.."

echo === Εγκατάσταση απαιτήσεων ===
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo === Δημιουργία exe ===
python packaging\build_exe.py %*
if errorlevel 1 goto :fail

echo.
echo Το exe βρίσκεται στο: packaging\dist\HikRTSPViewer.exe
explorer packaging\dist
pause
exit /b 0

:fail
echo.
echo Κάτι πήγε στραβά - δες τα μηνύματα παραπάνω.
pause
exit /b 1
