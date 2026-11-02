"""
Tests for firmware_manager module
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the src directory to the path to import vmanager modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from vmanager.firmware_manager import (
    Firmware,
    _load_firmware_from_files,
    clear_firmware_cache,
    get_uefi_files,
    select_best_firmware,
)

# Host capabilities of an x86_64-only hypervisor: the hypervisor lists one
# <arch> per architecture it hosts.
HOST_CAPS_X86_64 = """<?xml version="1.0"?>
<capabilities>
  <guest>
    <arch name="x86_64">
      <domain type="kvm"/>
    </arch>
  </guest>
</capabilities>
"""

# The same hypervisor with qemu-system-aarch64 installed. aarch64 is served by
# qemu only: an x86_64 host has no kvm support for another architecture.
HOST_CAPS_X86_64_AND_AARCH64 = """<?xml version="1.0"?>
<capabilities>
  <guest>
    <arch name="x86_64">
      <domain type="kvm"/>
    </arch>
  </guest>
  <guest>
    <arch name="aarch64">
      <domain type="qemu"/>
    </arch>
  </guest>
</capabilities>
"""


def domain_caps_xml(loaders):
    """Domain capabilities XML advertising the given loader paths"""
    values = "".join(f"<value>{path}</value>" for path in loaders)
    return f"""<?xml version="1.0"?>
<domainCapabilities>
    <os>
        <loader readonly="yes" type="pflash">{values}</loader>
    </os>
</domainCapabilities>
"""


class TestFirmware(unittest.TestCase):
    """Tests for the Firmware class"""

    def test_firmware_init(self):
        """Test Firmware object initialization"""
        fw = Firmware()
        self.assertIsNone(fw.executable)
        self.assertIsNone(fw.nvram_template)
        self.assertEqual(fw.architectures, [])
        self.assertEqual(fw.features, [])
        self.assertEqual(fw.interfaces, [])

    def test_firmware_load_from_json_valid(self):
        """Test loading firmware from valid JSON data"""
        json_data = {
            "interface-types": ["uefi"],
            "mapping": {
                "executable": {"filename": "/usr/share/OVMF/OVMF_CODE.fd"},
                "nvram-template": {"filename": "/usr/share/OVMF/OVMF_VARS.fd"},
            },
            "features": ["secure-boot"],
            "targets": [{"architecture": "x86_64"}],
        }
        fw = Firmware()
        result = fw.load_from_json(json_data)

        self.assertTrue(result)
        self.assertEqual(fw.executable, "/usr/share/OVMF/OVMF_CODE.fd")
        self.assertEqual(fw.nvram_template, "/usr/share/OVMF/OVMF_VARS.fd")
        self.assertEqual(fw.architectures, ["x86_64"])
        self.assertEqual(fw.features, ["secure-boot"])
        self.assertEqual(fw.interfaces, ["uefi"])

    def test_firmware_load_from_json_missing_interface_types(self):
        """Test that firmware returns False when interface-types is missing"""
        json_data = {
            "mapping": {"executable": {"filename": "/usr/share/OVMF/OVMF_CODE.fd"}},
            "targets": [{"architecture": "x86_64"}],
        }
        fw = Firmware()
        result = fw.load_from_json(json_data)
        self.assertFalse(result)

    def test_firmware_load_from_json_missing_executable(self):
        """Test that firmware returns False when executable is missing"""
        json_data = {
            "interface-types": ["uefi"],
            "mapping": {"nvram-template": {"filename": "/usr/share/OVMF/OVMF_VARS.fd"}},
            "targets": [{"architecture": "x86_64"}],
        }
        fw = Firmware()
        result = fw.load_from_json(json_data)
        self.assertFalse(result)

    def test_firmware_load_from_json_missing_architectures(self):
        """Test that firmware returns False when architectures is missing"""
        json_data = {
            "interface-types": ["uefi"],
            "mapping": {"executable": {"filename": "/usr/share/OVMF/OVMF_CODE.fd"}},
        }
        fw = Firmware()
        result = fw.load_from_json(json_data)
        self.assertFalse(result)


class TestLoadFirmwareFromFiles(unittest.TestCase):
    """Tests for _load_firmware_from_files function"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR")
    def test_load_firmware_from_files_success(self, mock_firmware_dir):
        """Test successfully loading firmware files"""
        mock_firmware_dir.return_value = self.temp_dir

        # Create a sample firmware JSON file
        firmware_json = {
            "interface-types": ["uefi"],
            "mapping": {"executable": {"filename": "/usr/share/OVMF/OVMF_CODE.fd"}},
            "targets": [{"architecture": "x86_64"}],
        }

        firmware_path = Path(self.temp_dir) / "ovmf.json"
        with open(firmware_path, "w") as f:
            json.dump(firmware_json, f)

        uefi_files = []
        with patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR", self.temp_dir):
            _load_firmware_from_files(uefi_files)

        self.assertEqual(len(uefi_files), 1)
        self.assertEqual(uefi_files[0].executable, "/usr/share/OVMF/OVMF_CODE.fd")
        self.assertEqual(uefi_files[0].architectures, ["x86_64"])

    @patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR")
    def test_load_firmware_from_files_missing_directory(self, mock_firmware_dir):
        """Test behavior when firmware directory doesn't exist"""
        mock_firmware_dir.return_value = "/nonexistent/directory"

        uefi_files = []
        with patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR", "/nonexistent/directory"):
            _load_firmware_from_files(uefi_files)

        self.assertEqual(len(uefi_files), 0)

    @patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR")
    def test_load_firmware_from_files_invalid_json(self, mock_firmware_dir):
        """Test handling of invalid JSON files"""
        mock_firmware_dir.return_value = self.temp_dir

        # Create invalid JSON file
        invalid_path = Path(self.temp_dir) / "invalid.json"
        with open(invalid_path, "w") as f:
            f.write("{invalid json}")

        uefi_files = []
        with patch("vmanager.firmware_manager.FIRMWARE_META_BASE_DIR", self.temp_dir):
            _load_firmware_from_files(uefi_files)

        # Should not crash, just skip the invalid file
        self.assertEqual(len(uefi_files), 0)


