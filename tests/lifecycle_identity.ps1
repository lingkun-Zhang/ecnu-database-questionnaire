param(
    [Parameter(Mandatory = $true)][string]$HelperPath,
    [string]$FixtureRoot = '',
    [ValidateSet('Suite', 'Parse', 'LockProbe')][string]$Mode = 'Suite'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$script:HarnessPath = $PSCommandPath

if ($Mode -eq 'Parse') {
    $scriptPaths = @((Get-ChildItem -LiteralPath (Split-Path -Parent $HelperPath) -Filter '*.ps1' -File).FullName) + @($script:HarnessPath)
    foreach ($scriptPath in $scriptPaths) {
        $parseTokens = $null
        $parseErrors = $null
        $null = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$parseTokens, [ref]$parseErrors)
        if ($parseErrors.Count -ne 0) { throw ('AST parse failed: ' + [IO.Path]::GetFileName($scriptPath)) }
    }
    Write-Output ('LIFECYCLE_AST_OK ' + $scriptPaths.Count)
    exit 0
}

. $HelperPath
if ($Mode -eq 'LockProbe') {
    $lease = $null
    try {
        try { $lease = Enter-ProjectLifecycle $FixtureRoot } catch { Write-Output 'LOCK_BUSY'; exit 17 }
        Write-Output 'LOCK_ACQUIRED'
        exit 0
    } finally { Exit-ProjectLifecycle $lease }
}

if (-not $FixtureRoot -or -not (Test-Path -LiteralPath $FixtureRoot -PathType Container)) { throw 'A disposable fixture directory is required.' }
$script:FixtureRoot = [IO.Path]::GetFullPath($FixtureRoot)
$script:Runtime = Join-Path $script:FixtureRoot '.runtime'
New-Item -ItemType Directory -Path $script:Runtime -Force | Out-Null
$script:ScenarioCount = 0
$script:AssertionCount = 0
$script:FailedScenarios = New-Object 'Collections.Generic.List[string]'
$script:ProcessMap = @{}
$script:ListenerMap = @{}
$script:ProbeCalls = 0
$script:ProbeResult = $null
$script:BaseDate = [datetime]'2026-10-20T00:00:00Z'
$script:ServerPid = 61102
$script:LauncherPid = 61101
$script:MongoPid = 61103
$script:ForeignPid = 61104
$script:VenvPython = Join-Path $script:FixtureRoot '.venv\Scripts\python.exe'
$script:BasePython = Join-Path $script:FixtureRoot '工具 运行时\python.exe'
$script:RunPath = Join-Path $script:FixtureRoot 'scripts\run.py'
$script:MongoPath = Join-Path $script:FixtureRoot '工具 运行时\mongod.exe'
$script:DataPath = Join-Path $script:Runtime 'mongo-data'
$script:MongoLog = Join-Path $script:Runtime 'mongod.log'

