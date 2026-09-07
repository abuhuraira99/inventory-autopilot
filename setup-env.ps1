<#
.SYNOPSIS
    Write the .env file, once, on a new Windows server.

.DESCRIPTION
    The Windows equivalent of setup-env.sh. Same job, same guarantees.

    The .env file has twenty-odd fields. Three are random keys that must be
    generated correctly, and one is a password that has to match exactly in two
    places. Hand-editing that in Notepad is the most error-prone step in the
    whole deployment, and the failure modes are badly proportioned to the
    mistake: a wrong character in DATABASE_URL is an unhelpful connection error
    today, while a wrong MASTER_KEY is credentials that cannot be decrypted,
    discovered weeks later.

    So this generates the three keys, asks for the four values only you know,
    and writes the file.

    THE THREE REAL SECRETS ARE NOT ASKED FOR, ON PURPOSE.
    The vendor FTP password, the Amazon Client Secret and the Amazon Refresh
    Token are left blank and typed into the dashboard afterwards, so they are
    encrypted at rest from the moment they arrive and never sit in a plain-text
    file on the server at all.

.EXAMPLE
    .\setup-env.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step { param([string]$Text) Write-Host "`n$Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host $Text -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host $Text -ForegroundColor Yellow }
function Stop-With  {
    param([string]$Text)
    Write-Host ""
    Write-Host "Stopped: $Text" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Refuse to destroy an existing .env
# ---------------------------------------------------------------------------
# The most important block in this file. MASTER_KEY is the only thing that can
# decrypt the stored Amazon and vendor credentials. Overwriting it with a fresh
# one does not "reset" anything -- it makes the saved credentials permanently
# unreadable, and the only recovery is to re-enter all three by hand. A setup
# script that clobbers .env on a second run is a trap laid for whoever runs it
# while debugging something else.
if (Test-Path -LiteralPath '.env') {
    Write-Host ""
    Write-Warn ".env already exists, so nothing has been changed."
    Write-Host ""
    Write-Host "That file holds MASTER_KEY, which is the only thing that can decrypt the"
    Write-Host "saved Amazon and vendor credentials. Replacing it would not reset them --"
    Write-Host "it would make them permanently unreadable."
    Write-Host ""
    Write-Host "  to look at it            notepad .env"
    Write-Host "  to change one value      notepad .env    (then restart the service)"
    Write-Host "  to genuinely start over  Rename-Item .env .env.old    then run this again"
    Write-Host ""
    exit 0
}

if (-not (Test-Path -LiteralPath 'app\config.py')) {
    Stop-With "run this from inside the inventory-autopilot folder"
}

# ---------------------------------------------------------------------------
# The three generated keys
# ---------------------------------------------------------------------------
# RandomNumberGenerator, not Get-Random. Get-Random is a seeded pseudo-random
# generator intended for sampling, not for key material.
function New-RandomBytes {
    param([int]$Count)
    $bytes = [byte[]]::new($Count)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

# 32 bytes, base64. Encrypts the stored credentials (AES-256-GCM).
$MasterKey = [Convert]::ToBase64String((New-RandomBytes -Count 32))

# Signs the dashboard login cookie. Changing it signs everybody out, which is a
# feature after a suspected leak.
$SessionSecret = -join ((New-RandomBytes -Count 32) | ForEach-Object { $_.ToString('x2') })

# Hex on purpose: this value is substituted into DATABASE_URL, and a password
# containing : / @ or ? would produce a URL that parses wrongly rather than
# failing outright, which is a worse outcome than either.
$PgPassword = -join ((New-RandomBytes -Count 24) | ForEach-Object { $_.ToString('x2') })

# ---------------------------------------------------------------------------
# The four values only you have
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Setting up the .env file" -ForegroundColor White -BackgroundColor DarkBlue
Write-Host ""
Write-Host "Four questions. All four answers are in SETUP.md - copy and paste them."
Write-Host "None of them is secret; the three real secrets are typed into the dashboard later."

function Read-Required {
    param([string]$Prompt, [string]$Example)
    while ($true) {
        Write-Step $Prompt
        Write-Host "  looks like: $Example" -ForegroundColor DarkGray
        $value = (Read-Host '  >')
        # Whitespace is stripped because these get pasted, and a trailing space
        # in a hostname or a Client ID produces a failure much later that looks
        # nothing like a typo.
        if ($null -ne $value) { $value = $value -replace '\s', '' }
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Warn "  That cannot be blank."
            continue
        }
        return $value
    }
}

$FtpHost  = Read-Required '1 of 4  The vendor''s FTP address'  'ftp.something.com'
$FtpUser  = Read-Required '2 of 4  The vendor''s FTP username' '110721B2CFTP'
$ClientId = Read-Required '3 of 4  The Amazon Client ID'       'amzn1.application-oa2-client.<32 characters>'
$SellerId = Read-Required '4 of 4  The Amazon Seller ID (Merchant Token)' 'A1B2C3D4E5F6G7'

# Catch the two confusions that actually happen, before they become a
# mysterious 400 from Amazon three steps later.
if ($ClientId -like 'amzn1.sp.solution.*') {
    Stop-With @"
that is the Application ID, not the Client ID.
    The Client ID starts with 'amzn1.application-oa2-client.' and is on the
    same Seller Central screen. Nothing has been written.
"@
}
if ($ClientId -like 'amzn1.oa2-cs.*') {
    Stop-With @"
that is the Client SECRET.
    It does not go in this file at all -- it is typed into the dashboard later so
    it can be encrypted. The Client ID starts with
    'amzn1.application-oa2-client.'. Nothing has been written.
"@
}
if ($ClientId -notlike 'amzn1.application-oa2-client.*') {
    Write-Warn "  Warning: a Client ID normally starts with 'amzn1.application-oa2-client.'"
    Write-Warn "  Carrying on, but check it if Amazon later says invalid_client."
}

# ---------------------------------------------------------------------------
# Write it
# ---------------------------------------------------------------------------
$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')

$content = @"
# =============================================================================
# Inventory Autopilot - server configuration
# =============================================================================
# Written by setup-env.ps1 on $stamp UTC.
#
# KEEP THIS FILE. MASTER_KEY below is the only thing that can decrypt the
# stored Amazon and vendor credentials. If you lose it, those three secrets
# must be re-entered by hand. Back it up somewhere that is NOT alongside a
# database backup -- one leaked backup should not be enough to read the other.
# =============================================================================

# --- generated keys: do not edit, do not share -------------------------------
MASTER_KEY=$MasterKey
SESSION_SECRET=$SessionSecret

# --- database ----------------------------------------------------------------
# PostgreSQL running as a Windows service on this machine, listening only on
# localhost. Create the role and database with the commands in SETUP.md.
POSTGRES_PASSWORD=$PgPassword
POSTGRES_USER=autopilot
POSTGRES_DB=autopilot
DATABASE_URL=postgresql+psycopg://autopilot:$PgPassword@127.0.0.1:5432/autopilot

# --- the vendor's file server ------------------------------------------------
VENDOR_FTP_HOST=$FtpHost
VENDOR_FTP_PORT=21
VENDOR_FTP_USER=$FtpUser
# ftps = explicit TLS on port 21, which is what this vendor provides. Plain
# 'ftp' would send the password in clear text and the app refuses to start.
VENDOR_FTP_MODE=ftps
VENDOR_FTP_PATH=/
# Blank on purpose. Typed into the dashboard so it is encrypted at rest.
VENDOR_FTP_PASSWORD=

# --- Amazon ------------------------------------------------------------------
LWA_CLIENT_ID=$ClientId
SELLER_ID=$SellerId
MARKETPLACE_ID=ATVPDKIKX0DER
SP_API_ENDPOINT=https://sellingpartnerapi-na.amazon.com
# Both blank on purpose. Typed into the dashboard so they are encrypted at rest.
LWA_CLIENT_SECRET=
LWA_REFRESH_TOKEN=

# --- application -------------------------------------------------------------
ENVIRONMENT=production
DEBUG=false
# Left as localhost because the dashboard is only served on 127.0.0.1 and is
# reached from the server's own browser over Remote Desktop. If you ever give it
# a real public address it must be https:// -- the app refuses to start on plain
# http to a non-local host, because the login cookie would cross the network in
# the clear.
BASE_URL=http://localhost:8000
ENABLE_SCHEDULER=true
LOG_LEVEL=INFO
LOG_JSON=true
"@

# UTF8 without a BOM. python-dotenv reads a BOM as part of the first key name,
# so "MASTER_KEY" would silently become "﻿MASTER_KEY" and the app would
# report the key as missing -- with a perfectly correct-looking file on screen.
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot '.env'),
    $content,
    (New-Object System.Text.UTF8Encoding($false))
)

