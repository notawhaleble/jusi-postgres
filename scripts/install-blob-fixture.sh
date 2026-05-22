#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${JUSI_POSTGRES_CONTAINER:-jusi-postgres-dev}"
POSTGRES_USER="${JUSI_POSTGRES_USER:-jusi}"
POSTGRES_DB="${JUSI_POSTGRES_DB:-jusi}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_PATH="${ROOT_DIR}/docker/postgres/initdb.d/002_jusi_blob_fixture.sql"

docker exec -i "${CONTAINER_NAME}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${SQL_PATH}"

cat <<'EOF'
Blob fixture installed.

Try:

%%sql local_postgres
select id, filename, content_type, payload
from demo.blob_files;
EOF
