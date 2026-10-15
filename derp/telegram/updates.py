"""Update types Derp subscribes to. Reaction and chat_member updates only arrive when Derp is an admin."""

ALLOWED_UPDATES: tuple[str, ...] = (
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "poll",
    "poll_answer",
    "message_reaction",
    "message_reaction_count",
    "my_chat_member",
    "chat_member",
    "pre_checkout_query",
    "subscription",
    "stopped_message_generation",
    "guest_message",  # guest mode: answered via answerGuestQuery, never stored
)
