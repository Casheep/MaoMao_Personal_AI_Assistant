$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$existingPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$env:PYTHONPATH = if ($existingPythonPath) {
    "$projectRoot;$existingPythonPath"
} else {
    $projectRoot
}
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $projectRoot 'requirements.txt'
$requirementsMarker = Join-Path $projectRoot '.venv\.requirements.sha256'

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython -c "import sys; raise SystemExit(0 if (3, 12) <= sys.version_info < (3, 14) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw '现有 .venv 版本不兼容，请使用 Python 3.12 或 3.13 重新创建。'
    }
} else {
    py -3.12 -c "import sys; raise SystemExit(0 if (3, 12) <= sys.version_info < (3, 14) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'MaoMao v0.0.3_beta2 需要 Python 3.12（或兼容的 3.13）。' }
    py -3.12 -m venv (Join-Path $projectRoot '.venv')
    & $venvPython -m pip install --upgrade pip
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirements).Hash
$installedHash = if (Test-Path -LiteralPath $requirementsMarker) {
    (Get-Content -LiteralPath $requirementsMarker -Raw).Trim()
} else {
    ''
}
if ($installedHash -ne $requirementsHash) {
    & $venvPython -m pip install --disable-pip-version-check -q -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "依赖安装失败，退出码：$LASTEXITCODE"
    }
    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -Encoding ascii
}

& $venvPython -m assistant_app.cli @args
exit $LASTEXITCODE
