param([string]$PythonExe = '')
$ErrorActionPreference = 'Stop'

function Resolve-InstallPython([string]$ExplicitPython) {
    $candidates = @()
    if ($ExplicitPython) { $candidates += @{Executable=$ExplicitPython; Options=@()} }
    else {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $candidates += @{Executable='py'; Options=@('-3.12')}
            $candidates += @{Executable='py'; Options=@('-3')}
        }
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += @{Executable='python'; Options=@()} }
    }
    $probe = 'import sys; sys.exit(3) if sys.version_info < (3,12) else print(sys.executable)'
    foreach ($candidate in $candidates) {
        try {
            $probeArguments = @($candidate.Options) + @('-c', $probe)
            $result = @(& $candidate.Executable @probeArguments 2>$null)
            if ($LASTEXITCODE -eq 0 -and $result.Count -and (Test-Path -LiteralPath $result[-1])) { return [string]$result[-1] }
        } catch { }
    }
    throw '未找到可用的Python 3.12及以上版本。请先安装，或使用 -PythonExe 指定python.exe的完整路径。'
}

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $projectRoot
try {
    if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw '请先安装Node.js 20.19+或22.12+（含npm），再重新安装。'
    }
    $nodeVersion = [version](& node -p 'process.versions.node')
    if (-not (($nodeVersion.Major -eq 20 -and $nodeVersion.Minor -ge 19) -or $nodeVersion -ge [version]'22.12.0')) {
        throw '当前Node.js版本不满足前端要求，请使用20.19+或22.12+。'
    }
    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        $basePython = Resolve-InstallPython $PythonExe
        & $basePython -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw '创建Python虚拟环境失败。' }
    }
    & $projectPython -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 3)'
    if ($LASTEXITCODE -ne 0) { throw '已有.venv版本低于Python 3.12；请另建干净项目目录后重新安装，原环境未删除。' }
    & $projectPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    & $projectPython scripts/setup.py
    if ($LASTEXITCODE -ne 0) { throw '本机配置生成失败。' }
    Push-Location frontend
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端类型检查或构建失败。' }
    } finally { Pop-Location }
    Write-Output '依赖和配置已准备好，请运行 Start.cmd。'
} finally { Pop-Location }
