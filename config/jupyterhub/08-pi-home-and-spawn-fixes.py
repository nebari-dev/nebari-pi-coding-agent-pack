import copy
import inspect
import os
import textwrap
from pathlib import Path

import z2jh

# 1) Home page UX: remove the old "Pi -> /hub/user-redirect/lab/workspaces/pi"
#    entry and ensure "Pi Coding Agent -> /services/pi-launcher/".
services = list(getattr(c.JupyterHub, "services", []) or [])
filtered_services = []
for svc in services:
    info = svc.get("info") if isinstance(svc, dict) else None
    old_pi_service = (
        isinstance(svc, dict)
        and svc.get("name") == "Pi"
        and isinstance(info, dict)
        and info.get("url") == "/hub/user-redirect/lab/workspaces/pi?reset"
    )
    existing_pi_service = isinstance(svc, dict) and svc.get("name") == "Pi Coding Agent"
    old_argo_service = (
        isinstance(svc, dict)
        and isinstance(info, dict)
        and info.get("url") == "/argo"
    )
    if old_pi_service or old_argo_service:
        continue
    if existing_pi_service:
        patched = copy.deepcopy(svc)
        patched_info = patched.get("info") if isinstance(patched.get("info"), dict) else {}
        patched_info.update(
            {
                "name": "Pi Coding Agent",
                "url": "/services/pi-launcher/",
                "external": True,
                "pinned": True,
                "description": "Open your Pi coding agent terminal (or go to launcher if it is not running).",
            }
        )
        patched["display"] = True
        patched["info"] = patched_info
        filtered_services.append(patched)
        continue
    filtered_services.append(svc)

if not any(
    isinstance(svc, dict) and svc.get("name") == "Pi Coding Agent"
    for svc in filtered_services
):
    filtered_services.append(
        {
            "name": "Pi Coding Agent",
            "display": True,
            "info": {
                "name": "Pi Coding Agent",
                "url": "/services/pi-launcher/",
                "external": True,
                "pinned": True,
                "description": "Open your Pi coding agent terminal (or go to launcher if it is not running).",
            },
        }
    )

c.JupyterHub.services = filtered_services

