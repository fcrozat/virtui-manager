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
        with patch.object(sys, "argv", ["vmc", "--json", "-c", "pool_info", "default"]), patch.object(
            sys, "stderr", io.StringIO()
        ), patch.object(sys, "stdout", document):
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


if __name__ == "__main__":
    unittest.main()
