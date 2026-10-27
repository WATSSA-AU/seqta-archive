#!/bin/bash
# SEQTA Archive - install or update on Debian/Ubuntu (systemd, no Docker).
#   bash linux/install.sh
# Safe to re-run: updates the code, keeps your config, data and accounts.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo bash "$0" "$@"; fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/seqta-archive
DATA=/srv/seqta-archive
STATE=/var/lib/seqta-archive
ETC=/etc/seqta-archive
say() { echo; echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

command -v systemctl >/dev/null || die "systemd is required"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu; see docs/INSTALL-linux.md for others"

say "packages"
PKGS="python3 python3-venv libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core"
# shellcheck disable=SC2086
if ! dpkg -s $PKGS >/dev/null 2>&1; then
  apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required"

say "service user and folders"
id seqta-archive >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin seqta-archive
install -d -o root -g seqta-archive -m 0750 "$APP" "$DATA" "$ETC"
install -d -o seqta-archive -g seqta-archive -m 0700 "$STATE"

say "application"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q --upgrade "${SRC}[pdf]"
chown -R root:seqta-archive "$APP"
chmod -R g+rX,o= "$APP"
ln -sf "$APP/venv/bin/seqta-archive" /usr/local/bin/seqta-archive

say "config ($ETC/seqta-archive.env)"
if [ ! -f "$ETC/seqta-archive.env" ]; then
  install -m 0640 -o root -g seqta-archive "$SRC/linux/seqta-archive.env.example" "$ETC/seqta-archive.env"
  echo "   created - edit SCHOOL_NAME and the sign-in settings, then: sudo systemctl restart seqta-archive"
else
  echo "   kept existing"
fi

say "systemd"
install -m 0644 "$SRC/linux/seqta-archive.service" /etc/systemd/system/seqta-archive.service
systemctl daemon-reload
systemctl enable seqta-archive >/dev/null
if [ -f "$DATA/seqta.db" ]; then
  systemctl restart seqta-archive
  sleep 2
  systemctl --no-pager --lines=5 status seqta-archive || true
else
  echo "   no database yet - import your export next: bash linux/import.sh /path/to/au_wa_yourschool"
fi

say "check"
sudo -u seqta-archive env SEQTA_CONFIG="$ETC/seqta-archive.env" "$APP/venv/bin/seqta-archive" check || true

cat <<EOF

Next:
  1. bash linux/import.sh /path/to/au_wa_yourschool      (if you haven't yet)
  2. sudo -u seqta-archive seqta-archive user add yourname --admin   (local sign-in mode)
  3. Put HTTPS in front (docs/INSTALL-linux.md) and browse to it.
EOF
