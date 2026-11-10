# Demo video

- The Remotion project behind the README's demo GIF and GitHub's social preview card. Nothing here is imported by the bot, shipped in the image (`.dockerignore`) or run by CI.
- `src/script.ts` is the storyboard: the chat, its timing and the captions. The dialogue is scripted to show features that exist (check `derp/agent/guide.md`); it is not a recording, and the README says so under the GIF. Keep both true when features change.
- `src/Chat.tsx` draws the Telegram-style phone, `src/Demo.tsx` the captions, intro and outro, `src/BotFather.tsx` the description picture set in @BotFather, `src/Root.tsx` the compositions (`Demo` 1920×1080 at 30 fps, `Social` 1280×640, `BotFather` and `BotFatherLoop` 1280×720, rendered with `--scale=0.5` to Telegram's 640×360).
- `public/` holds Derp's avatar (also its Telegram profile photo), the one generated illustration (`party.jpg`) and self-hosted OFL fonts (licenses beside them).
- Install with `pnpm install` (the workspace file sets the supply-chain policy), then `node_modules/.bin/remotion browser ensure`. `pnpm render` writes `out/derp-demo.mp4`, `pnpm still` writes `out/social-preview.png`, `pnpm studio` opens Remotion Studio. `out/` is gitignored.
- To refresh the README assets, run `./make-gif.sh`: it renders both, encodes the GIF and copies them into `.github/assets/`.
