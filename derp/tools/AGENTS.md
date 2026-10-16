# Tools

Plain capabilities behind the agent's utility tools (`derp/agent/tools/utilities.py`); no aiogram or Pydantic AI here.

- `lookups.py`: `Lookups`, one shared HTTPX client created and closed by `Services`. Keyless public APIs (Open-Meteo, CoinGecko, open.er-api.com, Wikipedia, Urban Dictionary, arXiv) plus WolframAlpha and JDoodle when their keys are configured.
- Every call has a deadline and a response-size cap and returns compact text for the model; slow-moving answers are cached in memory for a few minutes.
- Failures raise `LookupError` whose `reason` is safe to show (an exception class or HTTP status, never URLs, queries or bodies). Nothing here logs requests.
- Keys are sent only to their own service.
- Media effects: `workers.py` is the local CPU pool (2 slots + 4 queued, then "busy"; 20 MB in, 16 MP, 50 MB out, 60 s) and the killable subprocess runner (deadline, stdout cap, native diagnostics discarded). `effects.py` turns bytes into memes (image and video), reversed clips/voice, mirrored pictures, frames and audio samples via ffmpeg, which reads only local files of whitelisted containers and never sees user text. `captions.py` draws captions with Pillow (demotivator, Lobster, white panel) using the bundled OFL fonts in `fonts/`. `background.py` runs U²-NetP on CPU in its own rlimited process; its weights are fetched and checksum-verified at image build (`python -m derp.tools.background fetch PATH`), never during a request.
- `Lookups` also identifies songs (ACRCloud, signed with the configured keys) and anime scenes (trace.moe).
- `timezones.py`: IANA time-zone conversion (arithmetic is the agent's Python sandbox, see `derp/agent/tools/code.py`).
- Tests inject an `httpx.MockTransport`; e2e journeys serve canned JSON through the `web` fixture and fail everything else like an outage.
