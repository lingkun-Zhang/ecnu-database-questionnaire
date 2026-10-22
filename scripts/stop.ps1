param(
    [string]$Mongosh = '',
    [ValidateRange(1, 65535)][int]$Port = 27117,
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 30
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lifecycle.ps1')

function Test-StopProcessIdentity {
    param([int]$ProcessId, [string]$CreationDate)
    $process = Get-ProjectProcess $ProcessId
    return ($null -ne $process -and (Get-ProcessStamp $process) -ceq $CreationDate)
}

function Wait-StopBackend {
    param($Backend, [int]$Seconds)
    $identity = $Backend.Identity
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    do {
        $serverAlive = Test-StopProcessIdentity ([int]$identity.pid) ([string]$identity.creationDate)
        $launcherAlive = Test-StopProcessIdentity ([int]$identity.launcherPid) ([string]$identity.launcherCreationDate)
        if (-not $serverAlive -and -not $launcherAlive) {
            if (@(Get-ProjectPortListeners 8000).Count -ne 0) {
                throw '本项目服务已退出，但8000端口仍被占用；未继续关闭数据库。'
            }
            return
        }
        if ($timer.Elapsed.TotalSeconds -ge $Seconds) { break }
        Start-Sleep -Milliseconds 200
    } while ($true)
    throw '等待本项目服务正常退出超时；未强制终止进程，数据库保持运行。'
}

function Wait-StopMongo {
    param($Mongo, [int]$Seconds)
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    do {
        $alive = Test-StopProcessIdentity ([int]$Mongo.Pid) ([string]$Mongo.CreationDate)
        if (-not $alive) {
            if (@(Get-ProjectPortListeners ([int]$Mongo.Port)).Count -ne 0) {
                throw '本项目MongoDB进程已退出，但数据库端口仍被占用；未操作占用端口的其他进程。'
            }
            return
        }
        if ($timer.Elapsed.TotalSeconds -ge $Seconds) { break }
        Start-Sleep -Milliseconds 200
    } while ($true)
    throw '等待本项目MongoDB正常退出超时；未强制终止进程，请查看.runtime/mongod.log。'
}

$projectRoot = Get-ProjectRoot $PSScriptRoot
Assert-ProjectRuntime $projectRoot
$lease = Enter-ProjectLifecycle $projectRoot
try {
    # 在任何停止动作前，同时确认两项服务的归属。健康接口和旧PID均不能单独证明归属。
    $backend = Get-ProjectBackend $projectRoot
    $mongoShell = Get-MongoshPath $Mongosh
    $mongo = Get-ProjectMongo $projectRoot $Port $mongoShell

    if ($null -ne $backend) {
        $identity = $backend.Identity
        if (-not (Test-StopProcessIdentity ([int]$identity.pid) ([string]$identity.creationDate))) {
            throw '本项目服务在停止前发生变化，请重新运行Stop.cmd。'
        }
        $stopPath = Join-Path $projectRoot '.runtime\backend.stop.json'
        Write-ProjectJson $stopPath ([ordered]@{
            pid = [int]$backend.Instance.pid
            instanceToken = [string]$backend.Instance.instanceToken
        })
        Wait-StopBackend $backend $TimeoutSeconds
        Write-Output '本项目服务已正常退出。'
    } else {
        Write-Output '本项目服务已经停止。'
    }

    if ($null -ne $mongo) {
        # 应用退出后再次复核，避免在等待期间数据库已被替换。
        $currentMongo = Get-ProjectMongo $projectRoot $Port $mongoShell
        if ($null -eq $currentMongo) {
            Write-Output '本项目MongoDB已经停止。'
        } else {
            if ([int]$currentMongo.Pid -ne [int]$mongo.Pid -or
                [string]$currentMongo.CreationDate -cne [string]$mongo.CreationDate) {
                throw '数据库进程在停止前发生变化，未发送关闭命令。'
            }
            $expected = [ordered]@{
                pid = [int]$mongo.Pid
                dbPath = [string]$mongo.DbPath
                port = [int]$mongo.Port
                replicaSet = 'rs0'
            } | ConvertTo-Json -Compress
            # 由helper写入临时JS文件执行；这里的JSON不会经过native --eval参数转义。
            $shutdownScript = @'
"use strict";
const expected = __EXPECTED__;
const path = require("path").win32;
function fail(message) { throw new Error("Project shutdown refused: " + message); }
function normalizeDbPath(value) {
    if (typeof value !== "string" || !path.isAbsolute(value)) fail("absolute dbPath required");
    return path.normalize(value).replace(/[\\/]+$/, "").toLowerCase();
}
const optionsResult = db.adminCommand({getCmdLineOpts: 1});
const statusResult = db.adminCommand({serverStatus: 1});
if (optionsResult.ok !== 1 || statusResult.ok !== 1) fail("identity probe failed");
const parsed = optionsResult.parsed || {};
const net = parsed.net || {};
const storage = parsed.storage || {};
const replication = parsed.replication || {};
const bind = Array.isArray(net.bindIp) ? net.bindIp : [net.bindIp];
if (Number(statusResult.pid) !== expected.pid) fail("PID changed");
if (normalizeDbPath(storage.dbPath) !== normalizeDbPath(expected.dbPath)) fail("dbPath changed");
if (Number(net.port) !== expected.port) fail("port changed");
if (bind.length !== 1 || bind[0] !== "127.0.0.1") fail("bind address changed");
if ((replication.replSetName || replication.replSet) !== expected.replicaSet) fail("replica set changed");
if (db.hello().setName !== expected.replicaSet) fail("active replica set changed");
if (Number(db.adminCommand({serverStatus: 1}).pid) !== expected.pid) fail("PID changed before shutdown");
print("PROJECT_SHUTDOWN_REQUESTED");
db.adminCommand({shutdown: 1, force: false});
'@
            $shutdownScript = $shutdownScript.Replace('__EXPECTED__', $expected)
            $shutdownResult = Invoke-ProjectMongoFile $projectRoot $mongoShell $Port $shutdownScript
            # 正常shutdown也可能使mongosh断连并返回非0，最终以已识别进程退出且端口释放为准。
            Wait-StopMongo $mongo $TimeoutSeconds
            Write-Output '本项目MongoDB已正常退出，数据目录和项目配置已保留。'
        }
    } else {
        Write-Output '本项目MongoDB已经停止，数据目录和项目配置已保留。'
    }
} finally {
    Exit-ProjectLifecycle $lease
}
