param(
    [ValidateSet('lite', 'full')]
    [string]$Edition = 'full',
    [string]$PythonHome = '',
    [switch]$RebuildRuntime,
    [switch]$CompileLauncher,
    [switch]$RebuildLauncher
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseCache = Join-Path $projectRoot '.release-cache'
$distRoot = Join-Path $projectRoot 'dist'
$packageName = "MaoMao-beta-$Edition"
$stage = Join-Path $distRoot $packageName
$zipPath = Join-Path $distRoot "$packageName.zip"
$requirements = Join-Path $projectRoot 'requirements-release.txt'
$launcherSource = Join-Path $projectRoot 'launcher\MaoMaoLauncher.cs'
$launcherManifest = Join-Path $projectRoot 'launcher\beta.manifest'
$launcherSourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $launcherSource).Hash.Substring(0, 12)
$launcherManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $launcherManifest).Hash.Substring(0, 12)
$launcherCache = Join-Path $releaseCache "launchers\MaoMao-$launcherSourceHash-$launcherManifestHash.exe"
$compatibleLauncherCache = Join-Path $releaseCache 'launchers\MaoMao.exe'
if ($RebuildLauncher) { $CompileLauncher = $true }
if (-not $RebuildLauncher -and -not (Test-Path -LiteralPath $launcherCache) -and (Test-Path -LiteralPath $compatibleLauncherCache)) {
    # The launcher behavior is version-independent. Reusing a previously
    # verified binary also avoids repeated scans of newly compiled executables.
    $launcherCache = $compatibleLauncherCache
}
if (-not (Test-Path -LiteralPath $launcherCache) -and -not $CompileLauncher) {
    throw 'No verified launcher is available. Build in GitHub Actions or pass -CompileLauncher in a trusted build environment.'
}
$versionSource = Get-Content -LiteralPath (Join-Path $projectRoot 'assistant_app\version.py') -Raw -Encoding UTF8
$versionMatch = [regex]::Match($versionSource, '__version__\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) { throw 'Unable to read the MaoMao version.' }
$releaseVersion = $versionMatch.Groups[1].Value

function Remove-BuildPath([string]$Path, [string]$AllowedParent) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolvedParent = [System.IO.Path]::GetFullPath($AllowedParent).TrimEnd('\')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedParent + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside build directory: $resolvedPath"
    }
    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

if ($RebuildLauncher -and (Test-Path -LiteralPath $launcherCache)) {
    Remove-BuildPath $launcherCache $releaseCache
}

function Merge-ConfigObject([object]$Base, [object]$Override) {
    foreach ($property in $Override.PSObject.Properties) {
        $current = $Base.PSObject.Properties[$property.Name]
        if ($current -and $current.Value -is [PSCustomObject] -and $property.Value -is [PSCustomObject]) {
            Merge-ConfigObject $current.Value $property.Value
        } else {
            $Base | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value -Force
        }
    }
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirements).Hash.Substring(0, 16)
$runtimeCache = ''
if (-not $PythonHome -and -not $RebuildRuntime -and (Test-Path -LiteralPath $releaseCache)) {
    $cachedRuntime = Get-ChildItem -LiteralPath $releaseCache -Directory -Filter "python*-$requirementsHash" | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'pythonw.exe')
    } | Select-Object -First 1
    if ($cachedRuntime) { $runtimeCache = $cachedRuntime.FullName }
}
if (-not $runtimeCache) {
    $venvConfig = Join-Path $projectRoot '.venv\pyvenv.cfg'
    if (-not $PythonHome -and (Test-Path -LiteralPath $venvConfig)) {
        $homeLine = Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
        if ($homeLine) { $PythonHome = ($homeLine -split '=', 2)[1].Trim() }
    }
    $basePython = if ($PythonHome) { Join-Path $PythonHome 'python.exe' } else { '' }
    if (-not $basePython -or -not (Test-Path -LiteralPath $basePython)) {
        $PythonHome = & py -3.10 -c "import sys; print(sys.base_prefix)"
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required to build the portable runtime.' }
        $PythonHome = $PythonHome.Trim()
        $basePython = Join-Path $PythonHome 'python.exe'
    }
    $pythonTag = (& $basePython -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()
    $runtimeCache = Join-Path $releaseCache "python$pythonTag-$requirementsHash"
    if ($RebuildRuntime -and (Test-Path -LiteralPath $runtimeCache)) {
        Remove-BuildPath $runtimeCache $releaseCache
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $runtimeCache 'pythonw.exe'))) {
    if (-not $basePython) { throw 'Portable runtime cache is invalid.' }
    New-Item -ItemType Directory -Path $releaseCache -Force | Out-Null
    $buildingRuntime = "$runtimeCache.building"
    Remove-BuildPath $buildingRuntime $releaseCache
    New-Item -ItemType Directory -Path $buildingRuntime -Force | Out-Null

    foreach ($directory in @('DLLs', 'Lib', 'tcl')) {
        $sourceDirectory = Join-Path $PythonHome $directory
        if (-not (Test-Path -LiteralPath $sourceDirectory)) {
            throw "Python runtime directory is missing: $sourceDirectory"
        }
        Copy-Item -LiteralPath $sourceDirectory -Destination (Join-Path $buildingRuntime $directory) -Recurse
    }
    $baseSitePackages = Join-Path $buildingRuntime 'Lib\site-packages'
    if (Test-Path -LiteralPath $baseSitePackages) {
        Remove-BuildPath $baseSitePackages $buildingRuntime
    }
    New-Item -ItemType Directory -Path $baseSitePackages -Force | Out-Null

    Get-ChildItem -LiteralPath $PythonHome -File | Where-Object {
        $_.Name -match '^(python|pythonw).*\.exe$' -or
        $_.Name -match '^python.*\.dll$' -or
        $_.Name -match '^vcruntime.*\.dll$' -or
        $_.Name -match '^LICENSE'
    } | Copy-Item -Destination $buildingRuntime

    $pipCache = Join-Path $releaseCache 'pip'
    New-Item -ItemType Directory -Path $pipCache -Force | Out-Null
    & $basePython -m pip install --disable-pip-version-check --no-warn-script-location --cache-dir $pipCache --target $baseSitePackages -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Installing portable API dependencies failed.' }

    $oldPythonHome = $env:PYTHONHOME
    try {
        $env:PYTHONHOME = $buildingRuntime
        & (Join-Path $buildingRuntime 'python.exe') -c "import PySide6,httpx,numpy,sounddevice,soundfile,vosk,win32api,pywinauto,pyautogui,pycaw,psutil,PIL; print('portable runtime ok')"
        if ($LASTEXITCODE -ne 0) { throw 'Portable runtime import check failed.' }
    } finally {
        $env:PYTHONHOME = $oldPythonHome
    }
    Move-Item -LiteralPath $buildingRuntime -Destination $runtimeCache
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
$existingLauncher = Join-Path $stage 'MaoMao.exe'
if (-not $RebuildLauncher -and -not (Test-Path -LiteralPath $launcherCache) -and (Test-Path -LiteralPath $existingLauncher)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $launcherCache) -Force | Out-Null
    Copy-Item -LiteralPath $existingLauncher -Destination $launcherCache
}
Remove-BuildPath $stage $distRoot
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$runtimeTarget = Join-Path $stage 'runtime'
try {
    Copy-Item -LiteralPath $runtimeCache -Destination $runtimeTarget -Recurse
} catch {
    if (-not (Test-Path -LiteralPath $zipPath)) { throw }
    Write-Warning 'Runtime cache is not readable in this session; reusing the portable runtime from the previous release.'
    $runtimeFallback = Join-Path $distRoot ".$packageName-runtime-fallback"
    Remove-BuildPath $runtimeFallback $distRoot
    try {
        Expand-Archive -LiteralPath $zipPath -DestinationPath $runtimeFallback -Force
        $archivedRuntime = Join-Path $runtimeFallback 'runtime'
        if (-not (Test-Path -LiteralPath (Join-Path $archivedRuntime 'pythonw.exe'))) {
            throw 'The previous release does not contain a valid portable runtime.'
        }
        Remove-BuildPath $runtimeTarget $stage
        Copy-Item -LiteralPath $archivedRuntime -Destination $runtimeTarget -Recurse
    } finally {
        Remove-BuildPath $runtimeFallback $distRoot
    }
}

