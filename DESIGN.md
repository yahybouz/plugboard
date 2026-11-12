# Plugboard dashboard design system

The admin dashboard (`static/index.html`), the client portal (`static/portal.html`) and the
ticket board (`static/suivi.html`) share one stylesheet, `static/style.css`. Its `:root` block
holds every value the pages use: colors, type, spacing, radii, shadows. This file documents
those tokens and the rules around them.

## Principles

- **One source.** A size, a spacing, a radius or a color is a token from `style.css`, never a
  raw value in a rule, an HTML `style` attribute or a script.
- **Two themes.** Ink (dark, the default) and paper (light). `theme.js` applies the saved
  choice or follows the system setting. A new rule uses color tokens only, so it works in both.
- **Calm and readable.** Color carries meaning (status, accent), not decoration. Every text and
  background pair meets WCAG AA (4.5:1).
- **No behavior in the design.** Styling never changes what a control does; scripts toggle
  visibility, they do not style.

The tokens live between `/* design-tokens: start */` and `/* design-tokens: end */` in
`style.css`. `tests/test_dashboard_design_tokens.py` reads them and fails when a raw value, an
undefined variable or an undocumented token appears.

## Colors

The palette is "ink and champagne": warm ivory text, a champagne-gold accent and desaturated
status colors. Dark and light values:

| Token | Dark (ink) | Light (paper) | Use |
|---|---|---|---|
| `--bg` | `#0b0d12` | `#f6f3ec` | Page background |
| `--bg-secondary` | `#151922` | `#ece7dc` | Table heads, code, secondary panels |
| `--bg-card` | `#11141b` | `#fffdf8` | Cards, dialogs, tables |
| `--bg-glass` | `rgba(11,13,18,0.78)` | `rgba(246,243,236,0.82)` | Sticky top bar (with blur) |
| `--bg-sidebar` | `#0d1016` | `#fbf9f3` | Sidebar |
| `--bg-hover` | `#1a1f2a` | `#efe9dd` | Hovered rows and links |
| `--bg-active` | `#1d1a14` | `#f3ead6` | Current navigation item |
| `--bg-input` | `#0e1117` | `#fffdf8` | Form fields |
| `--code-bg` | `#07090d` | `#1c1a16` | Command blocks (dark in both themes) |
| `--code-text` | `#e6dfcf` | `#efe7d3` | Text on `--code-bg` |
| `--text` | `#ece6d8` | `#1b1a17` | Main text |
| `--text-secondary` | `#c2bbab` | `#45413a` | Table cells, labels |
| `--text-muted` | `#9a9384` | `#6b665c` | Hints, metadata |
| `--accent` | `#cdb07a` | `#8a6a2f` | Primary buttons, links, focus |
| `--accent-light` | `#e2c992` | `#6f5425` | Primary button hover |
| `--accent-dark` | `#b3955e` | `#6f5425` | Link hover, external links |
| `--accent-bg` | `#221d14` | `#f3ead6` | Accent badges |
| `--accent-border` | `rgba(205,176,122,0.32)` | `rgba(138,106,47,0.3)` | Accent outlines |
| `--accent-glow` | `rgba(205,176,122,0.14)` | `rgba(138,106,47,0.1)` | Reserved: soft accent fills |
| `--on-accent` | `#14110b` | `#fffdf8` | Text on `--accent` |
| `--sapphire` | `#7f9bd6` | `#3d5a99` | Second chart series (client portal) |
| `--green` | `#7cc29a` | `#2f7a52` | Success, online |
| `--green-bg` | `rgba(124,194,154,0.12)` | `#e6f2ea` | Success background |
| `--green-border` | `rgba(124,194,154,0.3)` | `rgba(47,122,82,0.25)` | Success outline |
| `--red` | `#e8907f` | `#a8402f` | Errors, destructive actions |
| `--red-bg` | `rgba(232,144,127,0.12)` | `#f8e7e3` | Error background |
| `--red-border` | `rgba(232,144,127,0.32)` | `rgba(168,64,47,0.25)` | Error outline |
| `--orange` | `#dcb15d` | `#9a6b14` | Warning fills |
| `--orange-bg` | `rgba(220,177,93,0.12)` | `#f7eedb` | Warning background |
| `--orange-border` | `rgba(220,177,93,0.32)` | `rgba(154,107,20,0.25)` | Warning outline |
| `--orange-text` | `#e3bd72` | `#7c5510` | Warning text |
| `--purple` | `#b4a2e0` | `#5e4a9e` | Reserved: further chart series |
| `--purple-bg` | `rgba(180,162,224,0.12)` | `#eeeaf7` | Reserved: further chart series |
| `--teal` | `#7cc2c2` | `#2c7373` | Reserved: further chart series |
| `--border` | `rgba(236,230,216,0.08)` | `rgba(27,26,23,0.09)` | Hairlines, chart grid |
| `--border-strong` | `rgba(236,230,216,0.16)` | `rgba(27,26,23,0.18)` | Field and button outlines |
| `--overlay` | `rgba(5,6,9,0.6)` | same | Behind dialogs |
| `--logo-bg` | radial gradient, `#2a241a` to `#120f0a` | same | Logo tile |
| `--inset-highlight` | `rgba(255,255,255,0.18)` | same | Top edge of primary buttons |

