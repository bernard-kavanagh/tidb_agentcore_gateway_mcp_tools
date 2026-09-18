"""Offline tests for the TLS hardening of the TiDB connection path.

Covers, with no live DB and no AWS:

  * the production engine (handlers/common/db.py) REQUIRES TLS and verifies the
    server cert + hostname explicitly (not PyMySQL's opportunistic ssl={} default)
  * missing / malformed CA-bundle paths fail loudly instead of degrading
  * TiDB TLS is MySQL-protocol STARTTLS (greeting-first), NOT implicit TLS — the
    old raw-socket wrap was wrong for exactly this reason
  * a successful preflight runs entirely through the real driver path and proves
    the session is encrypted
  * preflight classifies TLS vs auth vs CA-setup failures
  * the tenant-scoped role in the URL is untouched by the TLS hardening

    python3 tests/test_tls.py
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers.common.db as db  # noqa: E402
import handlers.preflight as preflight  # noqa: E402


# --------------------------------------------------------------------------
# helpers (no pytest fixtures — keeps the __main__ runner working too)
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def _creds(**over):
    base = dict(username="fact_app", password="pw",
                host="gateway01.eu-central-1.prod.aws.tidbcloud.com",
                port=4000, database="fact_layer")
    base.update(over)
    return base


def _valid_ca_path():
    """A real, parseable CA bundle path from a prod dependency (botocore)."""
    import botocore
    return os.path.join(os.path.dirname(botocore.__file__), "cacert.pem")


# --------------------------------------------------------------------------
# build_ssl_context: explicit verification + CA behavior
# --------------------------------------------------------------------------
def test_ssl_context_requires_verification_and_hostname():
    ctx = db.build_ssl_context(None)  # system trust store
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED, "must verify the server cert chain"
    assert ctx.check_hostname is True, "must check the hostname (defend against MITM)"


def test_ssl_context_pins_to_ca_bundle_path():
    # ssl_ca is a filesystem PATH to a bundle shipped in the Lambda package.
    ctx = db.build_ssl_context(_valid_ca_path())
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.get_ca_certs(), "the pinned bundle must actually load certs"


def test_ssl_context_missing_ca_raises():
    # A missing bundle path must fail loudly, never silently skip verification.
    raised = False
    try:
        db.build_ssl_context("/no/such/ca-bundle.pem")
    except (OSError, ssl.SSLError):
        raised = True
    assert raised, "missing ssl_ca path must raise, not degrade to unverified TLS"


def test_ssl_context_malformed_ca_raises():
    # A path that exists but is not a valid PEM must also fail loudly.
    bad = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_not_a_ca.tmp")
    with open(bad, "w") as f:
        f.write("this is not a certificate\n")
    try:
        raised = False
        try:
            db.build_ssl_context(bad)
        except (OSError, ssl.SSLError):
            raised = True
        assert raised, "malformed ssl_ca must raise"
    finally:
        os.remove(bad)


# --------------------------------------------------------------------------
# engine construction: TLS is REQUIRED (context, not empty dict) + scope intact
# --------------------------------------------------------------------------
def _capture_engine(creds):
    """Build an engine but capture the create_engine kwargs instead of connecting."""
    captured = {}

    def fake_create_engine(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return "ENGINE"

    with _patched(db, create_engine=fake_create_engine):
        db._build_engine(creds)
    return captured


def test_engine_forces_tls_context_not_empty_dict():
    cap = _capture_engine(_creds())
    ssl_arg = cap["connect_args"]["ssl"]
    # An SSLContext (truthy) puts PyMySQL in REQUIRED mode; an empty dict would be
    # PREFERRED (opportunistic) — the exact regression we are guarding against.
    assert isinstance(ssl_arg, ssl.SSLContext), "ssl must be an enforced context"
    assert ssl_arg.verify_mode == ssl.CERT_REQUIRED
    assert ssl_arg.check_hostname is True
    assert cap["connect_args"]["connect_timeout"] == 10


def test_engine_url_preserves_tenant_scoped_role():
    # The tenant scope IS the DB role carried in the (per-tenant) secret. TLS
    # hardening must not alter which credentials/database the engine binds to.
    cap = _capture_engine(_creds(username="tenant_acme_ro", database="fact_layer"))
    assert "tenant_acme_ro" in cap["url"]
    assert "fact_layer" in cap["url"]
    assert "gateway01.eu-central-1.prod.aws.tidbcloud.com" in cap["url"]


def test_build_engine_with_bad_ca_raises_before_connect():
    raised = False
    try:
        _capture_engine(_creds(ssl_ca="/no/such/ca-bundle.pem"))
    except (OSError, ssl.SSLError):
        raised = True
    assert raised, "a bad ssl_ca must fail at engine build, not at first query"


# --------------------------------------------------------------------------
# MySQL STARTTLS (greeting-first) vs implicit TLS — the core protocol fix
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _greeting_first_server():
    """A TCP server that sends a MySQL-style plaintext greeting on connect, like
    TiDB does: TLS is STARTTLS (negotiated in-protocol), never implicit."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        # MySQL Initial Handshake Packet: 3-byte length, seq 0, protocol version 10.
        payload = b"\x0a" + b"8.0.11-TiDB\x00" + b"\x00" * 20
        header = len(payload).to_bytes(3, "little") + b"\x00"
        try:
            conn.sendall(header + payload)
            conn.recv(64)  # whatever the client sends next
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield host, port
    finally:
        srv.close()


