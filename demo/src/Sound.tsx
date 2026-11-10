import React from "react";
import { Audio, Sequence, staticFile } from "remotion";
import { ME, messages, OUTRO_AT } from "./script";

// Light UI sounds, synthesised with ffmpeg (public/sfx): a pop per message, a blip per reaction,
// a chime on the intro and outro. No music, so the demo still works muted (README GIF, autoplay).
const cue = (at: number, file: string, volume: number, key: string) => (
  <Sequence key={key} from={at} durationInFrames={60} layout="none">
    <Audio src={staticFile(`sfx/${file}`)} volume={volume} />
  </Sequence>
);

export const Sound: React.FC = () => (
  <>
    {cue(2, "chime.wav", 0.9, "intro")}
    {messages.map((m) =>
      cue(m.at, m.from === "derp" ? "derp.wav" : m.from === ME ? "out.wav" : "in.wav", 1, m.id),
    )}
    {messages.flatMap((m) => (m.reaction ? [cue(m.reaction.at, "react.wav", 0.9, `${m.id}-r`)] : []))}
    {cue(OUTRO_AT + 10, "chime.wav", 0.9, "outro")}
  </>
);
