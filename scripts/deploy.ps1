<#
.SYNOPSIS
    Update a running Windows deployment to the latest code on GitHub.

.DESCRIPTION
    The Windows equivalent of deploy.sh, for a native (non-Docker) install.

    IN ORDER
      1. backs up the database FIRST, before anything else can go wrong
      2. refuses if there are uncommitted changes on the server
      3. pulls the new code
      4. updates the Python dependencies
      5. applies any database migrations
      6. restarts the service and waits for the health check
      7. if the health check fails, prints exactly how to roll back

    WHY A SCRIPT RATHER THAN A LIST OF COMMANDS
    Because step 1 is the one a human skips when they are in a hurry, and it is
    the only one that cannot be undone. The database holds the undo trail -- the
    record of every quantity this system has changed and what it was before.
    That is the single thing here that cannot be rebuilt from the vendor's files
    or from Amazon.

    NOTHING HAPPENS AUTOMATICALLY. Pushing to GitHub does NOT change this
    server. Somebody has to run this. That is deliberate: an automatic deploy on
    a system that writes to a live Amazon account means a bad commit reaches
    production with no human in between.

.EXAMPLE
    .\deploy.ps1
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'InventoryAutopilot',
    [int]$HealthTimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
# --------------------------------------------------------------------------
# The scripts live in scripts/ but operate on the repository root, so every
# path below is resolved from the parent of this file's directory rather than
# from the directory itself. Getting this wrong is quiet and nasty: setup-env
# would write scripts/.env, the app would report MASTER_KEY as missing, and the
# file on screen would look perfectly correct.
# --------------------------------------------------------------------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

function Write-Step { param([string]$T) Write-Host "`n==> $T" -ForegroundColor Cyan }
function Write-Ok   { param([string]$T) Write-Host "    ok  $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "    !   $T" -ForegroundColor Yellow }
function Stop-With  {
    param([string]$T)
    Write-Host ""
    Write-Host "FAILED: $T" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath '.env')) { Stop-With ".env is missing. See SETUP.md." }
if (-not (Test-Path -LiteralPath 'app\config.py')) {
    Stop-With "run this from inside the inventory-autopilot folder"
}

# Read the database settings out of .env rather than asking for them again.
$envMap = @{}
foreach ($line in [System.IO.File]::ReadAllLines((Join-Path $RepoRoot '.env'))) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
        $k, $v = $t.Split('=', 2)
        $envMap[$k.Trim()] = $v.Trim()
    }
}
$pgUser = if ($envMap['POSTGRES_USER']) { $envMap['POSTGRES_USER'] } else { 'autopilot' }
$pgDb   = if ($envMap['POSTGRES_DB'])   { $envMap['POSTGRES_DB'] }   else { 'autopilot' }
$pgPass = $envMap['POSTGRES_PASSWORD']

$previousCommit = (& git rev-parse --short HEAD).Trim()

# ---------------------------------------------------------------------------
Write-Step "1/6  Backing up the database"
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path 'data\backups' | Out-Null
$stamp  = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$backup = "data\backups\pre-deploy-$stamp.sql"

$pgDump = Get-Command pg_dump -ErrorAction SilentlyContinue
if (-not $pgDump) {
    # The PostgreSQL installer does not add itself to PATH by default.
    $found = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\pg_dump.exe' -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $pgDump = $found.FullName }
} else {
    $pgDump = $pgDump.Source
}

if ($pgDump) {
    $env:PGPASSWORD = $pgPass       # so pg_dump does not prompt
    try {
        & $pgDump -U $pgUser -h 127.0.0.1 -d $pgDb -f $backup
        if ($LASTEXITCODE -ne 0) { Stop-With "pg_dump failed. Refusing to deploy without a backup." }
        Compress-Archive -Path $backup -DestinationPath "$backup.zip" -Force
        Remove-Item $backup
        $backup = "$backup.zip"
        $size = [math]::Round((Get-Item $backup).Length / 1MB, 1)
        Write-Ok "$backup ($size MB)"
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
} else {
    Stop-With @"
pg_dump was not found, so no backup could be taken.
    Refusing to deploy. The database holds the undo trail and it is the only
    thing here that cannot be rebuilt.
    Look for it under C:\Program Files\PostgreSQL\<version>\bin and add that
    folder to PATH.
"@
}

# ---------------------------------------------------------------------------
Write-Step "2/6  Checking for local changes"
# ---------------------------------------------------------------------------
& git diff --quiet
$dirty = ($LASTEXITCODE -ne 0)
& git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { $dirty = $true }
if ($dirty) {
    Stop-With @"
there are uncommitted local changes on the server.
    Run 'git status' to see them, then either commit them or run
    'git checkout -- .' to discard them. Refusing to overwrite work.
"@
}
Write-Ok "working tree is clean"

# ---------------------------------------------------------------------------
Write-Step "3/6  Fetching the new code"
# ---------------------------------------------------------------------------
& git fetch --quiet origin
$behind = [int](& git rev-list --count HEAD..origin/main).Trim()
if ($behind -eq 0) {
    Write-Ok "already up to date at $previousCommit - nothing to deploy"
    exit 0
}
Write-Host "    $behind new commit(s):"
& git log --oneline HEAD..origin/main | ForEach-Object { "      $_" }
& git merge --ff-only origin/main --quiet
if ($LASTEXITCODE -ne 0) { Stop-With "could not fast-forward. The server's branch has diverged." }
$newCommit = (& git rev-parse --short HEAD).Trim()
Write-Ok "$previousCommit -> $newCommit"

# ---------------------------------------------------------------------------
Write-Step "4/6  Updating dependencies"
# ---------------------------------------------------------------------------
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) { Stop-With "pip install failed. Nothing has been restarted." }
Write-Ok "dependencies up to date"

