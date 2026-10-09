# BookScan on Ubuntu

This is the capture-first application. Inventory and library migration are independent;
EPUB work follows verified capture and page preparation.

## Start now

From the BookScan repository root:

```bash
./scripts/bookscan
```

The launcher uses the repository virtual environment and creates a new session under
`captures/session-YYYYMMDD-HHMMSS`. For a named book or to resume:

```bash
./scripts/bookscan --session captures/professions-rescan
```

Capture starts in **manual mode**, including after resuming. Position the page, inspect
its full framing, select spread/single page/cover/intentional blank, and click Capture
or press Space outside a text field. Printed page labels are optional and recorded;
the application does not infer page numbers. A capture uses a frame exposed after the
trigger when the driver supplies monotonic timestamps. The original is never resized,
converted to grayscale or recompressed. Preview is a separate reduced display image.

### Resume after a scanner disconnect

Reopen the existing session using the same path. Startup performs a quick, read-only
resume audit by default: it checks each capture's metadata, JPEG markers and dimensions,
file size, and SHA-256 without fully decoding the pixels. A progress window remains
responsive during this audit; **Cancel session audit** stops safely without changing
capture files. The next number is based on verified metadata records. If that path is
already occupied by an incomplete/unverified directory, BookScan stops with a collision
instead of replacing or renumbering anything.

```bash
./scripts/bookscan --session captures/saravali-vol2-20261008/
```

The session can open while the CZUR is disconnected. The status will say **SCANNER
DISCONNECTED**; reconnect the USB cable and press **Reconnect scanner**. This keeps the
session available for review and resumes the stream only after the device is found.
Capture remains manual after reconnect.

For a complete pixel decode as well as metadata/hash verification, use
`--audit-mode full` when opening the GUI, or `--audit` to run a full audit and exit.
For an interrupted/incomplete last capture, use `--audit-mode recovery`: verified
records remain available, while incomplete numbered folders and unpublished `.pending-*`
directories are reported and preserved for inspection. Recovery never repairs, deletes,
renames or overwrites a capture. Review reported items before continuing; if the next
sequence path itself is occupied, preserve it and investigate before resuming.

## Hands-free and retry

1. Choose the page mode and click **Set check regions**. Drag inside printed content, including charts and tables,
   avoiding the gutter, coloured mat, fingers and holders. Draw two boxes for a spread,
   one for a single page, cover or intentional blank. Each mode has independent regions
   saved to the session's `check-config.json`. Switching modes stops hands-free and
   rebuilds checks; Cover never inherits two spread boxes.
2. Set **Continuous stillness (seconds)** and click **Apply stillness**.
   The default is **2.0 seconds**; Apply remembers it in this session's
   `check-config.json`, including after reopening. Existing explicit settings are retained.
   Enable Hands-free. Motion resets the continuous timer; a frame gap over 0.8 seconds
   also resets it. At least three observations are required within this same duration.
   Waiting/checking gives a continuous low tone; it fades in/out and stops when waiting ends. Tone style and test playback are under Advanced / Diagnostics.
3. A verified durable acceptance gives blip and latches that page. There is silence
   while waiting for a page turn. Both distributed interior change and a stable new
   appearance are required before another attempt. Recent geometric matching flags
   the same page after larger repositioning. Similar pages can need manual review.
4. A rejected attempt gives bii-bööö once, shows the reason, and does not advance the
   counter. Correct the page sufficiently to re-settle, or use Retry / recapture.

**Retry / recapture** asks for a reason and permits an intentional duplicate/check
exception. It saves a new immutable capture and records which sequence it retries;
it never replaces a JPEG. **Reject last** appends a review decision, preserving the
original and sequence history. Blank pages are retained with Intentional blank;
identical consecutive blanks may require an intentional manual retry. Automatic
attempts never use a standing override reason from the text field. Manual overrides
cannot bypass JPEG decoding, colour/dimensions, read-back hashing or durable saving.

