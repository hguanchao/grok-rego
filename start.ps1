# grok-rego Windows 启动脚本（UTF-8 BOM，供 Windows PowerShell 5.1 正确解析中文）
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

function Write-Ok([string]$Message) {
    Write-Host "[OK]   $Message"
}

function Write-Fail([string]$Message) {
    Write-Host "[FAIL] $Message"
}

function Add-DirToPath([string]$Dir) {
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return }
    $parts = $env:Path -split ";" | Where-Object { $_ }
    if ($parts -contains $Dir) { return }
    $env:Path = "$Dir;" + $env:Path
}

# 双击 start.bat 时不会加载用户 profile，~/.local/bin 里的 uv 不在 PATH
Add-DirToPath (Join-Path $env:USERPROFILE ".local\bin")
Add-DirToPath (Join-Path $env:USERPROFILE ".cargo\bin")

$fail = $false

Write-Host ""
Write-Host "========================================"
Write-Host "  grok-rego 环境检查"
Write-Host "========================================"
Write-Host ""

if (Test-Path -LiteralPath "server\pyproject.toml") {
    Write-Ok "后端目录 server\"
} else {
    Write-Fail "未找到 server\pyproject.toml"
    $fail = $true
}

if (Test-Path -LiteralPath "web\package.json") {
    Write-Ok "前端目录 web\"
} else {
    Write-Fail "未找到 web\package.json"
    $fail = $true
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    Write-Fail "未找到 uv，请安装: https://docs.astral.sh/uv/"
    $fail = $true
} else {
    Write-Ok ((& uv --version) | Out-String).Trim()
}

$node = Get-Command node -ErrorAction SilentlyContinue
if ($null -eq $node) {
    Write-Fail "未找到 Node.js >= 18，请安装: https://nodejs.org/"
    $fail = $true
} else {
    $nodeVer = ((& node -v) | Out-String).Trim().TrimStart("v")
    $nodeMajorText = ($nodeVer -split "\.")[0]
    $nodeMajor = 0
    if (-not [int]::TryParse($nodeMajorText, [ref]$nodeMajor)) {
        Write-Fail "无法解析 Node.js 版本"
        $fail = $true
    } elseif ($nodeMajor -lt 18) {
        Write-Fail "Node.js 需要 >= 18，当前 v$nodeVer"
        $fail = $true
    } else {
        Write-Ok "Node.js v$nodeVer"
    }
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($null -eq $npm) {
    Write-Fail "未找到 npm"
    $fail = $true
} else {
    $npmVer = ((& npm -v) | Out-String).Trim()
    Write-Ok "npm $npmVer"
}

if ($fail) {
    Write-Host ""
    Write-Host "环境检查未通过，已中止启动。"
    exit 1
}

Write-Host ""
Write-Host "----------------------------------------"
Write-Host "  准备依赖"
Write-Host "----------------------------------------"
Write-Host ""

if (-not (Test-Path -LiteralPath "server\config.json")) {
    if (Test-Path -LiteralPath "server\config.example.json") {
        Copy-Item -LiteralPath "server\config.example.json" -Destination "server\config.json" -Force
        Write-Ok "已从 config.example.json 生成 server\config.json"
    } else {
        Write-Fail "缺少 server\config.json 与 config.example.json"
        exit 1
    }
} else {
    Write-Ok "server\config.json"
}

Push-Location -LiteralPath "server"
try {
    & uv sync 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "后端 uv sync 失败"
        exit 1
    }
    & uv run python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Python 需要 >= 3.13"
        exit 1
    }
    $pyVer = (& uv run python -c "import sys; print(sys.version.split()[0])" 2>&1 | Out-String).Trim()
    Write-Ok "Python $pyVer"
} finally {
    Pop-Location
}
Write-Ok "后端依赖已就绪"

if (-not (Test-Path -LiteralPath "web\node_modules\vite")) {
    Write-Host "[..]   前端依赖缺失，执行 npm install"
    Push-Location -LiteralPath "web"
    try {
        & npm install 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install 失败"
            exit 1
        }
    } finally {
        Pop-Location
    }
}
Write-Ok "前端依赖已就绪"

Write-Host ""
Write-Host "----------------------------------------"
Write-Host "  启动服务"
Write-Host "----------------------------------------"
Write-Host ""

$serverDir = Join-Path $PSScriptRoot "server"
$webDir = Join-Path $PSScriptRoot "web"
$logDir = Join-Path $serverDir "logs"
$serverLog = Join-Path $logDir "server.log"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$script:serverProc = $null
$script:webProc = $null

function Test-ListenPort([string]$BindHost, [int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($BindHost, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(200, $false)
        if ($ok) {
            try { $client.EndConnect($iar) | Out-Null } catch { $client.Close(); return $false }
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {}
    return $false
}

function Wait-ListenPort([string]$BindHost, [int]$Port, [System.Diagnostics.Process]$Proc, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($null -ne $Proc -and $Proc.HasExited) { return $false }
        if (Test-ListenPort $BindHost $Port) { return $true }
        Start-Sleep -Milliseconds 150
    }
    return $false
}

function Stop-ProcessTree([System.Diagnostics.Process]$Proc) {
    if ($null -eq $Proc -or $Proc.HasExited) { return }
    & taskkill.exe /PID $Proc.Id /T /F 2>$null | Out-Null
}

function Stop-Services {
    Stop-ProcessTree $script:serverProc
    Stop-ProcessTree $script:webProc
}

$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    Stop-ProcessTree $script:serverProc
    Stop-ProcessTree $script:webProc
}

try {
    # 1) 先启动后端。业务日志由 loguru 写 server.log。
    # 不可把 stdout/stderr 再重定向到 server.log（Windows 文件锁会死锁）。
    $stdioDir = Join-Path $env:TEMP "grok-rego"
    if (-not (Test-Path -LiteralPath $stdioDir)) {
        New-Item -ItemType Directory -Path $stdioDir -Force | Out-Null
    }
    $script:serverProc = Start-Process -FilePath "uv" `
        -ArgumentList @("run", "python", "main.py", "--serve", "8787") `
        -WorkingDirectory $serverDir `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $stdioDir "stdout.log") `
        -RedirectStandardError (Join-Path $stdioDir "stderr.log")
    if (-not (Wait-ListenPort "127.0.0.1" 8787 $script:serverProc 30)) {
        Write-Fail "后端未在 30s 内就绪 (http://127.0.0.1:8787)，详见 server/logs/server.log"
        exit 1
    }
    Write-Ok "后端服务  http://127.0.0.1:8787  (pid $($script:serverProc.Id))"

    # 2) 再启动前端（输出直接打印到控制台，不生成 web 日志文件）
    $script:webProc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/c", "npm run --silent dev") `
        -WorkingDirectory $webDir `
        -NoNewWindow -PassThru
    if (-not (Wait-ListenPort "127.0.0.1" 5274 $script:webProc 30)) {
        Write-Fail "前端未在 30s 内就绪 (http://127.0.0.1:5274)，请查看上方控制台输出"
        exit 1
    }
    Write-Ok "前端服务  http://127.0.0.1:5274  (pid $($script:webProc.Id))"

    Write-Host ""
    Write-Host "前后端已在后台运行，日志见 server/logs/。Ctrl+C 同时停止。"
    Write-Host ""

    $waitIds = @($script:serverProc.Id)
    if ($null -ne $script:webProc) { $waitIds += $script:webProc.Id }
    Wait-Process -Id $waitIds
} finally {
    Stop-Services
}
exit 0
