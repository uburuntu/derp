# Settings and user-facing surface

- Owns `/start`, `/help`, `/settings` and per-chat / per-user policy UI. Members can view; only admins mutate chat policy, and the role is re-checked on every mutation.
- `onboarding.py`: one compact notice per group — when Derp joins (it owns `my_chat_member`, recording membership via `history.members` first) or on the first invocation of a chat that predates it. It states how to summon Derp, whether Derp can actually see the chat ("mentions only" when ambient is off or privacy mode hides messages), and offers an optional "make me admin" deep link. The first person's language becomes the chat locale when none is set.
