# Scheduler

- Owns `jobs`: a reminder (`message`: posts the text) or a scheduled prompt (`prompt`: Derp answers it), once or repeating (`daily`, `weekdays`, `weekly`, `monthly`) at a wall-clock time in the chat's time zone.
- `jobs.py`: the rules — next occurrence (DST-safe, missed firings skipped, never replayed), limits per chat and per person, ISO local-time parsing, and `show_time` (a `tg://time` Markdown date that Telegram renders in each viewer's time zone).
- `worker.py`: one worker ticks every 20 s, claims due jobs with `SKIP LOCKED` and moves them on in the same transaction before firing, so a job fires at most once per occurrence. Prompts run through `Runtime.answer` as an id-0 invocation from the creator (`trigger='job'`, never stored), funded at fire time; an unfunded firing posts a note and `MISS_LIMIT` in a row pause the job. A chat refusing Derp's messages pauses its jobs.
- Who may do what lives in the agent tools (`agent/tools/scheduler.py`): anyone schedules one-offs; repeating jobs in groups need an admin; creators cancel/resume their own, admins any.
- Retention: finished jobs are deleted after 30 days; `/forget` (everything) deletes the person's jobs in that chat.
