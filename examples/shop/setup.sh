#!/usr/bin/env bash
# Start a throwaway PostgreSQL in Docker with three small databases (crm, billing, support) and a
# read-only role, then print the environment variables YoDb needs.  Remove it with: ./teardown.sh
set -euo pipefail
cd "$(dirname "$0")"
NAME=yodb-sample-pg
PORT=55440

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:$PORT:5432 postgres:17 >/dev/null
until docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
sleep 2
docker exec "$NAME" psql -U postgres -q -c "CREATE ROLE yodb_ro LOGIN PASSWORD 'yodb_ro'"
for db in crm billing support; do
  docker exec "$NAME" psql -U postgres -q -c "CREATE DATABASE $db"
  docker exec -i "$NAME" psql -U postgres -q -d "$db" -v ON_ERROR_STOP=1 < "seed-$db.sql"
  docker exec "$NAME" psql -U postgres -q -d "$db" -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO yodb_ro" -c "ANALYZE"
done

echo "PostgreSQL is running on port $PORT with the databases crm, billing and support."
echo
echo "Run this in your shell, then follow README.md:"
for db in crm billing support; do
  echo "  export YODB_CONN_$(echo $db | tr a-z A-Z)=\"host=localhost port=$PORT dbname=$db user=yodb_ro password=yodb_ro\""
done
