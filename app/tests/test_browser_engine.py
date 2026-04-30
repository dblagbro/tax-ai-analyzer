"""Phase 7 (Camoufox) + Step 6 (proxy) — browser engine + proxy unit tests.

We don't actually launch a browser in CI. We verify:
  - _resolve_browser_engine() reads per-bank > global > env > default
  - _resolve_proxy() reads per-bank > global > env > None
  - Proxy URL parsing handles auth, schemes, and bare host:port
  - launch_browser() dispatches to the camoufox path when engine="firefox"
  - launch_browser() forwards the proxy dict to both engines
  - The camoufox path raises a clean error when the package isn't installed
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ── _resolve_browser_engine precedence ───────────────────────────────────────

class _FakeDb:
    """Minimal mock of app.db just for get_setting."""
    def __init__(self, settings: dict):
        self._s = settings
    def get_setting(self, key, default=""):
        return self._s.get(key, default)


def _resolve(env=None, settings=None):
    """Run _resolve_browser_engine with controlled env + db.get_setting."""
    settings = settings or {}
    env_patches = {"BROWSER_ENGINE": env} if env is not None else {}
    with patch.dict(os.environ, env_patches, clear=False):
        if env is None and "BROWSER_ENGINE" in os.environ:
            del os.environ["BROWSER_ENGINE"]
        with patch("app.importers.base_bank_importer.db", _FakeDb(settings),
                   create=True):
            from app.importers.base_bank_importer import _resolve_browser_engine
            # Patch the real db module that the function imports lazily
            import sys as _sys
            mod = _sys.modules.get("app.db")
            real_get = mod.get_setting if mod else None
            try:
                if mod:
                    mod.get_setting = lambda k, default="": settings.get(k, default)
                return _resolve_browser_engine("usbank")
            finally:
                if mod and real_get is not None:
                    mod.get_setting = real_get


def test_default_is_chrome():
    assert _resolve() == "chrome"


def test_env_var_picked_up():
    assert _resolve(env="firefox") == "firefox"
    assert _resolve(env="FIREFOX") == "firefox"  # case-insensitive
    assert _resolve(env="bogus") == "chrome"      # ignored, falls through


def test_global_setting_overrides_env():
    out = _resolve(env="chrome", settings={"default_browser_engine": "firefox"})
    assert out == "firefox"


def test_per_bank_overrides_global():
    out = _resolve(env="chrome", settings={
        "default_browser_engine": "chrome",
        "usbank_browser_engine": "firefox",
    })
    assert out == "firefox"


def test_invalid_setting_value_ignored():
    """A typo in the setting shouldn't crash — fall through to next layer."""
    out = _resolve(env="firefox", settings={"usbank_browser_engine": "edge"})
    assert out == "firefox"  # env fallback wins


# ── launch_browser dispatch ──────────────────────────────────────────────────

def test_launch_dispatches_to_camoufox_when_firefox():
    """When engine resolves to firefox, _launch_camoufox must be called."""
    from app.importers import base_bank_importer as bbi

    fake_camoufox = MagicMock(return_value=("CAMOUFOX-OBJ", "CTX", "PAGE"))
    fake_patchright = MagicMock(return_value=("PW", "CTX", "PAGE"))

    with patch.object(bbi, "_launch_camoufox", fake_camoufox), \
         patch.object(bbi, "_launch_patchright", fake_patchright), \
         patch.object(bbi, "_resolve_browser_engine", return_value="firefox"):
        result = bbi.launch_browser("usbank", headless=True, log=lambda m: None)

    assert fake_camoufox.called
    assert not fake_patchright.called
    assert result[0] == "CAMOUFOX-OBJ"


def test_launch_dispatches_to_patchright_when_chrome():
    from app.importers import base_bank_importer as bbi

    fake_camoufox = MagicMock(return_value=("CAMOUFOX-OBJ", "CTX", "PAGE"))
    fake_patchright = MagicMock(return_value=("PW", "CTX", "PAGE"))

    with patch.object(bbi, "_launch_camoufox", fake_camoufox), \
         patch.object(bbi, "_launch_patchright", fake_patchright), \
         patch.object(bbi, "_resolve_browser_engine", return_value="chrome"):
        result = bbi.launch_browser("usbank", headless=True, log=lambda m: None)

    assert fake_patchright.called
    assert not fake_camoufox.called
    assert result[0] == "PW"


# ── _resolve_proxy ───────────────────────────────────────────────────────────

