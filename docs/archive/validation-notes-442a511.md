# Redacted local proof at commit 442a511 (not your IDs)

These numbers are historical. Do not put them in active config. A later commit on this branch **changes HEAD**, so you must run a **fresh** native workflow at **your** checked-out SHA before scoring.

At `442a511` on a local Langfuse 4.76.0:

- A native two-step workflow reached `completed` with wall time **23.837s** from root timestamps (not parent-chat latency)
- Ingest: **19 observations / 4 traces** for that run
- New-run automation labels (`identity_type=automation` / `kiro-automation` tag) and git SHA / workflow version were present on that ingest
- Task 3 wrote **3 BOOLEAN true** scores (`build_pass`, `test_pass`, `lint_clean`) on the **final-step** 32-hex trace
- v3 score readback was non-empty; a second finalize **deduped 3→3** (no extra writes)

Identifiers from that run (workflow id, project id, dashboard ids, user id) are **omitted on purpose**. They are not defaults for a clean clone.

Earlier historical ingest at a different SHA cannot receive checks from a later SHA (provenance gate).
