# Portal UI Design Contract

## 1. Visual Theme

Quiet operational workspace. Dense information is organized for repeated scanning, with neutral surfaces and a restrained cyan accent for interactive focus and source links.

## 2. Color System

- Primary text: `#060B11`; secondary text: `#666A73`; muted text: `#82828D`.
- Main surface: `#FFFFFF`; page surface: `#F8FAFD`; secondary surface: `#F4F7FB`.
- Borders: `#DFE2E7` and `#D1D2D7`.
- Accent and focus: `#007EAD`.
- Success: `#157347`; warning/empty: `#7A3D00`; failure: `#B42318`; quota: `#6F42C1`.
- Do not expose cluster, tool, SQL, credential, or internal URL values in user-facing text.

## 3. Typography

- Use the existing application font stack and `letter-spacing: 0`.
- Panel title: 20px/600; section title: 16px/600; item title: 14px/600.
- Body: 14px/400; metadata and badges: 12px/500.
- Monospace is reserved for exact parameter values and preserved raw output.

## 4. Component Language

- Panels use square-edged operational surfaces with 6-8px radii and 1px borders.
- Source cards remain compact: source, status, count, and elapsed time are always visible.
- Details are collapsed by default but remain in the DOM when hidden.
- INPUT and OUTPUT have explicit headers and distinct neutral surfaces.
- Status is communicated with both Korean text and color; color alone is never the signal.

## 5. Layout

- Keep the existing chat and side-panel split.
- Inspection cards use a single column on narrow panels and two columns for INPUT/OUTPUT when at least 760px is available.
- Tables show 15 rows initially; remaining rows stay in the DOM and are revealed in place.
- Controls keep stable heights and do not shift surrounding content when labels change.

## 6. Interaction

- Source and inspection expansion state is local to the rendered answer and deterministic on remount.
- Keyboard focus uses the existing 2px cyan outline.
- Expand-all/collapse-all controls affect only their current response.
- External links open in a new tab with `noopener noreferrer`; internal sources never fabricate links.

## 7. Responsive Behavior

- At 900px and below, the inspection panel remains the existing stacked lower pane.
- At 720px and below, INPUT and OUTPUT stack, controls wrap, and tables retain horizontal scrolling.
- Text wraps without overlapping badges, counts, or actions.
