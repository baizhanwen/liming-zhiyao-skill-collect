$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$PythonExe = Join-Path $Root '.venv/Scripts/python.exe'
if (-not (Test-Path $PythonExe) -or -not (Test-Path 'frontend/dist/index.html')) {
    throw '请先运行 powershell -File scripts/setup.ps1 安装并构建。'
}
if (-not $env:SKILL_COLLECT_DATA) { $env:SKILL_COLLECT_DATA = Join-Path $Root 'data' }
if (-not $env:SKILL_COLLECT_PORT) { $env:SKILL_COLLECT_PORT = '8765' }
& $PythonExe scripts/doctor.py --check-port $env:SKILL_COLLECT_PORT
if ($LASTEXITCODE -ne 0) { throw '端口无效或已占用。请检查现有服务或修改 SKILL_COLLECT_PORT；不会结束其他进程。' }
$LogDir = Join-Path $env:SKILL_COLLECT_DATA 'logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir 'server.log'
Write-Host "工作台：http://127.0.0.1:$env:SKILL_COLLECT_PORT"
Write-Host "数据：$env:SKILL_COLLECT_DATA"
Write-Host "日志：$Log"
Write-Host '按 Ctrl+C 停止服务。'
& $PythonExe -u -m uvicorn backend.main:app --host 127.0.0.1 --port $env:SKILL_COLLECT_PORT --no-access-log 2>&1 | Tee-Object -FilePath $Log -Append
if ($LASTEXITCODE -ne 0) { throw "服务已退出，退出码 $LASTEXITCODE" }
