"""jhub-apps integration configuration."""

# ruff: noqa: F821 - `c` is a magic global provided by JupyterHub
from kubespawner import KubeSpawner
from jhub_apps import theme_template_paths, themes
from jhub_apps.configuration import install_jhub_apps
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

# Install jhub-apps (sets up service, roles, etc.)
c = install_jhub_apps(c, spawner_to_subclass=KubeSpawner)

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