Shadows: `--shadow-sm` (cards, tables), `--shadow` (raised tiles), `--shadow-lg` (dialogs,
toasts, open mobile menu), `--shadow-glow` (focused fields). `--hairline` is the gold rule on
top of stat cards. Each has a dark and a light value.

## Type

Families: `--font` (Inter, the interface), `--display` (Instrument Serif, page titles and the
brand), `--mono` (JetBrains Mono, code, keys, figures in code). `html` sets `--fs-root`
(15px), so `1rem` is 15px.

| Token | Value | Size | Use |
|---|---|---|---|
| `--fs-label` | `0.72rem` | 10.8px | Uppercase labels: navigation sections, table heads, stat labels, badges |
| `--fs-xs` | `0.78rem` | 11.7px | Hints, metadata, code, small buttons |
| `--fs-sm` | `0.875rem` | 13.1px | Buttons, fields, tables, menus, alerts |
| `--fs-md` | `1rem` | 15px | Body text, card titles, dialog text |
| `--fs-lg` | `1.3rem` | 19.5px | Sub-headings, the brand name |
| `--fs-xl` | `1.75rem` | 26px | Page titles (serif) |
| `--fs-2xl` | `2.1rem` | 31.5px | Key figures, the sign-in title |
| `--fs-icon` | `2.5rem` | 37.5px | Empty-state icons |

Weights: `--fw-regular` (400), `--fw-medium` (500), `--fw-semibold` (600, buttons, titles,
labels), `--fw-bold` (700, sparingly). Inter is loaded in these four weights only.

## Spacing

Margins, gaps and paddings use a quarter-rem scale:

| Token | Value | Token | Value |
|---|---|---|---|
| `--space-1` | `0.25rem` (3.75px) | `--space-6` | `1.5rem` (22.5px) |
| `--space-2` | `0.5rem` (7.5px) | `--space-8` | `2rem` (30px) |
| `--space-3` | `0.75rem` (11.25px) | `--space-10` | `2.5rem` (37.5px) |
| `--space-4` | `1rem` (15px) | `--space-12` | `3rem` (45px) |
| `--space-5` | `1.25rem` (18.75px) | | |

The inner padding of controls keeps exact values, so a button or a field never changes height:

| Token | Value | Use |
|---|---|---|
| `--pad-control` | `0.45rem 0.9rem` | `.btn` |
| `--pad-control-sm` | `0.3rem 0.65rem` | `.btn-sm` |
| `--pad-input` | `0.5rem 0.8rem` | Text fields, selects, text areas |
| `--pad-badge` | `0.2rem 0.6rem` | `.badge`, counters |
| `--pad-chip` | `0.15rem 0.45rem` | Inline code in tables |
| `--pad-cell` | `0.75rem 1rem` | Table cells and heads |

