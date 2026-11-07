# Dost: design direction (web chat)

**Concept (v2):** a ChatGPT-style workspace: left sidebar (new chat, skills, community, memory, Telegram link), one centred reading column, bot replies as open text with a small avatar, user messages as soft grey pills, a rounded composer. Live work steps ("Searching the web…") appear above the answer. Sidebar becomes a drawer under 860 px. Earlier concept: a quiet, private messaging room for one person and their AI friend. Words come first, in any script: Devanagari, Latin, Arabic, Bengali and more. The design is a calm chat app, not a dashboard and not a landing page.

**One job of the screen:** talk. Skills, memory and Telegram linking sit in a side panel you open when you need them.

## Color (OKLCH-derived, used as semantic tokens)
| Token | Value | Role | Contrast |
|---|---|---|---|
| `--surface` | #eef2f0 (sage-tinted grey) | page background | n/a |
| `--surface-raised` | #ffffff | composer, bot bubbles, panel | n/a |
| `--text` | #16211d | primary text | 15.9:1 on raised |
| `--text-muted` | #55625d | timestamps, hints | 6.1:1 on raised, 5.4:1 on surface |
| `--accent` | #1d6b57 (tulsi green) | send button, user bubbles, current state | white on it: 6.4:1 |
| `--border` | #8a9a93 | input borders | 3.0:1 on raised |
| `--danger` | #a3261c | errors (always with a word) | 7.0:1 on raised |
| `--focus` | #0b5cad | focus ring | 6.3:1 on raised |

## Type
- One family because it is the subject: **Noto Sans + Noto Sans Devanagari** (Google Fonts, 400/600, swap). Hindi and English share the same metrics. Fallback: system-ui.
- Mono: `ui-monospace, "SFMono-Regular", Menlo, monospace` for code blocks the bot writes.
- Scale (1.2 on phones, 1.25 on desktop): 12 / 14 / 16 (body) / 20 / 25 / 31. The only display moment is the empty-state greeting (31 px, weight 400 against 600 labels).
- Body line-height 1.55; bubbles max 68ch.

## Space, radius, elevation
- 4 px base: 4 / 8 / 12 / 16 / 24 / 32 / 48.
- Radius: 18 px bubbles (with a 6 px tail corner), 12 px inputs, 8 px buttons in panels. Panels themselves are square-edged.
- Elevation: just one shadow, on the composer and the slide-over panel. Bubbles are flat.

## Motion
- New messages fade in over 160 ms (opacity only). A "typing" indicator uses three static dots that change opacity in sequence, with no bounce. The panel slides 200 ms ease-out; under reduced motion it only fades.

## Deliberately not doing
- No purple/indigo AI gradient, no sparkle icons, no glowing orb avatar.
- No marketing hero or feature cards; the first screen is the conversation.
- No glass panels; the side panel is a solid raised surface.

**Boldness lives in one place:** the user's own words in solid tulsi-green bubbles. Everything else stays neutral.
