# Known limitations

## Task 2 widgets are observation proxies

Shipped dashboards count **observations** (or boolean scores), not native workflow runs.

**Unsupported on the public unstable widget API (not faked):**

- Distinct `workflow.run_id` (custom metadata is not a widget dimension)
- Distinct human engineers / adoption
- Native completion rate
- Native workflow duration (use `root.startedAt` / `root.endedAt` from workflow-state)
- True per-tool latency (Kiro tool spans often `start==end` → 0ms, missing not fast)
- Team / repo unique-run group-bys
- Parent-chat metadata as a first-class dashboard dimension
- Legacy `traces` view for new widgets

Hung parent-chat latency is not workflow duration.

## Task 3 scores

- Attached to the **final completed step trace** (32-hex OTEL), not a workflow-root trace (`wf_*` is not a trace id)
- `build_pass` = `python3 -m py_compile` (syntax), **not** a package build
- `test_pass` / `lint_clean` from `.kiro/objective-checks.json`
- Submit only when observation `git.sha` matches this checkout’s `HEAD`
- Changing this repo (new commit) **invalidates** SHA match for older ingest — run a fresh native workflow at the SHA you checked out

## Platform

Prereq scripts and unit tests were exercised on **Linux**. macOS and Windows are **untested** in this handoff.

## Operations this repo refuses

- AWS / paid cloud install
- `docker compose down -v` (volume wipe)
- Overwriting an existing env file with the example
- Printing secret keys
- Calling Kiro / IDE models from scripts (the demo workflow uses **your** Kiro plan)
