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
- It looks at pictures in any image format (photos, iPhone HEIC files, image files), stickers and albums, photos
  inside rich messages and in messages quoted from another chat, and it opens links. A photo someone sent just
  before calling Derp comes along with the question ("make it a cat" edits it), and a reply to Derp's answer about
  a photo still sees that photo, unless the question is clearly about something else. Speech becomes text: voice
  messages and video notes are transcribed (in groups, ones nobody asked about only up to a daily limit; past it,
  when someone calls Derp in reply to one), and so are short audio files (forwarded memos, recordings) and the
  speech in videos when someone calls Derp with one or in reply to one. Recordings over the length limit, files too
  big for Telegram to hand bots and videos without sound aren't transcribed, and Derp says so instead of guessing.
  Asked about a video, GIF, video note, audio file, voice message or PDF, it opens the whole thing on a separate
  model that keeps nothing: watches clips with their sound, listens to audio and reads PDFs, and answers with what
  happens when (timestamps, page numbers, quoted speech). That costs a little extra, and paid answers take longer
  clips and recordings and more pages than the free allowance; its own voice messages need no listening, since it
  knows what it said. Whatever isn't opened whole (on free models, past the limits, when the answer can't afford
  it, or animated stickers and GIFs nobody asked about) is seen through a still frame or Telegram's preview
  picture, and Derp says why. Media tools also take videos, GIFs and audio (memes, reversing, song recognition).
- Profile pictures and the chat photo work as sources: "add me a hat on my profile picture", "draw Vasya as a
  knight", "meme @vasya's avatar" or "meme our chat photo". People are found by name or @username among those seen
  in this chat; in reply to a forward, it's the person it was forwarded from when Telegram shows them. Derp sends
  the result; bots can't change anyone's profile photo, so people set it themselves.
- Several members can be drawn together from their profile pictures: "draw Anya, Bob and me as our dungeon
  party" (a handful of people per picture; "me" is whoever asks). Each likeness stays on its own character;
  someone without a visible profile photo is drawn from the description instead, and Derp says so. The price is the
  same as any image.
- One tool's result feeds the next: draw a picture, then meme it, cut it out or make it a sticker, in one answer.
  Only the end result is posted (no trail of in-between pictures), and the drawing and the cutout are paid for
  only if something made from them arrives.
  Stickers are sent one by one; Derp doesn't make sticker packs.
- Quote stickers: "derp, quote this" (in reply), "quote the last few messages" or "quote the funniest moment today"
  turn chat messages into a Telegram-looking sticker — names in their colours, avatars (initials without a visible
  photo), reply bars, formatting, emoji and pictures — or a picture on request. A quote too long to read as a
  sticker comes as a picture. Only this chat's stored messages can be quoted, never forgotten or expired ones; for
  "the funniest moment" Derp finds the messages first.
- Asked to say something out loud (yell it, whisper it, sing it, laugh, do an accent, add a sound effect), Derp
  answers with a short voice message and acts it out in one of a few stock voices; it can't copy anyone's voice.
  It talks only when asked: ordinary answers stay text. A voice message can be played backwards like any other.
  It costs Derpcoins by length on paid answers; the free daily allowance covers a few short ones a day, counted
  together with heavy media effects. Free models can't speak.
- People never choose a model: Derp picks one by who pays (see money). Bought and free daily answers go only to
  AI providers that keep nothing (zero data retention).
