# Install on Windows

This works on Windows Server 2016 or later, or Windows 10/11. Use a domain-joined server you already patch and back up, not a staff laptop.

## 1. Get the files

Download the latest release ZIP from the repository's Releases page. Right-click it, choose **Properties**, tick **Unblock**, then extract it, for example to `C:\Install\seqta-archive`. (You have to unblock *before* extracting, or Windows will block the scripts.)

Copy your SEQTA export to the server, for example `D:\SEQTA\au_wa_yourschool`.

## 2. Install

Open **PowerShell as administrator**:

```
cd C:\Install\seqta-archive
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -SchoolName "Example Senior High School" -SchoolShort ESHS -Export "D:\SEQTA\au_wa_yourschool"
```

The installer:

- installs Python 3.12 with `winget` if needed (the server needs to reach pypi.org once to install packages);
- installs the app into `C:\Program Files\SEQTA Archive`;
- creates `C:\ProgramData\SEQTA Archive`, restricted to Administrators and SYSTEM, for the data, accounts and audit log;
- builds the database from your export (10–30 minutes) and copies the attachments;
- registers a scheduled task, **SEQTA Archive**, that runs the app at startup;
- asks for the first administrator's username and password.

## 3. Let staff reach it over HTTPS

By default the archive listens on `http://127.0.0.1:8080/`, which is reachable only from the server itself. Choose one option:

- **Built-in Caddy:**
  ```
  .\windows\install.ps1 -Https -SiteAddress archive.yourschool.example -CertFile C:\certs\archive.pem -KeyFile C:\certs\archive.key
  ```
  This uses your school's certificate in PEM format. Without `-CertFile`/`-KeyFile`, Caddy issues its own certificate, and browsers warn until its root certificate is deployed with Group Policy (the installer prints where to find it). The installer also opens port 443 for the Domain and Private firewall profiles.
- **Your existing reverse proxy** (IIS with ARR, a load balancer and so on): proxy to `http://<server>:8080`, and change `HOST` in the settings file so it listens on the right interface.

Don't publish it over plain HTTP.

## Day to day

| Task | How |
|---|---|
| Add or reset users | Sign in as an administrator, then open **Users** |
| Settings | Edit `C:\ProgramData\SEQTA Archive\seqta-archive.env`, then `Stop-ScheduledTask "SEQTA Archive"; Start-ScheduledTask "SEQTA Archive"` |
| Health check | `& "C:\Program Files\SEQTA Archive\venv\Scripts\seqta-archive.exe" check` |
| Re-import an export | `.\windows\import.ps1 -Export D:\SEQTA\au_wa_yourschool` |
| Update the app | Extract the new release, then re-run `.\windows\install.ps1` (settings and data are kept) |
| Remove | `.\windows\uninstall.ps1` (add `-RemoveData` to delete the data and audit log too) |

Back up `C:\ProgramData\SEQTA Archive` with your normal server backups.
