# Compatibility fixtures

`compat-model-config.json` is the public model catalog from the reviewed proxy
integration commit `c13c117`. It is a deterministic **test fixture**, not a runtime
configuration or a second editable model registry.

The imported historical tests contain literal model IDs, fixed dialog-tier choices
and mocked provider catalogs. Running them against a user's customized catalog
incorrectly treats valid configuration edits as code regressions. Their fixed
scenarios now use this fixture inside a temporary test sandbox.

Before those historical cases run, `scripts/run_compat_tests.py` loads the actual
current production catalog through the real runtime modules in the same isolated
sandbox. It checks schema/semantic validation, the complete model/provider registry,
dialog tiers, computed fallback chains, low-tier pool, embedding selection and
OpenRouter mappings. A failure there still fails CI. Additional tests exercise
arbitrary renamed models and detect mismatches between valid data and runtime code.

Production, live health probes and the Vercel editor continue to use only
`src/rp_core/compat_runtime/model-config.json`.
