import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { Backdrop } from "./Demo";
import { brand, font, tg } from "./theme";

// The picture above "What can this bot do?" in a new chat with Derp, set in @BotFather
// (Edit Bot, Edit Description Picture). Telegram wants 640×360: these render at 1280×720 with --scale=0.5.

export const ASKS = ["derp, settle this", "derp, what did I miss?", "дерп, кто красавчик дня?", "derp, draw us as a dungeon party"];
export const ASK_FRAMES = 48;

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

const Ask: React.FC<{ text: string; local: number; still: boolean }> = ({ text, local, still }) => {
  // Types in, holds, fades out, so the loop's first and last frames match (an empty slot).
  const typed = still ? text.length : Math.round(interpolate(local, [2, 22], [0, text.length], clamp));
  const shown = still ? 1 : interpolate(local, [0, 4, ASK_FRAMES - 6, ASK_FRAMES], [0, 1, 1, 0], clamp);
  const lift = still ? 0 : interpolate(local, [0, 6], [14, 0], clamp);
  return (
    <div
      style={{
        alignSelf: "flex-start",
        background: tg.outgoing,
        color: tg.text,
        fontSize: 40,
        padding: "14px 26px",
        borderRadius: "28px 28px 28px 8px",
        boxShadow: "0 12px 30px rgba(60, 40, 0, 0.25)",
        opacity: shown,
        transform: `translateY(${lift}px)`,
        whiteSpace: "nowrap",
      }}
    >
      {text.slice(0, typed)}
      {!still && typed < text.length && <span style={{ opacity: 0.6 }}>|</span>}
    </div>
  );
};

const Card: React.FC<{ still: boolean }> = ({ still }) => {
  const frame = useCurrentFrame();
  const index = Math.floor(frame / ASK_FRAMES) % ASKS.length;
  const local = frame % ASK_FRAMES;
  return (
    <AbsoluteFill style={{ fontFamily: font.sans, color: brand.ink }}>
      <Backdrop scale={0.66} />
      <div style={{ position: "absolute", left: 90, top: 0, bottom: 0, display: "flex", alignItems: "center", gap: 60 }}>
        <Img
          src={staticFile("derp.jpg")}
          style={{ width: 330, height: 330, borderRadius: 90, boxShadow: "0 24px 60px rgba(90,60,0,0.3)" }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
          <div style={{ fontSize: 120, fontWeight: 700, letterSpacing: -4, lineHeight: 0.9 }}>Derp</div>
          <div style={{ fontSize: 40, lineHeight: 1.2, color: brand.inkSoft }}>
            One of the group.
            <br />
            Remembers everything, roasts accordingly.
          </div>
          <div style={{ height: 82, display: "flex" }}>
            <Ask text={ASKS[still ? 1 : index]} local={local} still={still} />
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const BotFatherStill: React.FC = () => <Card still />;
export const BotFatherLoop: React.FC = () => <Card still={false} />;
