#!/usr/bin/env bash
#
# Write the .env file, once, on a new server.
#
#   ./setup-env.sh
#
# WHY THIS EXISTS
# The .env file has twenty-odd fields. Three of them are random keys that must
# be generated correctly, one of them repeats a password that has to match
# exactly in two places, and hand-editing all of that in a terminal editor is
# the single most error-prone step in the whole deployment. A mistake in the
# database URL shows up as an unhelpful connection error; a mistake in
# MASTER_KEY shows up weeks later as credentials that cannot be decrypted.
#
# So this script generates the three keys, asks for the four values only you
# know, and writes the file. It asks nothing that can be derived and nothing
# that can be guessed.
#
# THE THREE SECRETS ARE NOT ASKED FOR, ON PURPOSE
# The vendor FTP password, the Amazon Client Secret and the Amazon Refresh
# Token are left blank here and typed into the dashboard afterwards. That way
# they are encrypted at rest from the moment they arrive and never sit in a
# plain-text file on the server at all.

set -euo pipefail

# --------------------------------------------------------------------------
# The scripts live in scripts/ but operate on the repository root, so every
# path below is resolved from the parent of this file's directory rather than
# from the directory itself. Getting this wrong is quiet and nasty: setup-env
# would write scripts/.env, the app would report MASTER_KEY as missing, and the
# file on screen would look perfectly correct.
# --------------------------------------------------------------------------
cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; OFF=$'\033[0m'

die() { echo; echo "${RED}Stopped: $*${OFF}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Refuse to destroy an existing .env
# ---------------------------------------------------------------------------
# This is the most important line in the file. MASTER_KEY is the only thing
# that can decrypt the stored Amazon and vendor credentials. Overwriting it
# with a fresh one does not "reset" anything -- it makes the saved credentials
# permanently unreadable, and the only recovery is to re-enter all three by
# hand. A setup script that clobbers .env on a second run is a trap.
if [ -f .env ]; then
    echo
    echo "${YELLOW}.env already exists, so nothing has been changed.${OFF}"
    echo
    echo "That file holds MASTER_KEY, which is the only thing that can decrypt the"
    echo "saved Amazon and vendor credentials. Replacing it would not reset them -- it"
    echo "would make them permanently unreadable."
    echo
    echo "  to look at it            cat .env"
    echo "  to change one value      nano .env    (then: docker compose up -d)"
    echo "  to genuinely start over  mv .env .env.old    then run this again"
    echo
    exit 0
fi

[ -f docker-compose.yml ] || die "run this from inside the inventory-autopilot folder"
command -v openssl >/dev/null || die "openssl is not installed. Run: sudo apt install -y openssl"

# ---------------------------------------------------------------------------
# The three generated keys
# ---------------------------------------------------------------------------
# MASTER_KEY       32 random bytes, base64. Encrypts the stored credentials.
# SESSION_SECRET   signs the dashboard login cookie. Changing it signs
#                  everybody out, which is a feature after a suspected leak.
# POSTGRES_PASSWORD  hex on purpose: this value is substituted into
#                  DATABASE_URL, and a password containing : / @ or ? would
#                  produce a URL that parses wrongly rather than failing.
MASTER_KEY=$(openssl rand -base64 32)
SESSION_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 24)

# ---------------------------------------------------------------------------
# The four values only you have
# ---------------------------------------------------------------------------
echo
echo "${BOLD}Setting up the .env file${OFF}"
echo
echo "Four questions. All four answers are in SETUP.md, Step 4 -- copy and paste them."
echo "None of them is secret; the three real secrets are typed into the dashboard later."
echo

ask() {                      # ask <prompt> <example> <varname>
    local prompt="$1" example="$2" __var="$3" value=""
    while [ -z "$value" ]; do
        echo "${BLUE}$prompt${OFF}"
        echo "  looks like: $example"
        printf "  > "
        # Plain stdin rather than /dev/tty, so the answers can also be piped
        # in -- which is how this is tested. A failed read means end of input.
        if ! read -r value; then
            echo
            die "ran out of input. Run ./setup-env.sh again and answer all four."
        fi
        # Whitespace is stripped because these get pasted, and a trailing space
        # in a hostname or a Client ID produces a failure much later that looks
        # nothing like a typo.
        value="$(printf '%s' "$value" | tr -d '[:space:]')"
        [ -z "$value" ] && echo "  ${YELLOW}That cannot be blank.${OFF}"
        echo
    done
    eval "$__var=\$value"
}

