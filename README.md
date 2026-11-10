<div align="center">

<img src=".github/assets/derp.jpg" width="120" alt="Derp's avatar: a grinning robot head">

# Derp

**One of the group. Remembers everything, roasts accordingly.**

Quiet until called, it keeps up with the conversation, handles text, pictures and voice, and is honest about money.

[![Open in Telegram](https://img.shields.io/badge/Telegram-@DerpRobot-26A5E4?logo=telegram&logoColor=white)](https://t.me/DerpRobot)
[![CI](https://github.com/uburuntu/derp/actions/workflows/ci.yml/badge.svg)](https://github.com/uburuntu/derp/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)

<img src=".github/assets/demo.gif" width="860" alt="Demo: Derp in a group chat settles a dinner argument, catches someone up, draws the friends as a dungeon party, turns a message into a quote sticker, names the красавчик дня, runs a poll and shows what an answer cost">

<sub>A scripted demo of real features. The chat and the people in it are made up.</sub>

</div>

## What it does

Add [@DerpRobot](https://t.me/DerpRobot) to a group and say its name (derp, дерп), @mention it or reply to it. There are no
commands to learn. It replies in the language it's asked in; its menus speak English and Russian.

| | |
|---|---|
| 💬 **Talks like one of the group** | Short replies, opinions, running jokes. In groups it answers only when called; "classic derp moment" gets nothing. |
| 🧭 **Catches you up** | "What did I miss?" covers a day, a week or the time since your last message, with replies and questions for you first. It also finds half-remembered things by gist. |
| 🎨 **Makes pictures** | Draws and edits images, puts friends into one picture from their profile photos, makes memes, cutouts, stickers and quote stickers. |
| 👀 **Reads everything you send** | Photos, albums, stickers, voice messages, video notes, clips with sound, PDFs and links. |
| 🔎 **Looks things up** | Web search, weather, Wikipedia, prices for currencies, crypto and stocks, Polymarket odds, charts, and a Python sandbox for exact maths. |
| 🎲 **Runs the group** | Polls with members' photos, quizzes with a leaderboard, titles of the day, notes and chat rules. |
| ⏰ **Remembers** | One-off and repeating reminders, and scheduled prompts ("every morning, post a fun fact"). |
| 🗣️ **Talks back** | Short voice messages when asked: yell it, whisper it, sing it. |
| ⭐ **Pays fairly** | Free Derpcoins every day, more with Telegram Stars or a group wallet anyone can top up. Reply `/info` to any answer to see its model, tokens and cost. |
| 🔑 **Brings your own key** | People with ChatGPT Pro or Claude Max can plug their OpenAI or Anthropic API key into a chat for a smarter model. |

It also works inline (`@DerpRobot question` in any chat) and as a one-reply guest in chats it isn't a member of.
Ask Derp about itself and it answers from [`derp/agent/guide.md`](derp/agent/guide.md), with prices and limits taken
from the running configuration.

## Privacy

Paid and free daily answers go only to AI providers that keep nothing (zero data retention). Bot commands and inline or
guest conversations are never stored. Telemetry holds numbers only, except a shortened, masked copy of an answer that
failed. `/forget` removes what you said in a chat; admins can wipe the whole chat. Details are in [`PRIVACY.md`](PRIVACY.md).

## Run your own

You need a bot token from [@BotFather](https://t.me/BotFather), an [OpenRouter](https://openrouter.ai) key, Docker and
[uv](https://docs.astral.sh/uv/).

```bash
cp env.example .env       # bot token, OpenRouter key, your Telegram id
make install
make db-dev-up migrate    # PostgreSQL in Docker
make run
```

`make check` runs lint, types and every offline test. `make help` lists the rest.

Built with Python 3.14, [aiogram](https://github.com/aiogram/aiogram), [Pydantic AI](https://ai.pydantic.dev) over
[OpenRouter](https://openrouter.ai), PostgreSQL (SQLAlchemy and Alembic) and Logfire.

## Learn more

- [`AGENTS.md`](AGENTS.md): how the code is laid out and the rules it keeps. Every package has its own.
- [`deploy/README.md`](deploy/README.md): a push to `main` ships over SSH with preflight checks, migrations and
  automatic rollback.
- [`demo/`](demo/): the Remotion project behind the video above.

## License

[AGPL-3.0](LICENSE). If you run a modified Derp for other people, publish your changes under the same license.
