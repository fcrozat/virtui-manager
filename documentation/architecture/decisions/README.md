# Decisions

This directory is where this project keeps the decisions it would otherwise argue about
again in a code comment, a commit message, or a pull request.

These records are for whoever changes the code, not for the published manual: the
site is built from `documentation/manual` (see `docs_dir` in `mkdocs.yml`), so nothing
here is published with it. What a record states still shows up in the manual where
users need it — the manual says what a command does, this says why it does that.
The way in is from the code: the modules that act on a decision link to it where the
argument used to sit in a comment.

A record goes here when three things are true: the choice is not obvious from the
code, the reason for it does not fit in a comment, and undoing it later would cost
someone a decision rather than an edit. Anything else belongs in the code, where it
can rot honestly next to the thing it describes.

One file per decision, named `NNNN-a-short-phrase.md`, never renumbered and never
rewritten once accepted. A decision that changes gets a new record that supersedes
the old one, and the old one gets a status line saying so — the point of keeping the
superseded text is that a reader in two years can see why the new one was needed.

Each record answers four questions:

- **Context** — the situation that forced a choice, stated as it looked before the
  choice was made.
- **Decision** — what we do, in one sentence, in the present tense.
- **Why** — the reason, including the reasons the alternatives were refused.
- **What would make this wrong** — the observation that should send a future reader
  back to this decision. This section is the one that earns the record its place: a
  decision without a revisit condition is a belief.

The machine-readable surface of `vmc` has three deliberate departures from the
interactive tool. They look like inconsistencies to anyone reading the two side by
side, which is why each has a record:

- [0001 — Machine answers are not localized](0001-machine-answers-are-not-localized.md)
- [0002 — Machine commands read the hypervisor, not the cache](0002-machine-commands-read-the-hypervisor-directly.md)
- [0003 — Renaming a volume answers status 9](0003-renaming-a-volume-answers-status-9.md)
