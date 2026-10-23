# FlexyVotes backup worker: nightly encrypted pg_dump (+ private media),
# sync to a private GitHub repository, and restore-on-setup.
# Based on the same PostgreSQL major version as the database so pg_dump and
# pg_restore always match the server.
FROM postgres:16-alpine

# bash/coreutils: the scripts use GNU split/date/stat; openssl: encryption;
# git + curl: GitHub sync and the "is the repository private?" check;
# tzdata: run the schedule in the platform's time zone.
RUN apk add --no-cache bash coreutils openssl git curl tzdata util-linux \
    && mkdir -p /backups /var/lib/fv-backup

COPY scripts/backup.sh scripts/restore.sh /opt/flexyvotes/
COPY backup/fv-backup.sh /usr/local/bin/fv-backup
RUN chmod +x /usr/local/bin/fv-backup /opt/flexyvotes/*.sh

VOLUME ["/backups", "/var/lib/fv-backup"]
ENTRYPOINT ["/usr/local/bin/fv-backup"]
CMD ["cron"]
