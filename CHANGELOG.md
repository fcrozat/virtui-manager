# Changelog

All notable changes to Virtui Manager are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Machine-readable CLI mode: `vmc --json --server <name> -c "<command>"` prints exactly one JSON
  document on stdout, reports failures as `{"error": {"code", "message"}}`, and is documented in
  the CLI manual. Read-only commands (`capabilities`, `host_info`, `list`, `dominfo`, `pool_info`,
  `net_info`) landed with 4a8e2f2.
- Storage commands in machine mode: `pool_ensure`, `vol_info`, `vol_create` (empty or as a qcow2
  overlay of an existing volume), `vol_upload` and `vol_delete`.
- Domain lifecycle in machine mode: `define` (XML from a file or stdin), `start`, `shutdown`,
  `destroy` and `undefine` (`--nvram`, `--snapshots`, `--save`). `start` and `shutdown` are
  idempotent, and `shutdown` answers before the guest is down, as an ACPI request does.
- Install in machine mode: `installvm <name>` builds a domain from an install image. Since an
  install decides a name, an image, a distribution, a pool, a disk and a network before it can
  start, `installvm --resolve` prints that plan and the keys it could not answer, and the caller
  answers exactly those — with the flags `--iso`, `--os`, `--pool`, `--network`, `--memory`,
  `--cpus`, `--disk`, or with a plan file through `--answer`. `--wait` holds the command until
  the guest has asked the network for an address, which it reports as `ipv4`.

### Fixed
- Firmware discovery asked the hypervisor for x86_64 domain capabilities only, so the
  firmware of any other architecture — AAVMF for aarch64, for one — was never listed and
  could not be selected. It now queries every architecture the hypervisor reports and
  merges the loaders by path, so a loader served for several architectures is listed once with
  them all. The machine type is passed only for x86_64 (`pc`, as before) and left to libvirt for
  the others, whose machine names do not carry over. An architecture whose query fails is
  skipped rather than losing every firmware.

### Changed
- `vmc -c "<command>"` exits with the command's status instead of always 0.

## [3.3.9]

### Changed
- Use plain ACPI shutdown/reboot instead of the QEMU guest agent for reliable power
  actions.

### Fixed
- Refresh the VM list after a rename so the updated name is displayed.
- Remove all video devices (not just the first) and use correct model defaults when
  changing the video model.
- Fix Python 3.9 compatibility (PEP 604 union annotations).
- Pin the CI lint/test jobs to ubuntu-24.04 so the source-built libvirt-python
  10.0.0 matches the libvirt version shipped by that distro.

### Removed
- Cleaned up 4 unused constants.

## [3.3.6]

### Added
- Prefer the QEMU guest agent for graceful shutdown and reboot with automatic
  fallback to plain ACPI.
- Helper to detect whether the QEMU guest agent is available in the guest.

### Changed
- Snapshot quiesce now requires the guest agent to be both configured and running.

### Fixed
- Make libvirt event registration idempotent (no duplicate/leaked callbacks on
  connection re-registration).
- Fix a stale refresh re-adding a removed VM card in the main view.

### Refactored
- Consolidated the base/components/modals CSS rules into a single vmanager.css
  and merged remaining duplicate CSS rules.

## [3.3.5]

### Added
- Pre-flight check of required install binaries (7z, mkfs.vfat, mcopy, qemu-img)
  before starting a VM installation.
- Auto-create the target directory when creating a `dir` storage pool.
- VM card changes prepared alongside the install-tools pre-flight feature.

## [3.3.4]

### Removed
- Removed commented-out code and verified dead code (cleanup batches).
- Bumped release to 3.3.4 and cleaned up lint/format.

## [3.3.3]

### Added
- `vmc` alias for `virtui-manager-cmd` to simplify CLI usage.
- New `-c` non-interactive flag and a connect-pipeline command for scripting.
- Input validation for IP addresses, storage paths, MAC addresses, and input ranges
  (also MAC address, kernel parameters and markdown sanitization on user input).
- Documentation chapters on backup, pipelines, auto-install, UEFI firmware and
  viewer architecture.

### Changed
- Default timezone on new VMs set to UTC.
- Expert Mode and automation sections now collapse on install.
- Install modal keeps the install button visible when the form overflows.
- Notification bar is now color-coded by VM state.
- Clipboard menu labels corrected and auto-push UX improved.

### Fixed
- Remote provisioning: several bugs in `vm_provisioner` and `vmc` alignment with the
  TUI provisioning flow.
- Remote servers: skip local ISO existence checks for remote pool volumes, disable
  Browse buttons in `AddPoolModal`, skip path-existence checks for remote attach volume,
  and return `None` pool volumes when a pool lookup fails.
- Remote VMs: disable USB/PCI host tabs and "Add Disk" in VM details.
- `RemoteAutoHTTPServer`: SSH call quoting with `shlex`, path validation on `stop()`,
  `rm -rf` guard, SSH-call optimization and test updates.
- VM actions: clone NVRAM per VM to avoid SELinux label conflicts, skip CD-ROM disks when
  deleting VM storage, and fix `strip_installation_assets` reading live XML on a running VM.
- Route libvirt errors to logging instead of crashing, and replace bare `except:` with
  `except Exception:` across the codebase.
- Kill tmux sessions when the GUI dies without cleanup.
- Connect to newly added autoconnect servers when reloading the server list.
- Check for an existing ISO in the pool before downloading.

### Refactored
- Centralized backup manager, pipeline and CLI command refactoring.
- Refactored `vm_actions` XML edits into a shared helper.

### Performance
- Optimized `RemoteAutoHTTPServer` SSH calls.

### Security
- Added path validation to prevent dangerous `rm -rf` and to quote remote command args.

### Documentation / CI
- Removed marketing language from docs.
- Fixed CI to install the correct `libgirepository` and cairo dev packages for PyGObject.
