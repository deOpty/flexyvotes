#!/usr/bin/env bash
# FlexyVotes backup worker (runs in the `backup` and `restore` compose services).
#
#   fv-backup cron              schedule nightly runs (default command)
#   fv-backup nightly           backup + push to GitHub (what cron runs)
#   fv-backup backup            encrypted backup only, then verify it is restorable
#   fv-backup push              sync local backups to the GitHub repository
#   fv-backup list              show local and remote backups
#   fv-backup restore-on-setup  restore the newest backup into an EMPTY database
#   fv-backup health            container healthcheck
#
# Backups are encrypted (AES-256, PBKDF2) with BACKUP_PASSPHRASE before they
# leave the database container's network; GitHub only ever sees ciphertext.
#
# Every step checks its own errors explicitly: bash ignores `set -e` inside
# functions called from `a || b`, so steps that must not "half succeed" are run
# as separate processes ("$0" _backup, "$0" _push, "$0" _fetch).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
STATE_DIR="${BACKUP_STATE_DIR:-/var/lib/fv-backup}"
GIT_WORK="$STATE_DIR/git-sync"
SCRIPTS=/opt/flexyvotes
# Backups live on their own orphan branch of the repository (by default the
# application repository itself), never on the code branches.
BRANCH="${BACKUP_GIT_BRANCH:-backups}"
CHUNK_MB="${BACKUP_GIT_CHUNK_MB:-45}"          # GitHub rejects files over 100 MB
GIT_RETENTION_DAYS="${BACKUP_GIT_RETENTION_DAYS:-30}"
MIN_KEEP="${BACKUP_GIT_MIN_KEEP:-7}"           # never prune below this many sets
export GIT_TERMINAL_PROMPT=0
mkdir -p "$BACKUP_DIR" "$STATE_DIR"

