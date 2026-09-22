# Asset licenses — desktop-pet skins

## Live skin: DeepSeek brand whale (v6, current)
- Drawn procedurally by `assets/generate_deepseek_whale.py` — this plugin's own
  code and art. The body ramp references the DeepSeek brand blue (#4D6BFE) as a
  color cue; the character itself is an original drawing with no external
  assets and is not redistributed.

## Retired skin, kept on backup
- **Codex companion**: original OpenAI Codex companion spritesheet slice,
  restored from `assets/_codex_backup/` (official CDN `codex-spritesheet.webp`;
  local backup only, not redistributed).

## Historical skins (deleted per owner request)
- v3 "hamster-wheel-v3": "Mr. Cookies" by LuckyLoops (CC BY 4.0) +
  Aeynit hamster emoticons (CC BY 3.0).
- v4 "cheese-mouse-v1": procedural, this plugin's own work.
- v5 "antea-mouse-v1": white mouse by Antea (stcrbcn), CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/),
  source https://stcrbcn.itch.io/freedogcatmousesprites.

## Hard runtime rules (for any future generator)
- Binary alpha only (0 or 255) — required by the tkinter color-key
  transparency.
- The magic transparent color `#FE00FE` must never appear in output pixels.