# Only operating-system observations are replaced. Every identity, path, argument,
# JSON, and mutex decision below executes the project's real lifecycle helper.
function Get-ProjectProcess([int]$ProcessId) {
    if ($script:ProcessMap.ContainsKey($ProcessId)) { return $script:ProcessMap[$ProcessId] }
    return $null
}
function Get-ProjectPortListeners([int]$Port) {
    if ($script:ListenerMap.ContainsKey($Port)) { return @($script:ListenerMap[$Port]) }
    return @()
}
function Invoke-ProjectMongoFile([string]$ProjectRoot, [string]$Mongosh, [int]$Port, [string]$Script) {
    if ($Script -match '(?i)shutdown|dropDatabase|deleteMany|remove\s*\(') { throw 'The identity harness permits read-only probes only.' }
    $script:ProbeCalls++
    return $script:ProbeResult
}
function Check([bool]$Condition, [string]$Message) {
    $script:AssertionCount++
    if (-not $Condition) { throw $Message }
}
function Reject([scriptblock]$Action) {
    $rejected = $false
    try { $null = & $Action } catch { $rejected = $true }
    Check $rejected 'Expected an identity refusal.'
}
function Scenario([string]$Name, [scriptblock]$Action) {
    $script:ScenarioCount++
    try { & $Action; Write-Output ('OK ' + $Name) } catch {
        $script:FailedScenarios.Add($Name)
        Write-Output ('FAIL ' + $Name + ': ' + $_.Exception.Message)
    }
}
function Quote([string]$Value) { return ('"' + $Value + '"') }
function Reset-Fixture {
    $script:ProcessMap = @{}
    $script:ListenerMap = @{}
    $script:ProbeCalls = 0
    $script:ProbeResult = $null
    foreach ($name in @('backend.instance.json', 'backend.process.json', 'backend.pid', 'backend.stop.json', 'mongod.process.json')) {
        $path = Join-Path $script:Runtime $name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
}
function Assert-NoStopSignal {
    Check (-not (Test-Path -LiteralPath (Join-Path $script:Runtime 'backend.stop.json'))) 'A read-only identity check wrote a stop request.'
}
function Backend-Fixture {
    Reset-Fixture
    $script:Launcher = [pscustomobject]@{
        ProcessId = $script:LauncherPid; ParentProcessId = 61000; ExecutablePath = $script:VenvPython
        CommandLine = ((Quote $script:VenvPython) + ' ' + (Quote $script:RunPath)); CreationDate = $script:BaseDate
    }
    $script:Server = [pscustomobject]@{
        ProcessId = $script:ServerPid; ParentProcessId = $script:LauncherPid; ExecutablePath = $script:BasePython
        CommandLine = ((Quote $script:BasePython) + ' ' + (Quote $script:RunPath)); CreationDate = $script:BaseDate.AddSeconds(2)
    }
    $script:Instance = [pscustomobject]@{
        pid = $script:ServerPid; launcherPid = $script:LauncherPid; instanceToken = ('a' * 43)
        projectRoot = $script:FixtureRoot; runPath = $script:RunPath
        pythonPath = $script:VenvPython; basePythonPath = $script:BasePython; startedAt = '2026-10-20T00:00:01Z'
    }
    $script:ProcessMap[$script:LauncherPid] = $script:Launcher
    $script:ProcessMap[$script:ServerPid] = $script:Server
    $script:ListenerMap[8000] = @([pscustomobject]@{LocalPort = 8000; LocalAddress = '127.0.0.1'; OwningProcess = $script:ServerPid})
    Write-ProjectJson (Join-Path $script:Runtime 'backend.instance.json') $script:Instance
    Register-ProjectBackend $script:FixtureRoot $script:Instance $script:Server $script:Launcher
    $script:Identity = Read-ProjectJson (Join-Path $script:Runtime 'backend.process.json')
}
function Write-BackendIdentity { Write-ProjectJson (Join-Path $script:Runtime 'backend.process.json') $script:Identity }
function Mongo-Fixture {
    Reset-Fixture
    $script:Mongo = [pscustomobject]@{
        ProcessId = $script:MongoPid; ParentProcessId = 61000; ExecutablePath = $script:MongoPath
        CommandLine = ((Quote $script:MongoPath) + ' --dbpath ' + (Quote $script:DataPath) + ' --port 27117 --bind_ip 127.0.0.1 --replSet rs0 --logpath ' + (Quote $script:MongoLog) + ' --logappend')
        CreationDate = $script:BaseDate
    }
    $script:ProcessMap[$script:MongoPid] = $script:Mongo
    $script:ListenerMap[27117] = @([pscustomobject]@{LocalPort = 27117; LocalAddress = '127.0.0.1'; OwningProcess = $script:MongoPid})
    $script:ActualMongo = [pscustomobject]@{pid = $script:MongoPid; dbPath = $script:DataPath; port = 27117; bindIp = '127.0.0.1'; replicaSet = 'rs0'}
    Write-ProjectJson (Join-Path $script:Runtime 'mongod.process.json') ([ordered]@{pid = $script:MongoPid; creationDate = (Get-ProcessStamp $script:Mongo)})
    Update-Probe
}
function Update-Probe { $script:ProbeResult = [pscustomobject]@{ExitCode = 0; Output = ($script:ActualMongo | ConvertTo-Json -Compress)} }
function Read-Mongo { return Get-ProjectMongo $script:FixtureRoot 27117 'fixture-mongosh.exe' }

Scenario 'windows-arguments-chinese-spaces' {
    Backend-Fixture
    $arguments = @(Get-ProjectArguments $script:Server)
    Check ($arguments.Count -eq 2) 'Quoted argument count changed.'
    Check ($arguments[0] -ceq $script:BasePython) 'Quoted interpreter path changed.'
    Check ($arguments[1] -ceq $script:RunPath) 'Quoted script path changed.'
}
Scenario 'json-atomic-create-replace-roundtrip' {
    $path = Join-Path $script:Runtime 'atomic-fixture.json'
    Write-ProjectJson $path ([ordered]@{name = '中文 问卷'; revision = 1})
    Check ((Read-ProjectJson $path).name -ceq '中文 问卷') 'UTF-8 JSON round trip changed.'
    Write-ProjectJson $path ([ordered]@{name = '中文 问卷'; revision = 2})
    Check ((Read-ProjectJson $path).revision -eq 2) 'Atomic JSON replacement did not complete.'
    Check (@(Get-ChildItem -LiteralPath $script:Runtime -Filter 'atomic-fixture.json.*.tmp').Count -eq 0) 'An atomic-write temporary file remained.'
}
Scenario 'json-malformed-refused' {
    $path = Join-Path $script:Runtime 'malformed-fixture.json'
    [IO.File]::WriteAllText($path, '{')
    Reject { Read-ProjectJson $path }
}
Scenario 'json-oversized-refused' {
    $path = Join-Path $script:Runtime 'oversized-fixture.json'
    [IO.File]::WriteAllText($path, (' ' * 16385))
    Reject { Read-ProjectJson $path }
}
Scenario 'backend-venv-launcher-base-python-server' {
    Backend-Fixture
    $launcher = Assert-BackendInstance $script:Instance $script:Server $script:FixtureRoot
    Check ($launcher.ProcessId -eq $script:LauncherPid) 'The real launcher was not returned.'
    $backend = Get-ProjectBackend $script:FixtureRoot
    Check ($backend.Process.ProcessId -eq $script:ServerPid) 'The real server PID was not returned.'
    Check ($backend.Identity.creationDate -ceq (Get-ProcessStamp $script:Server)) 'Registered process time changed.'
    Assert-NoStopSignal
}
Scenario 'backend-foreign-port-without-instance-refused' {
    Reset-Fixture
    $script:ListenerMap[8000] = @([pscustomobject]@{LocalPort = 8000; LocalAddress = '127.0.0.1'; OwningProcess = $script:ForeignPid})
    Reject { Get-ProjectBackend $script:FixtureRoot }
    Assert-NoStopSignal
}
Scenario 'backend-same-executable-name-different-path-refused' {
    Backend-Fixture
    $script:Server.ExecutablePath = Join-Path $script:FixtureRoot 'foreign\python.exe'
    $script:Server.CommandLine = (Quote $script:Server.ExecutablePath) + ' ' + (Quote $script:RunPath)
    Reject { Get-ProjectBackend $script:FixtureRoot }
    Assert-NoStopSignal
}
Scenario 'backend-project-path-prefix-copy-refused' {
    Backend-Fixture
    $script:Server.CommandLine = (Quote $script:BasePython) + ' ' + (Quote (Join-Path ($script:FixtureRoot + '-copy') 'scripts\run.py'))
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-extra-argv-refused' {
    Backend-Fixture
    $script:Server.CommandLine += ' --reload'
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-script-as-other-argument-value-refused' {
    Backend-Fixture
    $script:Server.CommandLine = (Quote $script:BasePython) + ' --log-file ' + (Quote $script:RunPath)
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-wrong-parent-refused' {
    Backend-Fixture
    $script:Server.ParentProcessId = $script:ForeignPid
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-missing-launcher-refused' {
    Backend-Fixture
    $script:ProcessMap.Remove($script:LauncherPid)
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-server-pid-reuse-refused-before-operation' {
    Backend-Fixture
    $script:Server.CreationDate = $script:BaseDate.AddMinutes(1)
    Reject { Get-ProjectBackend $script:FixtureRoot }
    Check ($script:ProbeCalls -eq 0) 'A failed backend identity invoked MongoDB.'
    Assert-NoStopSignal
}
Scenario 'backend-launcher-pid-reuse-refused' {
    Backend-Fixture
    $script:Launcher.CreationDate = $script:BaseDate.AddMinutes(1)
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-stale-pid-without-listener-is-inactive' {
    Backend-Fixture
    $script:Server.CreationDate = $script:BaseDate.AddMinutes(1)
    $script:ListenerMap.Remove(8000)
    Check ($null -eq (Get-ProjectBackend $script:FixtureRoot)) 'A stale PID was returned as an active backend.'
    Assert-NoStopSignal
}
Scenario 'backend-identity-owner-token-changed-refused' {
    Backend-Fixture
    $script:Identity.instanceToken = ('A' * 43)
    Write-BackendIdentity
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-invalid-instance-token-refused' {
    Backend-Fixture
    $script:Instance.instanceToken = 'invalid'
    Write-ProjectJson (Join-Path $script:Runtime 'backend.instance.json') $script:Instance
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-instance-root-prefix-copy-refused' {
    Backend-Fixture
    $script:Instance.projectRoot = $script:FixtureRoot + '-copy'
    Write-ProjectJson (Join-Path $script:Runtime 'backend.instance.json') $script:Instance
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-missing-identity-refused' {
    Backend-Fixture
    Remove-Item -LiteralPath (Join-Path $script:Runtime 'backend.process.json')
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-corrupt-instance-refused' {
    Backend-Fixture
    [IO.File]::WriteAllText((Join-Path $script:Runtime 'backend.instance.json'), '{')
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-corrupt-identity-refused' {
    Backend-Fixture
    [IO.File]::WriteAllText((Join-Path $script:Runtime 'backend.process.json'), '{')
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
foreach ($field in @('ExecutablePath', 'CommandLine', 'CreationDate', 'ParentProcessId')) {
    Scenario ('backend-missing-cim-' + $field) {
        Backend-Fixture
        $script:Server.PSObject.Properties.Remove($field)
        Reject { Get-ProjectBackend $script:FixtureRoot }
        Assert-NoStopSignal
    }
}
Scenario 'backend-wrong-listener-owner-refused' {
    Backend-Fixture
    $script:ListenerMap[8000][0].OwningProcess = $script:ForeignPid
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'backend-public-listener-refused' {
    Backend-Fixture
    $script:ListenerMap[8000][0].LocalAddress = '0.0.0.0'
    Reject { Get-ProjectBackend $script:FixtureRoot }
}
Scenario 'mongo-owned-process-and-server-parameters' {
    Mongo-Fixture
    $mongo = Read-Mongo
    Check ($mongo.Pid -eq $script:MongoPid) 'MongoDB PID changed.'
    Check (Test-ProjectPath $mongo.DbPath $script:DataPath) 'MongoDB data path changed.'
    Check ($mongo.Port -eq 27117) 'MongoDB port changed.'
    Check ($script:ProbeCalls -eq 1) 'MongoDB server identity was not probed exactly once.'
    Assert-NoStopSignal
}
Scenario 'mongo-command-dbpath-prefix-copy-refused-before-probe' {
    Mongo-Fixture
    $script:Mongo.CommandLine = $script:Mongo.CommandLine.Replace($script:DataPath, (Join-Path ($script:FixtureRoot + '-copy') '.runtime\mongo-data'))
    Reject { Read-Mongo }
    Check ($script:ProbeCalls -eq 0) 'A foreign dbPath was probed.'
}
Scenario 'mongo-command-wrong-port-refused-before-probe' {
    Mongo-Fixture
    $script:Mongo.CommandLine = $script:Mongo.CommandLine.Replace('--port 27117', '--port 27118')
    Reject { Read-Mongo }
    Check ($script:ProbeCalls -eq 0) 'A foreign port was probed.'
}
Scenario 'mongo-command-extra-argv-refused' {
    Mongo-Fixture
    $script:Mongo.CommandLine += ' --fork'
    Reject { Read-Mongo }
    Check ($script:ProbeCalls -eq 0) 'An unknown command format was probed.'
}
Scenario 'mongo-command-duplicate-dbpath-refused' {
    Mongo-Fixture
    $script:Mongo.CommandLine += (' --dbpath ' + (Quote $script:DataPath))
    Reject { Read-Mongo }
}
Scenario 'mongo-command-bind-all-refused' {
    Mongo-Fixture
    $script:Mongo.CommandLine = $script:Mongo.CommandLine.Replace('--bind_ip 127.0.0.1', '--bind_ip 0.0.0.0')
    Reject { Read-Mongo }
}
foreach ($field in @('pid', 'dbPath', 'port', 'bindIp', 'replicaSet')) {
    Scenario ('mongo-parsed-server-wrong-' + $field + '-refused') {
        Mongo-Fixture
        switch ($field) {
            'pid' { $script:ActualMongo.pid = $script:ForeignPid }
            'dbPath' { $script:ActualMongo.dbPath = Join-Path ($script:FixtureRoot + '-copy') '.runtime\mongo-data' }
            'port' { $script:ActualMongo.port = 27118 }
            'bindIp' { $script:ActualMongo.bindIp = '0.0.0.0' }
            'replicaSet' { $script:ActualMongo.replicaSet = 'foreign' }
        }
        Update-Probe
        Reject { Read-Mongo }
        Check ($script:ProbeCalls -eq 1) 'The parsed-parameter refusal did not use the real probe boundary.'
        Assert-NoStopSignal
    }
}
Scenario 'mongo-probe-failure-refused' {
    Mongo-Fixture
    $script:ProbeResult = [pscustomobject]@{ExitCode = 2; Output = ''}
    Reject { Read-Mongo }
}
Scenario 'mongo-probe-malformed-refused' {
    Mongo-Fixture
    $script:ProbeResult = [pscustomobject]@{ExitCode = 0; Output = '{'}
    Reject { Read-Mongo }
}
Scenario 'mongo-probe-missing-fields-refused' {
    Mongo-Fixture
    $script:ProbeResult = [pscustomobject]@{ExitCode = 0; Output = '{}'}
    Reject { Read-Mongo }
}
Scenario 'mongo-pid-reuse-refused' {
    Mongo-Fixture
    $script:Mongo.CreationDate = $script:BaseDate.AddMinutes(1)
    Reject { Read-Mongo }
    Assert-NoStopSignal
}
foreach ($field in @('ExecutablePath', 'CommandLine', 'CreationDate')) {
    Scenario ('mongo-missing-cim-' + $field) {
        Mongo-Fixture
        $script:Mongo.PSObject.Properties.Remove($field)
        Reject { Read-Mongo }
    }
}
Scenario 'mongo-multiple-listener-owners-refused' {
    Mongo-Fixture
    $script:ListenerMap[27117] += [pscustomobject]@{LocalPort = 27117; LocalAddress = '127.0.0.1'; OwningProcess = $script:ForeignPid}
    Reject { Read-Mongo }
    Check ($script:ProbeCalls -eq 0) 'Multiple port owners reached the probe.'
}
Scenario 'mongo-bind-all-listener-refused' {
    Mongo-Fixture
    $script:ListenerMap[27117][0].LocalAddress = '0.0.0.0'
    Reject { Read-Mongo }
}
Scenario 'mongo-inactive-without-listener' {
    Reset-Fixture
    Check ($null -eq (Read-Mongo)) 'An absent MongoDB was returned as active.'
    Check ($script:ProbeCalls -eq 0) 'An absent MongoDB was probed.'
}

function Invoke-LockChild {
    $outputPath = Join-Path $script:Runtime ('lock-' + [guid]::NewGuid().ToString('N') + '.out')
    $errorPath = $outputPath + '.err'
    $child = $null
    try {
        $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Quote $script:HarnessPath), '-HelperPath', (Quote $HelperPath), '-FixtureRoot', (Quote $script:FixtureRoot.ToUpperInvariant()), '-Mode', 'LockProbe')
        $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath
        # PS5 may lose ExitCode for a short-lived Start-Process child unless its
        # native handle is retained before waiting (the mutex result still ran).
        $null = $child.Handle
        if (-not $child.WaitForExit(15000)) { throw 'The lock probe did not complete in 15 seconds.' }
        $child.Refresh()
        return [pscustomobject]@{ExitCode = $child.ExitCode; Output = ((Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8) -as [string]).Trim()}
    } finally {
        if ($child) { $child.Dispose() }
        foreach ($path in @($outputPath, $errorPath)) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path } }
    }
}
Scenario 'mutex-cross-powershell-contention-and-release' {
    $lease = Enter-ProjectLifecycle $script:FixtureRoot
    try {
        $blocked = Invoke-LockChild
        Check ($blocked.ExitCode -eq 17 -and $blocked.Output -ceq 'LOCK_BUSY') ('Lock contention failed: exit=' + $blocked.ExitCode + '; output=' + $blocked.Output)
    } finally { Exit-ProjectLifecycle $lease }
    $released = Invoke-LockChild
    Check ($released.ExitCode -eq 0 -and $released.Output -ceq 'LOCK_ACQUIRED') ('Lock release failed: exit=' + $released.ExitCode + '; output=' + $released.Output)
}

$result = [ordered]@{scenarios = $script:ScenarioCount; assertions = $script:AssertionCount; failed = $script:FailedScenarios.Count; failedScenarios = @($script:FailedScenarios)}
Write-Output ('LIFECYCLE_RESULT ' + ($result | ConvertTo-Json -Compress))
if ($script:FailedScenarios.Count -ne 0) { exit 1 }
exit 0