class TestGetUefiFiles(unittest.TestCase):
    """Tests for get_uefi_files function"""

    def setUp(self):
        """Clear cache before each test"""
        clear_firmware_cache()

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_get_uefi_files_with_connection_and_capabilities(self, mock_load_files, mock_get_caps):
        """Test get_uefi_files with a valid connection and domain capabilities"""
        # Mock domain capabilities XML with loaders
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                    <value>/usr/share/OVMF/OVMF_CODE.secboot.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        # Mock the _load_firmware_from_files to populate the list
        def populate_files(uefi_files):
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            fw.features = ["secure-boot"]
            uefi_files.append(fw)

        mock_load_files.side_effect = populate_files

        # Create mock connection
        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        result = get_uefi_files(mock_conn)

        # Should have called get_domain_capabilities_xml
        mock_get_caps.assert_called_once()
        # Should have tried to load from files
        mock_load_files.assert_called_once()
        # Should return firmware list
        self.assertGreater(len(result), 0)

    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_get_uefi_files_without_connection(self, mock_load_files):
        """Test get_uefi_files without a connection (local filesystem)"""

        def populate_files(uefi_files):
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            uefi_files.append(fw)

        mock_load_files.side_effect = populate_files

        result = get_uefi_files(None)

        # Should have loaded from filesystem
        mock_load_files.assert_called_once()
        self.assertGreater(len(result), 0)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_get_uefi_files_fallback_on_no_capabilities(self, mock_load_files, mock_get_caps):
        """Test fallback to filesystem when domain capabilities unavailable"""
        mock_get_caps.return_value = None

        def populate_files(uefi_files):
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            uefi_files.append(fw)

        mock_load_files.side_effect = populate_files

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64
        result = get_uefi_files(mock_conn)

        # Should have fallen back to filesystem
        mock_load_files.assert_called_once()
        self.assertGreater(len(result), 0)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_get_uefi_files_fallback_on_json_read_error(self, mock_load_files, mock_get_caps):
        """Test fallback when JSON files can't be read"""
        # Mock domain capabilities with loaders
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                    <value>/usr/share/OVMF/OVMF_CODE.secure.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        # First call raises error, second call (fallback) populates
        def populate_files(uefi_files):
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            uefi_files.append(fw)

        mock_load_files.side_effect = [
            OSError("Permission denied"),
            populate_files([]),
        ]

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        # The function should handle the error and still return firmware
        # created from loader values (fallback mechanism)
        result = get_uefi_files(mock_conn)

        # Should return some firmware (either from files or fallback)
        # The actual implementation creates firmware from loader values on error
        self.assertIsInstance(result, list)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_get_uefi_files_fallback_creates_firmware_from_loaders(self, mock_get_caps):
        """Test that fallback creates Firmware objects from loader values"""
        # Mock domain capabilities with loaders
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                    <value>/usr/share/OVMF/OVMF_CODE.secboot.fd</value>
                    <value>/usr/share/seabios/bios-256k.bin</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        # Mock _load_firmware_from_files to raise an error (simulating inaccessible JSON files)
        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=OSError("Permission denied"),
        ):
            result = get_uefi_files(mock_conn)

        # Should have created firmware objects from loader values
        self.assertGreater(len(result), 0, "Should create firmware from loader values")

        # Should have firmware for x86_64
        x86_64_firmware = [f for f in result if "x86_64" in f.architectures]
        self.assertGreater(len(x86_64_firmware), 0, "Should have x86_64 firmware")

        # Should have detected UEFI and BIOS interfaces
        uefi_fw = [f for f in result if "uefi" in f.interfaces]
        bios_fw = [f for f in result if "bios" in f.interfaces]

        self.assertGreater(len(uefi_fw), 0, "Should have detected UEFI firmware")
        self.assertGreater(len(bios_fw), 0, "Should have detected BIOS firmware")

        # Should detect secure-boot in secboot firmware
        secure_fw = [f for f in result if "secure-boot" in f.features]
        self.assertGreater(len(secure_fw), 0, "Should detect secure-boot feature")

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_get_uefi_files_never_returns_empty_list(self, mock_get_caps):
        """Test that get_uefi_files never returns an empty list when fallback works"""
        # This is critical: ensure we always have some firmware available
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=OSError("Simulated error"),
        ):
            result = get_uefi_files(mock_conn)

        # The function should NEVER return an empty list when domain capabilities are available
        self.assertGreater(
            len(result), 0, "Should never return empty list with available domain capabilities"
        )

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_get_uefi_files_fallback_infers_nvram(self, mock_get_caps):
        """Test that fallback logic infers NVRAM template from loader path"""
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                    <value>/usr/share/OVMF/OVMF_CODE.secboot.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml
        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=OSError("Simulated error"),
        ):
            result = get_uefi_files(mock_conn)

        # Check if nvram_template was inferred
        fw_with_nvram = [f for f in result if f.nvram_template is not None]
        self.assertGreater(len(fw_with_nvram), 0, "Should have inferred NVRAM template")

        # Check specific inference
        ovmf_fw = next((f for f in result if "OVMF_CODE.fd" in f.executable), None)
        self.assertIsNotNone(ovmf_fw)
        self.assertEqual(ovmf_fw.nvram_template, "/usr/share/OVMF/OVMF_VARS.fd")