# 1b) jhub-apps UI quick-access fix:
#     Current bundled UI only pins "Environments". Inject a Pi card
#     client-side so Pi appears in Quick Access alongside JupyterLab/VSCode.
custom_templates_dir = Path("/srv/jupyterhub/custom-templates")
custom_templates_dir.mkdir(parents=True, exist_ok=True)
japps_page_template = custom_templates_dir / "japps_page.html"
japps_page_template.write_text(
    """{% extends "page.html" %}
{% block main %}
<div id="root"></div>
<script src="/services/japps/static/js/index.js?v={{version_hash}}"></script>
<link
  rel="stylesheet"
  href="/services/japps/static/css/index.css?v={{version_hash}}"
/>
<script type="text/javascript">
  window.theme = {
    logo: "{{ logo }}",
  };
  document.querySelector(".navbar")?.style.setProperty("display", "none");

  (function ensurePiQuickAccessCard() {
    var CARD_ID = "pi-quick-access-card";
    var PI_TITLE = "Pi Coding Agent";
    var PI_DESC = "Open your Pi coding agent terminal.";
    var PI_LOGO_SVG = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%23ffffff'/><text x='32' y='44' text-anchor='middle' font-size='42' font-family='STIXGeneral,Times New Roman,serif' fill='%230b0b0b'>π</text></svg>";
    var SERVICE_PREFIX = "/services/pi-launcher";
    var USERNAME = "";
    var state = {
      exists: false,
      ready: false,
      stopped: true,
      pending: "",
      profile: "",
    };

    function getCookie(name) {
      var parts = ("; " + document.cookie).split("; " + name + "=");
      if (parts.length !== 2) {
        return "";
      }
      return decodeURIComponent(parts.pop().split(";").shift());
    }

    function ensureStyles() {
      if (document.getElementById("pi-quick-access-styles")) {
        return;
      }
      var style = document.createElement("style");
      style.id = "pi-quick-access-styles";
      style.textContent =
        ".pi-status-chip{display:inline-flex;align-items:center;border-radius:16px;padding:2px 10px;font-size:12px;font-weight:600;line-height:20px;}" +
        ".pi-status-ready{background:#fff;border:1px solid #2e7d32;color:#2e7d32;}" +
        ".pi-status-running{background:#2e7d32;color:#fff;border:1px solid #2e7d32;}" +
        ".pi-status-pending{background:#eab54e;color:#111;border:1px solid #eab54e;}" +
        ".pi-status-unknown{background:#79797c;color:#fff;border:1px solid #79797c;}" +
        ".pi-context-menu{background:transparent!important;border:0!important;box-shadow:none!important;width:auto!important;height:auto!important;top:8px!important;right:8px!important;}" +
        ".pi-context-menu .pi-menu-btn{border:1px solid #e6e6e6;background:#fff;color:#111;font-weight:700;border-radius:50%;width:24px;height:24px;line-height:20px;font-size:16px;padding:0;cursor:pointer;}" +
        ".pi-context-menu .pi-menu-btn:disabled{opacity:0.5;cursor:not-allowed;}" +
        ".pi-context-menu .pi-menu-dropdown{display:none;position:absolute;right:0;top:28px;min-width:124px;background:#fff;border:1px solid #d9d9d9;border-radius:8px;box-shadow:0 8px 20px rgba(0,0,0,0.15);padding:6px;z-index:30;}" +
        ".pi-context-menu.open .pi-menu-dropdown{display:block;}" +
        ".pi-context-menu .pi-menu-item{display:block;width:100%;text-align:left;border:0;background:#fff;padding:8px 10px;border-radius:6px;font-size:12px;cursor:pointer;color:#111;}" +
        ".pi-context-menu .pi-menu-item:hover{background:#f5f5f5;}" +
        ".pi-context-menu .pi-menu-item:disabled{opacity:0.45;cursor:not-allowed;}";
      document.head.appendChild(style);
    }

    function profileLabel(profile) {
      var map = {
        "pi-small": "Small",
        "pi-medium": "Medium",
        "pi-large": "Large",
      };
      if (map[profile]) {
        return map[profile];
      }
      if (!profile) {
        return "";
      }
      var cleaned = String(profile).replace(/^pi-/, "");
      if (!cleaned) {
        return "";
      }
      return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
    }

    function currentStatus() {
      var launcherUrl = SERVICE_PREFIX + "/";
      var userPiUrl =
        USERNAME && state.ready
          ? "/user/" + encodeURIComponent(USERNAME) + "/pi/"
          : launcherUrl;
      if (state.pending) {
        return {
          chipClass: "pi-status-pending",
          text: "Pending",
          href:
            USERNAME && state.exists
              ? "/hub/spawn-pending/" + encodeURIComponent(USERNAME) + "/pi"
              : launcherUrl,
          canStop: true,
        };
      }
      if (state.ready) {
        var size = profileLabel(state.profile);
        return {
          chipClass: "pi-status-running",
          text: size ? "Deployed on " + size : "Deployed",
          href: userPiUrl,
          canStop: true,
        };
      }
      if (state.exists && state.stopped) {
        return {
          chipClass: "pi-status-ready",
          text: "Ready",
          href: launcherUrl,
          canStop: false,
        };
      }
      if (!state.exists) {
        return {
          chipClass: "pi-status-ready",
          text: "Ready",
          href: launcherUrl,
          canStop: false,
        };
      }
      return {
        chipClass: "pi-status-unknown",
        text: "Unknown",
        href: launcherUrl,
        canStop: true,
      };
    }

    function ensureCard() {
      if (document.getElementById(CARD_ID)) {
        return true;
      }
      var sourceCard = document.querySelector(".card");
      if (!sourceCard || !sourceCard.parentElement) {
        return false;
      }

      var clone = sourceCard.cloneNode(true);
      clone.id = CARD_ID;
      clone.classList.add("service");

      var link = clone.querySelector("a[href]");
      if (link) {
        link.setAttribute("href", SERVICE_PREFIX + "/");
      }
      var icon = clone.querySelector("img");
      if (icon) {
        icon.setAttribute("src", "data:image/svg+xml;utf8," + encodeURIComponent(PI_LOGO_SVG));
        icon.setAttribute("alt", "Pi");
      }
      var media = clone.querySelector(".MuiCardMedia-root");
      if (media) {
        media.style.backgroundImage =
          "url('data:image/svg+xml;utf8," + encodeURIComponent(PI_LOGO_SVG) + "')";
        media.style.backgroundColor = "#fff";
        media.style.backgroundSize = "72px 72px";
        media.style.backgroundPosition = "center";
        media.style.backgroundRepeat = "no-repeat";
      }

      clone.querySelectorAll(".chip-container").forEach(function (el) {
        el.remove();
      });
      var oldMenu = clone.querySelector(".context-menu");
      if (oldMenu) {
        oldMenu.remove();
      }

      var overlay = clone.querySelector(".img-overlay, .img-overlay-service");
      if (overlay) {
        overlay.className = "img-overlay-service";
        overlay.innerHTML = "<span style='font-weight:700;font-size:34px;line-height:1'>π</span>";
      }

      var titleNodes = clone.querySelectorAll(".card-content-truncate, .card-title");
      titleNodes.forEach(function (el) {
        if (el && el.textContent !== undefined) {
          el.textContent = PI_TITLE;
        }
      });

      var desc = clone.querySelector(".card-description, .card-description-service");
      if (desc) {
        desc.textContent = PI_DESC;
      }

      var header = clone.querySelector(".card-content-header");
      if (header) {
        var chipWrap = document.createElement("div");
        chipWrap.className = "chip-container";
        chipWrap.innerHTML =
          '<div class="menu-chip"><span id="' +
          CARD_ID +
          '-chip" class="pi-status-chip pi-status-ready">Ready</span></div>';
        header.appendChild(chipWrap);

        var menu = document.createElement("div");
        menu.className = "context-menu pi-context-menu";
        menu.innerHTML =
          '<button type="button" class="pi-menu-btn" title="Menu options" aria-haspopup="true" aria-expanded="false">&#8230;</button>' +
          '<div class="pi-menu-dropdown" role="menu">' +
          '<button type="button" class="pi-menu-item" data-action="stop">Stop</button>' +
          "</div>";
        header.appendChild(menu);
      }

      sourceCard.parentElement.appendChild(clone);
      return true;
    }

    function readStateFromStatus(data) {
      if (!data || typeof data !== "object") {
        return;
      }
      if (typeof data.name === "string" && data.name) {
        USERNAME = data.name;
        var servers = data.servers && typeof data.servers === "object" ? data.servers : {};
        var server = servers.pi;
        if (!server || typeof server !== "object") {
          state.exists = false;
          state.ready = false;
          state.stopped = true;
          state.pending = "";
          state.profile = "";
          return;
        }
        var opts = server.user_options && typeof server.user_options === "object" ? server.user_options : {};
        var pending = server.pending;
        state.exists = true;
        state.ready = !!server.ready;
        state.stopped = !!server.stopped;
        state.pending = pending === null || pending === undefined ? "" : String(pending).trim();
        state.profile = typeof opts.profile === "string" ? opts.profile : "";
        return;
      }

      if (typeof data.username === "string" && data.username) {
        USERNAME = data.username;
      }
      state.exists = !!data.exists;
      state.ready = !!data.ready;
      state.stopped = !!data.stopped;
      var pending2 = data.pending;
      state.pending = pending2 === null || pending2 === undefined ? "" : String(pending2).trim();
      state.profile = typeof data.profile === "string" ? data.profile : "";
    }

    function updateCard() {
      var card = document.getElementById(CARD_ID);
      if (!card) {
        return;
      }
      var status = currentStatus();
      var busy = card.dataset.piActionBusy === "1";
      var link = card.querySelector("a[href]");
      var chip = card.querySelector("#" + CARD_ID + "-chip");
      var stopBtn = card.querySelector('.pi-menu-item[data-action="stop"]');
      var menuBtn = card.querySelector(".pi-menu-btn");
      if (link) {
        link.setAttribute("href", status.href);
      }
      if (chip) {
        chip.className = "pi-status-chip " + (busy ? "pi-status-pending" : status.chipClass);
        chip.textContent = busy ? "Pending" : status.text;
      }
      if (stopBtn) {
        stopBtn.disabled = busy || !status.canStop;
      }
      if (menuBtn) {
        menuBtn.disabled = busy;
      }
    }

    function pollStatus() {
      var xsrf = getCookie("_xsrf");
      var url = "/hub/api/user";
      if (xsrf) {
        url += "?_xsrf=" + encodeURIComponent(xsrf);
      }
      var headers = { Accept: "application/json" };
      if (xsrf) {
        headers["X-XSRFToken"] = xsrf;
      }
      fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        headers: headers,
      })
        .then(function (resp) {
          if (!resp || !resp.ok) {
            throw new Error("hub user status request failed");
          }
          return resp.json();
        })
        .then(function (data) {
          readStateFromStatus(data);
          updateCard();
        })
        .catch(function () {
          updateCard();
        });
    }

    function postAction() {
      if (!USERNAME) {
        return Promise.reject(new Error("missing username"));
      }
      var xsrf = getCookie("_xsrf");
      var url =
        "/hub/api/users/" +
        encodeURIComponent(USERNAME) +
        "/servers/pi?remove=false";
      if (xsrf) {
        url += "&_xsrf=" + encodeURIComponent(xsrf);
      }
      var headers = {
        Accept: "application/json",
      };
      if (xsrf) {
        headers["X-XSRFToken"] = xsrf;
      }
      return fetch(url, {
        method: "DELETE",
        credentials: "same-origin",
        headers: headers,
      }).then(function (resp) {
        if (
          !resp ||
          (resp.status !== 200 &&
            resp.status !== 202 &&
            resp.status !== 204 &&
            resp.status !== 400 &&
            resp.status !== 404)
        ) {
          throw new Error("action request failed");
        }
        return resp.text();
      });
    }

    function bindActions() {
      var card = document.getElementById(CARD_ID);
      if (!card || card.dataset.piActionsBound === "1") {
        return;
      }
      card.dataset.piActionsBound = "1";

      var menu = card.querySelector(".pi-context-menu");
      var menuBtn = card.querySelector(".pi-menu-btn");
      var stopBtn = card.querySelector('.pi-menu-item[data-action="stop"]');
      if (!menu || !menuBtn || !stopBtn) {
        return;
      }

      function closeMenu() {
        menu.classList.remove("open");
        menuBtn.setAttribute("aria-expanded", "false");
      }

      function runAction() {
        if (card.dataset.piActionBusy === "1") {
          return;
        }
        card.dataset.piActionBusy = "1";
        closeMenu();
        updateCard();
        postAction()
          .catch(function () {})
          .finally(function () {
            setTimeout(function () {
              card.dataset.piActionBusy = "0";
              pollStatus();
            }, 1000);
            setTimeout(pollStatus, 3000);
            setTimeout(pollStatus, 6000);
          });
      }

      menuBtn.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (menuBtn.disabled) {
          return;
        }
        var isOpen = menu.classList.toggle("open");
        menuBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
      });

      stopBtn.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        runAction();
      });

      document.addEventListener("click", function (event) {
        if (!menu.contains(event.target)) {
          closeMenu();
        }
      });
    }

    function mount() {
      ensureStyles();
      if (!ensureCard()) {
        return false;
      }
      bindActions();
      updateCard();
      pollStatus();
      return true;
    }

    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (mount() || tries > 120) {
        clearInterval(timer);
      }
    }, 500);

    setInterval(function () {
      if (document.getElementById(CARD_ID)) {
        pollStatus();
      }
    }, 10000);
  })();
</script>
{% endblock %}
"""
)

