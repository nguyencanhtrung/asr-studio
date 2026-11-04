---
name: ASR Studio
description: An editorial-engineering workbench for local, source-verifiable meeting transcription.
colors:
  light-background: "#faf9f7"
  light-surface: "#ffffff"
  light-surface-subtle: "#f6f4f0"
  light-ink: "#1c1c21"
  light-ink-secondary: "#52525b"
  light-ink-muted: "#6b6b77"
  light-accent: "#8e2433"
  light-accent-hover: "#741b29"
  light-accent-soft: "#f3e4e6"
  light-hairline: "#e5e2dc"
  light-control-border: "#858078"
  dark-background: "#131318"
  dark-surface: "#1b1b22"
  dark-surface-subtle: "#17171d"
  dark-ink: "#e9e7e3"
  dark-ink-secondary: "#a6a6b0"
  dark-ink-muted: "#83838e"
  dark-accent: "#e08b96"
  dark-accent-hover: "#eea2ab"
  dark-accent-soft: "#35222a"
  dark-hairline: "#2a2a33"
  dark-control-border: "#777783"
  success-light: "#226442"
  success-dark: "#86c9a4"
  warning-light: "#81540f"
  warning-dark: "#efbd69"
  danger-light: "#a63838"
  danger-dark: "#f0958e"
typography:
  access-display:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "clamp(2.65rem, 6vw, 4.5rem)"
    fontWeight: 500
    lineHeight: 1.02
    letterSpacing: "-0.045em"
  access-display-mobile:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "2.7rem"
    fontWeight: 500
    lineHeight: 1.02
    letterSpacing: "-0.045em"
  headline:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "clamp(1.5rem, 3vw, 2.125rem)"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  meeting-headline:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "clamp(1.625rem, 3vw, 2.25rem)"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.75rem"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  calendar-title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.75rem"
    fontWeight: 500
    lineHeight: 1.2
  dialog-title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.2
  dialog-title-mobile:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.2
  mobile-title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.2
  section-title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1.125rem"
    fontWeight: 560
    lineHeight: 1.3
  interface-heading:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1rem"
    fontWeight: 500
    lineHeight: 1.3
  intro:
    fontFamily: "Inter Variable, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.7
  reading:
    fontFamily: "Inter Variable, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.7
  panel-title:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1rem"
    fontWeight: 650
    lineHeight: 1.3
  subheading:
    fontFamily: "Newsreader Variable, Georgia, Times New Roman, serif"
    fontSize: "1rem"
    fontWeight: 650
    lineHeight: 1.3
  body:
    fontFamily: "Inter Variable, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.65
  control:
    fontFamily: "Inter Variable, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.55
  body-small:
    fontFamily: "Inter Variable, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.6
  metadata:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: "0.08em"
rounded:
  sm: "4px"
  md: "8px"
  lg: "12px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button-primary-light:
    backgroundColor: "{colors.light-accent}"
    textColor: "#fffafa"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 15px"
    height: "40px"
  button-primary-dark:
    backgroundColor: "{colors.dark-accent}"
    textColor: "#2a1117"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 15px"
    height: "40px"
  field-light:
    backgroundColor: "{colors.light-surface}"
    textColor: "{colors.light-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "40px"
  field-dark:
    backgroundColor: "{colors.dark-surface}"
    textColor: "{colors.dark-ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "40px"
  panel-light:
    backgroundColor: "{colors.light-surface}"
    textColor: "{colors.light-ink}"
    rounded: "{rounded.md}"
    padding: "20px"
  panel-dark:
    backgroundColor: "{colors.dark-surface}"
    textColor: "{colors.dark-ink}"
    rounded: "{rounded.md}"
    padding: "20px"
---

# Design System: ASR Studio

## Overview

**Creative North Star: "The Editorial Engineering Ledger"**

ASR Studio feels like a precise working ledger: calm enough for long review sessions, editorial enough to belong to the bibo product family, and technical enough to expose provenance, runtime state, and recoverable errors without ceremony. Warm paper and charcoal fields carry the visual world; burgundy signal lines, fine rules, and disciplined typography establish hierarchy.

