import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

from . import __version__
from .camera import Camera, discover
from .checks import Config, Gate, signature, quality, duplicate_distance
from .storage import Session, decode, decode_preview
from .matching import fingerprint, same_page, cv2
from .guides import Guides, render
from .holder import parked_holder
from .review import Review, ReviewWindow, caption


class Reader:
    def __init__(self, identity, replay=None, device=None):
        self.identity, self.replay, self.device = identity, replay, device
        self.frames = queue.Queue(maxsize=1)
        self.messages = queue.Queue()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def reconnect(self):
        self.commands.put('reconnect')

    def publish(self, data, metadata, settings):
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put((data, metadata, settings))

    def run(self):
        if self.replay:
            try:
                paths = sorted(Path(self.replay).glob('*.jpg'))
                if not paths:
                    raise ValueError('Replay directory needs full-resolution colour .jpg files.')
                settings = dict(source='replay', files=[str(p) for p in paths])
                self.messages.put(('settings', {'settings': settings, 'identity': {'source': 'replay'}}))
                for index, path in enumerate(paths):
                    data = path.read_bytes()
                    until = time.monotonic() + 6
                    while not self.stop.is_set():
                        self.publish(data, dict(driver_sequence=index, replay_source=str(path),
                                     received_monotonic=time.monotonic()), settings)
                        if self.stop.wait(.15): break
                        if time.monotonic() >= until and index != len(paths)-1: break
            except Exception as exc:
                self.messages.put(('error', str(exc)))
            return
        while not self.stop.is_set():
            camera = None
            reconnect_now = False
            try:
                identity = self.identity or discover(self.device)
                camera = Camera(identity['device'])
                self.identity = identity
                settings = dict(camera.settings)
                self.messages.put(('settings', {'settings': settings, 'identity': identity}))
                while not self.stop.is_set():
                    if not self.commands.empty():
                        self.commands.get_nowait()
                        reconnect_now = True
                        break
                    data, metadata = camera.frame(timeout=5)
                    self.publish(data, metadata, settings)
            except Exception as exc:
                if not self.stop.is_set(): self.messages.put(('error', str(exc)))
            finally:
                if camera: camera.close()
            if self.stop.is_set(): break
            if reconnect_now: continue
            while not self.stop.is_set():
                try:
                    self.commands.get(timeout=.25)
                    break
                except queue.Empty:
                    pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5.5)


def default_review_count(preferences, record_count):
    """Keep established sessions at the former one-image default; new sessions use three."""
    count=preferences.get('count') if preferences else None
    if count in (1,3,'1','3'):
        return str(count)
    return '1' if record_count else '3'


