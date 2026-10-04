# Rules

- Read `EMULEBB_WORKSPACE_ROOT\repos\emulebb-tooling\docs\WORKSPACE-POLICY.md`
  first; it is authoritative for workspace-wide rules.
- For materialization, topology, build orchestration, packaging, or generated
  workspace contracts, also read the routed
  `EMULEBB_WORKSPACE_ROOT\repos\emulebb-tooling\docs\reference\WORKSPACE-OPERATIONS-POLICY.md`
  annex.

Everything below is this repo's local deltas only:

- `python -m emule_workspace` is the authoritative orchestration surface.
- Keep orchestration topology-driven from the generated workspace manifest and
  repo-local `deps.json`.
- Do not add direct app-project build instructions to docs; route operators
  through this repo's supported entrypoints.
