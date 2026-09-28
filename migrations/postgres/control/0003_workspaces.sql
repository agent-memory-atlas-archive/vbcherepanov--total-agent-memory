-- Reverse map of provisioned workspaces (plan 1.3): schema and role names are hashes of the key.
-- Applied by team_memory.pg_provision into schema tam_control with search_path set to it.

CREATE TABLE workspace_schemas (
    key text PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 128),
    schema text NOT NULL UNIQUE CHECK (schema ~ '^ws_[0-9a-f]{48}$'),
    role text NOT NULL UNIQUE CHECK (role ~ '^wsr_[0-9a-f]{48}$'),
    created_at text NOT NULL DEFAULT (to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'))
);