existing_templates = list(getattr(c.JupyterHub, "template_paths", []) or [])
custom_template_path = str(custom_templates_dir)
if custom_template_path not in [str(p) for p in existing_templates]:
    c.JupyterHub.template_paths = [custom_template_path] + existing_templates

# 2) Spawn/profile fixes:
#    - strip stale `nebula-pi-cli` mounts from all profiles
#    - keep Pi sizes available only on the `pi` named server
#    - ensure `code-server` exists for VSCode on non-pi servers
_orig_profile_list = c.KubeSpawner.profile_list

PI_IMAGE = str(z2jh.get_config("custom.pi-image", "quay.io/nebari/pi-agent:latest") or "quay.io/nebari/pi-agent:latest")
PI_ENV = {
    # Token is injected by chart via hub env -> singleuser profile env.
    "NEBARI_HUB_API_TOKEN": os.environ.get("PI_M4_TOOLS_API_TOKEN", ""),
    "NEBARI_HUB_API_URL": str(z2jh.get_config("custom.pi-hub-api-url", "http://hub:8081/hub/api") or "http://hub:8081/hub/api"),
    "NEBARI_PROXY_URL": str(z2jh.get_config("custom.pi-proxy-url", "http://proxy-public") or "http://proxy-public"),
    # Keep Pi runtime state outside potentially read-only /home mounts.
    "PI_CODING_AGENT_DIR": str(z2jh.get_config("custom.pi-coding-agent-dir", "/tmp/pi-agent") or "/tmp/pi-agent"),
}
PI_CMD = [
    "python",
    "-m",
    "jhsingle_native_proxy.main",
    # Keep auth at the JupyterHub route layer; oauth mode in this package
    # triggers a Tornado async/authenticated mismatch and intermittent blank loads.
    "--authtype=none",
    "--force-alive",
    "--port=8888",
    "--destport=7681",
    "--",
    "ttyd",
    "-W",
    "-P",
    "2",
    "-t",
    "rendererType=dom",
    "-p",
    "7681",
    "--",
    "pi",
]

