import React from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ME, type Msg, type PersonKey, messages, people, statuses } from "./script";
import { font, tg } from "./theme";

const PHONE_W = 640;
const PHONE_H = 1000;
const BASE = 25; // body text size inside the phone

const byId = Object.fromEntries(messages.map((m) => [m.id, m]));

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

// ---------------------------------------------------------------- small parts

export const Avatar: React.FC<{ who: PersonKey; size: number }> = ({ who, size }) => {
  const p = people[who];
  if (p.photo) {
    return (
      <Img
        src={staticFile(p.photo)}
        style={{ width: size, height: size, borderRadius: size / 2, objectFit: "cover", flexShrink: 0 }}
      />
    );
  }
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: size / 2,
        flexShrink: 0,
        background: `linear-gradient(160deg, ${p.color}, ${shade(p.color, -0.25)})`,
        color: "white",
        fontWeight: 600,
        fontSize: size * 0.44,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {p.name[0]}
    </div>
  );
};

function shade(hex: string, amount: number): string {
  const n = parseInt(hex.slice(1), 16);
  const f = (c: number) => Math.max(0, Math.min(255, Math.round(c + c * amount)));
  const r = f(n >> 16);
  const g = f((n >> 8) & 255);
  const b = f(n & 255);
  return `rgb(${r}, ${g}, ${b})`;
}

/** **bold**, `code`, [link] and newlines: enough of Telegram's formatting for the script. */
const Rich: React.FC<{ text: string }> = ({ text }) => {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]|\/info)/g);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith("**")) return <b key={i}>{part.slice(2, -2)}</b>;
        if (part.startsWith("`"))
          return (
            <span key={i} style={{ fontFamily: font.mono, fontSize: BASE * 0.82, color: "#C9D6E3" }}>
              {part.slice(1, -1)}
            </span>
          );
        if (part.startsWith("["))
          return (
            <span key={i} style={{ color: tg.link }}>
              {part.slice(1, -1)}
            </span>
          );
        if (part === "/info")
          return (
            <span key={i} style={{ color: tg.link }}>
              {part}
            </span>
          );
        return <React.Fragment key={i}>{part}</React.Fragment>;
      })}
    </>
  );
};

const Time: React.FC<{ time: string; out: boolean; overlay?: boolean }> = ({ time, out, overlay }) => (
  <span
    style={{
      float: overlay ? undefined : "right",
      marginLeft: 12,
      marginTop: overlay ? 0 : BASE * 0.45,
      marginBottom: -4,
      fontSize: BASE * 0.6,
      color: overlay ? "white" : out ? tg.outMuted : tg.muted,
      background: overlay ? "rgba(0,0,0,0.45)" : undefined,
      padding: overlay ? "2px 8px" : undefined,
      borderRadius: overlay ? 10 : undefined,
      whiteSpace: "nowrap",
    }}
  >
    {time}
    {out ? " ✓✓" : ""}
  </span>
);

const ReplyBar: React.FC<{ id: string; out: boolean }> = ({ id, out }) => {
  const m = byId[id];
  const who = people[m.from];
  const snippet =
    m.kind === "text" ? m.text : m.kind === "image" ? `🖼 ${m.caption ?? "Photo"}` : m.kind === "poll" ? `📊 ${m.question}` : "Sticker";
  const color = out ? "#9FCBF5" : who.color;
  return (
    <div
      style={{
        borderLeft: `4px solid ${color}`,
        background: out ? "rgba(255,255,255,0.08)" : `${who.color}1F`,
        borderRadius: 6,
        padding: "4px 10px",
        marginBottom: 6,
        fontSize: BASE * 0.78,
        lineHeight: 1.25,
        maxWidth: 420,
      }}
    >
      <div style={{ color, fontWeight: 600 }}>{who.name}</div>
      <div style={{ color: "#C8D1DA", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {snippet}
      </div>
    </div>
  );
};

const Reaction: React.FC<{ m: Msg; out: boolean }> = ({ m, out }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!m.reaction || frame < m.reaction.at) return null;
  const s = spring({ frame: frame - m.reaction.at, fps, config: { damping: 9, stiffness: 180 } });
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        marginTop: 8,
        padding: "3px 10px 3px 7px",
        borderRadius: 16,
        background: out ? "rgba(255,255,255,0.14)" : "rgba(94,181,247,0.16)",
        color: out ? "white" : tg.accent,
        fontSize: BASE * 0.72,
        fontWeight: 600,
        transform: `scale(${s})`,
        transformOrigin: "left center",
      }}
    >
      <span style={{ fontSize: BASE * 0.85 }}>{m.reaction.emoji}</span>
      {m.reaction.count}
    </div>
  );
};

// ---------------------------------------------------------------- message kinds