The brand foundation is portable: palette roles, the Newsreader–Inter–JetBrains Mono relationship, hairlines, compact radii, accessible focus, and restrained motion. The sidebar, calendars, transcript generations, runtime panels, speaker tools, and one-step create-meeting dialog are ASR-specific operational components. Future products may reuse the foundation without copying this application's information architecture.

**Key Characteristics:**

- Editorial headlines paired with dense, legible controls.
- Flat, quiet surfaces divided by hairlines rather than decorative cards.
- Burgundy used as a sparse signal for action and selection.
- Technical metadata visually distinct in mono type.
- Light, dark, and system-following themes with the same semantic hierarchy.

## Colors

The palette pairs warm paper neutrals with charcoal dark surfaces and a reserved oxblood-to-dusty-rose accent.

### Primary

- **Oxblood Signal:** The light-theme action, link, focus, and selected-state color.
- **Dusty Rose Signal:** The corresponding dark-theme accent, raised in lightness to preserve contrast.

### Neutral

- **Warm Paper:** The light canvas; use restrained white and off-white surfaces to establish hierarchy.
- **Charcoal Field:** The dark canvas; use close charcoal steps instead of blue-tinted dashboard layers.
- **Ledger Ink:** Primary copy and titles; secondary and muted ink are reserved for explanation and metadata.
- **Hairline Rule:** One-pixel borders and dividers; it organizes information without becoming a frame.
- **Control Boundary:** `--control-border` separates editable fields, selects, language radio labels and the notes editor frame from their surroundings at ≥3:1 in both themes. Decorative dividers retain `--hairline` and `--line-strong`. Small text, including placeholders and dates outside the calendar month, stays ≥4.5:1; selected metadata uses secondary ink on the accent-soft fill. Placeholders use full opacity in every browser.

### Named Rules

**The Sparse Signal Rule.** Burgundy identifies primary action, selection, focus, or source linkage; it never becomes a full-screen wash.

**The Semantic Independence Rule.** Success, warning, and danger keep their own green, amber, and red roles in both themes and always appear with text, an icon, or another non-color cue.

## Typography

- **Display Font:** Newsreader Variable (with Georgia and Times New Roman fallbacks)
- **Body Font:** Inter Variable (with system UI fallbacks)
- **Label/Mono Font:** JetBrains Mono (with platform monospace fallbacks)

**Character:** Newsreader gives saved work and section titles an editorial voice. Inter keeps Vietnamese and English controls compact and readable; JetBrains Mono turns runtime state, timestamps, versions, IDs, and small uppercase labels into a distinct technical layer.

### Hierarchy

- **Headline:** Medium-weight Newsreader with tight tracking for access, empty-state, and meeting titles.
- **Title:** Medium-weight Newsreader for workspace and calendar headings.
- **Body:** Inter at compact dashboard sizes with generous line height for explanations and transcript prose.
- **Reading:** Inter at 16px, with a 1.7 line height for rendered meeting notes and 1.6 for transcript text; the Markdown editor stays in JetBrains Mono at 15px.
- **Label:** JetBrains Mono at 12px, usually uppercase with at most 0.08em tracking, for operational metadata rather than long content.
- **Scale:** Every size comes from the `--text-*` tokens in `design-system.css`: label 12, small 13, control 14, body 15, reading 16, subtitle 18, heading 20, title 24, title-lg 28 (px at the default 16px root).

### Named Rules

**The Three-Voice Rule.** Serif names the subject, sans-serif supports the task, and mono reports the machine; do not interchange those roles for decoration.

**The 12px Floor.** No visible text is smaller than 12px, controls are at least 14px and reading prose is at least 16px. Font sizes are rem tokens, never px, so the browser's text size setting applies.

## Layout

Desktop uses a narrow navigation ledger beside a flexible workspace capped at a readable maximum width. The ledger is fixed to the viewport height: it stays in place while the workspace scrolls and scrolls internally, with a thin scrollbar and never sideways, when its content is taller. The primary task and status stay above the fold; transcript, calendar, and dialog content remain dense but separated by consistent rhythm. Space follows the 4/8/12/16/24/32 progression, with larger gaps reserved for changes in meaning rather than card decoration.

