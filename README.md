# Derp

OpenRouter-first Telegram assistant built with
[aiogram](https://github.com/aiogram/aiogram) and
[Pydantic AI](https://github.com/pydantic/pydantic-ai). It supports scoped
conversation history, paid private inference, consented zero-cost models,
durable media delivery, Telegram Stars billing, and an in-Telegram operator
console.

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv)
- Docker (for PostgreSQL)
- Telegram bot token ([@BotFather](https://t.me/BotFather))
- OpenRouter API key
- Google API key for TTS

## Setup

```bash
cp env.example .env   # Configure your credentials
make dev-setup        # Creates venv, starts DB, runs migrations
make run              # Start the bot
```

The PostgreSQL 18 Compose service uses the `postgres-data-v18` volume. Export
and restore any data needed from the former `postgres-data` volume before
removing it; PostgreSQL 17 data directories are not binary-compatible with 18.

## Configuration

Key environment variables in `.env`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_URL` | PostgreSQL connection string |
| `OPENROUTER_API_KEY` | Primary text and image inference key |
| `OPENROUTER_ENABLED_FEATURES` | Reviewed OpenRouter feature set; production startup rejects route downgrades |
| `GOOGLE_API_PAID_KEY` | Google TTS key |
| `LOGFIRE_TOKEN` | Logfire observability token |
| `LOGFIRE_CAPTURE_AI_CONTENT` | Local-only opt-in for Pydantic AI text capture (default `false`) |
| `OPERATOR_IDS` | Required production Telegram operator allowlist |
| `PUBLIC_PURCHASES_ENABLED` | Public Stars intake gate; keep `false` until release validation completes |
| `ENVIRONMENT` | `dev` or `prod` |

See `env.example` for the complete list.

See [docs/observability.md](docs/observability.md) for telemetry ownership,
content-capture policy, privacy boundaries, and shutdown behavior.

Free OpenRouter models are unlimited after explicit versioned consent, but are
non-ZDR and available only in private and inline contexts. Paid and group
inference uses private ZDR routing. TTS stays on Google. Thinking, video
generation, and standalone transcription remain hidden and fail closed.

## Testing

`make test` runs the fast suite without PostgreSQL. `make test-db` runs the
ordinary database integration suite. `make test-e2e` runs the longer Telegram
journeys against the mocked Bot API and a migrated PostgreSQL database. The E2E
suite is isolated in CI and excluded from ordinary coverage runs. It uses real
aiogram polling and HTTP/JSON, production middleware, independent database
sessions, and deterministic local inference; unsupported Bot API methods fail
the test instead of receiving permissive mock responses.

## Commands

Run `make help` to see all available targets.

Cross-cutting work deferred from cleanup is tracked in
[`docs/architecture-roadmap.md`](docs/architecture-roadmap.md).

Release and operations:

- [`docs/release-v0.1.0.md`](docs/release-v0.1.0.md) - release checklist and launch economics
- [`docs/deployment.md`](docs/deployment.md) - deployment transaction and recovery
- [`docs/operator-console.md`](docs/operator-console.md) - private Telegram control panel
- [`docs/inference/openrouter-references.md`](docs/inference/openrouter-references.md) - reviewed provider evidence and drift workflow
- [`PRIVACY.md`](PRIVACY.md) and [`TERMS.md`](TERMS.md) - public privacy and service terms
