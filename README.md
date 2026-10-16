# Derp

The sharp friend already in your Telegram chat: [@DerpRobot](https://t.me/DerpRobot).

Quiet until called, aware of the conversation, good with text, pictures and voice, and honest about money.

- 💬 **Talk**: private chats, or groups when someone says its name, @mentions it or replies
- 🔎 **Look things up**: web search, links, weather, exchange rates, Wikipedia, a Python sandbox for exact maths
- 🎨 **Make things**: draw and edit pictures, memes, reversed clips, cutouts, plus song and anime recognition
- 👥 **Run the group**: polls and quizzes with a leaderboard, catch-up summaries, notes and chat rules
- ⏰ **Remember**: one-off or repeating reminders and scheduled prompts
- ⭐ **Pay fairly**: free Derpcoins every day, more with Telegram Stars, and `/info` on any answer shows its cost

## Stack

Python 3.14 · [aiogram](https://github.com/aiogram/aiogram) · [Pydantic AI](https://ai.pydantic.dev) over
[OpenRouter](https://openrouter.ai) · PostgreSQL (SQLAlchemy + Alembic) · Logfire

## Run it

```bash
cp env.example .env       # bot token, OpenRouter key, your Telegram id
make install
make db-dev-up migrate    # PostgreSQL in Docker
make run
```

`make check` runs lint, types and every offline test. `make help` lists the rest.

## Learn more

- [`AGENTS.md`](AGENTS.md): how the code is laid out and the rules it keeps. Every package has its own.
- [`deploy/README.md`](deploy/README.md): a push to `main` ships over SSH with preflight checks, migrations and
  automatic rollback.
- [`PRIVACY.md`](PRIVACY.md): what Derp stores and who processes it.
