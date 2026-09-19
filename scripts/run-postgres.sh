#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${JUSI_POSTGRES_IMAGE:-jusi-postgres-dev}"
CONTAINER_NAME="${JUSI_POSTGRES_CONTAINER:-jusi-postgres-dev}"
HOST_PORT="${JUSI_POSTGRES_PORT:-55432}"
POSTGRES_USER="${JUSI_POSTGRES_USER:-jusi}"
POSTGRES_PASSWORD="${JUSI_POSTGRES_PASSWORD:-jusi}"
POSTGRES_DB="${JUSI_POSTGRES_DB:-jusi}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker build -t "${IMAGE_NAME}" "${ROOT_DIR}"

if docker ps --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  echo "PostgreSQL container '${CONTAINER_NAME}' is already running on port ${HOST_PORT}."
else
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
    docker rm "${CONTAINER_NAME}" >/dev/null
  fi

  docker run \
    --detach \
    --name "${CONTAINER_NAME}" \
    --publish "127.0.0.1:${HOST_PORT}:5432" \
    --env "POSTGRES_USER=${POSTGRES_USER}" \
    --env "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
    --env "POSTGRES_DB=${POSTGRES_DB}" \
    "${IMAGE_NAME}" >/dev/null
fi

echo "Waiting for PostgreSQL to accept connections..."
for _ in $(seq 1 60); do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
    cat <<EOF
PostgreSQL is running.

jusi.toml:

[sql.targets.local_postgres]
provider = "postgres"
host = "127.0.0.1"
port = ${HOST_PORT}
dbname = "${POSTGRES_DB}"
user = "${POSTGRES_USER}"
password = "${POSTGRES_PASSWORD}"
initial_fetch = 25
EOF
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL did not become ready in time." >&2
docker logs "${CONTAINER_NAME}" >&2 || true
exit 1