Pause stops attempts, and preserves the acceptance latch. The waiting-tone style, volume, pitch and mute are under Advanced / Diagnostics; Waiting tone remains available in the normal view. **Test waiting sound** (or Ctrl+B) tests
playback; the result and meaningful playback failures are written separately to
`audio-diagnostics.jsonl`, not the capture event records. Audio errors appear visibly and do not
turn a failed save into an acceptance. Storage/device/decode failures pause capture
with a persistent reason; correct the problem and restart.

### Automatic timing

BookScan is Python with Tkinter/ttk; Pillow displays images and NumPy/OpenCV perform
checks. The stillness value controls only the minimum stable interval. Even at 0.05 seconds, three camera observations are mandatory, so a 15 fps stream needs at least roughly 0.13 seconds from first observation. There is no independent autofocus/exposure lock signal or fixed cooldown. Page-turn detection
runs during observation; accepted/rejected pages remain latched until a genuine
distributed change. Applying a duration clears unfinished settling without clearing
these repeat-suppression latches. Autofocus/exposure changes can reset stillness if
they exceed the configured motion threshold.

After stillness, processing still takes time: frame decoding, preview/holder checks,
recent-page matching, mandatory JPEG verification, read-back hashing and filesystem
syncs. Saving runs on a worker. Success is reported when its completion is polled;
thumbnail generation runs independently. The native stream is negotiated at 15 fps (about 67 ms between frames); the responsive UI polls at a 33 ms target and consumes the newest queued frame. The reduced preview is analyzed once per displayed frame. Expensive fingerprint, geometric/recent duplicate checks and full-resolution JPEG validation/save run on a worker; thumbnail verification/creation uses an independent worker after the durable save. The timing display separates queued, analyzed, saved and thumbnail-ready work. Full-resolution colour and all acceptance checks remain mandatory. Neither optimization weakens original-JPEG verification. Live autofocus,
lighting and disk load can extend the delay; manual capture bypasses only stillness.

## Review saved captures

The compact left panel keeps normal scanning controls visible without scrolling. The dominant live preview sits in the centre, with saved thumbnails in a vertical strip at the right. New sessions default to **3**; sessions without a saved choice retain the previous **1** default when they already contain captures. Choose **1** or **3** to show one thumbnail or the last three, newest first.
The choice is remembered per session in `review-config.json`. Each thumbnail shows
its capture number and saved mode; **Retry #N** identifies a deliberate retry and
**Override** identifies a recorded manual exception. The larger review distinguishes
heuristic overrides from other manual overrides.

Thumbnails are loaded from `000NNN/original.jpg` only after mandatory verification
and durable saving succeed. They are not snapshots of the live preview. Rejected
attempts, pending saves and failed saves do not replace the last successful image.
Reopening first audits the session, then restores the last three verified saved images
and the one/three preference. The last successfully saved mode is restored, with
hands-free still off. Blank and cover originals are reviewed in the same way.

Click a thumbnail for a larger, independent review window. Choose **Fit**, **50%**,
**100%** or **200%**, use the mouse wheel to zoom, and drag to pan. Review shows the
original without positioning/check overlays. Loading, hash/decode verification,
thumbnail creation and zoom rendering run on workers separate from capture saving;
only the small finished images are installed on the UI thread. Review does not write
or recompress originals, and no thumbnail files are added to capture directories.
While a new thumbnail is loading, the previous verified image remains visible.
Review read errors are shown in the panel/window and retain the previous thumbnail.

## Positioning guides and independent checks

Spread, Single page and Blank previews have a thin red fold/centre line, thin green content outlines (text,
charts and tables), grey physical page outlines, and muted ochre margin bands at
16% opacity. Spread outlines meet exactly at the fold: no physical central gap.
Content margins can still leave unprinted space near the fold.

**Adjust positioning guides** edits fold/centre X, page top/height, independent left
and right widths for spreads, single/blank width, and four content margins.
Positions/sizes use percentages of the camera frame; margins use percentages of
individual pages. Exact book measurements are not required. These are rectangular
alignment guides, not automatically detected curved page boundaries. **Show guides**
hides positioning overlays; the short legend remains visible.

