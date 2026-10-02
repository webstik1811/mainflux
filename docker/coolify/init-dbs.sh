#!/bin/sh
# Creates one database per Mainflux service in the shared Postgres (runs on first start only).
set -e
for db in $MF_DATABASES; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$db\""
done
