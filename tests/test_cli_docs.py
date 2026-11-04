"""The manual must describe the command surface `vmc` actually has.

A caller reads `documentation/manual/cli.md` to write a script against `vmc`, so
a command documented that the tool does not implement, or a flag documented for a
command that takes its subject as a positional, is a script that fails for a
reason the manual denies. Both directions are checked: what is documented exists,
and what is registered is documented.

The registry side is read from the code rather than restated here: the manual is
compared with `MACHINE_COMMANDS`, `OPTIONS` and `SUBJECT_WRITTEN_AFTER`, so this
fails when either side moves alone.
"""

import re
import unittest
from pathlib import Path

from vmanager.machine_mode import MACHINE_COMMANDS
from vmanager.machine_resources import OPTIONS, SUBJECT_WRITTEN_AFTER

REPO = Path(__file__).resolve().parents[1]
CLI_MANUAL = REPO / "documentation" / "manual" / "cli.md"
VMANAGER_CMD = REPO / "src" / "vmanager" / "vmanager_cmd.py"

# The one machine command not in OPTIONS: it answers what this vmc is without a
# connection, so it has no flags to validate and no subject to act on.
COMMAND_WITHOUT_OPTIONS = {"machine info"}


def _cells(line):
    """The cells of a markdown row, leaving an escaped `\\|` inside a cell alone."""
    parts = re.split(r"(?<!\\)\|", line.strip())
    return [part for part in parts[1:-1]]


def _machine_command_table():
    """The documented resource commands, as `{form: (flags, needs_name)}`.

    A row's first cell holds one or two backticked forms (`pool list` /
    `pool info <name>`), a form is a command, its subcommand — which may be
    several written once (`domain start|shutdown|destroy|reboot`) — then its
    arguments. A flag in square brackets is optional; a name in angle brackets is
    the subject written after the command.
    """
    text = CLI_MANUAL.read_text()
    section = text.split("### Resource commands")[1].split("### Choosing the hypervisor")[0]
    documented = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cell = _cells(line)[0]
        for form in re.findall(r"`([^`]+)`", cell):
            form = form.replace("\\|", "|")
            words = form.split()
            command = words[0]
            subcommands = [None]
            arguments = words[1:]
            if arguments and not arguments[0].startswith(("-", "<", "[")):
                subcommands = arguments[0].split("|")
                arguments = arguments[1:]
            flags = set()
            needs_name = False
            for argument in arguments:
                token = argument.strip("[]<>")
                if token.startswith("--"):
                    flags.add(token[2:])
                elif argument.startswith("<"):
                    # The subject written after the command, as `domain state <name>`.
                    needs_name = True
            for subcommand in subcommands:
                path = command if subcommand is None else f"{command} {subcommand}"
                documented[path] = (flags, needs_name)
    return documented


def _interactive_commands():
    """The commands named in the manual's Command Reference Summary."""
    text = CLI_MANUAL.read_text()
    summary = text.split("## Command Reference Summary")[1]
    names = set()
    for row in summary.splitlines():
        if not row.startswith("|"):
            continue
        parts = [part.strip() for part in re.split(r"(?<!\\)\|", row.strip())[1:-1]]
        if not parts:
            continue
        category = parts[0]
        commands = "|".join(parts[1:])
        if category in ("Category", "", "---", "----------"):
            continue
        # A row's commands cell may hold a backticked pipe — `pipeline` is described
        # with one — so the cell is everything after the category, not one field.
        names.update(
            token
            for token in re.findall(r"`([^`]+)`", commands)
            if token not in ("|",) and not token.startswith("!")
        )
    return names


def _implemented_commands():
    """Every `do_*` command the interactive CLI defines."""
    return set(re.findall(r"^    def do_([a-z_]+)\(", VMANAGER_CMD.read_text(), re.M))


class ResourceCommandTable(unittest.TestCase):
    def test_every_documented_command_exists(self):
        for path in _machine_command_table():
            with self.subTest(command=path):
                self.assertIn(
                    path.split()[0],
                    MACHINE_COMMANDS,
                    f"'{path}' is documented but vmc registers no '{path.split()[0]}' command.",
                )

    def test_every_documented_subcommand_is_registered(self):
        for path in _machine_command_table():
            if path in COMMAND_WITHOUT_OPTIONS:
                continue
            with self.subTest(command=path):
                self.assertIn(
                    path,
                    OPTIONS,
                    f"'{path}' is documented but is not in the vocabulary of subcommands "
                    "the resource commands dispatch on.",
                )

    def test_every_registered_subcommand_is_documented(self):
        for path in set(OPTIONS) | COMMAND_WITHOUT_OPTIONS:
            with self.subTest(command=path):
                self.assertIn(path, _machine_command_table())

    def test_documented_flags_match_the_code(self):
        for path, (flags, _needs_name) in _machine_command_table().items():
            if path in COMMAND_WITHOUT_OPTIONS:
                continue
            with self.subTest(command=path):
                self.assertEqual(
                    flags,
                    set(OPTIONS[path]),
                    f"the flags documented for '{path}' are not the flags the code reads.",
                )

    def test_documented_subjects_match_the_code(self):
        for path, (_flags, needs_name) in _machine_command_table().items():
            with self.subTest(command=path):
                self.assertEqual(
                    needs_name,
                    path in SUBJECT_WRITTEN_AFTER,
                    f"'{path}' is documented {'with a name written after it' if needs_name else 'with no name'}, "
                    "which is not how the code takes it.",
                )


class InteractiveCommandSummary(unittest.TestCase):
    def test_every_summary_command_exists(self):
        implemented = _implemented_commands()
        for name in _interactive_commands():
            with self.subTest(command=name):
                self.assertIn(name, implemented)

    def test_every_command_is_in_the_summary(self):
        documented = _interactive_commands()
        for name in _implemented_commands():
            with self.subTest(command=name):
                self.assertIn(name, documented)


if __name__ == "__main__":
    unittest.main()
