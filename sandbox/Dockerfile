# sandbox image: node + python toolchain, non-root `agent`, /workspace
FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git ripgrep jq unzip less procps \
        build-essential pkg-config \
        python3 python3-pip python3-venv python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN pip install --no-cache-dir pytest

# replace the image's `node` user (uid 1000) with `agent`
RUN userdel -r node \
    && useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /workspace && chown agent:agent /workspace

RUN git config --system user.name "patchbay" \
    && git config --system user.email "patchbay@localhost" \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory '*'

USER agent
WORKDIR /workspace

CMD ["sleep", "infinity"]
