# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.

## Repo-local label outside the five roles

| Label | Meaning |
| ----- | ------- |
| `implemented-awaiting-merge` | Work is committed on a feature branch with a note naming the merge commit, but no human has reviewed or merged it to `main` yet. |

This is **not** one of the five triage states and does not satisfy the "exactly one state role" rule; the issue carries no state role while it is in limbo. Two rules follow:

- Never apply `ready-for-agent` to it. `ready-for-agent` means *unstarted*: an agent scanning that label takes the issue and re-implements work that already exists, and a note in the thread is not read before the work starts.
- Drop the label when the branch merges to `main`, which is also when the issue gets closed. If the branch is abandoned, remove the label and return the issue to a real state.

Created 2026-11-05 for tickets #18-#31 on `vmc-review-followups`.
