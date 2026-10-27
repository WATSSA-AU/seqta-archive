# Security and privacy

## Reporting a vulnerability

Please **don't** open a public issue. Use GitHub's private vulnerability reporting (**Security → Report a vulnerability**) on this repository. Include steps to reproduce, and **never include real student data**.

## Running it responsibly

The archive gives authorised staff easy access to some of the most sensitive records a school holds: medical and mental-health notes, custody arrangements, behaviour histories, and family contact details. Treat the deployment accordingly.

**Before go-live**

- **Know your obligations.** Check your system's or sector's records-management, retention and privacy requirements before you deploy, and decide who signs off on access.
- **Keep access narrow.** Give it to the people who need historical records, typically school leaders, year coordinators and student services. Every user sees every student's full record. That's the point of the tool, but it makes the access list important.
- **Turn on two-step sign-in** (`REQUIRE_TOTP=1`) when using built-in accounts, or use your identity provider's MFA in proxy mode.
- **Use HTTPS only.** Keep the app on `127.0.0.1` behind an HTTPS reverse proxy. Never publish it over plain HTTP, and think carefully before making it reachable from outside your network.

**Once it's live**

- **Review the audit log.** The **Audit** page shows who opened which student's record and what they downloaded. Check it periodically, and investigate anything unexpected.
- **Clean up copies.** The raw export, test builds, "viewer" HTML files and downloaded bundles are all full copies of the data. After go-live, delete the ones you don't need and keep one master copy of the export somewhere access-controlled.
- **Back up two things:** the database directory (or keep the export so you can rebuild it), and the state directory, which holds accounts, the audit log and the session key.
- **Retention.** When the records reach the end of their retention period, delete the archive, its backups and the export.

## How it's built

- **Read-only data.** The SEQTA database is opened read-only. Nothing in the app can change a record.
- **Sandboxed email.** Archived emails and messages are rendered in a sandboxed frame. Scripts, forms, links and remote images are off, which also blocks tracking pixels.
- **Built-in accounts.**
  - Passwords are stored as scrypt hashes.
  - Optional TOTP, with replay protection.
  - Lockout after repeated failures.
  - Sessions are signed, HTTP-only and SameSite, expire after `SESSION_HOURS`, and are revoked when the password changes or the account is disabled.
  - Every form has a CSRF token.
- **Proxy mode** accepts identity headers only from `TRUSTED_PROXIES`.
- **File downloads** are restricted to the attachment folder. Non-PDF files are served with a sandboxing Content-Security-Policy.
- **Responses** carry `Cache-Control: no-store`, a strict Content-Security-Policy, `nosniff`, `no-referrer` and same-origin framing.
- **Audit log.** Profile views, record views, correspondence, file downloads, exports, searches, sign-ins and account changes all go to the audit log.

## Known limitations

- **Anything that can reach the app's port directly is trusted.** In proxy mode, a local process (or anything on `127.0.0.1`) can send identity headers. Run it on a host you control, and don't co-host untrusted services on it.
- **Audit records can be lost.** Audit writes are best-effort: if the state database can't be written, the page still loads and the failure goes to the server log. Monitor the log.
