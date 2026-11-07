# Examples

Live copy-once templates stay next to the hooks (Kiro reads `.kiro/`):

```bash
cp .kiro/tracing.context.example.json .kiro/tracing.context.json
cp .kiro/langfuse.local.env.example .kiro/langfuse.local.env && chmod 600 .kiro/langfuse.local.env
cp .kiro/objective-checks.json.template .kiro/objective-checks.json
```

Never copy an example **over** a filled env file.

The native workflow’s sample module is `sample/checkout_helper.py` (path referenced by the recipe). Do not move it without editing `.kiro/workflows/two-step-demo.workflow.json`.