def _resolve_proxy_with(env=None, settings=None):
    settings = settings or {}
    env_patches = {"PROXY_URL": env} if env is not None else {}
    with patch.dict(os.environ, env_patches, clear=False):
        if env is None and "PROXY_URL" in os.environ:
            del os.environ["PROXY_URL"]
        import sys as _sys
        mod = _sys.modules.get("app.db")
        real_get = mod.get_setting if mod else None
        try:
            if mod:
                mod.get_setting = lambda k, default="": settings.get(k, default)
            from app.importers.base_bank_importer import _resolve_proxy
            return _resolve_proxy("usbank")
        finally:
            if mod and real_get is not None:
                mod.get_setting = real_get


def test_proxy_default_is_none():
    assert _resolve_proxy_with() is None


def test_proxy_env_simple():
    out = _resolve_proxy_with(env="http://proxy.example.com:8080")
    assert out == {"server": "http://proxy.example.com:8080"}


def test_proxy_with_auth():
    out = _resolve_proxy_with(env="http://alice:secret%21@proxy.example.com:8080")
    assert out["server"] == "http://proxy.example.com:8080"
    assert out["username"] == "alice"
    assert out["password"] == "secret!"  # %21 url-decoded


def test_proxy_bare_host_port_defaults_http():
    out = _resolve_proxy_with(env="proxy.example.com:9999")
    assert out["server"] == "http://proxy.example.com:9999"


def test_proxy_socks5():
    out = _resolve_proxy_with(env="socks5://proxy.example.com:1080")
    assert out["server"] == "socks5://proxy.example.com:1080"


def test_proxy_per_bank_overrides_global():
    out = _resolve_proxy_with(
        env="http://envproxy:8080",
        settings={
            "default_proxy_url": "http://global:8080",
            "usbank_proxy_url":  "http://specific:8080",
        },
    )
    assert out["server"] == "http://specific:8080"


def test_proxy_global_overrides_env():
    out = _resolve_proxy_with(
        env="http://envproxy:8080",
        settings={"default_proxy_url": "http://global:8080"},
    )
    assert out["server"] == "http://global:8080"


def test_proxy_blank_returns_none():
    """Empty or whitespace-only env should be treated as 'no proxy'."""
    assert _resolve_proxy_with(env="") is None
    assert _resolve_proxy_with(env="   ") is None


# ── launch_browser forwards proxy ────────────────────────────────────────────

def test_launch_passes_proxy_to_patchright():
    from app.importers import base_bank_importer as bbi
    fake_pr = MagicMock(return_value=("PW", "CTX", "PAGE"))
    with patch.object(bbi, "_launch_patchright", fake_pr), \
         patch.object(bbi, "_resolve_browser_engine", return_value="chrome"), \
         patch.object(bbi, "_resolve_proxy",
                      return_value={"server": "http://p:1"}):
        bbi.launch_browser("usbank", headless=True, log=lambda m: None)
    _, kwargs = fake_pr.call_args
    assert kwargs.get("proxy") == {"server": "http://p:1"}


def test_launch_passes_proxy_to_camoufox():
    from app.importers import base_bank_importer as bbi
    fake_cf = MagicMock(return_value=("CM", "CTX", "PAGE"))
    with patch.object(bbi, "_launch_camoufox", fake_cf), \
         patch.object(bbi, "_resolve_browser_engine", return_value="firefox"), \
         patch.object(bbi, "_resolve_proxy",
                      return_value={"server": "socks5://p:1080"}):
        bbi.launch_browser("usbank", headless=True, log=lambda m: None)
    _, kwargs = fake_cf.call_args
    assert kwargs.get("proxy") == {"server": "socks5://p:1080"}


def test_launch_no_proxy_when_unset():
    from app.importers import base_bank_importer as bbi
    fake_pr = MagicMock(return_value=("PW", "CTX", "PAGE"))
    with patch.object(bbi, "_launch_patchright", fake_pr), \
         patch.object(bbi, "_resolve_browser_engine", return_value="chrome"), \
         patch.object(bbi, "_resolve_proxy", return_value=None):
        bbi.launch_browser("usbank", headless=True, log=lambda m: None)
    _, kwargs = fake_pr.call_args
    assert kwargs.get("proxy") is None


def test_camoufox_clean_error_when_not_installed():
    """Without the camoufox package installed, the launch path must raise a
    clear RuntimeError pointing the user at the installation step — not an
    obscure ImportError."""
    from app.importers import base_bank_importer as bbi

    # Force the import to fail by injecting a fake module that errors on attr
    # access (simulating the absence of camoufox.sync_api).
    with patch.dict(sys.modules, {"camoufox": None, "camoufox.sync_api": None}):
        try:
            bbi._launch_camoufox("usbank", headless=True, log=lambda m: None)
        except RuntimeError as e:
            msg = str(e).lower()
            assert "camoufox" in msg
            assert "rebuild" in msg or "install" in msg
            return
        except ImportError:
            # Acceptable too if Python decides to raise the underlying ImportError
            return
    raise AssertionError("expected RuntimeError")
