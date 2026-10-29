$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw '缺少 Node.js/npm，请从 https://nodejs.org/ 安装 Node.js 24 LTS。'
}
$NodeVersion = (& node --version).Trim()
if ($LASTEXITCODE -ne 0 -or $NodeVersion -notmatch '^v(\d+)\.(\d+)\.') { throw '无法读取 Node.js 版本。' }
$NodeMajor = [int]$Matches[1]
$NodeMinor = [int]$Matches[2]
if (-not (($NodeMajor -eq 20 -and $NodeMinor -ge 19) -or ($NodeMajor -eq 22 -and $NodeMinor -ge 12) -or $NodeMajor -ge 23)) {
    throw 'Node.js 版本不符合 Vite 8 要求，请从 https://nodejs.org/ 安装 Node.js 24 LTS。'
}
$PythonExe = $null
$Candidates = @((Join-Path $Root '.venv/Scripts/python.exe'), 'python3.12', 'python3.11', 'python3', 'python')
foreach ($Candidate in $Candidates) {
    if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
        & $Candidate -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = (& $Candidate -c 'import sys; print(sys.executable)').Trim()
            break
        }
    }
}
if (-not $PythonExe -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    $Found = & uv python find --no-python-downloads '>=3.11' 2>$null
    if ($LASTEXITCODE -eq 0) { $PythonExe = $Found.Trim() }
}
if (-not $PythonExe) { throw '需要 Python 3.11+，请从 https://www.python.org/downloads/ 安装；不会自动安装系统运行时。' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv sync --frozen --no-dev --no-python-downloads --python $PythonExe
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
} else {
    if (-not (Test-Path '.venv/Scripts/python.exe')) { & $PythonExe -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。' }
    & ./.venv/Scripts/python.exe -m pip install --requirement requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
}
& npm --prefix frontend ci --include=dev --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
& npm --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
& ./.venv/Scripts/python.exe -c 'from backend.store import Store; Store()'
if ($LASTEXITCODE -ne 0) { throw '数据库初始化失败。' }
& ./.venv/Scripts/python.exe scripts/install_browser.py
if ($LASTEXITCODE -ne 0) { throw '官方 Chromium 安装失败；请检查官方 Playwright 下载源网络后重跑 setup。' }
Write-Host '安装完成。运行 pwsh -File scripts/start.ps1；无需模型密钥即可使用工作台。'
