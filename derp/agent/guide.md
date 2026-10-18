# Derp — how it works

This guide is for answering people's questions about Derp. It explains concepts only: every number (prices,
allowances, limits, retention) is appended from the running configuration, and the commands and tool lists are
generated from the code, so they are always current. A test rejects digits in this file.

## overview

Derp is an AI friend that lives in Telegram chats: answers, banter, lookups, pictures, memes, polls, reminders.
- Private chat: Derp answers every message. Groups: only when called (see groups).
- Inline mode: type @username and a question in any chat; the answer is edited into the sent message.
- Guest mode: a mention in a chat Derp isn't a member of gets one reply. Inline and guest answers see no chat
  history and nothing from them is stored except accounting. They can still look things up (weather, exchange
  rates, time zones, Wikipedia and the like), calculate, answer about Derp itself and, beyond free models, search
  the web; they can't draw, post polls or stickers, or set reminders.
- It looks at photos (iPhone HEIC files and other image files too), stickers and albums, listens to voice messages
  and video notes (transcribed), and opens links. Animated and video stickers, GIFs and files that aren't pictures
  are seen through Telegram's preview picture of them. Photos inside rich messages and in messages quoted from
  another chat count like any photo. A photo someone sent just before calling Derp comes along with the question
  ("make it a cat" edits it), and a reply to Derp's answer about a photo still sees that photo, unless the question
  is clearly about something else. Asked about a video, GIF or video note, it looks at a still frame of it. Media
  tools also take videos, GIFs and audio (memes, reversing, song recognition). Group voice messages nobody asked
  about are transcribed in the background up to a daily limit; past it, a voice message is transcribed when someone
  calls Derp in reply to it. Short audio files (forwarded memos, recordings) and the speech in videos are
  transcribed when someone calls Derp with one or in reply to one. Recordings over the length limit aren't
  transcribed, and Derp says so instead of guessing.
- Profile pictures and the chat photo work as sources: "add me a hat on my profile picture", "draw Vasya as a
  knight", "meme @vasya's avatar" or "meme our chat photo". People are found by name or @username among those seen
  in this chat; in reply to a forward, it's the person it was forwarded from when Telegram shows them. Derp sends
  the result; bots can't change anyone's profile photo, so people set it themselves.
- One tool's result feeds the next: draw a picture, then meme it, cut it out or make it a sticker, in one answer.
  Only the end result is posted (no trail of in-between pictures), and the drawing and the cutout are paid for
  only if something made from them arrives.
  Stickers are sent one by one; Derp doesn't make sticker packs.
- People never choose a model: Derp picks one by who pays (see money). Bought and free daily answers go only to
  AI providers that keep nothing (zero data retention).
- /feedback (or asking Derp) passes words to Derp's developer, who may send a short thank-you in private once it's
  handled.

## groups

- Call Derp by @mention, by name (derp, дерп and their inflections) or by replying to one of its messages. It stays
  quiet otherwise and never joins in on its own. A message with just the name answers only when it talks to Derp
  ("derp, settle this for us"); the word used as slang ("classic derp moment") or Derp talked about to others
  ("derp drew a horse yesterday") gets nothing and counts as an ordinary message (in a mentions-only group it isn't
  kept). If that check is unavailable, Derp answers as usual.
- Context: while the group allows it, Derp reads the chat to keep up and remembers it for the retention period.
  Admins can switch to mentions only in /settings (that deletes the stored context). With Telegram's privacy mode
  on and Derp not an admin, it only sees messages addressed to it anyway.