log() { echo "[fv-backup $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
die() { log "ERROR: $*"; exit 1; }

sets_newest_first() {
    # Local backup sets, newest first (names carry a UTC timestamp).
    ls -1 "$BACKUP_DIR"/flexyvotes-*.dump.enc 2>/dev/null | sort -r || true
}

require_passphrase() {
    [ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE is not set. Set it in .env (keep a copy in your password manager)."
    [ "${#BACKUP_PASSPHRASE}" -ge 16 ] || die "BACKUP_PASSPHRASE must be at least 16 characters."
}

decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass env:BACKUP_PASSPHRASE -in "$1"; }

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
do_backup() {
    require_passphrase
    [ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set."
    local initialised previous latest
    initialised="$(psql "$DATABASE_URL" -tAc "SELECT to_regclass('public.django_migrations') IS NOT NULL")" \
        || die "Cannot connect to the database."
    # Never let backups of an empty database push the good ones out of retention.
    [ "$initialised" = "t" ] || die "The database is empty (no migrations applied) - not taking a backup."
    previous="$(sets_newest_first | head -n 1)"
    BACKUP_DIR="$BACKUP_DIR" bash "$SCRIPTS/backup.sh" || die "pg_dump/encryption failed."
    latest="$(sets_newest_first | head -n 1)"
    [ -n "$latest" ] && [ "$latest" != "$previous" ] || die "backup.sh finished but no new backup file was written."
    # Prove the new backup is restorable, not just written.
    decrypt "$latest" | pg_restore --list >/dev/null || die "$(basename "$latest") could not be decrypted/read back."
    date +%s > "$STATE_DIR/last_backup"
    basename "$latest" > "$STATE_DIR/last_backup_name"
    log "Backup OK and verified: $(basename "$latest") ($(du -h "$latest" | cut -f1))"
}

# ---------------------------------------------------------------------------
# GitHub sync
# ---------------------------------------------------------------------------
git_auth() {
    # Token goes in an HTTP header via environment config - never in the URL,
    # .git/config or the process list.
    if [ -n "${BACKUP_GIT_TOKEN:-}" ]; then
        export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
        GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$BACKUP_GIT_TOKEN" | base64 | tr -d '\n')"
        export GIT_CONFIG_VALUE_0
    fi
}

github_owner_repo() {
    # Prints "owner/repo" for github.com URLs, nothing otherwise.
    if [[ "$BACKUP_GIT_REPO" =~ github\.com[:/]+([^/]+)/([^/]+)$ ]]; then
        echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
    fi
}

check_repository_is_private() {
    local slug; slug="$(github_owner_repo)"
    if [ -z "$slug" ]; then
        log "WARNING: $BACKUP_GIT_REPO is not a github.com repository; cannot verify it is private."
        return 0
    fi
    [ -n "${BACKUP_GIT_TOKEN:-}" ] || die "BACKUP_GIT_TOKEN is required to push to GitHub over HTTPS."
    local response
    response="$(curl -fsS --max-time 20 -K - "https://api.github.com/repos/$slug" <<EOF
header = "Authorization: Bearer $BACKUP_GIT_TOKEN"
header = "Accept: application/vnd.github+json"
EOF
)" || die "Could not confirm that $slug is private (token invalid, or no access to that repository). Refusing to push."
    grep -Eq '"private": *true' <<<"$response" \
        || die "$slug is PUBLIC. Make it private (GitHub > Settings > General > Danger Zone > Change visibility) - refusing to push backups to a public repository."
    local default_branch
    default_branch="$(sed -nE 's/.*"default_branch": *"([^"]+)".*/\1/p' <<<"$response" | head -n 1)"
    [ -z "$default_branch" ] || [ "$default_branch" != "$BRANCH" ] \
        || die "BACKUP_GIT_BRANCH='$BRANCH' is the repository's default (code) branch. Use a dedicated branch such as 'backups'."
}

guard_branch() {
    # Backups are force-pushed as one snapshot commit: never let that touch a
    # code branch.
    case "$BRANCH" in
        main|master|develop|dev|trunk|release*|gh-pages)
            die "BACKUP_GIT_BRANCH='$BRANCH' looks like a code branch. Use a dedicated branch such as 'backups'." ;;
    esac
}

prepare_clone() {
    # Local working copy of the backup branch. It is rebuilt from the remote
    # whenever the repository or branch changes, or the branch is missing
    # remotely - a stale copy would otherwise report "up to date" and skip pushes.
    local rc=0 origin="$BACKUP_GIT_REPO#$BRANCH"
    if [ "$(cat "$STATE_DIR/git-sync-origin" 2>/dev/null)" != "$origin" ]; then
        rm -rf "$GIT_WORK"
    fi
    git ls-remote --exit-code --heads "$BACKUP_GIT_REPO" "$BRANCH" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        if [ -d "$GIT_WORK/.git" ]; then
            git -C "$GIT_WORK" fetch --quiet --depth 1 origin "$BRANCH" || die "git fetch failed."
            git -C "$GIT_WORK" reset --quiet --hard FETCH_HEAD || die "git reset failed."
            git -C "$GIT_WORK" clean -qfd || die "git clean failed."
        else
            rm -rf "$GIT_WORK"
            git clone --quiet --depth 1 --branch "$BRANCH" "$BACKUP_GIT_REPO" "$GIT_WORK" || die "git clone failed."
        fi
    elif [ "$rc" -eq 2 ]; then
        log "Repository has no '$BRANCH' branch yet - creating it as a separate (orphan) branch."
        rm -rf "$GIT_WORK"
        git init --quiet -b "$BRANCH" "$GIT_WORK" || die "git init failed."
        git -C "$GIT_WORK" remote add origin "$BACKUP_GIT_REPO" || die "git remote add failed."
    else
        die "Cannot reach $BACKUP_GIT_REPO (git exit code $rc)."
    fi
    echo "$origin" > "$STATE_DIR/git-sync-origin"
    # The backup branch must never be a branch that holds the application code.
    [ ! -f "$GIT_WORK/manage.py" ] || die "Branch '$BRANCH' of $BACKUP_GIT_REPO contains the application code. Use a dedicated branch such as 'backups'."
    git -C "$GIT_WORK" config user.name "FlexyVotes backup"
    git -C "$GIT_WORK" config user.email "backup@$(hostname)"
}

write_repo_readme() {
    [ -f "$GIT_WORK/README.md" ] && return 0
    cat > "$GIT_WORK/README.md" <<'EOF'
# FlexyVotes encrypted backups

This branch is written automatically by the FlexyVotes `backup` service every
night. It shares no history with the code branches - do not merge it.

- `backups/flexyvotes-<UTC timestamp>/` holds one backup set:
  `*.dump.enc` (PostgreSQL custom-format dump), `*.media.tgz.enc` (private
  evidence files) and `*.sha256` (checksums). Files over the chunk size are
  split into `*.part-000`, `*.part-001`, ... and joined on restore.
- Everything is encrypted with AES-256 (OpenSSL, PBKDF2 600k iterations) using
  `BACKUP_PASSPHRASE`. Without that passphrase - and the application's
  `SECRET_KEY` / encryption keys for the data inside - these files are useless.
- Restoring: on a new server, set `BACKUP_GIT_REPO`, `BACKUP_GIT_TOKEN`,
  `BACKUP_PASSPHRASE` and the original application keys in `.env`, then run
  `./deploy.sh`. An empty database is restored from the newest set automatically.

The repository must stay **private** - the backup service refuses to push to a
public one.
EOF
}

do_push() {
    if [ -z "${BACKUP_GIT_REPO:-}" ]; then
        log "BACKUP_GIT_REPO is not set - skipping the GitHub push (backups stay on this server only)."
        return 0
    fi
    guard_branch
    git_auth
    check_repository_is_private
    prepare_clone
    mkdir -p "$GIT_WORK/backups"
    write_repo_readme

    local added=0 dump name dest f
    while read -r dump; do
        [ -n "$dump" ] || continue
        name="$(basename "$dump" .dump.enc)"
        dest="$GIT_WORK/backups/$name"
        [ -f "$dest/$name.sha256" ] && continue          # already pushed
        [ -f "$BACKUP_DIR/$name.sha256" ] || { log "Skipping $name (no checksum file)."; continue; }
        mkdir -p "$dest"
        for f in "$BACKUP_DIR/$name".*; do
            [[ "$f" == *.sha256 ]] && continue
            if [ "$(stat -c %s "$f")" -gt $((CHUNK_MB * 1024 * 1024)) ]; then
                split -b "${CHUNK_MB}M" -d -a 3 "$f" "$dest/$(basename "$f").part-" || die "split failed for $f."
            else
                cp "$f" "$dest/" || die "copy failed for $f."
            fi
        done
        cp "$BACKUP_DIR/$name.sha256" "$dest/" || die "copy failed."  # written last = set complete
        added=$((added + 1))
    done < <(sets_newest_first | sort)

    # Retention: drop sets older than the window, but always keep MIN_KEEP.
    local cutoff kept=0 d stamp
    cutoff="$(date -u -d "-${GIT_RETENTION_DAYS} days" +%Y%m%d)"
    while read -r d; do
        [ -n "$d" ] || continue
        kept=$((kept + 1))
        stamp="$(basename "$d" | sed -E 's/^flexyvotes-([0-9]{8}).*/\1/')"
        if [ "$kept" -gt "$MIN_KEEP" ] && [ "$stamp" -lt "$cutoff" ]; then
            rm -rf "$d"
        fi
    done < <(ls -1d "$GIT_WORK"/backups/flexyvotes-* 2>/dev/null | sort -r)

    git -C "$GIT_WORK" add -A
    if git -C "$GIT_WORK" diff --cached --quiet; then
        log "GitHub already up to date ($(ls -1d "$GIT_WORK"/backups/flexyvotes-* 2>/dev/null | wc -l) sets)."
    else
        local total; total="$(ls -1d "$GIT_WORK"/backups/flexyvotes-* 2>/dev/null | wc -l)"
        git -C "$GIT_WORK" commit --quiet -m "Backup $(date -u +%Y-%m-%dT%H:%MZ): $added new set(s), $total kept" \
            || die "git commit failed."
        if [ "${BACKUP_GIT_KEEP_HISTORY:-false}" = "false" ]; then
            # Keep the repository small: replace history with one snapshot commit.
            git -C "$GIT_WORK" checkout --quiet --orphan fv-snapshot || die "git checkout failed."
            git -C "$GIT_WORK" commit --quiet -m "Backups snapshot $(date -u +%Y-%m-%dT%H:%MZ) ($total sets)" \
                || die "git commit failed."
            git -C "$GIT_WORK" branch -M fv-snapshot "$BRANCH" || die "git branch failed."
            git -C "$GIT_WORK" push --quiet --force origin "$BRANCH" || die "git push failed."
        elif ! git -C "$GIT_WORK" push --quiet origin "$BRANCH"; then
            log "Push rejected - rebasing onto the remote and retrying."
            git -C "$GIT_WORK" pull --quiet --rebase --depth 1 origin "$BRANCH" || die "git pull --rebase failed."
            git -C "$GIT_WORK" push --quiet origin "$BRANCH" || die "git push failed."
        fi
        log "Pushed $added new backup set(s) to ${BACKUP_GIT_REPO} ($total kept)."
    fi
    date +%s > "$STATE_DIR/last_push"
}

# ---------------------------------------------------------------------------
# Restore on setup
# ---------------------------------------------------------------------------
do_fetch() {
    # Reassemble the newest remote sets into $STATE_DIR/restore-files and list
    # their dump paths (newest first) in $STATE_DIR/restore-candidates.
    local out="$STATE_DIR/restore-files" d name f rc=0
    guard_branch
    git_auth
    check_repository_is_private
    rm -rf "$STATE_DIR/restore-src" "$out" && mkdir -p "$out"
    : > "$STATE_DIR/restore-candidates"
    git ls-remote --exit-code --heads "$BACKUP_GIT_REPO" "$BRANCH" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 2 ]; then log "Backup repository is empty."; return 0; fi
    [ "$rc" -eq 0 ] || die "Cannot reach $BACKUP_GIT_REPO (git exit code $rc)."
    git clone --quiet --depth 1 --branch "$BRANCH" "$BACKUP_GIT_REPO" "$STATE_DIR/restore-src" || die "git clone failed."
    while read -r d; do
        [ -n "$d" ] || continue
        name="$(basename "$d")"
        for f in "$d"/*; do
            case "$f" in *.part-*) ;; *) cp "$f" "$out/" || die "copy failed." ;; esac
        done
        for f in "$d"/*.part-000; do
            [ -e "$f" ] || continue
            cat "${f%.part-000}".part-* > "$out/$(basename "${f%.part-000}")" || die "could not join ${f%.part-000}."
        done
        if [ -f "$out/$name.dump.enc" ]; then echo "$out/$name.dump.enc" >> "$STATE_DIR/restore-candidates"; fi
    done < <(ls -1d "$STATE_DIR"/restore-src/backups/flexyvotes-* 2>/dev/null | sort -r | head -n 3)
    log "Fetched $(wc -l < "$STATE_DIR/restore-candidates") backup set(s) from GitHub."
}

restore_media() {
    local dump="$1" media sums dir="${PRIVATE_MEDIA_DIR:-}"
    media="${dump%.dump.enc}.media.tgz.enc"
    [ -n "$dir" ] && [ -f "$media" ] || return 0
    if [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
        log "Private media directory is not empty - leaving it as it is."
        return 0
    fi
    sums="${dump%.dump.enc}.sha256"
    if [ -f "$sums" ]; then
        (cd "$(dirname "$media")" && grep -F "$(basename "$media")" "$(basename "$sums")" | sha256sum -c - >/dev/null) \
            || die "Checksum mismatch for $(basename "$media")."
    fi
    decrypt "$media" | tar -C "$dir" -xzf -
    log "Private media restored into $dir."
}

reset_schema() {
    PGOPTIONS='--client-min-messages=warning' psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 \
        -c 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;' >/dev/null
}

do_restore_on_setup() {
    if [ "${BACKUP_RESTORE_ON_SETUP:-true}" != "true" ]; then
        log "BACKUP_RESTORE_ON_SETUP is not 'true' - skipping."
        return 0
    fi
    [ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set."
    local initialised
    initialised="$(psql "$DATABASE_URL" -tAc "SELECT to_regclass('public.django_migrations') IS NOT NULL")"
    if [ "$initialised" = "t" ]; then
        log "Database already initialised - nothing to restore."
        return 0
    fi

    local candidates=()
    if [ -n "${BACKUP_RESTORE_FILE:-}" ]; then
        candidates=("$BACKUP_RESTORE_FILE")
    else
        mapfile -t candidates < <(sets_newest_first | head -n 3)
        if [ "${#candidates[@]}" -eq 0 ] && [ -n "${BACKUP_GIT_REPO:-}" ]; then
            log "No local backups - fetching the newest from $BACKUP_GIT_REPO."
            # A configured but unreachable repository must stop the setup:
            # starting empty would let new nightly backups replace the real ones.
            "$0" _fetch || die "Could not fetch backups from GitHub. Fix BACKUP_GIT_REPO/BACKUP_GIT_TOKEN, or set BACKUP_RESTORE_ON_SETUP=false to start with an empty database."
            mapfile -t candidates < "$STATE_DIR/restore-candidates"
        fi
    fi
    if [ "${#candidates[@]}" -eq 0 ]; then
        log "Empty database and no backups found - starting fresh."
        return 0
    fi
    require_passphrase

    local dump
    for dump in "${candidates[@]}"; do
        log "Empty database found - restoring $(basename "$dump") ..."
        reset_schema
        if TARGET_DATABASE_URL="$DATABASE_URL" bash "$SCRIPTS/restore.sh" "$dump"; then
            restore_media "$dump"
            log "Restore complete from $(basename "$dump"). The app needs the SAME SECRET_KEY and encryption keys as the server that made this backup."
            return 0
        fi
        log "Restore of $(basename "$dump") failed - trying an older backup."
    done
    reset_schema
    die "No backup could be restored. Fix the cause (passphrase? corrupt file?) or set BACKUP_RESTORE_ON_SETUP=false to start with an empty database."
}

# ---------------------------------------------------------------------------
# Health, listing, scheduling
# ---------------------------------------------------------------------------
age_ok() {
    local file="$1" max=$(( ${BACKUP_MAX_AGE_HOURS:-26} * 3600 )) last
    last="$(cat "$file" 2>/dev/null || echo 0)"
    [ $(( $(date +%s) - last )) -le "$max" ]
}

do_health() {
    age_ok "$STATE_DIR/last_backup" || { echo "no successful backup in the last ${BACKUP_MAX_AGE_HOURS:-26}h"; exit 1; }
    if [ -n "${BACKUP_GIT_REPO:-}" ]; then
        age_ok "$STATE_DIR/last_push" || { echo "no successful GitHub push in the last ${BACKUP_MAX_AGE_HOURS:-26}h"; exit 1; }
    fi
    echo "ok: last backup $(cat "$STATE_DIR/last_backup_name" 2>/dev/null)"
}

do_list() {
    echo "Local backups ($BACKUP_DIR):"
    sets_newest_first | while read -r f; do [ -n "$f" ] && echo "  $(basename "$f")  $(du -h "$f" | cut -f1)"; done
    if [ -d "$GIT_WORK/backups" ]; then
        echo "Last synced to GitHub:"
        ls -1 "$GIT_WORK/backups" | sort -r | sed 's/^/  /'
    fi
}

run_locked() {
    exec 9>"$STATE_DIR/lock"
    flock -n 9 || die "Another backup run is in progress."
    "$@"
}

nightly() {
    local status=0
    "$0" _backup || status=$?
    "$0" _push || status=$?
    if [ "$status" -eq 0 ]; then log "Nightly run finished OK."; else log "Nightly run finished with errors (see above)."; fi
    return "$status"
}

do_cron() {
    # busybox crond does not pass the container environment to jobs.
    : > /etc/fv-backup.env && chmod 600 /etc/fv-backup.env
    env | while IFS='=' read -r key value; do
        case "$key" in
            BACKUP_*|DATABASE_URL|PRIVATE_MEDIA_DIR|TZ)
                printf "export %s='%s'\n" "$key" "$(printf %s "$value" | sed "s/'/'\\\\''/g")" >> /etc/fv-backup.env ;;
        esac
    done
    local schedule="${BACKUP_SCHEDULE:-0 2 * * *}"
    echo "$schedule . /etc/fv-backup.env; /usr/local/bin/fv-backup nightly >> /proc/1/fd/1 2>&1" > /etc/crontabs/root
    log "Nightly backup scheduled at '$schedule' (TZ=${TZ:-UTC}); GitHub: ${BACKUP_GIT_REPO:-not configured}."
    if [ "${BACKUP_ON_START:-true}" = "true" ] && ! age_ok "$STATE_DIR/last_backup"; then
        # The app applies migrations as it starts; wait (up to 15 min) so the
        # first backup is of a real database rather than refused as empty.
        local waited=0
        until [ "$(psql "$DATABASE_URL" -tAc "SELECT to_regclass('public.django_migrations') IS NOT NULL" 2>/dev/null)" = "t" ]; do
            [ "$waited" -ge 900 ] && break
            sleep 10; waited=$((waited + 10))
        done
        log "No recent backup - taking one now."
        "$0" nightly || log "Initial backup run failed (see above); cron retries at the next scheduled time."
    fi
    exec crond -f -d 8
}

case "${1:-cron}" in
    cron) do_cron ;;
    nightly) run_locked nightly ;;
    backup) run_locked do_backup ;;
    push) run_locked do_push ;;
    _backup) do_backup ;;
    _push) do_push ;;
    _fetch) do_fetch ;;
    list) do_list ;;
    restore-on-setup) do_restore_on_setup ;;
    health) do_health ;;
    *) exec "$@" ;;
esac
