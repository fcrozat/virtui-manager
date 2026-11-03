"""
Declarative deployment configuration for ``vmc --config``.

Loads and validates a JSON file that describes a *single* VM deployment. There
are two shapes, and a file is one or the other:

* an **install**: ``iso`` names install media, and ``autoinstall`` (optional)
  describes the answer file it should be handed. ``VMProvisioner.provision_vm``
  performs it.
* a **boot**: ``disk.volume`` names a disk the storage pool already holds, and
  the machine is defined over it without installing anything.
  ``VMProvisioner.provision_existing_volume`` performs it.

This module only *describes* the deployment: it performs no libvirt operation
and never touches the network. The resolved values are handed straight through
to the provisioner by the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .vm_provisioner import VMType, VM_TYPE_DEFAULTS


class DeployConfigError(Exception):
    """Raised when a deployment config file is missing, malformed or invalid."""


# Allowed values for the ``iso.source`` field.
ISO_SOURCES = ("url", "pool", "cached", "repository")

# What may boot first: the disk the machine lives on, or the CD-ROM the caller
# attached (install media, or a seed a built machine is configured from).
BOOT_DEVICES = ("disk", "cdrom")

# ``vm_type`` may be one of the VMType profile names (lowercase) or this value,
# which lets the user supply memory/disk/vCPU manually instead of a profile.
CUSTOM_VM_TYPE = "custom"

# Map of lowercase profile name -> VMType (e.g. "desktop" -> VMType.DESKTOP).
VM_TYPE_BY_NAME = {t.name.lower(): t for t in VMType}


def resolve_vm_resources(cfg: Dict[str, Any]):
    """Resolve ``(VMType, memory_mb, vcpu, disk_size_gb)`` for a validated config.

    A profile (non-custom) takes memory/vCPU from that profile's defaults in
    ``VM_TYPE_DEFAULTS`` (any explicit config value takes precedence); disk is
    left as ``None`` so ``provision_vm`` applies ``DEFAULT_DISK_SIZE_GB`` (single
    source of truth for disk). "custom" has no dedicated hardware profile, so it
    uses the config values directly and SERVER as a neutral base.
    """
    vm_type_raw = str(cfg["vm_type"])
    if vm_type_raw == CUSTOM_VM_TYPE:
        return VMType.SERVER, cfg["memory_mb"], cfg["vcpu"], cfg["disk_size_gb"]
    vm_type = VM_TYPE_BY_NAME[vm_type_raw.lower()]
    defaults = VM_TYPE_DEFAULTS[vm_type]
    memory_mb = cfg.get("memory_mb") or defaults["memory_mb"]
    vcpu = cfg.get("vcpu") or defaults["vcpu"]
    # No explicit disk -> None -> provision_vm uses DEFAULT_DISK_SIZE_GB[vm_type].
    disk_size_gb = cfg.get("disk_size_gb")
    return vm_type, memory_mb, vcpu, disk_size_gb


def load_deploy_config(path) -> Dict[str, Any]:
    """Load and validate a deployment config JSON file.

    Returns a normalized dict ready to be consumed by the CLI deploy path.
    Raises :class:`DeployConfigError` with a human-readable message on any
    problem (missing file, bad JSON, missing/invalid fields).
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise DeployConfigError(f"Config file not found: {p}")
    if not p.is_file():
        raise DeployConfigError(f"Config path is not a file: {p}")

    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise DeployConfigError(f"Invalid JSON in {p}: {e}") from e

    if not isinstance(cfg, dict):
        raise DeployConfigError("Config root must be a JSON object")

    _validate(cfg)
    return cfg


