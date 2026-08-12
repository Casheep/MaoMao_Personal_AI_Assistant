param(
    [string]$Commit = 'HEAD'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$archiveRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot '..\beta'))

$commitHash = (& git -C $projectRoot rev-parse --verify "$Commit^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or -not $commitHash) {
    throw "Unable to resolve commit: $Commit"
}

$versionSource = & git -C $projectRoot show "${commitHash}:assistant_app/version.py"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the version from commit $commitHash."
}
$versionMatch = [regex]::Match(($versionSource -join "`n"), '__version__\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "Unable to parse the version from commit $commitHash."
}
$version = $versionMatch.Groups[1].Value
$seriesMatch = [regex]::Match($version, '^(v\d+\.\d+\.\d+)(?:_|$)')
if (-not $seriesMatch.Success) {
    throw "Unsupported MaoMao version format: $version"
}

$archiveDirectory = [System.IO.Path]::GetFullPath(
    (Join-Path $archiveRoot $seriesMatch.Groups[1].Value)
)
if (-not $archiveDirectory.StartsWith($archiveRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Archive target is outside the beta archive root.'
}
New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null

$sourceArchive = Join-Path $archiveDirectory "MaoMao_${version}_source.zip"
$historyBundle = Join-Path $archiveDirectory "MaoMao_${version}_history.bundle"
$temporarySource = "$sourceArchive.building-$PID"
$temporaryBundle = "$historyBundle.building-$PID"

try {
    & git -C $projectRoot archive --format=zip --output=$temporarySource $commitHash
    if ($LASTEXITCODE -ne 0) { throw 'Creating the tracked-source archive failed.' }

    $bundleBranch = (& git -C $projectRoot branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $bundleBranch) {
        throw 'Creating a history bundle requires a checked-out branch.'
    }
    & git -C $projectRoot bundle create $temporaryBundle $bundleBranch
    if ($LASTEXITCODE -ne 0) { throw 'Creating the Git history bundle failed.' }
    & git -C $projectRoot bundle verify $temporaryBundle | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Verifying the Git history bundle failed.' }

    Move-Item -LiteralPath $temporarySource -Destination $sourceArchive -Force
    Move-Item -LiteralPath $temporaryBundle -Destination $historyBundle -Force
} finally {
    if (Test-Path -LiteralPath $temporarySource) {
        Remove-Item -LiteralPath $temporarySource -Force
    }
    if (Test-Path -LiteralPath $temporaryBundle) {
        Remove-Item -LiteralPath $temporaryBundle -Force
    }
}

Write-Output "Archived $version at $commitHash"
Write-Output $sourceArchive
Write-Output $historyBundle