PI_SPECS_BY_SIZE = {
    "small": {
        "display_name": "Pi Small",
        "description": "Pi agent (2 cpu / 8 GB ram)",
        "cpu_limit": 2,
        "cpu_guarantee": 1.5,
        "mem_limit": "8G",
        "mem_guarantee": "5G",
    },
    "medium": {
        "display_name": "Pi Medium",
        "description": "Pi agent (4 cpu / 16 GB ram)",
        "cpu_limit": 4,
        "cpu_guarantee": 3,
        "mem_limit": "16G",
        "mem_guarantee": "10G",
    },
    "large": {
        "display_name": "Pi Large",
        "description": "Pi agent (8 cpu / 32 GB ram)",
        "cpu_limit": 8,
        "cpu_guarantee": 6,
        "mem_limit": "32G",
        "mem_guarantee": "24G",
    },
}

_pi_profile_overrides = z2jh.get_config("custom.pi-profiles", {}) or {}
if isinstance(_pi_profile_overrides, dict):
    for _size, _override in _pi_profile_overrides.items():
        if _size in PI_SPECS_BY_SIZE and isinstance(_override, dict):
            PI_SPECS_BY_SIZE[_size].update(_override)

PI_RUN_AS_ROOT = bool(z2jh.get_config("custom.pi-run-as-root", False))

