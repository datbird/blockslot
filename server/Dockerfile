# BlockSlot server: Garage (the S3 store) and the web UI in one container.
#
# Build from the repo root, because the image takes engine/slotstore.py and
# engine/saveunits.py as they are, never a copy kept here:
#
#     docker build -f server/Dockerfile -t blockslot-server .
#
# BuildKit reads server/Dockerfile.dockerignore, so only the files below are
# sent to the builder. The web app is the Python standard library only, so
# there is no pip step and no third-party package to keep patched. Garage is a
# static binary, which runs on Alpine as it is.

ARG GARAGE_VERSION=v2.1.0
FROM dxflrs/garage:${GARAGE_VERSION} AS garage

FROM python:3.12-alpine

ARG VERSION=dev
LABEL org.opencontainers.image.title="BlockSlot server" \
      org.opencontainers.image.description="Game save sync store (Garage S3) with a web UI for settings, saves, devices and storage" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NO_COLOR=1

# The Garage image is the static binary at /garage and nothing else.
COPY --from=garage /garage /usr/local/bin/garage

# Writable by any user, so the container also runs with --user 99:100 (the
# runtime Garage config goes here, mode 0600).
RUN mkdir -p /run/blockslot && chmod 1777 /run/blockslot

WORKDIR /app
COPY engine/slotstore.py engine/saveunits.py /app/
COPY server/entrypoint.py /app/entrypoint.py
COPY server/blockslot_server /app/blockslot_server
COPY assets/icon.svg /app/blockslot_server/static/icon.svg
# Garage is AGPL-3.0: its notice and where its source is travel with it.
COPY THIRD-PARTY-NOTICES.md LICENSE /app/

VOLUME /data
# 3900: S3 for devices. 8761: the web UI. Garage's admin API (3903) and RPC
# (3901) stay inside the container.
EXPOSE 3900 8761

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/session' % os.environ.get('BLOCKSLOT_WEB_PORT', '8761'), timeout=4)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "/app/entrypoint.py"]
