# Tools

Plain capabilities behind the agent's utility tools (`derp/agent/tools/utilities.py`); no aiogram or Pydantic AI here.

- `lookups.py`: `Lookups`, one shared HTTPX client created and closed by `Services`. Keyless public APIs (Open-Meteo, CoinGecko, open.er-api.com, Wikipedia, Urban Dictionary, arXiv) plus WolframAlpha and JDoodle when their keys are configured.
- Every call has a deadline and a response-size cap and returns compact text for the model; slow-moving answers are cached in memory for a few minutes.
- Failures raise `LookupError` whose `reason` is safe to show (an exception class or HTTP status, never URLs, queries or bodies). Nothing here logs requests.
- Keys are sent only to their own service.
- `timezones.py`: IANA time-zone conversion (arithmetic is the agent's Python sandbox, see `derp/agent/tools/code.py`).
- Tests inject an `httpx.MockTransport`; e2e journeys serve canned JSON through the `web` fixture and fail everything else like an outage.
