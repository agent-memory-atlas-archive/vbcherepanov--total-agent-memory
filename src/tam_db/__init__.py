"""Database backends for the team server: SQLite (default) and an external PostgreSQL.

Only the team server imports this package; the personal install never does. The
package itself is import-cheap: ``contracts`` depends on the standard library alone,
and driver modules (psycopg) are imported explicitly by the code that needs them.
"""