# Development tests and generated command wrappers are unnecessary at runtime.
# Removing them also prevents third-party sample credentials and build paths
# from entering the public archive.
$runtimeCleanup = @(
    (Join-Path $runtimeTarget 'Lib\test'),
    (Join-Path $runtimeTarget 'Lib\site-packages\bin'),
    (Join-Path $runtimeTarget 'Lib\site-packages\Crypto\SelfTest')
)
$runtimeLibrary = Join-Path $runtimeTarget 'Lib'
if (Test-Path -LiteralPath $runtimeLibrary) {
    $runtimeCleanup += Get-ChildItem -LiteralPath $runtimeLibrary -Recurse -Directory | Where-Object {
        $_.Name -in @('test', 'tests', 'testing', 'unittests')
    } | Sort-Object { $_.FullName.Length } -Descending | Select-Object -ExpandProperty FullName
}
foreach ($cleanupPath in $runtimeCleanup) {
    Remove-BuildPath $cleanupPath $runtimeTarget
}

Copy-Item -LiteralPath (Join-Path $projectRoot 'assistant_app') -Destination (Join-Path $stage 'assistant_app') -Recurse
$unusedLocalWorker = Join-Path $stage 'assistant_app\workers\cosyvoice_worker.py'
if (Test-Path -LiteralPath $unusedLocalWorker) {
    Remove-Item -LiteralPath $unusedLocalWorker -Force
}
Get-ChildItem -LiteralPath (Join-Path $stage 'assistant_app') -Recurse -Directory -Filter '__pycache__' | ForEach-Object {
    Remove-BuildPath $_.FullName (Join-Path $stage 'assistant_app')
}
Get-ChildItem -LiteralPath (Join-Path $stage 'assistant_app') -Recurse -File | Where-Object {
    $_.Extension -in @('.pyc', '.pyo')
} | Remove-Item -Force