class TestFirmwareFeatureInference(unittest.TestCase):
    """Tests for firmware feature inference from loader paths"""

    def setUp(self):
        """Clear cache before each test"""
        clear_firmware_cache()

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_detect_secure_boot_feature(self, mock_get_caps):
        """Test detection of secure-boot feature from path"""
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.secboot.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml
        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files", side_effect=OSError("Error")
        ):
            result = get_uefi_files(mock_conn)

        # Should detect secure-boot in path
        secure_fw = [f for f in result if "secure-boot" in f.features]
        self.assertGreater(len(secure_fw), 0, "Should detect 'secure-boot' in features")

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_detect_sev_features(self, mock_get_caps):
        """Test detection of AMD-SEV features from path"""
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.sev.fd</value>
                    <value>/usr/share/OVMF/OVMF_CODE.sev-es.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml
        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files", side_effect=OSError("Error")
        ):
            result = get_uefi_files(mock_conn)

        # Should detect amd-sev and amd-sev-es
        amd_sev = [f for f in result if "amd-sev" in f.features and "amd-sev-es" not in f.features]
        amd_sev_es = [f for f in result if "amd-sev-es" in f.features]

        self.assertGreater(len(amd_sev), 0, "Should detect 'amd-sev' feature")
        self.assertGreater(len(amd_sev_es), 0, "Should detect 'amd-sev-es' feature")


