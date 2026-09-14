"""SQLAlchemy engine over the MySQL wire protocol (PyMySQL) to TiDB Cloud.

Credentials arrive as a dict resolved from Secrets Manager by ARN (see
secrets.py) — never from env vars. The engine is cached per secret ARN so a
warm Lambda container reuses its pooled connection.

TiDB Cloud requires TLS. If the secret provides `ssl_ca` we pin that CA bundle;
otherwise TLS is still enabled (empty ssl dict tells PyMySQL to negotiate TLS).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .secrets import get_secret


def _build_engine(creds: dict) -> Engine:
    url = (
        f"mysql+pymysql://{creds['username']}:{creds['password']}"
        f"@{creds['host']}:{int(creds.get('port', 4000))}/{creds['database']}"
    )
    ssl_ca = creds.get("ssl_ca")
    ssl_arg = {"ca": ssl_ca} if ssl_ca else {}  # empty dict => TLS on, default trust store
    return create_engine(
        url,
        connect_args={"ssl": ssl_arg, "connect_timeout": 10},
        pool_pre_ping=True,     # drop dead connections between Lambda invokes
        pool_recycle=280,       # under typical idle-timeout windows
        pool_size=1,            # Lambda: one active connection per warm container
        max_overflow=2,
        future=True,
    )


@lru_cache(maxsize=16)
def get_engine(secret_arn: str) -> Engine:
    """Engine for the tenant-scoped role identified by `secret_arn`.

    The interceptor swaps to the tenant-scoped credential and passes its ARN;
    the handler calls this with that ARN so the DB session is scoped to the
    tenant's role — the last-line data scope, independent of Cedar.
    """
    return _build_engine(get_secret(secret_arn))