At 980px and below, the ledger rail widens enough for the wordmark, language and theme controls to remain intact. OpenAI and Google status launchers stay in the sticky rail footer above the GitHub/version block; at 680px and below the launchers join the top bar as two 2.25rem logo buttons, each with its status LED on the top-right corner, after the language and theme toggles on the same row as the wordmark (GitHub/version stays hidden). The row wraps only if it cannot fit, never into page overflow. The calendar opener moves into the workspace header and multi-column content stacks. At 520px and below, the create action becomes an accessible icon control and dialogs use nearly the full viewport. The document must never create horizontal overflow at the supported 320px minimum.

A meeting page is a header and two tabs. The header carries the meeting name once (the page heading above it only says "Cuộc họp"), the date and language hint, and a short status strip, Bản ghi · Transcript · Notes. Its content and the workspace header share the same centered readable column; the header holds only the create action (and Lịch on phones). The tablist sits directly under the header at every width; the Meeting notes panel and the Transcript & bản ghi panel take turns below it, one visible at a time and each as wide as the content area.

In the Transcript tab the player is the one sticky element in the content area, at every width: a single bar, at most 72px tall, pinned to the top edge of that area while the transcript scrolls under it. The Meeting notes tab pins nothing and reserves no room for a bar. On desktop the navigation ledger stays pinned beside the bar; at 680px and below, where the navigation bar scrolls away with the page, the player is the only pinned piece. The recording heading and its hint stay in normal flow and scroll away. Anything the page scrolls to in the Transcript tab stops below the bar: the document scroller reserves the bar's height and each scroll target adds a 12px gap, so a segment never lands under it. The browser scrolls a keyboard-focused control clear of the bar too, but a text field may only reveal its caret line, so part of the field can stay beneath the bar. While audio plays and some of the transcript list shows below the bar, the active segment follows with a nearest-edge scroll, instant under reduced motion; the page holds still while audio is paused, for two seconds after a manual scroll, while a transcript field has focus, and while the whole list is off screen, which includes every moment the Meeting notes tab is open, so reading the notes is never interrupted.

## Elevation & Depth

The system is flat by default. Surface contrast and one-pixel hairlines carry most structure; the only ambient elevation belongs to modal dialogs and the small recording signal tile when it must separate from the canvas.

### Shadow Vocabulary

- **Ambient Tile:** A low-opacity, broad shadow used only beneath the recording signal tile.
- **Modal Lift:** A large soft shadow beneath dialogs, paired with a theme-aware overlay.

### Named Rules

**The Flat-by-Default Rule.** A panel earns elevation only when it floats above the current task; ordinary dashboard regions stay on the ledger plane. The pinned player floats over scrolling content but stays flat: an opaque surface and a hairline separate it, never a shadow.

## Shapes

Corners are compact and engineered: controls use the small radius, content panels use the medium radius, and modal dialogs use the large radius. Circular geometry is reserved for status dots, waveform orbit details, and count badges. Borders remain one pixel; avoid thick accent bars and nested rounded containers.

## Components

### Buttons

- **Shape:** Compact rectangular control with the small radius and a 40px default height.
- **Primary:** Solid accent fill with theme-specific contrast text; reserve it for the next decisive action.
- **Hover / Focus:** A one-pixel upward movement is allowed on hover; keyboard focus uses a two-pixel accent outline. Reduced-motion preferences collapse transitions to effectively instant.
- **Secondary / Text:** Secondary buttons use a neutral surface and strong hairline. Text actions remain transparent and underlined. Running work again over an existing result is secondary and only the first run is primary: **Chạy lại bản ghi** after **Bắt đầu chép lời**, and **Tạo lại notes · …** after **Tạo notes · …**.
- **Cancel:** The cancel control names what it stops (**Hủy chép lời**, **Hủy tạo notes**) and reads **Đang dừng…** once the request is sent.
- **Disabled:** Neutral surface and muted ink, plus the native disabled state; never communicate disabled only through opacity.

### Meeting Tabs

