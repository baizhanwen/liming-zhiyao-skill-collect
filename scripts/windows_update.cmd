@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
echo 请先关闭旧项目的启动窗口。将补丁完整解压后，选择原项目目录。
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0apply_windows_update.ps1" %*
set "SKILL_COLLECT_UPDATE_RESULT=%ERRORLEVEL%"
if not "%SKILL_COLLECT_UPDATE_NO_PAUSE%"=="1" pause
exit /b %SKILL_COLLECT_UPDATE_RESULT%