**Cover mode is simpler:** it has one adjustable grey rectangular book-placement
outline. It has no red fold line, paired pages, green content outlines or ochre margin
bands. In Cover mode, **Adjust positioning guides** offers only book centre X, top,
width and height. Cover placement is separate from spread/single geometry and from
cover checks. Older guide files migrate their former single-page placement values to
these independent cover settings. Optional cyan dashed check regions remain a separate
explicit diagnostic display; they are not placement guides.

Guide geometry lives in `bookscan/guides.py` and `guide-config.json`. Capture regions
live in `bookscan/checks.py` and `check-config.json`. **Changing a guide does not change
any capture check.** Enable **Show check regions (cyan dashed)** to inspect actual
analysis rectangles. The cyan dashed boxes show the normalized rectangles supplied to the active mode:

| Region shown | Modes | Current default | How the code uses it |
|---|---|---|---|
| Left spread interior | Spread | X 12–46%, Y 18–82% | Contributes to frame-change/stillness and distributed page-turn checks, per-region low-contrast checking, the duplicate fingerprint mask, and the yellow-holder core. |
| Right spread interior | Spread | X 54–88%, Y 18–82% | Same checks as the left spread interior; both sides participate in the broad-change gate. |
| Single-page interior | Single page | X 36–64%, Y 25–75% | One-region motion/stability and page-change signal, contrast/sharpness check, duplicate mask, holder core. |
| Cover interior | Cover | X 36–64%, Y 25–75% | One-region motion/stability and page-change signal, contrast/sharpness check, duplicate mask, holder core. |
| Intentional-blank interior | Intentional blank | X 36–64%, Y 25–75% | Motion/stability, page-change and duplicate mask, holder core. The blank mode skips the printed-content low-contrast rejection. |

**Set check regions** replaces the active mode's region(s): drag two boxes for Spread and one for another mode. The holder check uses the inset central core of each active region. The cyan outlines themselves are only a preview overlay: they do not draw on, crop, mask or alter saved JPEG pixels. Showing/hiding them does not change acceptance. The regions they outline do affect heuristic acceptance/rejection. They are separate from positioning guides; changing a guide does not move a check region. Use **Set check regions** separately when a differently sized
book requires new interiors. Guides and effective regions are independently recorded
on saves, with `guides_affect_checks=false`. All overlays are drawn on a separate
reduced preview copy, never on original JPEGs.

Spread uses two analysis regions. Cover and Single page each use one independent
central default rectangle. Intentional blank has its own single rectangle and skips
print-contrast/sharpness rejection. Blank still requires valid full-resolution colour
JPEG decoding and durable saving; deliberate repeated blanks can need Retry. Defaults
are starting points, not automatic page detection.

**Override rejected…** handles a heuristic rejection that looks acceptable on visual
review. It displays the prior rejection and asks for an explanation, then checks a
fresh frame. Prior rejection metrics/reasons/frame, current warnings and the explanation
are recorded with the new capture. Normal manual suspected duplicates are blocked with
a warning; deliberate retries/overrides remain available. No override bypasses JPEG
validity, required colour/dimensions or durable saving.

## Yellow-holder pause convention

**Yellow holder pauses** is enabled by default. The trial's yellow holder inside a
horizontal core of a check region shows **PAUSED — yellow holder in page interior**
and suppresses manual and automatic capture. Returning it to a margin resumes waiting;
pending manual triggers are discarded, so press Capture again. Automatic capture
settles again and still passes duplicate checks. Accepted-page latches are preserved.
A heuristic override cannot bypass an active holder pause.

The detector uses yellow pixels in the central 40% of each check region's width
(30% inset on either side), requiring 0.1% of total frame pixels by default. It does
not use positioning guides, infer user intent or recognize arbitrary fingers. Yellow
artwork can resemble the holder; book size/check regions, lighting, holder orientation
and other holder colours require calibration. Adjust `holder_core_inset` and
`holder_yellow_fraction`, or explicitly disable the checkbox if artwork is misidentified.
Toggle decisions are logged. OpenCV is required for detection and geometric matching.