# ---------------------------------------------------------------------------
# Lock the file down
# ---------------------------------------------------------------------------
# Windows has no chmod. The equivalent is to strip inherited permissions and
# grant only SYSTEM and the local Administrators group. Without this the file
# is readable by every user on the machine, and it contains MASTER_KEY.
try {
    $path = Join-Path $PSScriptRoot '.env'
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)   # stop inheriting, drop inherited rules

    # SYSTEM        - the Windows service runs as LocalSystem and must read this
    # Administrators- so it can be recovered
    # the current user - so YOU can read and edit it without elevating.
    #
    # That third entry is not laziness. Without it, viewing your own MASTER_KEY
    # or correcting a typo requires an elevated shell every time, and the first
    # thing that happens is somebody runs "icacls /grant Everyone:F" to make the
    # annoyance stop. A permission set that is usable is one that survives.
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    foreach ($who in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators', $me) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, 'FullControl', 'Allow')))
    }
    Set-Acl -LiteralPath $path -AclObject $acl
    $lockedDown = $true
} catch {
    $lockedDown = $false
}

Write-Host ""
Write-Ok "Written: .env"
Write-Host ""
Write-Host "  MASTER_KEY         generated (32 bytes, base64)"
Write-Host "  SESSION_SECRET     generated"
Write-Host "  POSTGRES_PASSWORD  generated"
Write-Host "  vendor + Amazon    as you entered them"
Write-Host "  the three secrets  left blank, for the dashboard"
Write-Host ""
if ($lockedDown) {
    Write-Ok "  Permissions restricted to SYSTEM, Administrators and $me."
    Write-Host "  Other users on this machine cannot read it." -ForegroundColor DarkGray
} else {
    Write-Warn "  Could not restrict the file permissions automatically."
    Write-Warn "  Run this as Administrator, or set them by hand: right-click .env ->"
    Write-Warn "  Properties -> Security, and remove Users."
}
Write-Host ""
Write-Host "Next: create the database role, then run the migrations. See SETUP.md." -ForegroundColor White
Write-Host ""