const Poll: React.FC<{ m: Extract<Msg, { kind: "poll" }> }> = ({ m }) => {
  const frame = useCurrentFrame();
  const total = m.options.reduce((a, o) => a + o.votes, 0);
  const t = interpolate(frame, [m.votesFrom, m.votesTo], [0, 1], clamp);
  const counted = Math.round(total * t);
  return (
    <div style={{ width: 430 }}>
      <div style={{ fontWeight: 700, fontSize: BASE, marginBottom: 2 }}>{m.question}</div>
      <div style={{ color: tg.muted, fontSize: BASE * 0.68, marginBottom: 12 }}>Public poll</div>
      {m.options.map((o, i) => {
        // Votes arrive one option after another, each easing in.
        const share = total ? o.votes / total : 0;
        const local = interpolate(frame, [m.votesFrom + i * 8, m.votesTo + i * 8], [0, 1], clamp);
        const eased = 1 - Math.pow(1 - local, 3);
        const pct = Math.round(share * 100 * eased);
        return (
          <div key={o.who} style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: BASE * 0.88 }}>
              <span style={{ width: 54, fontWeight: 700, textAlign: "right", opacity: t > 0 ? 1 : 0 }}>{pct}%</span>
              <Avatar who={o.who} size={30} />
              <span>{people[o.who].name}</span>
            </div>
            <div style={{ marginLeft: 64, marginTop: 6, height: 6, borderRadius: 3, background: "rgba(255,255,255,0.06)" }}>
              <div
                style={{
                  width: `${Math.max(share * 100 * eased, t > 0 ? 2 : 0)}%`,
                  height: 6,
                  borderRadius: 3,
                  background: tg.accent,
                }}
              />
            </div>
          </div>
        );
      })}
      <div style={{ color: tg.muted, fontSize: BASE * 0.68, marginTop: 2 }}>
        {counted} {counted === 1 ? "vote" : "votes"}
      </div>
    </div>
  );
};

const QuoteSticker: React.FC<{ m: Extract<Msg, { kind: "quote" }> }> = ({ m }) => {
  const q = byId[m.quoted];
  const who = people[q.from];
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 10, padding: 6 }}>
      <Avatar who={q.from} size={52} />
      <div
        style={{
          background: "#1E2C3A",
          borderRadius: "22px 22px 22px 6px",
          padding: "12px 18px",
          boxShadow: "0 6px 24px rgba(0,0,0,0.35)",
          maxWidth: 360,
        }}
      >
        <div style={{ color: who.color, fontWeight: 700, fontSize: BASE * 0.92 }}>{who.name}</div>
        <div style={{ fontSize: BASE * 1.05, lineHeight: 1.25, color: tg.text }}>{q.kind === "text" ? q.text : ""}</div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------- bubble

