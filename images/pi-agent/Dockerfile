FROM quay.io/nebari/nebari-data-science-pack-jupyterlab:latest

USER root

# Base image already includes node/npm/curl/git.
# Only add ttyd + jhsingle-native-proxy + Pi CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ttyd \
    && rm -rf /var/lib/apt/lists/*

# jhsingle-native-proxy is used as the HTTP wrapper in front of ttyd
RUN (python -m pip --version >/dev/null 2>&1 || python -m ensurepip --upgrade) \
    && python -m pip install --no-cache-dir jhsingle-native-proxy

# Install Pi CLI globally (provides `pi` binary)
RUN npm install -g @mariozechner/pi-coding-agent

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
    && nebari_app_deploy --help >/dev/null \
    && nebari_app_status --help >/dev/null \
    && nebari_app_logs --help >/dev/null \
    && nebari_app_stop --help >/dev/null \
    && nebari_app_delete --help >/dev/null \
    && nebari_app_doctor --help >/dev/null

USER 1000
