# BookScan quick start and control reference

The normal scanning view has a compact, non-scrolling control panel on the left, the dominant live preview in the centre, and a vertical strip of saved-original thumbnails on the right. The Advanced / Diagnostics window contains less-used controls and can be opened or closed without changing the scan layout. On resize, the preview uses available central space while the thumbnail strip retains a fixed useful width.

The saved JPEG remains the untouched full-resolution colour MJPEG frame from the scanner. Preview size, guide overlays, check-region outlines, thumbnail rendering and review zoom do not alter it. Automatic quality, holder, duplicate, JPEG-validity and durable-save checks remain active.

## Normal scanning view

| Exact displayed label | Control and action | Default / values | Saved original / checks / persistence |
|---|---|---|---|
| Session path (top) | Shows the open session directory. | Path supplied with `--session`, or the new session path. | Read-only display. |
| `Accepted captures: N` | Count of verified accepted sequences. | Current verified session count. | Increments only after durable save. |
| `Reconnect scanner` | Retries the CZUR connection while keeping the session open. | Available at any time; `F5` also invokes it. | No image/check change; immediate. |
| `Mode` | Read-only dropdown: `spread`, `single page`, `cover`, `intentional blank`. | Last accepted mode in an existing session; otherwise `spread`. | The chosen value is recorded on the next capture and selects that mode's check regions. Mode changes pause Hands-free. Last accepted mode restores on reopen. |
| `Printed page(s)` | Optional free-text label. | Empty; any text. | Recorded with the next capture, no pixel/check effect; cleared after successful save. |
| `Capture [Space]` | Requests a fresh frame for manual capture. Space works outside entries/comboboxes. | Available when connected, unpaused and not already saving. | All checks run; accepted content becomes a new sequence. |
| `Hands-free` | Enables continuous stillness and page-turn gated capture. | Off at launch/resume. | Stillness, broad page-change, quality, holder and duplicate checks remain active. Not restored on reopen. |
| `Pause` | Pauses new manual and automatic capture requests. | Off. | An in-flight save completes safely. Immediate; not persisted. |
| `Continuous seconds` | Editable stillness value. | Session value or `2.0`; positive finite seconds. Spin arrows span `.01`–`30` in `.05` steps. | Changes the stillness duration only; cannot remove the minimum three observations or page-turn/repeat, holder, JPEG or durability checks. |
| `Apply stillness` | Applies and saves the number above. | Requires a positive finite value; unavailable during a save. | Saves to session `check-config.json` and clears unfinished settling while preserving page latches. |
| `Show guides` | Shows/hides positioning guides on the preview. | On. | Overlay only; does not move check regions or alter saved pixels. Immediate, not persisted. |
| `Waiting tone` | Enables/disables the continuous waiting-state sound. | On. | No capture/check effect. Immediate; not persisted. |
| Audio status | Reports backend, playback or error state. | `Audio backend found; not yet tested` if `paplay` is present, otherwise its availability error. | Status only. Test outcomes/failures use a separate `audio-diagnostics.jsonl`, never capture metadata. |
| `Advanced / Diagnostics …` | Opens the scrollable Advanced / Diagnostics window. When open, its label becomes `Advanced / Diagnostics ▾`; click it again or use `Close Advanced / Diagnostics` to close. | Closed by default. | No image/check effect. The normal scanning controls do not require scrolling. |

## Advanced / Diagnostics window

