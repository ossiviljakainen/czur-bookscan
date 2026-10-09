# BookScan

BookScan is a Linux desktop capture application for CZUR book scanners. It captures full-resolution colour page images into numbered sessions and preserves each accepted original for later review and processing.

**Status: working beta.** Tested hardware: CZUR Shine 1300A3 Pro / ET13S. The implementation uses Linux USB identity checks and native V4L2/UVC streaming. Other scanner models and non-Linux systems are untested.

## Install on Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-venv python3-tk python3-pil python3-numpy v4l-utils pulseaudio-utils
cd ~/projects/bookscan
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-capture.txt
```

Connect the scanner, ensure the user has camera-device permissions, and start a graphical session. Launch the GUI with:

```bash
./scripts/bookscan
```

A new session is created under `captures/`. To open or resume a named session:

```bash
./scripts/bookscan --session captures/my-session
```

## Capture workflow

BookScan opens in manual mode. Choose spread, single-page, cover, or intentional-blank mode, position the book, inspect the preview and press **Capture** or Space. Hands-free mode waits for stillness and a genuine page change, then applies configured quality, holder, duplicate and persistence checks. Rejected attempts do not advance the sequence. Keyboard shortcuts and recovery options are described in [`docs/bookscan-quick-start.md`](docs/bookscan-quick-start.md) and [`docs/capture.md`](docs/capture.md).

Accepted originals are full-resolution colour JPEG frames. Preview overlays, thumbnails and review zoom do not alter originals. Session metadata includes hashes and capture settings. Reopening a session performs a quick integrity audit; full and recovery audits are available. After a scanner disconnect, the session can be reopened for review and the scanner reconnected without renumbering existing captures.

## Current limitations

This is a working beta, not a guarantee of complete or semantically correct page capture. Heuristics cannot prove page identity, detect every obstruction or glare condition, or replace visual review. Device-specific calibration may be needed. Hardware beyond CZUR Shine 1300A3 Pro / ET13S, other operating systems, and diverse lighting/book conditions have not been tested. Hands-free capture requires OpenCV; manual capture remains available without it.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

A display is needed for the GUI layout test; hardware and display-dependent live checks may be unavailable in headless environments.

## Contributing and issue reports

Contributions are welcome. Please open an issue with your OS and Python versions, scanner model and connection details, the exact steps to reproduce, and relevant sanitized logs. Do not attach book pages, session directories, serial numbers, personal paths, or private capture data. Pull requests should include focused tests and update capture documentation when user-visible behavior changes.
