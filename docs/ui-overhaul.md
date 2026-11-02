# UI overhaul brief

Decisions for the UniVerse redesign, made before any code changes. Every choice has a
reason; anything not listed here stays as it is.

## Direction

- **Reference:** macOS native, Notes and Xcode. Sidebar vibrancy, system controls, SF Pro.
  It should look like it belongs on the Mac, not like a web app inside a window.
- **Goal:** learning comes first. Anything on screen that isn't the material costs
  attention (cognitive load), so the chrome stays quiet and the material gets the space.
- **Platform:** Mac only for now. The toolbar, fonts and the accent go through a small platform
  layer so Windows (Mica/Acrylic, Segoe UI) can be added later without touching the panels.

## Layout

Three columns in a `QSplitter` (`universe/ui/workbench.py`) replace the floating docks:

| Column | Content | Surface |
|---|---|---|
| Sidebar | Subjects → lectures (⌥⌘S) | Solid |
| Center | Notes (⌥⌘N) beside Documents: list + slide viewer (⌥⌘P) | Solid |
| Inspector | Segmented control: Transcript · Recordings · AI Tutor (⌥⌘I) | Solid |

- Documents sit in the centre, not the inspector: slides are read next to the notes, so they
  get centre space. Practice stays a tab inside AI Tutor for now.
- Toolbar: on macOS a native `NSToolbar` (Liquid Glass on macOS 26), elsewhere a Qt
  toolbar. From left: sidebar toggle, Record, Timestamp, mode switcher, Export / Import,
  inspector toggle. With the native toolbar, timer, level meter and microphone sit in the
  status bar. No breadcrumb: the window title already says where you are,
  and the toolbar needs that room at half-screen width.
- Status bar: holds the activity indicator and transient messages. Its popup shows CPU, GPU and memory as three readouts over a two-minute live chart (usage_chart.py). The chart glides with the clock, breaks where sampling paused, shows values on hover, and stops moving with Reduce motion.
- **Narrow windows:** when the window is too narrow for every part you want (half a laptop
  screen), parts step aside, the inspector first, then the sidebar, and come back when
  there is room. The part you asked for last (e.g. AI Tutor via Export) always gets its
  place. Parts you hid yourself stay hidden.
- **Customisation, kept low-key:** divider positions, hidden parts and the inspector pane
  are remembered per mode (`settings.layout`). Two options in View: "Inspector on the Left"
  (also on right-click of the inspector header) and "Slides Below Notes". No free docking,
  no floating windows.

## Workspace modes

Presets that rearrange the columns. ⌘1–⌘4 switch between them, and each mode remembers your
own splitter sizes and inspector tab.

| Mode | Center | Inspector | Sidebar |
|---|---|---|---|
| Lecture (⌘1) | Notes + slides 3:2 | Transcript | Shown |
| Read (⌘2) | Notes + slides 1:2 | Hidden | Hidden |
| Review (⌘3) | Notes | Transcript, wider (480) | Shown |
| Exam (⌘4) | Notes | AI Tutor on Practice, wider (520) | Shown |

The switcher sits in the toolbar (icons only below 1200 px window width, where Timestamp
and Import reply also drop their labels) and in View. "Reset This Mode" puts back the preset; "Reset All Modes" starts over. The Panels menu and ⌘0–⌘5 dock shortcuts are gone since step 2; showing and hiding
parts lives in View, with the ⌥⌘ shortcuts above.

## Glass

- First attempt (removed): `NSVisualEffectView`s behind Qt widgets. It came out as plain
  see-through, text over the desktop was hard to read, and it didn't look like a Mac app.
- Now: glass only where macOS draws it itself. The main window gets a native `NSToolbar`
  (`universe/ui/platform/toolbar.py`, `_mac_toolbar.py`), so on macOS 26 the buttons are
  Liquid Glass capsules with SF Symbols, like Notes or Finder. Every item drives a
  `QAction` the menus and shortcuts share, and follows its enabled/checked state.
- The window uses `ExpandedClientAreaHint` + `NoTitleBarBackgroundHint`; Qt keeps the
  content below the toolbar through the safe area. Everything below it is solid Qt.
- Without pyobjc, off macOS, or in tests the Qt toolbar stays. Windows gets its own
  backend later.
- Below the toolbar, sidebar, notes, documents and inspector are floating panels
  (`workbench.Panel`): 12 px corners, a light rim, a short soft shadow, on a quiet grey
  window background (`Tokens.window`). Drawn by Qt, so text stays sharp. Real glass behind
  Qt widgets was tried twice; Qt paints over it, and over a flat background it looks the
  same as these panels anyway.
- Real Liquid Glass where Apple puts it, on controls floating over content: the PDF
  viewer's page, zoom and marking controls are an `NSGlassEffectView` capsule over the
  slide (`platform/floating.py`), like Preview. Each glass button mirrors a hidden Qt
  button, so logic, shortcuts and tests stay on the Qt side. In a narrow panel the least
  important items drop out first (zoom, then the colour menu); page navigation stays.
- The audio player floats the same way over the bottom of the notes: play/pause,
  ±10 s, a position slider, the time and a recording menu. The notes get room below the
  last line so the capsule never covers text you can't scroll to. In a narrow panel the
  slider steps aside before the time. Without recordings the plain hint stays under the notes.
- No glow, no gradients, no coloured shadows.

## Colour

- **Accent: muted blue.** Light `#2F6497`, dark `#6FA0D0` (text/icons) and `#3A72A8` (fills
  with white text). Hue ~211°, desaturated. Reason: blue is the most consistently studied
  colour for staying focused on long tasks without arousal. The evidence is modest but
  points one way. It's kept off the stock Tailwind/macOS blue and out of the indigo/violet band.