const Bubble: React.FC<{ m: Msg; first: boolean; last: boolean }> = ({ m, first, last }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const out = m.from === ME;
  const enter = spring({ frame: frame - m.at, fps, config: { damping: 18, stiffness: 160 } });
  const grow = interpolate(frame - m.at, [0, 7], [0, 1], { ...clamp });
  const showName = first && !out && m.kind !== "quote";
  const tail = last ? (out ? "20px 20px 6px 20px" : "20px 20px 20px 6px") : "20px";

  let body: React.ReactNode;
  let bare = false;
  if (m.kind === "image") {
    const sharp = interpolate(frame - m.at, [4, 22], [0, 1], clamp);
    body = (
      <div style={{ width: 440 }}>
        <div style={{ position: "relative", overflow: "hidden", borderRadius: "16px 16px 4px 4px", margin: "-6px -12px 0" }}>
          <Img
            src={staticFile(m.src)}
            style={{
              width: 464,
              height: 464,
              objectFit: "cover",
              display: "block",
              filter: `blur(${(1 - sharp) * 18}px)`,
              transform: `scale(${1.06 - sharp * 0.06})`,
            }}
          />
        </div>
        {m.caption && (
          <div style={{ marginTop: 8 }}>
            <Rich text={m.caption} />
            <Time time={m.time} out={out} />
          </div>
        )}
      </div>
    );
  } else if (m.kind === "quote") {
    bare = true;
    const pop = spring({ frame: frame - m.at, fps, config: { damping: 10, stiffness: 140 } });
    body = (
      <div style={{ transform: `scale(${pop}) rotate(${(1 - pop) * -8}deg)`, transformOrigin: "left bottom" }}>
        <QuoteSticker m={m} />
        <div style={{ marginLeft: 70 }}>
          <Time time={m.time} out={false} overlay />
        </div>
      </div>
    );
  } else if (m.kind === "poll") {
    body = (
      <>
        <Poll m={m} />
        <Time time={m.time} out={out} />
      </>
    );
  } else {
    body = (
      <>
        <Rich text={m.text} />
        <Time time={m.time} out={out} />
      </>
    );
  }

  return (
    <div style={{ display: "grid", gridTemplateRows: `${grow}fr` }}>
      <div style={{ minHeight: 0, overflow: "visible" }}>
        <div
          style={{
            display: "flex",
            justifyContent: out ? "flex-end" : "flex-start",
            alignItems: "flex-end",
            gap: 10,
            marginTop: first ? 14 : 4,
            opacity: enter,
            transform: `translateY(${(1 - enter) * 26}px) scale(${0.94 + enter * 0.06})`,
            transformOrigin: out ? "right bottom" : "left bottom",
          }}
        >
          {!out && (last ? <Avatar who={m.from} size={42} /> : <div style={{ width: 42, flexShrink: 0 }} />)}
          <div style={{ maxWidth: m.kind === "image" ? undefined : 480 }}>
            <div
              style={
                bare
                  ? {}
                  : {
                      background: out ? tg.outgoing : tg.incoming,
                      color: tg.text,
                      borderRadius: tail,
                      padding: "8px 14px 9px",
                      fontSize: BASE,
                      lineHeight: 1.3,
                      whiteSpace: "pre-wrap",
                      overflow: m.kind === "image" ? "hidden" : undefined,
                    }
              }
            >
              {showName && (
                <div style={{ color: people[m.from].color, fontWeight: 600, fontSize: BASE * 0.86, marginBottom: 2 }}>
                  {people[m.from].name}
                </div>
              )}
              {m.replyTo && <ReplyBar id={m.replyTo} out={out} />}
              {body}
              {!bare && <Reaction m={m} out={out} />}
            </div>
            {m.kind === "text" && m.button && (
              <div
                style={{
                  marginTop: 6,
                  background: tg.button,
                  borderRadius: 14,
                  color: "white",
                  textAlign: "center",
                  padding: "10px 0",
                  fontSize: BASE * 0.84,
                  fontWeight: 600,
                }}
              >
                {m.button}
              </div>
            )}
            {bare && <Reaction m={m} out={out} />}
          </div>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------- phone

const StatusBar: React.FC = () => (
  <div
    style={{
      height: 50,
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "6px 40px 0",
      color: "white",
      fontWeight: 600,
      fontSize: 22,
      background: tg.header,
    }}
  >
    <span>21:42</span>
    <span style={{ letterSpacing: 4, fontSize: 18 }}>▂▄▆█ ▮</span>
  </div>
);

const Header: React.FC = () => {
  const frame = useCurrentFrame();
  const status = statuses.find((s) => frame >= s.from && frame < s.to);
  return (
    <div
      style={{
        height: 86,
        background: tg.header,
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 22px",
        borderBottom: "1px solid rgba(0,0,0,0.4)",
      }}
    >
      <span style={{ color: tg.accent, fontSize: 40, lineHeight: 1, marginTop: -6 }}>‹</span>
      <div
        style={{
          width: 54,
          height: 54,
          borderRadius: 27,
          background: "linear-gradient(160deg, #F7B267, #E07A3F)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 28,
        }}
      >
        🏕
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ color: "white", fontWeight: 700, fontSize: 25 }}>Weekend crew</div>
        <div style={{ color: status ? tg.accent : tg.muted, fontSize: 20 }}>{status ? status.text : "6 members"}</div>
      </div>
      <span style={{ color: tg.muted, fontSize: 30 }}>⋮</span>
    </div>
  );
};

const InputBar: React.FC = () => (
  <div
    style={{
      height: 84,
      background: tg.header,
      display: "flex",
      alignItems: "center",
      gap: 16,
      padding: "0 24px",
      color: tg.muted,
      fontSize: 24,
    }}
  >
    <span style={{ fontSize: 30 }}>📎</span>
    <span style={{ flex: 1 }}>Message</span>
    <span style={{ fontSize: 28 }}>🎙</span>
  </div>
);

export const Phone: React.FC = () => {
  const frame = useCurrentFrame();
  const visible = messages.filter((m) => frame >= m.at);
  return (
    <div
      style={{
        width: PHONE_W,
        height: PHONE_H,
        borderRadius: 64,
        padding: 14,
        background: "#0B0F14",
        boxShadow: "0 40px 90px rgba(80, 50, 0, 0.45), 0 0 0 3px #2A2F36 inset",
      }}
    >
      <div
        style={{
          width: "100%",
          height: "100%",
          borderRadius: 52,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          fontFamily: font.sans,
          background: tg.bg,
        }}
      >
        <StatusBar />
        <Header />
        <div
          style={{
            flex: 1,
            position: "relative",
            overflow: "hidden",
            background:
              "radial-gradient(circle at 20% 15%, rgba(94,181,247,0.07), transparent 45%), radial-gradient(circle at 85% 80%, rgba(244,193,80,0.06), transparent 50%), #0E1621",
          }}
        >
          <div
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              bottom: 0,
              padding: "0 16px 16px",
              display: "flex",
              flexDirection: "column",
            }}
          >
            {visible.map((m, i) => {
              const prev = visible[i - 1];
              const next = visible[i + 1];
              const first = !prev || prev.from !== m.from;
              const last = !next || next.from !== m.from;
              return <Bubble key={m.id} m={m} first={first} last={last} />;
            })}
          </div>
        </div>
        <InputBar />
      </div>
    </div>
  );
};
