# Strix security agent (https://docs.strix.ai/quickstart)
# Self-contained: `docker build` works standalone, and scripts/generate.py
# translates it into the VM image build (after the spec's roles run).
# Platform deps live here on purpose so the file is testable with docker.
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends pipx \
 && rm -rf /var/lib/apt/lists/*
ENV PIPX_HOME=/opt/pipx
ENV PIPX_BIN_DIR=/usr/local/bin
ENV PATH=/usr/local/bin:/usr/bin:/bin
RUN pipx install strix-agent
