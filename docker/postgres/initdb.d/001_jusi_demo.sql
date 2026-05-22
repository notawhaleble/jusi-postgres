CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.accounts (
    id bigserial PRIMARY KEY,
    email text NOT NULL UNIQUE,
    display_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS demo.events (
    id bigserial PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES demo.accounts(id),
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO demo.accounts (email, display_name, active)
VALUES
    ('ada@example.test', 'Ada Lovelace', true),
    ('grace@example.test', 'Grace Hopper', true),
    ('alan@example.test', 'Alan Turing', false)
ON CONFLICT (email) DO NOTHING;

INSERT INTO demo.events (account_id, event_type, payload)
SELECT account_id, event_type, payload::jsonb
FROM (
    VALUES
        (1, 'login', '{"ip": "127.0.0.1"}'),
        (1, 'query', '{"rows": 42}'),
        (2, 'login', '{"ip": "127.0.0.2"}'),
        (3, 'disabled_login', '{"reason": "inactive"}')
) AS seed(account_id, event_type, payload)
WHERE NOT EXISTS (SELECT 1 FROM demo.events);

CREATE OR REPLACE FUNCTION demo.account_event_count(account_id bigint)
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT count(*) FROM demo.events e WHERE e.account_id = $1
$$;

CREATE OR REPLACE VIEW demo.account_summary AS
SELECT
    a.id,
    a.email,
    a.display_name,
    a.active,
    demo.account_event_count(a.id) AS event_count
FROM demo.accounts a;
