"""KubeSpawner configuration."""
# ruff: noqa: F821 - `c` is a magic global provided by JupyterHub

# Home directory: Dynamic PVC per user via the cluster's default StorageClass.
# We configure volumes here instead of via singleuser.storage in values.yaml because
# jhub-apps' JHubSpawner expects volumes as a list, but the subchart's dynamic storage
# generates a dict, causing a TraitError on startup.
#
# Using fixed /home/jovyan mount path because {username} expands differently in
# KubeSpawner (escaped slug) vs base Spawner traits like notebook_dir (raw username),
# causing a path mismatch when usernames contain special characters (e.g. emails).
# Use one home PVC per user *per server name* so default Jupyter and Pi can run
# concurrently on different nodes without RWO multi-attach conflicts.
# - default server -> claim-{username}
# - pi named server -> claim-{username}{servername}
# (servername expands to empty for default and to a suffix for named servers)
c.KubeSpawner.pvc_name_template = "claim-{username}{servername}"
c.KubeSpawner.storage_pvc_ensure = True
c.KubeSpawner.storage_capacity = "10Gi"
c.KubeSpawner.storage_access_modes = ["ReadWriteOnce"]
c.KubeSpawner.volumes = [
    {
        "name": "home",
        "persistentVolumeClaim": {
            "claimName": "claim-{username}{servername}",
        },
    },
]
c.KubeSpawner.volume_mounts = [
    {
        "name": "home",
        "mountPath": "/home/jovyan",
    },
]

c.KubeSpawner.notebook_dir = "/home/jovyan"
c.KubeSpawner.working_dir = "/home/jovyan"
c.KubeSpawner.environment = {
    "HOME": "/home/jovyan",
}