- Making Derp an admin is optional: it then sees every message, reactions and members joining or leaving, and can
  pin messages for admins. The join notice and /settings (while Derp can't read the chat) carry a button for it.
- In forum groups each topic is its own conversation.
- When Derp cites an earlier message it links it (supergroups and channels), and it can @mention someone
  when asked to tag them.
- It catches people up (day summaries), runs polls and quizzes with a per-chat leaderboard, and keeps notes and
  the chat's rules (see memory).
- Titles of the day ("who's the красавчик дня?"): Derp draws one of the recently active members at random; asking
  again the same day gives the same person, and the chat keeps a leaderboard. /forget removes a person's titles.
- Commands in groups work as /command@username or from the command menu; the menu's personal commands are
  ephemeral (only the sender sees the answer). Bare commands may belong to another bot and are ignored.

## money

- Answers cost Derpcoins: what the AI providers actually charged plus a margin, rounded up. If nothing reached
  the chat, nothing is charged. If an answer breaks or gets too expensive after part of it (even just a
  reaction) reached the chat, the work so far is charged, and Derp says so. Reply /info to any answer to see its
  model, tokens, cost and who paid.
- Free daily Derpcoins: everyone gets an allowance each UTC day on a cheaper model with fewer tools; it doesn't
  accumulate. Drawing images needs paid Derpcoins (Derp+, bought packs or a group wallet). On paid Derpcoins a
  clearly hard question (multi-step math, code, proofs) gets more thinking, which costs a little more.
- Packs: bought with Telegram Stars in /buy; bought Derpcoins don't expire.
- Derp+: a Stars subscription with a monthly allowance that doesn't roll over; nothing else changes. Cancel it in
  Telegram's Stars settings; it runs to the end of the paid period. If a renewal payment fails, Derp tells the
  subscriber privately once, with a button to subscribe again.
- Group wallet: anyone can buy Derpcoins for a group; they are spent first in that group. In a group, /buy asks
  whose wallet first — the group's (its balance is shown to everyone) or the presser's own — and the pack menu then
  opens only for the presser, with payment links; the payment happens in their private chat with Derp, where the
  receipt goes too. A personal purchase never shows anyone's balance in the group.
- Buying for a group posts one quiet thank-you in that group (in the topic it was bought from): the buyer's name,
  the exact amount and roughly how long the wallet lasts at the group's recent pace (early on, about how many
  answers). More purchases soon after update that post instead of adding new ones. Only the buyer can hide their
  name, with the post's button for a few minutes; that also keeps them "a mysterious patron" in later group
  purchases. Asking Derp ("don't name me when I chip in", or the opposite) switches it either way. /help and
  /credits in a group list who chipped in lately (named buyers only, refunds excluded); Derp mentions patrons only
  when asked.
- When a group's wallet can no longer pay for an answer, Derp says so once, quietly, with a top-up button, and not
  again until someone tops it up; from then on each asker's own Derpcoins pay, as below.
- Who pays — private chat: the person's paid Derpcoins (Derp+ first, as it expires, then bought packs), then
  their free daily ones. Group: the group wallet, then the asker's paid Derpcoins (unless they turned off "my
  Derpcoins in groups" in /settings), then the asker's free daily ones.
- When nobody can pay, Derp offers free models: opt-in (an admin turns them on for a group), and their providers
  may store what they get. The offer says when the free daily Derpcoins refill and offers buying first (in a group,
  topping up the group's wallet). In a group each person sees it at most once a UTC day; after that Derp only reacts
  with 🙈. Asking Derp to turn them on only brings up the notice; the person (or in a group an admin)
  presses its button. Free models get read-only and Telegram tools only.
- A request that ran out of Derpcoins isn't lost: when the asker buys Derpcoins shortly after (for themselves, or
  for the group in the topic they asked in), Derp offers a Continue your request button under it. Only the asker
  can press it, and only once; it answers the original request, charged like any answer. A purchase by someone else
  never replays another person's request, and an old request just says to ask again. Turning on free models answers
  the waiting request the same way.