Geometric duplicate protection also checks strongly unchanged textured print after
alignment and light smoothing, so changed holders/fingers need not outweigh unchanged
page content. Strong evidence on one page of a spread creates a **suspected duplicate**,
requiring visual review even if the other page has moved or changed. This can flag a
legitimate partial-page change; a recorded manual retry is available. It does not
prove printed page identity.

## Originals and recovery

Each sequence directory contains untouched `original.jpg` and `capture.json`, with
sequence, SHA-256, exact byte size, UTC time, USB identity, negotiated mode, frame/driver
timestamps, startup controls, check thresholds/metrics, page labels and overrides.
Controls are sampled at stream start; auto focus/exposure can vary during streaming.
`events.jsonl` records stream starts, rejected attempts and later review decisions.

Saving uses exclusive staging files, full decode, fsync, read-back hash verification,
and atomic directory publication. The session lock excludes a second application
writer. Linux's exclusive rename is preferred; filesystems without it use ordinary
atomic directory rename under the lock after checking collisions. An existing populated
capture directory cannot be replaced. Interrupted `.pending-*` directories are retained
for diagnosis and never counted. A final directory fsync failure pauses with no blip;
a complete published capture is recovered and verified on restart. Never manually
renumber or remove sequence directories while the session is open.

Default resume checks metadata, JPEG header/markers/dimensions, byte size and SHA-256; it does not fully decode old pixels. `--audit-mode full` requests pixel decoding too. Resume refuses gaps/corruption, reconstructs the last-five duplicate history and latches the latest original.

```bash
./scripts/bookscan --audit --session captures/professions-rescan
./scripts/bookscan --discover
```

Discovery checks USB **1e4f:1309**, then the node's actual capture + streaming capabilities,
excluding metadata nodes. Native V4L2 negotiates, queries and streams **4160 × 3120 MJPG
on one open handle**. It refuses a fallback mode. `--device /dev/videoN` still verifies
USB identity; it is only needed if several scanners are attached. Close other camera
applications first. The implementation follows the kernel's
[format negotiation](https://docs.kernel.org/userspace-api/media/v4l/vidioc-g-fmt.html)
and [mmap streaming](https://docs.kernel.org/userspace-api/media/v4l/mmap.html) interfaces.

## Checks and limits

The current configurable checks cover colour JPEG decoding/dimensions, durable saving,
stillness, recent duplicates, unexpectedly low-contrast/blank interiors and optional
sharpness. Sharpness rejection defaults to disabled to avoid rejecting faint printing
before calibration. Geometric duplicate matching uses features, robust alignment and
pixel comparison, following [OpenCV's matching/homography approach](https://docs.opencv.org/4.x/d1/de0/tutorial_py_feature_homography.html).

This slice does **not** automatically verify complete page boundaries, distinguish
finger occlusion from print, detect all glare, read page numbers or prove a semantic
page turn. Inspect framing and set interiors carefully. Blank/low-feature pages,
large non-rigid movements, shared illustrations and very similar text can remain
ambiguous. All checks are defect detectors, not a guarantee; use a recorded manual
retry for intentional repeats and review the saved sequence. Templates/thresholds
are starting values, not measured universal calibration.

Copy `docs/check-config.example.json`, adjust values, and pass `--config PATH`.
Settings are recorded on each attempt. With no explicit config, a resumed session's
saved region configuration is loaded. Manual mode can run without OpenCV, but
hands-free requires it so geometric duplicate protection is always enabled.

## Installation and validation

Install the Ubuntu system prerequisites, then the Python dependencies and run the capture tests:

```bash
sudo apt install python3-venv python3-tk python3-pil python3-numpy pulseaudio-utils
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-capture.txt
.venv/bin/python -m unittest discover -s tests -v
```

A normal Ubuntu graphical user session with camera-device permissions is required.
`paplay` uses PulseAudio or a compatible PipeWire PulseAudio service. Some tests
need a display; live capture tests require the supported scanner.

Replay accepts full-resolution colour JPEG fixtures and holds each for six seconds:

```bash
./scripts/bookscan --replay /path/to/jpeg-fixtures --session captures/replay-test --mute
```

Keep replay fixtures and real-book captures in separate sessions.
