// The demo's storyboard: one group chat, seven beats, captions on the left.
// The dialogue is scripted to show real features (see derp/agent/guide.md); it is not a recording.

export type PersonKey = "anya" | "bob" | "max" | "lena" | "dima" | "derp";

export type Person = { name: string; color: string; photo?: string };

export const people: Record<PersonKey, Person> = {
  anya: { name: "Anya", color: "#E17076" },
  bob: { name: "Bob", color: "#7BC862" },
  max: { name: "Max", color: "#65AADD" },
  lena: { name: "Lena", color: "#FAA774" },
  dima: { name: "Дима", color: "#A695E7" },
  derp: { name: "Derp", color: "#F4C150", photo: "derp.jpg" },
};

/** Whose phone we are looking at: their messages sit on the right. */
export const ME: PersonKey = "max";

export type PollOption = { who: PersonKey; votes: number };

export type Msg = {
  id: string;
  at: number;
  from: PersonKey;
  time: string;
  replyTo?: string;
  reaction?: { emoji: string; count: number; at: number };
} & (
  | { kind: "text"; text: string; button?: string }
  | { kind: "image"; src: string; caption?: string }
  | { kind: "quote"; quoted: string }
  | { kind: "poll"; question: string; options: PollOption[]; votesFrom: number; votesTo: number }
);

export type Status = { from: number; to: number; text: string };

export type Caption = { from: number; to: number; kicker: string; title: string; body: string };

export const INTRO = 75;
export const OUTRO_AT = 1250;
export const DURATION = 1390;

export const messages: Msg[] = [
  // 1 · quiet until called
  { id: "m1", at: 85, from: "anya", time: "20:14", kind: "text", text: "pizza or sushi tonight?" },
  { id: "m2", at: 112, from: "bob", time: "20:14", kind: "text", text: "sushi. obviously 🍣" },
  { id: "m3", at: 140, from: "max", time: "20:15", kind: "text", text: "classic derp moment, we never decide" },
  { id: "m4", at: 182, from: "anya", time: "20:15", kind: "text", text: "derp, settle this" },
  {
    id: "m5",
    at: 222,
    from: "derp",
    time: "20:15",
    kind: "text",
    text: "Pizza. Bob has picked sushi three Fridays running and still owes Lena for the eel.",
    reaction: { emoji: "😂", count: 2, at: 262 },
  },

  // 2 · catch-up
  { id: "m6", at: 300, from: "lena", time: "21:02", kind: "text", text: "just woke up 😴 derp what did I miss?" },
  {
    id: "m7",
    at: 345,
    from: "derp",
    time: "21:02",
    kind: "text",
    text:
      "Since your last message:\n" +
      "• Max asked if you're driving on Saturday [↗] — still waiting on you\n" +
      "• Dinner is pizza at 8, Bob is sulking [↗]\n" +
      "• Anya booked the cabin [↗]",
  },

  // 3 · pictures
  { id: "m8", at: 478, from: "max", time: "21:05", kind: "text", text: "derp draw the four of us as a dungeon party" },
  {
    id: "m9",
    at: 540,
    from: "derp",
    time: "21:05",
    kind: "image",
    src: "party.jpg",
    caption: "Bob is the bard. Nobody asked him to be.",
    reaction: { emoji: "🤣", count: 3, at: 598 },
  },

  // 4 · quote stickers
  { id: "m10", at: 655, from: "bob", time: "21:06", replyTo: "m9", kind: "text", text: "excuse me, I'm an incredible bard" },
  { id: "m11", at: 686, from: "anya", time: "21:06", replyTo: "m10", kind: "text", text: "derp quote this" },
  { id: "m12", at: 722, from: "derp", time: "21:06", kind: "quote", quoted: "m10" },

  // 5 · Russian, titles of the day
  { id: "m13", at: 805, from: "dima", time: "21:30", kind: "text", text: "дерп, кто красавчик дня? 👑" },
  {
    id: "m14",
    at: 845,
    from: "derp",
    time: "21:30",
    kind: "text",
    text: "Красавчик дня — Лена 👑\nПроспала весь чат и всё равно в курсе.",
    reaction: { emoji: "🔥", count: 4, at: 890 },
  },

  // 6 · games
  { id: "m15", at: 955, from: "anya", time: "21:41", kind: "text", text: "derp poll: who's late on Saturday" },
  {
    id: "m16",
    at: 992,
    from: "derp",
    time: "21:41",
    kind: "poll",
    question: "Who'll be late on Saturday?",
    options: [
      { who: "bob", votes: 3 },
      { who: "lena", votes: 1 },
      { who: "max", votes: 1 },
      { who: "anya", votes: 0 },
    ],
    votesFrom: 1012,
    votesTo: 1075,
  },

  // 7 · money
  { id: "m17", at: 1108, from: "max", time: "21:42", replyTo: "m16", kind: "text", text: "/info" },
  {
    id: "m18",
    at: 1138,
    from: "derp",
    time: "21:42",
    kind: "text",
    text:
      "🧾 **About this answer**\n" +
      "Model: `deepseek-v4.1-flash`\n" +
      "Tokens: 5,214 in · 38 out · tool calls: 1\n" +
      "Charged: **0.3** dc — from this chat's Derpcoins",
    button: "⚠️ Report a problem",
  },
];

export const statuses: Status[] = [
  { from: 194, to: 222, text: "Derp is typing…" },
  { from: 314, to: 345, text: "Derp is typing…" },
  { from: 494, to: 540, text: "Derp is sending a photo…" },
  { from: 700, to: 722, text: "Derp is choosing a sticker…" },
  { from: 820, to: 845, text: "Дерп печатает…" },
  { from: 970, to: 992, text: "Derp is typing…" },
  { from: 1120, to: 1138, text: "Derp is typing…" },
];

export const captions: Caption[] = [
  {
    from: INTRO,
    to: 290,
    kicker: "In groups",
    title: "Quiet until called.",
    body: "Say its name, @mention it or reply. A passing “classic derp moment” gets nothing.",
  },
  {
    from: 290,
    to: 465,
    kicker: "Catch-up",
    title: "“What did I miss?”",
    body: "A day, a week or since your last message. Questions waiting for you come first, with links.",
  },
  {
    from: 465,
    to: 645,
    kicker: "Pictures",
    title: "Draws your friends.",
    body: "From their profile photos. Then meme it, cut it out or make it a sticker.",
  },
  {
    from: 645,
    to: 795,
    kicker: "Stickers",
    title: "Quotes the chat.",
    body: "One message, or “the funniest moment today”, as a Telegram-looking sticker.",
  },
  {
    from: 795,
    to: 945,
    kicker: "По-русски тоже",
    title: "Speaks the chat's language.",
    body: "English or Russian, slang included. Titles of the day keep a leaderboard.",
  },
  {
    from: 945,
    to: 1098,
    kicker: "Games",
    title: "Runs the group.",
    body: "Polls with members' photos, quizzes with a leaderboard, reminders that repeat.",
  },
  {
    from: 1098,
    to: OUTRO_AT,
    kicker: "Money",
    title: "Honest about money.",
    body: "Free Derpcoins every day, more with Telegram Stars. /info shows what any answer cost.",
  },
];
