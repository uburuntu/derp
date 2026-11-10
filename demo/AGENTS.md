# Demo video

- The Remotion project behind the README's demo video and GitHub's social preview card. Nothing here is imported by the bot, shipped in the image (`.dockerignore`) or run by CI.
- `src/script.ts` is the storyboard: the chat, its timing and the captions. The dialogue is scripted to show features that exist (check `derp/agent/guide.md`); it is not a recording, and the README says so under the video. Keep both true when features change.
- `src/Chat.tsx` draws the Telegram-style phone, `src/Demo.tsx` the captions, intro and outro, `src/BotFather.tsx` the description picture set in @BotFather, `src/Root.tsx` the compositions (`Demo` 1920×1080 at 30 fps, `Social` 1280×640, `BotFather` and `BotFatherLoop` 1280×720: the still renders with `--scale=0.5` to Telegram's 640×360, the loop with `--scale=0.75` to 960×540, the largest animation BotFather takes).
- `src/Sound.tsx` places light UI sounds (`public/sfx/`, synthesised with ffmpeg `aevalsrc`, no music): a pop per message (incoming, sent, Derp's), a blip per reaction, a chime on the intro and outro. The mp4 carries them.
- `public/` holds Derp's avatar (also its Telegram profile photo), the one generated illustration (`party.jpg`) and self-hosted OFL fonts (licenses beside them).
- Install with `pnpm install` (the workspace file sets the supply-chain policy), then `node_modules/.bin/remotion browser ensure`. `pnpm render` writes `out/derp-demo.mp4`, `pnpm still` writes `out/social-preview.png`, `pnpm studio` opens Remotion Studio. `out/` is gitignored.
- To refresh the README assets, run `./render.sh` (the video and the card). The README plays the video from a GitHub attachment, which only the github.com editor can create: drag `out/derp-demo.mp4` in there and swap the URL in `README.md`.
