FROM quay.io/nebari/nebari-data-science-pack-jupyterlab:latest

USER root

ARG PI_SKILLS_URL
ARG PI_SKILLS_SHA256

# Base image already includes node/npm/git.
# Add ttyd runtime and tooling needed to fetch/extract baked skills artifact.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ttyd curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

# jhsingle-native-proxy is used as the HTTP wrapper in front of ttyd
RUN (python -m pip --version >/dev/null 2>&1 || python -m ensurepip --upgrade) \
    && python -m pip install --no-cache-dir jhsingle-native-proxy

# Install Pi CLI globally (provides `pi` binary)
RUN npm install -g @mariozechner/pi-coding-agent

# Bake shared skills into the image under /opt/nebari/baked-skills/shared-skills.
RUN test -n "$PI_SKILLS_URL" \
    && mkdir -p /opt/nebari/baked-skills \
    && curl -fsSL "$PI_SKILLS_URL" -o /tmp/shared-skills.tar.gz \
    && if [ -n "$PI_SKILLS_SHA256" ]; then echo "$PI_SKILLS_SHA256  /tmp/shared-skills.tar.gz" | sha256sum -c -; fi \
    && tar -xzf /tmp/shared-skills.tar.gz -C /opt/nebari/baked-skills \
    && rm -f /tmp/shared-skills.tar.gz

# Install jhub-app lifecycle wrapper commands used by shared skills.
COPY nebari_app_wrapper.py /opt/nebari/bin/nebari_app_wrapper.py
RUN install -m 0755 /opt/nebari/bin/nebari_app_wrapper.py /usr/local/bin/nebari_app_wrapper \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_deploy \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_status \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_logs \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_stop \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_delete \
    && ln -sf /usr/local/bin/nebari_app_wrapper /usr/local/bin/nebari_app_doctor

# Basic sanity checks at build time
RUN ttyd --version \
    && pi --version \
    && test -d /opt/nebari/baked-skills/shared-skills \
    && nebari_app_deploy --help >/dev/null \
    && nebari_app_status --help >/dev/null \
    && nebari_app_logs --help >/dev/null \
    && nebari_app_stop --help >/dev/null \
    && nebari_app_delete --help >/dev/null \
    && nebari_app_doctor --help >/dev/null

USER 1000
