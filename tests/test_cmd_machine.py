"""
Tests for the machine-readable CLI: `vmc --json -c "<command>"`.

What is checked here is the contract a program driving virtui-manager relies
on: stdout is one JSON document and nothing else, a failure is a JSON error
with a code, and the exit code says which of the two it was.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Add the src directory to the path to import vmanager modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import libvirt  # noqa: E402

from vmanager import (
    cmd_machine,  # noqa: E402
    vmanager_cmd,  # noqa: E402
)

DOMAIN_XML = """<domain type='kvm'>
  <name>vm</name>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <nvram template='/usr/share/qemu/ovmf-vars.bin'>/var/lib/libvirt/qemu/nvram/vm_VARS.bin</nvram>
  </os>
  <devices>
    <interface type='network'><mac address='52:54:00:00:00:01'/><source network='default'/></interface>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/vm.qcow2'/><target dev='vda'/>
    </disk>
  </devices>
</domain>"""

CAPABILITIES_XML = """<capabilities>
  <host><cpu><arch>x86_64</arch></cpu></host>
  <guest arch='x86_64' os_type='hvm'>
    <arch name='x86_64'>
      <domain type='kvm'><machine>pc-q35-8.1</machine></domain>
      <domain type='qemu'><machine>pc</machine></domain>
    </arch>
  </guest>
</capabilities>"""

CONFIG = {"servers": [{"name": "local", "uri": "qemu:///system"}]}


def make_domain(
    name="vm",
    state=libvirt.VIR_DOMAIN_RUNNING,
    xml=DOMAIN_XML,
    active=True,
    addresses=None,
):
    """A domain quiet enough that the commands under test see only what they ask."""
    domain = MagicMock()
    domain.name.return_value = name
    domain.UUIDString.return_value = "uuid-of-" + name
    domain.state.return_value = (state,)
    domain.XMLDesc.return_value = xml
    domain.isActive.return_value = active
    if addresses is None:
        domain.interfaceAddresses.side_effect = libvirt.libvirtError("no addresses")
    else:
        domain.interfaceAddresses.return_value = addresses
    return domain


def make_connection(domains=(), error_code=None):
    """A connection whose lookups answer what the test handed in."""
    conn = MagicMock()
    conn.getCapabilities.return_value = CAPABILITIES_XML
    conn.getVersion.return_value = 10002000
    conn.listAllDomains.return_value = list(domains)

    def lookup(name):
        for domain in domains:
            if domain.name.return_value == name:
                return domain
        error = libvirt.libvirtError(f"no domain named '{name}'")
        error.get_error_code = lambda: libvirt.VIR_ERR_NO_DOMAIN
        raise error

    conn.lookupByName.side_effect = lookup
    return conn


VOLUME_XML = """<volume type='file'>
  <name>base</name>
  <capacity unit='bytes'>10737418240</capacity>
  <target><path>/var/lib/libvirt/images/base.qcow2</path><format type='qcow2'/></target>
</volume>"""

OVERLAY_XML = """<volume type='file'>
  <name>disk</name>
  <target><format type='qcow2'/></target>
  <backingStore type='file'>
    <path>/var/lib/libvirt/images/base.qcow2</path><format type='qcow2'/>
  </backingStore>
