# Configuration

Settings come from environment variables, or from a `KEY=VALUE` file. Environment variables override the file. The file is read from `$SEQTA_CONFIG`, or by default:

- Linux: `/etc/seqta-archive/seqta-archive.env`
- Windows: `C:\ProgramData\SEQTA Archive\seqta-archive.env`

Restart the app after changing settings.

## School

| Setting | Default | |
|---|---|---|
| `SCHOOL_NAME` | `Our school` | Shown on every page and in reports |
| `SCHOOL_SHORT` | | Short name, used in PDF footers and the authenticator-app label |
| `ARCHIVE_NOTE` | (generic) | The "About this archive" paragraph on the home page |
| `TIMEZONE` | `Australia/Perth` | Used for audit times and report timestamps |

## Paths

| Setting | Linux default | Windows default |
|---|---|---|
| `SEQTA_DATA_DIR` | `/srv/seqta-archive` | `%ProgramData%\SEQTA Archive\data` |
| `SEQTA_DB` | `$SEQTA_DATA_DIR/seqta.db` | |
| `SEQTA_FILES` | `$SEQTA_DATA_DIR/files`, falling back to the export's own `files/` folder | |
| `SEQTA_STATE_DIR` | `/var/lib/seqta-archive` | `%ProgramData%\SEQTA Archive\state` |
| `SEQTA_STATE_DB` | `$SEQTA_STATE_DIR/state.db` (accounts and audit log) | |

## Server

| Setting | Default | |
|---|---|---|
| `HOST` | `127.0.0.1` | Keep it on loopback and put HTTPS in front |
| `PORT` | `8080` | |
| `THREADS` | `8` | |
| `URL_PREFIX` | | For example `/seqta`, to serve under a sub-path of an existing site |

## Sign-in

| Setting | Default | |
|---|---|---|
| `AUTH_MODE` | `local` | `local` uses built-in accounts; `proxy` uses an authenticating reverse proxy |
| `REQUIRE_TOTP` | `0` | `1` makes every local user set up an authenticator app at first sign-in. **Recommended.** |
| `SESSION_HOURS` | `8` | |
| `COOKIE_SECURE` | `1` | Set to `0` only for testing on plain HTTP |
| `MIN_PASSWORD_LENGTH` | `12` | |
| `LOCKOUT_ATTEMPTS` / `LOCKOUT_MINUTES` | `5` / `15` | |
| `SECRET_KEY` | (generated) | Session-signing key. If unset, a random key is generated once and stored in the state directory. |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Proxy mode only: IPs, CIDRs or hostnames allowed to send identity headers |
| `ACCESS_GROUPS` | `seqta-archive-users,seqta-archive-admin` | Proxy mode only |
| `ADMIN_GROUPS` | `seqta-archive-admin` | Proxy mode only |
| `OIDC_CLIENT_ID` | | Proxy mode only. Accepts `role:<client>:<role>` group values from Keycloak for this client. |

## PDF

| Setting | |
|---|---|
| `PDF_BROWSER` | Path to Edge or Chrome, if WeasyPrint isn't installed and the browser isn't found automatically |

## Command line

```
seqta-archive build --export PATH [--out FILE] [--no-corr-fts]
seqta-archive serve [--host H] [--port P]
seqta-archive user add NAME [--admin] [--display "Full Name"] [--random | --password-stdin]
seqta-archive user passwd|disable|enable|unlock|reset-totp NAME
seqta-archive user list
seqta-archive check
```
