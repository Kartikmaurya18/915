# Interface design

The web app's visual and interaction layer: an original, restrained design for a simulated-trading product. This document is the design
reference: what each page is for, the tokens, the component tree, the states every module handles, and how to verify it.
Code lives in [`web/src`](../web/src); the tokens are the single block of CSS variables in [`web/src/styles.css`](../web/src/styles.css).

**What this is not.** A copy of any brand. The look is inspired by the clarity of modern investing apps (generous whitespace, one accent colour,
large legible numbers) but uses its own palette, wordmark-free header and wording. Every order surface says **Simulated**, because no real order
is ever placed.

## 1. UX breakdown

### Goals per page

| Page | The user wants to… | Primary action |
|---|---|---|
| Sign in / Create account | get in quickly and know it is a simulation | Sign in |
| Overview | see the market now: price, ATM, the chart, the chain; see their own book at a glance | New simulated order |
| Paper Trading | review and manage positions; act on selected contracts | New simulated order, Execute selected |
| History | look back at a past day: candles, a strike's straddle, their trades | Previous / Next day |
| RRG | see which contracts are strengthening or fading | (read-only) |
| Data Quality | trust the feed, and know when not to | (read-only) |
| Logs, Settings (admin) | operate the engine | Save |

### Layout, desktop (≥ 1280 px)

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ ▲ Market Data  SIMULATED  [Overview] Trading History RRG Quality Logs Settings   🔍 Ctrl K  ●Live  ◐  A▾ │  sticky header
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ Overview                                                        [⚡ New simulated order]  │
│ ┌ NIFTY spot ┐ ┌ ATM strike ┐ ┌ ATM straddle ┐ ┌ Feed ───────┐                            │  market metrics
│ Your simulated book                                          Open Paper Trading →        │
│ ┌ Total P&L ▲ ┐ ┌ Open positions ┐ ┌ Win rate ┐ ┌ Turnover ┐                              │  paper metrics
│ ┌──────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ NIFTY 25,286.39 ▲+1.72%           [1m 5m 15m] [Candles|Line] [1D 1W 1M 1Y ALL]       │ │  chart card
│ │ ▁▃▅▇ candles ▇▅▃                                                                     │ │
│ │ View as a table ▸                                                                    │ │
│ └──────────────────────────────────────────────────────────────────────────────────────┘ │
│ ┌ ATM straddle · 25,300 ──────── [Straddle|CE|PE] [1m|5m|15m] ┐                          │
│ ┌ Option chain ───────────────────────────────── [Trade 2 selected →] ┐                  │
│ │ ☐ CE LTP  CE Δ │ STRIKE │ PE Δ  PE LTP ☐   CE+PE  OI  OI  ⚡                          │ │  sticky header, sticky strike
│ └ Recent candles ┘                                                                        │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### Layout, phone (360 px)

```
┌───────────────────────┐
│ ▲            🔍  ◐  A▾ │  sticky header (icon buttons)
│ ● Live                 │
│ Overview               │
│ [⚡ New simulated order]│
│ ┌ NIFTY ┐ ┌ ATM     ┐  │  2-column metric cards
│ ┌ chart (full width) ─┐│
│ ┌ option chain ──────┐ │  scrolls sideways; the strike column stays pinned
│ ⌂ Home  Trade History RRG Quality │  fixed bottom bar (5 primary pages)
└───────────────────────┘   Order panel and search open as a bottom sheet / full-width dialog
```
Below 1280 px the page navigation moves to the bottom bar (the seven inline links do not fit at 1024 px). Admin pages stay in the account menu and in search.

### Component tree

```
main.tsx
 BrowserRouter > ThemeProvider > AuthProvider > SelectionProvider > UIProvider > App
 App > RealtimeProvider > Routes
  Layout  (skip link · LiveAnnouncer · header · main · bottom bar)
   ├ header: Brand · inline nav · search button · StatusPill · ThemeToggle · UserMenu
   ├ <Outlet/>: Overview | Trading | History | Rrg | Quality | Logs | Settings   (lazy)
   ├ CommandPalette  (lazy, first use)  > Dialog > combobox + listbox
   └ OrderDrawer     (lazy, first use)  > Dialog > form → review → done
  AuthPage (sign in / register)

ui/         Button Card Badge/DeltaBadge/Signed Segmented Dialog fields(Field Input Select Checkbox Kbd) feedback(Skeleton EmptyState ErrorState FormMessage) layout(PageHeader Metric MetricGrid TableWrap)
components/ Layout StatusPill CandleChart PriceChartCard StraddleChart CommandPalette OrderDrawer guards
```

## 2. Design tokens