def test_implicit_tls_wrap_fails_against_starttls_server():
    # The OLD preflight bug: wrap the raw socket immediately. Against a
    # greeting-first (STARTTLS) server this MUST fail — the plaintext greeting is
    # read as a bogus TLS record (WRONG_VERSION_NUMBER). That is precisely why the
    # raw-socket probe was wrong for TiDB.
    with _greeting_first_server() as (host, port):
        with socket.create_connection((host, port), timeout=5) as s:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raised = False
            try:
                with ctx.wrap_socket(s, server_hostname=host) as tls_sock:
                    tls_sock.recv(16)
            except ssl.SSLError:
                raised = True
            assert raised, "implicit-TLS wrap must fail against a greeting-first server"


def test_starttls_reads_plaintext_greeting_first():
    # The correct MySQL behavior (what PyMySQL / the driver does): read the
    # plaintext greeting first; TLS is negotiated in-protocol afterwards.
    with _greeting_first_server() as (host, port):
        with socket.create_connection((host, port), timeout=5) as s:
            greeting = s.recv(64)
            assert greeting, "server sends a plaintext greeting first"
            assert greeting[4] == 0x0A, "MySQL protocol v10 handshake packet expected"


# --------------------------------------------------------------------------
# preflight through the REAL driver path (stubbed engine, no live DB)
# --------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, first=None, mrow=None):
        self._first = first
        self._mrow = mrow

    def first(self):
        return self._first

    def mappings(self):
        mrow = self._mrow

        class _M:
            def first(self_inner):
                return mrow

        return _M()


class _FakeConn:
    def __init__(self, cipher="TLS_AES_256_GCM_SHA384"):
        self.cipher = cipher
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, clause):
        sql = str(clause)
        self.executed.append(sql)
        if "Ssl_cipher" in sql:
            return _FakeResult(first=("Ssl_cipher", self.cipher) if self.cipher else None)
        if "VERSION()" in sql:
            return _FakeResult(mrow={"version": "8.0.11-TiDB", "current_user": "fact_app@%",
                                     "server_hostname": "tidb-server-1", "db": "fact_layer"})
        return _FakeResult()


class _FakeEngine:
    def __init__(self, conn=None, connect_error=None):
        self._conn = conn or _FakeConn()
        self._connect_error = connect_error

    def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        return self._conn


class _CtxSock:
    """Stand-in for the socket returned by create_connection (context manager)."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_ARN = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:tidb-XyZ012"


@contextlib.contextmanager
def _preflight_env(engine, creds=None):
    creds = creds or _creds()
    with _patched(
        preflight,
        get_secret=lambda arn: creds,
        get_engine=lambda arn: engine,
    ), _patched(
        preflight.socket,
        getaddrinfo=lambda h, p, **k: [(2, 1, 6, "", ("13.248.150.187", p))],
        create_connection=lambda addr, timeout=None: _CtxSock(),
    ):
        yield


def test_preflight_success_uses_real_db_path():
    with _preflight_env(_FakeEngine()):
        out = preflight.handler({"secret_arn": _ARN})
    assert out["ok"] is True, out
    assert out["stages_passed"] == ["dns", "tcp", "tls", "auth", "query"], out
    assert out["tls_cipher"] == "TLS_AES_256_GCM_SHA384"
    assert out["server"]["version"].endswith("TiDB")
    assert out["host"].endswith("tidbcloud.com")


def test_preflight_fails_when_session_not_encrypted():
    # If the driver somehow connected without TLS, preflight must not report ok.
    with _preflight_env(_FakeEngine(conn=_FakeConn(cipher=""))):
        out = preflight.handler({"secret_arn": _ARN})
    assert out["ok"] is False
    assert out["failed_stage"] == "tls"
    assert "not encrypted" in out["error"].lower()


def test_preflight_classifies_tls_handshake_failure():
    from sqlalchemy.exc import OperationalError
    err = OperationalError("stmt", {}, ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    with _preflight_env(_FakeEngine(connect_error=err)):
        out = preflight.handler({"secret_arn": _ARN})
    assert out["ok"] is False
    assert out["failed_stage"] == "tls", out


def test_preflight_classifies_auth_failure():
    from sqlalchemy.exc import OperationalError
    err = OperationalError("stmt", {}, Exception("(1045, \"Access denied for user 'x'\")"))
    with _preflight_env(_FakeEngine(connect_error=err)):
        out = preflight.handler({"secret_arn": _ARN})
    assert out["ok"] is False
    assert out["failed_stage"] == "auth", out


def test_preflight_reports_bad_ca_as_tls_stage():
    def boom(arn):
        raise FileNotFoundError("[Errno 2] No such file or directory: '/no/such/ca.pem'")

    with _patched(preflight, get_secret=lambda arn: _creds()), \
         _patched(preflight.socket,
                  getaddrinfo=lambda h, p, **k: [(2, 1, 6, "", ("13.248.150.187", p))],
                  create_connection=lambda addr, timeout=None: _CtxSock()), \
         _patched(preflight, get_engine=boom):
        out = preflight.handler({"secret_arn": _ARN})
    assert out["ok"] is False
    assert out["failed_stage"] == "tls"
    assert out["error_type"] == "FileNotFoundError"


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — surface setup errors in the runner too
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