def _validate(cfg: Dict[str, Any]) -> None:
    """Validate and normalize ``cfg`` in place. Raises DeployConfigError."""
    # --- required scalar fields ---
    for key in ("server", "vm_name", "vm_type"):
        if not cfg.get(key):
            raise DeployConfigError(f"Missing required field: '{key}'")

    # --- vm_type must be a known profile or "custom" ---
    vm_type_raw = str(cfg["vm_type"])
    is_custom = vm_type_raw == CUSTOM_VM_TYPE
    if not is_custom and vm_type_raw.lower() not in VM_TYPE_BY_NAME:
        valid = ", ".join(sorted(VM_TYPE_BY_NAME)) + ", custom"
        raise DeployConfigError(f"Invalid vm_type '{vm_type_raw}' (valid: {valid})")

    # --- hardware ---
    # For "custom" the user must supply all three; for a profile they are
    # optional overrides (absent values fall back to the profile defaults).
    if is_custom:
        cfg["memory_mb"] = _required_positive_int(cfg, "memory_mb")
        cfg["vcpu"] = _required_positive_int(cfg, "vcpu")
        cfg["disk_size_gb"] = _required_positive_int(cfg, "disk_size_gb")
    else:
        cfg["memory_mb"] = _positive_int(cfg, "memory_mb", None)
        cfg["vcpu"] = _positive_int(cfg, "vcpu", None)
        cfg["disk_size_gb"] = _positive_int(cfg, "disk_size_gb", None)
    if cfg.get("disk_format") is not None and not isinstance(cfg["disk_format"], str):
        raise DeployConfigError("'disk_format' must be a string (e.g. 'qcow2')")

    # --- boot / graphics / network / pool defaults ---
    cfg["boot_uefi"] = bool(cfg.get("boot_uefi", True))
    cfg["graphics"] = cfg.get("graphics", "spice")
    cfg["network"] = cfg.get("network", "default")
    cfg["storage_pool"] = cfg.get("storage_pool", "default")
    cfg["start"] = bool(cfg.get("start", True))

    boot = cfg.get("boot", "disk")
    if boot not in BOOT_DEVICES:
        raise DeployConfigError(f"'boot' must be one of {', '.join(BOOT_DEVICES)} (got '{boot}')")
    cfg["boot"] = boot

    # --- disk: the volume to boot, instead of installing ---
    disk = cfg.get("disk") or {}
    if not isinstance(disk, dict):
        raise DeployConfigError("'disk' must be an object")
    cfg["disk_volume"] = disk.get("volume")
    cfg["backing_volume"] = disk.get("backing_volume")
    if cfg["backing_volume"] and not cfg["disk_volume"]:
        raise DeployConfigError("disk.backing_volume needs disk.volume to overlay")

    # --- seed: a read-only CD-ROM the guest reads its configuration from ---
    seed = cfg.get("seed")
    if seed is None:
        cfg["seed_volume"] = None
    elif not isinstance(seed, dict):
        raise DeployConfigError("'seed' must be an object")
    else:
        cfg["seed_volume"] = seed.get("volume")
        if not cfg["seed_volume"]:
            raise DeployConfigError("seed.volume is required when 'seed' is given")

    # --- metadata: whose this machine is, kept in its XML and reported back ---
    metadata = cfg.get("metadata")
    if metadata is None:
        cfg["metadata"] = {}
    elif not isinstance(metadata, dict):
        raise DeployConfigError("'metadata' must be an object")
    else:
        for key, value in metadata.items():
            if not isinstance(value, (str, int, float, bool)):
                raise DeployConfigError(
                    f"metadata.{key} must be a single value, not {type(value).__name__}"
                )
        cfg["metadata"] = {str(k): str(v) for k, v in metadata.items()}

    # --- iso: required to install, forbidden when booting a volume ---
    iso = cfg.get("iso")
    if cfg["disk_volume"]:
        if iso:
            raise DeployConfigError(
                "disk.volume and iso describe different deployments: a machine boots "
                "a disk it has, or it installs from media, not both"
            )
        if cfg.get("autoinstall"):
            raise DeployConfigError("autoinstall needs install media; disk.volume installs nothing")
        return
    if not isinstance(iso, dict):
        raise DeployConfigError("Missing required object: 'iso'")
    source = iso.get("source", "url")
    if source not in ISO_SOURCES:
        raise DeployConfigError(
            f"iso.source must be one of {', '.join(ISO_SOURCES)} (got '{source}')"
        )
    if source == "url" and not iso.get("url"):
        raise DeployConfigError("iso.url is required when iso.source is 'url'")
    if source == "pool" and not (iso.get("pool") and iso.get("volume")):
        raise DeployConfigError("iso.pool and iso.volume are required when iso.source is 'pool'")
    if source == "cached" and not iso.get("volume"):
        raise DeployConfigError("iso.volume is required when iso.source is 'cached'")
    if source == "repository" and not iso.get("version"):
        raise DeployConfigError("iso.version is required when iso.source is 'repository'")

    # --- autoinstall (optional) ---
    ai = cfg.get("autoinstall")
    if ai is not None and not isinstance(ai, dict):
        raise DeployConfigError("'autoinstall' must be an object")
    ai_enabled = ai is not None and ai.get("enabled", True)

    # --- post_install (optional) ---
    # Post-install commands are baked into the auto-install template's
    # post-install script (AutoYaST <scripts><post> / Agama scripts.post), so
    # they only make sense when auto-install is enabled.
    pi = cfg.get("post_install")
    if pi is not None:
        if not isinstance(pi, dict):
            raise DeployConfigError("'post_install' must be an object")
        cmds = pi.get("commands")
        if cmds is not None and (
            not isinstance(cmds, list) or not all(isinstance(c, str) for c in cmds)
        ):
            raise DeployConfigError("post_install.commands must be a list of strings")
        if not ai_enabled:
            raise DeployConfigError(
                "post_install requires autoinstall to be enabled "
                "(commands are baked into the auto-install template)"
            )

    if ai is None:
        cfg["autoinstall"] = None
        return
    if ai_enabled and not ai.get("template"):
        raise DeployConfigError(
            "autoinstall.template is required when autoinstall.enabled is true"
        )
    pkgs = ai.get("packages")
    if pkgs is not None:
        if not isinstance(pkgs, list) or not all(isinstance(p, str) for p in pkgs):
            raise DeployConfigError("autoinstall.packages must be a list of strings")
    scc = ai.get("scc")
    if scc is not None and not isinstance(scc, dict):
        raise DeployConfigError("autoinstall.scc must be an object")


def _positive_int(cfg: Dict[str, Any], key: str, default, minimum: int = 1):
    """Return cfg[key] as an int >= ``minimum``, applying ``default`` when absent."""
    if key not in cfg or cfg[key] is None:
        return default
    try:
        val = int(cfg[key])
    except (TypeError, ValueError) as e:
        raise DeployConfigError(f"'{key}' must be an integer (got {cfg[key]!r})") from e
    if val < minimum:
        raise DeployConfigError(f"'{key}' must be >= {minimum}")
    return val


def _required_positive_int(cfg: Dict[str, Any], key: str) -> int:
    """Return cfg[key] as an int >= 1, raising when it is absent (custom VMs)."""
    if key not in cfg or cfg[key] is None:
        raise DeployConfigError(f"'{key}' is required when vm_type is 'custom'")
    return _positive_int(cfg, key, 1)