ask "1 of 4  The vendor's FTP address" "ftp.something.com" VENDOR_FTP_HOST
ask "2 of 4  The vendor's FTP username" "110721B2CFTP" VENDOR_FTP_USER
ask "3 of 4  The Amazon Client ID" "amzn1.application-oa2-client.<32 characters>" LWA_CLIENT_ID
ask "4 of 4  The Amazon Seller ID (Merchant Token)" "A1B2C3D4E5F6G7" SELLER_ID

# Catch the two confusions that actually happen, before they become a
# mysterious 400 from Amazon three steps later.
case "$LWA_CLIENT_ID" in
    amzn1.application-oa2-client.*) ;;
    amzn1.sp.solution.*)
        die "that is the Application ID, not the Client ID. The Client ID starts with
    'amzn1.application-oa2-client.' and is on the same Seller Central screen." ;;
    amzn1.oa2-cs.*)
        die "that is the Client SECRET. It does not go in this file at all -- it is
    typed into the dashboard later so it can be encrypted. The Client ID starts
    with 'amzn1.application-oa2-client.'" ;;
    *)
        echo "${YELLOW}  Warning: a Client ID normally starts with 'amzn1.application-oa2-client.'${OFF}"
        echo "${YELLOW}  Carrying on, but check it if Amazon later says invalid_client.${OFF}"
        echo ;;
esac

# ---------------------------------------------------------------------------
# Write it
# ---------------------------------------------------------------------------
umask 077          # so the file is 600 from the moment it exists, not after
cat > .env <<ENV
# =============================================================================
# Inventory Autopilot - server configuration
# =============================================================================
# Written by setup-env.sh on $(date -u '+%Y-%m-%d %H:%M UTC').
#
# KEEP THIS FILE. MASTER_KEY below is the only thing that can decrypt the
# stored Amazon and vendor credentials. If you lose it, those three secrets
# must be re-entered by hand. Back it up somewhere that is NOT alongside a
# database backup -- one leaked backup should not be enough to read the other.
# =============================================================================

# --- generated keys: do not edit, do not share -------------------------------
MASTER_KEY=$MASTER_KEY
SESSION_SECRET=$SESSION_SECRET

# --- database (runs in Docker on this machine, not exposed to the network) ---
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_USER=autopilot
POSTGRES_DB=autopilot
DATABASE_URL=postgresql+psycopg://autopilot:$POSTGRES_PASSWORD@db:5432/autopilot

# --- the vendor's file server ------------------------------------------------
VENDOR_FTP_HOST=$VENDOR_FTP_HOST
VENDOR_FTP_PORT=21
VENDOR_FTP_USER=$VENDOR_FTP_USER
# ftps = explicit TLS on port 21, which is what this vendor provides. Plain
# 'ftp' would send the password in clear text and the app refuses to start.
VENDOR_FTP_MODE=ftps
VENDOR_FTP_PATH=/
# Blank on purpose. Typed into the dashboard so it is encrypted at rest.
VENDOR_FTP_PASSWORD=

# --- Amazon ------------------------------------------------------------------
LWA_CLIENT_ID=$LWA_CLIENT_ID
SELLER_ID=$SELLER_ID
MARKETPLACE_ID=ATVPDKIKX0DER
SP_API_ENDPOINT=https://sellingpartnerapi-na.amazon.com
# Both blank on purpose. Typed into the dashboard so they are encrypted at rest.
LWA_CLIENT_SECRET=
LWA_REFRESH_TOKEN=

# --- application -------------------------------------------------------------
ENVIRONMENT=production
DEBUG=false
# Left as localhost because the dashboard is reached through an SSH tunnel or a
# Cloudflare Tunnel rather than being published. If you ever give it a real
# public address it must be https:// -- the app refuses to start on plain http
# to a non-local host, because the login cookie would cross the network in the
# clear.
BASE_URL=http://localhost:8000
ENABLE_SCHEDULER=true
LOG_LEVEL=INFO
LOG_JSON=true
ENV
chmod 600 .env

echo "${GREEN}Written: .env${OFF}  (permissions 600 - only you can read it)"
echo
echo "  MASTER_KEY         generated"
echo "  SESSION_SECRET     generated"
echo "  POSTGRES_PASSWORD  generated"
echo "  vendor + Amazon    as you entered them"
echo "  the three secrets  left blank, for the dashboard"
echo
echo "${BOLD}Next:${OFF}  docker compose up -d --build"
echo
