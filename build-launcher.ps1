$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectRoot 'launcher\MaoMaoLauncher.cs'
$manifest = Join-Path $projectRoot 'launcher\app.manifest'
$icon = Join-Path $projectRoot 'miao.ico'
$outputName = 'MaoMao.exe'
$output = Join-Path $projectRoot $outputName
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

foreach ($required in @($source, $manifest, $icon, $compiler)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing launcher build input: $required"
    }
}

& $compiler `
    /nologo `
    /target:winexe `
    /optimize+ `
    /debug- `
    /codepage:65001 `
    "/win32icon:$icon" `
    "/win32manifest:$manifest" `
    "/reference:System.Windows.Forms.dll" `
    "/out:$output" `
    $source

if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $output)) {
    throw 'Launcher build failed.'
}

Write-Host "Built: $output"