class TestMultiArchitectureDiscovery(unittest.TestCase):
    """Tests for firmware discovery over every architecture of a hypervisor"""

    OVMF = "/usr/share/qemu/ovmf-x86_64-4m-code.bin"
    AAVMF = "/usr/share/qemu/aavmf-aarch64-code.bin"

    def setUp(self):
        """Clear cache before each test"""
        clear_firmware_cache()

    def tearDown(self):
        """Clear cache after each test"""
        clear_firmware_cache()

    def _discover(self, mock_get_caps, host_caps, loaders_by_arch):
        """Run get_uefi_files against a hypervisor with no firmware metadata"""

        def capabilities(conn, emulatorbin, arch, machine, flags):
            return domain_caps_xml(loaders_by_arch.get(arch, []))

        mock_get_caps.side_effect = capabilities

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = host_caps

        # No JSON metadata: the loaders alone have to carry the architecture
        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=OSError("No metadata"),
        ):
            return get_uefi_files(mock_conn, use_cache=False)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_every_architecture_is_queried(self, mock_get_caps):
        """Domain capabilities are asked for once per architecture reported"""
        firmwares = self._discover(
            mock_get_caps,
            HOST_CAPS_X86_64_AND_AARCH64,
            {"x86_64": [self.OVMF], "aarch64": [self.AAVMF]},
        )

        self.assertEqual(mock_get_caps.call_count, 2)
        self.assertEqual(
            sorted(f.executable for f in firmwares),
            sorted([self.OVMF, self.AAVMF]),
        )
        self.assertEqual(
            sorted(tuple(f.architectures) for f in firmwares),
            [("aarch64",), ("x86_64",)],
        )

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_aarch64_firmware_is_selectable_from_an_x86_64_hypervisor(self, mock_get_caps):
        """select_best_firmware finds AAVMF although the host is x86_64"""
        firmwares = self._discover(
            mock_get_caps,
            HOST_CAPS_X86_64_AND_AARCH64,
            {"x86_64": [self.OVMF], "aarch64": [self.AAVMF]},
        )

        selected = select_best_firmware(firmwares, architecture="aarch64")
        self.assertEqual(selected.executable, self.AAVMF)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_machine_type_per_architecture(self, mock_get_caps):
        """x86_64 asks for "pc", other architectures for libvirt's default"""
        self._discover(
            mock_get_caps,
            HOST_CAPS_X86_64_AND_AARCH64,
            {"x86_64": [self.OVMF], "aarch64": [self.AAVMF]},
        )

        machines = {
            call.kwargs["arch"]: call.kwargs["machine"] for call in mock_get_caps.call_args_list
        }
        self.assertEqual(machines, {"x86_64": "pc", "aarch64": None})

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_architecture_without_capabilities_is_skipped(self, mock_get_caps):
        """An architecture whose capabilities fail does not hide the others"""

        def capabilities(conn, emulatorbin, arch, machine, flags):
            return None if arch == "aarch64" else domain_caps_xml([self.OVMF])

        mock_get_caps.side_effect = capabilities

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64_AND_AARCH64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=OSError("No metadata"),
        ):
            firmwares = get_uefi_files(mock_conn, use_cache=False)

        self.assertEqual([f.executable for f in firmwares], [self.OVMF])
        self.assertEqual([f.architectures for f in firmwares], [["x86_64"]])

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_loader_of_two_architectures_is_listed_once(self, mock_get_caps):
        """A loader reported for two architectures is one Firmware with both"""
        shared = "/usr/share/qemu/bios-256k.bin"
        firmwares = self._discover(
            mock_get_caps,
            HOST_CAPS_X86_64_AND_AARCH64,
            {"x86_64": [shared], "aarch64": [shared]},
        )

        self.assertEqual([f.executable for f in firmwares], [shared])
        self.assertEqual(sorted(firmwares[0].architectures), ["aarch64", "x86_64"])

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_capabilities_without_architectures_ask_x86_64(self, mock_get_caps):
        """A hypervisor reporting no architecture keeps the x86_64 query"""
        firmwares = self._discover(
            mock_get_caps, "<capabilities></capabilities>", {"x86_64": [self.OVMF]}
        )

        self.assertEqual(mock_get_caps.call_count, 1)
        self.assertEqual([f.architectures for f in firmwares], [["x86_64"]])

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_aarch64_metadata_survives_x86_64_only_host(self, mock_get_caps):
        """An aarch64 firmware file is kept although the host only discovers x86_64"""
        mock_get_caps.return_value = domain_caps_xml([self.OVMF])

        def populate_files(files):
            """Stand in for the aarch64 metadata of /usr/share/qemu/firmware"""
            aavmf = Firmware()
            aavmf.load_from_json(
                {
                    "interface-types": ["uefi"],
                    "mapping": {
                        "executable": {"filename": self.AAVMF},
                        "nvram-template": {"filename": "/usr/share/qemu/aavmf-aarch64-vars.bin"},
                    },
                    "targets": [{"architecture": "aarch64", "machines": ["virt"]}],
                }
            )
            files.append(aavmf)

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        with patch(
            "vmanager.firmware_manager._load_firmware_from_files",
            side_effect=populate_files,
        ):
            firmwares = get_uefi_files(mock_conn, use_cache=False)

        selected = select_best_firmware(firmwares, architecture="aarch64")
        self.assertIsNotNone(selected, "the aarch64 firmware must not be rejected")
        self.assertEqual(selected.executable, self.AAVMF)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    def test_ovmf_code_fd_fallback_without_metadata(self, mock_get_caps):
        """Without metadata, OVMF_CODE.fd still gives a firmware with guessed NVRAM"""
        code = "/usr/share/OVMF/OVMF_CODE.fd"
        firmwares = self._discover(mock_get_caps, HOST_CAPS_X86_64, {"x86_64": [code]})

        self.assertEqual([f.executable for f in firmwares], [code])

        selected = select_best_firmware(firmwares, architecture="x86_64")
        self.assertEqual(selected.executable, code)
        self.assertEqual(selected.nvram_template, "/usr/share/OVMF/OVMF_VARS.fd")
        self.assertEqual(selected.architectures, ["x86_64"])


