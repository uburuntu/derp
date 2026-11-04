# Context tools — measuring the prompt cache

- Owns offline measurement of how well requests reuse the provider's prompt cache, for evolving the window (`agent/context.py`), the prompt layout (`agent/persona.py`) and chat routing (`inference/`). Nothing here is imported by the bot.
- `simulate.py` replays a chat's real runs through window strategies using the real `build_window`: per run, the window, the prefix shared with the topic's previous request, the situation, the gap and the run's tool rounds. It reads the database read-only, prints numbers only, and runs inside the prod container (script on stdin; see its docstring). Its `prod` strategy must match production's window policy: update its defaults when `agent/context.py` changes.
- `analyze.py` (local, stdlib only) turns that output into cache shares and input costs under assumptions about how long a provider keeps a prefix, which route's prices apply, and how much of a warm prefix the provider actually serves.
- `probe.py` measures real cache retention per provider route (model@endpoint) over gaps of seconds to 10 minutes. It spends real money (about a cent per route) and runs where the OpenRouter key is.
- Read real measurements beside the simulation: `provider_calls` (per call: provider, input, cache read) shows what providers actually served. The simulation is the ceiling a perfect cache would reach.