**Colour.** One emerald-teal accent, slate neutrals, green/red for gain/loss. Tokens are CSS variables; dark mode is the same names with other values
(`:root` and `.dark` in `styles.css`). Contrast is enforced by `web/tests/contrast.test.ts`, which parses the file: a palette change that breaks WCAG AA fails the build.

| Token | Role | Light | Dark | Contrast light / dark (against) |
|---|---|---|---|---|
| `bg` | Page background | `#f7f9fb` | `#0a0f1c` | decorative |
| `surface` | Cards, header, dialogs | `#ffffff` | `#0f172a` | decorative |
| `surface-2` | Subtle fills, table stripes, hover | `#f1f5f9` | `#1b2538` | decorative |
| `line` | Decorative card and table borders | `#e2e8f0` | `#263449` | decorative |
| `control` | Input and checkbox borders | `#6b7a90` | `#7487a3` | 4.36 / 4.88 (on surface) |
| `fg` | Primary text | `#0f172a` | `#e6ebf2` | 17.85 / 14.90 (on surface) |
| `muted` | Secondary text | `#475569` | `#a3b1c6` | 7.58 / 8.21 (on surface) |
| `subtle` | Captions, placeholders | `#5a687c` | `#8493aa` | 5.67 / 5.73 (on surface) |
| `brand` | Brand fills, focus ring | `#0a7f61` | `#34d399` | 4.97 / 9.29 (on surface) |
| `brand-fg` | Brand-coloured text and links | `#066b52` | `#34d399` | 6.50 / 9.29 (on surface) |
| `brand-soft` | Brand tint (selected, badges) | `#e3f5ef` | `#0e2f2a` | decorative |
| `on-brand` | Text on a brand fill | `#ffffff` | `#03211a` | 4.97 / 8.84 (on brand) |
| `pos` | Gains | `#047857` | `#34d399` | 5.48 / 9.29 (on surface) |
| `pos-soft` | Gain badge background | `#e7f7ef` | `#0e2f2a` | decorative |
| `neg` | Losses | `#a81a1a` | `#f87171` | 7.43 / 6.45 (on surface) |
| `neg-soft` | Loss badge background | `#fdecec` | `#3a1a1f` | decorative |
| `warn` | Warnings | `#92400e` | `#fbbf24` | 6.37 / 8.29 (on warn-soft) |
| `warn-soft` | Warning background | `#fef3c7` | `#3a2a0e` | decorative |
| `ring` | Focus ring | `#0a7f61` | `#34d399` | 4.97 / 9.29 (on surface) |

Gain and loss differ in brightness as well as hue (tested), and are always paired with an arrow, a sign and a hidden word, never colour alone.

**Typography.** Inter (variable, self-hosted via `@fontsource-variable/inter`, so there is no third-party request and the CSP stays `font-src 'self'`). Every figure that can
change uses `font-variant-numeric: tabular-nums` (the `num` utility), so a tick never shifts the layout. Numbers use Indian digit grouping (`12,34,567.89`).

| Use | Size / weight |
|---|---|
| Page title (h1) | 24 px / 600, tracking tight |
| Card title (h2) | 16 px / 600 |
| Metric figure | 24 px / 600 tabular |
| Body, table | 15 px / 400, 14 px in tables |
| Caption, label | 12 px / 500, uppercase tracking for metric captions |

