# Install with Docker

You need Docker Engine with Compose v2.24.4 or newer (`docker compose version`). Disk space needed is about 1.5 times the size of your export.

## 1. Get the files

```
git clone https://github.com/WATSSA-AU/seqta-archive.git
cd seqta-archive/docker
cp .env.example .env
```

Edit `.env`: set `SCHOOL_NAME`, `SCHOOL_SHORT` and `SITE_ADDRESS` (the name staff will browse to).

## 2. Import your export

Copy (or move) the export folder into `docker/export/`, so you have `docker/export/au_wa_yourschool/data/…`. Then lock both folders down to the container's user (uid 10001), so other accounts on the host can't read student records:

```
sudo install -d -o 10001 -g 10001 -m 0700 data
sudo chown -R 10001:10001 export && sudo chmod -R go-rwx export
docker compose run --rm build
```

Large schools take 10–30 minutes. When it finishes it prints `done: /data/seqta.db (x.xx GB)`.

Attachments stay in `export/au_wa_yourschool/files`, and the app finds them there.

## 3. Start it and create an administrator

```
docker compose up -d
docker compose exec archive seqta-archive user add yourname --admin
docker compose exec archive seqta-archive check
```

## 4. HTTPS

The archive only listens on `127.0.0.1:8080` until you put HTTPS in front of it. Choose one option:

- **Caddy, included.** Run `docker compose --profile https up -d`. By default Caddy uses its own certificate authority, so browsers will warn until that CA is trusted. To use your school's certificate instead, put `cert.pem` and `key.pem` in `docker/certs/` and change the `tls` line in `docker/Caddyfile`. If `SITE_ADDRESS` is a public DNS name, remove the `tls` line and Caddy will get a Let's Encrypt certificate automatically.
- **Your existing reverse proxy.** Traefik, nginx, IIS, a load balancer and so on. Proxy to `http://<docker host>:8080` (change it with `HOST_PORT`; set `BIND=0.0.0.0` only if that proxy runs on another host and your firewall restricts who can reach the port). Allow responses to take up to 5 minutes, because large bundles take a while to build.

## Sign in with your identity provider instead (optional)

1. Register an OIDC client with redirect URI `https://<site>/oauth2/callback`. Make sure tokens carry a `groups` claim containing `seqta-archive-users` for people who should have access, and `seqta-archive-admin` for those who also get the audit log. Keycloak setup is in [INSTALL-linux.md](INSTALL-linux.md#keycloak).
2. Edit `docker/oauth2-proxy.cfg` (issuer, client ID, redirect URL).
3. In `.env`, set `OIDC_CLIENT_SECRET`, `OIDC_COOKIE_SECRET` and `UPSTREAM=oauth2-proxy:4180`.
4. Start it:
   ```
   docker compose -f compose.yaml -f compose.oidc.yaml --profile https up -d
   ```

## Updating

```
git pull
docker compose build
docker compose up -d
```

Accounts and the audit log live in the `state` volume and survive updates. Back that volume up.
