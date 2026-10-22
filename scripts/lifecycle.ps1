$ErrorActionPreference = 'Stop'

function Get-ProjectRoot([string]$ScriptsDirectory) {
    return [IO.Path]::GetFullPath((Join-Path $ScriptsDirectory '..')).TrimEnd('\')
}
function Test-ProjectPath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) { return $false }
    try { return [string]::Equals([IO.Path]::GetFullPath($Left).TrimEnd('\'), [IO.Path]::GetFullPath($Right).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) } catch { return $false }
}
function Assert-PlainProjectPath([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "启停文件不能使用链接或联接目录：$Path" }
    }
}
function Assert-ProjectRuntime([string]$ProjectRoot) {
    $runtimePath = Join-Path $ProjectRoot '.runtime'
    Assert-PlainProjectPath $runtimePath
    if (-not (Test-Path -LiteralPath $runtimePath)) { New-Item -ItemType Directory -Path $runtimePath | Out-Null }
    Assert-PlainProjectPath (Join-Path $runtimePath 'mongo-data')
}
function Enter-ProjectLifecycle([string]$ProjectRoot) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $identity = ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\').ToLowerInvariant())))).Replace('-', '') } finally { $algorithm.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\WenchuanLifecycle_' + $identity))
    try {
        try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw '本项目正在进行另一次启动或停止，请等待完成后重试。' }
        return $mutex
    } catch { $mutex.Dispose(); throw }
}
function Exit-ProjectLifecycle($Lease) { if ($Lease) { try { $Lease.ReleaseMutex() } finally { $Lease.Dispose() } } }
function Read-ProjectJson([string]$Path) {
    Assert-PlainProjectPath $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    if ((Get-Item -LiteralPath $Path).Length -gt 16384) { throw '启停身份记录过大，拒绝使用。' }
    try {
        $jsonParameters = @{ErrorAction='Stop'}
        # PowerShell 7.5 can turn ISO identity stamps into DateTime values; keep
        # the exact wire string as PowerShell 5.1 does, including subsecond data.
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $jsonParameters.DateKind = 'String' }
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json @jsonParameters)
    } catch { throw '启停身份记录损坏，请保留现场检查，不会按错误记录停止进程。' }
}
function Write-ProjectJson([string]$Path, $Value) {
    Assert-PlainProjectPath (Split-Path -Parent $Path)
    Assert-PlainProjectPath $Path
    $temporaryPath = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporaryPath, ($Value | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporaryPath, $Path, [NullString]::Value) } else { [IO.File]::Move($temporaryPath, $Path) }
    } finally { if (Test-Path -LiteralPath $temporaryPath) { Remove-Item -LiteralPath $temporaryPath } }
}
function Get-ProjectProcess([int]$ProcessId) { if ($ProcessId -le 0) { return $null }; return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop }
function Get-ProcessStamp($Process) { if (-not $Process.CreationDate) { throw '无法读取进程创建时间，拒绝操作。' }; return ([datetime]$Process.CreationDate).ToUniversalTime().ToString('o') }
function Get-ProjectPortListeners([int]$Port) { return @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Port }) }

