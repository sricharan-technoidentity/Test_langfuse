# Archive (historical, host-specific)

These notes describe **past** local acceptance on someone else’s Langfuse box. They are not your project ids, dashboard ids, trace ids, or user ids.

| File | What it is |
|------|------------|
| `LAUNCH.md` | Older validation checklist |
| `RUNTIME_ACCEPTANCE.md` | Parent-box acceptance after `9d5390b` / `442a511` patches |
| `validation-notes-442a511.md` | Redacted proof that scores can ingest when SHA matches |

Do not copy identifiers from these files into `.kiro/tracing.context.json` or `.kiro/langfuse.local.env`.

A labeled fixture of a failed-acceptance dashboard cleanup plan lives at `tests/fixtures/failed-acceptance-placements.fixture.json` (`fixture: true`, `not_user_defaults: true`).
