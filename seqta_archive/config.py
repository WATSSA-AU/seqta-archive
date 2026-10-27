"""
Settings, from environment variables or a KEY=VALUE config file.

The config file is found at $SEQTA_CONFIG, else the platform default:
  Linux/macOS: /etc/seqta-archive/seqta-archive.env
  Windows:     %ProgramData%\\SEQTA Archive\\seqta-archive.env
Environment variables always win over the file.
"""

import os
import secrets
import sys

IS_WINDOWS = sys.platform.startswith("win")


def _platform_base():
    if IS_WINDOWS:
        return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "SEQTA Archive")
    return None


def default_config_path():
    if IS_WINDOWS:
        return os.path.join(_platform_base(), "seqta-archive.env")
    return "/etc/seqta-archive/seqta-archive.env"


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ without overriding existing values."""
    try:
        with open(path, encoding="utf-8-sig") as f:   # -sig: Windows PowerShell writes a BOM
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k, v)
        return True
    except FileNotFoundError:
        return False


CONFIG_PATH = os.environ.get("SEQTA_CONFIG") or default_config_path()
CONFIG_LOADED = load_env_file(CONFIG_PATH)


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# ---------------------------------------------------------------- paths
if IS_WINDOWS:
    _DATA_DEFAULT = os.path.join(_platform_base(), "data")
    _STATE_DEFAULT = os.path.join(_platform_base(), "state")
else:
    _DATA_DEFAULT = "/srv/seqta-archive"
    _STATE_DEFAULT = "/var/lib/seqta-archive"

DATA_DIR = env("SEQTA_DATA_DIR", _DATA_DEFAULT)
DB_PATH = env("SEQTA_DB", os.path.join(DATA_DIR, "seqta.db"))
FILES_ROOT = env("SEQTA_FILES", os.path.join(DATA_DIR, "files"))
STATE_DIR = env("SEQTA_STATE_DIR", _STATE_DEFAULT)
# SEQTA_AUDIT_DB kept for installs that predate the shared state DB
STATE_DB = env("SEQTA_STATE_DB", env("SEQTA_AUDIT_DB", os.path.join(STATE_DIR, "state.db")))

# ---------------------------------------------------------------- server
HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8080"))
THREADS = int(env("THREADS", "8"))
URL_PREFIX = env("URL_PREFIX", "").rstrip("/")
if URL_PREFIX and not URL_PREFIX.startswith("/"):
    URL_PREFIX = "/" + URL_PREFIX

# ---------------------------------------------------------------- school / display
SCHOOL_NAME = env("SCHOOL_NAME", "Our school")
SCHOOL_SHORT = env("SCHOOL_SHORT", "")
ARCHIVE_NOTE = env(
    "ARCHIVE_NOTE",
    "This is the full export SEQTA supplied when the school stopped using SEQTA, "
    "loaded unchanged: nothing has been edited or summarised.")
TIMEZONE = env("TIMEZONE", "Australia/Perth")

# ---------------------------------------------------------------- auth
AUTH_MODE = env("AUTH_MODE", "local").strip().lower()      # local | proxy
if AUTH_MODE not in ("local", "proxy"):
    raise SystemExit(f"AUTH_MODE must be 'local' or 'proxy', not {AUTH_MODE!r}")

# local accounts
REQUIRE_TOTP = env_bool("REQUIRE_TOTP", False)
SESSION_HOURS = float(env("SESSION_HOURS", "8"))
COOKIE_SECURE = env_bool("COOKIE_SECURE", True)
MIN_PASSWORD = int(env("MIN_PASSWORD_LENGTH", "12"))
LOCKOUT_ATTEMPTS = int(env("LOCKOUT_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(env("LOCKOUT_MINUTES", "15"))

# proxy mode (oauth2-proxy or similar sets X-Forwarded-* headers)
TRUSTED_PROXIES = env_list("TRUSTED_PROXIES", "127.0.0.1,::1")
ACCESS_GROUPS = set(env_list("ACCESS_GROUPS", "seqta-archive-users,seqta-archive-admin"))
ADMIN_GROUPS = set(env_list("ADMIN_GROUPS", "seqta-archive-admin"))
OIDC_CLIENT_ID = env("OIDC_CLIENT_ID", env("KEYCLOAK_CLIENT_ID", ""))

# ---------------------------------------------------------------- PDF
PDF_BROWSER = env("PDF_BROWSER", "")          # path to Edge/Chrome if WeasyPrint isn't available


def secret_key():
    """SECRET_KEY from the environment, else a random key persisted in the state dir."""
    k = env("SECRET_KEY")
    if k:
        return k
    path = os.path.join(STATE_DIR, "secret_key")
    try:
        with open(path, encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    except FileNotFoundError:
        pass
    os.makedirs(STATE_DIR, exist_ok=True)
    k = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(k)
    return k
