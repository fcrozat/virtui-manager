"""The plan ``vmc installvm`` acts on: every choice an install makes, settled up front.

Machine mode never asks a question, so the decisions the interactive wizards take
along the way are worked out here first, from the host, the ISO cache and the
caller's answers. A decision that cannot be settled is left in ``unresolved``
with the values that would settle it rather than guessed at: ``--resolve`` prints
the plan, the caller answers, and the plan is built again.

Nothing here creates, changes or starts anything. The one thing an install decides
for itself afterwards is ``automation``: what ``provision_vm`` serves the installer
as its answer file, which depends on the distribution in a way this module cannot
see until the install runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import libvirt

from .config import AppInfo, load_config
from .firmware_manager import get_uefi_files, select_best_firmware
from .storage_manager import list_storage_pools
from .vm_provisioner import DEFAULT_DISK_SIZE_GB, OSType, VMType

# What a build is handed when nothing overrides it. A plan is meant to say what the
# install really gets, so these are written down here instead of left to the call site.
DEFAULT_VM_TYPE = VMType.SERVER
DEFAULT_MEMORY_MB = 4096
DEFAULT_VCPU_COUNT = 2
DEFAULT_NETWORK = "default"

#: Every key a plan has. A plan always has all of them.
PLAN_KEYS = (
    "arch",
    "automation",
    "disk_size_bytes",
    "exists",
    "firmware",
    "iso",
    "kvm",
    "memory_mb",
    "name",
    "network",
    "os",
    "pool",
    "tools_missing",
    "unresolved",
    "vcpu",
)

#: The keys ``--answer`` may settle. The others are facts about the host and the
#: domain, reported by a plan but not open to the caller.
ANSWERABLE_KEYS = (
    "automation",
    "disk_size_bytes",
    "iso",
    "memory_mb",
    "network",
    "os",
    "pool",
    "vcpu",
)


class NeedsAnswer(Exception):  # noqa: N818 - it is a question, not a failure
    """A decision machine mode cannot make on its own.

    Carries what the caller needs to settle it: the plan key, why it is open, and
    the values that would close it.
    """

    def __init__(self, key: str, reason: str, choices: list | None = None) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason
        self.choices = list(choices or [])

    def as_dict(self) -> dict:
        return {"key": self.key, "reason": self.reason, "choices": self.choices}


def pick(key: str, choices: list, wanted: str | None = None, reason: str = "") -> Any:
    """Return the one choice ``wanted`` names, or raise `NeedsAnswer` when it does not.

    Without an answer a single choice is obvious enough to take, and none is a
    decision to hand back. Values are compared case-insensitively, so a plan can be
    answered with its own words.
    """
    choices = list(choices)
    if wanted is not None:
        for choice in choices:
            if str(choice).lower() == str(wanted).lower():
                return choice
        raise NeedsAnswer(
            key,
            f"no {key} named '{wanted}'",
            [str(choice) for choice in choices],
        )
    if len(choices) == 1:
        return choices[0]
    raise NeedsAnswer(
        key,
        reason or "nothing to choose from",
        [str(choice) for choice in choices],
    )


def apply_answers(source: Any) -> dict:
    """Normalise what ``--answer`` names into the ``{plan key: value}`` map to build with.

    The file is either that map on its own, the same map wrapped in ``{"answers": ...}``,
    or a whole plan as ``--resolve`` printed it — which is answerable with its own
    values, so resolved plans round-trip. Anything else is a mistake worth reporting
    rather than a plan to ignore.
    """
    if isinstance(source, dict) and "plan" in source:
        source = source["plan"]
    if isinstance(source, dict) and "answers" in source:
        source = source["answers"]
    if not isinstance(source, dict):
        raise NeedsAnswer("<file>", "an answer file is a map of plan keys to values, or a plan")
    # A whole plan carries the facts it was only reporting (the host's architecture,
    # whether the domain exists), so those pass through an answer file unanswered; a
    # name no plan has at all is a typo, and a caller that mistyped an answer should
    # hear about it rather than have the install go ahead without it.
    unknown = sorted(
        str(key) for key in source if key not in ANSWERABLE_KEYS and key not in PLAN_KEYS
    )
    if unknown:
        raise NeedsAnswer(
            unknown[0],
            f"'{unknown[0]}' is not a plan key; answer one of: {', '.join(ANSWERABLE_KEYS)}",
        )
    return {str(key): value for key, value in source.items() if key in ANSWERABLE_KEYS}


def build_plan(
    provisioner: Any, name: str, capabilities: dict, answers: dict | None = None
) -> dict:
    """Work out what an install of `name` would do, asking nothing.

    `capabilities` is what ``vmc capabilities`` reports (`_host_capabilities`), passed
    in so a plan and the command never disagree about the host. `answers` is the map
    from ``--answer``: every answer is checked against what the host offers instead
    of trusted. `unresolved` collects the decisions still open, in the order the
    choices below are made.
    """
    answers = dict(answers or {})
    conn = provisioner.conn
    plan: dict[str, Any] = {
        "name": name,
        "exists": _domain_exists(conn, name),
        "arch": provisioner.host_arch,
        "kvm": bool(capabilities.get("kvm")),
        "iso": None,
        "os": None,
        "firmware": None,
        "pool": None,
        "network": DEFAULT_NETWORK,
        "memory_mb": DEFAULT_MEMORY_MB,
        "vcpu": DEFAULT_VCPU_COUNT,
        "disk_size_bytes": DEFAULT_DISK_SIZE_GB[DEFAULT_VM_TYPE] * 1024**3,
        # What the installer is served as its answer file, handed to provision_vm as
        # automation_config. Its content belongs to the caller; what provision_vm does
        # with it is what an install actually does, so an install is only unattended
        # when the caller answers this.
        "automation": answers.get("automation"),
        "tools_missing": [],
        "unresolved": [],
    }

    def settle(key: str, chooser: Callable[[], Any]) -> None:
        try:
            plan[key] = chooser()
        except NeedsAnswer as question:
            plan["unresolved"].append(question.as_dict())

    settle("iso", lambda: _iso(answers.get("iso")))
    settle("os", lambda: _os(provisioner, plan["iso"], answers.get("os")))
    settle("pool", lambda: _pool(conn, answers.get("pool")))
    settle("firmware", lambda: _firmware(provisioner))
    if answers.get("network") is not None:
        settle("network", lambda: _network(conn, answers["network"]))
    for key in ("memory_mb", "vcpu", "disk_size_bytes"):
        if answers.get(key) is not None:
            settle(key, lambda key=key: _count(key, answers[key]))

    # Which tools an install needs is only known once the ISO and its distribution are.
    if plan["iso"] and plan["os"]:
        plan["tools_missing"] = provisioner.check_install_tools(
            provisioner.required_install_tools(
                _as_enum(plan["os"]),
                custom_path=plan["iso"],
                boot_uefi=bool(plan["firmware"]),
            )
        )
    return plan


# --- the choices a plan makes, and what it reports ---------------------------------

#: The distributions a plan can name, as the TUI names them: 'Ubuntu' is OSType.Ubuntu.
OS_NAMES = {os_type.name: os_type for os_type in OSType}


def _as_enum(value: Any) -> OSType:
    """A plan's `os` back to the `OSType` provisioning is called with."""
    return value if isinstance(value, OSType) else OS_NAMES[str(value)]


