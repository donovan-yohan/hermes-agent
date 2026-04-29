# CLAUDE.md

repo discipline for donovan-yohan/hermes-agent fork:

- `upstream/main` is the canonical Nous branch.
- local/fork `main` is the upstream-sync lane. keep it as clean as possible and do **not** land Kyle-specific customizations directly on it unless Kyle explicitly decides to abandon that pattern.
- `dy-main` is the default customized branch for Donovan/Kyle-specific Hermes behavior.
- in normal conversation inside this fork, `main` should be interpreted as `dy-main` unless the speaker explicitly says `upstream/main`, `Nous main`, or otherwise clearly refers to the upstream Hermes branch.
- new feature branches should normally branch from `dy-main` and PR back into `dy-main`.
- when syncing upstream changes, update `main` from `upstream/main` first, then rebase/merge `dy-main` onto the refreshed `main`.

runtime vs development checkout:

- do not develop directly inside `~/.hermes/hermes-agent` unless there is a very explicit reason.
- use a separate development clone at `~/Documents/Programs/personal/hermes-agent` for code edits, tests, branch work, and agent-driven coding sessions.
- treat `~/.hermes/hermes-agent` as the live runtime install that current Hermes CLI and gateway processes execute.
- after validating changes in the development clone and landing them on `dy-main`, update the live runtime copy by pulling the desired branch there and restarting affected long-running processes.
- this separation matters because Hermes may be editing its own source tree; do not mutate the live runtime install casually while it is actively running.

sidecar / browser-extension coordination:

- companion sidecar repo lives at `~/Documents/Programs/personal/hermes-browser-sidecar`.
- when changing Hermes browser/sidecar integration surfaces, inspect both repos together.
- do not silently break sidecar assumptions around:
  - bridge or sidecar health checks
  - capabilities discovery
  - default localhost URLs/ports
  - auth/header expectations
  - session/interrupt/message transport semantics
- if Hermes-side changes affect the sidecar contract, update the sidecar repo or document the exact incompatibility before merging.
- prefer explicit compatibility tests or a reproducible manual verification path when changing cross-repo integration behavior.

pr hygiene:

- keep upstream-sync work separate from custom feature work.
- keep repo-structure/branch-policy commits separate from behavior changes when practical.
- before opening a PR for sidecar-related changes, verify the current branch target matches the branch discipline above.

upstream sync workflow (reduce conflict noise):

- the prior squash-sync pattern (`chore: upstream sync (N commits) (#PR)`) imports upstream commits as one giant blob on `dy-main`. on the *next* sync, git can't tell which lines are real customizations vs prior upstream content, and re-prompts conflicts on every file the squash touched. these are "ghost conflicts" — `dy-main` has no actual customization for that file, only a stale upstream snapshot.
- preferred sync flow:
  1. `git fetch upstream && git fetch origin`
  2. fast-forward fork `main` to `upstream/main` and push (no merge commit, no squash).
  3. branch `chore/upstream-sync-YYYY-MM-DD` from `origin/dy-main`.
  4. `git merge upstream/main` from that branch.
  5. compute the real-custom-file set with: `git log --no-merges --name-only --format=COMMIT:%H upstream/main..origin/dy-main` then exclude any prior `chore: upstream sync` squash commits — what's left is files with genuine `dy-main` edits.
  6. for files in conflict but NOT in the real-custom set: `git checkout --theirs -- <file>` (or copy from `upstream/main`) — these are ghost conflicts.
  7. hand-merge only the real-custom files. expect ~10–15 of those, not the 100+ surface from a naive merge.
  8. PR back into `dy-main` as a normal merge commit (not squash) so future syncs preserve the per-commit blame trail and `git merge-base` continues to work.
- isolate `dy-main` customizations into their own files when possible (e.g. `gateway/local_client_bridge.py`, `agent/onboarding.py` overrides). edits inside upstream-owned files (`run_agent.py`, `cli.py`, `gateway/run.py`) are the ones that re-conflict each sync — keep those edits minimal and well-commented.
- when a `dy-main` customization is small + general-interest (default change, new alias, bug fix), prefer upstreaming it to Nous rather than carrying it locally. that converts a recurring conflict into a one-time PR.
- if a sync introduces an obvious wrong-shape merge artifact (duplicate dataclass field, dead import, broken test asserting impossible logic), fix it in the same sync PR — don't ship merge debt.
