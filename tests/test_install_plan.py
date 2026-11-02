"""
Tests for `vmc installvm` and the plan it acts on (`vmanager.install_plan`).

An install is the one machine command that has to decide several things before it
can start, so what is checked here is how those decisions are settled: from the
host and the ISO cache when nothing is answered, from the caller's answers when
something is, and as a `needs_answer` error naming the open decision when neither
can settle it - because machine mode has nobody to ask.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Add the src directory to the path to import vmanager modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import libvirt  # noqa: E402
from test_cmd_machine import make_connection, make_domain, run_command  # noqa: E402

from vmanager import cmd_machine, install_plan  # noqa: E402
from vmanager.vm_provisioner import OSType  # noqa: E402

ISO_NAME = "SLE-15-SP6-x86_64.iso"


def make_provisioner(domain=None):
    """A provisioner that reports what a plan reads from it, and records the install."""
    provisioner = MagicMock()
    provisioner.host_arch = "x86_64"
    provisioner.required_install_tools.return_value = ["virt-install"]
    provisioner.check_install_tools.return_value = []
    provisioner._resolve_os_type.return_value = OSType.OPENSUSE
    provisioner.provision_vm.return_value = domain or make_domain("install-me")
    return provisioner


class InstallTestCase(unittest.TestCase):
    """A host with one cached image, one active pool, no UEFI loader of its own.

    Everything a plan reads about the host is held here at its plainest, so a test
    that wants a different answer changes that one thing.
    """

    def setUp(self):
        self.provisioner = make_provisioner()
        # The cached image is a file that is really there: provisioning reads the path.
        handle = tempfile.NamedTemporaryFile("w", suffix="-" + ISO_NAME, delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.iso_path = handle.name
        self.cached_isos = [{"name": ISO_NAME, "path": self.iso_path, "size_bytes": 42}]
        self.pools = [
            {"name": "default", "status": "active"},
            {"name": "scratch", "status": "inactive"},
        ]
        for target, attribute, value in [
            (install_plan, "_cached_isos", lambda: self.cached_isos),
            (install_plan, "list_storage_pools", lambda conn: self.pools),
            (install_plan, "get_uefi_files", lambda conn: []),
            (install_plan, "select_best_firmware", lambda *a, **kw: None),
            (
                cmd_machine,
                "_host_capabilities",
                lambda conn: {"kvm": True, "architectures": ["x86_64"]},
            ),
            (cmd_machine, "VMProvisioner", lambda conn: self.provisioner),
        ]:
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def connection(self, domains=()):
        conn = make_connection(domains)
        self.provisioner.conn = conn  # a plan reads the host through the provisioner's own
        conn.listNetworks.return_value = ["default"]
        conn.listDefinedNetworks.return_value = []
        return conn

    def run_install(self, argument, conn=None):
        return run_command(f"installvm {argument}".strip(), conn or self.connection())

    def answer_file(self, document):
        """A temporary answer file holding `document`, as `--answer` reads it."""
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(document, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def plan(self):
        """The plan `--resolve` reports for `install-me`."""
        code, document = self.run_install("install-me --resolve")
        self.assertEqual(0, code, document)
        return document["plan"]


class TestResolvedPlan(InstallTestCase):
    def test_resolve_reports_a_plan_and_installs_nothing(self):
        plan = self.plan()

        self.assertEqual("install-me", plan["name"])
        self.assertEqual(self.iso_path, plan["iso"])
        self.assertEqual("OPENSUSE", plan["os"])  # recognised in the image's name
        self.assertEqual("default", plan["pool"])  # the only active pool
        self.assertEqual("default", plan["network"])
        self.assertEqual(4096, plan["memory_mb"])
        self.assertEqual(2, plan["vcpu"])
        self.assertEqual(32 * 1024**3, plan["disk_size_bytes"])
        self.assertIsNone(plan["firmware"])  # no UEFI loader on this host
        self.assertEqual([], plan["unresolved"])
        self.assertFalse(plan["exists"])
        self.provisioner.provision_vm.assert_not_called()

    def test_a_plan_holds_every_key_it_declares(self):
        self.assertEqual(set(install_plan.PLAN_KEYS), set(self.plan()))

    def test_nothing_to_install_from_is_a_question_naming_the_cache(self):
        self.cached_isos = []

        code, document = self.run_install("install-me")

        self.assertEqual(1, code)
        self.assertEqual("needs_answer", document["error"]["code"])
        self.assertIn("iso", document["error"]["message"])

    def test_an_answer_the_host_has_not_gives_the_choices(self):
        code, document = self.run_install("install-me --iso tinycore.iso")

        self.assertEqual(1, code)
        self.assertEqual("needs_answer", document["error"]["code"])
        self.assertIn("tinycore.iso", document["error"]["message"])
        self.assertIn(ISO_NAME, document["error"]["message"])

    def test_an_existing_domain_is_not_installed_over(self):
        code, document = self.run_install(
            "install-me", self.connection([make_domain("install-me")])
        )

        self.assertEqual(1, code)
        self.assertEqual("domain_exists", document["error"]["code"])

    def test_a_missing_tool_says_which_one(self):
        self.provisioner.check_install_tools.return_value = ["virt-install"]

        code, document = self.run_install("install-me")

        self.assertEqual(1, code)
        self.assertEqual("install_failed", document["error"]["code"])
        self.assertIn("virt-install", document["error"]["message"])


class TestAnswers(InstallTestCase):
    def test_flags_answer_the_plan(self):
        code, document = self.run_install(
            f"install-me --iso {ISO_NAME} --pool default --network default "
            "--memory 8192 --cpus 4 --disk 64G"
        )

        self.assertEqual(0, code, document)
        self.assertEqual("install-me", document["name"])
        self.assertEqual("uuid-of-install-me", document["uuid"])
        settings = self.provisioner.provision_vm.call_args.kwargs
        self.assertEqual(self.iso_path, settings["iso_url"])
        self.assertEqual("default", settings["storage_pool_name"])
        self.assertEqual("default", settings["network_name"])
        self.assertEqual(8192, settings["memory_mb"])
        self.assertEqual(4, settings["vcpu"])

    def test_a_disk_answer_of_bytes_is_provisioned_in_whole_gib(self):
        self.run_install("install-me --disk 17179869185")  # 16 GiB + 1 byte

        self.assertEqual(17, self.provisioner.provision_vm.call_args.kwargs["disk_size_gb"])

    def test_an_answer_file_of_plan_keys_answers_the_plan(self):
        self.cached_isos = []
        path = self.answer_file({"iso": self.iso_path, "os": "OPENSUSE"})

        code, document = self.run_install(f"install-me --answer {path}")

        self.assertEqual(0, code, document)
        self.assertEqual(self.iso_path, self.provisioner.provision_vm.call_args.kwargs["iso_url"])

    def test_a_resolved_plan_answers_itself(self):
        """What `--resolve` prints is what `--answer` reads, so a plan round-trips."""
        path = self.answer_file({"plan": self.plan()})

        code, document = self.run_install(f"install-me --answer {path}")

        self.assertEqual(0, code, document)

    def test_a_key_that_is_not_part_of_a_plan_is_reported(self):
        path = self.answer_file({"colour": "blue"})

        code, document = self.run_install(f"install-me --answer {path}")

        self.assertEqual(1, code)
        self.assertEqual("bad_answer", document["error"]["code"])
        self.assertIn("colour", document["error"]["message"])

    def test_an_answer_file_that_is_not_there_is_reported(self):
        code, document = self.run_install("install-me --answer /nonexistent/answers.json")

        self.assertEqual(1, code)
        self.assertEqual("no_file", document["error"]["code"])

    def test_an_automation_answer_reaches_the_installer(self):
        """An unattended install is the caller's answer file, held as a plan key.

        `automation_config` is what `provision_vm` renders an installer profile from,
        so the machine interface hands it on as it is given: a template named by the
        caller and the values that fill it in.
        """
        config = {"template_name": "autoyast-basic.xml", "root_password": "hunter2"}

        self.run_install(f"install-me --answer {self.answer_file({'automation': config})}")

        self.assertEqual(
            config, self.provisioner.provision_vm.call_args.kwargs["automation_config"]
        )

    def test_a_size_that_is_not_one_is_refused(self):
        for argument in ("--memory lots", "--cpus 0", "--disk -5"):
            with self.subTest(argument=argument):
                code, document = self.run_install(f"install-me {argument}")

                self.assertEqual(1, code)
                self.assertEqual("bad_argument", document["error"]["code"])

    def test_a_name_is_taken_the_way_the_wizard_takes_one(self):
        code, document = self.run_install("install/me! --resolve")

        self.assertEqual(0, code, document)
        self.assertEqual("install-me", self.plan()["name"])

    def test_a_name_with_no_characters_in_it_is_refused(self):
        code, document = self.run_install("***")

        self.assertEqual(1, code)
        self.assertEqual("bad_argument", document["error"]["code"])


class TestWaiting(InstallTestCase):
    def test_waiting_returns_the_address_when_the_guest_reports_one(self):
        address = {"52:54:00:12:34:56": {"type": 1, "addr": "192.168.122.44"}}
        domain = make_domain("install-me", addresses=address)
        self.provisioner.provision_vm.return_value = domain

        code, document = self.run_install("install-me --wait")

        self.assertEqual(0, code, document)
        self.assertEqual("192.168.122.44", document["ipv4"])

    def test_no_address_by_the_deadline_is_still_a_successful_install(self):
        with (
            patch.object(cmd_machine.time, "monotonic", side_effect=[0.0, 5.0]),
            patch.object(cmd_machine.time, "sleep") as sleep,
        ):
            code, document = self.run_install("install-me --wait --timeout 1")

        self.assertEqual(0, code, document)
        self.assertIsNone(document["ipv4"])
        sleep.assert_not_called()

    def test_a_domain_that_stops_existing_while_waiting_is_not_a_finished_install(self):
        domain = make_domain("install-me")
        gone = libvirt.libvirtError("Domain 42 not found")
        gone.get_error_code = lambda: libvirt.VIR_ERR_NO_DOMAIN
        domain.ID.side_effect = gone
        self.provisioner.provision_vm.return_value = domain

        code, document = self.run_install("install-me --wait --timeout 1")

        self.assertEqual(1, code)
        self.assertEqual("wait_failed", document["error"]["code"])

    def test_a_timeout_that_is_not_seconds_is_refused(self):
        code, document = self.run_install("install-me --wait --timeout soon")

        self.assertEqual(1, code)
        self.assertEqual("bad_argument", document["error"]["code"])

    def test_without_waiting_nothing_is_waited_for(self):
        with patch.object(cmd_machine, "_wait_for_address") as waited:
            code, document = self.run_install("install-me")

        self.assertEqual(0, code, document)
        waited.assert_not_called()
        self.assertIsNone(document["ipv4"])


class TestPlanHelpers(unittest.TestCase):
    """The two rules an answer file is read through, without a host to ask."""

    def test_pick_takes_the_single_choice_and_answers_are_case_insensitive(self):
        self.assertEqual("a", install_plan.pick("key", ["a"]))
        self.assertEqual("a", install_plan.pick("key", ["a", "b"], wanted="A"))

    def test_pick_hands_back_the_choices_it_cannot_settle(self):
        with self.assertRaises(install_plan.NeedsAnswer) as question:
            install_plan.pick("pool", ["a", "b"])

        self.assertEqual(["a", "b"], question.exception.choices)

    def test_an_answer_file_may_be_a_map_the_plan_keys_of_it(self):
        answers = install_plan.apply_answers({"iso": "/tmp/x.iso", "memory_mb": 2048})

        self.assertEqual({"iso": "/tmp/x.iso", "memory_mb": 2048}, answers)


if __name__ == "__main__":
    unittest.main()
