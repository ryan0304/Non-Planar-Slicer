## REJECT
- Never let a bare `system-ui` lead a font stack; lead with `-apple-system, BlinkMacSystemFont`, then `Inter`.
- Never use `font-weight: 500`; the ladder is `300/400/600/700` only.
- Never add a decorative shadow to buttons, chips, cards, or the timeline.
- Never use a hover lift or shadow bloom; the only press state is `scale(0.95)`.
- Never collapse `--ok`/`--warn`/`--danger` or `--accent-purple`/`--measure`/`--cage` into one accent color.
- Never retune data-encoding colors (viridis legend, toolpath/blob colors, printer materials) to match the theme.
- Never treat the Z-height legend gradient as decorative -- it's data.
- Never soften a `--warn` signal into a bare outline.
- Never adopt a light canvas, 80px section padding, or full-bleed tile layout in the control panel.
- Never use a border radius outside the `sm`/`md`/`lg`/`pill` grammar.

## REQUIRE
- Reserve the pill radius for the primary action button only; keep secondary buttons rectangular.
- Set numeric/measured readouts in monospace tabular-nums; keep labels sans-serif.
- Keep the viewer's theme dark ("instrument dark").
- Measure text contrast against `--bg` before shipping a palette change (>=4.5:1, >=5.8:1 for safety colors).
- Give risk/caution copy its own visually distinct treatment, not the muted style of ordinary help text.

## WHEN AMBIGUOUS
- When a marketing design spec conflicts with instrument usability, side with the instrument.
- When unsure if a color is decorative or data, treat it as data and leave it alone.
