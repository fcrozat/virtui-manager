"""
Machine-readable command interface for the CLI.

`vmc --json -c "<command>"` answers one command about one server and prints
exactly one JSON document on stdout: the command result, or
`{"error": {"code": ..., "message": ...}}`. The exit code is 0 on success and
1 on failure, so a caller never has to read prose to know whether it worked.

This path deliberately shares nothing with the interactive shell beyond the
managers it calls: no intro, no colour, no readline, no CLILogger wrapping
sys.stdout, no Textual. It is what a program driving virtui-manager reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable
from xml.sax.saxutils import escape

import libvirt

from . import install_plan
from .config import load_config
from .firmware_manager import get_uefi_files
from .libvirt_utils import get_host_architecture, get_network_info
from .storage_manager import _safe_get_volume_info, create_storage_pool
from .utils import _sanitize_domain_name, sanitize_sensitive_data
from .vm_actions import force_off_vm, start_vm, stop_vm
from .vm_provisioner import VMProvisioner

# Machine API level. A caller that needs a command added after this level
# checks it here rather than parsing a version string.
API_LEVEL = 1

# libvirt's domain states, in the words a script matches on.
_DOMAIN_STATE = {
    libvirt.VIR_DOMAIN_NOSTATE: "nostate",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
}

# libvirt has no named constants for pool states in its Python bindings.
_POOL_STATE = {
    0: "inactive",
    1: "building",
    2: "running",
    3: "degraded",
    4: "inaccessible",
    5: "deleting",
}

# Sizes are typed by a human ("20G") and handed to libvirt in bytes.
_SIZE_PATTERN = re.compile(r"(\d+)\s*([KMGT]?)B?")
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

# Where libvirt puts the pool it calls 'default'.
_DEFAULT_POOL_PATH = "/var/lib/libvirt/images"

# Where to look for a guest address, in the order that works without a
# guest agent: the network's DHCP lease, then the agent, then ARP.
_ADDRESS_SOURCES = (
    ("lease", libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE),
    ("agent", libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT),
    ("arp", libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP),
)


class MachineError(Exception):
    """A command failure, reported to stdout as {"error": {code, message}}."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def emit(document: Any) -> None:
    """Write the one JSON document stdout is allowed to contain."""
    json.dump(document, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _take_option(args: list[str], name: str) -> str | None:
    """Remove and return "--<name> <value>" from args, or None when absent."""
    flag = "--" + name
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        raise MachineError("bad_argument", f"{flag} needs a value")
    value = args.pop(index + 1)
    args.pop(index)
    return value


def _take_flag(args: list[str], name: str) -> bool:
    """Remove and report whether "--<name>" was present."""
    flag = "--" + name
    if flag not in args:
        return False
    args.remove(flag)
    return True


def _reject_leftover(args: list[str], command: str) -> None:
    if args:
        raise MachineError("bad_argument", f"{command}: unexpected argument '{args[0]}'")


def _connect(server: str | None, config_file: str | None) -> libvirt.virConnect:
    """Open the connection one command runs against."""
    try:
        config = load_config(config_file)
    except FileNotFoundError as exc:
        raise MachineError("bad_configuration", str(exc)) from exc

    servers = config.get("servers") or []
    if server:
        matches = [entry for entry in servers if entry.get("name") == server]
        if not matches:
            known = ", ".join(str(entry.get("name")) for entry in servers) or "none"
            raise MachineError(
                "unknown_server", f"no server '{server}' in the configuration (known: {known})"
            )
        entry = matches[0]
    elif len(servers) == 1:
        entry = servers[0]
    else:
        known = ", ".join(str(item.get("name")) for item in servers) or "none"
        raise MachineError("no_server", f"name the server with --server (known: {known})")

    uri = entry.get("uri")
    if not uri:
        raise MachineError("bad_configuration", f"server '{server}' has no uri")

    try:
        conn = libvirt.open(str(uri))
    except libvirt.libvirtError as exc:
        conn = None
        reason = str(exc).strip()
    else:
        reason = ""
    if conn is None:
        raise MachineError(
            "connection_failed",
            f"cannot connect to {sanitize_sensitive_data(uri)}"
            + (f": {sanitize_sensitive_data(reason)}" if reason else ""),
        )
    return conn


def _domain(conn: libvirt.virConnect, name: str) -> libvirt.virDomain:
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise MachineError("no_domain", f"no domain named '{name}'") from exc
        raise


def _domain_state(domain: libvirt.virDomain) -> str:
    try:
        return _DOMAIN_STATE.get(domain.state()[0], "unknown")
    except libvirt.libvirtError:
        return "unknown"


def _domain_document(domain: libvirt.virDomain) -> dict[str, Any]:
    """What a power command says about the domain it acted on."""
    return {
        "name": domain.name(),
        "uuid": domain.UUIDString(),
        "state": _domain_state(domain),
    }


def _version(conn: libvirt.virConnect) -> str:
    """The hypervisor's libvirt version as major.minor.micro."""
    try:
        raw = conn.getVersion()
    except libvirt.libvirtError:
        return ""
    if not isinstance(raw, int):
        return str(raw)
    return f"{raw // 1000000}.{raw % 1000000 // 1000}.{raw % 1000}"


def _host_capabilities(conn: libvirt.virConnect) -> dict[str, Any]:
    """Guest architectures and whether KVM is available on this host."""
    try:
        root = ET.fromstring(conn.getCapabilities())
    except (libvirt.libvirtError, ET.ParseError) as exc:
        raise MachineError("capabilities_failed", f"cannot read host capabilities: {exc}") from exc

    arches = set()
    kvm = False
    for guest in root.findall("guest"):
        for arch in guest.findall("arch"):
            name = arch.get("name")
            if name:
                arches.add(name)
                if arch.find("domain[@type='kvm']") is not None:
                    kvm = True
        # Older capability documents name the architecture on <guest> itself.
        if guest.get("arch"):
            arches.add(guest.get("arch"))
    return {"arches": arches, "kvm": kvm}


def _firmware_entries(conn: libvirt.virConnect) -> list[dict[str, Any]]:
    """One entry per firmware and architecture pair the hypervisor offers."""
    entries = []
    for firmware in get_uefi_files(conn) or []:
        for arch in firmware.architectures or []:
            entries.append(
                {
                    "arch": arch,
                    "code": firmware.executable,
                    "vars": firmware.nvram_template,
                    "secure": "secure-boot" in (firmware.features or []),
                }
            )
    return entries


def _address(domain: libvirt.virDomain, macs: list[str]) -> str | None:
    """The guest's IPv4 address, preferring the address of our own interfaces."""
    for source, flag in _ADDRESS_SOURCES:
        try:
            table = domain.interfaceAddresses(flag)
        except libvirt.libvirtError:
            continue
        addresses = table or {}
        for mac in macs:
            entry = addresses.get(mac) or {}
            if entry.get("addr"):
                return str(entry["addr"])
        for entry in addresses.values():
            if entry.get("addr"):
                return str(entry["addr"])
    return None


def _disks(root: ET.Element) -> list[dict[str, Any]]:
    disks = []
    for disk in root.findall("devices/disk"):
        source = disk.find("source")
        target = disk.find("target")
        path = None
        if source is not None:
            path = source.get("file") or source.get("dev") or source.get("name")
        disks.append(
            {
                "device": disk.get("device"),
                "type": disk.get("type"),
                "dev": target.get("dev") if target is not None else None,
                "path": path,
            }
        )
    return disks


def _parse_size(value: Any, what: str = "size") -> int:
    """Read '20G', '512M' or a plain byte count as bytes."""
    match = _SIZE_PATTERN.fullmatch(str(value or "").strip().upper())
    if not match:
        raise MachineError(
            "bad_argument", f"invalid {what} '{value}', expected e.g. 20G, 512M or 4096"
        )
    number = int(match.group(1)) * _SIZE_UNITS[match.group(2)]
    if number <= 0:
        raise MachineError("bad_argument", f"{what} must be greater than zero, got '{value}'")
    return number


def _pool_by_name(conn: libvirt.virConnect, name: str):
    try:
        return conn.storagePoolLookupByName(name)
    except libvirt.libvirtError as exc:
        raise MachineError("no_pool", f"no storage pool named '{name}'") from exc


def _volume_by_name(pool, name: str):
    try:
        return pool.storageVolLookupByName(name)
    except libvirt.libvirtError as exc:
        raise MachineError(
            "no_volume", f"no volume named '{name}' in pool '{pool.name()}'"
        ) from exc


def _volume_at(conn: libvirt.virConnect, spec: str):
    """Find a volume written as '<pool>/<volume>' or as a path on the hypervisor."""
    if spec.startswith("/"):
        try:
            return conn.storageVolLookupByPath(spec)
        except libvirt.libvirtError as exc:
            raise MachineError("no_volume", f"no volume at path '{spec}'") from exc
    pool_name, separator, volume_name = spec.partition("/")
    if not separator or not volume_name:
        raise MachineError(
            "bad_argument",
            f"invalid volume '{spec}', expected <pool>/<volume> or a path on the hypervisor",
        )
    return _volume_by_name(_pool_by_name(conn, pool_name), volume_name)


def _pool_document(pool) -> dict[str, Any]:
    info = pool.info()
    target = ET.fromstring(pool.XMLDesc(0)).find("target/path")
    return {
        "name": pool.name(),
        "state": _POOL_STATE.get(info[0], "unknown"),
        "path": target.text if target is not None else None,
        "autostart": bool(pool.autostart()),
        "capacity_bytes": info[1],
        "allocation_bytes": info[2],
        "available_bytes": info[3],
    }


def _volume_format(vol) -> str | None:
    """The on-disk format a volume records for itself."""
    try:
        root = ET.fromstring(vol.XMLDesc(0))
    except libvirt.libvirtError:
        return None
    node = root.find("target/format")
    return node.get("type") if node is not None else None


def _volume_document(vol) -> dict[str, Any]:
    _, capacity, allocation = _safe_get_volume_info(vol)
    root = ET.fromstring(vol.XMLDesc(0))
    format_node = root.find("target/format")
    backing_node = root.find("backingStore")
    try:
        pool_name = vol.storagePoolLookupByVolume().name()
    except libvirt.libvirtError:
        pool_name = ""
    document = {
        "pool": pool_name,
        "name": vol.name(),
        "path": vol.path(),
        "format": format_node.get("type") if format_node is not None else None,
        "capacity_bytes": capacity,
        "allocation_bytes": allocation,
    }
    if backing_node is not None and (backing_node.findtext("path") or ""):
        # libvirt writes the format either as a <format> child or as an attribute of
        # <backingStore>, depending on how the volume was created.
        format_node = backing_node.find("format")
        document["backing"] = {
            "path": backing_node.findtext("path").strip(),
            "format": (format_node.get("type") if format_node is not None else None)
            or backing_node.get("format"),
        }
    return document


def _start_pool(pool) -> None:
    """Bring a pool up, building its target directory the first time."""
    try:
        pool.create(0)
        return
    except libvirt.libvirtError as exc:
        first = exc
    try:
        pool.build(0)
        pool.create(0)
    except libvirt.libvirtError as exc:
        raise MachineError(
            "pool_ensure_failed", f"cannot start storage pool '{pool.name()}': {exc or first}"
        ) from exc


#
# Commands
#


def cmd_capabilities(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """What this hypervisor is, and whether it speaks our machine API."""
    _reject_leftover(args, "capabilities")
    caps = _host_capabilities(conn)
    return {
        "api": API_LEVEL,
        "libvirt_version": _version(conn),
        "arch": get_host_architecture(conn),
        "arches": sorted(caps["arches"]),
        "kvm": caps["kvm"],
        "firmware": _firmware_entries(conn),
    }


def cmd_host_info(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """CPU, memory and KVM availability."""
    _reject_leftover(args, "host_info")
    try:
        node = conn.getInfo()
        # getMemoryStats reports KiB; every memory field here is MiB.
        stats = conn.getMemoryStats(libvirt.VIR_NODE_MEMORY_STATS_ALL_CELLS)
    except libvirt.libvirtError as exc:
        raise MachineError("host_info_failed", f"cannot read host resources: {exc}") from exc
    return {
        "arch": get_host_architecture(conn),
        "model": node[0],
        "cpus": node[2],
        # virNodeGetInfo already gives MiB, unlike the stats.
        "memory_mb": node[1],
        "available_memory_mb": stats.get("total", 0) // 1024,
        "free_memory_mb": stats.get("free", 0) // 1024,
        "kvm": _host_capabilities(conn)["kvm"],
    }


def cmd_list(conn: libvirt.virConnect, args: list[str]) -> list[dict[str, Any]]:
    """Every domain on the connection, running or not."""
    _reject_leftover(args, "list")
    return [
        {"name": domain.name(), "state": _domain_state(domain), "uuid": domain.UUIDString()}
        for domain in conn.listAllDomains(0)
    ]


def cmd_dominfo(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """State, addresses, disks and NVRAM of one domain."""
    if not args:
        raise MachineError("bad_argument", "dominfo needs a domain name")
    domain = _domain(conn, args[0])
    _reject_leftover(args[1:], "dominfo")
    root = ET.fromstring(domain.XMLDesc(0))
    macs = [
        mac.get("address") for mac in root.findall("devices/interface/mac") if mac.get("address")
    ]
    nvram = root.find("os/nvram")
    return {
        "name": domain.name(),
        "uuid": domain.UUIDString(),
        "state": _domain_state(domain),
        "macs": macs,
        "ipv4": _address(domain, macs) if domain.isActive() else None,
        "disks": _disks(root),
        "nvram": nvram.text if nvram is not None else None,
    }


def cmd_pool_info(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """State, path and capacity of one storage pool."""
    if not args:
        raise MachineError("bad_argument", "pool_info needs a pool name")
    _reject_leftover(args[1:], "pool_info")
    return _pool_document(_pool_by_name(conn, args[0]))


def cmd_net_info(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Bridge, forward mode and DHCP range of one network, plus whether it is active."""
    if not args:
        raise MachineError("bad_argument", "net_info needs a network name")
    _reject_leftover(args[1:], "net_info")
    info = get_network_info(conn, args[0])
    if not info:
        raise MachineError("no_network", f"no network named '{args[0]}'")
    try:
        info["active"] = bool(conn.networkLookupByName(args[0]).isActive())
    except libvirt.libvirtError:
        info["active"] = False
    return info


def cmd_pool_ensure(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Make a directory pool exist and run, creating it where it is missing."""
    if not args:
        raise MachineError("bad_argument", "pool_ensure needs <name> [--path <dir>]")
    name, args = args[0], args[1:]
    path = _take_option(args, "path")
    _reject_leftover(args, "pool_ensure")

    try:
        pool = conn.storagePoolLookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_POOL:
            raise MachineError(
                "pool_ensure_failed", f"cannot look up storage pool '{name}': {exc}"
            ) from exc
        pool = None

    created = pool is None
    if created:
        target = path or (
            _DEFAULT_POOL_PATH if name == "default" else f"{_DEFAULT_POOL_PATH}/{name}"
        )
        try:
            pool = create_storage_pool(conn, name, "dir", target)
        except libvirt.libvirtError as exc:
            raise MachineError(
                "pool_ensure_failed", f"cannot create storage pool '{name}': {exc}"
            ) from exc
        if pool is None:
            raise MachineError(
                "pool_ensure_failed", f"cannot create storage pool '{name}' at '{target}'"
            )
    else:
        document = _pool_document(pool)
        if path and document["path"] and os.path.realpath(path) != document["path"]:
            raise MachineError(
                "pool_path_conflict",
                f"storage pool '{name}' already uses '{document['path']}', not '{path}'",
            )
        if document["state"] != "running":
            _start_pool(pool)

    document = _pool_document(pool)
    document["created"] = created
    return document


def cmd_vol_info(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Report one volume, named '<pool>/<volume>', as a pool and volume, or by path."""
    if not args:
        raise MachineError("bad_argument", "vol_info needs <pool> <volume>")
    _reject_leftover(args[2:], "vol_info")
    if len(args) == 1:
        return _volume_document(_volume_at(conn, args[0]))
    return _volume_document(_volume_by_name(_pool_by_name(conn, args[0]), args[1]))


def cmd_vol_create(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Create a volume, empty or as a qcow2 overlay of one that already exists."""
    if len(args) < 2:
        raise MachineError(
            "bad_argument",
            "vol_create needs <pool> <name> --size <size> "
            "[--backing <pool/volume|path>] [--format qcow2]",
        )
    pool_name, name, args = args[0], args[1], args[2:]
    size = _take_option(args, "size")
    backing = _take_option(args, "backing")
    volume_format = _take_option(args, "format") or ("qcow2" if backing else "raw")
    _reject_leftover(args, "vol_create")

    pool = _pool_by_name(conn, pool_name)
    if _pool_document(pool)["state"] != "running":
        _start_pool(pool)

    backing_path = backing_format = None
    if backing:
        backing_vol = _volume_at(conn, backing)
        backing_path = backing_vol.path()
        backing_format = _volume_format(backing_vol) or "qcow2"
        capacity = _parse_size(size) if size else _safe_get_volume_info(backing_vol)[1]
        if not capacity:
            raise MachineError(
                "bad_argument",
                f"cannot tell the size of backing volume '{backing}', pass --size",
            )
    else:
        if not size:
            raise MachineError("bad_argument", "vol_create needs --size <size>")
        capacity = _parse_size(size)

    xml = (
        f"<volume><name>{escape(name)}</name>"
        f"<capacity unit='bytes'>{capacity}</capacity>"
        f"<target><format type='{escape(volume_format)}'/></target>"
    )
    if backing_path:
        xml += (
            f"<backingStore><path>{escape(backing_path)}</path>"
            f"<format type='{escape(backing_format)}'/></backingStore>"
        )
    xml += "</volume>"

    try:
        vol = pool.createXML(xml, 0)
    except libvirt.libvirtError as exc:
        raise MachineError(
            "vol_create_failed", f"cannot create volume '{name}' in '{pool_name}': {exc}"
        ) from exc
    if vol is None:
        raise MachineError("vol_create_failed", f"cannot create volume '{name}' in '{pool_name}'")
    return _volume_document(vol)


def cmd_vol_upload(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Copy a file of this machine into a volume that exists on the hypervisor."""
    if len(args) < 3:
        raise MachineError(
            "bad_argument",
            "vol_upload needs <pool> <volume> <local-path> [--offset <size>]",
        )
    pool_name, volume_name, local_path, args = args[0], args[1], args[2], args[3:]
    offset_option = _take_option(args, "offset")
    offset = _parse_size(offset_option, "offset") if offset_option else 0
    _reject_leftover(args, "vol_upload")
    if not os.path.exists(local_path):
        raise MachineError("no_file", f"no such file: {local_path}")

    vol = _volume_by_name(_pool_by_name(conn, pool_name), volume_name)
    size = os.path.getsize(local_path)
    stream = conn.newStream(0)
    try:
        vol.upload(stream, offset, size)
        with open(local_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                stream.send(chunk)
        stream.finish()
    except libvirt.libvirtError as exc:
        # finish() releases the stream; a failed transfer is abandoned instead.
        try:
            stream.abort()
        except libvirt.libvirtError:
            pass
        raise MachineError(
            "upload_failed",
            f"cannot upload '{local_path}' to '{pool_name}/{volume_name}': {exc}",
        ) from exc
    document = _volume_document(vol)
    document["bytes"] = size
    return document


def cmd_vol_delete(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Delete a volume. libvirt refuses while a running domain holds it open."""
    if len(args) < 2:
        raise MachineError("bad_argument", "vol_delete needs <pool> <volume> [--snapshots]")
    pool_name, volume_name, args = args[0], args[1], args[2:]
    with_snapshots = _take_flag(args, "snapshots")
    _reject_leftover(args, "vol_delete")
    vol = _volume_by_name(_pool_by_name(conn, pool_name), volume_name)
    flags = (
        libvirt.VIR_STORAGE_VOL_DELETE_WITH_SNAPSHOTS
        if with_snapshots
        else libvirt.VIR_STORAGE_VOL_DELETE_NORMAL
    )
    try:
        vol.delete(flags)
    except libvirt.libvirtError as exc:
        raise MachineError(
            "vol_delete_failed",
            f"cannot delete volume '{volume_name}' from '{pool_name}': {exc}",
        ) from exc
    return {"deleted": True, "pool": pool_name, "name": volume_name}


def cmd_define(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Define a domain from XML read from a file, or from stdin when that is '-'."""
    if not args:
        raise MachineError("bad_argument", "define needs <xml-file|->")
    _reject_leftover(args[1:], "define")
    if args[0] == "-":
        xml = sys.stdin.read()
    else:
        if not os.path.exists(args[0]):
            raise MachineError("no_file", f"no such file: {args[0]}")
        with open(args[0], encoding="utf-8") as handle:
            xml = handle.read()
    if not xml.strip():
        raise MachineError("bad_argument", "the domain XML is empty")

    try:
        domain = conn.defineXML(xml)
    except libvirt.libvirtError as exc:
        raise MachineError("define_failed", f"cannot define the domain: {exc}") from exc
    if domain is None:
        raise MachineError("define_failed", "cannot define the domain")
    return _domain_document(domain)


def cmd_start(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Boot a domain; one that is already running is answered as it is."""
    if not args:
        raise MachineError("bad_argument", "start needs a domain name")
    name, args = args[0], args[1:]
    _reject_leftover(args, "start")
    domain = _domain(conn, name)
    if not domain.isActive():
        try:
            # start_vm checks that the domain's disks are there before booting it,
            # and reports a missing volume as a plain Exception rather than a libvirt one.
            start_vm(domain)
        except Exception as exc:
            raise MachineError("start_failed", f"cannot start domain '{name}': {exc}") from exc
    return _domain_document(domain)


def cmd_shutdown(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Press the power button and return; the guest takes its time to go down."""
    if not args:
        raise MachineError("bad_argument", "shutdown needs a domain name")
    name, args = args[0], args[1:]
    _reject_leftover(args, "shutdown")
    domain = _domain(conn, name)
    if domain.isActive():
        try:
            stop_vm(domain)
        except libvirt.libvirtError as exc:
            raise MachineError(
                "shutdown_failed", f"cannot shut down domain '{name}': {exc}"
            ) from exc
    return _domain_document(domain)


def cmd_destroy(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Power a domain off without asking the guest."""
    if not args:
        raise MachineError("bad_argument", "destroy needs a domain name")
    name, args = args[0], args[1:]
    _reject_leftover(args, "destroy")
    domain = _domain(conn, name)
    if domain.isActive():
        try:
            force_off_vm(domain)
        except libvirt.libvirtError as exc:
            raise MachineError(
                "destroy_failed", f"cannot force off domain '{name}': {exc}"
            ) from exc
    return _domain_document(domain)


def cmd_undefine(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Forget a domain, optionally with its UEFI variables, snapshots and saved state."""
    if not args:
        raise MachineError(
            "bad_argument", "undefine needs <domain> [--nvram] [--snapshots] [--save]"
        )
    name, args = args[0], args[1:]
    with_nvram = _take_flag(args, "nvram")
    with_snapshots = _take_flag(args, "snapshots")
    with_saved = _take_flag(args, "save")
    _reject_leftover(args, "undefine")
    domain = _domain(conn, name)
    if domain.isActive():
        raise MachineError(
            "undefine_failed", f"domain '{name}' is still running; shut it down first"
        )

    flags = 0
    if with_nvram:
        flags |= libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    if with_snapshots:
        flags |= libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
    if with_saved:
        flags |= libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
    try:
        if flags:
            domain.undefineFlags(flags)
        else:
            domain.undefine()
    except libvirt.libvirtError as exc:
        raise MachineError("undefine_failed", f"cannot undefine domain '{name}': {exc}") from exc
    return {"undefined": True, "name": name}


#: How long `installvm --wait` looks for a guest address before it stops. An installer
#: takes minutes, so this is what a caller waits rather than a deadline the install has
#: to meet: no address by then is reported as no address, not as a failed install.
DEFAULT_INSTALL_TIMEOUT = 600


def _positive_int(raw: str) -> int:
    """A count answered on the command line, which has to be at least one.

    A flag asked for with no count in it is a mistake in the command, so it is reported
    as one here rather than reaching the plan, where a value that settles nothing comes
    back as a question.
    """
    number = int(raw)  # a value that is not a number is reported by the caller
    if number < 1:
        raise ValueError(f"'{raw}' is not a positive number")
    return number


def _positive_bytes(raw: str) -> int:
    """A size answered on the command line, in bytes, which has to be at least one."""
    return _positive_int(str(_parse_size(raw)))


#: The flags `installvm` answers a plan with, as (flag, plan key, what to make of it).
#: `--dist` and `--cpus` are the words a human and a distribution catalog use for the
#: keys named `os` and `vcpu`; on a command line both names give the same answer.
_INSTALL_ANSWER_FLAGS = (
    ("iso", "iso", str),
    ("os", "os", str),
    ("dist", "os", str),
    ("pool", "pool", str),
    ("network", "network", str),
    ("memory", "memory_mb", _positive_int),
    ("cpus", "vcpu", _positive_int),
    ("disk", "disk_size_bytes", _positive_bytes),
)


def _install_name(name: str | None) -> str:
    """The name an install is asked for, in the form libvirt and `vmc list` use.

    The rule the New VM wizard applies to a name typed into it (see
    `utils._sanitize_domain_name`); a name arriving on a command line has had no other
    chance to be cleaned up, and a domain named with a character libvirt refuses cannot
    be found again by the caller that asked for it.
    """
    if not name:
        raise MachineError("bad_argument", "installvm needs the name of the domain to install")
    try:
        cleaned, _changed = _sanitize_domain_name(name)
    except ValueError as exc:
        raise MachineError("bad_argument", f"name: {exc}") from exc
    if not cleaned:
        raise MachineError("bad_argument", f"'{name}' has no characters a domain name can hold")
    return cleaned


def _read_answers(path: str) -> Any:
    """The document `--answer` names; `install_plan.apply_answers` is what reads it."""
    if not os.path.exists(path):
        raise MachineError("no_file", f"no answer file '{path}'")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MachineError("bad_answer", f"answer file '{path}': {exc}") from exc


def _timeout_seconds(value: str) -> int:
    """A `--timeout` as whole seconds."""
    try:
        seconds = int(value)
    except ValueError:
        raise MachineError(
            "bad_argument", f"--timeout: '{value}' is not a number of seconds"
        ) from None
    if seconds < 1:
        raise MachineError(
            "bad_argument", f"--timeout: '{value}' is not a positive number of seconds"
        )
    return seconds


def cmd_installvm(conn: libvirt.virConnect, args: list[str]) -> dict[str, Any]:
    """Install a domain from an ISO, or say what that install would do.

    An install decides a name, an ISO, an OS, a pool, a disk and a network before it can
    start, and every one of those decisions is worked out into a plan first (see
    `install_plan`), by the same code the New VM wizard runs. `--resolve` answers with
    that plan and changes nothing. Without it the plan has to be closed: a decision still
    open is a `needs_answer` error naming it, because a machine reading this output has no
    way to be asked a question halfway through an install, and a plan the caller answered
    is the only way to say what it wanted.
    """
    answers_path = _take_option(args, "answer")
    timeout = _take_option(args, "timeout")
    resolve = _take_flag(args, "resolve")
    wait = _take_flag(args, "wait")

    answers: dict[str, Any] = {}
    for flag, key, cast in _INSTALL_ANSWER_FLAGS:
        raw = _take_option(args, flag)
        if raw is None:
            continue
        try:
            answers[key] = cast(raw)
        except ValueError as exc:
            raise MachineError("bad_argument", f"--{flag}: {exc}") from exc

    name = _take_option(args, "name")
    if name is None and args and not args[0].startswith("-"):
        name = args.pop(0)
    _reject_leftover(args, "installvm")
    name = _install_name(name)
    limit = _timeout_seconds(timeout) if timeout is not None else DEFAULT_INSTALL_TIMEOUT

    if answers_path is not None:
        try:
            answers = {**install_plan.apply_answers(_read_answers(answers_path)), **answers}
        except install_plan.NeedsAnswer as exc:
            raise MachineError("bad_answer", f"--answer: {exc}") from exc

    provisioner = VMProvisioner(conn)
    plan = install_plan.build_plan(provisioner, name, _host_capabilities(conn), answers)
    if resolve:
        return {"plan": plan}
    if plan["unresolved"]:
        open_decisions = "; ".join(
            f"{entry['key']}: {entry['reason']}"
            + (f" (one of: {', '.join(entry['choices'])})" if entry["choices"] else "")
            for entry in plan["unresolved"]
        )
        raise MachineError(
            "needs_answer", f"an install of '{name}' is not settled - {open_decisions}"
        )
    if plan["exists"]:
        raise MachineError("domain_exists", f"a domain named '{name}' is already defined")
    if plan["tools_missing"]:
        # Named here rather than left to the install: without these the installer never
        # starts, and `install_failed` saying which tool is missing beats a timeout.
        raise MachineError(
            "install_failed",
            f"an install of {plan['os']} needs {', '.join(plan['tools_missing'])} on the hypervisor",
        )

    domain = install_plan.provision_from_plan(provisioner, plan, name)
    if not domain:
        raise MachineError(
            "install_failed",
            f"could not install '{name}'; the hypervisor's virt-manager log says why",
        )
    result: dict[str, Any] = {
        "name": domain.name(),
        "uuid": domain.UUIDString(),
        "ipv4": None,
        "state": _domain_state(domain),
    }
    if wait:
        result["ipv4"] = _wait_for_address(domain, limit)
    return result


def _wait_for_address(domain: libvirt.virDomain, limit: int) -> str | None:
    """The guest's address, waiting up to `limit` seconds for the install to report one.

    An address is the only sign from outside that something is running inside the guest:
    libvirt says a domain is running, not that anything booted in it. So no address by the
    deadline is reported as `null`, which is still a success: the install happened, and
    the caller that wants the lease can look it up itself or ask again with `dominfo`.
    """
    deadline = time.monotonic() + limit
    while True:
        try:
            address = _address(domain, [])
        except libvirt.libvirtError:
            return None  # the domain is gone, so nothing will report an address
        if address or time.monotonic() >= deadline:
            return address
        time.sleep(2)


_COMMANDS: dict[str, Callable[[libvirt.virConnect, list[str]], Any]] = {
    "installvm": cmd_installvm,
    "define": cmd_define,
    "start": cmd_start,
    "shutdown": cmd_shutdown,
    "destroy": cmd_destroy,
    "undefine": cmd_undefine,
    "capabilities": cmd_capabilities,
    "host_info": cmd_host_info,
    "list": cmd_list,
    "dominfo": cmd_dominfo,
    "pool_info": cmd_pool_info,
    "net_info": cmd_net_info,
    "pool_ensure": cmd_pool_ensure,
    "vol_info": cmd_vol_info,
    "vol_create": cmd_vol_create,
    "vol_upload": cmd_vol_upload,
    "vol_delete": cmd_vol_delete,
}


def run(command: str, server: str | None = None, config_file: str | None = None) -> int:
    """Run one machine command and return the process exit code."""
    try:
        argv = shlex.split(command)
        if not argv:
            raise MachineError("empty_command", "no command given")
        handler = _COMMANDS.get(argv[0])
        if handler is None:
            known = ", ".join(sorted(_COMMANDS))
            raise MachineError("unknown_command", f"unknown command '{argv[0]}' (known: {known})")

        conn = _connect(server, config_file)
        try:
            result = handler(conn, argv[1:])
        finally:
            try:
                conn.close()
            except libvirt.libvirtError:
                pass
    except MachineError as exc:
        emit({"error": {"code": exc.code, "message": exc.message}})
        return 1
    except libvirt.libvirtError as exc:
        emit(
            {
                "error": {
                    "code": "libvirt_error",
                    "message": sanitize_sensitive_data(str(exc).strip()),
                }
            }
        )
        return 1
    except ValueError as exc:
        # shlex.split on an unbalanced quote.
        emit({"error": {"code": "bad_command", "message": str(exc)}})
        return 1
    except Exception as exc:  # a bug still has to look like a failure, not a traceback
        logging.exception("machine command %r failed", command)
        emit({"error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}})
        return 1

    emit(result)
    return 0
