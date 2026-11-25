"""Connection pool and role-scoped transactions.

The pool connects as the login role, which is NOINHERIT and holds no direct
privileges: every unit of work runs inside ``tx(role)``, which switches role with
``SET LOCAL ROLE`` so the switch ends with the transaction. The pool's reset
callback clears any session-level role or setting a caller left behind before a
connection is handed out again.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Self

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from treant import names


def _reset(conn: psycopg.Connection) -> None:
    # DISCARD ALL also resets the role, temp tables, advisory locks and server-side prepared
    # statements. psycopg only notices it for statements it prepared itself, so the pool
    # disables automatic preparation (prepare_threshold=None) to keep nothing stale.
    conn.execute("DISCARD ALL")


class Database:
    def __init__(self, dsn: str, prefix: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.prefix = prefix
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True, "prepare_threshold": None},
            reset=_reset,
            open=True,
            name="treant",
        )

    def close(self) -> None:
        self.pool.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self, role: str) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield conn

    def ws_tx(self, workspace: str) -> AbstractContextManager[psycopg.Connection]:
        return self.tx(names.ws_role(self.prefix, workspace))

    def control_tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self.tx(names.role(self.prefix, "control"))

    def gate_tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self.tx(names.role(self.prefix, "gate"))

    def publisher_tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self.tx(names.role(self.prefix, "publisher"))

    def admin_tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self.tx(names.role(self.prefix, "admin"))
