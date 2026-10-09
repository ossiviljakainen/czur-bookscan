"""Non-blocking capture alerts and continuous synthesized waiting feedback."""
import json
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np


class Audio:
    RATE = 22050
    CHUNK_SAMPLES = 882  # 40 ms; frequent state checks keep waiting feedback responsive.
    WAIT_VARIANTS = ('gentle modulation', 'pure sine', 'two-tone')

    def __init__(self, volume=0.25, pitch=1.0, muted=False):
        self.volume, self.pitch, self.muted = volume, pitch, muted
        self.waiting = False
        self.waiting_variant = 'gentle modulation'
        self.events = queue.Queue()
        self.diagnostics = queue.Queue()
        self.stop = threading.Event()
        self.available = shutil.which('paplay') is not None
        self.error = None if self.available else 'paplay absent; audio unavailable'
        self.process = None
        self.log_path = None
        self.stream_name = None
        self.stream_queued_at = None
        self.stream_started = None
        self.stream_elapsed = 0.0
        self.stream_phase = 0.0
        self.modulation_phase = 0.0
        self.thread = threading.Thread(target=self.run, daemon=True, name='bookscan-audio')
        self.thread.start()

    def signal(self, name):
        self.events.put((name, time.monotonic()))

    def test_waiting(self):
        # Let the user retry after restoring a sink; backend failure is not permanent.
        self.available = shutil.which('paplay') is not None
        if self.available:
            self.error = None
        self.events.put(('test_waiting', time.monotonic()))

    def log_diagnostic(self, event):
        """Keep deliberate sound tests and meaningful playback results outside capture records."""
        if self.log_path is None or (event.get('name') == 'waiting' and event.get('event') == 'played'):
            return
        entry = dict(utc=datetime.now(timezone.utc).isoformat(), **event)
        try:
            with Path(self.log_path).open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, sort_keys=True) + '\n')
        except OSError as exc:
            self.diagnostics.put(dict(event='log_failed', name=event.get('name'), error=str(exc)))

    def pcm(self, name):
        # Preserve the established success/rejection waveforms byte-for-byte in shape.
        notes = {'accepted': [(1150, .10)], 'rejected': [(740, .16), (0, .05), (280, .30)]}[name]
        chunks = []
        for hz, duration in notes:
            n = int(self.RATE * duration)
            t = np.arange(n) / self.RATE
            envelope = np.minimum(1, np.minimum(t / .008, (duration - t) / .015))
            samples = np.sin(2*np.pi*hz*self.pitch*t) * envelope * self.volume * .65
            chunks.append((samples*32767).astype('<i2').tobytes())
        return b''.join(chunks)

    @staticmethod
    def smoothstep(value):
        value = np.clip(value, 0.0, 1.0)
        return value*value*(3.0-2.0*value)

    def waiting_pcm(self, samples=None, phase=None, modulation_phase=None,
                    elapsed=0.0, fade_out=False, variant=None):
        """One continuous, phase-contiguous chunk; no per-chunk attack or pulse."""
        samples = self.CHUNK_SAMPLES if samples is None else samples
        phase = self.stream_phase if phase is None else phase  # elapsed carrier time, seconds
        modulation_phase = self.modulation_phase if modulation_phase is None else modulation_phase
        variant = variant or self.waiting_variant
        t = np.arange(samples) / self.RATE
        omega = 2*np.pi  # Waiting tone stays low; Pitch only changes success/rejection alerts.
        base_hz = 240.0
        carrier = np.sin(omega*base_hz*(phase+t))
        if variant == 'two-tone':
            carrier = .5*(np.sin(omega*220*(phase+t)) + np.sin(omega*260*(phase+t)))
        elif variant == 'gentle modulation':
            modulation = 1.0 + .03*np.sin(modulation_phase + 2*np.pi*2.5*t)
            carrier *= modulation
        elif variant != 'pure sine':
            raise ValueError(f'Unknown waiting-tone variant: {variant}')
        envelope = self.smoothstep((elapsed+t)/.15)  # Smooth 150 ms fade-in at stream start.
        if fade_out:
            envelope *= 1.0-self.smoothstep(t/max(t[-1], 1/self.RATE))
        level = max(0.0, min(1.0, self.volume))*.20
        pcm = (carrier*envelope*level*32767).astype('<i2').tobytes()
        next_phase = phase + samples/self.RATE
        next_modulation_phase = (modulation_phase + 2*np.pi*2.5*samples/self.RATE) % (2*np.pi)
        return pcm, next_phase, next_modulation_phase

    def _start_stream(self, name, queued_at):
        try:
            self.process = subprocess.Popen(['paplay', '--raw', '--format=s16le',
                '--rate=22050', '--channels=1'], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            self.error = str(exc)
            self.available = False
            event = dict(event='failed', name=name, error=self.error, at=time.monotonic())
            self.diagnostics.put(event); self.log_diagnostic(event)
            return False
        self.stream_name = name
        self.stream_queued_at = queued_at
        self.stream_started = time.monotonic()
        self.stream_elapsed = 0.0
        self.stream_phase = 0.0
        self.modulation_phase = 0.0
        self.processed_samples = 0
        self.diagnostics.put(dict(event='started',name=name,queued_at=queued_at,
            playback_started=self.stream_started,waiting_variant=self.waiting_variant))
        return True

    def _write_waiting_chunk(self, fade_out=False):
        process = self.process
        if process is None or process.poll() is not None:
            return False
        pcm, self.stream_phase, self.modulation_phase = self.waiting_pcm(
            elapsed=self.stream_elapsed, fade_out=fade_out)
        try:
            process.stdin.write(pcm)
            self.stream_elapsed += self.CHUNK_SAMPLES / self.RATE
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _stop_stream(self):
        process = self.process
        name, queued_at, started = self.stream_name, self.stream_queued_at, self.stream_started
        if process is None:
            return
        # Deliver a 40 ms smooth tail, then close the stream; this runs only on the
        # dedicated audio thread and cannot delay capture or Tk.
        if process.poll() is None:
            self._write_waiting_chunk(fade_out=True)
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=.12)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=.12)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        stderr = b''
        if process.stderr:
            try: stderr = process.stderr.read()
            except OSError: pass
        code = process.returncode
        finished = time.monotonic()
        if code not in (0, None) and name != 'test_waiting':
            self.error = stderr.decode(errors='replace').strip() or f'paplay exited {code}'
            self.available = False
            event = dict(event='failed', name=name, queued_at=queued_at,
                         playback_started=started, playback_finished=finished,
                         returncode=code, error=self.error)
        elif name == 'test_waiting':
            if code not in (0, None):
                self.error = stderr.decode(errors='replace').strip() or f'paplay exited {code}'
                self.available = False
                event = dict(event='failed', name=name, queued_at=queued_at,
                    playback_started=started, playback_finished=finished,
                    returncode=code, error=self.error)
            else:
                self.error = None
                event = dict(event='played', name=name, queued_at=queued_at,
                    playback_started=started, playback_finished=finished,
                    queue_delay_seconds=started-queued_at, waiting_variant=self.waiting_variant)
        else:
            event = dict(event='played', name=name, queued_at=queued_at,
                         playback_started=started, playback_finished=finished,
                         queue_delay_seconds=started-queued_at, waiting_variant=self.waiting_variant)
        self.diagnostics.put(event); self.log_diagnostic(event)
        self.process = None
        self.stream_name = None
        self.stream_queued_at = self.stream_started = None

    def _play_alert(self, name, queued_at):
        try:
            started = time.monotonic()
            pcm = self.pcm(name)
            process = subprocess.Popen(['paplay', '--raw', '--format=s16le',
                '--rate=22050', '--channels=1'], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.process = process
            _, error = process.communicate(pcm, timeout=3)
            finished = time.monotonic()
            if process.returncode:
                self.error = error.decode(errors='replace').strip()
                self.available = False
                event = dict(event='failed', name=name, queued_at=queued_at,
                    playback_started=started, playback_finished=finished,
                    returncode=process.returncode, error=self.error)
            else:
                self.error = None
                event = dict(event='played', name=name, queued_at=queued_at,
                    playback_started=started, playback_finished=finished,
                    queue_delay_seconds=started-queued_at)
            self.diagnostics.put(event); self.log_diagnostic(event)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc); self.available = False
            event = dict(event='failed', name=name, error=self.error, at=time.monotonic())
            self.diagnostics.put(event); self.log_diagnostic(event)
            if self.process and self.process.poll() is None:
                self.process.kill(); self.process.wait()
        finally:
            self.process = None

    def run(self):
        test_end = None
        while not self.stop.is_set():
            try:
                name, queued_at = self.events.get(timeout=.005 if self.process else .05)
            except queue.Empty:
                name = None; queued_at = None

            if name in ('accepted', 'rejected'):
                self._stop_stream()
                if self.available and not self.muted:
                    self._play_alert(name, queued_at)
                elif self.muted or not self.available:
                    event = dict(event='skipped', name=name, muted=self.muted,
                                 available=self.available, at=time.monotonic(), error=self.error)
                    self.diagnostics.put(event); self.log_diagnostic(event)
                test_end = None
                continue
            if name == 'test_waiting':
                self._stop_stream()
                if not self.muted and self.available and self._start_stream('test_waiting', queued_at):
                    test_end = time.monotonic()+.6
                else:
                    event = dict(event='skipped', name='test_waiting', muted=self.muted,
                                 available=self.available, at=time.monotonic(), error=self.error)
                    self.diagnostics.put(event); self.log_diagnostic(event)
                    test_end = None
                continue

            test_active = test_end is not None and time.monotonic() < test_end
            should_wait = bool(self.waiting and not self.muted and self.available)
            if self.process and self.stream_name == 'test_waiting' and not test_active:
                self._stop_stream(); test_end = None
            elif self.process and self.stream_name == 'waiting' and not should_wait:
                self._stop_stream()
            elif self.process and self.process.poll() is not None:
                # Detect a backend disappearing during continuous playback.
                self._stop_stream()
            if self.process is None and should_wait:
                self._start_stream('waiting', time.monotonic())
            if self.process is not None and self.stream_name in ('waiting', 'test_waiting'):
                if not self._write_waiting_chunk():
                    self._stop_stream()
                else:
                    time.sleep(self.CHUNK_SAMPLES/self.RATE)

        self._stop_stream()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
