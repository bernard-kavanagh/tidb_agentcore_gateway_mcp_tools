"""preflight — non-destructive TiDB connectivity diagnostic.

Runs the same secret-by-ARN + TLS path the real handlers use, so it proves the
DEPLOYMENT-region Lambda (eu-west-1) can actually reach the TiDB cluster in its
own region (eu-central-1) over the public TLS endpoint. It is an OPERATIONS
Lambda, invoked directly (`aws lambda invoke`), NOT an MCP tool behind Cedar —
it takes no tenant argument and asserts nothing.

It distinguishes failure LAYERS so an operator can tell connectivity problems
apart, returning a `stage` that reached and a `failed_stage` on error:

    dns   -> hostname resolves
    tcp   -> TCP connect to host:port
    tls   -> encrypted session established AND verified via the real driver
    auth  -> Secrets Manager creds authenticate to TiDB
    query -> a trivial read (SELECT 1) returns

TLS is proven through the SAME PyMySQL/SQLAlchemy engine the handlers use, not a
raw-socket wrap: TiDB speaks the MySQL wire protocol, where TLS is negotiated
*inside* the protocol (STARTTLS-style, after the server's plaintext greeting).
Wrapping the raw socket immediately — as if port 4000 were an implicit-TLS port —
fails against that greeting-first handshake, so we let the driver do the upgrade
and then confirm the session is actually encrypted (non-empty `Ssl_cipher`).

Non-destructive: only SHOW SESSION STATUS / SELECT 1 / VERSION() /
CURRENT_USER() / @@hostname / DATABASE(). No writes, no DDL. Never logs or
returns secret material.
"""

from __future__ import annotations

import os
import socket
import ssl
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .common.db import get_engine
from .common.secrets import get_secret, region_from_arn

_TIDB_REGION = os.environ.get("TIDB_REGION", "unknown")


def _resolve_secret_arn(event: dict) -> str:
    """Preflight takes the ARN from the event or FACT_LAYER_SECRET_ARN env.

    (The production handlers resolve it from configuration for the requested
    tenant scope; preflight is an ops tool so it accepts a direct override, but
    still resolves the material by ARN — never a raw credential.)
    """
    arn = (event or {}).get("secret_arn") or os.environ.get("FACT_LAYER_SECRET_ARN")
    if not arn:
        raise ValueError("no secret_arn in event and FACT_LAYER_SECRET_ARN unset")
    return arn


def _looks_like_tls_error(exc: BaseException) -> bool:
    """True if any error in the chain is a TLS/certificate problem.

    SQLAlchemy wraps the driver error; a TLS failure during connect can surface
    as an ssl.SSLError nested under OperationalError (or as its message), so we
    walk .orig / __cause__ / __context__ rather than trusting the top type.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        msg = str(cur).lower()
        if any(k in msg for k in ("ssl", "certificate", "tls", "handshake",
                                  "wrong_version", "self-signed", "self signed")):
            return True
        cur = getattr(cur, "orig", None) or cur.__cause__ or cur.__context__
    return False


def handler(event, _lambda_context=None):
    t0 = time.perf_counter()
    stages: list[str] = []
    result: dict = {"ok": False, "tidb_region": _TIDB_REGION,
                    "secret_region": None, "host": None, "port": None,
                    "stages_passed": stages}

    # --- resolve the secret (material stays in-process; never returned) ---
    try:
        secret_arn = _resolve_secret_arn(event)
        result["secret_region"] = region_from_arn(secret_arn)
        creds = get_secret(secret_arn)
        host = creds["host"]
        port = int(creds.get("port", 4000))
        result["host"] = host
        result["port"] = port
    except (ValueError, KeyError) as e:
        result["failed_stage"] = "config"
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
        return result

    # --- 1. DNS ---
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port,
                                                              proto=socket.IPPROTO_TCP)})
        stages.append("dns")
        result["resolved_addresses"] = addrs
    except socket.gaierror as e:
        result["failed_stage"] = "dns"
        result["error"] = f"DNS resolution failed for {host}: {e}"
        return result

    # --- 2. TCP (bare reachability; TLS is negotiated later, in-protocol) ---
    try:
        with socket.create_connection((host, port), timeout=10):
            stages.append("tcp")
    except (socket.timeout, OSError) as e:
        result["failed_stage"] = "tcp"
        result["error"] = f"tcp to {host}:{port} failed: {e}"
        return result

    # --- build the real engine (this is where the TLS context/CA is assembled) ---
    try:
        engine = get_engine(secret_arn)
    except (ssl.SSLError, OSError) as e:
        # A missing/invalid ssl_ca bundle path fails here, before any connection.
        result["failed_stage"] = "tls"
        result["error"] = f"TLS context/CA setup failed: {e}"
        result["error_type"] = type(e).__name__
        return result

    # --- 3. TLS + 4. AUTH: both happen inside the driver's connect() ---
    # PyMySQL reads the greeting, upgrades to TLS (verified, hostname-checked),
    # then authenticates. A connect failure is a TLS problem unless it is clearly
    # an access-denied (auth) rejection.
    try:
        cx = engine.connect()
    except SQLAlchemyError as e:
        orig = str(getattr(e, "orig", e))
        if _looks_like_tls_error(e):
            result["failed_stage"] = "tls"
        elif "1045" in orig or "access denied" in orig.lower():
            result["failed_stage"] = "auth"
        else:
            result["failed_stage"] = "tls"  # failure before any query ran
        result["error"] = f"{result['failed_stage']} to {host}:{port} failed: {type(e).__name__}"
        result["error_detail"] = orig[:200]
        return result

    # --- confirm the session is actually encrypted + 5. QUERY ---
    try:
        with cx:
            cipher_row = cx.execute(
                text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            ).first()
            cipher = cipher_row[1] if cipher_row else ""
            if not cipher:
                result["failed_stage"] = "tls"
                result["error"] = (
                    f"connection to {host}:{port} is NOT encrypted (empty Ssl_cipher)"
                )
                return result
            stages.append("tls")
            result["tls_cipher"] = cipher

            cx.execute(text("SELECT 1"))
            stages.append("auth")

            # `current_user` is a reserved word in TiDB — quote the alias (same
            # reserved-word class as the `rank` fix) or the parser rejects it (1064).
            row = cx.execute(text(
                "SELECT VERSION() AS version, CURRENT_USER() AS `current_user`, "
                "@@hostname AS server_hostname, DATABASE() AS db"
            )).mappings().first()
            stages.append("query")
            result["server"] = dict(row) if row else {}
    except SQLAlchemyError as e:
        result["failed_stage"] = "query" if "tls" in stages else "tls"
        result["error"] = f"{result['failed_stage']} failed: {type(e).__name__}"
        result["error_detail"] = str(getattr(e, "orig", e))[:200]
        return result

    result["ok"] = True
    result["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def lambda_handler(event, context_):  # noqa: N802 (AWS naming)
    return handler(event, context_)