- **Shape:** A WAI-ARIA tablist of two flat tabs under the header, resting on a one-pixel hairline: Meeting notes and Transcript & bản ghi. No boxes or fills. The selected tab takes accent ink, heavier type and a two-pixel accent underline that replaces the hairline beneath it, so colour is never the only cue. The Transcript tab label carries its counts in mono (12px) beside the name, wrapping under it when the row is narrow: the segments and how many of them show the amber review cue, for example "42 đoạn · 5 cần kiểm tra". With no transcript the label has no counts.
- **Behavior:** Roving tabindex, so only the selected tab is a Tab stop; Left and Right move and wrap, Home and End jump, and the selection follows focus. Each change is a history entry (`#notes` or `#transcript`, path and query kept), so Back returns to the previous tab and a reload keeps the tab. With no hash the meeting picks its tab: Meeting notes when it has a saved notes revision or a notes run in progress, otherwise Transcript, and a saved-source link without a hash opens Transcript, where the source card lives. A hash always wins. Until the notes read has answered (four seconds at most) no tab is selected: the notes panel stays hidden and the transcript panel is laid out but transparent, so neither panel flashes and the transcript's controls are already in place when it is the one; the price is that during that short wait those transparent controls can still take keyboard focus, with no visible ring.
- **Focus / motion:** The usual two-pixel accent outline, drawn inside the tab. The colour transition collapses under reduced motion; forced colors draw the selected tab's underline in `Highlight` and none under the other tab.
- **Narrow:** The two tabs share the row; labels wrap and nothing scrolls sideways down to 320px.

### Status Strip

- **Shape:** One wrapping row of three flat chips under the date and language: Bản ghi, Transcript, Notes (13px, one-pixel hairline, small radius, surface fill). Each holds a 6px status dot and one short line, for example "Transcript · Đang chép lời" or "Notes · 2 bản".
- **State:** The words carry the state; the dot (neutral, success, accent, warning, danger) only repeats it. It is a plain list, not a live region.

### Player

- **Shape:** One bar with the small radius, a one-pixel hairline and the surface background, holding only the native audio control and no heading, hint or shadow.
- **Behavior:** Sticky to the top edge of the content area while the Transcript tab is open, opaque so scrolling content never shows through, in light and dark.
- **Across tabs:** Choosing the Meeting notes tab hides the bar but does not pause the audio, so the reader can listen while reading the notes and pauses from the Transcript tab.
- **Focus:** The two-pixel accent outline is drawn inside the bar, so the bar edge and the top of the viewport never clip it.

### Transcript Segments

- **Read mode (default):** One bordered row per segment, on the subtle surface with the medium radius. It holds the timestamp button (mono, 14px, accent; seeks and plays), the speaker name (14px, semibold ink), the text (Inter reading, 16px) and, when the segment needs review, the amber "Cần kiểm tra" cue with its reasons (13px, bordered, never color alone). No field, select or save button is rendered. The playing segment keeps its accent border and soft fill.
- **Sửa:** A secondary button, always visible and never hover-only, at the end of the speaker line. A read row has two tab stops: the timestamp and "Sửa". Its accessible name carries the start time and, with an unsaved draft, "(có bản nháp chưa lưu)".
- **Editor:** "Sửa" swaps only that row for the editor: speaker select, text area (16px), "Lưu đoạn" and "Hủy", and a closed "Chi tiết" disclosure (14px) holding "Gán ban đầu" and the model measures. One editor is open at a time; opening another returns the first row to read mode and keeps its unsaved text. The select saves on change; Esc or "Hủy" closes, Ctrl/⌘+Enter saves from any control in the editor, and focus returns to that row's "Sửa" afterwards. Esc and "Hủy" drop ordinary typed text but keep a conflict-marked draft: the row stays marked, and "Sửa" reopens it with the draft and its review panel.
- **Draft mark:** A row with an unsaved draft shows "Bản nháp chưa lưu" in warning ink on a dashed warning border, so the mark never relies on color alone. After a version conflict, "Tải lại bản mới nhất" sits in the open editor or rename form, and in the conflict panel above the transcript while that row or chip is closed.
- **Locked / past:** A locked source disables "Sửa" with the reason from the generation status line attached by `aria-describedby`; in an open editor the fields and "Lưu đoạn" are disabled and "Hủy" stays usable. A past transcript is read-only: no "Sửa", and each row keeps only the closed "Chi tiết".
- **Feedback:** A row has no live region of its own for save messages. Every "Đã lưu" and error goes to the one shared polite status above the transcript; the in-editor status line is plain text.

### Speaker Chips

