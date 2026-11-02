# UI overhaul brief

Decisions for the UniVerse redesign, made before any code changes. Every choice has a
reason; anything not listed here stays as it is.

## Direction

- **Reference:** macOS native, Notes and Xcode. Sidebar vibrancy, system controls, SF Pro.
  It should look like it belongs on the Mac, not like a web app inside a window.
- **Goal:** learning comes first. Anything on screen that isn't the material costs
  attention (cognitive load), so the chrome stays quiet and the material gets the space.
- **Platform:** Mac only for now. Glass, fonts and the accent go through a small platform
  layer so Windows (Mica/Acrylic, Segoe UI) can be added later without touching the panels.

## Layout

Three columns in a `QSplitter` (`universe/ui/workbench.py`) replace the floating docks:

| Column | Content | Surface |
|---|---|---|
| Sidebar | Subjects → lectures (⌥⌘S) | Glass (`sidebar` material) |
| Center | Notes (⌥⌘N) beside Documents: list + slide viewer (⌥⌘P) | Solid |
| Inspector | Segmented control: Transcript · Recordings · AI Tutor (⌥⌘I) | Solid, glass header |

- Documents sit in the centre, not the inspector: slides are read next to the notes, so they
  get centre space. Practice stays a tab inside AI Tutor for now.
- Toolbar: unified with the title bar, glass. From left: sidebar toggle, **record button +
  timer + level meter, always visible**, Timestamp, then (step 4) mode switcher, Export /
  Import, inspector toggle. No breadcrumb: the window title already says where you are,
  and the toolbar needs that room at half-screen width.
- Status bar: glass, holds only the activity indicator and transient messages.
- **Narrow windows:** when the window is too narrow for every part you want (half a laptop
  screen), parts step aside, the inspector first, then the sidebar, and come back when
  there is room. The part you asked for last (e.g. AI Tutor via Export) always gets its
  place. Parts you hid yourself stay hidden.
- **Customisation, kept low-key:** divider positions, hidden parts and the inspector pane
  are remembered (`settings.layout`). Right-click options ("Open in center", "Move
  inspector to left") come with the modes in step 4. No free docking, no floating windows.

## Workspace modes

Presets that rearrange the columns. ⌘1–⌘4 switch between them, and each mode remembers your
own splitter sizes and inspector tab.

| Mode | Center | Inspector | Sidebar |
|---|---|---|---|
| Lecture | Notes + slides | Transcript (live) | Shown |
| Read | Slides large, notes narrow | Hidden | Collapsed |
| Review | Notes | Transcript + player | Shown |
| Exam | Notes | Practice / Claude | Shown |

"Reset mode" puts back the preset. The Panels menu and ⌘0–⌘5 dock shortcuts are gone since step 2; showing and hiding
parts lives in View, with the ⌥⌘ shortcuts above.

## Glass

- Native `NSVisualEffectView` through pyobjc (`pyobjc-framework-Cocoa`), behind the
  sidebar, toolbar, inspector header and status bar only. Materials: `sidebar`,
  `titlebar`, `headerView`.
- Notes, slides, transcript and practice stay opaque so text is always readable.
- If macOS "Reduce transparency" is on, or pyobjc is missing, those surfaces fall back to solid
  `palette(window)`. The app has to look finished without glass too.
- Interface: `universe/ui/platform/glass.py` with `apply(widget, role)`; `_mac.py`
  implements it, `_fallback.py` paints solid. Windows gets `_win.py` later.
- No glow, no gradients, no coloured shadows. Glass panels are separated by a 1 px
  hairline, not by shadow.

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

## Motion

Subtle but present, always for a reason:

- Pane/mode switch: 150 ms cross-fade of the changing column.
- Sidebar and inspector collapse: 180 ms width animation, ease-out.
- Recording start: record button colour change 120 ms; level meter is live, not animated.
- No hover-grow, no fade-in on lists, no looping animation.
- macOS "Reduce motion" turns all of it off.

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
- Native glass under Qt widgets needs `WA_TranslucentBackground` on the host widgets;
  check text rendering and that the PDF viewer isn't affected.

## Suggested order

1. Tokens + icon loader + platform glass layer (no visible layout change yet).
2. New window skeleton: splitter columns, inspector with segmented control, toolbar.
3. Move panels in; delete docks; settings migration; fix tests.
4. Workspace modes + shortcuts.
5. Restyle each panel against the tokens; remove inline styles and emoji.
6. Motion + Reduce motion/transparency checks; light and dark pass; half-screen pass.
7. unslop-ui audit by eye (spacing, alignment, overflow with long subject names).
