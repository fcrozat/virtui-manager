# 0002 — Machine commands read the hypervisor, not the cache

Date: 2026-11-05
Status: Accepted

## Context

The interactive tool reads virtual machine state through the caches in `vm_service`.
That is not an optimization: a TUI that refreshes a list of machines over libvirt on
every paint is a TUI that freezes, so the cache exists to keep the interface responsive
and is invalidated whenever anything in the app changes a machine.

The `--machine` commands needed the same facts — is this domain running, what volumes
are in that pool — and the same helpers were sitting there.

## Decision

Machine commands ask the hypervisor directly. They never read through the session
caches, and one connection serves a whole run: `vmc -u <uri>` connects once, before the
command runs, and a command never picks a server of its own.

## Why

A scripted caller asks once and wants the hypervisor's `now`. The staleness a cache
introduces is invisible to the caller: the interactive tool can afford it because a
person looking at a slightly old list will look again, whereas a script that reads
`"running": true` and then ssh's into the machine has already acted on it. The cache is
there to stop a freezing UI, which is a cost a single command does not pay; what it
would buy such a caller is a wrong answer and a round trip it did not ask for.

The words are read separately for a related reason. `vm_queries.get_status` answers
`StatusText.RUNNING`, which is localized and rewordable; a program compares `running`,
so the number is turned into a name by `STATE_NAMES` in `machine_resources.py`. What is
read is the same call — `domain.info()` — which is what makes the two surfaces
comparable even though neither asks the other for anything.

## What would make this wrong

- If machine commands start polling — a `wait`, a `watch`, a loop in a script that runs
  every few seconds against one host — then reading the hypervisor every time becomes
  the expensive thing, and a short-lived cache with a stated TTL is the right answer.
  That is a decision about staleness, and it would have to be made out loud rather than
  inherited from the UI.
- If direct reads start costing the hypervisor something (a remote libvirtd behind a
  slow link, a host with hundreds of domains), the same revisit applies.

## Where this shows in the code

`src/vmanager/machine_resources.py`: the comment above `STATE_NAMES`, and `_connection`.
