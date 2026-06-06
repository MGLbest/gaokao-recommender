@echo off
chcp 65001 >nul
echo ============================================
echo   高考志愿推荐系统 - 启动(公网版)
echo ============================================
echo.

:: Kill any existing instances
taskkill /F /IM python.exe /FI "WINDOWTITLE eq GaokaoBackend*" >nul 2>&1
taskkill /F /IM ssh.exe >nul 2>&1

:: Start Flask backend
echo [1/2] 启动后端服务...
start "GaokaoBackend" /MIN python backend\app_v3.py
timeout /t 6 /nobreak >nul

:: Start SSH tunnel to serveo, capture output
echo [2/2] 启动公网隧道...
echo.
echo   系统已启动！复制下面的地址发给任何人即可访问：
echo ============================================
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ExitOnForwardFailure=yes -R 80:localhost:5000 serveo.net 2>&1 | findstr /C:"Forwarding" /C:"https://"
echo ============================================

:: If SSH drops, retry
:retry
echo 隧道断开，5秒后重连...
timeout /t 5 /nobreak >nul
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ExitOnForwardFailure=yes -R 80:localhost:5000 serveo.net 2>&1 | findstr /C:"Forwarding" /C:"https://"
goto retry
