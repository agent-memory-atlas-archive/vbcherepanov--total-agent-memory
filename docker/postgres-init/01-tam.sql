-- Convenience bootstrap for docker-compose.team.postgres.yml. NOT FOR PRODUCTION: in production the
-- DBA runs the equivalent statements from docs/TEAM_POSTGRES.md on the organization's own server.
--
-- Runs once, as the image's superuser, when the data volume is empty. Creates what TAM requires
-- (plan 1.4): a LOGIN role with CREATEROLE (TAM creates one role per workspace), a UTF8 database
-- with the builtin C.UTF-8 locale (bytewise ordering, like SQLite), and the vector extension in
-- schema "extensions". The role password comes from TAM_TEAM_PG_PASSWORD.

\set ON_ERROR_STOP on
\getenv tam_password TAM_TEAM_PG_PASSWORD
\if :{?tam_password}
\else
  \echo 'TAM_TEAM_PG_PASSWORD is not set'
  \quit 1
\endif

CREATE ROLE tam LOGIN CREATEROLE PASSWORD :'tam_password';
CREATE DATABASE tam OWNER tam TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8';
REVOKE ALL ON DATABASE tam FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE tam TO tam;

\connect tam
CREATE SCHEMA extensions AUTHORIZATION tam;
CREATE EXTENSION vector SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
