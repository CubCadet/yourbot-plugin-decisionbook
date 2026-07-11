# DecisionBook brand

## Identity

- Name: **DecisionBook**
- Tagline: **Decisions remembered. Context preserved.**
- Marketplace line: **Record what was decided, why it was chosen, and how it turned out.**

DecisionBook is deliberate, trustworthy, calm, and clear. Use concise factual language. Prefer
“recorded,” “closed,” “outcome,” and “reasoning”; avoid bureaucratic ticketing vocabulary.

## Product language

The v0.2 interface uses one `/decision` root with `add`, `view`, `list`, `close`, and `help`
subcommands. Spell these with spaces—never revive the v0.1 `/decision-add` style.

- Creation says **Record a decision**, not “create ticket” or “submit request.”
- Completion says **Close decision** and **Outcome**, not “resolve issue.”
- Errors distinguish “not found,” “temporarily unavailable,” “nothing was saved,” and “saved but
  confirmation could not be displayed.” Do not obscure record integrity behind a generic error.
- Empty states should teach the next action: start with `/decision add` or refresh with
  `/decision list`.
- Original details are **immutable**; avoid wording that implies edits or deletion.

## Interface accessibility

- Pair icons and palette colors with the literal words **Open** and **Closed**; color alone must not
  carry status.
- Render recorded identities as safe, non-pinging Discord mentions and interaction times as
  viewer-localized Discord timestamps.
- Preserve intentional paragraph breaks in decisions, reasoning, and outcomes.
- Escape user-authored Markdown and suppress mentions without making international text or joined
  emoji feel broken.
- Keep action labels direct: **View #12**, **Previous**, **Next**, and **Close decision**.
- On narrow dashboard layouts, prioritize ID, title, status, summary, author, and recorded time; the
  dashboard remains read-only.

## Palette

- Ink `#172033` — primary surface and open-book outline
- Parchment `#F6F0E3` — pages
- Decision gold `#C99A2E` — active/open decisions
- Success green `#2D7A5E` — closed decisions
- Muted slate `#667085` — secondary text
- Error red `#B54747` — errors only

## Mark

The mark combines an open book, central gold bookmark, and green check. Preserve clear space equal
to the bookmark width. Do not recolor the check red or distort the aspect ratio.

The SVG source is for brand/export use. A 512 px, marketplace-sized raster export is included as
`decisionbook-icon-512.png`. The manifest intentionally omits `icon_url` until a human owner
hosts that PNG over HTTPS. The manifest `author` is likewise the real YourBot developer identity;
neither value should be invented by automation.
