# jusi-postgres

`jusi-postgres` is the PostgreSQL provider plugin for the shared `jusi-sql`
layer. It exposes the `postgres` SQL provider for `%%sql` cells resolved
through the active Jusi session config.

Example session config:

```toml
[sql.analytics]
provider = "postgres"
host = "localhost"
port = 5432
dbname = "analytics"
user = "me"
initial_fetch = 100
```

All psycopg connection options may be provided in the target config. The
additional `krb5ccname` option points at a custom Kerberos credential cache and
is passed to libpq through `KRB5CCNAME` while connecting.

VisiData mappings:

- `1` to `9`: fetch that many more rows for the active result sheet.
- `gf`: prompt for a row count to fetch; `0` fetches the rest of the cursor.
- `gc`: commit the current connection.
- `gr`: roll back the current connection.
- `gb`: write the selected raw cell value to a temporary file and open it with
  VisiData. The command prompts for an optional extension; an empty extension
  lets VisiData infer the file type from content and filename.

## Local development database

Start a disposable PostgreSQL fixture:

```sh
scripts/run-postgres.sh
```

The script builds `jusi-postgres-dev`, runs a local container on
`127.0.0.1:55432`, waits for readiness, and prints this config:

```toml
[sql.local_postgres]
provider = "postgres"
host = "127.0.0.1"
port = 55432
dbname = "jusi"
user = "jusi"
password = "jusi"
initial_fetch = 25
```

The fixture creates `demo.accounts`, `demo.events`, `demo.account_summary`,
`demo.account_event_count(bigint)`, and `demo.blob_files` for result browsing,
completion checks, and raw bytea/blob handling.

For an already-running development container, install or refresh the blob table:

```sh
scripts/install-blob-fixture.sh
```

The source zip fixture is stored at `fixtures/test-blob.zip`.
