import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from bookscan.app import App
from bookscan.checks import Config
from bookscan.storage import Session


@unittest.skipUnless(os.environ.get('DISPLAY'),'Tk layout smoke test requires a display (run under Xvfb in CI).')
class ResponsiveLayoutTests(unittest.TestCase):
    def test_left_control_panel_and_preview_resize_without_scanner(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory:
            session=Session(Path(directory)/'session')
            try:root=tk.Tk()
            except tk.TclError as exc:self.skipTest(f'X display is not reachable: {exc}')
            root.geometry('1540x980')
            app=App(root,Namespace(volume=.25,pitch=1,mute=False,replay=None,device=None),session,None,Config())
            try:
                root.update();self.assertEqual(app.review_count.get(),'3')
                self.assertEqual(int(app.review_panel.grid_info()['column']),1)
                self.assertLessEqual(app.left_panel.winfo_reqheight(),app.left_panel.winfo_height())
                app.toggle_advanced();root.update();self.assertTrue(app.advanced_window.winfo_exists())
                app.close_advanced();root.update();self.assertIsNone(app.advanced_window)
                preview_wide=app.canvas.winfo_width()
                root.geometry('1100x700');root.update()
                preview_narrow=app.canvas.winfo_width()
                self.assertGreater(preview_wide,1000)
                self.assertGreater(preview_narrow,500)
                self.assertGreaterEqual(preview_wide,preview_narrow)
            finally:app.close()