code_server_bootstrap_script = textwrap.dedent(
    """
    set -u
    if [ "${JUPYTERHUB_SERVER_NAME:-}" = "pi" ]; then
      exit 0
    fi
    CS_BIN="$HOME/.local/bin/code-server"
    if [ -x "$CS_BIN" ]; then
      exit 0
    fi
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64|amd64) ARCH_TAG="amd64" ;;
      aarch64|arm64) ARCH_TAG="arm64" ;;
      *)
        echo "Unsupported architecture for code-server bootstrap: $ARCH" >&2
        exit 0
        ;;
    esac
    VERSION="4.98.2"
    INSTALL_BASE="$HOME/.local/lib/code-server-$VERSION-linux-$ARCH_TAG"
    mkdir -p "$HOME/.local/bin" "$HOME/.local/lib"
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    ARCHIVE="code-server-$VERSION-linux-$ARCH_TAG.tar.gz"
    URL="https://github.com/coder/code-server/releases/download/v$VERSION/$ARCHIVE"
    curl -fsSL "$URL" -o "$TMP_DIR/$ARCHIVE" || exit 0
    tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR" || exit 0
    rm -rf "$INSTALL_BASE"
    mv "$TMP_DIR/code-server-$VERSION-linux-$ARCH_TAG" "$INSTALL_BASE" || exit 0
    ln -sf "$INSTALL_BASE/bin/code-server" "$CS_BIN" || exit 0
    chmod 0755 "$INSTALL_BASE/bin/code-server" "$CS_BIN" || exit 0
    """
).strip()

