from contextlib import contextmanager

from app.services import mfa_service
from app.utils import strategy_runtime_logs


class _CaptureCursor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def close(self):
        self.closed = True


class _CaptureConn:
    def __init__(self):
        self.cursor_obj = _CaptureCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


@contextmanager
def _capture_connection(conn):
    yield conn


class _FakeTotp:
    def __init__(self, secret):
        self.secret = secret

    def provisioning_uri(self, name, issuer_name):
        return f"otpauth://totp/{issuer_name}:{name}?secret={self.secret}"


class _FakePyotp:
    @staticmethod
    def random_base32():
        return "ABCDEFGHIJKLMNOP"

    TOTP = _FakeTotp


def test_append_strategy_log_uses_parent_exists_guard(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setattr(strategy_runtime_logs, "get_db_connection", lambda: _capture_connection(conn))

    strategy_runtime_logs.append_strategy_log(3347, "info", "Strategy execution loop exited")

    sql, params = conn.cursor_obj.calls[0]
    assert "WHERE EXISTS" in sql
    assert "qd_strategies_trading" in sql
    assert params[0] == 3347
    assert params[-1] == 3347
    assert conn.committed
    assert conn.cursor_obj.closed


def test_mfa_start_setup_returns_user_id_not_missing_id(monkeypatch):
    conn = _CaptureConn()
    monkeypatch.setenv("MFA_ENABLED", "true")
    monkeypatch.setattr(mfa_service.MfaService, "ensure_schema", lambda self: None)
    monkeypatch.setattr(mfa_service, "get_db_connection", lambda: _capture_connection(conn))
    monkeypatch.setattr(mfa_service, "encrypt_credential_blob", lambda secret: f"encrypted:{secret}")

    service = mfa_service.MfaService()
    monkeypatch.setattr(service, "_load_totp_libs", lambda: (_FakePyotp, object()))
    monkeypatch.setattr(service, "_make_qr_data_url", lambda _qrcode, _uri: "data:image/png;base64,test")

    result = service.start_setup(9383, "user@example.com")

    sql, params = conn.cursor_obj.calls[0]
    assert "RETURNING user_id" in sql
    assert "RETURNING id" not in sql
    assert params == (9383, "encrypted:ABCDEFGHIJKLMNOP")
    assert result["secret"] == "ABCDEFGHIJKLMNOP"
    assert conn.committed