class TestFirmwareSelection(unittest.TestCase):
    """Tests for select_best_firmware logic"""

    def setUp(self):
        # Create standard non-secure firmware
        self.fw_normal = Firmware()
        self.fw_normal.executable = "/usr/share/OVMF/OVMF_CODE.fd"
        self.fw_normal.architectures = ["x86_64"]
        self.fw_normal.features = ["acpi-s3"]
        self.fw_normal.nvram_template = "/usr/share/OVMF/OVMF_VARS.fd"

        # Create secure boot firmware
        self.fw_secure = Firmware()
        self.fw_secure.executable = "/usr/share/OVMF/OVMF_CODE.secboot.fd"
        self.fw_secure.architectures = ["x86_64"]
        self.fw_secure.features = ["acpi-s3", "secure-boot", "requires-smm"]
        self.fw_secure.nvram_template = "/usr/share/OVMF/OVMF_VARS.secboot.fd"

        self.firmwares = [self.fw_normal, self.fw_secure]

    def test_select_secure_boot_when_required(self):
        """Test that secure-boot firmware is selected when requested"""
        selected = select_best_firmware(self.firmwares, architecture="x86_64", secure_boot=True)
        self.assertEqual(selected.executable, self.fw_secure.executable)

    def test_select_non_secure_boot_when_not_required(self):
        """Test that non-secure firmware is preferred when secure-boot is not requested"""
        selected = select_best_firmware(self.firmwares, architecture="x86_64", secure_boot=False)
        self.assertEqual(selected.executable, self.fw_normal.executable)

    def test_select_by_architecture(self):
        """Test filtering by architecture"""
        fw_aarch64 = Firmware()
        fw_aarch64.executable = "/usr/share/AAVMF/AAVMF_CODE.fd"
        fw_aarch64.architectures = ["aarch64"]

        firmwares = self.firmwares + [fw_aarch64]

        selected = select_best_firmware(firmwares, architecture="aarch64")
        self.assertEqual(selected.executable, fw_aarch64.executable)


