# 0001 — Machine answers are not localized

Date: 2026-11-05
Status: Accepted

## Context

`vmc` answers in two voices. The interactive tool's words are passed through `_()` and
are translated, which is right: a person reading "Arrêt en cours" is not harmed by it,
and rewording a status some Tuesday costs nothing.

The `--machine` contract was added later, and every failure path it needed already had
a string. Reusing those strings was the obvious move — the wording existed, it was
already reviewed, and it was already the project's voice. The same argument applies to
the strings a `--dryrun` reports and to what a deploy failure answers with.

## Decision

The words a machine caller is answered with are **not** passed through `_()`. They live
in `constants.py` in `MachineModeText`, `MachineResourceText` and `DeployText`, and they
stay in one language in every environment.

## Why

The `--machine` answer is a JSON object a program reads. Its stable part is `code`, and
`message` beside it is written for the human reading the log afterwards. Localizing
`message` would make the answer depend on the caller's environment: the same command,
run twice with a different `LANG`, would produce two different JSON documents. A
contract that varies with the environment is not a contract — and the exit status would
go on saying the same thing either way, so the translation adds no information, only a
second way for the answer to disagree with itself.

Reusing the interactive strings was refused for the same reason in the other direction:
`StatusText.RUNNING` is allowed to be reworded because nothing compares it, and borrowing
it would quietly make a piece of prose load-bearing for scripts.

Shipping both — a translated message and a stable one — was refused because it puts two
fields under an obligation to stay in step for the benefit of a reader who can already
read one of them.

## What would make this wrong

- If a caller starts branching on `message` rather than on `code` — which is how such a
  habit forms, one `grep` at a time — then the wording is a machine field for real and
  owes the versioning discipline that implies. At that point the fields need pinning
  with a contract test, not just a convention.
- If the project decides to translate everything, including the JSON answers, then this
  record is the thing to argue with, and the argument has to include what happens to a
  caller whose `LANG` changes between two runs.

## Where this shows in the code

`src/vmanager/constants.py`, the docstrings of `MachineModeText`, `MachineResourceText`
and `DeployText`. `tests/test_deploy_wording.py` pins a class of these wordings; the
rest are pinned by the manual.
