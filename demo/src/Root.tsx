import React from "react";
import { Composition, Img, Still, staticFile } from "remotion";
import { Backdrop, Demo } from "./Demo";
import { DURATION } from "./script";
import { brand, font, FPS } from "./theme";

/** GitHub's social preview card (1280×640). */
const Social: React.FC = () => (
  <div style={{ width: "100%", height: "100%", position: "relative", fontFamily: font.sans, color: brand.ink }}>
    <Backdrop scale={0.62} />
    <div style={{ position: "absolute", left: 90, top: 0, bottom: 0, display: "flex", alignItems: "center", gap: 56 }}>
      <Img src={staticFile("derp.jpg")} style={{ width: 300, height: 300, borderRadius: 82, boxShadow: "0 24px 60px rgba(90,60,0,0.3)" }} />
      <div>
        <div style={{ fontSize: 132, fontWeight: 700, letterSpacing: -4, lineHeight: 1 }}>Derp</div>
        <div style={{ marginTop: 18, fontSize: 44, lineHeight: 1.2, color: brand.inkSoft, maxWidth: 760 }}>
          The sharp friend already in your Telegram chat.
        </div>
        <div style={{ marginTop: 34, display: "flex", gap: 44, fontSize: 30, color: brand.inkSoft, fontFamily: font.mono }}>
          <span>t.me/DerpRobot</span>
          <span>github.com/uburuntu/derp</span>
        </div>
      </div>
    </div>
  </div>
);

export const Root: React.FC = () => (
  <>
    <Composition id="Demo" component={Demo} durationInFrames={DURATION} fps={FPS} width={1920} height={1080} />
    <Still id="Social" component={Social} width={1280} height={640} />
  </>
);
