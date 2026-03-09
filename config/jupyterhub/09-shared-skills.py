import copy
import inspect
import os

SHARED_SKILLS_MOUNT_ROOT = "/opt/nebari/shared-skills"
SHARED_SKILLS_MODE = "pvc-subpath"
SHARED_SKILLS_DIR = f"{SHARED_SKILLS_MOUNT_ROOT}/current"
SHARED_SKILLS_VOLUME_NAME = "pi-shared-skills"
SHARED_SKILLS_PVC_NAME = (
    os.environ.get("NEBARI_SHARED_SKILLS_PVC_NAME", "shared-storage").strip()
    or "shared-storage"
)
SHARED_SKILLS_PVC_SUBPATH = (
    os.environ.get("NEBARI_SHARED_SKILLS_PVC_SUBPATH", "shared-skills").strip()
    or "shared-skills"
)

# PostStart hooks should never crash the user server. Best effort only.
shared_skills_post_start_snippet = (
    'mkdir -p "$HOME/.pi/agent/skills" >/dev/null 2>&1 || true; '
    'ln -sfn "$NEBARI_SHARED_SKILLS_DIR" "$HOME/.pi/agent/skills/shared" >/dev/null 2>&1 || true'
)

shared_skills_hook_block = (
    "# Mount shared Pi skills for all user pods.\n" + shared_skills_post_start_snippet
)


def _ensure_shared_skills_volume(override):
    pod_cfg = copy.deepcopy(override.get("extra_pod_config") or {})
    if not isinstance(pod_cfg, dict):
        pod_cfg = {}

    volumes = list(pod_cfg.get("volumes") or [])
    volumes = [
        v
        for v in volumes
        if not (
            isinstance(v, dict)
            and v.get("name") in {"pi-shared-skills", "home"}
            and isinstance(v.get("persistentVolumeClaim"), dict)
            and v.get("persistentVolumeClaim", {}).get("claimName") == SHARED_SKILLS_PVC_NAME
        )
    ]

    volumes.append(
        {
            "name": SHARED_SKILLS_VOLUME_NAME,
            "persistentVolumeClaim": {
                "claimName": SHARED_SKILLS_PVC_NAME,
            },
        }
    )

    pod_cfg["volumes"] = volumes
    override["extra_pod_config"] = pod_cfg


def _ensure_shared_skills_mount(override):
    container_cfg = copy.deepcopy(override.get("extra_container_config") or {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}
    mounts = list(container_cfg.get("volumeMounts") or [])
    mounts = [
        mount
        for mount in mounts
        if not (
            isinstance(mount, dict)
            and mount.get("mountPath") == SHARED_SKILLS_MOUNT_ROOT
        )
    ]

    mounts.append(
        {
            "name": SHARED_SKILLS_VOLUME_NAME,
            "mountPath": SHARED_SKILLS_MOUNT_ROOT,
            "subPath": SHARED_SKILLS_PVC_SUBPATH,
            "readOnly": True,
        }
    )

    container_cfg["volumeMounts"] = mounts
    override["extra_container_config"] = container_cfg


def _ensure_pi_skill_flag(cmd_list):
    if not isinstance(cmd_list, list) or not cmd_list:
        return cmd_list
    updated = list(cmd_list)
    saw_skill_flag = False
    for idx in range(len(updated) - 1):
        if updated[idx] != "--skill":
            continue
        saw_skill_flag = True
        if str(updated[idx + 1]) == SHARED_SKILLS_MOUNT_ROOT:
            updated[idx + 1] = SHARED_SKILLS_DIR
    if not saw_skill_flag:
        try:
            pi_index = len(updated) - 1 - updated[::-1].index("pi")
        except ValueError:
            pi_index = -1
        if pi_index >= 0:
            updated = (
                updated[: pi_index + 1]
                + ["--skill", SHARED_SKILLS_DIR]
                + updated[pi_index + 1 :]
            )
    return updated


def _inject_shared_skills_mount(profile):
    p = copy.deepcopy(profile or {})
    override = copy.deepcopy(p.get("kubespawner_override") or {})
    _ensure_shared_skills_volume(override)
    _ensure_shared_skills_mount(override)

    env = copy.deepcopy(override.get("environment") or {})
    if not isinstance(env, dict):
        env = {}
    env["NEBARI_SHARED_SKILLS_DIR"] = SHARED_SKILLS_DIR
    env["NEBARI_SHARED_SKILLS_MOUNT_ROOT"] = SHARED_SKILLS_MOUNT_ROOT
    env["NEBARI_SHARED_SKILLS_MODE"] = SHARED_SKILLS_MODE
    env["NEBARI_SHARED_SKILLS_PVC_NAME"] = SHARED_SKILLS_PVC_NAME
    env["NEBARI_SHARED_SKILLS_PVC_SUBPATH"] = SHARED_SKILLS_PVC_SUBPATH
    override["environment"] = env

    hooks = copy.deepcopy(override.get("lifecycle_hooks") or {})
    if not isinstance(hooks, dict):
        hooks = {}
    post_start = hooks.get("postStart")
    if not isinstance(post_start, dict):
        post_start = {}
    exec_cfg = post_start.get("exec")
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
    cmd = exec_cfg.get("command")

    if (
        isinstance(cmd, list)
        and len(cmd) >= 3
        and str(cmd[0]).endswith("sh")
        and cmd[1] in ("-c", "-lc")
    ):
        existing_script = str(cmd[2] or "").lstrip()
        if not existing_script.startswith(shared_skills_hook_block):
            cmd[2] = shared_skills_hook_block + "\n\n" + existing_script
    else:
        hooks["postStart"] = {
            "exec": {"command": ["sh", "-lc", shared_skills_hook_block]}
        }
    override["lifecycle_hooks"] = hooks

    cmd_list = override.get("cmd")
    fixed_cmd = _ensure_pi_skill_flag(cmd_list)
    if fixed_cmd != cmd_list:
        override["cmd"] = fixed_cmd

    p["kubespawner_override"] = override
    return p


_orig_profile_list_shared_skills = c.KubeSpawner.profile_list


async def _profile_list_with_shared_skills(spawner):
    if callable(_orig_profile_list_shared_skills):
        profiles = _orig_profile_list_shared_skills(spawner)
    else:
        profiles = _orig_profile_list_shared_skills

    if inspect.isawaitable(profiles):
        profiles = await profiles

    return [_inject_shared_skills_mount(profile) for profile in (profiles or [])]


c.KubeSpawner.profile_list = _profile_list_with_shared_skills
