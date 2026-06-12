# Multi-arch Home Assistant base image (Alpine + s6-overlay + bashio).
# Pin to a specific release for reproducible builds if you prefer.
ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

# Surfaced to the app as the displayed version string.
ARG BUILD_VERSION="dev"
ENV BUILD_VERSION=${BUILD_VERSION}

# Python runtime (the plain base image is Alpine without Python).
RUN apk add --no-cache python3 py3-pip

# Python dependencies. --break-system-packages: install into the system
# interpreter inside this throwaway container image (PEP 668 / Alpine).
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Application code.
COPY app /app
COPY gunicorn.conf.py /app/gunicorn.conf.py

# Startup script.
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
