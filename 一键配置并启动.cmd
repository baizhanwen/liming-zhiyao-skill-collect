@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "SKILL_COLLECT_BOOTSTRAP_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "SKILL_COLLECT_BUNDLE_MANIFEST=__BUNDLE_MANIFEST_SHA256__"
if "%SKILL_COLLECT_BUNDLE_MANIFEST:~0,2%"=="__" set "SKILL_COLLECT_BUNDLE_MANIFEST="
if not exist "%SKILL_COLLECT_BOOTSTRAP_POWERSHELL%" (
    echo Windows PowerShell is unavailable. No environment or data was changed.
    set "SKILL_COLLECT_BOOTSTRAP_RESULT=1"
    goto finish
)
if not exist "%~dp0scripts\windows_bootstrap.ps1" (
    echo Please fully extract the application package before starting it.
    set "SKILL_COLLECT_BOOTSTRAP_RESULT=1"
    goto finish
)
if defined SKILL_COLLECT_BUNDLE_MANIFEST (
    "%SKILL_COLLECT_BOOTSTRAP_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0scripts\windows_bootstrap.ps1" -BundleManifestSha256 "%SKILL_COLLECT_BUNDLE_MANIFEST%" %*
) else (
    "%SKILL_COLLECT_BOOTSTRAP_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0scripts\windows_bootstrap.ps1" %*
)
set "SKILL_COLLECT_BOOTSTRAP_RESULT=%ERRORLEVEL%"
:finish
if not "%SKILL_COLLECT_BOOTSTRAP_NO_PAUSE%"=="1" pause
exit /b %SKILL_COLLECT_BOOTSTRAP_RESULT%
