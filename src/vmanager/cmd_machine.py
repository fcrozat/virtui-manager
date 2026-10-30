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
import shlex
import sys
import xml.etree.ElementTree as ET
from typing import Any, Callable

import libvirt

from .config import load_config
from .firmware_manager import get_uefi_files
from .libvirt_utils import get_host_architecture, get_network_info
from .utils import sanitize_sensitive_data

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
    try:
        pool = conn.storagePoolLookupByName(args[0])
    except libvirt.libvirtError as exc:
        raise MachineError("no_pool", f"no storage pool named '{args[0]}'") from exc
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


_COMMANDS: dict[str, Callable[[libvirt.virConnect, list[str]], Any]] = {
    "capabilities": cmd_capabilities,
    "host_info": cmd_host_info,
    "list": cmd_list,
    "dominfo": cmd_dominfo,
    "pool_info": cmd_pool_info,
    "net_info": cmd_net_info,
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