| Exact displayed label | Control and action | Default / values | Effect and persistence |
|---|---|---|---|
| `Override reason` | Free-text explanation for a deliberate exception. | Empty; any text. | Recorded with a permitted manual override; never bypasses JPEG, dimensions, holder or durable-save requirements. |
| `Retry / recapture` | Prompts for a reason and saves a separate new sequence linked to the latest. | Requires confirmation/reason. | Original retained; retry marker appears under its thumbnail. Existing checks still run. |
| `Override rejected…` | Shows rejection reasons and asks why a fresh capture is acceptable. | Available after a heuristic rejection; requires reason. | Prior rejection and reason are recorded; does not disable required validation or holder pause. |
| `Reject last` | Prompts to mark the latest accepted sequence rejected for review. | Available when captures exist. | Appends a decision; never deletes or rewrites an original. |
| `Adjust positioning guides` | Opens mode-specific percentage fields. | Defaults below. | `Apply guides only` or `Apply placement only` changes preview geometry, not checks or saved pixels. |
| `Set check regions` | Drag-selects analysis rectangles: two for Spread, one for other modes. | Independent defaults per mode. | Saves active mode's regions to `check-config.json`; they affect heuristics, not crop/saved pixels. |
| `Show check regions (cyan dashed)` | Shows/hides the active analysis rectangles. | Off. | Overlay visibility alone changes no decision. Region pixels are used by motion/stillness, page-turn, quality, duplicate and holder analysis. Independent from positioning guides. |
| `Yellow holder pauses` | Enables the yellow-holder pause heuristic. | On from default check configuration. | A detected yellow holder in the inset interior pauses capture. Saved to `check-config.json`; immediate. |
| `Show timing details` | Shows measured frame, gate, duplicate, validation, write/fsync, notification, thumbnail and audio stages in the main window. | Off unless stored in session `session-ui.json`. | Diagnostic display only; immediate. |
| `Mute` | Mutes all synthesized sounds. | Off unless launched with `--mute`. | Immediate; no capture/check effect. |
| `Volume` | Sound amplitude slider. | `0.25`; range `0`–`1`. | Immediate; no capture/check effect. |
| `Pitch` | Success/rejection pitch multiplier. | `1`; range `0.5`–`2`. | Immediate; no capture/check effect. |
| `Waiting tone style` | Selects the continuous waiting waveform. | `gentle modulation`; values `gentle modulation`, `pure sine`, `two-tone`. | Persisted for the session in `session-ui.json`; takes effect on subsequent waiting playback. |
| `Test waiting sound` | Plays a short sample of the selected waiting style; `Ctrl+B` also invokes it. | Available any time. | Test result/failure is logged to `audio-diagnostics.jsonl`, not capture records. It can retry after restoring the audio sink. |
| `Audio ready` / dynamic audio status | Shows sound backend status and playback errors. | Current audio state. | Diagnostic only. |
| `Recovery, full audit and replay` | Displays command-line instructions for the existing read-only modes. | Full audit/recovery use `--audit-mode full` / `--audit-mode recovery`; synthetic playback uses `--replay DIRECTORY`. | Run on the next launch; this window cannot start a second process against the open session lock. |
| `Close Advanced / Diagnostics` | Closes the advanced window. | Visible at the bottom of the window. | Returns to the unchanged normal scanning view. |

## Right-side saved-original strip

`Saved originals` is a vertical panel beside the preview and stays visible while scanning. `Thumbnails` offers `1` or `3`; a new empty session defaults to `3`, while an established session without a recorded preference retains the former `1` default. A saved preference is restored from `review-config.json`. Up to the last three verified originals are loaded asynchronously, newest first.

Each thumbnail includes capture number and mode. Captions also show `Retry #N` and/or `Override` where applicable. Rejected, failed or pending captures never replace successful thumbnails. Click one to open a verified-original window with a `Zoom` dropdown (`Fit`, `50%`, `100%`, `200%`); mouse wheel zooms and dragging pans. Review is read-only.

## Positioning guide fields

All values are percentages. Normal modes display `Fold / centre X (%)`, `Page top (%)`, `Page height (%)`, `Left page width (spread) (%)`, `Right page width (spread) (%)`, `Single / blank width (%)`, `Content left margin (%)`, `Content right margin (%)`, `Content top margin (%)`, and `Content bottom margin (%)`. Cover mode shows `Book centre X (%)`, `Book top (%)`, `Book height (%)`, `Book width (%)`. Defaults are fold/centre 50%, top 17%, height 68%, spread left/right widths 32%, single/blank width 34%, margins 10% left/right and 8% top/bottom; cover centre 50%, top 17%, width 34%, height 68%. These guide fields change visual placement only.

The cyan dashed regions are analysis areas, not positioning guides. Spread has left and right interior regions; Single page, Cover and Intentional blank each have one independent region. Their image data affects the corresponding checks, while showing the outlines does not. Full explanation and per-mode defaults are in [capture documentation](capture.md).

## Sound behavior

The waiting feedback is a continuous low tone while the application is waiting for a frame or checking/settling an automatic capture. It has a smooth 150 ms fade-in and a short 40 ms fade-out when waiting ends. The audio worker streams small phase-contiguous PCM blocks and never blocks Tk or capture. The default `gentle modulation` is a 240 Hz sine with very shallow 2.5 Hz amplitude modulation; the alternative pure sine and two-tone versions are available under Advanced / Diagnostics. Success and rejection alerts retain their established sounds. A person still needs to confirm audibility through the actual room speakers; `paplay` accepting a stream cannot prove that it sounds clear or pleasant.
