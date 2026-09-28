-- Fingerprint of what ensure() last applied to a workspace role (password digest, connection limit,
-- session settings). When it still matches and the catalog agrees, ensure() changes nothing, so
-- spawning a worker does not rewrite the role (and its password) every time.
-- Applied by team_memory.pg_provision into schema tam_control with search_path set to it.

ALTER TABLE workspace_schemas ADD COLUMN fingerprint text;