class App:
    def __init__(self, root, args, session, identity, config):
        import tkinter as tk
        from tkinter import ttk
        from .audio import Audio
        self.tk, self.root, self.args, self.session = tk, root, args, session
        self.identity, self.config = identity, config
        self.guides = Guides.load(session.root/'guide-config.json')
        self.last_rejection = None
        self.holder_parked = False
        self.gate = Gate(config)
        self.history = []
        self.fingerprints = []
        self.original_previews = []
        self.selecting = False
        self.selected_rois = []
        self.last_image_size = None
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.review = Review(session.root)
        self.review_windows = []
        self.closed = False
        self.future = None
        self.pending = None
        self.retry_of = None
        self.last_capture_timing = None
        self.fatal = False
        self.camera_failed = False
        self.last_frame = time.monotonic()
        self.started = self.last_frame
        self.last_tick_started=None
        self.latest_frame_timing={}
        self.last_audio_error = None
        self.audio = Audio(args.volume, args.pitch, args.mute)
        self.audio.log_path=session.root/'audio-diagnostics.jsonl'
        review_path=session.root/'review-config.json'
        review_prefs=json.loads(review_path.read_text()) if review_path.exists() else {}
        ui_path=session.root/'session-ui.json'
        ui_prefs=json.loads(ui_path.read_text()) if ui_path.exists() else {}
        self.review_count=tk.StringVar(value=default_review_count(review_prefs,len(session.records)))
        self.debug_timing=tk.BooleanVar(value=bool(ui_prefs.get('show_timing',False)))
        recovery_issues = getattr(session, 'recovery_issues', [])
        self.recovery_notice = (f"Recovery audit found {len(recovery_issues)} incomplete/unverified record(s); "
                                'they were preserved and skipped. Review before continuing. ' if recovery_issues else '')
        self.status = tk.StringVar(value=(self.recovery_notice + 'Opening scanner stream…'))
        self.timing_detail=tk.StringVar(value='')
        self.counter = tk.StringVar(value=f'Accepted captures: {len(session.records)}')
        self.auto = tk.BooleanVar(value=False)  # Always start/resume in manual mode.
        self.pause = tk.BooleanVar(value=False)
        self.mute = tk.BooleanVar(value=args.mute)
        self.wait_tone = tk.BooleanVar(value=True)
        last_mode = session.records[-1].get('kind','spread') if session.records else 'spread'
        self.kind = tk.StringVar(value=last_mode if last_mode in ('spread','single page','cover','intentional blank') else 'spread')
        self.show_guides = tk.BooleanVar(value=True)
        self.show_checks = tk.BooleanVar(value=False)
        self.holder_enabled = tk.BooleanVar(value=config.holder_pause)
        self.label = tk.StringVar()
        self.reason = tk.StringVar()
        root.title('BookScan — CZUR full-resolution originals' + (' — REPLAY' if args.replay else ''))
        self.volume=tk.DoubleVar(value=args.volume);self.pitch=tk.DoubleVar(value=args.pitch)
        self.waiting_variant=tk.StringVar(value=ui_prefs.get('waiting_tone_variant','gentle modulation'))
        if self.waiting_variant.get() not in Audio.WAIT_VARIANTS:self.waiting_variant.set('gentle modulation')
        self.audio.waiting_variant=self.waiting_variant.get()
        self.sound_status=tk.StringVar(value='Audio backend found; not yet tested' if self.audio.available else self.audio.error)
        self.advanced_window=None
        root.geometry('1540x980'); root.minsize(1050,700)
        root.columnconfigure(0,weight=0);root.columnconfigure(1,weight=1);root.rowconfigure(1,weight=1)
        top=ttk.Frame(root,padding=(10,5));top.grid(row=0,column=0,columnspan=2,sticky='ew')
        ttk.Label(top,text=str(session.root)).pack(side='left',fill='x',expand=True)
        ttk.Label(top,textvariable=self.counter).pack(side='right')

        left=ttk.Frame(root,width=290,padding=(6,0,3,2));left.grid(row=1,column=0,sticky='nsw');left.grid_propagate(False)
        left.columnconfigure(0,weight=1)
        self.left_panel=left
        def group(parent,title):
            box=ttk.LabelFrame(parent,text=title,padding=(6,3));box.pack(fill='x',pady=2);return box

        session_box=group(left,'Session')
        ttk.Button(session_box,text='Reconnect scanner',command=self.reconnect_scanner).pack(fill='x')
        mode_box=group(left,'Mode')
        mode_row=ttk.Frame(mode_box);mode_row.pack(fill='x')
        ttk.Label(mode_row,text='Mode').pack(side='left')
        mode=ttk.Combobox(mode_row,textvariable=self.kind,state='readonly',
                          values=['spread','single page','cover','intentional blank'],width=19)
        mode.pack(side='right',fill='x',expand=True);self.kind.trace_add('write',self.mode_changed)
        label_row=ttk.Frame(mode_box);label_row.pack(fill='x',pady=(3,0))
        ttk.Label(label_row,text='Printed page(s)').pack(side='left')
        ttk.Entry(label_row,textvariable=self.label,width=16).pack(side='right',fill='x',expand=True)

        capture_box=group(left,'Capture')
        ttk.Button(capture_box,text='Capture [Space]',command=self.trigger).pack(fill='x',pady=1)
        action_row=ttk.Frame(capture_box);action_row.pack(fill='x')
        ttk.Checkbutton(action_row,text='Hands-free',variable=self.auto,command=self.reset_wait).pack(side='left')
        ttk.Checkbutton(action_row,text='Pause',variable=self.pause,command=self.reset_wait).pack(side='right')

        automation=group(left,'Stillness')
        still_row=ttk.Frame(automation);still_row.pack(fill='x')
        ttk.Label(still_row,text='Continuous seconds').pack(side='left')
        self.stillness=tk.StringVar(value=f'{config.settle_seconds:g}')
        ttk.Spinbox(still_row,textvariable=self.stillness,from_=.01,to=30,increment=.05,width=7).pack(side='right')
        ttk.Button(automation,text='Apply stillness',command=self.apply_stillness).pack(fill='x',pady=(3,0))

        guide_box=group(left,'Preview')
        ttk.Checkbutton(guide_box,text='Show guides',variable=self.show_guides,
                        command=self.redraw_current_preview).pack(anchor='w')

        sound_box=group(left,'Audio status')
        ttk.Checkbutton(sound_box,text='Waiting tone',variable=self.wait_tone).pack(anchor='w')
        ttk.Label(sound_box,textvariable=self.sound_status,wraplength=260).pack(fill='x')
        self.advanced_button=ttk.Button(left,text='Advanced / Diagnostics …',command=self.toggle_advanced)
        self.advanced_button.pack(fill='x',pady=(5,2))

        right=ttk.Frame(root,padding=(4,0,6,3));right.grid(row=1,column=1,sticky='nsew')
        right.columnconfigure(0,weight=1);right.columnconfigure(1,weight=0,minsize=200);right.rowconfigure(0,weight=1)
        self.canvas=tk.Canvas(right,background='#202020',highlightthickness=0)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        self.review_panel=ttk.LabelFrame(right,text='Saved originals',padding=4,width=196)
        self.review_panel.grid(row=0,column=1,sticky='ns',padx=(4,0));self.review_panel.grid_propagate(False)
        review_head=ttk.Frame(self.review_panel);review_head.pack(fill='x')
        ttk.Label(review_head,text='Thumbnails').pack(anchor='w')
        review_count=ttk.Combobox(review_head,textvariable=self.review_count,values=['1','3'],state='readonly',width=4)
        review_count.pack(anchor='w',pady=(2,3));review_count.bind('<<ComboboxSelected>>',self.review_setting)
        self.review_status=tk.StringVar(value='No successful captures yet.')
        ttk.Label(review_head,textvariable=self.review_status,wraplength=165).pack(anchor='w')
        self.review_tiles=ttk.Frame(self.review_panel);self.review_tiles.pack(fill='both',expand=True)
        self.canvas.bind('<ButtonPress-1>', self.region_start)
        self.canvas.bind('<B1-Motion>', self.region_move)
        self.canvas.bind('<ButtonRelease-1>', self.region_end)
        footer=ttk.Frame(root,padding=(8,3));footer.grid(row=2,column=0,columnspan=2,sticky='ew')
        self.status_label=ttk.Label(footer,textvariable=self.status,wraplength=1300)
        self.status_label.pack(fill='x')
        self.timing_label=ttk.Label(footer,textvariable=self.timing_detail,wraplength=1300)
        if self.debug_timing.get():self.timing_label.pack(fill='x')
        self.legend = ttk.Label(footer, text='Red: fold / centre. Green: content. Ochre: margins. Grey: page bounds. Cyan dashed: analysis regions only.',wraplength=1300)
        self.legend.pack(fill='x')
        if self.kind.get() == 'cover':
            self.legend.configure(text='Cover: one grey book-placement outline. Placement changes do not change cover checks. '
                                  'Cyan dashed checks are optional; inspect glare/framing yourself.')
        # Space only captures outside text/combobox widgets.
        root.bind('<space>', self.space)
        root.bind('<F5>',lambda _e:self.reconnect_scanner())
        root.bind('<Control-b>',lambda _e:self.test_waiting_sound())
        root.protocol('WM_DELETE_WINDOW', self.close)
        for record in session.records[-5:]:
            path = session.root / f"{record['sequence']:06d}" / 'original.jpg'
            image = decode_preview(path.read_bytes())
            self.history.append(signature(image, config, self.kind.get()))
            self.fingerprints.append(fingerprint(image, config, self.kind.get()))
            self.original_previews.append(image)
        if self.history:
            self.gate.latch(self.history[-1], accepted=True)
        for record in session.records[-3:]:
            self.review.submit(record)
        self.reader = Reader(identity, args.replay, getattr(args, 'device', None))
        self.tick_after = root.after(50, self.tick)

    def toggle_advanced(self):
        if self.advanced_window and self.advanced_window.winfo_exists():
            self.advanced_window.destroy();self.advanced_window=None
            self.advanced_button.configure(text='Advanced / Diagnostics …')
            return
        from tkinter import ttk
        dialog=self.tk.Toplevel(self.root);self.advanced_window=dialog
        dialog.title('Advanced / Diagnostics');dialog.geometry('620x760');dialog.minsize(500,560)
        dialog.protocol('WM_DELETE_WINDOW',self.close_advanced)
        outer=ttk.Frame(dialog,padding=6);outer.pack(fill='both',expand=True)
        canvas=self.tk.Canvas(outer,highlightthickness=0)
        scroll=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        content=ttk.Frame(canvas,padding=5);window=canvas.create_window((0,0),window=content,anchor='nw')
        content.bind('<Configure>',lambda _e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        def group(title):
            box=ttk.LabelFrame(content,text=title,padding=7);box.pack(fill='x',pady=4);return box

        labels=group('Labels and review actions')
        ttk.Label(labels,text='Override reason').pack(anchor='w')
        ttk.Entry(labels,textvariable=self.reason).pack(fill='x')
        for text,command in [('Retry / recapture',self.retry),('Override rejected…',self.override_rejected),('Reject last',self.reject_last)]:
            ttk.Button(labels,text=text,command=command).pack(fill='x',pady=2)
        ttk.Label(labels,text='Retries and overrides save a new sequence; the prior original is retained.',wraplength=540).pack(anchor='w')

        guides=group('Guides and check-region diagnostics')
        ttk.Button(guides,text='Adjust positioning guides',command=self.adjust_guides).pack(fill='x',pady=2)
        ttk.Button(guides,text='Set check regions',command=self.select_regions).pack(fill='x',pady=2)
        ttk.Checkbutton(guides,text='Show check regions (cyan dashed)',variable=self.show_checks,
                        command=self.redraw_current_preview).pack(anchor='w')
        ttk.Checkbutton(guides,text='Yellow holder pauses',variable=self.holder_enabled,
                        command=self.holder_setting).pack(anchor='w')
        ttk.Label(guides,text='Cyan boxes are analysis areas, separate from positioning guides; see the quick-start guide.',wraplength=540).pack(anchor='w')

        timing=group('Timing diagnostics')
        ttk.Checkbutton(timing,text='Show timing details',variable=self.debug_timing,
                        command=self.save_ui_preferences).pack(anchor='w')
        ttk.Label(timing,text='Shows frame, gate, duplicate, validation, durable-save, notification and thumbnail stages in the main window.',wraplength=540).pack(anchor='w')

        sound=group('Audio diagnostics')
        ttk.Checkbutton(sound,text='Mute',variable=self.mute).pack(anchor='w')
        row=ttk.Frame(sound);row.pack(fill='x',pady=2)
        ttk.Label(row,text='Volume').pack(side='left');ttk.Scale(row,variable=self.volume,from_=0,to=1).pack(side='left',fill='x',expand=True,padx=5)
        ttk.Label(row,text='Pitch').pack(side='left');ttk.Scale(row,variable=self.pitch,from_=.5,to=2).pack(side='left',fill='x',expand=True,padx=5)
        variant_row=ttk.Frame(sound);variant_row.pack(fill='x',pady=2)
        ttk.Label(variant_row,text='Waiting tone style').pack(side='left')
        variant=ttk.Combobox(variant_row,textvariable=self.waiting_variant,state='readonly',
                             values=['gentle modulation','pure sine','two-tone'],width=22)
        variant.pack(side='right');variant.bind('<<ComboboxSelected>>',self.waiting_variant_changed)
        ttk.Button(sound,text='Test waiting sound',command=self.test_waiting_sound).pack(fill='x',pady=2)
        ttk.Label(sound,textvariable=self.sound_status,wraplength=540).pack(anchor='w')

        recovery=group('Recovery, full audit and replay')
        ttk.Label(recovery,text='Read-only audit on next launch: ./scripts/bookscan --session "SESSION_PATH" --audit-mode full|recovery',wraplength=540).pack(anchor='w')
        ttk.Label(recovery,text='Synthetic/replay testing: ./scripts/bookscan --replay DIRECTORY. These options do not start while this window owns the session lock.',wraplength=540).pack(anchor='w')
        self.advanced_button.configure(text='Advanced / Diagnostics ▾')
        ttk.Button(outer,text='Close Advanced / Diagnostics',command=self.close_advanced).pack(fill='x',pady=(5,0))

    def close_advanced(self):
        if self.advanced_window and self.advanced_window.winfo_exists():self.advanced_window.destroy()
        self.advanced_window=None
        if hasattr(self,'advanced_button'):self.advanced_button.configure(text='Advanced / Diagnostics …')

    def waiting_variant_changed(self,_event=None):
        self.audio.waiting_variant=self.waiting_variant.get()
        self.save_ui_preferences()

    def mode_changed(self, *_):
        # A mode change cannot leave signatures of incompatible shape or silently
        # reuse spread regions for a single cover. Pause auto until explicitly enabled.
        if self.future:
            self.kind.set(self.pending_kind)
            return
        self.selecting = False
        self.auto.set(False)
        self.pending = None
        self.last_rejection = None
        self.rebuild_checks()
        self.legend.configure(text=('Cover: one grey book-placement outline. Placement changes do not change cover checks. '
                                    'Cyan dashed checks are optional; inspect glare/framing yourself.' if self.kind.get() == 'cover'
            else 'Red: fold / centre. Green: content (text, charts, tables). Ochre: margins. Grey: physical pages. '
                 'Guides do not change checks; cyan dashed boxes are checks.'))
        self.status.set('Mode changed: independent check regions loaded. Inspect cyan boxes before capture.')

    def review_setting(self, _event=None):
        try:
            (self.session.root/'review-config.json').write_text(json.dumps({'count':int(self.review_count.get())})+'\n')
        except OSError as exc:
            self.review_status.set('Review preference could not be saved: '+str(exc))
        self.draw_reviews()

    def save_ui_preferences(self):
        path=self.session.root/'session-ui.json'
        temp=path.with_name('.'+path.name+'.tmp')
        try:
            with temp.open('w',encoding='utf-8') as handle:
                json.dump({'show_timing':bool(self.debug_timing.get()),'waiting_tone_variant':self.waiting_variant.get()},handle)
                handle.write('\n');handle.flush();os.fsync(handle.fileno())
            os.replace(temp,path)
            from .storage import sync_dir
            sync_dir(path.parent)
            if self.debug_timing.get():self.timing_label.pack(fill='x')
            else:self.timing_label.pack_forget()
        except OSError as exc:
            self.status.set('Timing display preference could not be saved: '+str(exc))

    @staticmethod
    def format_timing(timing):
        if not timing:return 'No timing data yet.'
        labels=('camera_wait_seconds','camera_dequeue_copy_seconds','camera_frame_total_seconds',
                'scheduled_ui_poll_interval_seconds','frame_queue_age_seconds',
                'reduced_frame_decode_seconds','check_region_motion_signature_seconds',
                'preview_resize_render_seconds','holder_detection_seconds','motion_gate_seconds',
                'check_region_quality_seconds','capture_worker_queue_delay_seconds',
                'fingerprint_seconds','repeat_suppression_seconds','input_full_decode_validate_seconds',
                'input_hash_seconds','original_file_write_seconds','original_file_fsync_seconds',
                'readback_read_seconds','readback_hash_seconds','readback_header_seconds',
                'metadata_file_write_seconds','metadata_file_fsync_seconds',
                'staging_directory_fsync_seconds','exclusive_publish_seconds',
                'session_directory_fsync_seconds','durable_save_seconds',
                'notification_poll_delay_seconds','first_stable_to_durable_save_seconds',
                'first_stable_to_notification_seconds','thumbnail_queue_delay_seconds',
                'thumbnail_hash_seconds','thumbnail_full_decode_seconds','thumbnail_resize_seconds',
                'thumbnail_total_seconds','success_audio_queue_delay_seconds',
                'success_audio_playback_seconds')
        parts=[]
        for key in labels:
            value=timing.get(key)
            if isinstance(value,(int,float)):parts.append(f'{key.replace("_seconds", "")}={value*1000:.0f}ms')
        if 'mandatory_observation_count' in timing:parts.append(f"observations={timing['mandatory_observation_count']}")
        if 'gate_state' in timing:parts.append(f"gate={timing['gate_state']}")
        if timing.get('jpeg_encoding_note'):parts.append('jpeg=hardware MJPEG (no app encode)')
        return 'Timing: '+', '.join(parts)

    def test_waiting_sound(self):
        self.audio.muted=self.mute.get();self.audio.volume=self.volume.get();self.audio.pitch=self.pitch.get()
        self.audio.waiting_variant=self.waiting_variant.get()
        self.audio.test_waiting()

    def redraw_current_preview(self):
        if getattr(self,'current_preview_image',None) is not None:
            self.show(self.current_preview_image)

    def apply_stillness(self):
        import math
        from tkinter import messagebox
        if self.future:
            self.status.set('Wait for the current save before changing continuous stillness.')
            return
        try:
            seconds=float(self.stillness.get())
            if not math.isfinite(seconds) or seconds<=0:
                raise ValueError('Enter a positive, finite number of seconds.')
        except ValueError as exc:
            messagebox.showerror('Continuous stillness',str(exc),parent=self.root)
            return
        old=self.config.settle_seconds
        self.config.settle_seconds=seconds
        try:
            self.config.save(self.session.root/'check-config.json')
            self.session.event('continuous_stillness_changed',seconds=seconds,previous_seconds=old)
        except OSError as exc:
            self.config.settle_seconds=old
            self.fail('Could not persist stillness setting: '+str(exc))
            return
        self.pending=None
        self.gate.reset_settling()  # Preserve accepted/rejected page latches.
        self.stillness.set(f'{seconds:g}')
        self.status.set(f'Continuous stillness set to {seconds:g} seconds and saved. Repeat suppression unchanged.')

    def draw_reviews(self):
        from tkinter import ttk
        from PIL import ImageTk
        for child in self.review_tiles.winfo_children():child.destroy()
        self.review_photos=[]
        for row,(record,image) in enumerate(reversed(self.review.items[-int(self.review_count.get()):])):
            photo=ImageTk.PhotoImage(image,master=self.root);self.review_photos.append(photo)
            button=ttk.Button(self.review_tiles,image=photo,command=lambda record=record:self.open_review(record))
            button.grid(row=row*2,column=0,padx=3,pady=(4,0),sticky='ew')
            ttk.Label(self.review_tiles,text=caption(record,compact=True),wraplength=155,justify='left').grid(row=row*2+1,column=0,sticky='w',padx=3,pady=(0,4))
        self.review_status.set(('Review error: '+self.review.error) if self.review.error else
                               'Verified saved JPEGs' if self.review.items else 'No successful captures yet.')

    def open_review(self, record):
        self.review_windows=[window for window in self.review_windows if not window.closed]
        self.review_windows.append(ReviewWindow(self.root,self.session.root,record))

    def holder_setting(self):
        self.config.holder_pause=self.holder_enabled.get()
        self.pending=None
        self.gate.reset_settling()
        try:
            self.session.event('holder_pause_setting',enabled=self.config.holder_pause)
            (self.session.root/'check-config.json').write_text(json.dumps(asdict(self.config),indent=2)+'\n')
        except OSError as exc:
            self.fail(str(exc))

    def rebuild_checks(self):
        kind = self.kind.get()
        self.history = [signature(image, self.config, kind) for image in self.original_previews]
        self.fingerprints = [fingerprint(image, self.config, kind) for image in self.original_previews]
        self.gate = Gate(self.config)
        if self.history:
            self.gate.latch(self.history[-1], accepted=True)

    def adjust_guides(self):
        from tkinter import ttk, messagebox
        dialog = self.tk.Toplevel(self.root)
        cover = self.kind.get() == 'cover'
        dialog.title(('Cover placement' if cover else 'Positioning guides')+' — independent of capture checks')
        variables = {}
        labels = dict(fold='Fold / centre X', top='Page top', height='Page height',
                      left_width='Left page width (spread)', right_width='Right page width (spread)',
                      single_width='Single / blank width', margin_left='Content left margin',
                      margin_right='Content right margin', margin_top='Content top margin',
                      margin_bottom='Content bottom margin',cover_center='Book centre X',
                      cover_top='Book top',cover_width='Book width',cover_height='Book height')
        settings=asdict(self.guides)
        fields=['cover_center','cover_top','cover_height','cover_width'] if cover else [name for name in settings if not name.startswith('cover_')]
        for i,name in enumerate(fields):
            value=settings[name]
            ttk.Label(dialog,text=labels[name]+' (%)').grid(row=i,column=0,sticky='w',padx=8,pady=3)
            var=self.tk.StringVar(value=f'{value*100:.1f}');variables[name]=var
            ttk.Entry(dialog,textvariable=var,width=10).grid(row=i,column=1,padx=8)
        ttk.Label(dialog,text=('Cover position/size: % of frame. Apply changes placement only; checks stay unchanged.' if cover else
                  'Page sizes/position: % of frame. Margins: % of each page.\nSpread pages meet exactly at fold. Apply changes guides only.')).grid(row=len(fields),column=0,columnspan=2,padx=8,pady=8)
        def apply():
            try:
                guides=Guides(**(settings | {name:float(var.get())/100 for name,var in variables.items()})).validate()
                (self.session.root/'guide-config.json').write_text(json.dumps(asdict(guides),indent=2)+'\n')
                self.guides=guides
                self.session.event('positioning_guides_changed', guides=asdict(guides), checks_changed=False)
            except (ValueError,OSError) as exc:
                messagebox.showerror('Guide settings',str(exc),parent=dialog)
                return
            self.status.set('Positioning guides updated. Capture check regions are unchanged.')
            dialog.destroy()
        ttk.Button(dialog,text='Apply placement only' if cover else 'Apply guides only',command=apply).grid(row=len(fields)+1,column=0,columnspan=2,pady=8)

    def override_rejected(self):
        from tkinter import simpledialog
        if not self.last_rejection or self.future or self.fatal or getattr(self,'camera_failed',False) or self.pause.get():
            self.status.set('Capture first; this button overrides a heuristic rejection after visual review.')
            return
        rejection=self.last_rejection
        reason=simpledialog.askstring('Override heuristic rejection',
            'Rejected because:\n'+'\n'.join(rejection['reasons'])+
            '\n\nInspect the page. Why is this capture acceptable? A fresh original will be checked and saved.',parent=self.root)
        if reason and reason.strip():
            self.reason.set(reason.strip())
            self.trigger()
            if self.pending:
                self.pending['overridden_rejection']=rejection

    def select_regions(self):
        if self.future or self.fatal or self.last_image_size is None:
            return
        self.pause.set(True)
        self.auto.set(False)
        self.reset_wait()
        self.selecting = True
        self.show_checks.set(True)
        self.selected_rois = []
        self.status.set('Drag a box INSIDE each printed page, excluding gutter, fingers and mat. '
                        + ('Draw two boxes.' if self.kind.get() == 'spread' else 'Draw one box.'))

    def region_start(self, event):
        if self.selecting:
            self.drag_start = (event.x, event.y)

    def region_move(self, event):
        if self.selecting and hasattr(self, 'drag_start'):
            self.canvas.delete('selection')
            self.canvas.create_rectangle(*self.drag_start, event.x, event.y,
                                         outline='#ffd166', width=3, tags='selection')

    def region_end(self, event):
        if not self.selecting or not hasattr(self, 'drag_start'):
            return
        x, y, w, h = self.last_image_size
        a, b = self.drag_start
        box = [max(0, min(1, (min(a,event.x)-x)/w)),
               max(0, min(1, (min(b,event.y)-y)/h)),
               max(0, min(1, (max(a,event.x)-x)/w)),
               max(0, min(1, (max(b,event.y)-y)/h))]
        if box[2]-box[0] < .04 or box[3]-box[1] < .04:
            return
        self.selected_rois.append(box)
        needed = 2 if self.kind.get() == 'spread' else 1
        if len(self.selected_rois) < needed:
            self.status.set('First interior selected; drag the second printed-page interior.')
            return
        if self.kind.get() == 'spread':
            self.config.rois = self.selected_rois.copy()
        else:
            self.config.mode_rois[self.kind.get()] = self.selected_rois.copy()
        self.rebuild_checks()
        self.selecting = False
        try:
            self.session.event('analysis_regions_changed', kind=self.kind.get(), rois=self.config.regions(self.kind.get()))
            # User config, separate from immutable capture metadata; replaces only this setting file.
            path = self.session.root / 'check-config.json'
            path.write_text(json.dumps(asdict(self.config), indent=2)+'\n')
        except OSError as exc:
            self.fail(str(exc))
            return
        self.pause.set(False)
        self.status.set('Page interiors set. Manual capture ready; enable Hands-free when positioned.')

    def space(self, event):
        if event.widget.winfo_class() not in ('TEntry', 'Entry', 'TCombobox'):
            self.trigger()
            return 'break'

    def reset_wait(self):
        if self.auto.get() and cv2 is None:
            self.auto.set(False)
            self.status.set('Hands-free requires OpenCV. Use the project .venv or install requirements-capture.txt.')
        self.pending = None
        self.gate.reset_settling()  # Do not release acceptance/rejection latch.

    def trigger(self, retry_of=None):
        if self.fatal or getattr(self,'camera_failed',False) or self.future or self.pause.get():
            return
        if self.holder_parked:
            self.status.set('PAUSED — yellow holder in page interior. Move it to the margin, then capture.')
            return
        self.pending = dict(requested_monotonic=time.monotonic(), trigger='manual',
                            kind=self.kind.get(), printed_pages=self.label.get(),
                            override_reason=self.reason.get().strip(), retry_of=retry_of)
        self.status.set('Waiting for a fresh full-resolution frame…')

    def retry(self):
        from tkinter import simpledialog
        if self.fatal or getattr(self,'camera_failed',False) or self.future or self.pause.get():
            return
        reason = simpledialog.askstring('Manual retry', 'Why recapture this page? Original remains saved.',
                                       initialvalue='Manual recapture', parent=self.root)
        if reason:
            self.reason.set(reason)
            self.trigger(self.session.records[-1]['sequence'] if self.session.records else None)

    def reject_last(self):
        from tkinter import simpledialog
        if not self.session.records or self.future or self.fatal or getattr(self,'camera_failed',False):
            return
        reason = simpledialog.askstring('Reject last', 'Reason (original will be retained):', parent=self.root)
        if reason:
            try:
                self.session.event('reject_acceptance', sequence=self.session.records[-1]['sequence'], reason=reason)
                self.status.set('Last marked rejected for review; original retained. Use Retry / recapture.')
            except OSError as exc:
                self.fail(str(exc))

    def fail(self, reason, camera=False):
        if camera:
            self.camera_failed = True
            self.pending = None
            self.auto.set(False)
            self.audio.waiting = False
            self.status.set(getattr(self,'recovery_notice','') + 'SCANNER DISCONNECTED — session remains open; reconnect the USB cable, then choose Reconnect scanner.')
            return
        self.fatal = True
        self.pending = None
        self.auto.set(False)
        self.audio.waiting = False
        if hasattr(self.reader, 'stop'):
            self.reader.stop.set()
        self.status.set('PAUSED — ' + reason + ' Restart after correcting the problem.')
        self.audio.signal('rejected')

    def reconnect_scanner(self):
        if self.args.replay or self.fatal:
            return
        self.camera_failed = True
        self.last_frame = time.monotonic()
        self.status.set('Reconnecting to CZUR scanner… session remains open.')
        self.reader.reconnect()

    def attempt(self, data, frame, settings, sig, options, image=None, holder=None):
        image = decode_preview(data) if image is None else image
        holder = parked_holder(image, self.config, options['kind']) if holder is None else holder
        if holder['paused']:
            self.pending = None
            self.gate.reset_settling()
            self.status.set('PAUSED — yellow holder in page interior. Move it to the margin, then capture.')
            return
        options=dict(options)
        snapshot=dict(config=Config(**asdict(self.config)),guides=asdict(self.guides),device=self.identity,
            history=list(self.history),fingerprints=list(self.fingerprints),
            audio_config=dict(volume=self.volume.get(),pitch=self.pitch.get(),muted=self.mute.get(),
                              waiting_tone=self.wait_tone.get()),last_rejection=self.last_rejection)
        self.saving_sig = sig.copy()
        self.pending_kind = options['kind']
        self.saving_preview = image.copy()
        self.save_submitted_monotonic=time.monotonic()
        self.save_completed_monotonic=None
        self.saving_timing=dict(getattr(self,'latest_frame_timing',{}),**options.get('automatic_timing',{}),
                                qualifying_attempt_submitted_monotonic=self.save_submitted_monotonic)
        self.status.set('Qualifying page — checking repeat protection and saving the full-resolution original…')
        self.audio.waiting = self.wait_tone.get()
        self.future = self.pool.submit(self.verify_and_save,data,frame,settings,sig,options,image,holder,
                                       snapshot,self.saving_timing,self.save_submitted_monotonic)
        self.future.add_done_callback(self.note_save_completed)

    def verify_and_save(self,data,frame,settings,sig,options,image,holder,snapshot,base_timing,submitted):
        started=time.monotonic();timing=dict(base_timing)
        timing['capture_worker_queue_delay_seconds']=started-submitted
        stage=time.monotonic();metrics,reasons=quality(sig,snapshot['config'],options['kind'])
        timing['check_region_quality_seconds']=time.monotonic()-stage
        stage=time.monotonic();current_fingerprint=fingerprint(image,snapshot['config'],options['kind'])
        timing['fingerprint_seconds']=time.monotonic()-stage
        stage=time.monotonic()
        geometry=[same_page(old,current_fingerprint,snapshot['config'].geometric_duplicate_difference)
                  for old in snapshot['fingerprints']]
        metrics['geometric_comparisons']=geometry
        if any(result['geometric_duplicate'] for result in geometry):
            reasons.append('Same page recognized after repositioning; use Retry / recapture for an intentional repeat.')
        elif any(result.get('suspected_duplicate') for result in geometry):
            reasons.append('Suspected duplicate: substantial printed content is unchanged despite holder/page movement. Inspect and use Override rejected / Retry if deliberate.')
        if any(duplicate_distance(sig,old)<snapshot['config'].duplicate_difference for old in snapshot['history']):
            reasons.append('Possible recent duplicate; use Retry / recapture or an explicit override reason.')
        timing['repeat_suppression_seconds']=time.monotonic()-stage
        override=options.get('override_reason','') if options['trigger']=='manual' else ''
        last_rejection=snapshot['last_rejection']
        if override and last_rejection and last_rejection['kind']==options['kind']:
            options.setdefault('overridden_rejection',last_rejection)
        timing['focus_exposure_stability']='Not measured independently; image changes feed the motion heuristic.'
        record=dict(app_version=__version__,device=snapshot['device'],capture_settings=settings,
            check_timing=dict(started_monotonic=started,seconds=timing['check_region_quality_seconds']),
            positioning_guides=snapshot['guides'],guides_affect_checks=False,
            effective_check_regions=snapshot['config'].regions(options['kind']),holder_check=holder,
            audio_config=snapshot['audio_config'],
            controls_note='Control values sampled at stream start; automatic controls may vary.',
            frame=frame,check_config=asdict(snapshot['config']),check_metrics=metrics,
            check_warnings=reasons,performance_timing=dict(timing),**options)
        if reasons and not override:
            rejection=dict(reasons=reasons,frame=frame,kind=options['kind'],metrics=metrics,
                           check_regions=snapshot['config'].regions(options['kind']))
            event_started=time.monotonic();self.session.event('rejected_attempt',**rejection)
            timing['rejection_event_fsync_seconds']=time.monotonic()-event_started
            timing['worker_finished_monotonic']=time.monotonic()
            return dict(accepted=False,rejection=rejection,timing=timing,fingerprint=current_fingerprint)
        timing['jpeg_encoding_seconds']=0.0
        timing['jpeg_encoding_note']='Camera supplies MJPEG; captured encoded bytes are stored without re-encoding.'
        save_started=time.monotonic();record['performance_timing']=dict(timing)
        accepted=self.session.accept(data,record)
        timing['durable_save_seconds']=time.monotonic()-save_started
        timing.update(self.session.last_accept_timing)
        timing['worker_finished_monotonic']=time.monotonic()
        return dict(accepted=True,record=accepted,timing=timing,fingerprint=current_fingerprint)

    def note_save_completed(self, _future):
        # Worker-side timestamp only; no Tk operations and no additional waits.
        self.save_completed_monotonic=time.monotonic()

    def show(self, image):
        from PIL import ImageTk, Image
        self.current_preview_image=image
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale=min(w/image.width,h/image.height,1)
        image=image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.BILINEAR)
        image = render(image, self.guides, self.kind.get(), self.show_guides.get(),
                       self.config.regions(self.kind.get()) if self.show_checks.get() else ())
        self.preview = ImageTk.PhotoImage(image)
        self.canvas.delete('all')
        x, y = (w-image.width)//2, (h-image.height)//2
        self.last_image_size = (x, y, image.width, image.height)
        self.canvas.create_image(x, y, anchor='nw', image=self.preview)

    def tick(self):
        if getattr(self, 'closed', False):
            return
        tick_started=time.monotonic()
        previous_tick=getattr(self,'last_tick_started',None)
        scheduled_poll_seconds=(tick_started-previous_tick) if previous_tick else None
        self.last_tick_started=tick_started
        if self.review.poll():
            self.draw_reviews()
            if self.debug_timing.get() and getattr(self.review,'last_timing',None):
                self.timing_detail.set(self.format_timing(dict(self.last_capture_timing or {},**self.review.last_timing)))
        self.audio.muted, self.audio.volume, self.audio.pitch = self.mute.get(), self.volume.get(), self.pitch.get()
        diagnostics=getattr(self.audio,'diagnostics',None)
        while diagnostics is not None and not diagnostics.empty():
            audio_event=diagnostics.get_nowait()
            if audio_event['event']=='started':
                self.sound_status.set(('Waiting sound active' if audio_event['name']=='waiting'
                    else 'Testing waiting sound')+' — '+audio_event.get('waiting_variant',''))
            elif audio_event['event']=='played':
                self.sound_status.set(f"{audio_event['name']} sound sent to audio backend ({audio_event['queue_delay_seconds']*1000:.0f} ms queue delay).")
                if audio_event['name']=='accepted' and getattr(self,'last_capture_timing',None) is not None:
                    self.last_capture_timing.update(success_audio_queue_delay_seconds=audio_event['queue_delay_seconds'],
                        success_audio_playback_seconds=audio_event['playback_finished']-audio_event['playback_started'],
                        success_audio_backend='paplay')
            elif audio_event['event']=='skipped':
                self.sound_status.set('Sound skipped: muted or audio backend unavailable.')
                if audio_event.get('name')=='accepted' and getattr(self,'last_capture_timing',None) is not None:
                    self.last_capture_timing['success_audio_skipped']=True
            elif audio_event['event']=='log_failed':
                self.sound_status.set('Could not write audio diagnostics: '+audio_event.get('error','unknown file error'))
            else:self.sound_status.set('Audio playback failed: '+str(audio_event.get('error','unknown backend error')))
        if self.audio.error != self.last_audio_error:
            self.last_audio_error = self.audio.error
            if self.audio.error:self.sound_status.set(self.audio.error)
        if self.future and self.future.done():
            try:
                result=self.future.result();completed=getattr(self,'save_completed_monotonic',None) or time.monotonic()
                if not result['accepted']:
                    self.last_rejection=result['rejection']
                    self.gate.latch(self.saving_sig)
                    self.status.set('Rejected — '+'; '.join(result['rejection']['reasons']))
                    self.audio.waiting=False;self.audio.signal('rejected')
                    self.last_capture_timing=result['timing']
                else:
                    record=result['record'];self.saving_fingerprint=result['fingerprint']
                    self.gate.latch(self.saving_sig,accepted=True)
                    self.history=(self.history+[self.saving_sig])[-5:]
                    self.fingerprints=(self.fingerprints+[self.saving_fingerprint])[-5:]
                    self.original_previews=(self.original_previews+[self.saving_preview])[-5:]
                    self.counter.set(f'Accepted captures: {len(self.session.records)}')
                    self.review.submit(record)
                    notification=time.monotonic()
                    self.status.set(f"Saved {record['sequence']:06d} — full-resolution colour original durably verified; review thumbnail pending.")
                    self.audio.waiting=False;self.audio.signal('accepted')
                    self.label.set('');self.reason.set('');self.last_rejection=None
                    stable_start=result['timing'].get('stillness_started_monotonic')
                    self.last_capture_timing=dict(result['timing'],sequence=record['sequence'],
                        save_worker_finished_monotonic=completed,success_notification_monotonic=notification,
                        notification_poll_delay_seconds=notification-completed,
                        first_stable_to_durable_save_seconds=(completed-stable_start if stable_start else None),
                        first_stable_to_notification_seconds=(notification-stable_start if stable_start else None),
                        thumbnail_queued_monotonic=self.review.jobs[-1][2] if self.review.jobs else None)
                if self.debug_timing.get():self.timing_detail.set(self.format_timing(self.last_capture_timing))
            except Exception as exc:
                self.fail('Save/verification failed: ' + str(exc))
            self.future = None
        while not self.reader.messages.empty():
            kind, detail = self.reader.messages.get_nowait()
            if kind == 'error':
                self.fail('Camera: ' + detail, camera=True)
            else:
                try:
                    self.identity = detail.get('identity', self.identity)
                    settings = detail.get('settings', detail)
                    self.session.event('stream_started', device=self.identity, settings=settings,
                                       check_config=asdict(self.config), app_version=__version__)
                    self.camera_failed = False
                    self.status.set(getattr(self,'recovery_notice','') + 'Manual mode ready. Position page, check framing, then Space / Capture.')
                except OSError as exc:
                    self.fail(str(exc))
        try:
            data, frame, settings = self.reader.frames.get_nowait()
        except queue.Empty:
            data = None
        if data is not None and not self.fatal:
            self.last_frame = time.monotonic()
            try:
                stages={};stage=time.monotonic();image = decode_preview(data)
                stages['reduced_frame_decode_seconds']=time.monotonic()-stage
                stage=time.monotonic();sig = signature(image, self.config, self.kind.get())
                stages['check_region_motion_signature_seconds']=time.monotonic()-stage
                stage=time.monotonic();self.show(image)
                stages['preview_resize_render_seconds']=time.monotonic()-stage
                stage=time.monotonic();holder = parked_holder(image, self.config, self.kind.get())
                stages['holder_detection_seconds']=time.monotonic()-stage
                parked = holder['paused']
                if parked != self.holder_parked:
                    self.session.event('holder_pause' if parked else 'holder_pause_cleared', metrics=holder, kind=self.kind.get())
                    self.holder_parked = parked
                    self.pending = None
                    self.gate.reset_settling()
                    if not parked:
                        self.status.set('Holder returned to margin. Manual capture ready; hands-free waits for a distinct still page.')
                if parked and not self.future:
                    self.status.set('PAUSED — yellow holder in page interior. Move it to the margin to resume.')
                gate_timing=0.0
                received=frame.get('received_monotonic',time.monotonic())
                self.latest_frame_timing=dict(frame.get('acquisition_timing',{}),
                    scheduled_ui_poll_interval_seconds=scheduled_poll_seconds,
                    frame_queue_age_seconds=max(0,time.monotonic()-received),**stages,
                    motion_gate_seconds=gate_timing,observation_count=getattr(self.gate,'samples',0),
                    gate_state=self.gate.state,focus_exposure_stability='Not independently measured; image motion is measured.')
                if not self.future and not self.pause.get() and not parked:
                    if self.pending and (frame.get('captured_monotonic') or frame['received_monotonic']) >= self.pending['requested_monotonic']:
                        options, self.pending = self.pending, None
                        self.attempt(data, frame, settings, sig, options, image=image,holder=holder)
                    elif self.auto.get():
                        observed=frame.get('captured_monotonic') or frame['received_monotonic']
                        gate_started=time.monotonic()
                        ready = self.gate.update(sig, observed)
                        gate_timing=time.monotonic()-gate_started
                        self.latest_frame_timing.update(motion_gate_seconds=gate_timing,
                            observation_count=self.gate.samples,gate_state=self.gate.state)
                        if ready:
                            self.attempt(data, frame, settings, sig,
                                         dict(trigger='automatic', kind=self.kind.get(),
                                              printed_pages=self.label.get(), override_reason='',
                                              automatic_timing=dict(stillness_started_monotonic=self.gate.since,
                                                  stillness_observed_seconds=observed-self.gate.since,
                                                  mandatory_observation_count=self.gate.samples,
                                                  observed_poll_interval_seconds=scheduled_poll_seconds,
                                                  ready_frame_monotonic=observed,decision_monotonic=time.monotonic())),
                                         image=image,holder=holder)
                        elif self.gate.state == 'settling':
                            elapsed=max(0,observed-self.gate.since) if self.gate.since is not None else 0
                            self.status.set(f'Continuous stillness {elapsed:.1f} / {self.config.settle_seconds:g} seconds — keep page interiors still…')
                        elif self.gate.state in ('wait for page turn', 'same page; wait for page turn'):
                            self.status.set(self.gate.state)
                self.latest_frame_timing.update(motion_gate_seconds=gate_timing,
                    observation_count=getattr(self.gate,'samples',0),gate_state=self.gate.state)
                if self.debug_timing.get():self.timing_detail.set(self.format_timing(self.latest_frame_timing))
                self.audio.waiting = (self.wait_tone.get() and not self.pause.get() and not parked
                    and (self.future is not None or self.pending is not None
                         or (self.auto.get() and self.gate.state in ('waiting', 'settling'))))
            except Exception as exc:
                self.fail('Frame/check failure: ' + str(exc), camera=True)
        if not self.fatal and not self.camera_failed and time.monotonic()-self.last_frame > 8:
            self.fail('No fresh camera frames for 8 seconds.', camera=True)
        if self.pause.get():
            self.audio.waiting = False
        # Flush layout/redraw work without dispatching new input or re-entering tick.
        self.root.update_idletasks()
        # Poll at 30 Hz; frame production is independent and keeps only newest JPEG.
        # Yield at least 8 ms to Tk for input, redraw and geometry work.
        self.tick_after = self.root.after(max(8,33-round((time.monotonic()-tick_started)*1000)), self.tick)

    def close(self):
        self.closed = True
        self.root.after_cancel(self.tick_after)
        for window in self.review_windows:
            if not window.closed:window.close()
        self.review.close()
        self.audio.close()
        self.reader.close()
        self.pool.shutdown(wait=True)  # Finish any authorized save before releasing session lock.
        self.session.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='CZUR BookScan: untouched 4160 × 3120 colour JPEG originals.')
    parser.add_argument('--discover', action='store_true', help='verify CZUR USB identity and capture interface')
    parser.add_argument('--device', help='explicit /dev/videoN (must still match CZUR USB identity)')
    parser.add_argument('--session', type=Path, help='new or existing capture-session directory')
    parser.add_argument('--config', type=Path, help='JSON check thresholds and normalized page-interior regions')
    parser.add_argument('--audit', action='store_true', help='fully decode/hash-check a session, then exit (alias for full audit)')
    parser.add_argument('--audit-mode', choices=('quick','full','recovery'), default='quick',
                        help='startup verification: quick (default), full pixel decode, or recovery with incomplete records reported and skipped')
    parser.add_argument('--replay', type=Path, help='test UI with full-resolution .jpg files, no camera')
    parser.add_argument('--volume', type=float, default=.25)
    parser.add_argument('--pitch', type=float, default=1)
    parser.add_argument('--mute', action='store_true')
    args = parser.parse_args()
    if not 0 <= args.volume <= 1 or not .5 <= args.pitch <= 2:
        parser.error('Require volume 0–1 and pitch 0.5–2.')
    session = None
    try:
        config_path = args.config
        if config_path is None and args.session and (args.session/'check-config.json').exists():
            config_path = args.session/'check-config.json'
        config = Config.load(config_path)
        if args.discover:
            identity = discover(args.device)
            with_camera = Camera(identity['device'])
            try:
                decode(with_camera.frame()[0])
                print(json.dumps(dict(device=identity, negotiated=with_camera.settings,
                                      decoded_dimensions=[4160, 3120]), indent=2))
            finally:
                with_camera.close()
            return
        if not args.session:
            parser.error('--session is required for capture or audit.')
        if (args.audit or args.audit_mode == 'recovery') and not args.session.is_dir():
            parser.error('Audit requires an existing session directory.')
        if args.audit:
            def report_progress(i, total, name, status):
                print(f'Full audit {i}/{total}: {name} {status}', file=sys.stderr, flush=True)
            session = Session(args.session, audit_mode='full', progress=report_progress)
            print(f'PASS: {len(session.records)} originals fully decoded and SHA-256 verified.')
            return
        import tkinter as tk
        root = tk.Tk()
        root.title('BookScan — checking session')
        root.geometry('560x160')
        status = tk.StringVar(value='Preparing recovery-friendly session audit…')
        tk.Label(root, textvariable=status, justify='left', wraplength=520, padx=16, pady=18).pack(fill='both', expand=True)
        cancel_event = threading.Event()
        progress_queue = queue.Queue()
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='session-audit')
        def progress(i, total, name, result):
            progress_queue.put((i, total, name, result))
        future = worker.submit(Session, args.session, audit_mode=args.audit_mode,
                               progress=progress, cancel=cancel_event)
        cancel_button = tk.Button(root, text='Cancel session audit', command=lambda: cancel_event.set())
        cancel_button.pack(pady=(0,12))
        closing = {'requested': False}
        def request_startup_close():
            closing['requested'] = True
            cancel_event.set()
            status.set('Cancelling the read-only audit safely… no capture files are being changed.')
            cancel_button.configure(state='disabled')
            root.after(0, poll_startup)
        root.protocol('WM_DELETE_WINDOW', request_startup_close)
        def poll_startup():
            nonlocal session
            latest = None
            while True:
                try: latest = progress_queue.get_nowait()
                except queue.Empty: break
            if latest:
                i,total,name,result=latest
                status.set(f"Session audit {i}/{total}: {name} — {result}\nThe session is not being modified.")
            if future.done():
                cancel_button.configure(state='disabled')
                if closing['requested']:
                    try:
                        opened = future.result()
                        opened.close()
                    except Exception:
                        pass
                    worker.shutdown(wait=False, cancel_futures=True)
                    root.destroy()
                    return
                try:
                    opened = future.result()
                    session = opened
                    identity = {'source':'replay'} if args.replay else None
                    root.title('BookScan — CZUR full-resolution originals')
                    for child in root.winfo_children(): child.destroy()
                    App(root, args, session, identity, config)
                except Exception as exc:
                    status.set('Session audit stopped: ' + str(exc) + '\nNo capture files were changed. Close this window or restart with --audit-mode recovery after reviewing the report.')
                    cancel_button.configure(text='Close', state='normal', command=root.destroy)
                finally:
                    worker.shutdown(wait=False, cancel_futures=True)
                return
            root.after(100, poll_startup)
        root.after(50, poll_startup)
        root.mainloop()
    except Exception as exc:
        print('BookScan: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if session and not session.lock.closed:
            session.close()
