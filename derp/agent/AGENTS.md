# Agent

- `models.py`: `agent_runs` (one row per invocation: trigger, funding, served model, usage, cost, charge, Pydantic AI history) and `run_outputs` (every delivered message/poll/sticker/image/reaction, so `/info` resolves any output to its run).
- `runs.py` owns the run lifecycle. `finish_run` is the single place that ledgers a run's provider responses and stores its history; stored history replaces inline binary with markers and follows the chat's retention.
- Tools never receive `chat_id`/`user_id` arguments; scope comes from run dependencies.
