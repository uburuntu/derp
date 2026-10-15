# History — chat knowledge

- Owns everything Derp knows about a chat: chats, users, members, messages and media, reactions, polls, stickers, digests.
- Rows are always scoped by `chat_id`; query helpers take the chat from the run context, never from tool arguments.
- Raw content follows the chat's retention window; privacy actions tombstone and scrub instead of leaving copies behind.
