# Eval tools — live A/B checks of the agent

- Owns live evals of what the agent does with its tools: the right tool, source and arguments, wasted turns, tokens and cost per ask. Nothing here is imported by the bot or run by `make test`; every run spends real money on OpenRouter (a full run is about $0.10, images and voice lines included).
- `world.py`: Derp's real dispatcher, runtime and models against the fake Bot API (`tests/e2e/fake_api.py`) and a scratch database migrated afresh each run (`EVAL_DATABASE_URL`, by default `derp_eval` in the `make db-test-up` container); `seed` builds one synthetic group per scenario. Synthetic people and chats only, never real ones.
- `scenarios.py`: one ask each and a check of the decision that matters. Checks name the current tools and arguments, so a change to the tool surface updates them in the same change.
- `run.py` runs them (`uv run python -m tools.eval.run LABEL --repeat 3`; results and `--record`ed request bodies under `out/`, gitignored), `compare.py` sets two runs side by side, `tokens.py` measures a recorded request's real prompt tokens per part and per tool on one pinned provider.
- Decide on three or more runs per side, never one: the same ask flips between runs. For an A/B against an older commit, run that commit's copy from a `git worktree` with its own `uv sync` and another `EVAL_DATABASE_URL`.
