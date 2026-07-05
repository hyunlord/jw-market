# JW Chat Agent POC Design System

## 1. Atmosphere & Identity

A quiet operational chat console for healthcare market analysis. The signature is a bottom composer with a calm, document-like transcript above it, so PL users can keep asking follow-up questions without losing the input surface.

## 2. Color

### Palette

| Role | Token | Light | Dark | Usage |
| --- | --- | --- | --- | --- |
| Surface/primary | --surface-primary | #F6F7F9 | #101820 | Page background |
| Surface/secondary | --surface-secondary | #FFFFFF | #17212B | Conversation panel |
| Surface/elevated | --surface-elevated | #FBFDFF | #1E2935 | Messages, chart cards |
| Text/primary | --text-primary | #18212F | #F8FAFC | Body and headings |
| Text/secondary | --text-secondary | #667587 | #A9B7C7 | Captions and metadata |
| Border/default | --border-default | #D9E0EA | #334155 | Panels and messages |
| Border/subtle | --border-subtle | #DDE2EA | #263545 | Soft panel outlines |
| Accent/primary | --accent-primary | #17324D | #8CB8E8 | Submit action |
| Accent/hover | --accent-hover | #234766 | #A5CBF4 | Submit hover |
| Accent/soft | --accent-soft | #E9F0F7 | #243447 | Source tags |
| User/message | --message-user | #EEF6F3 | #16312A | User bubble |
| User/border | --message-user-border | #C8DED8 | #285247 | User bubble border |

### Rules

- Use the subdued blue accent only for interactive actions and source affordances.
- Keep analysis content on white or near-white surfaces for table readability.
- Do not introduce decorative gradients or saturated status colors.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Tracking | Usage |
| --- | --- | --- | --- | --- | --- |
| H1 | 24px | 700 | 1.25 | 0 | App title |
| H2 | 20px | 600 | 1.35 | 0 | Markdown sections |
| H3 | 16px | 600 | 1.4 | 0 | Chart titles, subsections |
| Body | 16px | 400 | 1.62 | 0 | Answers and transcript |
| Body/sm | 14px | 400 | 1.5 | 0 | Tables |
| Caption | 12px | 500 | 1.4 | 0 | Sources and metadata |

### Font Stack

- Primary: system UI stack (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, sans-serif)
- Mono: inherited browser monospace for inline code

### Rules

- Keep data tables readable before expressive typography.
- Use tabular-style alignment where the browser default permits it for numeric-heavy content.

## 4. Spacing & Layout

### Base Unit

All spacing derives from 4px.

| Token | Value | Usage |
| --- | --- | --- |
| --space-1 | 4px | Tight inline gaps |
| --space-2 | 8px | Tags and compact gaps |
| --space-3 | 12px | Message padding |
| --space-4 | 16px | Form and section rhythm |
| --space-5 | 20px | Panel padding |
| --space-6 | 24px | Page gutters |
| --space-7 | 28px | Desktop shell padding |

### Grid

- Max content width: 980px
- Shell: viewport-height grid with header, scrollable chat panel, sticky composer
- Mobile: reduce outer padding to 16px and keep controls in a single responsive row where possible

### Rules

- The composer remains visible at the bottom of the viewport.
- The conversation panel owns vertical scrolling; the page should not require body-level scrolling during chat.
- The panel has enough bottom padding so the final message is not hidden behind the composer.

## 5. Components

### Chat Composer

- **Structure**: `<form class="composer">` with one text input and one submit button.
- **Spacing**: `--space-3` to `--space-4`.
- **States**: visible focus ring on input and button, hover/active on button.
- **Accessibility**: native form and button semantics.

### Conversation Panel

- **Structure**: `<section class="chat-panel">` containing sources, transcript, answer, and charts.
- **Spacing**: `--space-5` panel padding and `--space-6` bottom padding.
- **Behavior**: scrolls independently and auto-scrolls on new streamed content.

## 6. Motion & Interaction

| Type | Duration | Easing | Usage |
| --- | --- | --- | --- |
| Micro | 150ms | ease-out | Button hover/active |
| Standard | 200ms | ease-in-out | Focus and source affordances |

### Rules

- Animate only `transform`, `opacity`, and color/background changes.
- Keep scroll behavior smooth unless users request reduced motion through browser settings.

## 7. Depth & Surface

### Strategy

Borders-only with tonal shifts.

| Type | Value | Usage |
| --- | --- | --- |
| Default | 1px solid var(--border-default) | Messages, charts, input |
| Subtle | 1px solid var(--border-subtle) | Chat panel and composer |

No box shadows are required for the POC shell. Depth comes from white surfaces, muted borders, and consistent spacing.