# ---------------------------------------------------------------------------
Write-Step "5/6  Applying database migrations"
# ---------------------------------------------------------------------------
& .\.venv\Scripts\python.exe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Stop-With "the migration failed. The service has NOT been restarted, so it is still running the old code against the old schema." }
Write-Ok "schema up to date"

# ---------------------------------------------------------------------------
Write-Step "6/6  Restarting and checking health"
# ---------------------------------------------------------------------------
# The app is supervised by a Scheduled Task, not a Windows Service, because
# that needs no third-party wrapper (see docs/WINDOWS-DEPLOYMENT.md). A Service
# is still handled, in case someone has set one up with NSSM.
#
# Getting this wrong is worse than it looks: an earlier version of this script
# only knew about Get-Service, so on a scheduled-task install it printed a
# warning and carried on -- reporting a successful deploy while the old code
# kept running. A deploy that does not restart anything is a deploy that lies.
$restarted = $false

$task = Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
if ($task) {
    # There is no Restart-ScheduledTask cmdlet. Stop, wait for it to actually
    # stop, then start.
    Stop-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if ((Get-ScheduledTask -TaskName $ServiceName).State -ne 'Running') { break }
        Start-Sleep -Milliseconds 500
    }
    Start-ScheduledTask -TaskName $ServiceName
    Write-Ok "scheduled task '$ServiceName' restarted"
    $restarted = $true
}

if (-not $restarted) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Restart-Service -Name $ServiceName -Force
        Write-Ok "service '$ServiceName' restarted"
        $restarted = $true
    }
}

if (-not $restarted) {
    Stop-With @"
found neither a scheduled task nor a service called '$ServiceName',
    so the new code has NOT been started and the old code is still running.

    The database backup and the migration have already been applied, so the
    schema is current -- nothing is broken, but the deploy is incomplete.

    Register the task as shown in docs/WINDOWS-DEPLOYMENT.md, then run this
    again. Or pass the right name: .\deploy.ps1 -ServiceName <name>
"@
}

Write-Host -NoNewline "    waiting for the health check"
$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 3
}
Write-Host ""

if ($healthy) {
    Write-Ok "healthy"
    Write-Host ""
    Write-Host "Deployed $newCommit successfully." -ForegroundColor Green
    Write-Host "Backup taken before the change: $backup"
    exit 0
}

Write-Host "The application did not come up healthy within $HealthTimeoutSeconds seconds." -ForegroundColor Red
Write-Host ""
Write-Host "Look at the log first:"
Write-Host "    Get-Content .\data\logs\service-error.log -Tail 60"
Write-Host ""
Write-Host "To roll back to the previous version:"
Write-Host "    git checkout $previousCommit"
Write-Host "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
Write-Host "    Restart-Service $ServiceName"
Write-Host ""
Write-Host "Do NOT run 'alembic downgrade'. Most releases add no migration, and a newer"
Write-Host "schema is harmless to older code here. Downgrading the first revision drops"
Write-Host "every table including the undo trail, and is guarded against for that reason."
Write-Host ""
Write-Host "To restore the database from the backup taken above:"
Write-Host "    Stop-Service $ServiceName"
Write-Host "    Expand-Archive $backup -DestinationPath .\data\backups\restore -Force"
Write-Host "    psql -U $pgUser -h 127.0.0.1 -d $pgDb -f .\data\backups\restore\<file>.sql"
Write-Host "    Start-Service $ServiceName"
exit 1
