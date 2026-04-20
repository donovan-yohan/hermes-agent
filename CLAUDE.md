# CLAUDE.md

repo discipline for donovan-yohan/hermes-agent fork:

- `upstream/main` is the canonical Nous branch.
- local/fork `main` is the upstream-sync lane. keep it as clean as possible and do **not** land Kyle-specific customizations directly on it unless Kyle explicitly decides to abandon that pattern.
- `dy-main` is the default customized branch for Donovan/Kyle-specific Hermes behavior.
- in normal conversation inside this fork, `main` should be interpreted as `dy-main` unless the speaker explicitly says `upstream/main`, `Nous main`, or otherwise clearly refers to the upstream Hermes branch.
- new feature branches should normally branch from `dy-main` and PR back into `dy-main`.
- when syncing upstream changes, update `main` from `upstream/main` first, then rebase/merge `dy-main` onto the refreshed `main`.

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
