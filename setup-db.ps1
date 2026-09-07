<#
.SYNOPSIS
    Create the PostgreSQL role and database, using the password already in .env.

.DESCRIPTION
    WHY THIS EXISTS
    setup-env.ps1 generates a random POSTGRES_PASSWORD and writes it into .env,
    in two places -- once on its own line and once inside DATABASE_URL. The
    PostgreSQL role has to be created with that same password.

    Doing that by hand means opening .env, copying a 48-character hex string,
    and pasting it into a psql command without picking up a stray space. Get it
    wrong and the app fails with "password authentication failed for user
    autopilot", which tells you nothing about which of the two values is wrong.

    So this reads the password out of .env and creates the role with it. There
    is no copying and no opportunity to mistype.

    It is safe to run twice: it checks whether the role and database exist and
    only creates what is missing.

.EXAMPLE
    .\setup-db.ps1
#>

[CmdletBinding()]
param(
    [string]$SuperUser = 'postgres',
    [string]$PgHost = '127.0.0.1',
    [int]$Port = 5432
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Ok   { param([string]$T) Write-Host "  ok  $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "  !   $T" -ForegroundColor Yellow }
function Stop-With  {
    param([string]$T)
    Write-Host ""
    Write-Host "Stopped: $T" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Read what setup-env.ps1 generated
# ---------------------------------------------------------------------------
$envPath = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Stop-With ".env not found. Run .\setup-env.ps1 first."
}

$envMap = @{}
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
        $k, $v = $t.Split('=', 2)
        $envMap[$k.Trim()] = $v.Trim()
    }
}

$role = if ($envMap['POSTGRES_USER']) { $envMap['POSTGRES_USER'] } else { 'autopilot' }
$db   = if ($envMap['POSTGRES_DB'])   { $envMap['POSTGRES_DB'] }   else { 'autopilot' }
$pass = $envMap['POSTGRES_PASSWORD']

if ([string]::IsNullOrWhiteSpace($pass)) {
    Stop-With "POSTGRES_PASSWORD is empty in .env. Delete .env and run .\setup-env.ps1 again."
}

# ---------------------------------------------------------------------------
# Find psql
# ---------------------------------------------------------------------------
# The PostgreSQL installer does not add itself to PATH by default, which is the
# first thing that goes wrong on a fresh Windows install.
$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
    $psql = (Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1).FullName
}
if (-not $psql) {
    Stop-With @"
psql.exe was not found.
    Is PostgreSQL installed? It normally lives in
      C:\Program Files\PostgreSQL\16\bin
    If it is installed, add that folder to PATH and open a new PowerShell.
"@
}
Write-Ok "using $psql"

# ---------------------------------------------------------------------------
# The superuser password, which only you know
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Enter the password you chose for the PostgreSQL '$SuperUser' account" -ForegroundColor Cyan
Write-Host "during the PostgreSQL installer. It is not stored anywhere by this script." -ForegroundColor DarkGray
$secure = Read-Host '  >' -AsSecureString
$superPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))

function Invoke-Psql {
    param([string]$Sql, [string]$Database = 'postgres')
    $env:PGPASSWORD = $superPass
    try {
        $out = & $psql -U $SuperUser -h $PgHost -p $Port -d $Database -t -A -c $Sql 2>&1
        return @{ ExitCode = $LASTEXITCODE; Output = ($out -join "`n").Trim() }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

$probe = Invoke-Psql -Sql 'SELECT 1'
if ($probe.ExitCode -ne 0) {
    Stop-With @"
could not connect to PostgreSQL as '$SuperUser'.

    $($probe.Output)

    Most likely: the password is wrong, or the PostgreSQL service is not
    running. Check it with:
      Get-Service postgresql*
"@
}
Write-Ok "connected to PostgreSQL"

# ---------------------------------------------------------------------------
# Create the role
# ---------------------------------------------------------------------------
# The password is passed as a quoted literal. It is generated hex, so it cannot
# contain a quote -- but it is escaped anyway rather than relying on that,
# because the day somebody edits it by hand is the day it matters.
$escaped = $pass.Replace("'", "''")

$exists = Invoke-Psql -Sql "SELECT 1 FROM pg_roles WHERE rolname = '$role'"
if ($exists.Output -eq '1') {
    # Re-set the password rather than leaving a mismatch in place. This is the
    # whole point of the script: .env is the single source of truth, so the
    # role is made to agree with it.
    $r = Invoke-Psql -Sql "ALTER ROLE `"$role`" WITH LOGIN PASSWORD '$escaped'"
    if ($r.ExitCode -ne 0) { Stop-With "could not update the role: $($r.Output)" }
    Write-Ok "role '$role' already existed; its password now matches .env"
} else {
    $r = Invoke-Psql -Sql "CREATE ROLE `"$role`" WITH LOGIN PASSWORD '$escaped'"
    if ($r.ExitCode -ne 0) { Stop-With "could not create the role: $($r.Output)" }
    Write-Ok "created role '$role'"
}

# ---------------------------------------------------------------------------
# Create the database
# ---------------------------------------------------------------------------
$dbExists = Invoke-Psql -Sql "SELECT 1 FROM pg_database WHERE datname = '$db'"
if ($dbExists.Output -eq '1') {
    Write-Ok "database '$db' already exists"
} else {
    # CREATE DATABASE cannot run inside a transaction block, which is why this
    # is a separate call rather than part of a script.
    $r = Invoke-Psql -Sql "CREATE DATABASE `"$db`" OWNER `"$role`" ENCODING 'UTF8'"
    if ($r.ExitCode -ne 0) { Stop-With "could not create the database: $($r.Output)" }
    Write-Ok "created database '$db' owned by '$role'"
}

# Owner already implies this, but an explicit grant makes the intent visible
# and survives somebody changing the owner later.
$r = Invoke-Psql -Sql "GRANT ALL PRIVILEGES ON DATABASE `"$db`" TO `"$role`""
if ($r.ExitCode -ne 0) { Write-Warn "grant returned: $($r.Output)" }

# The public schema is not writable by a non-owner from PostgreSQL 15 onwards,
# which is a change that catches people out. Granted explicitly.
$r = Invoke-Psql -Sql "GRANT ALL ON SCHEMA public TO `"$role`"" -Database $db
if ($r.ExitCode -ne 0) { Write-Warn "schema grant returned: $($r.Output)" }

# ---------------------------------------------------------------------------
# Prove it works as the application will connect
# ---------------------------------------------------------------------------
$env:PGPASSWORD = $pass
try {
    $check = & $psql -U $role -h $PgHost -p $Port -d $db -t -A -c 'SELECT current_user, current_database()' 2>&1
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

Write-Host ""
if ($code -eq 0) {
    Write-Host "Verified: logged in as $($check -join '') using the password from .env." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next: .\.venv\Scripts\python.exe -m alembic upgrade head" -ForegroundColor White
} else {
    Stop-With @"
the role was created, but logging in with the password from .env failed:

    $($check -join "`n")

    That usually means pg_hba.conf is set to 'trust' or 'ident' for local
    connections. Find it under C:\Program Files\PostgreSQL\<version>\data,
    set the host lines for 127.0.0.1 to 'scram-sha-256', then run:
      Restart-Service postgresql*
"@
}
Write-Host ""
