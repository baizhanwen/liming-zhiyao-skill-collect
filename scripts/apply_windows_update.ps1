param(
    [string]$ProjectPath,
    [switch]$NoStart,
    [int]$Port = 0
)
# This template is pinned by build_windows_update.py. Do not run the template.
$ExpectedManifestSha256 = '__MANIFEST_SHA256__'
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$Product = 'liming-zhiyao-skill-collect'
$StaticFiles = @('backend/__init__.py', 'backend/browser_capture.py', 'backend/media.py',
    'backend/models.py', 'backend/task_progress.py', 'frontend/scripts/capture-douyin.mjs',
    'pyproject.toml', 'uv.lock', 'frontend/package.json', 'frontend/package-lock.json')
$FailureText = '升级校验未完成，原代码未改动。'
$Committed = [System.Collections.Generic.List[object]]::new()
$Lock = $null
$LockPath = $null
$Backup = $null
$Installed = $false
$DiagnosticStage = 'package_check'

function Stop-Update([string]$Message) { $script:FailureText = $Message; throw [InvalidOperationException]::new($Message) }
function Assert-LocalPath([string]$Path) {
    if ($Path -notmatch '^[A-Za-z]:[\\/]' -or $Path.Substring(2).Contains(':')) {
        Stop-Update '请选择普通本地磁盘上的原项目目录；不支持网络路径。'
    }
    $Full = [IO.Path]::GetFullPath($Path).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
    if ($Full -match '^[A-Za-z]:$') { Stop-Update '请选择项目文件夹，不能选择整个磁盘。' }
    $Walk = $Full
    while ($Walk) {
        try {
            $Attributes = [IO.File]::GetAttributes($Walk)
            if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Update '检测到符号链接、联接或重解析点；请使用普通本地目录。'
            }
        } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
        $Next = [IO.Path]::GetDirectoryName($Walk)
        if ($Next -eq $Walk) { break }
        $Walk = $Next
    }
    return $Full
}
function In-Root([string]$Root, [string]$Path) {
    return $Path.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}
