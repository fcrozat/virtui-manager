"""Tests for booting a machine from a volume it already has.

`VMProvisioner.provision_existing_volume` defines a domain over a disk the
storage pool holds, with no installer involved. What matters here is the XML
libvirt is handed: the disk, the seed, the boot order, and the metadata the
caller said the machine is for.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vmanager.vm_provisioner import METADATA_NS, VMProvisioner, VMType


def provisioner():
    conn = MagicMock()
    conn.getCapabilities.return_value = (
        "<capabilities><guest><arch name='x86_64'>"
        "<machine>pc-q35-8.2</machine><machine>pc-i440fx-2.12</machine>"
        "</arch></guest></capabilities>"
    )
    return VMProvisioner(conn), conn


class InstallXmlTest(unittest.TestCase):
    """The other half of the deploy file: installing from media.

    `generate_xml` is what that route hands libvirt, so it is where a fact the
    caller supplied either reaches the domain or does not.
    """

    def setUp(self):
        self.provisioner, _ = provisioner()
        self.provisioner.host_arch = "x86_64"

    def install(self, **kwargs):
        args = dict(
            vm_name="sidekick-worker01",
            vm_type=VMType.SERVER,
            disk_path="/var/lib/libvirt/images/sidekick-worker01.qcow2",
            iso_path="/var/lib/libvirt/images/SLE-15.iso",
        )
        args.update(kwargs)
        return ET.fromstring(self.provisioner.generate_xml(**args))

    def test_the_metadata_of_the_caller_is_kept_in_the_machine(self):
        xml = self.install(metadata={"owner": "mcp-sidekick", "instance": "worker01"})
        properties = xml.find(f"metadata/{{{METADATA_NS}}}properties")
        self.assertEqual(properties.find(f"{{{METADATA_NS}}}owner").text, "mcp-sidekick")
        self.assertEqual(properties.find(f"{{{METADATA_NS}}}instance").text, "worker01")

    def test_an_install_with_nothing_to_keep_has_no_empty_element(self):
        xml = self.install()
        self.assertIsNone(xml.find(f"metadata/{{{METADATA_NS}}}properties"))


class VolumeBootXmlTest(unittest.TestCase):
    def setUp(self):
        self.provisioner, self.conn = provisioner()
        self.defined = []
        self.conn.defineXML.side_effect = lambda xml: self.defined.append(xml) or MagicMock()

    def boot(self, **kwargs):
        args = dict(
            vm_name="sidekick-worker01",
            disk_path="/var/lib/libvirt/images/sidekick-worker01.qcow2",
            disk_format="qcow2",
            memory_mb=2048,
            vcpu_count=2,
            network_name="default",
        )
        args.update(kwargs)
        domain = self.provisioner.provision_existing_volume(**args)
        self.assertIsNotNone(domain)
        return ET.fromstring(self.defined[-1])

    def test_the_disk_it_was_given_is_what_boots(self):
        xml = self.boot()
        disk = xml.find("devices/disk")
        self.assertEqual(disk.find("source").get("file"), self.DISK)
        self.assertEqual(disk.find("driver").get("type"), "qcow2")
        self.assertEqual(disk.find("target").get("bus"), "virtio")
        self.assertEqual(xml.find("os/boot").get("dev"), "hd")

    DISK = "/var/lib/libvirt/images/sidekick-worker01.qcow2"

    def test_a_seed_is_a_second_read_only_cdrom(self):
        xml = self.boot(seed_path="/var/lib/libvirt/images/seed.iso")
        cdroms = [d for d in xml.find("devices") if d.get("device") == "cdrom"]
        self.assertEqual(len(cdroms), 1)
        self.assertEqual(cdroms[0].find("source").get("file"), "/var/lib/libvirt/images/seed.iso")
        self.assertIsNotNone(cdroms[0].find("readonly"))

    def test_no_seed_means_no_cdrom(self):
        xml = self.boot()
        self.assertFalse([d for d in xml.find("devices") if d.get("device") == "cdrom"])

    def test_booting_from_the_seed_puts_it_first(self):
        xml = self.boot(seed_path="/x/seed.iso", boot_device="cdrom")
        disk, cdrom = [d for d in xml.find("devices") if d.get("device") in ("disk", "cdrom")]
        self.assertEqual(cdrom.find("boot").get("order"), "1")
        self.assertEqual(disk.find("boot").get("order"), "2")
        self.assertEqual(xml.find("os/boot").get("dev"), "cdrom")

    def test_the_memory_and_cpus_the_caller_asked_for(self):
        xml = self.boot(memory_mb=4096, vcpu_count=4)
        self.assertEqual(xml.find("memory").text, "4096")
        self.assertEqual(xml.find("vcpu").text, "4")

    def test_there_is_no_screen_unless_one_is_asked_for(self):
        self.assertIsNone(self.boot().find("devices/graphics"))
        self.assertIsNotNone(self.boot(graphics="vnc").find("devices/graphics"))

    def test_uefi_is_the_firmware_the_caller_named(self):
        with patch.object(
            self.provisioner,
            "_setup_uefi_nvram",
            return_value=("/usr/share/OVMF/code.fd", "/pool/nvram.fd"),
        ):
            xml = self.boot(boot_uefi=True)
        self.assertEqual(xml.find("os/loader").text, "/usr/share/OVMF/code.fd")
        self.assertEqual(xml.find("os/nvram").text, "/pool/nvram.fd")

    def test_the_metadata_of_the_caller_is_kept_in_the_machine(self):
        xml = self.boot(metadata={"owner": "mcp-sidekick", "instance": "worker01"})
        # The namespace on `properties` covers its children, as any namespace
        # declared on an element does: a reader repeats it for each one.
        properties = xml.find(f"metadata/{{{METADATA_NS}}}properties")
        self.assertEqual(properties.find(f"{{{METADATA_NS}}}owner").text, "mcp-sidekick")
        self.assertEqual(properties.find(f"{{{METADATA_NS}}}instance").text, "worker01")

    def test_a_value_cannot_close_the_element_it_is_in(self):
        xml = self.boot(metadata={"name": "</properties><evil/>"})
        self.assertEqual(len(xml.find(f"metadata/{{{METADATA_NS}}}properties")), 1)

    def test_the_machine_starts_unless_the_caller_says_otherwise(self):
        domain = MagicMock()
        domain.isActive.return_value = 0
        self.conn.defineXML.side_effect = lambda _xml: self.defined.append(_xml) or domain
        self.provisioner.provision_existing_volume(
            vm_name="v", disk_path="/p/v.qcow2", memory_mb=1024, vcpu_count=1, start=False
        )
        domain.create.assert_not_called()

    def test_progress_arrives_as_a_message_and_a_percentage(self):
        """Both provisioners report progress the same way: (message, percent).

        The contract `provision_vm` and the machine envelope share; a boot that
        reports (percent, message) instead puts numbers in the stage field.
        """
        events = []
        self.boot(progress_callback=lambda *args: events.append(args))
        self.assertIn(("Defining the machine", 40), events)
        for message, percent in events:
            self.assertIsInstance(message, str)
            self.assertIsInstance(percent, int)

    def test_a_domain_libvirt_refuses_is_an_error_the_caller_reads(self):
        self.conn.defineXML.side_effect = RuntimeError("refused")
        with self.assertRaises(RuntimeError):
            self.boot()


if __name__ == "__main__":
    unittest.main()
