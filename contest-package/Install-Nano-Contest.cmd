@echo off
setlocal
set "NANO=jetson@10.244.173.220"
set "PACKAGE=%~dp0Nano-Contest.zip"
set "NETWORK_OPTION=--plc-network"
if /i "%~1"=="--app-only" set "NETWORK_OPTION="

if not exist "%PACKAGE%" (
    echo Put Nano-Contest.zip beside this installer and try again.
    pause
    exit /b 1
)

echo Connecting to %NANO%
scp -o ConnectTimeout=8 "%PACKAGE%" %NANO%:Nano-Contest.zip
if errorlevel 1 goto failed

ssh -t -o ConnectTimeout=8 %NANO% "python3 -m zipfile -e Nano-Contest.zip nano-contest-install && bash nano-contest-install/install.sh %NETWORK_OPTION%"
if errorlevel 1 goto failed

echo Installation completed. Open Lab Safety Demo on the Nano desktop.
if defined LAB_SAFETY_INSTALL_STATUS (
    >"%LAB_SAFETY_INSTALL_STATUS%" echo INSTALLED
    exit /b 0
)
pause
exit /b 0

:failed
echo Installation did not finish. Check the error above and the Nano hotspot IP.
if defined LAB_SAFETY_INSTALL_STATUS >"%LAB_SAFETY_INSTALL_STATUS%" echo FAILED
pause
exit /b 1