Layout: `--sidebar-w` (256px, 0 under 768px) offsets the main column.

## Radii and motion

| Token | Value | Use |
|---|---|---|
| `--radius-xs` | `4px` | Progress bars, command blocks |
| `--radius-sm` | `6px` | Small buttons, inline code |
| `--radius-control` | `8px` | Buttons, fields, navigation items |
| `--radius` | `10px` | Alerts, toasts, inner panels |
| `--radius-lg` | `14px` | Cards, tables, chat bubbles |
| `--radius-xl` | `18px` | Dialogs, the live view |
| `--radius-pill` | `999px` | Badges, counters, the in-flight pill |

`2px`, `3px` and `50%` stay literal: they draw shapes (an indicator bar, a scrollbar, a dot),
not surfaces. `--transition` (160ms) times every hover and focus change; under
`prefers-reduced-motion` all motion stops.

## Components

| Component | Tokens |
|---|---|
| Button `.btn`, `.btn-primary`, `.btn-danger`, `.btn-ghost`, `.btn-sm` | `--pad-control`, `--pad-control-sm`, `--fs-sm`, `--fs-xs`, `--fw-semibold`, `--radius-control`, `--accent`, `--on-accent` |
| Badge `.badge-*` | `--pad-badge`, `--fs-label`, `--radius-pill`, the matching `-bg`/`-border` pair |
| Card `.card`, `.stat-card` | `--bg-card`, `--border`, `--radius-lg`, `--shadow-sm`, `--hairline`, `--fs-2xl` for the figure |
| Table | `--pad-cell`, `--fs-sm`, `--fs-label` heads, `--bg-secondary` head row |
| Field | `--pad-input`, `--fs-sm`, `--bg-input`, `--border-strong`, `--shadow-glow` on focus |
| Alert `.alert-*`, toast | `--space-3`, `--radius`, the status `-bg`/`-border`/text trio |
| Section title | `--fs-label`, uppercase, `--text-muted`, a `--border` rule after it |

Shared helpers in `style.css`: `.mt-1` to `.mt-4`, `.mb-2` to `.mb-4`, `.ml-1`, `.ml-2`
(margins on the scale), `.row`, `.row-between`, `.grow`, `.text-muted`, `.text-xs`,
`.fw-semibold`, `.card-body`. Prefer a named component class when a block has more than two
rules.

## Exceptions

- **The live view (3D routing scene)** is dark in both themes and draws on a canvas: its rules
  sit between `/* design-tokens: exception start */` and `/* design-tokens: exception end */`
  in `style.css`, its legend sets `--c` per item in `index.html`, and `live3d.js` keeps its own
  scene colors.
- **Inline styles that scripts need** stay in the markup: an initial `display:none` that a
  script toggles, a progress `width`, and the `--c` of the 3D legend. Nothing else goes in a
  `style` attribute or in `cssText`.
- **The client portal chart** reads its colors from the tokens when it is drawn
  (`--accent`, `--sapphire`, `--text-muted`, `--border`); a theme change shows after a reload.

## Labels

- US English everywhere in the product (catalog, color, canceled).
- Sentence case for titles, buttons, labels and menus; proper names and acronyms keep their
  capitals (API, CLI, MCP, OAuth, GitHub, Ollama, Plugboard).
- No space before a colon. `…` ends a label for work in progress (`Loading…`); a bare `...`
  placeholder that tests wait on stays.
- "Sign in" and "Log out", as verbs.

## Adding a page or a component

1. Reuse a component above before writing a new class.
2. New rules use tokens only: `var(--fs-*)`, `var(--fw-*)`, `var(--space-*)`, `var(--pad-*)`,
   `var(--radius-*)` and color tokens.
3. A new token goes in the `:root` block (and in both light blocks when it is a color) and in
   this file.
4. Check both themes and a 390px-wide screen; touch targets stay 44px under
   `@media (pointer: coarse)`.
5. Run `pytest tests/test_dashboard_design_tokens.py`.
