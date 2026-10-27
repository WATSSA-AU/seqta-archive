# Install on Linux (Debian/Ubuntu, systemd)

```
git clone https://github.com/WATSSA-AU/seqta-archive.git
cd seqta-archive
bash linux/install.sh
sudo nano /etc/seqta-archive/seqta-archive.env        # SCHOOL_NAME etc.
bash linux/import.sh /path/to/au_wa_yourschool         # 10-30 minutes
sudo -u seqta-archive seqta-archive user add yourname --admin
```

The installer:

- puts the app in `/opt/seqta-archive`;
- uses `/srv/seqta-archive` for the data and attachments;
- uses `/var/lib/seqta-archive` for accounts, the audit log and the session key;
- runs it as the `seqta-archive` systemd service on `127.0.0.1:8080`.

Always run account commands as the service user (`sudo -u seqta-archive …`).

## HTTPS in front

Use whatever you already run. Allow responses up to 5 minutes, because large student bundles take time to build.

**nginx**

```nginx
server {
    listen 443 ssl;
    server_name archive.yourschool.example;
    ssl_certificate     /etc/ssl/certs/archive.pem;
    ssl_certificate_key /etc/ssl/private/archive.key;
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

**Caddy**

```
archive.yourschool.example {
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            response_header_timeout 300s
        }
    }
}
```

**Traefik (file provider)**: a router for your host pointing at `http://<vm>:8080`. To serve the archive under a sub-path of an existing site, set `URL_PREFIX=/seqta` and add `PathPrefix(`/seqta`)` to the router rule.

## Sign in with Keycloak (or another OIDC provider) {#keycloak}

Run oauth2-proxy in front of the app and set `AUTH_MODE=proxy`. The app then trusts `X-Forwarded-User` and `X-Forwarded-Groups`, but only from `TRUSTED_PROXIES` (default `127.0.0.1`), so keep the app on `127.0.0.1` and run oauth2-proxy on the same host.

### Keycloak client setup

This recipe has been tested in production.

1. **Clients → Create client:** OpenID Connect, client ID `seqta-archive`. Turn on Client authentication, tick Standard flow only, and turn on Require PKCE (S256).
   - Valid redirect URI: `https://<site>/oauth2/callback`. If you use `URL_PREFIX` (for example `/seqta`) with oauth2-proxy's `proxy_prefix = "/seqta/oauth2"`, use `https://<site>/seqta/oauth2/callback` instead.
   - Web origin: `https://<site>`.
2. **Roles tab:** create the client roles `seqta-archive-users` and `seqta-archive-admin`. Keep them plain roles: **don't add associated roles**. Associated roles make the role composite, and anyone holding it would inherit whatever you associated.
3. **Client scopes → `<client>-dedicated`:**
   - Add a **User Client Role** mapper: Client ID = this client, Multivalued On, Token Claim Name `groups`, add to ID token, access token and userinfo.
   - Add an **Audience** mapper: Included Client Audience = this client, and pick it from the list rather than typing it, or it saves blank. Add to access token.
   - On the **Scope** tab, turn Full scope allowed **Off**.
4. **Groups → (your deputies', year coordinators' and student services' groups) → Role mapping → Assign role → Filter by clients:** assign `seqta-archive-users`. Assign `seqta-archive-admin` to your IT group.
5. **Check it:** go to **Client scopes → Evaluate**, pick a user, and open Generated access token. You should see `"groups": ["seqta-archive-users"]`, and `aud` should contain your client.

### oauth2-proxy

Use `docker/oauth2-proxy.cfg` as the template:

- Set `upstreams = ["http://127.0.0.1:8080/"]` and `http_address = "127.0.0.1:4180"`.
- Point your HTTPS proxy at `127.0.0.1:4180`.
- Keep `session_store_type = "redis"`. Keycloak tokens make cookie sessions large enough to trip header-size limits on some networks.
- Set `upstream_timeout = "300s"` if your oauth2-proxy version supports it (`oauth2-proxy --help | grep upstream-timeout`).

In `/etc/seqta-archive/seqta-archive.env`:

```
AUTH_MODE=proxy
TRUSTED_PROXIES=127.0.0.1,::1
OIDC_CLIENT_ID=seqta-archive
```
