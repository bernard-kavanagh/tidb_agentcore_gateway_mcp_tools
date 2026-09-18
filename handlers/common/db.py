"""SQLAlchemy engine over the MySQL wire protocol (PyMySQL) to TiDB Cloud.

Credentials arrive as a dict resolved from Secrets Manager by ARN (see
secrets.py) — never from env vars. The engine is cached per secret ARN so a
warm Lambda container reuses its pooled connection.

TiDB Cloud requires TLS and we ENFORCE it here rather than leaning on PyMySQL's
opportunistic default. We hand PyMySQL a fully-configured ``ssl.SSLContext``:

  * TiDB speaks the MySQL wire protocol, so TLS is negotiated *inside* the
    protocol (a STARTTLS-style upgrade after the server's plaintext greeting) —
    NOT as an implicit TLS socket. PyMySQL performs that upgrade for us.
  * Passing a context object (a truthy `ssl` arg) puts PyMySQL in *required*
    mode — it will not silently fall back to an unencrypted connection.
  * The context verifies the server certificate chain (CERT_REQUIRED) and checks
    the hostname. This is explicit here because PyMySQL's own ``ssl={...}`` dict
    handling disables hostname checking whenever no CA file is supplied.

`ssl_ca`, when present in the secret, is a filesystem PATH to a CA bundle shipped
inside the Lambda package/layer; it pins trust to that bundle. When absent we
verify against the system trust store (TiDB Cloud serves a publicly-rooted cert).
"""

from __future__ import annotations

import ssl
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .secrets import get_secret


def build_ssl_context(ssl_ca: str | None) -> ssl.SSLContext:
    """A TLS context that REQUIRES a verified, hostname-matched server cert.

    `ssl_ca` is a path to a CA bundle file shipped in the Lambda package; None
    verifies against the system trust store. Handing this context to PyMySQL both
    forces TLS on (a truthy `ssl` arg => required, not preferred) and bypasses
    PyMySQL's ssl-dict handling, which would otherwise silently disable hostname
    checking whenever no CA file is given.

    Raises OSError (e.g. FileNotFoundError) or ssl.SSLError if `ssl_ca` points at
    a missing or malformed bundle — a misconfigured CA fails loudly instead of
    quietly degrading to an unverified connection.
    """
    ctx = ssl.create_default_context(cafile=ssl_ca)  # raises if ssl_ca is missing/unreadable
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def build_connect_args(creds: dict) -> dict:
    """PyMySQL connect kwargs: an enforced-TLS context plus a connect timeout."""
    return {
        "ssl": build_ssl_context(creds.get("ssl_ca")),
        "connect_timeout": 10,
    }


def _build_engine(creds: dict) -> Engine:
    url = (
        f"mysql+pymysql://{creds['username']}:{creds['password']}"
        f"@{creds['host']}:{int(creds.get('port', 4000))}/{creds['database']}"
    )
    return create_engine(
        url,
        connect_args=build_connect_args(creds),
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
