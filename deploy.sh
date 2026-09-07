#!/usr/bin/env bash
#
# Update a running deployment to the latest code on GitHub.
#
#   ./deploy.sh
#
# WHAT THIS DOES, IN ORDER
#   1. backs up the database FIRST, before anything else can go wrong
#   2. pulls the new code
#   3. rebuilds the container
#   4. applies any database migrations
#   5. restarts, and waits for the health check to pass
#   6. if the health check fails, tells you exactly how to roll back
#
# WHY A SCRIPT RATHER THAN A LIST OF COMMANDS
# Because step 1 is the one a human skips when they are in a hurry, and it is
# the only one that cannot be undone. The database holds the undo trail -- the
# record of every quantity this system has changed and what it was before. That
# is the single thing here that cannot be rebuilt from the vendor's files or
# from Amazon.
#
# NOTHING HAPPENS AUTOMATICALLY. Pushing to GitHub does NOT change the server.
# Somebody has to run this. That is deliberate: an automatic deploy on a system
# that writes to a live Amazon account means a bad commit reaches production
# with no human in between.

set -euo pipefail

cd "$(dirname "$0")"

BLUE=$'\033[0;34m'; GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[1;33m'; OFF=$'\033[0m'
step() { echo; echo "${BLUE}==> $*${OFF}"; }
ok()   { echo "${GREEN}    ok${OFF} $*"; }
warn() { echo "${YELLOW}    ! ${OFF} $*"; }
die()  { echo; echo "${RED}FAILED: $*${OFF}" >&2; exit 1; }

[ -f docker-compose.yml ] || die "run this from the inventory-autopilot directory"
[ -f .env ]               || die ".env is missing. See SETUP.md."

PREVIOUS_COMMIT=$(git rev-parse --short HEAD)

# -----------------------------------------------------------------------------
step "1/6  Backing up the database"
# -----------------------------------------------------------------------------
mkdir -p data/backups
STAMP=$(date -u +%Y%m%d-%H%M%S)
BACKUP="data/backups/pre-deploy-${STAMP}.sql.gz"

if docker compose ps db --status running >/dev/null 2>&1; then
    docker compose exec -T db pg_dump -U autopilot autopilot | gzip > "$BACKUP"
    chmod 600 "$BACKUP"
    ok "$BACKUP ($(du -h "$BACKUP" | cut -f1))"
else
    warn "the database container is not running, so there is nothing to back up"
fi

# -----------------------------------------------------------------------------
step "2/6  Pausing the sync"
# -----------------------------------------------------------------------------
# A run that is halfway through when the container restarts would leave a batch
# in an ambiguous state. The advisory lock makes that safe rather than
# corrupting anything, but waiting is tidier.
if docker compose ps app --status running >/dev/null 2>&1; then
    ok "the current run will finish; the container waits for it on shutdown"
else
    warn "the app container is not running"
fi

# -----------------------------------------------------------------------------
step "3/6  Fetching the new code"
# -----------------------------------------------------------------------------
if ! git diff --quiet || ! git diff --cached --quiet; then
    die "there are uncommitted local changes on the server.
    Run 'git status' to see them, then either commit them or run
    'git checkout -- .' to discard them. Refusing to overwrite work."
fi

git fetch --quiet origin
BEHIND=$(git rev-list --count HEAD..origin/main)
if [ "$BEHIND" -eq 0 ]; then
    ok "already up to date at $PREVIOUS_COMMIT — nothing to deploy"
    exit 0
fi

echo "    $BEHIND new commit(s):"
git log --oneline HEAD..origin/main | sed 's/^/      /'
git merge --ff-only origin/main --quiet
NEW_COMMIT=$(git rev-parse --short HEAD)
ok "$PREVIOUS_COMMIT -> $NEW_COMMIT"

# -----------------------------------------------------------------------------
step "4/6  Rebuilding"
# -----------------------------------------------------------------------------
docker compose build --quiet app
ok "image rebuilt"

# -----------------------------------------------------------------------------
step "5/6  Applying database migrations"
# -----------------------------------------------------------------------------
docker compose run --rm app alembic upgrade head
ok "schema up to date"

# -----------------------------------------------------------------------------
step "6/6  Restarting and checking health"
# -----------------------------------------------------------------------------
docker compose up -d
echo -n "    waiting for the health check"
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo
        ok "healthy"
        echo
        echo "${GREEN}Deployed $NEW_COMMIT successfully.${OFF}"
        echo "Backup taken before the change: $BACKUP"
        exit 0
    fi
    echo -n "."
    sleep 2
done

echo
echo "${RED}The application did not come up healthy within 60 seconds.${OFF}"
echo
echo "Look at the log first:"
echo "    docker compose logs app --tail 60"
echo
echo "To roll back to the previous version:"
echo "    git checkout $PREVIOUS_COMMIT"
echo "    docker compose up -d --build"
echo
echo "${YELLOW}Do NOT run 'alembic downgrade'.${OFF}"
echo "Most releases add no migration at all, and a newer schema is harmless to"
echo "older code in this project -- added columns are nullable or defaulted."
echo "Downgrading the FIRST revision drops every table, including the undo trail,"
echo "so that command is guarded and will refuse. If a release genuinely does need"
echo "its schema change undone, restore the backup below instead: it was taken"
echo "before the migration ran and is therefore the schema the old code expects."
echo
echo "To restore the database from the backup taken above:"
echo "    docker compose stop app"
echo "    gunzip -c $BACKUP | docker compose exec -T db psql -U autopilot -d autopilot"
echo "    docker compose start app"
exit 1
