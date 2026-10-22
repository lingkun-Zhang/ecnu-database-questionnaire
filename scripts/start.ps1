param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lifecycle.ps1')
$projectRoot = Get-ProjectRoot $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw '请先运行 Install.cmd 安装依赖。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) { throw '请先运行 Install.cmd 构建页面。' }
Assert-ProjectRuntime $projectRoot
$lease = Enter-ProjectLifecycle $projectRoot
Push-Location $projectRoot
try {
    & $projectPython (Join-Path $PSScriptRoot 'setup.py')
    if ($LASTEXITCODE -ne 0) { throw '项目配置检查失败。' }
    $existing = Get-ProjectBackend $projectRoot
    & (Join-Path $PSScriptRoot 'mongo.ps1') -InternalLockHeld
    $runtimeRoot = Join-Path $projectRoot '.runtime'
    if (-not $existing) {
        Assert-PlainProjectPath (Join-Path $runtimeRoot 'backend.log')
        Assert-PlainProjectPath (Join-Path $runtimeRoot 'backend-error.log')
        $serviceArguments = @('"' + (Join-Path $PSScriptRoot 'run.py') + '"')
        $serviceProcess = Start-Process -FilePath $projectPython -ArgumentList $serviceArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeRoot 'backend.log') -RedirectStandardError (Join-Path $runtimeRoot 'backend-error.log')
        Set-Content -LiteralPath (Join-Path $runtimeRoot 'backend.pid') -Value $serviceProcess.Id -Encoding ASCII
        $launcher = Get-ProjectProcess $serviceProcess.Id
        $timer = [Diagnostics.Stopwatch]::StartNew()
        do {
            $instance = Read-ProjectJson (Join-Path $runtimeRoot 'backend.instance.json')
            if ($instance -and [int]$instance.launcherPid -eq [int]$serviceProcess.Id) {
                $serverProcess = Get-ProjectProcess ([int]$instance.pid)
                if ($serverProcess) { Register-ProjectBackend $projectRoot $instance $serverProcess $launcher; $existing = Get-ProjectBackend $projectRoot; break }
            }
            if (-not (Get-ProjectProcess $serviceProcess.Id)) { throw '后端启动进程已退出，请检查.runtime/backend-error.log。' }
            Start-Sleep -Milliseconds 200
        } while ($timer.Elapsed.TotalSeconds -lt 20)
        if (-not $existing) { throw '无法确认新后端实例，未按端口猜测归属，请检查运行记录。' }
    }
    $ready = $false
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $verified = Get-ProjectBackend $projectRoot
        if (-not $verified) { throw '本项目后端已退出，请检查.runtime/backend-error.log。' }
        $listeners = @(Get-ProjectPortListeners 8000)
        if ($listeners.Count) {
            try { $ready = (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 2).status -eq 'ok' } catch { $ready = $false }
            if ($ready) { break }
        }
        Start-Sleep -Milliseconds 200
    } while ($timer.Elapsed.TotalSeconds -lt 20)
    if (-not $ready) { throw '服务未能就绪；已识别进程记录保留，可运行Stop.cmd正常停止后检查日志。' }
    Write-Output '问卷系统已启动：http://127.0.0.1:8000'
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8000' }
} finally { Pop-Location; Exit-ProjectLifecycle $lease }