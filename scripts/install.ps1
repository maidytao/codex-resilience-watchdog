[CmdletBinding()]
param(
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [switch]$DryRun,
    [switch]$Enable,
    [switch]$Force
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

function Get-TreeHashes([string[]]$Roots) {
    $items = @()
    foreach ($root in $Roots) {
        Get-ChildItem -LiteralPath $root -File -Recurse | Sort-Object FullName | ForEach-Object {
            $stream = [IO.File]::OpenRead($_.FullName)
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $hash = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
            }
            finally {
                $sha.Dispose()
                $stream.Dispose()
            }
            $items += [ordered]@{
                path = $_.FullName
                sha256 = $hash
            }
        }
    }
    return $items
}

function Write-JsonAtomic([object]$Value, [string]$Path) {
    $temporary = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-StartupName([string]$CodexHomePath) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($CodexHomePath.ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").Substring(0, 10)
    }
    finally {
        $sha.Dispose()
    }
    return "CodexResilienceWatchdog-$digest"
}

function Register-WatchdogStartup(
    [string]$Name,
    [string]$Python,
    [string]$Runtime,
    [string]$CodexHomePath
) {
    $daemonPython = $Python
    $pythonw = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
    if (Test-Path -LiteralPath $pythonw -PathType Leaf) {
        $daemonPython = $pythonw
    }
    $arguments = ('"{0}" --home "{1}" daemon' -f $Runtime, $CodexHomePath)

    try {
        $action = New-ScheduledTaskAction -Execute $daemonPython -Argument $arguments -WorkingDirectory (Split-Path -Parent $Runtime)
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Description "Bounded recovery monitor for Codex tasks" -Force | Out-Null
        return [ordered]@{ method = "scheduled-task"; identifier = $Name; python = $daemonPython }
    }
    catch {
        $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        if (-not (Test-Path -LiteralPath $runKey)) {
            New-Item -Path $runKey -Force | Out-Null
        }
        $command = ('"{0}" {1}' -f $daemonPython, $arguments)
        New-ItemProperty -LiteralPath $runKey -Name $Name -Value $command -PropertyType String -Force | Out-Null
        return [ordered]@{ method = "hkcu-run"; identifier = $Name; python = $daemonPython }
    }
}

$homePath = Get-FullPath $CodexHome
$sourceRoot = Split-Path -Parent $PSScriptRoot
$sourcePackage = Join-Path $sourceRoot "src"
$sourceRuntime = Join-Path $sourceRoot "scripts\runtime_entry.py"
$sourceSkill = Join-Path $sourceRoot "skills\codex-resilience-watchdog"
$dataDir = Assert-Within (Join-Path $homePath "watchdog") $homePath
$appDir = Assert-Within (Join-Path $dataDir "app") $dataDir
$skillDir = Assert-Within (Join-Path $homePath "skills\codex-resilience-watchdog") $homePath
$manifestPath = Assert-Within (Join-Path $dataDir "install-manifest.json") $dataDir

foreach ($required in @($sourcePackage, $sourceRuntime, $sourceSkill)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required source is missing: $required"
    }
}

$pythonCommand = Get-Command python -ErrorAction Stop
$codexCommand = Get-Command codex -ErrorAction Stop
$resumeHelp = & $codexCommand.Source exec resume --help 2>&1
if ($LASTEXITCODE -ne 0 -or (($resumeHelp -join "`n") -notmatch "SESSION_ID")) {
    throw "Installed Codex CLI does not support exec resume"
}

if ($DryRun) {
    [ordered]@{
        action = "dry-run"
        home = $homePath
        app = $appDir
        skill = $skillDir
        enable = [bool]$Enable
    } | ConvertTo-Json -Depth 4
    exit 0
}

if ((Test-Path -LiteralPath $manifestPath) -and (-not $Force)) {
    throw "An installation manifest already exists. Use -Force to replace this installation."
}

$backupRoot = Assert-Within (Join-Path $dataDir "backups") $dataDir
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

if (Test-Path -LiteralPath $appDir) {
    $appBackup = Assert-Within (Join-Path $backupRoot "app-$stamp") $backupRoot
    Move-Item -LiteralPath $appDir -Destination $appBackup
}
if (Test-Path -LiteralPath $skillDir) {
    $skillBackup = Assert-Within (Join-Path $backupRoot "skill-$stamp") $backupRoot
    Move-Item -LiteralPath $skillDir -Destination $skillBackup
}

New-Item -ItemType Directory -Path $appDir -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $skillDir) -Force | Out-Null
Copy-Item -LiteralPath $sourcePackage -Destination (Join-Path $appDir "src") -Recurse -Force
Copy-Item -LiteralPath $sourceRuntime -Destination (Join-Path $appDir "runtime_entry.py") -Force
Copy-Item -LiteralPath $sourceSkill -Destination $skillDir -Recurse -Force

$runtime = Join-Path $appDir "runtime_entry.py"
& $pythonCommand.Source $runtime --home $homePath --json status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Installed runtime failed its status check"
}

$startup = [ordered]@{ method = "none"; identifier = $null; python = $pythonCommand.Source }
if ($Enable) {
    & $pythonCommand.Source $runtime --home $homePath --json enable | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed runtime could not be enabled"
    }
    $startupName = Get-StartupName $homePath
    $startup = Register-WatchdogStartup $startupName $pythonCommand.Source $runtime $homePath
}

$manifest = [ordered]@{
    schema = 1
    installedAtUtc = [DateTime]::UtcNow.ToString("o")
    codexHome = $homePath
    interpreter = $pythonCommand.Source
    codexCli = $codexCommand.Source
    installedRoots = @($appDir, $skillDir)
    startup = $startup
    files = @(Get-TreeHashes @($appDir, $skillDir))
}
Write-JsonAtomic $manifest $manifestPath

if ($Enable) {
    if ($startup.method -eq "scheduled-task") {
        Start-ScheduledTask -TaskName $startup.identifier
    }
    else {
        $arguments = @('"' + $runtime + '"', "--home", '"' + $homePath + '"', "daemon")
        Start-Process -FilePath $startup.python -ArgumentList $arguments -WindowStyle Hidden
    }
}

[ordered]@{
    action = "installed"
    home = $homePath
    app = $appDir
    skill = $skillDir
    manifest = $manifestPath
    startup = $startup
} | ConvertTo-Json -Depth 6
