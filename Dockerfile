FROM postgres:16-alpine

LABEL org.opencontainers.image.title="jusi-postgres-dev"
LABEL org.opencontainers.image.description="Local PostgreSQL fixture for jusi-postgres development"

COPY docker/postgres/initdb.d/ /docker-entrypoint-initdb.d/
