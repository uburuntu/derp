"""Prompt authority keeps admin policy trusted and factual memory untrusted."""

from types import SimpleNamespace

from derp.llm.prompts import BASE_SYSTEM_PROMPT, build_chat_system_prompt


def test_admin_policy_is_the_only_dynamic_trusted_instruction() -> None:
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            chat_model=SimpleNamespace(admin_policy="Prefer concise replies."),
        )
    )

    prompt = build_chat_system_prompt(ctx)

    assert prompt.startswith(BASE_SYSTEM_PROMPT)
    assert "## Admin Chat Policy\nPrefer concise replies." in prompt
    assert "Chat Memory" not in prompt


def test_missing_chat_policy_keeps_prefix_stable() -> None:
    ctx = SimpleNamespace(deps=SimpleNamespace(chat_model=None))

    assert build_chat_system_prompt(ctx) == BASE_SYSTEM_PROMPT
