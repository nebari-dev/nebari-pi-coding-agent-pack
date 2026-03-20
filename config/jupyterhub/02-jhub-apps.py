"""jhub-apps integration configuration."""

# ruff: noqa: F821 - `c` is a magic global provided by JupyterHub
from urllib.parse import urlparse

from jupyterhub import orm
from jupyterhub.handlers.base import BaseHandler
from kubespawner import KubeSpawner
from jhub_apps import theme_template_paths, themes
from jhub_apps.configuration import install_jhub_apps
from tornado.web import HTTPError
from z2jh import get_config

# Configure jhub-apps
# bind_url must include the real external hostname so JupyterHub constructs
# correct OAuth redirect URLs for internal services like jhub-apps.
# See: nebari's 02-spawner.py for the same pattern.
domain = str(get_config("custom.external-url", "") or "").strip()
if domain:
    if domain.startswith("http://") or domain.startswith("https://"):
        c.JupyterHub.bind_url = domain
    else:
        scheme = str(get_config("custom.external-url-scheme", "https") or "https").strip()
        c.JupyterHub.bind_url = f"{scheme}://{domain}"
else:
    c.JupyterHub.bind_url = "http://0.0.0.0:8000"
# Route users through japps login shim first so service JWT cookie is present
# before the React app calls /services/japps/* APIs.
c.JupyterHub.default_url = "/services/japps/jhub-login"
c.JupyterHub.template_paths = theme_template_paths
c.JupyterHub.template_vars = themes.DEFAULT_THEME
c.JAppsConfig.jupyterhub_config_path = "/usr/local/etc/jupyterhub/jupyterhub_config.py"

# Apply JAppsConfig overrides from Helm values (jupyterhub.custom.japps-config).
# Any key in the dict is set as an attribute on c.JAppsConfig, e.g.:
#   japps-config:
#     app_title: "My Launcher"
#     service_workers: 2
#     allowed_frameworks: ["panel", "streamlit"]
japps_config = get_config("custom.japps-config", {})
for key, value in japps_config.items():
    setattr(c.JAppsConfig, key, value)

# Ensure Pi appears as a pinned quick-access card in japps launcher.
# Use append() because traitlets may provide a LazyConfigValue here.
c.JAppsConfig.additional_services.append(
    {
        "name": "Pi Coding Agent",
        "url": "/services/pi-launcher/",
        "description": "Open your Pi coding agent terminal (or go to launcher if it is not running).",
        "pinned": True,
    }
)

# Install jhub-apps (sets up service, roles, etc.)
c = install_jhub_apps(c, spawner_to_subclass=KubeSpawner)


def _safe_next_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("/"):
        return "/hub/home"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/hub/home"
    return raw


class PiTokenLoginHandler(BaseHandler):
    """Bootstrap a browser login session from an API token.

    Supports:
    - user API token (token belongs directly to a user)
    - admin service token + `user=<name>` (service can impersonate target user)
    """

    async def get(self):
        raw_token = str(self.get_argument("token", "") or "").strip()
        target_user = str(self.get_argument("user", "") or "").strip()
        next_url = _safe_next_url(self.get_argument("next", "/hub/home"))

        if not raw_token:
            raise HTTPError(400, "missing token")

        api_token = orm.APIToken.find(self.db, raw_token)
        if api_token is None:
            raise HTTPError(403, "invalid token")

        user = None
        if api_token.user is not None:
            user = self._user_from_orm(api_token.user)
        elif api_token.service is not None:
            service = api_token.service
            if not getattr(service, "admin", False):
                raise HTTPError(403, "service token lacks admin rights")
            if not target_user:
                raise HTTPError(400, "missing user for service token")
            orm_user = orm.User.find(self.db, target_user)
            if orm_user is None:
                raise HTTPError(404, "target user not found")
            user = self._user_from_orm(orm_user)
        else:
            raise HTTPError(403, "unsupported token type")

        self.set_login_cookie(user)
        self.redirect(next_url)


# Ensure all authenticated Hub users can access launcher services.
# This prevents intermittent "You do not have permission to access JupyterHub service japps"
# errors in local setups with mixed/stale cookies.
existing_roles = list(getattr(c.JupyterHub, "load_roles", []) or [])
extra_scopes = {
    "self",
    "access:services!service=japps",
    "access:services!service=pi-launcher",
}

# De-duplicate any pre-existing "user" role definitions and re-add exactly one.
merged_user_scopes = set()
filtered_roles = []
for role in existing_roles:
    if isinstance(role, dict) and role.get("name") == "user":
        scopes = role.get("scopes")
        if isinstance(scopes, list):
            merged_user_scopes.update(scopes)
        continue
    filtered_roles.append(role)

merged_user_scopes.update(extra_scopes)
filtered_roles.append(
    {
        "name": "user",
        "scopes": sorted(merged_user_scopes),
    }
)

c.JupyterHub.load_roles = filtered_roles

# Add token->cookie bootstrap endpoint used by in-pod browser smoke checks.
# This allows short-lived API tokens to establish browser session cookies without
# storing static username/password credentials in pod env.
# NOTE: `c.JupyterHub.extra_handlers` can be a LazyConfigValue in traitlets,
# so append directly instead of coercing to list().
c.JupyterHub.extra_handlers.append((r"/pi-token-login", PiTokenLoginHandler))
c.JupyterHub.extra_handlers.append((r"/hub/pi-token-login", PiTokenLoginHandler))

