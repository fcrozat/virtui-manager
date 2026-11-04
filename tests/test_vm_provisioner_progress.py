"""Tests for the numbers a deployment reports as it runs.

A percent is for a progress bar, so a caller is entitled to two things: the bar
never rewinds, and one event carries one number. Both are the job of the reporter
both deploy routes report through, and both were broken — the install route
looked the ISO up twice, at 55 and again at 55 after having reported 82, and
uploaded files with a percent of their own in the message.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from unittest.mock import MagicMock

from vmanager.vm_provisioner import VMProvisioner, progress_reporter


class PercentTest(unittest.TestCase):
    def events(self, *reports, watched=True):
        seen = []
        report = progress_reporter(
            (lambda message, percent: seen.append((message, percent))) if watched else None
        )
        for message, percent in reports:
            report(message, percent)
        return seen

    def percents(self, *reports, **kwargs):
        return [percent for _message, percent in self.events(*reports, **kwargs)]

    def test_a_stage_reports_the_percent_it_was_given(self):
        self.assertEqual(
            self.percents(("defining", 40), ("starting", 60), ("done", 100)), [40, 60, 100]
        )

    def test_a_percent_never_goes_backwards(self):
        """The transcript that started this: 82, then 55, then 81."""
        self.assertEqual(
            self.percents(("automation", 82), ("iso lookup", 55), ("kernel", 81)),
            [82, 82, 82],
        )

    def test_a_lookup_that_changed_nothing_reports_nothing(self):
        """The second finding of an ISO that was there all along is not a stage."""
        self.assertEqual(
            [
                message
                for message, _percent in self.events(
                    ("Using existing ISO volume", 55),
                    ("automation", 82),
                    ("Using existing ISO volume", 55),
                )
            ],
            ["Using existing ISO volume", "automation"],
        )

    def test_a_stage_that_has_gone_further_is_reported_again(self):
        """A transfer repeated with more bytes done is progress, not a rewind."""
        self.assertEqual(
            self.percents(("uploading", 55), ("uploading", 65), ("uploading", 75)), [55, 65, 75]
        )

    def test_a_run_nobody_watches_is_still_written_down(self):
        report = progress_reporter(None)
        with self.assertLogs(level="INFO") as logs:
            report("Defining the machine", 40)
        self.assertIn("40%", "\n".join(logs.output))


class DeployProgressTest(unittest.TestCase):
    """A real route run, rather than the rules about it."""

    def setUp(self):
        conn = MagicMock()
        conn.getCapabilities.return_value = (
            "<capabilities><guest><arch name='x86_64'>"
            "<machine>pc-q35-8.2</machine>"
            "</arch></guest></capabilities>"
        )
        self.provisioner = VMProvisioner(conn)
        self.events = []

    def test_the_percents_of_a_boot_are_not_in_the_past(self):
        self.provisioner.provision_existing_volume(
            vm_name="sidekick-worker01",
            disk_path="/var/lib/libvirt/images/sidekick-worker01.qcow2",
            disk_format="qcow2",
            memory_mb=2048,
            vcpu_count=2,
            network_name="default",
            progress_callback=lambda message, percent: self.events.append((message, percent)),
        )
        percents = [percent for _message, percent in self.events]
        self.assertTrue(percents, "a deploy that reports nothing reports nothing monotonic either")
        self.assertEqual(
            percents,
            sorted(percents),
            f"a progress bar that rewinds: {[(m, p) for m, p in self.events]}",
        )


if __name__ == "__main__":
    unittest.main()