- Saturated colour is reserved for state: record red `#E5484D`, warning amber `#C27C0E`,
  success green `#30A46C` (level meter only). Everything else is neutral, so these stand out.
- Neutrals come from the system palette (`palette(...)`) so light/dark follows macOS;
  both themes are designed, not just one.
- All tokens live in one place (`style.py` → `Tokens`); no inline `setStyleSheet`
  colours in panels (6 call sites to move).

## Type

- SF Pro (system) for the UI, SF Mono for times, timestamps and the record timer.
  Windows later: Segoe UI / Cascadia Mono.
- Chrome 13 px, secondary 11 px, section headers 11 px semibold uppercase-free.
- Reading surfaces (notes, transcript, practice): 15 px body, ~1.5 line height,
  lines capped at about 70 characters (wider panes get side margins, not longer lines).
- Headings by weight and size only, no colour.

## Density

Compact chrome, comfortable content:

- Sidebar rows 24 px, toolbar controls 26 px, inspector list rows 28 px.
- Spacing scale 4 / 8 / 12 / 16 / 24; nothing off-scale.
- Radius scale 4 (controls, list rows) / 6 (cards, inputs) / 10 (popovers). The record
  button is the only pill.

## Icons

- Lucide SVGs (ISC licence), bundled under `universe/ui/icons/`, tinted at load to the
  current text or accent colour, 16 px in chrome, 14 px inline.
- Replace every emoji and Unicode glyph used as an icon (⏱ ⇪ ⇩ ▦ ✂ ❚ ✕ ★ and the
  emoji in transcript, sidebar, jobs, audio, documents, main window). Speaker and
  importance markers in the transcript become icons with tooltips.
- Toolbar buttons: icon + short label for the primary actions (Record, Export), icon
  only with tooltip for the rest.
- Done in step 5. Content glyphs stay: `[▶ 00:12:31]` stamps, `→` slide links, `★` ratings
  and the Claude export markers, `✓`/`✗` in status text.
- Transcript lines carry one icon each (exam star, question, answer, private) instead of
  emoji prefixes; lecturer lines have no speaker prefix, other voices are named.
- Microphone check: a coloured status dot (`icons.dot`) instead of coloured emoji circles.
- Note toolbar: icons only for lists, to-do, slide link and timestamp; B / I / H1–H3 as text.

## Motion

Subtle but present, always for a reason (`universe/ui/motion.py`):

- Switching a mode or an inspector pane, or showing/hiding a part: a 150 ms cross-fade from
  a snapshot of the old look. Only that snapshot is animated, never the panes.
- No fade when parts step aside during a window resize: that would flicker.
- Not built, on purpose: a width animation for sidebar/inspector (the splitter's minimum
  widths make it jumpy) and a colour fade on the record button (stylesheets can't animate).
- No hover-grow, no fade-in on lists, no looping animation.
- macOS "Reduce motion" turns all of it off; "Reduce transparency" is handled by
  macOS for the native toolbar.

## Light and dark

- Switching macOS appearance while UniVerse runs rebuilds the stylesheet with the new tokens.
- Icons look their colour up when they are painted (a custom `QIconEngine`), so they follow
  the switch by themselves: text colour, accent, muted when disabled, white on selected rows.
- Timestamps in notes take the new accent too.

## Copy

Short labels, saying what happens: "Export for Claude", "Import reply", "Start recording".
No exclamation marks, no "magic", no emoji in headings or buttons.

## Out of scope

- Feature changes: export, transcription, practice logic stay as they are.
- The PDF renderer itself; only its frame and toolbar get restyled.
- Windows build (only the hooks for it).

## Migration and risks

- Old `window_state` (dock `saveState`) is dropped on first launch of the new layout;
  `STATE_VERSION` bump, and new settings keys for mode, splitter sizes and inspector tab.
- `tests/test_main_window.py` and `tests/test_documents_panel.py` reference docks and
  need updating with the restructure.
- pyobjc becomes a Mac-only dependency (`; sys_platform == "darwin"` in requirements).

## Suggested order

1. Tokens + icon loader + platform glass layer (no visible layout change yet).
2. New window skeleton: splitter columns, inspector with segmented control, toolbar.
3. Move panels in; delete docks; settings migration; fix tests.
4. Workspace modes + shortcuts.
5. Restyle each panel against the tokens; remove inline styles and emoji.
6. Motion + Reduce motion/transparency checks; light and dark pass; half-screen pass.
7. unslop-ui audit by eye (spacing, alignment, overflow with long subject names).

## Audit (step 7)

- Clean by scan: no gradients, glow, violet/indigo, emoji icons or default fonts; every colour
  is a token, a state colour or a tag colour you chose.
- Fixed: off-scale spacing (3/6/10/14 px → 4/8/16), tag chips from pills to the 4 px radius,
  the "Slides" tag blue matched to the accent, leftover emoji-sized fonts on two buttons.
- Fixed: long PDF and lecture names in the documents list were cut off; now "…" in the
  middle of file names (the extension stays) and at the end of lecture names. Sidebar rows
  show the full name as a tooltip.
- Fixed: the start page was hero + full-width buttons + three cards. Now a task heading, one
  sentence, buttons at natural width with a real "+" icon, and the first steps as a list.
- Accepted: with a very long word in a lecture title the notes column needs ~260 px, so at
  half-screen the sidebar steps aside; ⌥⌘S brings it back. Breaking words would read worse.