function Allowed-Relative([string]$Path) {
    if (-not $Path -or $Path -match '[^\x20-\x7e]' -or $Path -match '[\\:]' -or $Path.StartsWith('/') -or $Path -match '(^|/)\.{1,2}(/|$)' -or $Path.Contains('//')) { return $false }
    foreach ($Part in $Path.Split('/')) { if ($Part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { return $false } }
    return ($StaticFiles -ccontains $Path) -or $Path -ceq 'frontend/dist/index.html' -or $Path -cmatch '^frontend/dist/assets/[A-Za-z0-9_-]+\.(js|css)$'
}
function Child-Path([string]$Root, [string]$Relative) {
    if (-not (Allowed-Relative $Relative)) { Stop-Update '补丁文件超出本次明确升级范围。' }
    $Full = [IO.Path]::GetFullPath((Join-Path $Root $Relative.Replace('/', [IO.Path]::DirectorySeparatorChar)))
    if (-not (In-Root $Root $Full)) { Stop-Update '补丁路径不在原项目内。' }
    return Assert-LocalPath $Full
}
function Hash-File([string]$Path) {
    $null = Assert-LocalPath $Path
    if (-not [IO.File]::Exists($Path)) { Stop-Update '补丁或项目文件缺失。' }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Assert-Keys($Value, [string[]]$Keys) {
    if ($null -eq $Value) { Stop-Update '补丁清单结构无效。' }
    $Actual = @($Value.PSObject.Properties.Name)
    if ($Actual.Count -ne $Keys.Count -or @($Actual | Where-Object { $Keys -cnotcontains $_ }).Count) {
        Stop-Update '补丁清单包含未知字段。'
    }
}
function Assert-PortFree([int]$Number) {
    if ($Number -lt 1024 -or $Number -gt 65535) { Stop-Update '本地端口设置无效。' }
    $Socket = [Net.Sockets.Socket]::new([Net.Sockets.AddressFamily]::InterNetwork, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
    try {
        $Socket.ExclusiveAddressUse = $true
        $Socket.Bind([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $Number))
    } catch { Stop-Update '端口正在使用。请先关闭旧项目启动窗口，再运行升级；不会结束任何进程。' }
    finally { $Socket.Dispose() }
}
function Invoke-PythonProbe([string]$Python, [string]$Script, [hashtable]$Values) {
    # Arguments contain no script or user path: PS5.1 and PS7 use the same stdin.
    $Info = [Diagnostics.ProcessStartInfo]::new()
    $Info.FileName = $Python
    $Info.Arguments = '-I -'
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.RedirectStandardInput = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    foreach ($Key in $Values.Keys) { $Info.EnvironmentVariables[$Key] = [string]$Values[$Key] }
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $Info
    try {
        $null = $Process.Start()
        $Output = $Process.StandardOutput.ReadToEndAsync()
        $Errors = $Process.StandardError.ReadToEndAsync()
        $Process.StandardInput.WriteLine($Script)
        $Process.StandardInput.Close()
        if (-not $Process.WaitForExit(15000)) {
            # Only this probe process is terminated; the existing server is untouched.
            $Process.Kill()
            $null = $Process.WaitForExit(5000)
            throw [TimeoutException]::new('Python probe timeout')
        }
        if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($Output, $Errors), 1000)) {
            throw [TimeoutException]::new('Python probe stream timeout')
        }
        return $Process.ExitCode
    } finally { $Process.Dispose() }
}
function Write-CodeFile([string]$Source, [string]$Destination) {
    $null = Assert-LocalPath $Source
    $null = Assert-LocalPath $Destination
    $Parent = [IO.Path]::GetDirectoryName($Destination)
    $null = [IO.Directory]::CreateDirectory($Parent)
    $null = Assert-LocalPath $Parent
    $Temporary = Join-Path $Parent ('.skill-update-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::Copy($Source, $Temporary, $false)
        $null = Assert-LocalPath $Destination
        if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($Temporary, $Destination, [NullString]::Value) }
        else { [IO.File]::Move($Temporary, $Destination) }
    } finally {
        if ([IO.File]::Exists($Temporary)) { [IO.File]::Delete($Temporary) }
    }
}

