[CmdletBinding()]
param(
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [switch]$DryRun,
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

function Get-FullPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw "CodexHome must be an absolute path"
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Assert-Within([string]$Path, [string]$Root) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $prefix = $fullRoot + [IO.Path]::DirectorySeparatorChar
    if (($fullPath -ne $fullRoot) -and (-not $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw "Path is outside the permitted root: $fullPath"
    }
    return $fullPath
}

$homePath = Get-FullPath $CodexHome
$dataDir = Assert-Within (Join-Path $homePath "watchdog") $homePath
$appDir = Assert-Within (Join-Path $dataDir "app") $dataDir
$skillDir = Assert-Within (Join-Path $homePath "skills\codex-resilience-watchdog") $homePath
$manifestPath = Assert-Within (Join-Path $dataDir "install-manifest.json") $dataDir
$runtime = Assert-Within (Join-Path $appDir "runtime_entry.py") $appDir
$manifest = $null

if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([IO.Path]::GetFullPath([string]$manifest.codexHome) -ne $homePath) {
        throw "The manifest belongs to a different Codex home"
    }
}

if ($DryRun) {
    [ordered]@{
        action = "dry-run"
        home = $homePath
        app = $appDir
        skill = $skillDir
        purgeData = [bool]$PurgeData
    } | ConvertTo-Json -Depth 4
    exit 0
}

if (Test-Path -LiteralPath $runtime -PathType Leaf) {
    $python = $null
    if ($manifest -and $manifest.interpreter -and (Test-Path -LiteralPath ([string]$manifest.interpreter) -PathType Leaf)) {
        $python = [string]$manifest.interpreter
    }
    else {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    & $python $runtime --home $homePath --json disable | Out-Null
}

$daemonLock = Assert-Within (Join-Path $dataDir "daemon.lock") $dataDir
for ($attempt = 0; $attempt -lt 30 -and (Test-Path -LiteralPath $daemonLock); $attempt++) {
    Start-Sleep -Milliseconds 100
}

if ($manifest -and $manifest.startup) {
    $method = [string]$manifest.startup.method
    $identifier = [string]$manifest.startup.identifier
    if ($method -eq "scheduled-task" -and $identifier) {
        $task = Get-ScheduledTask -TaskName $identifier -ErrorAction SilentlyContinue
        if ($task) {
            Stop-ScheduledTask -TaskName $identifier -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $identifier -Confirm:$false
        }
    }
    elseif ($method -eq "hkcu-run" -and $identifier) {
        $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        if (Test-Path -LiteralPath $runKey) {
            Remove-ItemProperty -LiteralPath $runKey -Name $identifier -ErrorAction SilentlyContinue
        }
    }
}

foreach ($target in @($appDir, $skillDir)) {
    $validated = if ($target -eq $appDir) { Assert-Within $target $dataDir } else { Assert-Within $target $homePath }
    if (Test-Path -LiteralPath $validated) {
        Remove-Item -LiteralPath $validated -Recurse -Force
    }
}

if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    Remove-Item -LiteralPath $manifestPath -Force
}

if ($PurgeData -and (Test-Path -LiteralPath $dataDir)) {
    $verifiedData = Assert-Within $dataDir $homePath
    if ([IO.Path]::GetFileName($verifiedData) -ne "watchdog") {
        throw "Refusing to purge an unexpected data directory"
    }
    Remove-Item -LiteralPath $verifiedData -Recurse -Force
}

[ordered]@{
    action = "uninstalled"
    home = $homePath
    dataPreserved = -not [bool]$PurgeData
} | ConvertTo-Json -Depth 4
