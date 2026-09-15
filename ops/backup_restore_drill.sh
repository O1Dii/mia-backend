#!/bin/sh
# Backup and restore drill for the MIA PostgreSQL database.
#
# Takes a compressed logical dump of MIA_SOURCE_URL, restores it into the
# disposable database MIA_RESTORE_URL and verifies that the restored copy is at
# the same Alembic revision with the same row counts. It never prints a
# connection string, a credential or any row content.
#
# Required: MIA_SOURCE_URL, MIA_RESTORE_URL (libpq URLs, from the secret store).
# Optional: MIA_DUMP_DIR (default: a private temporary directory).
#
# Encrypt the dump before it leaves the runner (for example with `age` or the
# managed backup service) and delete the restore database after the drill.
set -eu

: "${MIA_SOURCE_URL:?set MIA_SOURCE_URL}"
: "${MIA_RESTORE_URL:?set MIA_RESTORE_URL}"

dump_dir="${MIA_DUMP_DIR:-$(mktemp -d)}"
dump_file="$dump_dir/mia-$(date -u +%Y%m%dT%H%M%SZ).dump"
umask 077

echo "drill: creating dump"
pg_dump --format=custom --no-owner --no-privileges --file "$dump_file" "$MIA_SOURCE_URL"

echo "drill: restoring into the disposable database"
psql --quiet --no-psqlrc --dbname "$MIA_RESTORE_URL" \
    --command 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;' >/dev/null
pg_restore --no-owner --no-privileges --dbname "$MIA_RESTORE_URL" "$dump_file"

counts() {
    psql --quiet --tuples-only --no-align --no-psqlrc --dbname "$1" --command "
        SELECT (SELECT version_num FROM alembic_version)
            || ':' || (SELECT count(*) FROM users)
            || ':' || (SELECT count(*) FROM conversations)
            || ':' || (SELECT count(*) FROM entries)
            || ':' || (SELECT count(*) FROM tool_executions);"
}

source_state="$(counts "$MIA_SOURCE_URL")"
restored_state="$(counts "$MIA_RESTORE_URL")"
if [ "$source_state" != "$restored_state" ]; then
    echo "drill: FAILED, restored revision or row counts differ" >&2
    exit 1
fi

echo "drill: revision and row counts match ($restored_state)"
echo "drill: dump kept at $dump_file; encrypt or delete it, then drop the restore database"
