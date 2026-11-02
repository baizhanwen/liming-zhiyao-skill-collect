param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [switch]$CheckOnly,
    [switch]$Json,
    [switch]$NoStart,
    [switch]$NoBrowser,
    [switch]$Offline,
    [switch]$ForcePrivateRuntimes,
    [switch]$SelectDataDirectory,
    [string]$BundleManifestSha256 = ''
)
# Runs on Windows PowerShell 5.1 and PowerShell 7. Only the current process and
# its children receive PATH/cache changes. No account, registry, global Python,
# global Node.js, browser profile, or application data is modified here.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$script:SCStage = 'preflight'
$script:SCErrorCode = 'preflight'
$script:SCErrorMessage = '环境准备未完成；原有配置和资料保留。'
$script:SCJson = [bool]$Json
$script:SCLog = $null
$script:SCLock = $null
$script:SCServer = $null
$script:SCRoot = $null
$script:SCPrivate = $null
$script:SCVersion = $null
$script:SCForcePrivate = [bool]$ForcePrivateRuntimes
$script:SCOffline = [bool]$Offline
$script:SCDownloadHosts = @('api.nuget.org', 'globalcdn.nuget.org', 'www.nuget.org',
    'nodejs.org', 'www.python.org', 'www.gyan.dev', 'github.com',
    'release-assets.githubusercontent.com', 'objects.githubusercontent.com')

