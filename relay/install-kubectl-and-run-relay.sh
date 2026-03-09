#!/usr/bin/env sh
set -eu

BIN_DIR="${RELAY_BIN_DIR:-/opt/relay/bin}"
KUBECTL_BIN_PATH="${KUBECTL_BIN:-${BIN_DIR}/kubectl}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.32.11}"

mkdir -p "${BIN_DIR}"

if [ ! -x "${KUBECTL_BIN_PATH}" ]; then
  ARCH_RAW="$(uname -m)"
  case "${ARCH_RAW}" in
    x86_64|amd64)
      ARCH="amd64"
      ;;
    aarch64|arm64)
      ARCH="arm64"
      ;;
    *)
      echo "Unsupported architecture: ${ARCH_RAW}" >&2
      exit 1
      ;;
  esac

  URL="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"
  echo "Downloading kubectl ${KUBECTL_VERSION} from ${URL}" >&2

  python3 - "${URL}" "${KUBECTL_BIN_PATH}" <<'PY'
import os
import sys
import urllib.request

url = sys.argv[1]
dst = sys.argv[2]

with urllib.request.urlopen(url, timeout=120) as response:
    payload = response.read()

with open(dst, "wb") as fh:
    fh.write(payload)

os.chmod(dst, 0o755)
PY
fi

export PATH="${BIN_DIR}:${PATH}"
exec python3 /opt/relay/app/relay_server.py