def _clean_profile(profile):
    p = copy.deepcopy(profile)
    override = p.get("kubespawner_override") or {}
    if isinstance(override, dict):
        override.pop("fs_gid", None)

    pod_cfg = override.get("extra_pod_config") or {}
    if isinstance(pod_cfg, dict):
        volumes = pod_cfg.get("volumes")
        if isinstance(volumes, list):
            pod_cfg["volumes"] = [
                v
                for v in volumes
                if not (isinstance(v, dict) and v.get("name") == "nebula-pi-cli")
            ]

        # Avoid pod-level fsGroup on large RWX volumes; it can trigger very slow ownership walks.
        sec_ctx = pod_cfg.get("securityContext")
        if isinstance(sec_ctx, dict):
            sec_ctx.pop("fsGroup", None)
            sec_ctx.pop("fsGroupChangePolicy", None)
            if sec_ctx:
                pod_cfg["securityContext"] = sec_ctx
            else:
                pod_cfg.pop("securityContext", None)

        override["extra_pod_config"] = pod_cfg

    container_cfg = override.get("extra_container_config") or {}
    if isinstance(container_cfg, dict):
        mounts = container_cfg.get("volumeMounts")
        if isinstance(mounts, list):
            container_cfg["volumeMounts"] = [
                m
                for m in mounts
                if not (
                    isinstance(m, dict)
                    and (
                        m.get("name") == "nebula-pi-cli"
                        or m.get("mountPath") == "/usr/local/bin/pi"
                    )
                )
            ]
        override["extra_container_config"] = container_cfg

    hooks = override.get("lifecycle_hooks")
    if isinstance(hooks, dict):
        post_start = hooks.get("postStart")
        exec_cfg = (
            post_start.get("exec")
            if isinstance(post_start, dict)
            else None
        )
        cmd = exec_cfg.get("command") if isinstance(exec_cfg, dict) else None
        if (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and str(cmd[0]).endswith("sh")
            and cmd[1] == "-c"
        ):
            existing_script = str(cmd[2] or "")
            if "code-server-$VERSION-linux-$ARCH_TAG" not in existing_script:
                cmd[2] = (
                    existing_script.rstrip()
                    + "\n\n# Ensure VSCode dependency exists in user HOME.\n"
                    + code_server_bootstrap_script
                )

    p["kubespawner_override"] = override
    return p

