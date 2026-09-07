<#
.SYNOPSIS
    Back up the database. Meant to be run nightly by Task Scheduler.

.DESCRIPTION
    WHY THIS MATTERS MORE THAN A USUAL BACKUP
    The database holds push_items -- the before-and-after quantity of every
    product this system has ever changed. That is what "Undo" reads.

    Everything else in the database can be rebuilt: vendor products come back
    with the next feed, Amazon listings with the next catalogue refresh, reports
    regenerate. The undo trail cannot be rebuilt from anything, by anyone.

    Keeps 30 days and deletes older files.

.EXAMPLE
    .\backup.ps1
#>

[CmdletBinding()]
param(
    [int]$KeepDays = 30
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

# Read the credentials out of .env rather than duplicating them here, so there
# is exactly one place a password is written down.
$envPath = Join-Path $RepoRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) { throw ".env not found in $RepoRoot" }

$envMap = @{}
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
    $t = $line.Trim()
    if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
        $k, $v = $t.Split('=', 2)
        $envMap[$k.Trim()] = $v.Trim()
    }
}
$pgUser = if ($envMap['POSTGRES_USER']) { $envMap['POSTGRES_USER'] } else { 'autopilot' }
$pgDb   = if ($envMap['POSTGRES_DB'])   { $envMap['POSTGRES_DB'] }   else { 'autopilot' }

# The PostgreSQL installer does not add itself to PATH by default, so look for
# it rather than assuming. Newest version wins.
$pgDump = (Get-Command pg_dump -ErrorAction SilentlyContinue).Source
if (-not $pgDump) {
    $pgDump = (Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\pg_dump.exe' -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1).FullName
}
if (-not $pgDump) { throw "pg_dump not found. Add C:\Program Files\PostgreSQL\<version>\bin to PATH." }

$dir = Join-Path $RepoRoot 'data\backups'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$sql   = Join-Path $dir "db-$stamp.sql"
$zip   = "$sql.zip"

$env:PGPASSWORD = $envMap['POSTGRES_PASSWORD']
try {
    & $pgDump -U $pgUser -h 127.0.0.1 -d $pgDb -f $sql
    if ($LASTEXITCODE -ne 0) { throw "pg_dump exited with $LASTEXITCODE" }
} finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

Compress-Archive -Path $sql -DestinationPath $zip -Force
Remove-Item $sql

# Restrict the archive the same way .env is restricted. A database dump
# contains the encrypted credentials; the ciphertext is useless without
# MASTER_KEY, but there is no reason to leave it world-readable.
try {
    $acl = Get-Acl -LiteralPath $zip
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($who in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators') {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, 'FullControl', 'Allow')))
    }
    Set-Acl -LiteralPath $zip -AclObject $acl
} catch {
    Write-Warning "could not restrict permissions on $zip : $_"
}

$size = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Output "$(Get-Date -Format s)  wrote $zip ($size MB)"

# Prune. Only files this script created, matched by name, so nothing else in
# the folder can be caught by it.
Get-ChildItem $dir -Filter 'db-*.sql.zip' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    ForEach-Object {
        Write-Output "$(Get-Date -Format s)  pruning $($_.Name)"
        Remove-Item $_.FullName -Force
    }
