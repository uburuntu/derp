# Deployment tools

- `build_release.py` (CI) builds `derp:<sha>` (the commit also goes in as `SOURCE_REVISION`, so Derp links its own source at the deployed revision) and a private gzip archive with metadata; `client.py` (CI) streams it over verified SSH; `host.py` is the host's forced-command wrapper, installed as `~/derp-deploy/deploy.py`. The flow and host setup are in `deploy/README.md`.
- These run on system Pythons (3.10 on the host, the runner's `python3`), standard library only: ruff and pyright target 3.10 here. `client.py` and `build_release.py` load `host.py` for the shared contract.
- The request carries only `action`, image ID, revision, archive checksum and size; configuration stays on the host (`derp.env`, copied privately per release). Never accept commands, paths or environment from the request.
- Keep: archive validation before `docker load`, the host lock, preflight and migrations before cutover (named task containers, removed on timeout; migrations set a lock timeout), the legacy bot retired after the first successful release, one poller per token (stop before start), health-gated cutover with automatic restore, private diagnostics only (`PUBLIC_STATUS` lines are all CI ever prints).
- Tests: `tests/deployment/` drives `host.py` with synthetic archives and a scripted Docker.
