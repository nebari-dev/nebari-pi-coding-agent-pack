FROM quay.io/nebari/nebari-data-science-pack-jupyterlab:latest

USER root

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Base image already includes node/npm/git.
# Add ttyd runtime and common CLI tools used by skills.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ttyd sudo curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && KUBECTL_ARCH="$(dpkg --print-architecture)" \
    && if [ "$KUBECTL_ARCH" = "amd64" ]; then KUBECTL_ARCH="amd64"; elif [ "$KUBECTL_ARCH" = "arm64" ]; then KUBECTL_ARCH="arm64"; else echo "Unsupported arch for kubectl: $KUBECTL_ARCH"; exit 1; fi \
    && KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# jhsingle-native-proxy is used as the HTTP wrapper in front of ttyd.
RUN (python -m pip --version >/dev/null 2>&1 || python -m ensurepip --upgrade) \
    && python -m pip install --no-cache-dir jhsingle-native-proxy playwright \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

# Install Pi CLI globally (provides `pi` binary).
RUN npm install -g @mariozechner/pi-coding-agent

# Bake shared skills from local repository files.
COPY skills/shared-skills /opt/nebari/baked-skills/shared-skills

# Install deps for baked web-browser helper scripts.
RUN if [ -f /opt/nebari/baked-skills/shared-skills/web-browser/scripts/package.json ]; then \
      npm --prefix /opt/nebari/baked-skills/shared-skills/web-browser/scripts install --omit=dev && \
      chmod +x /opt/nebari/baked-skills/shared-skills/web-browser/scripts/*.js; \
    fi

# Browser smoke-check helper used by deploy verification.
COPY images/pi-agent/pi-browser-smoke.py /opt/nebari/bin/pi-browser-smoke.py
RUN install -m 0755 /opt/nebari/bin/pi-browser-smoke.py /usr/local/bin/pi-browser-smoke

# Basic sanity checks at build time.
RUN ttyd --version \
    && pi --version \
    && kubectl version --client=true --output=yaml >/dev/null \
    && python -c "import playwright" \
    && command -v pi-browser-smoke >/dev/null \
    && test -f /opt/nebari/baked-skills/shared-skills/jhub-deploy/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/observability/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/web-browser/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/web-browser/scripts/start.js \
    && ! command -v nebari_app_deploy >/dev/null 2>&1 \
    && ! command -v nebari_app_status >/dev/null 2>&1 \
    && ! command -v nebari_app_logs >/dev/null 2>&1 \
    && ! command -v nebari_app_stop >/dev/null 2>&1 \
    && ! command -v nebari_app_delete >/dev/null 2>&1 \
    && ! command -v nebari_app_doctor >/dev/null 2>&1

USER 1000