- **Shape:** One compact button per speaker (30px minimum height, large radius, strong hairline, surface fill, 14px semibold name) in a wrapping row; hover shifts to the accent border and soft fill.
- **Marks:** A speaker still carrying the system default name ("Speaker N") shows an outlined "đặt tên" mark in accent. A name typed and left unsaved shows "bản nháp". Marks are words, not color alone.
- **Rename:** A chip click swaps only that chip for an inline form on its own line (name field, "Lưu tên", "Hủy", "Khôi phục tên mặc định"; the label is 12px). Enter saves, Esc cancels, one rename is open at a time, and focus returns to the chip. "Thêm người nói" stays below the chips.
- **Disabled / read-only:** A locked source disables chips with muted ink and a plain hairline, tied to the lock reason like "Sửa". In a past transcript chips are plain text.

### Task Progress

- **Bar:** A native `<progress>` under the transcript heading while a transcription task is active: 6px tall, small radius, `max="1"`, an accent fill over a hairline track. Its accessible name is the visible Vietnamese stage label on the line above, with the percent at the end of that line in mono tabular figures.
- **Value:** Transcript end divided by recording length, clamped to 1. The percent never shows 100% before the whole recording is transcribed. Later stages (speaker recognition, saving) leave the bar where the transcript ended and change only the label.
- **Unknown:** Without a usable recording length the bar has no value and no percent: a dashed accent outline that pulses. Reduced motion removes the pulse; the dashed outline remains, so the state never relies on motion or colour alone.
- **Forced colors:** The track becomes a one-pixel `CanvasText` outline and the fill `Highlight`, so the bar stays visible in the user's own palette; the dashed unknown state is already an outline and is unchanged.
- **Data:** It is painted from the existing task poll and adds no request.

### Notes Steps

- **Shape:** One flat ordered list on the subtle surface with a hairline and the small radius: Nhận yêu cầu, Khởi chạy, Tạo notes, Lưu notes. Each row has a small round mark, the step name (13px) and its state word in mono at the end.
- **States:** done (success mark, "Đã xong"), current (accent mark, semibold, "Đang chạy", `aria-current="step"`), upcoming (hollow mark, "Chưa tới"). The step that ended a run reads "Thất bại" in danger ink, or "Đã hủy" / "Bị gián đoạn" in warning ink. The word carries the state; the mark only repeats it.
- **Unknown step:** The backend overwrites the stage when a run ends, so the page remembers the last step it saw running. A run first seen after it ended shows dashed marks without state words and one closing row naming how it ended; it never claims "Đã xong" for steps it cannot vouch for.
- **Meta:** Below a hairline, the elapsed time (ink, mono) and one sub-line of provider · model · effort (secondary ink).

### Processing Details

- A closed `<details>` with the summary "Chi tiết xử lý" (14px, secondary ink, standard two-pixel accent focus outline) holds the compute backend and model profile. Opening it shows the same flat bordered group; nothing is removed, only collapsed.

### Cards / Containers

- **Corner Style:** Medium radius for panels and compact radius for list rows.
- **Background:** One semantic surface step above the canvas.
- **Shadow Strategy:** Flat at rest; see the elevation rules.
- **Border:** One-pixel semantic hairline.
- **Internal Padding:** Usually 16–24px on desktop and 14–18px on mobile.

### Inputs / Fields

- **Style:** Neutral surface, one-pixel border, compact radius, and Inter for ordinary values; tokens, commands, and technical values may use JetBrains Mono.
- **Focus:** Accent outline and/or border shift with sufficient contrast in both themes.
- **Error / Disabled:** Danger copy accompanies invalid state; disabled fields retain readable value text.

### Notes Frame

