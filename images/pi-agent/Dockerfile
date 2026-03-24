FROM quay.io/nebari/nebari-data-science-pack-jupyterlab:latest

USER root

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Base image already includes node/npm/git.
# Add ttyd runtime and common CLI tools used by skills.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ttyd sudo curl ca-certificates ffmpeg \
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

# Install Pi CLI globally (provides `pi` binary) and pi-self-learning package.
# Wrap `pi` so pi-self-learning is auto-loaded (works even when chart command is just `pi ...`).
RUN npm install -g @mariozechner/pi-coding-agent pi-self-learning \
    && mkdir -p /usr/local/lib/node_modules \
    && ln -sfn "$(npm root -g)/pi-self-learning" /usr/local/lib/node_modules/pi-self-learning \
    && PI_BIN="$(command -v pi)" \
    && mv "$PI_BIN" "${PI_BIN}.real" \
    && install -m 0755 /dev/stdin "$PI_BIN" <<'EOF'
#!/usr/bin/env sh
set -eu

SELF_LEARNING_EXT="/usr/local/lib/node_modules/pi-self-learning/extensions/self-learning.ts"
OPENAI_CODEX_DEVICE_EXT="/opt/nebari/extensions/openai-codex-device-auth.ts"
SESSION_SHARE_EXT="/opt/nebari/extensions/session-share.ts"
PI_BIN="$(command -v pi)"
REAL_PI="${PI_BIN}.real"

if [ ! -x "$REAL_PI" ]; then
  echo "pi wrapper error: missing real binary at $REAL_PI" >&2
  exit 1
fi

# Prepend extensions only when present and not already passed.
if [ -f "$OPENAI_CODEX_DEVICE_EXT" ]; then
  found=0
  for arg in "$@"; do
    [ "$arg" = "$OPENAI_CODEX_DEVICE_EXT" ] && found=1 && break
  done
  [ "$found" -eq 1 ] || set -- -e "$OPENAI_CODEX_DEVICE_EXT" "$@"
fi

if [ -f "$SESSION_SHARE_EXT" ]; then
  found=0
  for arg in "$@"; do
    [ "$arg" = "$SESSION_SHARE_EXT" ] && found=1 && break
  done
  [ "$found" -eq 1 ] || set -- -e "$SESSION_SHARE_EXT" "$@"
fi

if [ -f "$SELF_LEARNING_EXT" ]; then
  found=0
  for arg in "$@"; do
    [ "$arg" = "$SELF_LEARNING_EXT" ] && found=1 && break
  done
  [ "$found" -eq 1 ] || set -- -e "$SELF_LEARNING_EXT" "$@"
fi

exec "$REAL_PI" "$@"
EOF

# Install gog CLI (Google Workspace CLI) used by the gog skill.
RUN set -eux; \
    GOG_VERSION="0.12.0"; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
      amd64) GOG_ARCH="linux_amd64"; GOG_SHA="a03fccbd67ea2e59a26a56e92de8918577f4bebe4b2f946823419777827cdab2" ;; \
      arm64) GOG_ARCH="linux_arm64"; GOG_SHA="d7f20494d7eb0e8716631853d055ccbb368c7b81cb8165f55b45884bccb67b4b" ;; \
      *) echo "Unsupported arch for gog: $ARCH"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gogcli.tar.gz "https://github.com/steipete/gogcli/releases/download/v${GOG_VERSION}/gogcli_${GOG_VERSION}_${GOG_ARCH}.tar.gz"; \
    echo "${GOG_SHA}  /tmp/gogcli.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gogcli.tar.gz -C /tmp; \
    install -m 0755 /tmp/gog /usr/local/bin/gog; \
    rm -f /tmp/gog /tmp/gogcli.tar.gz

# Install uv/uvx for markdown-converter skill (uvx markitdown ...).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && install -m 0755 /root/.local/bin/uv /usr/local/bin/uv \
    && if [ -f /root/.local/bin/uvx ]; then install -m 0755 /root/.local/bin/uvx /usr/local/bin/uvx; else ln -sf /usr/local/bin/uv /usr/local/bin/uvx; fi

# Bake shared skills from local repository files.
COPY skills/shared-skills /opt/nebari/baked-skills/shared-skills

# Install deps for baked web-browser helper scripts.
RUN if [ -f /opt/nebari/baked-skills/shared-skills/web-browser/scripts/package.json ]; then \
      npm --prefix /opt/nebari/baked-skills/shared-skills/web-browser/scripts install --omit=dev && \
      chmod +x /opt/nebari/baked-skills/shared-skills/web-browser/scripts/*.js; \
    fi

RUN if [ -f /opt/nebari/baked-skills/shared-skills/video-frames/scripts/frame.sh ]; then \
      chmod +x /opt/nebari/baked-skills/shared-skills/video-frames/scripts/frame.sh; \
    fi

# Browser smoke-check helper used by deploy verification.
COPY images/pi-agent/pi-browser-smoke.py /opt/nebari/bin/pi-browser-smoke.py
RUN install -m 0755 /opt/nebari/bin/pi-browser-smoke.py /usr/local/bin/pi-browser-smoke

# Custom Pi extensions (Codex auth override + session sharing commands).
COPY extensions/openai-codex-device-auth.ts /opt/nebari/extensions/openai-codex-device-auth.ts
COPY extensions/session-share.ts /opt/nebari/extensions/session-share.ts

# Basic sanity checks at build time.
RUN ttyd --version \
    && pi --version \
    && test -f "$(npm root -g)/pi-self-learning/extensions/self-learning.ts" \
    && test -f /usr/local/lib/node_modules/pi-self-learning/extensions/self-learning.ts \
    && gog --help >/dev/null \
    && ffmpeg -version >/dev/null \
    && uv --version >/dev/null \
    && uvx --help >/dev/null \
    && kubectl version --client=true --output=yaml >/dev/null \
    && python -c "import playwright" \
    && command -v pi-browser-smoke >/dev/null \
    && test -f /opt/nebari/extensions/openai-codex-device-auth.ts \
    && test -f /opt/nebari/extensions/session-share.ts \
    && test -f /opt/nebari/baked-skills/shared-skills/jhub-deploy/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/observability/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/web-browser/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/web-browser/scripts/start.js \
    && test -f /opt/nebari/baked-skills/shared-skills/gog/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/notion/SKILL.md \
    && test -f /opt/nebari/baked-skills/shared-skills/video-frames/SKILL.md \
    && test -x /opt/nebari/baked-skills/shared-skills/video-frames/scripts/frame.sh \
    && test -f /opt/nebari/baked-skills/shared-skills/markdown-converter/SKILL.md \
    && ! command -v nebari_app_deploy >/dev/null 2>&1 \
    && ! command -v nebari_app_status >/dev/null 2>&1 \
    && ! command -v nebari_app_logs >/dev/null 2>&1 \
    && ! command -v nebari_app_stop >/dev/null 2>&1 \
    && ! command -v nebari_app_delete >/dev/null 2>&1 \
    && ! command -v nebari_app_doctor >/dev/null 2>&1

USER 1000