</volume>"""


def make_volume(
    name="base",
    path="/var/lib/libvirt/images/base.qcow2",
    capacity=10737418240,
    allocation=197120,
    xml=VOLUME_XML,
    pool=None,
):
    """A volume that answers what the storage commands read from one."""
    vol = MagicMock()
    vol.name.return_value = name
    vol.path.return_value = path
    vol.info.return_value = (0, capacity, allocation)
    vol.XMLDesc.return_value = xml
    vol.storagePoolLookupByVolume.return_value = pool or make_pool()
    return vol


def make_pool(name="default", path="/var/lib/libvirt/images", active=True, volumes=()):
    """A dir pool holding the volumes the test handed in."""
    pool = MagicMock()
    pool.name.return_value = name
    pool.autostart.return_value = 1
    pool.XMLDesc.return_value = (
        f"<pool type='dir'><name>{name}</name><target><path>{path}</path></target></pool>"
    )
    pool.info.return_value = (2 if active else 0, 100 * 1024**3, 40 * 1024**3, 60 * 1024**3)
    pool.isActive.return_value = active

    def find_volume(volume_name):
        for volume in volumes:
            if volume.name.return_value == volume_name:
                return volume
        error = libvirt.libvirtError(f"no volume named '{volume_name}'")
        error.get_error_code = lambda: libvirt.VIR_ERR_NO_STORAGE_VOL
        raise error

    pool.storageVolLookupByName.side_effect = find_volume
    return pool


def make_pool_connection(pools=(), volumes=()):
    """A connection whose pool lookups answer what the test handed in."""
    conn = make_connection()
    known = {pool.name.return_value: pool for pool in pools}

    def find_pool(pool_name):
        if pool_name in known:
            return known[pool_name]
        error = libvirt.libvirtError(f"no storage pool named '{pool_name}'")
        error.get_error_code = lambda: libvirt.VIR_ERR_NO_STORAGE_POOL
        raise error

    conn.storagePoolLookupByName.side_effect = find_pool

    def find_volume_by_path(path):
        for volume in volumes:
            if volume.path.return_value == path:
                return volume
        error = libvirt.libvirtError(f"no volume at path '{path}'")
        error.get_error_code = lambda: libvirt.VIR_ERR_NO_STORAGE_VOL
        raise error

    conn.storageVolLookupByPath.side_effect = find_volume_by_path
    return conn


def run_command(command, conn=None, config=None, server=None):
    """Run a machine command, returning (exit code, the JSON document on stdout)."""
    document = io.StringIO()
    connection = conn if conn is not None else make_connection()
    with (
        patch.object(sys, "stdout", document),
        patch.object(
            cmd_machine, "load_config", return_value=config if config is not None else CONFIG
        ),
        patch.object(cmd_machine.libvirt, "open", return_value=connection),
    ):
        code = cmd_machine.run(command, server=server)
    return code, json.loads(document.getvalue())


class TestReadOnlyCommands(unittest.TestCase):
    def test_list_names_every_domain_with_its_state(self):
        conn = make_connection(
            [
                make_domain("running-vm", libvirt.VIR_DOMAIN_RUNNING),
                make_domain("off-vm", libvirt.VIR_DOMAIN_SHUTOFF, active=False),
            ]
        )

        code, document = run_command("list", conn)

        self.assertEqual(0, code)
        self.assertEqual(
            [
                {"name": "off-vm", "state": "shutoff", "uuid": "uuid-of-off-vm"},
                {"name": "running-vm", "state": "running", "uuid": "uuid-of-running-vm"},
            ],
            sorted(document, key=lambda entry: entry["name"]),
        )

    def test_dominfo_reports_macs_address_disks_and_nvram(self):
        mac = "52:54:00:00:00:01"
        domain = make_domain(addresses={mac: {"addr": "192.168.122.71", "type": 1}})
        conn = make_connection([domain])

        code, document = run_command("dominfo vm", conn)

        self.assertEqual(0, code)
        self.assertEqual("running", document["state"])
        self.assertEqual([mac], document["macs"])
        self.assertEqual("192.168.122.71", document["ipv4"])
        self.assertEqual(
            [
                {
                    "device": "disk",
                    "dev": "vda",
                    "path": "/var/lib/libvirt/images/vm.qcow2",
                    "type": "file",
                }
            ],
            document["disks"],
        )
        self.assertEqual("/var/lib/libvirt/qemu/nvram/vm_VARS.bin", document["nvram"])

    def test_dominfo_has_no_address_while_the_domain_is_off(self):
        domain = make_domain(state=libvirt.VIR_DOMAIN_SHUTOFF, active=False)
        conn = make_connection([domain])

        code, document = run_command("dominfo vm", conn)

        self.assertEqual(0, code)
        self.assertIsNone(document["ipv4"])

    def test_capabilities_reports_api_level_arches_kvm_and_firmware(self):
        firmware = MagicMock()
        firmware.architectures = ["x86_64"]
        firmware.executable = "/usr/share/qemu/ovmf-code.bin"
        firmware.nvram_template = "/usr/share/qemu/ovmf-vars.bin"
        firmware.features = ["secure-boot"]
        conn = make_connection()

        with (
            patch.object(cmd_machine, "get_host_architecture", return_value="x86_64"),
            patch.object(cmd_machine, "get_uefi_files", return_value=[firmware]),
        ):
            code, document = run_command("capabilities", conn)

        self.assertEqual(0, code)
        self.assertEqual(cmd_machine.API_LEVEL, document["api"])
        self.assertEqual("10.2.0", document["libvirt_version"])
        self.assertEqual(["x86_64"], document["arches"])
        self.assertTrue(document["kvm"])
        self.assertEqual(
            [
                {
                    "arch": "x86_64",
                    "code": "/usr/share/qemu/ovmf-code.bin",
                    "vars": "/usr/share/qemu/ovmf-vars.bin",
                    "secure": True,
                }
            ],
            document["firmware"],
        )

    def test_host_info_reports_memory_in_mebibytes(self):
        conn = make_connection()
        conn.getInfo.return_value = ["x86_64", 32768, 16, 3600, 1, 1, 8, 2]
        conn.getMemoryStats.return_value = {"total": 33554432, "free": 8388608}

        with patch.object(cmd_machine, "get_host_architecture", return_value="x86_64"):
            code, document = run_command("host_info", conn)

        self.assertEqual(0, code)
        self.assertEqual(16, document["cpus"])
        self.assertEqual(32768, document["memory_mb"])
        self.assertEqual(32768, document["available_memory_mb"])
        self.assertEqual(8192, document["free_memory_mb"])

    def test_pool_info_reads_state_path_and_capacity(self):
        pool = MagicMock()
        pool.name.return_value = "images"
        pool.info.return_value = (2, 1000, 400, 600)
        pool.autostart.return_value = 1
        pool.XMLDesc.return_value = (
            "<pool><target><path>/var/lib/libvirt/images</path></target></pool>"
        )
        conn = make_connection()
        conn.storagePoolLookupByName.return_value = pool

        code, document = run_command("pool_info images", conn)

        self.assertEqual(0, code)
        self.assertEqual("running", document["state"])
        self.assertEqual("/var/lib/libvirt/images", document["path"])
        self.assertTrue(document["autostart"])
        self.assertEqual(600, document["available_bytes"])

    def test_net_info_adds_whether_the_network_is_active(self):
        conn = make_connection()
        network = MagicMock()
        network.isActive.return_value = 1
        conn.networkLookupByName.return_value = network

        with patch.object(
            cmd_machine,
            "get_network_info",
            return_value={"name": "default", "bridge_name": "virbr0", "dhcp": True},
        ):
            code, document = run_command("net_info default", conn)

        self.assertEqual(0, code)
        self.assertTrue(document["active"])
        self.assertEqual("virbr0", document["bridge_name"])


class TestMachineContract(unittest.TestCase):
    def test_unknown_command_is_a_json_error_and_a_failing_exit(self):
        code, document = run_command("frobnicate")

        self.assertEqual(1, code)
        self.assertEqual("unknown_command", document["error"]["code"])
        self.assertIn("list", document["error"]["message"])

    def test_unknown_domain_is_reported_as_such(self):
        conn = make_connection()

        code, document = run_command("dominfo ghost", conn)

        self.assertEqual(1, code)
        self.assertEqual("no_domain", document["error"]["code"])

    def test_missing_configuration_file_is_reported(self):
        document = io.StringIO()
        with (
            patch.object(sys, "stdout", document),
            patch.object(
                cmd_machine,
                "load_config",
                side_effect=FileNotFoundError("configuration file not found: /nope"),
            ),
        ):
            code = cmd_machine.run("list", config_file="/nope")

        self.assertEqual(1, code)
        self.assertEqual("bad_configuration", json.loads(document.getvalue())["error"]["code"])

    def test_server_has_to_be_one_of_the_configured_ones(self):
        code, document = run_command("list", config=CONFIG, server="elsewhere")

        self.assertEqual(1, code)
        self.assertEqual("unknown_server", document["error"]["code"])

    def test_a_connection_with_no_name_to_choose_from_is_refused(self):
        code, document = run_command("list", config={"servers": [{"name": "a"}, {"name": "b"}]})

        self.assertEqual(1, code)
        self.assertEqual("no_server", document["error"]["code"])

    def test_connection_failures_have_one_code_whatever_libvirt_did(self):
        document = io.StringIO()
        with (
            patch.object(sys, "stdout", document),
            patch.object(cmd_machine, "load_config", return_value=CONFIG),
            patch.object(
                cmd_machine.libvirt,
                "open",
                side_effect=libvirt.libvirtError("Failed to connect socket"),
            ),
        ):
            code = cmd_machine.run("list")

        self.assertEqual(1, code)
        self.assertEqual("connection_failed", json.loads(document.getvalue())["error"]["code"])

    def test_libvirt_uri_passwords_do_not_reach_the_error(self):
        document = io.StringIO()
        with (
            patch.object(sys, "stdout", document),
            patch.object(
                cmd_machine,
                "load_config",
                return_value={
                    "servers": [{"name": "local", "uri": "qemu+ssh://user:sekret@host/system"}]
                },
            ),
            patch.object(cmd_machine.libvirt, "open", return_value=None),
        ):
            code = cmd_machine.run("list")

        self.assertEqual(1, code)
        self.assertNotIn("sekret", document.getvalue())

    def test_a_bug_still_leaves_one_json_document_and_a_failing_exit(self):
        conn = make_connection()
        conn.listAllDomains.side_effect = RuntimeError("boom")

        with patch("vmanager.cmd_machine.logging.exception"):
            code, document = run_command("list", conn)

        self.assertEqual(1, code)
        self.assertEqual("internal_error", document["error"]["code"])
        self.assertIn("boom", document["error"]["message"])

    def test_arguments_the_command_does_not_take_are_refused(self):
        conn = make_connection()

        code, document = run_command("list --all", conn)

        self.assertEqual(1, code)
        self.assertEqual("bad_argument", document["error"]["code"])
        conn.listAllDomains.assert_not_called()

    def test_unbalanced_quotes_are_refused_before_connecting(self):

        with patch.object(cmd_machine.libvirt, "open") as open_connection:
            document = io.StringIO()
            with patch.object(sys, "stdout", document):
                code = cmd_machine.run("dominfo 'unterminated")

        self.assertEqual(1, code)
        self.assertEqual("bad_command", json.loads(document.getvalue())["error"]["code"])
        open_connection.assert_not_called()


class TestCommandLine(unittest.TestCase):
    def test_json_mode_answers_with_usage_and_exit_two_without_a_command(self):
        document = io.StringIO()
        with (
            patch.object(sys, "argv", ["vmc", "--json"]),
            patch.object(sys, "stderr", io.StringIO()),
            patch.object(vmanager_cmd, "VManagerCMD") as shell,
        ):
            with patch.object(sys, "stdout", document):
                code = vmanager_cmd.main()

        self.assertEqual(2, code)
        self.assertEqual("bad_usage", json.loads(document.getvalue())["error"]["code"])
        shell.assert_not_called()

    def test_a_command_line_argparse_rejects_still_answers_with_json(self):
        document = io.StringIO()
        with (
            patch.object(sys, "argv", ["vmc", "--json", "-c", "pool_info", "default"]),
            patch.object(sys, "stderr", io.StringIO()),
            patch.object(sys, "stdout", document),
        ):
            with self.assertRaises(SystemExit) as caught:
                vmanager_cmd.main()

        self.assertEqual(2, caught.exception.code)
        self.assertEqual("bad_usage", json.loads(document.getvalue())["error"]["code"])

    def test_json_mode_runs_the_machine_path_without_the_interactive_shell(self):
        with (
            patch.object(sys, "argv", ["vmc", "--json", "--server", "local", "-c", "list"]),
            patch.object(vmanager_cmd, "VManagerCMD") as shell,
            patch.object(cmd_machine, "run", return_value=0) as machine_run,
        ):
            code = vmanager_cmd.main()

        self.assertEqual(0, code)
        shell.assert_not_called()
        machine_run.assert_called_once_with("list", server="local", config_file=None)


class TestSizes(unittest.TestCase):
    def test_sizes_are_read_the_way_a_human_types_them(self):
        for text, expected in [("4096", 4096), ("512M", 512 * 1024**2), ("20G", 20 * 1024**3)]:
            self.assertEqual(expected, cmd_machine._parse_size(text))

    def test_a_size_that_is_not_a_size_is_refused(self):
        for text in ["ten", "20X", "0", None]:
            with self.subTest(text=text):
                with self.assertRaises(cmd_machine.MachineError) as caught:
                    cmd_machine._parse_size(text)
                self.assertEqual("bad_argument", caught.exception.code)


class TestStorageCommands(unittest.TestCase):
    def test_pool_ensure_reports_an_existing_pool_without_creating_one(self):
        conn = make_pool_connection(pools=[make_pool("vm-sidekick")])

        with patch.object(cmd_machine, "create_storage_pool") as created:
            code, document = run_command("pool_ensure vm-sidekick", conn)

        self.assertEqual(0, code)
        self.assertFalse(document["created"])
        created.assert_not_called()

    def test_pool_ensure_creates_a_dir_pool_at_the_convention_for_its_name(self):
        conn = make_pool_connection()
        made = make_pool("vm-sidekick", path="/var/lib/libvirt/images/vm-sidekick", active=False)

        with patch.object(cmd_machine, "create_storage_pool", return_value=made) as created:
            code, document = run_command("pool_ensure vm-sidekick", conn)

        self.assertEqual(0, code)
        self.assertTrue(document["created"])
        self.assertEqual(
            (conn, "vm-sidekick", "dir", "/var/lib/libvirt/images/vm-sidekick"),
            created.call_args[0],
        )

    def test_pool_ensure_keeps_the_usual_path_for_the_default_pool(self):
        conn = make_pool_connection()

        with patch.object(cmd_machine, "create_storage_pool", return_value=make_pool()) as created:
            run_command("pool_ensure default", conn)

        self.assertEqual("/var/lib/libvirt/images", created.call_args[0][3])

    def test_pool_ensure_starts_a_pool_that_exists_but_is_inactive(self):
        pool = make_pool("vm-sidekick", active=False)
        conn = make_pool_connection(pools=[pool])

        code, document = run_command("pool_ensure vm-sidekick", conn)

        self.assertEqual(0, code)
        pool.create.assert_called_once_with(0)

    def test_pool_ensure_refuses_to_move_a_pool_that_is_somewhere_else(self):
        conn = make_pool_connection(pools=[make_pool("vm-sidekick")])

        code, document = run_command("pool_ensure vm-sidekick --path /srv/other", conn)

        self.assertEqual(1, code)
        self.assertEqual("pool_path_conflict", document["error"]["code"])

    def test_vol_info_reads_a_volume_by_pool_and_name(self):
        volume = make_volume()
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool], volumes=[volume])

        code, document = run_command("vol_info default base", conn)

        self.assertEqual(0, code)
        self.assertEqual("qcow2", document["format"])
        self.assertEqual(10737418240, document["capacity_bytes"])
        self.assertEqual(197120, document["allocation_bytes"])

    def test_vol_info_reads_a_volume_by_its_path_on_the_hypervisor(self):
        volume = make_volume()
        conn = make_pool_connection(volumes=[volume])

        code, document = run_command(f"vol_info {volume.path.return_value}", conn)

        self.assertEqual(0, code)
        self.assertEqual("base", document["name"])

    def test_vol_create_writes_an_empty_volume_of_the_requested_size(self):
        volume = make_volume(name="disk")
        pool = make_pool("default")
        pool.createXML.return_value = volume
        conn = make_pool_connection(pools=[pool])

        code, document = run_command("vol_create default disk --size 20G", conn)

        self.assertEqual(0, code)
        xml = pool.createXML.call_args[0][0]
        self.assertIn("<name>disk</name>", xml)
        self.assertIn("<capacity unit='bytes'>21474836480</capacity>", xml)
        self.assertIn("<format type='raw'/>", xml)
        self.assertNotIn("backingStore", xml)

    def test_vol_create_of_a_backing_volume_makes_an_overlay_the_hypervisor_can_read(self):
        base = make_volume()
        pool = make_pool("default", volumes=[base])
        pool.createXML.return_value = make_volume(name="disk", xml=OVERLAY_XML, pool=pool)
        conn = make_pool_connection(pools=[pool], volumes=[base])

        code, document = run_command("vol_create default disk --backing default/base", conn)

        self.assertEqual(0, code)
        xml = pool.createXML.call_args[0][0]
        self.assertIn("<format type='qcow2'/></target>", xml)
        self.assertIn("<backingStore>", xml)
        self.assertIn("/var/lib/libvirt/images/base.qcow2", xml)
        self.assertIn("<capacity unit='bytes'>10737418240</capacity>", xml)
        self.assertEqual("/var/lib/libvirt/images/base.qcow2", document["backing"]["path"])

    def test_vol_create_needs_a_size_when_there_is_nothing_to_back_it(self):
        conn = make_pool_connection(pools=[make_pool("default")])

        code, document = run_command("vol_create default disk", conn)

        self.assertEqual(1, code)
        self.assertEqual("bad_argument", document["error"]["code"])

    def test_a_volume_libvirt_refuses_to_create_is_reported_as_such(self):
        pool = make_pool("default")
        pool.createXML.side_effect = libvirt.libvirtError("no space left on device")
        conn = make_pool_connection(pools=[pool])

        code, document = run_command("vol_create default disk --size 20G", conn)

        self.assertEqual(1, code)
        self.assertEqual("vol_create_failed", document["error"]["code"])

    def test_vol_upload_sends_the_file_and_reports_its_size(self):
        volume = make_volume(name="base")
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool])
        stream = conn.newStream.return_value

        with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as handle:
            handle.write(b"a" * 4096)
            local_path = handle.name
        try:
            code, document = run_command(f"vol_upload default base {local_path}", conn)
        finally:
            os.unlink(local_path)

        self.assertEqual(0, code)
        volume.upload.assert_called_once_with(stream, 0, 4096)
        self.assertEqual(b"a" * 4096, b"".join(c[0][0] for c in stream.send.call_args_list))
        stream.finish.assert_called_once_with()
        self.assertEqual(4096, document["bytes"])

    def test_vol_upload_of_a_file_that_is_not_there_is_refused_before_connecting(self):
        conn = make_pool_connection(pools=[make_pool("default")])

        code, document = run_command("vol_upload default base /no/such/file.qcow2", conn)

        self.assertEqual(1, code)
        self.assertEqual("no_file", document["error"]["code"])
        conn.newStream.assert_not_called()

    def test_a_broken_upload_is_reported_and_the_stream_is_given_up(self):
        volume = make_volume(name="base")
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool])
        volume.upload.side_effect = libvirt.libvirtError("cannot write to volume")

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x")
            local_path = handle.name
        try:
            code, document = run_command(f"vol_upload default base {local_path}", conn)
        finally:
            os.unlink(local_path)

        self.assertEqual(1, code)
        self.assertEqual("upload_failed", document["error"]["code"])
        conn.newStream.return_value.abort.assert_called_once_with()

    def test_vol_delete_asks_libvirt_to_take_the_volume(self):
        volume = make_volume(name="disk")
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool])

        code, document = run_command("vol_delete default disk", conn)

        self.assertEqual(0, code)
        self.assertTrue(document["deleted"])
        volume.delete.assert_called_once_with(libvirt.VIR_STORAGE_VOL_DELETE_NORMAL)

    def test_vol_delete_can_take_the_snapshots_that_were_taken_from_it(self):
        volume = make_volume(name="disk")
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool])

        run_command("vol_delete default disk --snapshots", conn)

        volume.delete.assert_called_once_with(libvirt.VIR_STORAGE_VOL_DELETE_WITH_SNAPSHOTS)

    def test_a_volume_a_running_domain_holds_is_reported_as_a_failure(self):
        volume = make_volume(name="disk")
        volume.delete.side_effect = libvirt.libvirtError("volume is in use")
        pool = make_pool("default", volumes=[volume])
        conn = make_pool_connection(pools=[pool])

        code, document = run_command("vol_delete default disk", conn)

        self.assertEqual(1, code)
        self.assertEqual("vol_delete_failed", document["error"]["code"])


if __name__ == "__main__":
    unittest.main()