if (-not ('WenchuanLifecycleArguments' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WenchuanLifecycleArguments {
    [DllImport("shell32.dll", SetLastError=true)] static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string value, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr value);
    public static string[] Split(string value) {
        int count; IntPtr pointer=CommandLineToArgvW(value,out count);
        if(pointer==IntPtr.Zero) throw new InvalidOperationException("Cannot parse process arguments");
        try { var result=new string[count]; for(int i=0;i<count;i++) result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer,i*IntPtr.Size)); return result; }
        finally { LocalFree(pointer); }
    }
}
'@
}
function Get-ProjectArguments($Process) {
    if (-not $Process.ExecutablePath -or -not $Process.CommandLine) { throw '无法确认进程路径与命令行，拒绝操作。' }
    return [WenchuanLifecycleArguments]::Split([string]$Process.CommandLine)
}
function Assert-BackendCommand($Process, [string]$ProjectRoot, [string[]]$AllowedPython) {
    $arguments = @(Get-ProjectArguments $Process)
    $executableAllowed = @($AllowedPython | Where-Object { Test-ProjectPath $_ $Process.ExecutablePath }).Count -gt 0
    if (-not $executableAllowed -or $arguments.Count -ne 2 -or -not (Test-ProjectPath $arguments[0] $Process.ExecutablePath) -or -not (Test-ProjectPath $arguments[1] (Join-Path $ProjectRoot 'scripts\run.py'))) { throw '进程不是本项目的后端，拒绝复用或停止。' }
}
function Assert-BackendInstance($Instance, $Process, [string]$ProjectRoot) {
    if (-not $Instance -or $Instance.pid -is [bool] -or [int]$Instance.pid -le 0 -or [int]$Instance.pid -ne [int]$Process.ProcessId -or [string]$Instance.instanceToken -notmatch '^[A-Za-z0-9_-]{43}$' -or -not (Test-ProjectPath $Instance.projectRoot $ProjectRoot) -or -not (Test-ProjectPath $Instance.runPath (Join-Path $ProjectRoot 'scripts\run.py')) -or -not (Test-ProjectPath $Instance.pythonPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))) { throw '后端实例记录与本项目不匹配。' }
    Assert-BackendCommand $Process $ProjectRoot @($Instance.pythonPath, $Instance.basePythonPath)
    $launcher = Get-ProjectProcess ([int]$Instance.launcherPid)
    if (-not $launcher -or [int]$Process.ParentProcessId -ne [int]$launcher.ProcessId) { throw '后端启动进程关系已变化，拒绝操作。' }
    Assert-BackendCommand $launcher $ProjectRoot @((Join-Path $ProjectRoot '.venv\Scripts\python.exe'))
    return $launcher
}
function Register-ProjectBackend([string]$ProjectRoot, $Instance, $Process, $ExpectedLauncher) {
    $launcher = Assert-BackendInstance $Instance $Process $ProjectRoot
    if ([int]$launcher.ProcessId -ne [int]$ExpectedLauncher.ProcessId -or (Get-ProcessStamp $launcher) -ne (Get-ProcessStamp $ExpectedLauncher)) { throw '新后端不是本次启动的子进程。' }
    $identity = [ordered]@{pid=[int]$Process.ProcessId;creationDate=(Get-ProcessStamp $Process);executablePath=$Process.ExecutablePath;instanceToken=$Instance.instanceToken;launcherPid=[int]$launcher.ProcessId;launcherCreationDate=(Get-ProcessStamp $launcher);launcherExecutablePath=$launcher.ExecutablePath}
    Write-ProjectJson (Join-Path $ProjectRoot '.runtime\backend.process.json') $identity
}
function Get-ProjectBackend([string]$ProjectRoot) {
    $listeners = @(Get-ProjectPortListeners 8000)
    $instance = Read-ProjectJson (Join-Path $ProjectRoot '.runtime\backend.instance.json')
    $identity = Read-ProjectJson (Join-Path $ProjectRoot '.runtime\backend.process.json')
    if (-not $instance) {
        if ($listeners.Count) { throw '8000端口存在未识别的服务，不会复用或停止。' }
        if ($identity) { $existing = Get-ProjectProcess ([int]$identity.pid); if ($existing -and (Get-ProcessStamp $existing) -eq $identity.creationDate) { throw '后端仍在运行但缺少实例记录，不会猜测停止方式。' } }
        $legacyPath = Join-Path $ProjectRoot '.runtime\backend.pid'
        if (Test-Path -LiteralPath $legacyPath) { $legacyValue = (Get-Content -LiteralPath $legacyPath -Raw).Trim(); if ($legacyValue -match '^\d+$') { $legacyProcess = Get-ProjectProcess ([int]$legacyValue); if ($legacyProcess) { $legacyArguments = @(Get-ProjectArguments $legacyProcess); if ($legacyArguments.Count -eq 2 -and (Test-ProjectPath $legacyArguments[1] (Join-Path $ProjectRoot 'scripts\run.py'))) { throw '旧版启动进程仍在运行，请先完成旧服务迁移。' } } } }
        return $null
    }
    $process = Get-ProjectProcess ([int]$instance.pid)
    if (-not $process -or ($identity -and (Get-ProcessStamp $process) -ne $identity.creationDate)) { if ($listeners.Count) { throw '端口归属或旧进程身份已变化，拒绝操作。' }; return $null }
    $launcher = Assert-BackendInstance $instance $process $ProjectRoot
    if (-not $identity -or [int]$identity.pid -ne [int]$process.ProcessId -or $identity.instanceToken -cne $instance.instanceToken -or -not (Test-ProjectPath $identity.executablePath $process.ExecutablePath) -or [int]$identity.launcherPid -ne [int]$launcher.ProcessId -or $identity.launcherCreationDate -ne (Get-ProcessStamp $launcher) -or -not (Test-ProjectPath $identity.launcherExecutablePath $launcher.ExecutablePath)) { throw '后端身份记录不完整或已变化，拒绝操作。' }
    foreach ($listener in $listeners) { if ([int]$listener.OwningProcess -ne [int]$process.ProcessId -or $listener.LocalAddress -ne '127.0.0.1') { throw '8000端口不是本项目的回环服务。' } }
    return [pscustomobject]@{Instance=$instance;Process=$process;Identity=$identity}
}
function Get-MongoshPath([string]$Mongosh) {
    if (-not $Mongosh) { $command = Get-Command mongosh -ErrorAction Stop; $Mongosh = $command.Source }
    return (Get-Item -LiteralPath $Mongosh -ErrorAction Stop).FullName
}
function Invoke-ProjectMongoFile([string]$ProjectRoot, [string]$Mongosh, [int]$Port, [string]$Script) {
    $temporaryPath = Join-Path $ProjectRoot ('.runtime\mongo-control-' + [Guid]::NewGuid().ToString('N') + '.js')
    $savedPreference = $ErrorActionPreference
    try {
        Assert-ProjectRuntime $ProjectRoot
        [IO.File]::WriteAllText($temporaryPath, $Script, (New-Object Text.UTF8Encoding($false)))
        $ErrorActionPreference = 'Continue'
        $nativeOutput = & $Mongosh "mongodb://127.0.0.1:$Port/?directConnection=true&retryReads=false&retryWrites=false&serverSelectionTimeoutMS=2000&connectTimeoutMS=2000" --quiet --file $temporaryPath 2>&1
        $nativeExitCode = $LASTEXITCODE
        return [pscustomobject]@{ExitCode=$nativeExitCode;Output=($nativeOutput -join "`n")}
    } finally { $ErrorActionPreference = $savedPreference; if (Test-Path -LiteralPath $temporaryPath) { Remove-Item -LiteralPath $temporaryPath } }
}
function Get-ProjectMongo([string]$ProjectRoot, [int]$Port, [string]$Mongosh) {
    $listeners = @(Get-ProjectPortListeners $Port)
    $identity = Read-ProjectJson (Join-Path $ProjectRoot '.runtime\mongod.process.json')
    if (-not $listeners.Count) {
        if ($identity) { $existing=Get-ProjectProcess ([int]$identity.pid); if ($existing -and (Get-ProcessStamp $existing) -eq $identity.creationDate) { throw '本项目MongoDB进程仍在运行但没有预期监听端口，请检查日志。' } }
        return $null
    }
    $owners = @($listeners.OwningProcess | Select-Object -Unique)
    if ($owners.Count -ne 1 -or @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count) { throw '数据库端口不是单一回环服务，拒绝操作。' }
    $process = Get-ProjectProcess ([int]$owners[0])
    $arguments = @(Get-ProjectArguments $process)
    $expectedDataPath = Join-Path $ProjectRoot '.runtime\mongo-data'
    $allowedFlags = @('--dbpath','--port','--bind_ip','--replSet','--logpath','--logappend')
    $flags = @{}
    for ($index=1; $index -lt $arguments.Count; $index++) {
        $flag=$arguments[$index]
        if ($flag -notin $allowedFlags -or $flags.ContainsKey($flag)) { throw 'MongoDB启动参数不属于本项目管理格式。' }
        if ($flag -eq '--logappend') { $flags[$flag]=$true; continue }
        $index++; if ($index -ge $arguments.Count) { throw 'MongoDB启动参数不完整。' }; $flags[$flag]=$arguments[$index]
    }
    if ([IO.Path]::GetFileName($process.ExecutablePath) -ine 'mongod.exe' -or -not (Test-ProjectPath $arguments[0] $process.ExecutablePath) -or -not (Test-ProjectPath $flags['--dbpath'] $expectedDataPath) -or $flags['--port'] -ne [string]$Port -or $flags['--bind_ip'] -ne '127.0.0.1' -or $flags['--replSet'] -ne 'rs0' -or -not (Test-ProjectPath $flags['--logpath'] (Join-Path $ProjectRoot '.runtime\mongod.log'))) { throw '数据库进程不属于本项目数据目录、端口和副本集，拒绝操作。' }
    $probe = Invoke-ProjectMongoFile $ProjectRoot $Mongosh $Port 'const a=db.adminCommand({getCmdLineOpts:1}).parsed; print(JSON.stringify({pid:Number(db.serverStatus().pid),dbPath:a.storage.dbPath,port:Number(a.net.port),bindIp:a.net.bindIp,replicaSet:(a.replication.replSetName||a.replication.replSet)}));'
    if ($probe.ExitCode -ne 0) { throw '无法核实数据库实际参数，拒绝操作。' }
    try { $actual = ($probe.Output.Trim().Split("`n") | Select-Object -Last 1) | ConvertFrom-Json } catch { throw '数据库身份响应无效，拒绝操作。' }
    if ([int]$actual.pid -ne [int]$process.ProcessId -or -not (Test-ProjectPath $actual.dbPath $expectedDataPath) -or [int]$actual.port -ne $Port -or $actual.bindIp -ne '127.0.0.1' -or $actual.replicaSet -ne 'rs0') { throw '数据库实际参数与项目不匹配。' }
    if ($identity -and [int]$identity.pid -eq [int]$process.ProcessId -and $identity.creationDate -ne (Get-ProcessStamp $process)) { throw '数据库PID曾被重用，拒绝使用旧记录。' }
    return [pscustomobject]@{Pid=[int]$process.ProcessId;CreationDate=(Get-ProcessStamp $process);ExecutablePath=$process.ExecutablePath;DbPath=$expectedDataPath;Port=$Port;ReplicaSet='rs0'}
}
