import React from "react";
import { AbsoluteFill, Easing, Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { Phone } from "./Chat";
import { DURATION, INTRO, OUTRO_AT, captions } from "./script";
import { brand, font } from "./theme";

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

export const Backdrop: React.FC<{ scale?: number }> = ({ scale = 1 }) => (
  <AbsoluteFill
    style={{
      background: `radial-gradient(circle at 70% 45%, ${brand.yellowLight} 0%, ${brand.yellow} 55%, ${brand.yellowDeep} 120%)`,
    }}
  >
    {/* The avatar's big "D", echoed behind everything. */}
    <div
      style={{
        position: "absolute",
        right: -60 * scale,
        top: -170 * scale,
        fontFamily: font.sans,
        fontWeight: 700,
        fontSize: 1500 * scale,
        lineHeight: 1,
        color: "rgba(214, 145, 25, 0.16)",
      }}
    >
      D
    </div>
  </AbsoluteFill>
);

const Logo: React.FC<{ size: number }> = ({ size }) => (
  <div style={{ display: "flex", alignItems: "center", gap: size * 0.32 }}>
    <Img
      src={staticFile("derp.jpg")}
      style={{ width: size, height: size, borderRadius: size * 0.28, boxShadow: "0 8px 24px rgba(90,60,0,0.25)" }}
    />
    <span style={{ fontFamily: font.sans, fontWeight: 700, fontSize: size * 0.62, color: brand.ink, letterSpacing: -1 }}>
      Derp
    </span>
  </div>
);

const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pop = spring({ frame, fps, config: { damping: 11, stiffness: 120 } });
  const line = interpolate(frame, [14, 30], [0, 1], clamp);
  const leave = interpolate(frame, [INTRO - 18, INTRO], [0, 1], { ...clamp, easing: Easing.in(Easing.cubic) });
  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        opacity: 1 - leave,
        transform: `translateY(${-leave * 40}px)`,
      }}
    >
      <div style={{ transform: `scale(${0.6 + pop * 0.4})`, opacity: pop }}>
        <Logo size={190} />
      </div>
      <div
        style={{
          marginTop: 40,
          fontFamily: font.sans,
          fontSize: 50,
          color: brand.inkSoft,
          opacity: line,
          transform: `translateY(${(1 - line) * 16}px)`,
        }}
      >
        The sharp friend already in your Telegram chat.
      </div>
    </AbsoluteFill>
  );
};

const CaptionBlock: React.FC = () => {
  const frame = useCurrentFrame();
  const index = captions.findIndex((c) => frame >= c.from && frame < c.to);
  if (index < 0) return null;
  const c = captions[index];
  const inT = interpolate(frame, [c.from, c.from + 14], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) });
  const outT = interpolate(frame, [c.to - 10, c.to], [0, 1], { ...clamp, easing: Easing.in(Easing.cubic) });
  const kickerT = interpolate(frame, [c.from + 4, c.from + 18], [0, 1], clamp);
  const bodyT = interpolate(frame, [c.from + 8, c.from + 24], [0, 1], clamp);
  return (
    <div
      style={{
        position: "absolute",
        left: 150,
        top: 0,
        bottom: 0,
        width: 860,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        fontFamily: font.sans,
        color: brand.ink,
        opacity: 1 - outT,
        transform: `translateY(${-outT * 30}px)`,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          fontSize: 30,
          fontWeight: 700,
          letterSpacing: 3,
          textTransform: "uppercase",
          color: "#8A5A00",
          opacity: kickerT,
        }}
      >
        <span style={{ fontFamily: font.mono, letterSpacing: 0, fontSize: 26 }}>
          {String(index + 1).padStart(2, "0")}/{String(captions.length).padStart(2, "0")}
        </span>
        <span style={{ width: 40, height: 3, background: "#8A5A00", borderRadius: 2 }} />
        {c.kicker}
      </div>
      <div
        style={{
          marginTop: 22,
          fontSize: 104,
          fontWeight: 700,
          lineHeight: 1.0,
          letterSpacing: -2.5,
          opacity: inT,
          transform: `translateY(${(1 - inT) * 40}px)`,
        }}
      >
        {c.title}
      </div>
      <div
        style={{
          marginTop: 30,
          fontSize: 40,
          lineHeight: 1.3,
          color: brand.inkSoft,
          maxWidth: 780,
          opacity: bodyT,
          transform: `translateY(${(1 - bodyT) * 24}px)`,
        }}
      >
        {c.body}
      </div>
    </div>
  );
};

const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - OUTRO_AT - 10;
  if (local < -10) return null;
  const pop = spring({ frame: local, fps, config: { damping: 12, stiffness: 110 } });
  const line = interpolate(local, [12, 28], [0, 1], clamp);
  const foot = interpolate(local, [24, 40], [0, 1], clamp);
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: font.sans, color: brand.ink }}>
      <div style={{ transform: `scale(${0.7 + pop * 0.3})`, opacity: pop, display: "flex", flexDirection: "column", alignItems: "center" }}>
        <Img src={staticFile("derp.jpg")} style={{ width: 210, height: 210, borderRadius: 58, boxShadow: "0 20px 50px rgba(90,60,0,0.3)" }} />
        <div style={{ marginTop: 38, fontSize: 120, fontWeight: 700, letterSpacing: -3 }}>@DerpRobot</div>
      </div>
      <div style={{ marginTop: 6, fontSize: 46, color: brand.inkSoft, opacity: line }}>
        Add it to your group. Then just say “derp”.
      </div>
      <div
        style={{
          position: "absolute",
          bottom: 80,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 10,
          opacity: foot,
        }}
      >
        <span style={{ fontSize: 32, color: brand.inkSoft }}>Open source under AGPL-3.0</span>
        <span style={{ fontFamily: font.mono, fontSize: 30, color: brand.ink }}>github.com/uburuntu/derp</span>
      </div>
    </AbsoluteFill>
  );
};

export const Demo: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const phoneIn = spring({ frame: frame - (INTRO - 12), fps, config: { damping: 16, stiffness: 90 } });
  const phoneOut = interpolate(frame, [OUTRO_AT - 6, OUTRO_AT + 14], [0, 1], { ...clamp, easing: Easing.in(Easing.cubic) });
  const logoT = interpolate(frame, [INTRO - 4, INTRO + 12], [0, 1], clamp) * (1 - phoneOut);
  const fadeOut = interpolate(frame, [DURATION - 12, DURATION], [0, 1], clamp);
  return (
    <AbsoluteFill style={{ background: brand.yellow }}>
      <Backdrop />
      {frame < INTRO + 2 && <Intro />}
      <div style={{ position: "absolute", left: 150, top: 70, opacity: logoT }}>
        <Logo size={64} />
      </div>
      <CaptionBlock />
      <div
        style={{
          position: "absolute",
          right: 170,
          top: 40,
          transform: `translateX(${(1 - phoneIn) * 900 + phoneOut * 900}px) rotate(${(1 - phoneIn) * 6}deg)`,
        }}
      >
        <Phone />
      </div>
      <Outro />
      <AbsoluteFill style={{ background: brand.yellow, opacity: fadeOut }} />
    </AbsoluteFill>
  );
};