- /feedback (or asking Derp) passes words to Derp's developer, who may send a short thank-you in private once it's
  handled. To flag one answer, reply /info to it and press Report a problem: pick what went wrong (wrong answer,
  missed context, wrong image, broken), then optionally send one message saying more in the chat where you pressed,
  or skip. In a group that message is visible like any other; Derp marks it with 👌 instead of replying.

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
- It catches people up: a day, yesterday, the past week, "what did I miss since my last message", or everything
  after a message (reply to it and ask). Replies to you, mentions of you and questions waiting for you come first,
  with links to the messages. It also finds half-remembered things by gist ("that restaurant near the station
  someone recommended"), even when it was said in the other language (Russian or English), and shows what was
  said around it.
- It runs polls and quizzes with a per-chat leaderboard (a quiz series stays open a day by default, so
  people can answer whenever they drop in; ask for a shorter one for a live game), and keeps notes and the
  chat's rules (see memory).
- Titles of the day ("who's the красавчик дня?"): Derp draws one of the recently active members at random; asking
  again the same day gives the same person, and the chat keeps a leaderboard. /forget removes a person's titles.
- Commands in groups work as /command@username or from the command menu; the menu's personal commands are
  ephemeral (only the sender sees the answer; when Telegram won't let Derp answer there in time, the answer
  comes in private). Bare commands may belong to another bot and are ignored.

## money

- Answers cost Derpcoins: what the AI providers actually charged plus a margin, rounded up. If nothing reached
  the chat, nothing is charged. If an answer breaks or gets too expensive after part of it (even just a
  reaction) reached the chat, the work so far is charged, and Derp says so. Reply /info to any answer to see its
  model, tokens, cost and who paid (and to report a problem with it).
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
  receipt goes too. The group's pack menu also estimates roughly how long each pack would last at the group's
  recent pace (left out while the group has spent too little to tell). A personal purchase never shows anyone's
  balance in the group.
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
  topping up the group's wallet). In a group each person sees it at most once a UTC day; after that Derp only
  reacts with 🙈. Asking Derp to turn them on only brings up the notice; the person (or in a group an admin)
  presses its button. Free models get read-only and Telegram tools only.
- A request that ran out of Derpcoins isn't lost: when the asker buys Derpcoins shortly after (for themselves, for
  their latest such request wherever they asked; for a group, in the topic they asked in), Derp offers a Continue
  your request button under it, without saying who bought what. Only the asker
  can press it, and only once; it answers the original request, charged like any answer. A purchase by someone else
  never replays another person's request, and an old request just says to ask again. Turning on free models answers
  the waiting request the same way.
- An action that costs more than the approval threshold (or a large share of the payer's balance) shows its price
  with Make it / Cancel buttons first; only the asker decides. Make it finishes the whole request: "draw a frog and
  caption it", "draw it and make it a sticker" or "draw it, then cut it out" still get the caption, sticker or
  cutout. It is charged once the result is in the chat, like any answer. An unanswered prompt expires and loses its
  buttons; cancelling or letting it expire charges nothing for the action (the reply that asked is charged like
  any reply). Once the answer arrives the prompt disappears; if nothing arrives, the prompt says so and nothing is
  charged. If the payer can no longer cover it when Make it is pressed, the prompt says so, with a button to buy
  (in a group, to top up the group).
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
  set, Derp asks once before scheduling at a clock time. In a group anyone can set a missing zone; changing it
  needs an admin. Repeating jobs keep their local time when the zone changes; one-off reminders keep their moment, and
  setting the first zone moves nothing. Day summaries and "today" follow the chat's zone; the free daily
  Derpcoins still refill on the UTC day.
- Anyone can schedule one-offs; repeating jobs in groups need an admin. Creators manage their own jobs and
  admins any: Derp moves, rewords, re-repeats, cancels or resumes them on request ("move that one to tomorrow at
  six"), changing the job in place, and /settings has a page of the chat's reminders (beside the zone) with
  pause, resume and delete buttons. Moving a one-off that already fired brings it back. A scheduled prompt is
  paid by its creator, so only they can make one, reword it or change how often it runs; admins can still move,
  pause or cancel it.
- A reminder arrives with snooze buttons (a few minutes, an hour, tomorrow at the same time) for its creator or
  an admin; others get a short refusal. Snoozing a one-off moves it; snoozing a repeating reminder adds one
  extra reminder and leaves the series as it was (a second snooze moves that extra one, and it
  pauses, resumes and cancels with it).
- A scheduled prompt is paid when it fires, by the usual payer order; after several unpaid firings in a row the
  job pauses, and the note saying so has a Resume button. Removing Derp from a chat pauses its jobs.

## privacy

- Bot commands and inline/guest conversations are never stored. Telemetry holds numbers only, except
  that when an answer fails a shortened copy of that exchange, credentials masked, is kept for debugging.
- /forget as a reply removes that message; /forget alone removes everything the person said in that chat, with
  their reactions, votes, notes and reminders there. Day summaries that mention it are redone without it.
  Its prompt also offers admins (anyone, in a private chat) "forget the whole chat": after a second
  confirmation every stored message (Derp's too), notes, summaries, reactions, polls and membership history
  of the chat go, and the prompt turns into a notice to the chat saying who did it. Rules, reminders, quiz
  points, titles of the day, settings and Derpcoins stay.
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