def provision_from_plan(provisioner: Any, plan: dict, name: str) -> Any:
    """Carry out a resolved plan and return the domain. The only thing here that changes state.

    A plan is what a build would do, so this is where it says so: every value comes from
    `plan`, and nothing is chosen here that a plan could have said. The two it cannot
    say are the shape of the guest and its console, which an install of this kind always
    takes as a server with a SPICE console.
    """
    # Provisioning sizes a disk in whole GiB, so an answer in bytes rounds up to the
    # GiB that holds it rather than losing a partial one to integer division.
    disk_size_gb = -(-int(plan["disk_size_bytes"]) // 1024**3)
    return provisioner.provision_vm(
        vm_name=name,
        vm_type=DEFAULT_VM_TYPE,
        iso_url=plan["iso"],
        storage_pool_name=plan["pool"],
        memory_mb=plan["memory_mb"],
        vcpu=plan["vcpu"],
        disk_size_gb=disk_size_gb,
        boot_uefi=bool(plan["firmware"]),
        network_name=plan["network"],
        automation_config=plan["automation"],
    )


def _domain_exists(conn: Any, name: str) -> bool:
    """Whether a domain of this name is already defined.

    A connection that would not answer is reported as 'not there': the command that
    acts on the name reads it again and fails there, with libvirt's own reason.
    """
    try:
        return conn.lookupByName(name) is not None
    except libvirt.libvirtError:
        return False


def _cached_isos() -> list[dict]:
    """The images in the local cache as ``{"name", "path", "size_bytes"}``.

    ``VMProvisioner.get_cached_isos`` lists the same files for the wizard, but puts the
    filename where a path is needed, so the cache is listed here for the plan to offer
    names and resolve them in one place.
    """
    cache = Path(
        load_config().get("ISO_DOWNLOAD_PATH", str(Path.home() / ".cache" / AppInfo.name / "isos"))
    )
    if not cache.is_dir():
        return []
    return [
        {"name": entry.name, "path": str(entry), "size_bytes": entry.stat().st_size}
        for entry in sorted(cache.glob("*.iso"))
    ]


def _iso(wanted: str | None) -> str:
    """The image to install from, as the path provisioning is handed.

    The cache is offered by filename; an answer naming a file that exists is taken as
    it stands, which is how provisioning reads a path of its own.
    """
    cached = {entry["name"]: entry["path"] for entry in _cached_isos()}
    if wanted:
        for filename, path in cached.items():
            if filename.lower() == str(wanted).lower():
                return path
        if os.path.isfile(str(wanted)):
            return str(wanted)
        raise NeedsAnswer("iso", f"no cached image or file named '{wanted}'", sorted(cached))
    return cached[pick("iso", sorted(cached), reason="nothing in the ISO cache to install from")]


def _os(provisioner: Any, iso: str | None, wanted: str | None) -> str:
    """The distribution to install, named the way the TUI names it, as 'Ubuntu'.

    Taken from the image when nothing is answered, the way `_get_vm_settings` does it:
    a distribution is recognised in a cached image's filename. A custom image with no
    name to go on stays a decision for the caller.
    """
    if wanted is None:
        if not iso:
            raise NeedsAnswer("os", "no image to recognise a distribution in", sorted(OS_NAMES))
        # The same recognition provision_vm does, from the image's name alone.
        detected = provisioner._resolve_os_type(None, custom_path=iso)
        if detected == OSType.LINUX:
            raise NeedsAnswer(
                "os",
                f"'{os.path.basename(iso)}' is not a distribution this recognises",
                sorted(OS_NAMES),
            )
        return detected.name
    return pick("os", sorted(OS_NAMES), wanted, reason="no distribution to install")


def _pool(conn: Any, wanted: str | None) -> str:
    """The storage pool the install would place its disks in, by name.

    Any pool that can hold a disk will do, so the active ones are offered and an answer
    is checked against them. Starting a pool is a change of state, which is what
    ``vmc pool_ensure`` is for rather than this, and free space is libvirt's to refuse.
    """
    active = sorted(
        str(entry["name"])
        for entry in list_storage_pools(conn)
        if entry.get("status") == "active" and entry.get("name")
    )
    return pick("pool", active, wanted, reason="no storage pool is active")


def _network(conn: Any, wanted: str | None) -> str:
    """A virtual network the hypervisor has, running or merely defined.

    The name goes straight into the domain XML, so a network that is not there would
    only show up as a libvirt failure partway through an install.
    """
    known: set[str] = set()
    for listing in (conn.listNetworks, conn.listDefinedNetworks):
        try:
            known.update(listing())
        except libvirt.libvirtError:
            pass
    return pick(
        "network",
        sorted(known) or [DEFAULT_NETWORK],
        wanted,
        reason="no virtual network is defined",
    )


def _count(key: str, value: Any) -> int:
    """An answer that has to be a whole number of something, however it is written."""
    try:
        number = int(str(value).strip())
    except ValueError:
        raise NeedsAnswer(key, f"'{value}' is not a number") from None
    if number < 1:
        raise NeedsAnswer(key, f"'{value}' is not a positive number")
    return number


def _firmware(provisioner: Any) -> str | None:
    """The UEFI loader the guest would boot with, or None to leave the choice to libvirt.

    The same loader `_setup_uefi_nvram` picks, without the NVRAM volume it creates,
    which is what building a plan must not do. Secure Boot is the one difference:
    `DEFAULT_VM_TYPE` is SERVER, which never asks for it, so the exceptions a
    Debian-family or Alpine install makes are not choices this plan can face.
    """
    best = select_best_firmware(
        get_uefi_files(provisioner.conn),
        architecture=provisioner.host_arch,
        secure_boot=False,
        prefer_nvram=True,
    )
    return best.executable if best else None