Saved notes sit in one full-width frame: a hairline box on the subtle surface with the small radius. One toolbar above it serves both modes and holds the version select, the **Xem | Viết** group, the save, copy, and download actions and, as its last full-width row, the notes action (see Notes Action), so no control moves when the mode changes. The group is two toggle buttons with `aria-pressed`; the pressed button takes the accent-soft fill and accent ink, the switch is instant, and keyboard focus is the usual two-pixel accent outline drawn inside the group. Xem renders the draft at the reading size with headings starting at h4, below the panel's h3; Viết shows a full-width mono textarea at least 420px tall on desktop. Unsaved work is marked twice: a warning dot hangs off the **Viết** label while its accessible name says there are unsaved changes, and Xem opens with a thin warning-soft line, "Đang xem bản nháp chưa lưu", so a draft is never mistaken for the saved version. Two polite status lines sit directly under the toolbar, so each message appears beside the button that caused it: the first says whether the draft differs from the saved Markdown, the second reports the result of the last save, copy, or download. The conflict block follows them, above the frame, in both modes. When the saved Markdown holds a source link that opens no saved source, a bordered warning-soft note sits between the metadata line and the frame, saying in words how many such links there are and quoting the first in mono (12px). In Xem each of them is plain text in warning ink with a dashed underline, never an `<a>`, and carries the words “liên kết nguồn không hợp lệ” for assistive technology, so the mark is not colour alone; the Markdown itself is never changed. At 760px and below the toolbar stacks into full-width rows; at 520px and below its actions stack too.

### Notes Citation Chips

In Xem, a bullet that has source segments ends with one chip per segment, in the document's order. A chip is a link (`<a>`) to `/meetings/{id}?notes_source={snapshot}&segment={segment}#transcript` and reads the segment's start time in mono at 14px (a grouped range chip too), `mm:ss`, or `h:mm:ss` from one hour on. Its accessible name is "Nghe lại đoạn nguồn lúc …" with the same time, so the visible label stays inside the name. The chip is a small rounded flat pill: one-pixel accent border, accent-soft fill, accent ink (accent fill and accent-contrast ink on hover), at least 24px tall with a 6px gap after the text, wrapping with the bullet and never widening the frame down to 320px. Each chip is its own Tab stop with the usual two-pixel accent outline, offset two pixels so neighbours do not touch; forced colors draw it in `LinkText`. Nothing about a chip animates.

A chip belongs to a bullet only while the bullet's section heading and text equal what the notes were made with. An edited or moved bullet shows none and other bullets keep theirs, with one exception: bullets with the same heading and identical text cannot be told apart in the Markdown, so the group keeps its chips (each bullet its own, in order) only while the notes hold as many of them as they were made with. Editing or deleting one of the group, or adding a copy of it (including by editing another bullet into one), removes the chips of the whole group rather than risk one on the wrong bullet. Viết, the saved Markdown, copy and download never carry a chip or a link. Revisions made before citations were kept show none.

### Source Card

Opening a chip, or a pasted link, shows **Nguồn đã lưu lúc tạo notes** in the Transcript tab as its own card between the sticky player and the transcript: hairline box on the surface fill with the medium radius, the serif title with **Đóng nguồn** beside it (the button drops under the title on a narrow screen), the standing line that this is the frozen snapshot, a three-cell mono block with the time, the speaker and the meeting date of the snapshot (**Chưa đặt ngày** when it had none; one column at 520px and below), the snapshot's text behind a one-pixel accent rule, and a polite status line. Whenever the card shows, whether from a chip, a pasted link or Back and Forward, focus moves to its title without scrolling. When the current transcript differs from the snapshot a bordered warning-soft note under the text says so in words (the segment text or speaker changed, the transcript is another run, or the meeting date was added, changed or cleared). **Tua bản ghi tới mốc này** seeks without playing.

The transcript row the source names carries a three-pixel accent bar, an accent border and the pill label **Nguồn của mục notes** (13px, accent on accent-soft, bordered), so the mark is text as well as colour and stays while playback moves on. If the segment ID is not in the transcript on screen, the row spanning its start time is marked. The row scrolls into view with the nearest scroll, below the player and clear of the card as far as that allows, and instantly when the reader asked for reduced motion.

### Notes Empty State

Without a complete transcript the Meeting notes tab holds one heading, **Cần transcript trước**, one line saying whether the transcript is being made or still has to be, and a secondary button to the Transcript tab. No action, no **Đổi…** and no select is drawn; the notes action returns with the first complete transcript.

### Notes Action