try {
    if ($env:OS -ne 'Windows_NT') { Stop-Update '本升级器仅适用于 Windows。' }
    $Package = Assert-LocalPath $PSScriptRoot
    $ManifestPath = Join-Path $Package 'manifest.json'
    $DiagnosticStage = 'manifest_hash'
    if ($ExpectedManifestSha256 -cnotmatch '^[0-9a-f]{64}$' -or (Hash-File $ManifestPath) -cne $ExpectedManifestSha256) {
        Stop-Update '补丁清单 SHA-256 校验失败，请重新获取完整升级包。'
    }
    if ((Get-Item -LiteralPath $ManifestPath).Length -gt 1048576) { Stop-Update '补丁清单超出大小限制。' }
    $DiagnosticStage = 'manifest_read'
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $DiagnosticStage = 'manifest_schema'
    Assert-Keys $Manifest @('schema_version', 'product', 'version', 'supported_versions', 'files')
    if ($Manifest.schema_version -ne 1 -or $Manifest.product -cne $Product -or $Manifest.version -cne '0.2.3' -or
        (@($Manifest.supported_versions) -join ',') -cne '0.2.2,0.2.3') { Stop-Update '此包不是本项目 0.2.3 专用升级包。' }
    $Entries = @($Manifest.files)
    if ($Entries.Count -lt 12 -or $Entries.Count -gt 128) { Stop-Update '补丁文件数量无效。' }
    $Payload = Assert-LocalPath (Join-Path $Package 'payload')
    $Seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $Directories = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $Total = [long]0
    $DiagnosticStage = 'payload_entries'
    foreach ($Entry in $Entries) {
        Assert-Keys $Entry @('path', 'size', 'sha256')
        if ($Entry.path -isnot [string] -or -not (Allowed-Relative $Entry.path) -or -not $Seen.Add($Entry.path) -or
            $Entry.sha256 -isnot [string] -or $Entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or [string]$Entry.size -notmatch '^\d+$') { Stop-Update '补丁路径、哈希或文件大小无效。' }
        $Size = [long]$Entry.size
        if ($Size -le 0 -or $Size -gt 10485760) { Stop-Update '补丁文件大小超限。' }
        $Total += $Size
        $Source = Child-Path $Payload $Entry.path
        if ((Get-Item -LiteralPath $Source).Length -ne $Size -or (Hash-File $Source) -cne $Entry.sha256) { Stop-Update '补丁文件 SHA-256 校验失败，原项目未更新。' }
        $Parts = $Entry.path.Split('/')
        for ($Index = 1; $Index -lt $Parts.Length; $Index++) { $null = $Directories.Add(($Parts[0..($Index - 1)] -join '/')) }
    }
    if ($Total -gt 33554432) { Stop-Update '补丁总大小超限。' }
    foreach ($Required in @($StaticFiles + 'frontend/dist/index.html')) { if (-not $Seen.Contains($Required)) { Stop-Update '补丁缺少必要代码或版本文件。' } }
    if (-not @($Entries | Where-Object { $_.path -cmatch '^frontend/dist/assets/[A-Za-z0-9_-]+\.js$' }).Count) { Stop-Update '补丁缺少已构建前端。' }
    $DiagnosticStage = 'payload_walk'
    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($Payload)
    $Checked = 0
    while ($Pending.Count) {
        foreach ($Item in @(Get-ChildItem -LiteralPath ($Pending.Pop()) -Force)) {
            $Checked++
            if ($Checked -gt 256) { Stop-Update '补丁目录条目超限。' }
            $null = Assert-LocalPath $Item.FullName
            $Relative = $Item.FullName.Substring($Payload.Length + 1).Replace('\', '/')
            if ($Item.PSIsContainer) {
                if (-not $Directories.Contains($Relative)) { Stop-Update '补丁包含未知目录。' }
                $Pending.Push($Item.FullName)
            } elseif (-not $Seen.Contains($Relative)) { Stop-Update '补丁包含清单之外的文件。' }
        }
    }
    $DiagnosticStage = 'project_check'
    if (-not $ProjectPath) {
        Add-Type -AssemblyName System.Windows.Forms
        $Picker = New-Object Windows.Forms.FolderBrowserDialog
        $Picker.Description = '选择已经安装 0.2.2 或 0.2.3 的原项目文件夹（里面有 backend、frontend、.venv）'
        $Picker.ShowNewFolderButton = $false
        if ($Picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { Write-Host '已取消，没有修改原项目。'; exit 0 }
        $ProjectPath = $Picker.SelectedPath
        $Picker.Dispose()
    }
    $Project = Assert-LocalPath $ProjectPath
    if (-not [IO.Directory]::Exists($Project) -or $Project.Equals($Package, [StringComparison]::OrdinalIgnoreCase) -or (In-Root $Project $Package) -or (In-Root $Package $Project)) {
        Stop-Update '请将补丁解压到原项目以外，再选择原项目目录。'
    }
    foreach ($Marker in @('pyproject.toml', 'backend/__init__.py', 'backend/main.py', 'frontend/package.json', '.venv/pyvenv.cfg', '.venv/Scripts/python.exe')) {
        $MarkerPath = Assert-LocalPath (Join-Path $Project $Marker)
        if (-not [IO.File]::Exists($MarkerPath)) { Stop-Update '所选目录不是已安装本项目的原目录，缺少必要文件或 .venv。' }
    }
    $ProjectToml = Get-Content -LiteralPath (Join-Path $Project 'pyproject.toml') -Raw -Encoding UTF8
    $Init = Get-Content -LiteralPath (Join-Path $Project 'backend/__init__.py') -Raw -Encoding UTF8
    $Frontend = Get-Content -LiteralPath (Join-Path $Project 'frontend/package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $NameMatch = [regex]::Match($ProjectToml, '(?m)^name\s*=\s*["'']([^"'']+)["'']\s*$')
    $VersionMatch = [regex]::Match($ProjectToml, '(?m)^version\s*=\s*["'']([^"'']+)["'']\s*$')
    $InitMatch = [regex]::Match($Init, '^\s*__version__\s*=\s*["'']([^"'']+)["'']\s*$')
    if (-not $NameMatch.Success -or $NameMatch.Groups[1].Value -cne $Product -or -not $VersionMatch.Success -or
        @('0.2.2', '0.2.3') -cnotcontains $VersionMatch.Groups[1].Value -or -not $InitMatch.Success -or
        $InitMatch.Groups[1].Value -cne $VersionMatch.Groups[1].Value -or $Frontend.name -cne ($Product + '-workbench') -or $Frontend.version -cne $VersionMatch.Groups[1].Value) {
        Stop-Update '此补丁仅支持版本一致的本项目 0.2.2 或 0.2.3；不会覆盖其他项目或版本。'
    }
    if ($Port -eq 0) { if ($env:SKILL_COLLECT_PORT) { $Port = [int]$env:SKILL_COLLECT_PORT } else { $Port = 8765 } }
    Assert-PortFree $Port
    $Python = Assert-LocalPath (Join-Path $Project '.venv/Scripts/python.exe')
    $DiagnosticStage = 'runtime_probe'
    $Check = 'import sys,importlib.util;sys.exit(0 if sys.version_info>=(3,11) and all(importlib.util.find_spec(x) is not None for x in ("fastapi","uvicorn","httpx","pydantic","multipart","cryptography")) else 1)'
    if ((Invoke-PythonProbe $Python $Check @{}) -ne 0) { Stop-Update '原项目 .venv 的 Python 或依赖不完整；本升级包不会安装或修改全局环境。' }
    $DiagnosticStage = 'identity_probe'
    $Identity = 'import os,sys,tomllib;from pathlib import Path;p=tomllib.loads(Path(os.environ["SKILL_COLLECT_UPDATE_TOML"]).read_text(encoding="utf-8"))["project"];sys.exit(0 if p.get("name")=="liming-zhiyao-skill-collect" and p.get("version")==os.environ["SKILL_COLLECT_UPDATE_VERSION"] else 1)'
    $ProbeValues = @{ SKILL_COLLECT_UPDATE_TOML = (Join-Path $Project 'pyproject.toml'); SKILL_COLLECT_UPDATE_VERSION = $VersionMatch.Groups[1].Value }
    if ((Invoke-PythonProbe $Python $Identity $ProbeValues) -ne 0) { Stop-Update '原项目 TOML 标识或版本不匹配；未修改代码。' }
    foreach ($Entry in $Entries) {
        $Destination = Child-Path $Project $Entry.path
        if ([IO.Directory]::Exists($Destination)) { Stop-Update '原代码目标被同名目录占用。' }
    }
    $DiagnosticStage = 'backup'
    $BackupRoot = Assert-LocalPath (Join-Path $Project '.skill-collect-update-backups')
    $null = [IO.Directory]::CreateDirectory($BackupRoot)
    $LockPath = Join-Path $BackupRoot 'update.lock'
    $null = Assert-LocalPath $LockPath
    try { $Lock = [IO.FileStream]::new($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose) }
    catch { Stop-Update '另一个升级程序正在使用该目录，请等待完成。' }
    $Backup = Join-Path $BackupRoot ('0.2.3-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N'))
    $null = [IO.Directory]::CreateDirectory($Backup)
    $Originals = [System.Collections.Generic.List[object]]::new()
    foreach ($Entry in $Entries) {
        $Destination = Child-Path $Project $Entry.path
        $Exists = [IO.File]::Exists($Destination)
        $Original = @{ path = $Entry.path; existed = $Exists; sha256 = $null }
        if ($Exists) {
            $Original.sha256 = Hash-File $Destination
            $Saved = Child-Path $Backup $Entry.path
            $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Saved))
            [IO.File]::Copy($Destination, $Saved, $false)
            if ((Hash-File $Saved) -cne $Original.sha256) { Stop-Update '原代码备份校验失败，未执行升级。' }
        }
        $Originals.Add($Original)
    }
    @{ product = $Product; source_version = $VersionMatch.Groups[1].Value; files = @($Originals.ToArray()) } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Backup 'backup.json') -Encoding UTF8
    Assert-PortFree $Port
    $DiagnosticStage = 'code_write'
    $FailureText = '代码写入未完成，正在恢复旧代码。'
    foreach ($Entry in $Entries) {
        $Source = Child-Path $Payload $Entry.path
        if ((Hash-File $Source) -cne $Entry.sha256) { Stop-Update '补丁在复制前发生变化，正在恢复旧代码。' }
        $Destination = Child-Path $Project $Entry.path
        # Include the attempted file even if replacement or its verification fails.
        $Committed.Add(($Originals | Where-Object { $_.path -ceq $Entry.path } | Select-Object -First 1))
        Write-CodeFile $Source $Destination
        if ((Hash-File $Destination) -cne $Entry.sha256) { Stop-Update '复制后哈希校验失败，正在恢复旧代码。' }
    }
    $Installed = $true
    Write-Host '代码升级至 0.2.3 完成。现有资料、模型和飞书配置保持原目录。'
    Write-Host '原代码备份保存在所选项目的 .skill-collect-update-backups 文件夹。'
} catch {
    $ExceptionType = $_.Exception.GetType().Name
    if ($_.Exception.InnerException) { $ExceptionType = $_.Exception.InnerException.GetType().Name }
    if ($ExceptionType -cnotmatch '^[A-Za-z][A-Za-z0-9]{0,63}$') { $ExceptionType = 'UnexpectedError' }
    $RollbackOk = $true
    if ($Backup -and $Committed.Count) {
        for ($Index = $Committed.Count - 1; $Index -ge 0; $Index--) {
            try {
                $Original = $Committed[$Index]
                $Destination = Child-Path $Project $Original.path
                if ($Original.existed) {
                    $Saved = Child-Path $Backup $Original.path
                    if ((Hash-File $Saved) -cne $Original.sha256) { throw 'backup integrity' }
                    Write-CodeFile $Saved $Destination
                    if ((Hash-File $Destination) -cne $Original.sha256) { throw 'rollback integrity' }
                } elseif ([IO.File]::Exists($Destination)) { [IO.File]::Delete($Destination) }
            } catch { $RollbackOk = $false }
        }
    }
    Write-Host $FailureText -ForegroundColor Red
    # Fixed stage, validated type name and integer source line only; never raw error.
    Write-Host ('诊断代码：{0}/{1}' -f $DiagnosticStage, $ExceptionType)
    Write-Host ('诊断行号：{0}' -f [int]$_.InvocationInfo.ScriptLineNumber)
    if ($Committed.Count) { if ($RollbackOk) { Write-Host '旧代码已恢复；未启动服务。' } else { Write-Host '恢复未完全确认；请保留项目内 .skill-collect-update-backups 备份并联系维护助手。' } }
    exit 1
} finally {
    if ($Lock) { $Lock.Dispose() }
}
if ($Installed -and -not $NoStart) {
    try {
        Assert-PortFree $Port
        Set-Location -LiteralPath $Project
        $env:PYTHONUTF8 = '1'
        Write-Host "工作台：http://127.0.0.1:$Port；此窗口保持打开，Ctrl+C 停止服务。"
        & $Python -u -m uvicorn backend.main:app --host 127.0.0.1 --port $Port --no-access-log
        if ($LASTEXITCODE -ne 0) { Write-Host '代码已更新，但服务启动/运行报错；原代码备份已保留，未结束其他进程。'; exit 1 }
    } catch { Write-Host '代码已更新，启动未完成；请保留原代码备份并检查现有启动窗口。'; exit 1 }
} elseif ($Installed) { Write-Host '按 NoStart 要求未启动服务。' }
