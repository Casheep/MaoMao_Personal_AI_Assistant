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

if (-not (Test-Path -LiteralPath $venvPython)) {
    py -3.10 -m venv (Join-Path $projectRoot '.venv')
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