- **One action:** Under the notes heading one button names the saved choice: **Tạo notes · English · ChatGPT GPT 6.1 Sol** (output language, then provider and model). It is the one primary button of the tab. Once notes exist it reads **Tạo lại notes · …**, turns secondary and moves, with everything that belongs to it, into the notes toolbar as its last row, so saved notes never sit beside a primary button. The group moves as one: the button keeps its identity and any keyboard focus inside the group goes with it. While a run is being sent or is active the button stays where it is, disabled, beside **Hủy tạo notes**.
- **Đổi…:** A text button (14px, underlined, accent) beside the action, with `aria-expanded` and `aria-controls`, opens a flat region under it: the output language, provider and model selects in a wrapping row, the line saying what the next run uses (provider · model · effort), the save feedback, and the description of what is sent. The region is closed on every meeting until opened; opening and closing makes no request and no motion. Saving a choice, and the rule that nothing switches provider or model on its own, are as before.
- **Connect:** When the chosen provider is not connected, or has no usable model, the same button reads **Kết nối ChatGPT để tạo notes** or **Kết nối Google Antigravity để tạo notes** and opens the connections dialog at that provider's section, as its header button does. The line explaining why shows without opening **Đổi…**, with **Tải lại lựa chọn** when the saved choice could not be read. A run in progress keeps its own button. When the dialog reports the provider connected the button becomes the action again in place, so the dialog hands focus back to a control that is still there.
- **Speaker reminder:** While the current transcript still has speakers with the system's default name (“Speaker N”), one line under the action reads **Notes sẽ dùng tên người nói hiện tại: Speaker 1, Speaker 2.** (13px, secondary ink) followed by the link **Đặt tên trước**. The link opens the Transcript tab on the current transcript, a past one on screen giving way to it, and focuses the speaker catalog heading, so the chips are the next Tab stops. The line never disables the action and steps aside while a run holds the source, when names cannot change.
- **Narrow:** The action's long name wraps inside the button. At 520px and below the button takes the full row and **Đổi…** sits below it; nothing scrolls sideways down to 320px.

### Navigation

The ASR Studio identity pairs a monoline speech waveform with a studio fader; the app mastheads, favicon and README logo share this mark. The wordmark reads “ASR Studio”.

Navigation is a quiet ledger rail: mono wordmark, uppercase technical section labels, compact rows, and an accent-soft current state. A compact sun/moon toggle sits beside the wordmark and exposes a dynamic accessible name for the opposite theme. The language switch borrows the portfolio's pattern: a mono button in the same square box that names the other language (**EN** in Vietnamese, **VI** in English), stores the choice and reloads the page. Vietnamese stays the default. On mobile the rail becomes a top bar containing the brand, language and theme toggles, plus the two provider logo buttons on the same row; the access page keeps its existing masthead arrangement.

### Operational Status

Provider connection appears as two equal-width sticky sidebar-footer buttons, OpenAI and Google, directly above the GitHub/version block; on phones they become logo buttons (the OpenAI and Google marks, inline SVG, nothing loaded from a third party) beside the toggles, the status LED on each button's top-right corner and the name hidden visually. Each pairs a status dot with explicit status text (the accessible name on a phone) and its own accessible name; the ChatGPT usage link in the connections dialog is 14px. The runtime profile sits in the closed Processing Details disclosure; it, task measurements, generations, timestamps, and diagnostic IDs use mono metadata inside flat bordered groups. Loading, saved, warning, and error states pair semantic color with explicit language. Transcript and history layouts belong to this product; preserve their scan order when restyling.

## Do's and Don'ts

### Do:

- **Do** use semantic tokens from `design-system.css`; add a role there before adding a page-specific color.
- **Do** load fonts and visual assets from the packaged application only.
- **Do** preserve visible keyboard focus, semantic labels, and reduced-motion behavior.
- **Do** keep operational state compact, textual, and easy to scan in both themes.
- **Do** verify new surfaces at desktop and narrow mobile widths in light and dark modes.

### Don't:

- **Don't** return to the teal, blue-black SaaS dashboard palette.
- **Don't** copy portfolio hero, timeline, or marketing-page composition into the workspace.
- **Don't** use burgundy for success, warning, or danger.
- **Don't** introduce gradients, glass effects, heavy shadows, pill-shaped primary controls, or decorative card grids.
- **Don't** stack uppercase mono overlabels directly above headings; put essential state in operational metadata.
- **Don't** load fonts, icons, CSS, scripts, telemetry, or visual assets from third-party origins at runtime.
