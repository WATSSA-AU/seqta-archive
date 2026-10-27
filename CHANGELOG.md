# Changelog

## 3.0.0 (first public release)

- Generic package: works with any school's SEQTA offboarding export.
- Built-in accounts: passwords, optional TOTP, lockout, user admin page and CLI.
- Proxy mode for OIDC providers via oauth2-proxy.
- Install paths: Docker Compose (optional Caddy HTTPS and an OIDC overlay), Debian/Ubuntu with systemd, and Windows (PowerShell installer, scheduled task, optional Caddy).
- PDF reports via WeasyPrint, or headless Edge/Chrome on Windows.
- Exports with missing tables or columns show the rest of the record instead of failing.
- A fake-export generator and test suite. CI runs on Linux and Windows and builds the Docker image.

## 2.x (single-school deployment)

- Student profiles, full-text search, correspondence linking, the per-student ZIP bundle (PDF plus attachments), and the audit log.
