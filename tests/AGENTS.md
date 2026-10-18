# Tests

- Layout mirrors `derp/`. Unit tests need nothing; `db`-marked tests need `TEST_DATABASE_URL` (`make db-test-up`); the schema is always built from Alembic, and tables are truncated before each test.
- `e2e/` drives the real dispatcher over HTTP against `FakeBotAPI`, a strict Bot API: requests are validated against aiogram's method models, unknown methods/fields and unanswered callbacks fail at teardown. Add a `_m_<method>` implementation (or `api.handle(...)` in a test) when the bot starts using a new method.
- The harness polls with `handle_as_tasks=False`, so `conversation.settle(sent)` means the handler finished. Prefer waiting on calls or DB state over sleeps.
- Inference in tests is scripted with Pydantic AI `FunctionModel`/`TestModel` (e2e journeys swap the chat model through `Services.model_factory`: `e2e/model.py` `ScriptedModel`); tests never reach the network. `live`-marked tests are excluded by default and spend real money.