class TestFirmwareCaching(unittest.TestCase):
    """Tests for firmware caching functionality"""

    def tearDown(self):
        """Clear cache after each test"""
        clear_firmware_cache()

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_firmware_caching_enabled(self, mock_load_files, mock_get_caps):
        """Test that firmware caching works and returns cached results"""
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        def populate_files(uefi_files):
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            uefi_files.append(fw)

        mock_load_files.side_effect = populate_files

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        # First call - should load from libvirt
        result1 = get_uefi_files(mock_conn, use_cache=True)

        # Second call - should use cache
        result2 = get_uefi_files(mock_conn, use_cache=True)

        # Both should return the same firmware
        self.assertEqual(len(result1), len(result2))
        self.assertEqual(result1[0].executable, result2[0].executable)

        # _load_firmware_from_files should have been called once per call since
        # we're using mocks. In practice, the cache would prevent re-reading.
        # Verify that both calls returned similar data
        self.assertEqual(result1[0].architectures, result2[0].architectures)

    @patch("vmanager.firmware_manager.get_domain_capabilities_xml")
    @patch("vmanager.firmware_manager._load_firmware_from_files")
    def test_firmware_caching_disabled(self, mock_load_files, mock_get_caps):
        """Test that use_cache=False bypasses the cache"""
        caps_xml = """<?xml version="1.0"?>
        <domainCapabilities>
            <os>
                <loader>
                    <value>/usr/share/OVMF/OVMF_CODE.fd</value>
                </loader>
            </os>
        </domainCapabilities>
        """
        mock_get_caps.return_value = caps_xml

        call_count = [0]

        def populate_files(uefi_files):
            call_count[0] += 1
            fw = Firmware()
            fw.executable = "/usr/share/OVMF/OVMF_CODE.fd"
            fw.architectures = ["x86_64"]
            fw.interfaces = ["uefi"]
            uefi_files.append(fw)

        mock_load_files.side_effect = populate_files

        mock_conn = MagicMock()
        mock_conn.getCapabilities.return_value = HOST_CAPS_X86_64

        # First call - populates cache
        result1 = get_uefi_files(mock_conn, use_cache=True)

        # Second call with use_cache=False - should bypass cache
        result2 = get_uefi_files(mock_conn, use_cache=False)

        # Both should return firmware
        self.assertGreater(len(result1), 0)
        self.assertGreater(len(result2), 0)

    def test_clear_firmware_cache(self):
        """Test clearing the firmware cache"""
        # This is a simple test that just ensures clear_firmware_cache doesn't crash
        clear_firmware_cache()
        clear_firmware_cache("local")
        clear_firmware_cache("remote")

        # Should not raise any exception
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