foreach ($asset in @('miao.ico', 'miao-icon.png', 'miao.jpg')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $asset) -Destination $stage
}
$baseConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$betaOverrides = Get-Content -LiteralPath (Join-Path $projectRoot 'release\beta-overrides.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Merge-ConfigObject $baseConfig $betaOverrides
$baseConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $stage 'config.json') -Encoding UTF8
$stageKeyFile = Join-Path $stage 'api_key.txt'
"kimi_key=`nmimo_key=`n" | Set-Content -LiteralPath $stageKeyFile -Encoding UTF8

if ($Edition -eq 'full') {
    $voskSource = Join-Path $projectRoot 'engines\vosk\vosk-model-small-cn-0.22'
    if (-not (Test-Path -LiteralPath (Join-Path $voskSource 'am\final.mdl'))) {
        throw 'The full edition requires engines\vosk\vosk-model-small-cn-0.22.'
    }
    $voskTarget = Join-Path $stage 'engines\vosk\vosk-model-small-cn-0.22'
    New-Item -ItemType Directory -Path (Split-Path -Parent $voskTarget) -Force | Out-Null
    Copy-Item -LiteralPath $voskSource -Destination $voskTarget -Recurse
}
New-Item -ItemType Directory -Path (Join-Path $stage 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'voices') -Force | Out-Null

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$launcherOutput = Join-Path $stage 'MaoMao.exe'
if (-not (Test-Path -LiteralPath $launcherCache)) {
    if (-not $CompileLauncher) {
        throw 'Local launcher compilation is disabled by default.'
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $launcherCache) -Force | Out-Null
    & $compiler /nologo /target:winexe /optimize+ /debug- /codepage:65001 "/win32icon:$(Join-Path $projectRoot 'miao.ico')" "/win32manifest:$launcherManifest" "/reference:System.Windows.Forms.dll" "/out:$launcherCache" $launcherSource
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $launcherCache)) {
        throw 'Building the cached beta launcher failed.'
    }
}
Copy-Item -LiteralPath $launcherCache -Destination $launcherOutput

$forbiddenNames = @('assistant.db', 'xiaomi_token.txt', 'config.local.json', 'wake-templates.npz')
$forbiddenFiles = Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $forbiddenNames -contains $_.Name }
if ($forbiddenFiles) {
    throw "Private files entered the release: $($forbiddenFiles.FullName -join ', ')"
}
$publicTextFiles = Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object {
    $_.FullName -notlike "$runtimeTarget*" -and
    $_.FullName -ne $stageKeyFile -and
    $_.Extension -in @('.py', '.json', '.md', '.txt', '.ps1', '.cmd')
}
foreach ($textFile in $publicTextFiles) {
    $text = Get-Content -LiteralPath $textFile.FullName -Raw -Encoding UTF8
    if ($text -match '(?i)[A-Z]:[\\/]Users[\\/]' -or
        $text -match 'BEGIN (OPENSSH|RSA|EC) PRIVATE KEY' -or
        $text -match '(^|[^A-Za-z])sk-[A-Za-z0-9_-]{12,}') {
        throw "Potential private data entered the public release: $($textFile.FullName)"
    }
}
$releaseFiles = Get-ChildItem -LiteralPath $stage -Recurse -File
$excludedItems = @('publisher keys', 'local memory', 'usage database', 'screenshots', 'voice templates', 'Qwen ASR', 'CosyVoice', 'F5-TTS', 'Xiaomi token')
if ($Edition -eq 'lite') { $excludedItems += 'Vosk wake-word model' }
$manifest = [ordered]@{
    name = 'MaoMao beta'
    version = $releaseVersion
    edition = $Edition
    created_at = (Get-Date).ToString('o')
    file_count = $releaseFiles.Count
    size_bytes = ($releaseFiles | Measure-Object Length -Sum).Sum
    bundled_local_model = if ($Edition -eq 'full') { 'Vosk small Chinese wake-word model' } else { 'none' }
    startup_component_policy = if ($Edition -eq 'lite') { 'download-missing' } else { 'bundled' }
    bundled_keys = $false
    excluded = $excludedItems
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'release-manifest.json') -Encoding UTF8

if (Test-Path -LiteralPath $zipPath) {
    Remove-BuildPath $zipPath $distRoot
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)

$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
Write-Host "Package: $zipPath"
Write-Host "Folder:  $stage"
Write-Host "SHA256:  $zipHash"
Write-Host 'Public beta: api_key.txt contains no API keys.'
