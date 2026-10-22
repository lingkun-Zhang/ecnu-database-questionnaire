param([string]$Mongod = '', [string]$Mongosh = '', [ValidateRange(1,65535)][int]$Port = 27117, [switch]$InternalLockHeld)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lifecycle.ps1')
$projectRoot = Get-ProjectRoot $PSScriptRoot
Assert-ProjectRuntime $projectRoot
$lease = $null
if (-not $InternalLockHeld) { $lease = Enter-ProjectLifecycle $projectRoot }
try {
    $runtimeRoot = Join-Path $projectRoot '.runtime'
    $databaseRoot = Join-Path $runtimeRoot 'mongo-data'
    if (-not (Test-Path -LiteralPath $databaseRoot)) { New-Item -ItemType Directory -Path $databaseRoot | Out-Null }
    if (-not $Mongod) { $Mongod = (Get-Command mongod -ErrorAction Stop).Source }
    $Mongod = (Get-Item -LiteralPath $Mongod -ErrorAction Stop).FullName
    $mongoShell = Get-MongoshPath $Mongosh
    $mongo = Get-ProjectMongo $projectRoot $Port $mongoShell
    if (-not $mongo) {
        Assert-PlainProjectPath (Join-Path $runtimeRoot 'mongod.log')
        $mongoArguments = @('--dbpath', ('"' + $databaseRoot + '"'), '--port', $Port, '--bind_ip', '127.0.0.1', '--replSet', 'rs0', '--logpath', ('"' + (Join-Path $runtimeRoot 'mongod.log') + '"'), '--logappend')
        $mongoProcess = Start-Process -FilePath $Mongod -ArgumentList $mongoArguments -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath (Join-Path $runtimeRoot 'mongod.pid') -Value $mongoProcess.Id -Encoding ASCII
        $timer = [Diagnostics.Stopwatch]::StartNew()
        do {
            if (-not (Get-ProjectProcess $mongoProcess.Id)) { throw 'MongoDB启动失败，请检查.runtime/mongod.log。' }
            if (@(Get-ProjectPortListeners $Port).Count) { $mongo = Get-ProjectMongo $projectRoot $Port $mongoShell; if ($mongo) { break } }
            Start-Sleep -Milliseconds 200
        } while ($timer.Elapsed.TotalSeconds -lt 20)
        if (-not $mongo) { throw 'MongoDB未能就绪，请检查.runtime/mongod.log。' }
    }
    Write-ProjectJson (Join-Path $runtimeRoot 'mongod.process.json') ([ordered]@{pid=$mongo.Pid;creationDate=$mongo.CreationDate;executablePath=$mongo.ExecutablePath;dbPath=$mongo.DbPath;port=$Port})
    Set-Content -LiteralPath (Join-Path $runtimeRoot 'mongod.pid') -Value $mongo.Pid -Encoding ASCII
    $initialization = 'try { rs.status(); } catch(e) { if(e.code===94) { rs.initiate({_id:"rs0",members:[{_id:0,host:"127.0.0.1:__PORT__"}]}); } else { throw e; } }'
    $initResult = Invoke-ProjectMongoFile $projectRoot $mongoShell $Port $initialization.Replace('__PORT__', [string]$Port)
    if ($initResult.ExitCode -ne 0) { throw '副本集初始化检查失败。' }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $primary = Invoke-ProjectMongoFile $projectRoot $mongoShell $Port 'print(JSON.stringify({primary:db.hello().isWritablePrimary,setName:db.hello().setName}));'
        if ($primary.ExitCode -eq 0) {
            $status = ($primary.Output.Trim().Split("`n") | Select-Object -Last 1) | ConvertFrom-Json
            if ($status.primary -eq $true -and $status.setName -eq 'rs0') { Write-Output "项目MongoDB已就绪：127.0.0.1:$Port，副本集rs0"; return }
        }
        Start-Sleep -Milliseconds 300
    } while ($timer.Elapsed.TotalSeconds -lt 20)
    throw '等待副本集主节点超时。'
} finally { Exit-ProjectLifecycle $lease }