- An action that costs more than the approval threshold (or a large share of the payer's balance) shows its price
  with Make it / Cancel buttons first; only the asker decides. Make it finishes the whole request: "draw a frog and
  caption it", "draw it and make it a sticker" or "draw it, then cut it out" still get the caption, sticker or
  cutout. It is charged once the result is in the chat, like any answer. An unanswered prompt expires and loses its
  buttons; cancelling or letting it expire is free. Once the answer arrives the prompt disappears; if nothing
  arrives, the prompt says so and nothing is charged. If the payer can no longer cover it when Make it is pressed,
  the prompt says so, with a button to buy (in a group, to top up the group).
- Balances are private: /credits in a group answers ephemerally or by DM, never in the group.
- Derpcoins aren't money: no cash-out, no transfers between people. Payment problems: /paysupport. A refund
  returns the Stars and removes what is left of that purchase's Derpcoins; refunding a Derp+ payment also ends
  Derp+ and stops its renewal.

## memory

- Notes: "remember that…" saves a short note. Chat notes are shared in that chat; personal notes belong to one
  person in one chat and only they can change or remove them. Nothing crosses chats.
- A saved fact can be corrected ("actually, it's Thursdays"): Derp rewrites that note instead of adding a second
  one. A list (films to watch, places, games) lives in one note that Derp edits to add or strike items.
- Derp sees the newest notes with every message, with room kept for the asker's own; it searches older ones
  when they matter.
- Admins choose the chat notes mode in /settings: auto (Derp also saves lasting chat lore on its own,
  sparingly), members (saved only when someone asks), admins (only when asked; others' new notes and changes
  become proposals approved in /memory) or off (only admins change or remove the chat notes left). Personal
  notes work in every mode.
- Rules: standing instructions an admin sets for how Derp behaves in the chat. Rules quoted word for word apply at
  once; anything Derp would reword is shown with Apply / Cancel for that admin.
- /memory lists the rules and notes. Notes and rules need bought or free daily Derpcoins (not free models).

## reminders

- A reminder posts its text at a time; a scheduled prompt makes Derp answer a request at that time ("every
  morning post a fun fact"). Both can repeat daily, on weekdays, weekly or monthly in the chat's time zone.
- Time zone: each chat has one, set by telling Derp where you are ("I'm in Moscow", "use Berlin time"). Until it is
  set, Derp asks once before scheduling at a clock time. In a group anyone can set a missing zone; changing it needs
  an admin. Repeating jobs keep their local time when the zone changes; one-off reminders keep their moment, and
  setting the first zone moves nothing. Day summaries and "today" follow the chat's zone; the free daily
  Derpcoins still refill on the UTC day.
- Anyone can schedule one-offs; repeating jobs in groups need an admin. Creators manage their own jobs and
  admins any: Derp moves, rewords, re-repeats, cancels or resumes them on request ("move that one to tomorrow at
  six"), changing the job in place, and /settings has a page of the chat's reminders (beside the zone) with
  pause, resume and delete buttons. Moving a one-off that already fired brings it back.
- A reminder arrives with snooze buttons (a few minutes, an hour, tomorrow at the same time) for its creator or
  an admin; others get a short refusal. Snoozing a one-off moves it; snoozing a repeating reminder adds one
  extra reminder and leaves the series as it was (a second snooze moves that extra one, cancelling the series
  cancels it too).
- A scheduled prompt is paid when it fires, by the usual payer order; after several unpaid firings in a row the
  job pauses, and the note saying so has a Resume button. Removing Derp from a chat pauses its jobs.

## privacy

- Bot commands and inline/guest conversations are never stored. Telemetry holds numbers only, except
  that when an answer fails a shortened copy of that exchange, credentials masked, is kept for debugging.
- /forget as a reply removes that message; /forget alone removes everything the person said in that chat, with
  their reactions, votes, notes and reminders there. Day summaries that mention it are redone without it.
- Shortening how long a chat's history is kept asks for confirmation first, since older messages get deleted;
  making it longer applies at once.
- The full privacy page follows.

## limits

- Each answer has hard caps: tool calls, visible outputs, output length, time and Derpcoins. Nearing the tool,
  time or Derpcoin cap, Derp stops using tools and answers with what it has. An empty reply or a busy model gets
  one retry on a backup model. Media effects have size and length limits. Values follow.

## tools

- Tiers: "paid" is bought Derpcoins, Derp+ or the group wallet; "sponsored" is the free daily allowance; "free" is
  free models. On free models, toolbox tools are found on demand; they work the same.
- Every answer can also search the web and open links (paid and sponsored) and run exact maths in a Python sandbox
  with no network or files (every tier). A web search has its own fee, so it runs only while the answer's budget
  still covers it.

## source

- Derp is open source. For exactly how something works, its own code and developer notes are readable with
  search_source and read_source (paid and sponsored answers); cite the lines with the links they return.
