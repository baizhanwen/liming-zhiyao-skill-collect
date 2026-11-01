@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
echo 请先关闭旧项目的启动窗口。将补丁完整解压后，选择原项目目录。
set "SKILL_COLLECT_UPDATE_PWSH="
for %%I in (pwsh.exe) do set "SKILL_COLLECT_UPDATE_PWSH=%%~$PATH:I"
if not defined SKILL_COLLECT_UPDATE_PWSH if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "SKILL_COLLECT_UPDATE_PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined SKILL_COLLECT_UPDATE_PWSH (
    echo 需要已安装的 PowerShell 7（pwsh）。本升级包不会自动安装或修改全局环境。
    echo 官方安装说明：https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows
    set "SKILL_COLLECT_UPDATE_RESULT=1"
    goto finish
)
"%SKILL_COLLECT_UPDATE_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0apply_windows_update.ps1" %*
set "SKILL_COLLECT_UPDATE_RESULT=%ERRORLEVEL%"
:finish
if not "%SKILL_COLLECT_UPDATE_NO_PAUSE%"=="1" pause
exit /b %SKILL_COLLECT_UPDATE_RESULT%