**Shape and depth.** Radii: 8 px for controls (inputs, buttons, chips), 12 px for cards and dialogs. Borders 1 px (`line`). Two shadows: `shadow-card`
(two soft layers) and `shadow-pop` (dialogs, menus); dark mode uses deeper alphas. Spacing is a 4 px grid (Tailwind's scale).

**Motion.** 120–220 ms for dialogs and hover colour; `prefers-reduced-motion` removes all animation globally.

## 3. Where the code is

| Module | Files |
|---|---|
| Tokens, base styles, focus ring | `web/src/styles.css`, `web/public/theme-init.js` (sets the theme before first paint, so no flash) |
| Theme (system / light / dark, persists, works without storage) | `web/src/theme/ThemeProvider.tsx` |
| Primitives | `web/src/ui/*` |
| App shell, navigation, announcer, account menu | `web/src/components/Layout.tsx`, `web/src/nav.ts`, `StatusPill.tsx` |
| Dashboard cards | `web/src/ui/layout.tsx` (`Metric`), used by Overview, Trading, History |
| Charts and time ranges | `web/src/components/CandleChart.tsx`, `PriceChartCard.tsx`, `web/src/lib/daily.ts`, `api/useDailyBars.ts` |
| Command palette | `web/src/components/CommandPalette.tsx`, `web/src/ui/UIProvider.tsx` (Ctrl/Cmd+K) |
| Order panel | `web/src/components/OrderDrawer.tsx` |
| Option chain | `web/src/pages/Overview.tsx` (`OptionChain`, memoised `ChainRow`) |

**Dependencies added:** `tailwindcss` + `@tailwindcss/vite` (styling), `lucide-react` (icons, tree-shaken), `@fontsource-variable/inter` (the font, self-hosted for privacy and CSP).
`axe-core` is a **dev-only** test tool and is not shipped. No headless-UI library was needed: dialogs use the native `<dialog>` element, which gives the focus trap, inert page, Escape handling and
focus return for free.

## 4. States checklist

Legend: ✓ implemented and checked.

| Module | Default | Hover | Focus | Disabled | Loading | Empty | Error | Offline / stale feed |
|---|---|---|---|---|---|---|---|---|
| Header, nav, theme toggle | ✓ | ✓ | ✓ ring | – | – | – | – | pill: Live / Connecting / Reconnecting / Feed stale / Offline / Signed out (+ spoken) |
| Metric cards | ✓ | – | – | – | ✓ skeleton | ✓ "—" | – | values keep their last data; pill shows staleness |
| Price chart | ✓ | ✓ crosshair | ✓ | – | ✓ skeleton | ✓ "No price history yet" | ✓ alert | stale pill; last candles remain |
| Straddle chart | ✓ | ✓ | ✓ | – | – | ✓ "Waiting for an ATM strike" / "No candles yet" | ✓ alert | as above |
| Option chain | ✓ | ✓ row/price | ✓ checkbox | – | ✓ placeholder | ✓ "No option chain yet" | – | last chain remains |
| Command palette | ✓ | ✓ | ✓ active row | – | – | ✓ "Nothing matches…" (+ spoken) | – | contracts list empty until the chain arrives |
| Order drawer | ✓ | ✓ | ✓ | ✓ Review disabled until valid | ✓ spinner on place | ✓ "Waiting for the option chain…" | ✓ inline + alert; retry is safe (same key) | order refused by the server if the quote is stale |
| Positions / History tables | ✓ | ✓ | ✓ scroll region | ✓ Close while closing | ✓ skeleton | ✓ | ✓ | – |
| Forms (settings, TP/SL, sign in) | ✓ | ✓ | ✓ | ✓ | ✓ button spinner | – | ✓ field + form level | – |

## 5. Accessibility notes (WCAG 2.2 AA)

* Skip link is the first tab stop; one `h1` per page; landmarks (`header`, `nav` ×2 with distinct names, `main`); route changes update the title and move focus to the content.
* Dialogs: native modal (inert background, focus trap, Escape, focus returns to the opener); the search is an ARIA combobox (`aria-activedescendant`, grouped listbox, result count spoken).
* Tabs (`Segmented`) follow the tabs pattern: one tab stop, arrow keys, Home/End. Visible text is part of every accessible name (WCAG 2.5.3).
* Tables have captions and scroll in a focusable, labelled region; the strike column is pinned on small screens.
* Charts are labelled groups with a plain-language summary, and have a **View as a table** alternative. The RRG plot has a text summary and the full data table beside it.
* Connection changes are spoken once through a polite live region (not every tick).
* Target sizes: buttons and inputs are 40 px tall (32 px in dense tables, 28 px for the small tab controls), header controls 36 px, phone bottom-bar items 56 px. All exceed the WCAG 2.2 minimum of 24 px; a larger hit area for the smallest controls would be a sensible refinement.

## 6. How to verify

Automated (run on every change):

```
cd web
npm run typecheck && npm test        # 285 tests, including contrast, theme, palette, drawer, primitives
npm run build                        # initial JS about 75 KB gzipped; the chart, palette and drawer load on demand
```

Manual and browser checks (what was run for this design, with headless Chrome and axe-core):

1. **Keyboard.** Reload, press Tab: the first stop is "Skip to content". `Ctrl/Cmd+K` opens search with focus in the box; arrows move, Enter runs, Esc closes and focus returns to the page. In the order panel, Tab cycles inside it and never reaches the page behind.
2. **Contrast.** `npm test` covers the tokens. In the browser, axe reports no colour-contrast violations in either theme.
3. **Responsive.** Check 360, 768, 1024 and 1440 px: no horizontal page scroll; the bottom bar appears below 1280 px.
4. **Dark mode.** Cycle the theme button (system → light → dark); reload and confirm it persists, and that with site storage blocked it still switches for the session.
5. **Screen reader.** Not tested with a real screen reader. Use NVDA or VoiceOver to confirm the chart summaries, the search results announcement and the connection announcements read well.
6. **Real browsers.** Only Chrome was exercised. Check Safari and Firefox (native `<dialog>` and `:focus-visible` are supported by current versions).