function SC-Fail([string]$Code, [string]$Message) {
    $script:SCErrorCode = $Code
    $script:SCErrorMessage = $Message
    throw [InvalidOperationException]::new($Code)
}
function SC-Stage([string]$Name, [string]$Message) {
    $script:SCStage = $Name
    $script:SCErrorCode = $Name
    $script:SCErrorMessage = '当前环境配置步骤未完成；请检查网络和文件权限后重试，原有配置和资料保留。'
    if (-not $script:SCJson) { Write-Host ('[' + $Name + '] ' + $Message) }
    if ($script:SCLog) {
        [IO.File]::AppendAllText($script:SCLog, ([DateTime]::UtcNow.ToString('o') + ' ' + $Name + [Environment]::NewLine), [Text.Encoding]::UTF8)
    }
}
function SC-AssertLocalPath([string]$Path) {
    if (-not $Path -or $Path -notmatch '^[A-Za-z]:[\\/]' -or $Path.Substring(2).Contains(':')) {
        SC-Fail 'unsafe_path' '请将项目放在普通本地磁盘文件夹中；不支持网络路径。'
    }
    $Full = [IO.Path]::GetFullPath($Path).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
    if ($Full -match '^[A-Za-z]:$') { SC-Fail 'unsafe_path' '请选择项目文件夹，不能使用整个磁盘。' }
    $Walk = $Full
    while ($Walk) {
        try {
            if (([IO.File]::GetAttributes($Walk) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                SC-Fail 'unsafe_path' '项目或环境目录包含链接、联接或重解析点；请使用普通本地目录。'
            }
        } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
        $Next = [IO.Path]::GetDirectoryName($Walk)
        if ($Next -eq $Walk) { break }
        $Walk = $Next
    }
    return $Full
}
function SC-Child([string]$Root, [string]$Relative) {
    if (-not $Relative -or $Relative.StartsWith('/') -or $Relative -match '[\\:]' -or $Relative.Contains('//')) {
        SC-Fail 'unsafe_path' '安装清单含无效的相对路径。'
    }
    foreach ($Part in $Relative.Split('/')) {
        if (-not $Part -or $Part -in @('.', '..') -or $Part -match '[\x00-\x1f<>"|?*]' -or
                $Part.EndsWith('.') -or $Part.EndsWith(' ') -or $Part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            SC-Fail 'unsafe_path' '安装清单含无效的相对路径。'
        }
    }
    $Full = [IO.Path]::GetFullPath((Join-Path $Root $Relative.Replace('/', [IO.Path]::DirectorySeparatorChar)))
    if (-not $Full.StartsWith($Root.TrimEnd('\') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        SC-Fail 'unsafe_path' '安装文件超出项目专属目录。'
    }
    return SC-AssertLocalPath $Full
}
function SC-HashFile([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant() }
    finally { $Hasher.Dispose(); $Stream.Dispose() }
}
function SC-QuoteArgument([string]$Value) {
    if ($null -eq $Value) { $Value = '' }
    $Escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Escaped = [regex]::Replace($Escaped, '(\\+)$', '$1$1')
    return '"' + $Escaped + '"'
}
function SC-ProcessInfo([string]$File, [string[]]$Arguments) {
    $Info = [Diagnostics.ProcessStartInfo]::new()
    $Info.FileName = $File
    $Info.Arguments = (($Arguments | ForEach-Object { SC-QuoteArgument $_ }) -join ' ')
    $Info.WorkingDirectory = $script:SCRoot
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $Info.EnvironmentVariables['PYTHONNOUSERSITE'] = '1'
    $Info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    foreach ($Key in @($Info.EnvironmentVariables.Keys)) {
        if ($Key -match '^(?i:NODE_OPTIONS|NODE_PATH|NODE_V8_COVERAGE|NODE_COMPILE_CACHE|NODE_REDIRECT_WARNINGS|PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONINSPECT|NODE_EXTRA_CA_CERTS|SSLKEYLOGFILE|NODE_TLS_REJECT_UNAUTHORIZED|PLAYWRIGHT_DOWNLOAD_HOST|PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST|PLAYWRIGHT_FFMPEG_DOWNLOAD_HOST|NPM_TOKEN|NODE_AUTH_TOKEN)$' -or $Key -match '^(?i:PIP_|NPM_CONFIG_)') {
            $Info.EnvironmentVariables.Remove($Key)
        }
    }
    if ($script:SCPrivate) {
        $Info.EnvironmentVariables['PIP_CACHE_DIR'] = Join-Path $script:SCPrivate 'pip-cache'
        $Info.EnvironmentVariables['NPM_CONFIG_CACHE'] = Join-Path $script:SCPrivate 'npm-cache'
    }
    return $Info
}
function SC-StopOwnProcess([Diagnostics.Process]$Process) {
    try { $Running = $Process -and -not $Process.HasExited } catch { return }
    if ($Running) {
        try {
            $Killer = [Diagnostics.ProcessStartInfo]::new()
            $Killer.FileName = Join-Path $env:SystemRoot 'System32/taskkill.exe'
            $Killer.Arguments = '/PID ' + $Process.Id + ' /T /F'
            $Killer.UseShellExecute = $false
            $Killer.CreateNoWindow = $true
            $Killer.RedirectStandardOutput = $true
            $Killer.RedirectStandardError = $true
            $Child = [Diagnostics.Process]::Start($Killer)
            if ($Child) { $null = $Child.WaitForExit(10000); $Child.Dispose() }
        } catch { try { $Process.Kill() } catch { } }
    }
}
function SC-Run([string]$File, [string[]]$Arguments, [int]$TimeoutSeconds = 30) {
    $Info = SC-ProcessInfo $File $Arguments
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $Info
    try {
        $null = $Process.Start()
        $Out = $Process.StandardOutput.ReadToEndAsync()
        $Err = $Process.StandardError.ReadToEndAsync()
        $Timer = [Diagnostics.Stopwatch]::StartNew()
        $NextMessage = 15
        while (-not $Process.WaitForExit(500)) {
            if ($Timer.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
                SC-StopOwnProcess $Process
                SC-Fail 'process_timeout' '当前环境配置步骤等待超时；可重新双击继续，已有资料保留。'
            }
            if (-not $script:SCJson -and $Timer.Elapsed.TotalSeconds -ge $NextMessage) {
                Write-Host ('[' + $script:SCStage + '] 仍在运行，已等待 ' + [int]$Timer.Elapsed.TotalSeconds + ' 秒。')
                $NextMessage += 15
            }
        }
        if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($Out, $Err), 2000)) {
            SC-Fail 'process_output' '子进程未正常结束；请关闭本次安装窗口后重试。'
        }
        # Never print package-manager stdout/stderr: external config can contain
        # credential-bearing URLs. Only fixed stage/error codes reach the log.
        $Output = $Out.Result
        if ($Output.Length -gt 1048576) { $Output = $Output.Substring(0, 1048576) }
        return @{exit_code = $Process.ExitCode; output = $Output}
    } finally {
        SC-StopOwnProcess $Process
        $Process.Dispose()
    }
}
function SC-Probe([string]$File, [string[]]$Arguments, [string]$Pattern) {
    try {
        if (-not $File -or -not [IO.File]::Exists($File)) { return $null }
        $Result = SC-Run $File $Arguments 15
        if ($Result.exit_code -eq 0 -and $Result.output -match $Pattern) { return $Matches[1] }
    } catch { }
    return $null
}
function SC-SystemCommand([string]$Name) {
    $Found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Found) { return $Found.Source }
    return $null
}
function SC-NodeCompatible([string]$Version) {
    if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)$') { return $false }
    $Major = [int]$Matches[1]; $Minor = [int]$Matches[2]
    return (($Major -eq 20 -and $Minor -ge 19) -or ($Major -eq 22 -and $Minor -ge 12) -or $Major -ge 23)
}
function SC-PythonCompatible([string]$File) {
    return SC-Probe $File @('-c', 'import sys;print(".".join(map(str,sys.version_info[:3])));sys.exit(0 if sys.version_info >= (3,11) and sys.maxsize > 2**32 else 1)') '^([0-9]+\.[0-9]+\.[0-9]+)\s*$'
}
function SC-ReadLock {
    $Path = SC-Child $script:SCRoot 'scripts/windows-runtime-lock.json'
    if (-not [IO.File]::Exists($Path) -or (Get-Item -LiteralPath $Path).Length -gt 65536) {
        SC-Fail 'runtime_lock' '缺少完整的官方环境清单，请重新下载完整安装包。'
    }
    try { $Lock = [IO.File]::ReadAllText($Path) | ConvertFrom-Json }
    catch { SC-Fail 'runtime_lock' '环境清单无法读取，请重新下载完整安装包。' }
    if ($Lock.schema_version -ne 1 -or $Lock.platform -cne 'win-x64') { SC-Fail 'runtime_lock' '环境清单版本或架构不支持。' }
    foreach ($Name in @('python', 'node', 'ffmpeg')) {
        $Entry = $Lock.runtimes.$Name
        if ($Entry.kind -cne 'zip' -or $Entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Entry.version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
            SC-Fail 'runtime_lock' '环境清单中的版本或校验值无效。'
        }
        $Uri = [Uri]$Entry.url
        if ($Uri.Scheme -cne 'https' -or $Uri.UserInfo -or $Uri.Port -ne 443 -or $Uri.Host -notin $script:SCDownloadHosts) {
            SC-Fail 'runtime_lock' '环境清单没有使用允许的官方 HTTPS 下载来源。'
        }
        $null = SC-Child 'C:\validation-root' ([string]$Entry.executable)
        if ($Name -eq 'ffmpeg') { $null = SC-Child 'C:\validation-root' ([string]$Entry.probe) }
        if ($Entry.archive_prefix) { $null = SC-Child 'C:\validation-root' ([string]$Entry.archive_prefix).TrimEnd('/') }
        if ([long]$Entry.max_download_bytes -lt 1 -or [long]$Entry.max_download_bytes -gt 1073741824) {
            SC-Fail 'runtime_lock' '环境下载大小限制无效。'
        }
    }
    return $Lock
}
function SC-CheckBundle {
    $Manifest = SC-Child $script:SCRoot 'bundle-manifest.json'
    if (-not $BundleManifestSha256) {
        if ([IO.File]::Exists($Manifest)) { SC-Fail 'bundle_pin' '此分发包需要从自带的一键入口启动，以核对文件完整性。' }
        return
    }
    if ($BundleManifestSha256 -cnotmatch '^[0-9a-f]{64}$' -or -not [IO.File]::Exists($Manifest) -or
            (SC-HashFile $Manifest) -cne $BundleManifestSha256 -or (Get-Item -LiteralPath $Manifest).Length -gt 1048576) {
        SC-Fail 'bundle_hash' '安装包清单校验失败；没有配置环境，请重新下载完整安装包。'
    }
    try { $Data = [IO.File]::ReadAllText($Manifest) | ConvertFrom-Json }
    catch { SC-Fail 'bundle_manifest' '安装包清单无法读取。' }
    if ($Data.schema_version -ne 1 -or $Data.product -cne 'liming-zhiyao-skill-collect' -or $Data.runtimes_bundled -ne $false -or
            @($Data.files).Count -lt 10 -or @($Data.files).Count -gt 5000) { SC-Fail 'bundle_manifest' '安装包清单内容不符合本项目格式。' }
    $Seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($Item in $Data.files) {
        $Relative = [string]$Item.path
        if ($Relative -match '^(?i:data|uploads|tasks|backups|\.runtime|\.venv|\.git)(/|$)' -or
                $Relative -match '(^|/)(?i:secrets\.enc|\.secrets\.key|\.env)$') { SC-Fail 'bundle_private' '安装包清单错误地包含私人资料路径；没有读取或覆盖这些资料。' }
        $Path = SC-Child $script:SCRoot $Relative
        if (-not $Seen.Add($Relative) -or $Item.sha256 -cnotmatch '^[0-9a-f]{64}$' -or [long]$Item.size -lt 0 -or
                [long]$Item.size -gt 16777216 -or -not [IO.File]::Exists($Path) -or
                (Get-Item -LiteralPath $Path).Length -ne [long]$Item.size -or (SC-HashFile $Path) -cne $Item.sha256) {
            SC-Fail 'bundle_payload' '安装包文件缺失或校验失败；没有配置环境，请重新下载完整安装包。'
        }
    }
    foreach ($Required in @('scripts/windows_bootstrap.ps1', 'scripts/windows-runtime-lock.json', 'requirements.txt',
            'frontend/package-lock.json', 'frontend/dist/index.html', 'backend/main.py')) {
        if (-not $Seen.Contains($Required)) { SC-Fail 'bundle_manifest' '安装包清单没有覆盖必需程序文件。' }
    }
}
function SC-FindRuntimes($Lock) {
    $Found = @{}
    $Python = SC-Child $script:SCPrivate ('python/' + $Lock.runtimes.python.executable)
    $Version = SC-PythonCompatible $Python
    if ($Version) { $Found['python'] = @{path = $Python; version = $Version; source = 'project'} }
    if (-not $Found['python'] -and -not $script:SCForcePrivate) {
        $Candidates = @((SC-Child $script:SCRoot '.venv/Scripts/python.exe'))
        foreach ($Name in @('python3.12.exe', 'python3.13.exe', 'python3.11.exe', 'python.exe', 'python3.exe')) {
            $Candidate = SC-SystemCommand $Name
            if ($Candidate -and $Candidate -notmatch '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { $Candidates += $Candidate }
        }
        foreach ($Candidate in $Candidates) {
            $Version = SC-PythonCompatible $Candidate
            if ($Version) { $Found['python'] = @{path = $Candidate; version = $Version; source = 'system'}; break }
        }
    }
    $Node = SC-Child $script:SCPrivate ('node/' + $Lock.runtimes.node.executable)
    $NodeCandidates = @(@{path = $Node; source = 'project'})
    if (-not $script:SCForcePrivate) { $NodeCandidates += @{path = (SC-SystemCommand 'node.exe'); source = 'system'} }
    foreach ($Candidate in $NodeCandidates) {
        $Version = SC-Probe $Candidate.path @('--version') '^v([0-9]+\.[0-9]+\.[0-9]+)\s*$'
        if ($Version -and (SC-NodeCompatible $Version)) {
            $Npm = Join-Path ([IO.Path]::GetDirectoryName($Candidate.path)) 'node_modules/npm/bin/npm-cli.js'
            if ([IO.File]::Exists($Npm)) { $Found['node'] = @{path = $Candidate.path; version = $Version; source = $Candidate.source; npm = $Npm}; break }
        }
    }
    foreach ($Name in @('ffmpeg', 'ffprobe')) {
        $Relative = if ($Name -eq 'ffmpeg') { $Lock.runtimes.ffmpeg.executable } else { $Lock.runtimes.ffmpeg.probe }
        $Candidates = @(@{path = (SC-Child $script:SCPrivate ('ffmpeg/' + $Relative)); source = 'project'})
        if (-not $script:SCForcePrivate) { $Candidates += @{path = (SC-SystemCommand ($Name + '.exe')); source = 'system'} }
        foreach ($Candidate in $Candidates) {
            $Version = SC-Probe $Candidate.path @('-version') ('(?m)^' + $Name + ' version ([0-9A-Za-z_.+\-]+)')
            if ($Version) { $Found.$Name = @{path = $Candidate.path; version = $Version.Substring(0, [Math]::Min(64, $Version.Length)); source = $Candidate.source}; break }
        }
    }
    return $Found
}
function SC-EnvironmentChecks($Found) {
    $Checks = [System.Collections.Generic.List[object]]::new()
    foreach ($Name in @('python', 'node', 'ffmpeg', 'ffprobe')) {
        $Item = $Found[$Name]
        $Checks.Add(@{name = $Name; ok = [bool]$Item; source = $(if ($Item) { $Item.source } else { 'missing' }); version = $(if ($Item) { $Item.version } else { $null })})
    }
    $Venv = SC-Child $script:SCRoot '.venv/Scripts/python.exe'
    $Dependencies = SC-Probe $Venv @('-c', 'import fastapi,uvicorn,httpx,pydantic,multipart,cryptography;print("ready")') '^(ready)\s*$'
    $Checks.Add(@{name = 'python_dependencies'; ok = [bool]$Dependencies; source = $(if ($Dependencies) { 'project' } else { 'missing' })})
    $Frontend = [IO.File]::Exists((SC-Child $script:SCRoot 'frontend/dist/index.html'))
    $Checks.Add(@{name = 'frontend'; ok = $Frontend; source = $(if ($Frontend) { 'project' } else { 'missing' })})
    $Browser = $false
    if ($Found['node']) {
        $Runtime = SC-Child $script:SCRoot 'frontend/scripts/capture-douyin.mjs'
        try {
            $Reply = SC-Run $Found['node'].path @($Runtime, '--check-runtime') 20
            $Data = $Reply.output | ConvertFrom-Json
            $Browser = $Reply.exit_code -eq 0 -and $Data.ok -eq $true -and $Data.browser_installed -eq $true
        } catch { }
    }
    $Checks.Add(@{name = 'chromium'; ok = [bool]$Browser; source = $(if ($Browser) { 'project' } else { 'missing' })})
    return ,$Checks.ToArray()
}
function SC-Download($Entry, [string]$Target) {
    if ($script:SCOffline) { SC-Fail 'offline_missing' '离线模式下缺少必要环境；请联网后重新双击，或使用完整离线环境包。' }
    $null = SC-AssertLocalPath $Target
    if ([IO.File]::Exists($Target) -and (SC-HashFile $Target) -ceq $Entry.sha256) { return }
    $Part = $Target + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
    $Uri = [Uri]$Entry.url
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $NextProgress = 0
    $Response = $null; $Input = $null; $Output = $null
    try {
        for ($Redirect = 0; $Redirect -le 5; $Redirect++) {
            if ($Uri.Scheme -cne 'https' -or $Uri.Port -ne 443 -or $Uri.UserInfo -or $Uri.Host -notin $script:SCDownloadHosts) {
                SC-Fail 'download_origin' '下载跳转超出允许的官方 HTTPS 来源。'
            }
            $Request = [Net.HttpWebRequest]::Create($Uri)
            $Request.AllowAutoRedirect = $false
            $Request.Timeout = 30000
            $Request.ReadWriteTimeout = 30000
            $Request.UserAgent = 'SkillCollect-Windows-Bootstrap/1'
            $Response = $Request.GetResponse()
            $Status = [int]$Response.StatusCode
            if ($Status -ge 300 -and $Status -lt 400) {
                $Location = $Response.Headers['Location']
                $Response.Close(); $Response = $null
                if (-not $Location -or $Redirect -eq 5) { SC-Fail 'download_redirect' '官方环境下载跳转未完成。' }
                $Uri = [Uri]::new($Uri, $Location)
                continue
            }
            if ($Status -ne 200 -or $Response.ContentLength -gt [long]$Entry.max_download_bytes) {
                SC-Fail 'download_size' '官方环境下载响应或文件大小不符合清单。'
            }
            break
        }
        if (-not $Response) { SC-Fail 'download_response' '未取得官方环境下载响应。' }
        $Input = $Response.GetResponseStream()
        $Output = [IO.File]::Open($Part, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $Buffer = New-Object byte[] 65536
        $Total = [long]0
        while (($Count = $Input.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $Total += $Count
            if ($Total -gt [long]$Entry.max_download_bytes -or $Timer.Elapsed.TotalSeconds -gt 900) {
                SC-Fail 'download_budget' '环境下载超过大小或等待限制；可重新双击继续。'
            }
            $Output.Write($Buffer, 0, $Count)
            if (-not $script:SCJson -and $Timer.Elapsed.TotalSeconds -ge $NextProgress) {
                $Progress = '[' + $script:SCStage + '] 已下载 ' + [Math]::Round($Total / 1048576, 1) + ' MB'
                if ($Response.ContentLength -gt 0) { $Progress += ' / ' + [Math]::Round($Response.ContentLength / 1048576, 1) + ' MB' }
                Write-Host $Progress
                $NextProgress = $Timer.Elapsed.TotalSeconds + 2
            }
        }
        $Output.Dispose(); $Output = $null
        if ($Total -le 0 -or (SC-HashFile $Part) -cne $Entry.sha256) { SC-Fail 'download_hash' '环境文件 SHA-256 校验失败；没有运行该文件，请重试或重新下载官方安装包。' }
        if ([IO.File]::Exists($Target)) { [IO.File]::Delete($Target) }
        [IO.File]::Move($Part, $Target)
    } finally {
        if ($Output) { $Output.Dispose() }
        if ($Input) { $Input.Dispose() }
        if ($Response) { $Response.Close() }
        if ([IO.File]::Exists($Part)) { [IO.File]::Delete($Part) }
    }
}
function SC-ExpandZip([string]$Archive, [string]$Destination, [string]$Prefix = '') {
    $null = SC-AssertLocalPath $Destination
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Stream = [IO.File]::OpenRead($Archive)
    $Zip = [IO.Compression.ZipArchive]::new($Stream, [IO.Compression.ZipArchiveMode]::Read)
    $Seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $Planned = [System.Collections.Generic.List[object]]::new()
    $Total = [long]0
    try {
        if ($Zip.Entries.Count -gt 50000) { SC-Fail 'archive_size' '环境压缩包文件数超过限制。' }
        foreach ($Entry in $Zip.Entries) {
            $Name = $Entry.FullName
            if ($Prefix) {
                if (-not $Name.StartsWith($Prefix, [StringComparison]::Ordinal)) { SC-Fail 'archive_prefix' '环境压缩包目录与清单不一致。' }
                $Name = $Name.Substring($Prefix.Length)
                if (-not $Name) { continue }
            }
            $Directory = $Name.EndsWith('/')
            if ($Directory) { $Name = $Name.TrimEnd('/') }
            $Path = SC-Child $Destination $Name
            if (-not $Seen.Add($Path) -or (($Entry.ExternalAttributes -shr 16) -band 0xf000) -eq 0xa000 -or
                    ($Entry.ExternalAttributes -band 0x400) -ne 0) { SC-Fail 'archive_path' '环境压缩包包含重复、链接或重解析文件。' }
            $Total += $Entry.Length
            if ($Entry.Length -gt 1073741824 -or $Total -gt 2147483648) { SC-Fail 'archive_size' '环境解压大小超过限制。' }
            $Planned.Add(@{entry = $Entry; path = $Path; directory = $Directory})
        }
        # Validate every entry before making the first extracted file.
        $null = [IO.Directory]::CreateDirectory($Destination)
        foreach ($Item in $Planned) {
            if ($Item.directory) { $null = [IO.Directory]::CreateDirectory($Item.path); continue }
            $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Item.path))
            $null = SC-AssertLocalPath $Item.path
            $Input = $Item.entry.Open()
            $Output = [IO.File]::Open($Item.path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $Buffer = New-Object byte[] 65536
                $Written = [long]0
                while (($Count = $Input.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
                    $Written += $Count
                    if ($Written -gt $Item.entry.Length -or $Written -gt 1073741824) { SC-Fail 'archive_size' '环境实际解压字节超过已校验的文件长度。' }
                    $Output.Write($Buffer, 0, $Count)
                }
            } finally { $Output.Dispose(); $Input.Dispose() }
            if ((Get-Item -LiteralPath $Item.path).Length -ne $Item.entry.Length) { SC-Fail 'archive_size' '环境解压文件长度不符。' }
        }
    } finally { $Zip.Dispose(); $Stream.Dispose() }
}
function SC-InstallRuntime([string]$Name, $Entry) {
    SC-Stage ('runtime_' + $Name) ('准备项目专属 ' + $Name + '，不会修改系统全局环境。')
    $Downloads = SC-Child $script:SCPrivate 'downloads'
    $null = [IO.Directory]::CreateDirectory($Downloads)
    $Archive = SC-Child $Downloads ($Entry.sha256 + '.zip')
    SC-Download $Entry $Archive
    $Staging = SC-Child $script:SCPrivate ('stage-' + $Name + '-' + [Guid]::NewGuid().ToString('N'))
    $Target = SC-Child $script:SCPrivate $Name
    $Backup = $null
    try {
        SC-ExpandZip $Archive $Staging ([string]$Entry.archive_prefix)
        $Executable = SC-Child $Staging ([string]$Entry.executable)
        if (-not [IO.File]::Exists($Executable)) { SC-Fail 'runtime_executable' '环境压缩包没有清单指定的可执行文件。' }
        $Version = if ($Name -eq 'python') { SC-PythonCompatible $Executable } elseif ($Name -eq 'node') {
            SC-Probe $Executable @('--version') '^v([0-9]+\.[0-9]+\.[0-9]+)\s*$'
        } else { SC-Probe $Executable @('-version') '(?m)^ffmpeg version ([0-9]+\.[0-9]+\.[0-9]+)' }
        if ($Version -cne $Entry.version) { SC-Fail 'runtime_version' '解压环境实际版本与清单不一致。' }
        if ($Name -eq 'ffmpeg') {
            $Probe = SC-Child $Staging ([string]$Entry.probe)
            if ((SC-Probe $Probe @('-version') '(?m)^ffprobe version ([0-9]+\.[0-9]+\.[0-9]+)') -cne $Entry.version) {
                SC-Fail 'runtime_executable' '环境压缩包缺少有效的 FFprobe。'
            }
        }
        [IO.File]::WriteAllText((Join-Path $Staging '.skill-collect-managed.json'), (@{schema_version = 1; runtime = $Name; version = $Entry.version; sha256 = $Entry.sha256} | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
        if ([IO.Directory]::Exists($Target)) {
            if (-not [IO.File]::Exists((Join-Path $Target '.skill-collect-managed.json'))) {
                SC-Fail 'unmanaged_runtime' '项目专属环境目录已有未识别内容；没有覆盖，请保留目录并查看安装说明。'
            }
            $Backup = SC-Child $script:SCPrivate ('backup-' + $Name + '-' + [Guid]::NewGuid().ToString('N'))
            [IO.Directory]::Move($Target, $Backup)
        }
        try { [IO.Directory]::Move($Staging, $Target) }
        catch { if ($Backup -and -not [IO.Directory]::Exists($Target)) { [IO.Directory]::Move($Backup, $Target) }; throw }
    } finally {
        if ([IO.Directory]::Exists($Staging)) { [IO.Directory]::Delete($Staging, $true) }
        # Backups remain available; never recursively remove a previous runtime.
    }
}
function SC-UseRuntimes($Found) {
    $Parts = @()
    foreach ($Name in @('python', 'node', 'ffmpeg', 'ffprobe')) {
        if ($Found[$Name]) { $Parts += [IO.Path]::GetDirectoryName($Found[$Name].path) }
    }
    $env:PATH = (($Parts | Select-Object -Unique) -join ';') + ';' + $env:PATH
    $env:PYTHONUTF8 = '1'
    $env:PYTHONNOUSERSITE = '1'
}
function SC-RunRequired([string]$File, [string[]]$Arguments, [int]$TimeoutSeconds, [string]$Code, [string]$Message) {
    $Result = SC-Run $File $Arguments $TimeoutSeconds
    if ($Result.exit_code -ne 0) { SC-Fail $Code $Message }
}
function SC-SetUpProject($Found) {
    $VenvRoot = SC-Child $script:SCRoot '.venv'
    $Venv = SC-Child $script:SCRoot '.venv/Scripts/python.exe'
    if (-not (SC-PythonCompatible $Venv)) {
        $VenvMarker = SC-Child $script:SCRoot '.venv/.skill-collect-created.json'
        if ([IO.Directory]::Exists($VenvRoot)) {
            $Managed = $false
            if ([IO.File]::Exists($VenvMarker) -and (Get-Item -LiteralPath $VenvMarker).Length -le 4096) {
                try {
                    $Marker = [IO.File]::ReadAllText($VenvMarker) | ConvertFrom-Json
                    $Managed = $Marker.schema_version -eq 1 -and $Marker.product -ceq 'liming-zhiyao-skill-collect' -and $Marker.purpose -ceq 'bootstrap-created-venv'
                } catch { }
            }
            if (-not $Managed) { SC-Fail 'venv_invalid' '原项目虚拟环境损坏且不属于本安装器；没有覆盖。请保留原目录并按安装说明修复。' }
            if ($script:SCOffline) { SC-Fail 'offline_missing' '离线模式不修复未完成的项目 Python 环境；请联网重新双击。' }
            SC-Stage 'venv_recovery' '检测到上次未完成的受管 Python 环境，保留旧环境备份后重新创建。'
            $Backup = SC-Child $script:SCPrivate ('backup-venv-' + [Guid]::NewGuid().ToString('N'))
            [IO.Directory]::Move($VenvRoot, $Backup)
        }
        if ($script:SCOffline) { SC-Fail 'offline_missing' '离线模式下缺少项目 Python 环境；请联网重新双击。' }
        SC-Stage 'python_environment' '创建项目独立 Python 环境。'
        $null = [IO.Directory]::CreateDirectory($VenvRoot)
        [IO.File]::WriteAllText($VenvMarker, (@{schema_version = 1; product = 'liming-zhiyao-skill-collect'; purpose = 'bootstrap-created-venv'} | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
        SC-RunRequired $Found['python'].path @('-m', 'venv', $VenvRoot) 180 'venv_create' '创建项目 Python 环境失败；原有资料保留。'
    }
    $Requirements = SC-Child $script:SCRoot 'requirements.txt'
    $PackageLock = SC-Child $script:SCRoot 'frontend/package-lock.json'
    $StampFile = SC-Child $script:SCPrivate 'environment-ready.json'
    $Fingerprint = (SC-HashFile $Requirements) + ':' + (SC-HashFile $PackageLock)
    $Stamped = $false
    if ([IO.File]::Exists($StampFile)) {
        try { $Stamp = [IO.File]::ReadAllText($StampFile) | ConvertFrom-Json; $Stamped = $Stamp.fingerprint -ceq $Fingerprint }
        catch { }
    }
    $Checks = SC-EnvironmentChecks $Found
    $MissingPython = -not ($Checks | Where-Object { $_.name -eq 'python_dependencies' }).ok
    $MissingBrowser = -not ($Checks | Where-Object { $_.name -eq 'chromium' }).ok
    if (-not $Stamped -or $MissingPython) {
        if ($script:SCOffline) {
            if ($MissingPython) { SC-Fail 'offline_missing' '离线模式下缺少 Python 依赖；请联网重新双击。' }
        } else {
            SC-Stage 'python_packages' '安装项目锁定的 Python 依赖（首次需要联网）。'
            SC-RunRequired $Venv @('-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple', '--requirement', $Requirements) 900 'python_packages' 'Python 依赖安装未完成；请检查 PyPI 官方源网络后重试。'
        }
    }
    $NodeModules = SC-Child $script:SCRoot 'frontend/node_modules/@playwright/test/cli.js'
    if (-not $Stamped -or -not [IO.File]::Exists($NodeModules)) {
        if ($script:SCOffline) {
            if (-not [IO.File]::Exists($NodeModules)) { SC-Fail 'offline_missing' '离线模式下缺少前端和采集依赖；请联网重新双击。' }
        } else {
            SC-Stage 'node_packages' '安装项目锁定的前端和采集依赖（首次需要联网）。'
            $UserConfig = SC-Child $script:SCPrivate 'npm-user-empty.conf'
            $GlobalConfig = SC-Child $script:SCPrivate 'npm-global-empty.conf'
            [IO.File]::WriteAllText($UserConfig, '', [Text.Encoding]::UTF8)
            [IO.File]::WriteAllText($GlobalConfig, '', [Text.Encoding]::UTF8)
            SC-RunRequired $Found['node'].path @($Found['node'].npm, '--prefix', (SC-Child $script:SCRoot 'frontend'), 'ci', '--include=dev', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org/', ('--userconfig=' + $UserConfig), ('--globalconfig=' + $GlobalConfig)) 900 'node_packages' 'Node.js 依赖安装未完成；请检查 npm 官方源网络后重试。'
        }
    }
    # Bundles include compiled frontend assets. Source checkouts build them only
    # when absent; existing customer frontend/data are not blindly overwritten.
    if (-not [IO.File]::Exists((SC-Child $script:SCRoot 'frontend/dist/index.html'))) {
        SC-Stage 'frontend_build' '构建工作台页面。'
        SC-RunRequired $Found['node'].path @($Found['node'].npm, '--prefix', (SC-Child $script:SCRoot 'frontend'), 'run', 'build') 300 'frontend_build' '工作台构建失败；请核对完整项目文件后重试。'
    }
    if ($MissingBrowser) {
        if ($script:SCOffline) { SC-Fail 'offline_missing' '离线模式下缺少配套 Chromium；请联网重新双击。' }
        SC-Stage 'chromium' '下载或复用官方 Chromium；不读取个人浏览器登录。'
        SC-RunRequired $Venv @((SC-Child $script:SCRoot 'scripts/install_browser.py')) 1000 'chromium' 'Chromium 安装未完成；请检查官方 Playwright 下载源网络后重试。'
    }
    [IO.File]::WriteAllText($StampFile, (@{schema_version = 1; fingerprint = $Fingerprint} | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
    $LocalEnvironment = @{schema_version = 1; python = $Found['python'].path; virtualenv = $Venv; node = $Found['node'].path;
        ffmpeg = $Found['ffmpeg'].path; ffprobe = $Found['ffprobe'].path; browser_cache = $env:PLAYWRIGHT_BROWSERS_PATH;
        requirements_sha256 = (SC-HashFile $Requirements); package_lock_sha256 = (SC-HashFile $PackageLock)}
    [IO.File]::WriteAllText((SC-Child $script:SCPrivate 'environment.json'), ($LocalEnvironment | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
}
function SC-DataDirectory {
    if ($SelectDataDirectory) {
        if ($CheckOnly) { SC-Fail 'selection_check' '只读检查不会打开资料选择器；请从沿用旧资料入口启动。' }
        Add-Type -AssemblyName System.Windows.Forms
        $Dialog = [Windows.Forms.FolderBrowserDialog]::new()
        try {
            $Dialog.Description = '请选择原项目文件夹（内有data），或原资料目录（内有app.sqlite3）。不会复制或覆盖资料。'
            $Dialog.ShowNewFolderButton = $false
            if ($Dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { SC-Fail 'selection_cancelled' '已取消选择原资料；没有改变资料位置。' }
            $Selected = SC-AssertLocalPath $Dialog.SelectedPath
        } finally { $Dialog.Dispose() }
        $Database = Join-Path $Selected 'app.sqlite3'
        if (-not [IO.File]::Exists($Database)) {
            $Selected = SC-AssertLocalPath (Join-Path $Selected 'data')
            $Database = Join-Path $Selected 'app.sqlite3'
        }
        if (-not [IO.File]::Exists($Database)) { SC-Fail 'selection_database' '所选目录中没有原资料库app.sqlite3；没有创建空库或改变原资料。' }
        $null = SC-AssertLocalPath $Database
        return @{path = $Selected; selected = $true}
    }
    if ($env:SKILL_COLLECT_DATA) { return @{path = (SC-AssertLocalPath $env:SKILL_COLLECT_DATA); selected = $false} }
    $Pointer = SC-Child $script:SCPrivate 'data-location.json'
    if ([IO.File]::Exists($Pointer)) {
        try {
            if ((Get-Item -LiteralPath $Pointer).Length -gt 65536) { throw 'pointer' }
            $Data = [IO.File]::ReadAllText($Pointer) | ConvertFrom-Json
            if ($Data.schema_version -ne 1) { throw 'pointer' }
            $Selected = SC-AssertLocalPath ([string]$Data.path)
            if (-not [IO.File]::Exists((Join-Path $Selected 'app.sqlite3'))) { throw 'pointer' }
            return @{path = $Selected; selected = $false}
        } catch { SC-Fail 'data_pointer' '之前选择的原资料目录不可用；请使用沿用旧资料入口重新选择，不会建立替代空库。' }
    }
    return @{path = (SC-AssertLocalPath (Join-Path $script:SCRoot 'data')); selected = $false}
}
function SC-PortOpen([int]$Port) {
    $Client = [Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne(300)) { return $false }
        $Client.EndConnect($Async)
        return $true
    } catch { return $false } finally { $Client.Dispose() }
}
function SC-OwnedServer([int]$Port, [string]$DataDirectory) {
    try {
        $Request = [Net.HttpWebRequest]::Create(('http://127.0.0.1:' + $Port + '/api/status'))
        $Request.Proxy = $null
        $Request.Timeout = 3000
        $Request.ReadWriteTimeout = 3000
        $Request.AllowAutoRedirect = $false
        $Response = $Request.GetResponse()
        try {
            if ($Response.ContentLength -gt 1048576) { return $false }
            $Reader = [IO.StreamReader]::new($Response.GetResponseStream())
            try {
                $Text = [Text.StringBuilder]::new()
                $Buffer = New-Object char[] 4096
                while (($Count = $Reader.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
                    if ($Text.Length + $Count -gt 1048576) { return $false }
                    $null = $Text.Append($Buffer, 0, $Count)
                }
                $Status = $Text.ToString() | ConvertFrom-Json
            }
            finally { $Reader.Dispose() }
            if ($Status.mode -cne 'local_single_user' -or $Status.name -cne '利明智耀.skill clloect' -or $Status.version -cne $script:SCVersion) { return $false }
            $Expected = [IO.Path]::GetFullPath((Join-Path $DataDirectory 'app.sqlite3'))
            return [IO.Path]::GetFullPath([string]$Status.diagnostics.sqlite).Equals($Expected, [StringComparison]::OrdinalIgnoreCase)
        } finally { $Response.Close() }
    } catch { return $false }
}
function SC-OpenBrowser([int]$Port) {
    if (-not $NoBrowser) { Start-Process ('http://127.0.0.1:' + $Port + '/') | Out-Null }
}
function SC-StartApp([int]$Port, [string]$DataDirectory) {
    SC-Stage 'starting' '正在启动本机工作台；使用期间保持本窗口打开。'
    $env:SKILL_COLLECT_DATA = $DataDirectory
    $env:SKILL_COLLECT_PORT = [string]$Port
    $Venv = SC-Child $script:SCRoot '.venv/Scripts/python.exe'
    $Info = SC-ProcessInfo $Venv @('-u', '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', [string]$Port, '--no-access-log')
    $Info.CreateNoWindow = $false
    $script:SCServer = [Diagnostics.Process]::Start($Info)
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    while ($Timer.Elapsed.TotalSeconds -lt 45) {
        if ($script:SCServer.HasExited) { SC-Fail 'startup_exit' '后台启动失败；请查看本机日志，原资料保留。' }
        if (SC-OwnedServer $Port $DataDirectory) {
            SC-Stage 'running' ('工作台已启动：http://127.0.0.1:' + $Port + '/；关闭窗口将停止本次服务。')
            SC-OpenBrowser $Port
            $script:SCServer.WaitForExit()
            if ($script:SCServer.ExitCode -ne 0) { SC-Fail 'server_exit' '后台已退出；请按任务阶段或本机日志处理后重新双击。' }
            return
        }
        Start-Sleep -Milliseconds 500
    }
    SC-Fail 'startup_timeout' '后台未在等待时限内就绪；请检查本机日志后重试。'
}
function SC-Report([bool]$Ok, [object[]]$Checks, [string]$Mode) {
    $Result = @{schema_version = 1; product = 'liming-zhiyao-skill-collect'; mode = $Mode; ok = $Ok; stage = $script:SCStage; checks = @($Checks)}
    if (-not $Ok) { $Result.error = @{code = $script:SCErrorCode; message = $script:SCErrorMessage} }
    if ($script:SCJson) { [Console]::Out.WriteLine(($Result | ConvertTo-Json -Depth 8 -Compress)) }
    return $Result
}
function Invoke-SkillCollectBootstrap {
    $Checks = @()
    try {
        if ($env:OS -cne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess -or
                $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { SC-Fail 'unsupported_platform' '本安装器目前支持 Windows 10/11 x64；请使用对应架构的系统终端。' }
        if ($Json -and -not $CheckOnly -and -not $NoStart) { SC-Fail 'json_arguments' 'JSON 模式请同时使用 -CheckOnly 或 -NoStart。' }
        $script:SCRoot = SC-AssertLocalPath $ProjectPath
        foreach ($Required in @('pyproject.toml', 'requirements.txt', 'backend/main.py', 'frontend/package-lock.json', 'frontend/scripts/capture-douyin.mjs')) {
            if (-not [IO.File]::Exists((SC-Child $script:SCRoot $Required))) { SC-Fail 'project_incomplete' '项目文件不完整；请先完整解压安装包，保留原资料目录。' }
        }
        $MetadataFile = SC-Child $script:SCRoot 'pyproject.toml'
        if ((Get-Item -LiteralPath $MetadataFile).Length -gt 1048576) { SC-Fail 'project_identity' '项目标识不符合本产品；没有创建环境。' }
        $Metadata = [IO.File]::ReadAllText($MetadataFile)
        if ($Metadata -cnotmatch '(?m)^\[project\]\s*$' -or
                $Metadata -cnotmatch '(?m)^name\s*=\s*"liming-zhiyao-skill-collect"\s*$') {
            SC-Fail 'project_identity' '项目标识不符合本产品；没有创建环境。'
        }
        if ($Metadata -cmatch '(?m)^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$') { $script:SCVersion = $Matches[1] }
        else { SC-Fail 'project_identity' '项目版本标识无法读取；没有创建环境。' }
        $script:SCPrivate = SC-Child $script:SCRoot '.runtime'
        if (-not $env:PLAYWRIGHT_BROWSERS_PATH) { $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $script:SCPrivate 'ms-playwright' }
        SC-CheckBundle
        $Lock = SC-ReadLock
        $Found = SC-FindRuntimes $Lock
        $Checks = SC-EnvironmentChecks $Found
        if ($CheckOnly) {
            $script:SCStage = 'checked'
            $Ok = @($Checks | Where-Object { -not $_.ok }).Count -eq 0
            if (-not $Ok) { $script:SCErrorCode = 'environment_missing'; $script:SCErrorMessage = '检测到缺失环境；正常双击入口可联网自动准备，不需要安装 Codex。' }
            if (-not $Json) { foreach ($Item in $Checks) { Write-Host (([string]$Item.name) + ': ' + $(if ($Item.ok) { '通过' } else { '缺失' })) } }
            $null = SC-Report $Ok $Checks 'check'
            return $(if ($Ok) { 0 } else { 1 })
        }
        $Port = 8765
        if ($env:SKILL_COLLECT_PORT) {
            if (-not [int]::TryParse($env:SKILL_COLLECT_PORT, [ref]$Port) -or $Port -lt 1024 -or $Port -gt 65535) { SC-Fail 'port_invalid' 'SKILL_COLLECT_PORT 必须为1024—65535的整数。' }
        }
        $Data = SC-DataDirectory
        $DataDirectory = $Data.path
        if (SC-PortOpen $Port) {
            if (-not (SC-OwnedServer $Port $DataDirectory)) { SC-Fail 'port_in_use' '端口已被其他程序或另一个资料目录使用；没有结束任何进程。请关闭旧启动窗口后重试。' }
            if ($Data.selected) { SC-Fail 'selected_data_in_use' '所选原资料正在使用；请关闭旧启动窗口后重新选择，当前没有保存或更改资料位置。' }
            if ($NoStart -or @($Checks | Where-Object { -not $_.ok }).Count) { SC-Fail 'running_configuration' '工作台正在运行，本次没有配置或修复环境。请关闭旧启动窗口后重新双击。' }
            SC-Stage 'already_running' '同一资料目录的工作台已经运行，直接打开现有窗口；没有重复安装或启动。'
            if (-not $NoStart) { SC-OpenBrowser $Port }
            $null = SC-Report $true $Checks 'configure'
            return 0
        }
        $null = [IO.Directory]::CreateDirectory($script:SCPrivate)
        $null = SC-AssertLocalPath $script:SCPrivate
        try { $script:SCLock = [IO.File]::Open((SC-Child $script:SCPrivate 'bootstrap.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { SC-Fail 'installer_busy' '另一个环境配置窗口正在运行；请等待它结束，不要重复启动。' }
        $script:SCLog = SC-Child $script:SCPrivate 'bootstrap.log'
        if ($Data.selected) {
            [IO.File]::WriteAllText((SC-Child $script:SCPrivate 'data-location.json'), (@{schema_version = 1; path = $DataDirectory} | ConvertTo-Json -Compress), [Text.Encoding]::UTF8)
        }
        SC-Stage 'checking' '检测并准备缺少的环境；保留已有模型配置和资料。'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        foreach ($Name in @('python', 'node', 'ffmpeg')) {
            if (-not $Found[$Name] -or ($Name -eq 'ffmpeg' -and -not $Found['ffprobe'])) { SC-InstallRuntime $Name $Lock.runtimes.$Name }
        }
        $Found = SC-FindRuntimes $Lock
        foreach ($Name in @('python', 'node', 'ffmpeg', 'ffprobe')) { if (-not $Found[$Name]) { SC-Fail 'runtime_unavailable' '环境已准备但实际命令检查未通过；请重新获取完整安装包。' } }
        SC-UseRuntimes $Found
        SC-SetUpProject $Found
        $Checks = SC-EnvironmentChecks $Found
        if (@($Checks | Where-Object { -not $_.ok }).Count) { SC-Fail 'environment_incomplete' '环境准备结束但检查未全部通过；请按缺失项重试。' }
        SC-Stage 'ready' '环境检查通过；首次模型与飞书配置仍在本地设置中完成。'
        if ($NoStart) { $null = SC-Report $true $Checks 'configure'; return 0 }
        # Installation lock remains held by this launcher for its server's life,
        # preventing a second window from changing the running virtualenv.
        SC-StartApp $Port $DataDirectory
        return 0
    } catch {
        if (-not $Json) {
            Write-Host ('[' + $script:SCStage + '/' + $script:SCErrorCode + '] ' + $script:SCErrorMessage)
            Write-Host '可保留此窗口并重新双击重试。不要把密钥、Cookie或验证码发送给他人。'
        }
        $null = SC-Report $false $Checks $(if ($CheckOnly) { 'check' } else { 'configure' })
        return 1
    } finally {
        if ($script:SCServer) { SC-StopOwnProcess $script:SCServer; $script:SCServer.Dispose(); $script:SCServer = $null }
        if ($script:SCLock) { $script:SCLock.Dispose(); $script:SCLock = $null }
    }
}

# Dot-sourcing exposes only helpers for explicit contract tests; it never runs
# installation, download, server startup, or any browser action.
if ($MyInvocation.InvocationName -ne '.') {
    $Code = Invoke-SkillCollectBootstrap
    exit $Code
}