def _apply_pi_root_access(override):
    if not PI_RUN_AS_ROOT:
        return override

    if not isinstance(override, dict):
        override = {}

    override["uid"] = 0
    override["gid"] = 0

    container_cfg = copy.deepcopy(override.get("extra_container_config") or {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}
    sec_ctx = copy.deepcopy(container_cfg.get("securityContext") or {})
    if not isinstance(sec_ctx, dict):
        sec_ctx = {}
    sec_ctx["runAsUser"] = 0
    sec_ctx["runAsGroup"] = 0
    sec_ctx["runAsNonRoot"] = False
    # Keep this true for root shell workflows inside the user pod.
    sec_ctx["allowPrivilegeEscalation"] = True
    container_cfg["securityContext"] = sec_ctx
    override["extra_container_config"] = container_cfg

    pod_cfg = copy.deepcopy(override.get("extra_pod_config") or {})
    if not isinstance(pod_cfg, dict):
        pod_cfg = {}
    pod_sec_ctx = copy.deepcopy(pod_cfg.get("securityContext") or {})
    if not isinstance(pod_sec_ctx, dict):
        pod_sec_ctx = {}
    pod_sec_ctx["runAsNonRoot"] = False
    pod_cfg["securityContext"] = pod_sec_ctx
    override["extra_pod_config"] = pod_cfg

    return override


def _build_pi_profile_from_base(base_profile, size_key):
    spec = PI_SPECS_BY_SIZE.get(size_key) or {}
    p = copy.deepcopy(base_profile)
    p["display_name"] = spec.get("display_name", f"Pi {size_key.title()}")
    p["description"] = spec.get("description", "")

    override = copy.deepcopy(p.get("kubespawner_override") or {})
    base_env = override.get("environment") if isinstance(override.get("environment"), dict) else {}
    override["environment"] = {**base_env, **dict(PI_ENV)}
    override["image"] = PI_IMAGE
    override["cmd"] = PI_CMD
    override["args"] = []
    override["cpu_limit"] = spec.get("cpu_limit")
    override["cpu_guarantee"] = spec.get("cpu_guarantee")
    override["mem_limit"] = spec.get("mem_limit")
    override["mem_guarantee"] = spec.get("mem_guarantee")
    override = _apply_pi_root_access(override)
    p["kubespawner_override"] = override
    return p


def _build_pi_profile_minimal(size_key):
    spec = PI_SPECS_BY_SIZE.get(size_key) or {}
    override = {
        "environment": dict(PI_ENV),
        "image": PI_IMAGE,
        "cmd": PI_CMD,
        "args": [],
        "cpu_limit": spec.get("cpu_limit"),
        "cpu_guarantee": spec.get("cpu_guarantee"),
        "mem_limit": spec.get("mem_limit"),
        "mem_guarantee": spec.get("mem_guarantee"),
    }
    override = _apply_pi_root_access(override)
    return {
        "display_name": spec.get("display_name", f"Pi {size_key.title()}"),
        "description": spec.get("description", ""),
        "default": size_key == "small",
        "kubespawner_override": override,
    }

async def _profile_list_without_nebula_pi_cli(spawner):
    if callable(_orig_profile_list):
        profiles = _orig_profile_list(spawner)
    else:
        profiles = _orig_profile_list

    if inspect.isawaitable(profiles):
        profiles = await profiles

    cleaned = []
    for profile in (profiles or []):
        p = _clean_profile(profile)
        display_name = str(p.get("display_name") or "").strip().lower()
        # Remove Pi profiles from default/JupyterLab/VSCode flows.
        if display_name.startswith("pi "):
            continue
        cleaned.append(p)

    if (getattr(spawner, "name", "") or "").strip() == "pi":
        base_by_size = {}
        for profile in cleaned:
            size_key = str(profile.get("display_name") or "").strip().lower()
            if size_key in PI_SPECS_BY_SIZE:
                base_by_size[size_key] = profile

        # Some base profile providers return an empty list for named servers.
        # Fall back to the default-server profile set and reuse those mounts/settings.
        if len(base_by_size) < len(PI_SPECS_BY_SIZE) and callable(_orig_profile_list):
            default_spawner = None
            user = getattr(spawner, "user", None)
            user_spawners = getattr(user, "spawners", None)
            if isinstance(user_spawners, dict):
                default_spawner = user_spawners.get("")
            if default_spawner is None:
                default_spawner = copy.copy(spawner)
                default_spawner.name = ""

            fallback_profiles = _orig_profile_list(default_spawner)
            if inspect.isawaitable(fallback_profiles):
                fallback_profiles = await fallback_profiles
            for profile in (fallback_profiles or []):
                p = _clean_profile(profile)
                size_key = str(p.get("display_name") or "").strip().lower()
                if size_key in PI_SPECS_BY_SIZE and size_key not in base_by_size:
                    base_by_size[size_key] = p

        pi_profiles = []
        for size_key in ("small", "medium", "large"):
            base = base_by_size.get(size_key)
            if base is not None:
                pi_profiles.append(_build_pi_profile_from_base(base, size_key))
            else:
                pi_profiles.append(_build_pi_profile_minimal(size_key))
        return pi_profiles

    return cleaned

c.KubeSpawner.profile_list = _profile_list_without_nebula_pi_cli

existing_lifecycle_hooks = copy.deepcopy(getattr(c.KubeSpawner, "lifecycle_hooks", {}) or {})
if not isinstance(existing_lifecycle_hooks, dict):
    existing_lifecycle_hooks = {}

existing_lifecycle_hooks["postStart"] = {
    "exec": {"command": ["sh", "-lc", code_server_bootstrap_script]}
}
c.KubeSpawner.lifecycle_hooks = existing_lifecycle_hooks

# Avoid expensive recursive fsGroup ownership changes on large RWX homes at every spawn.
# Without this, pods can remain PodInitializing for many minutes and hit start_timeout.
existing_pod_security_context = copy.deepcopy(
    getattr(c.KubeSpawner, "pod_security_context", {}) or {}
)
if isinstance(existing_pod_security_context, dict):
    if "fsGroup" in existing_pod_security_context:
        existing_pod_security_context.setdefault("fsGroupChangePolicy", "OnRootMismatch")
        c.KubeSpawner.pod_security_context = existing_pod_security_context

# kubespawner in this stack still emits pod fsGroup without fsGroupChangePolicy.
# For RWX homes this can stall startup for many minutes. Force-disable fs_gid before spawn
# and rely on explicit init-container chmod for writable paths.
c.KubeSpawner.fs_gid = None

_previous_pre_spawn_hook = getattr(c.Spawner, "pre_spawn_hook", None)


async def _pre_spawn_disable_fs_gid(spawner):
    spawner.fs_gid = None
    if callable(_previous_pre_spawn_hook):
        result = _previous_pre_spawn_hook(spawner)
        if inspect.isawaitable(result):
            await result


c.Spawner.pre_spawn_hook = _pre_spawn_disable_fs_gid

_previous_modify_pod_hook = getattr(c.KubeSpawner, "modify_pod_hook", None)


async def _modify_pod_strip_fs_group(spawner, pod):
    if callable(_previous_modify_pod_hook):
        maybe_pod = _previous_modify_pod_hook(spawner, pod)
        if inspect.isawaitable(maybe_pod):
            pod = await maybe_pod
        elif maybe_pod is not None:
            pod = maybe_pod

    try:
        sec = pod.spec.security_context
        if sec is not None:
            sec.fs_group = None
            sec.fs_group_change_policy = None
            pod.spec.security_context = sec
    except Exception:
        pass

    return pod


c.KubeSpawner.modify_pod_hook = _modify_pod_strip_fs_group
