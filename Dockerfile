FROM python:3.12-slim-bookworm

# Pango/HarfBuzz for WeasyPrint (PDF reports); DejaVu as a fallback font
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/seqta-archive
COPY pyproject.toml README.md LICENSE ./
COPY seqta_archive ./seqta_archive
RUN pip install --no-cache-dir ".[pdf]" \
 && useradd --system --uid 10001 --home-dir /state --shell /usr/sbin/nologin archive \
 && mkdir -p /state /data \
 && chown archive:archive /state

ENV SEQTA_DATA_DIR=/data \
    SEQTA_STATE_DIR=/state \
    SEQTA_CONFIG=/state/seqta-archive.env \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/state"]
EXPOSE 8080
USER archive

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080' + os.environ.get('URL_PREFIX','') + '/healthz', timeout=4)"

ENTRYPOINT ["seqta-archive"]
CMD ["serve"]
