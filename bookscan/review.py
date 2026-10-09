"""Read-only saved-original review, on workers independent of capture saving."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import time
from .storage import decode
from PIL import Image


def load_original(root, record):
    path=Path(root)/f"{record['sequence']:06d}"/'original.jpg'
    data=path.read_bytes()
    if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
        raise ValueError('Saved original changed since verification; review refused.')
    return decode(data)


def caption(record, compact=False):
    text=f"{record['sequence']:06d} · {record.get('kind','unknown')}"
    if compact:
        modes={'intentional blank':'Blank','single page':'Single page','cover':'Cover','spread':'Spread'}
        text=f"{record['sequence']:06d} · {modes.get(record.get('kind'),record.get('kind','unknown'))}"
        flags=[]
        if record.get('retry_of') is not None:flags.append(f"Retry #{record['retry_of']}")
        if record.get('override_reason'):flags.append('Override')
        return text+ ('\n'+' · '.join(flags) if flags else '')
    if record.get('retry_of') is not None:text+=f"\nRetry of {record['retry_of']:06d}"
    if record.get('override_reason'):text+='\nHeuristic override' if record.get('check_warnings') or record.get('overridden_rejection') else '\nManual override'
    return text


class Review:
    def __init__(self, root):
        self.root=root
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='saved-review')
        self.jobs=[]
        self.items=[]
        self.error=None
        self.last_timing=None

    def submit(self, record):
        # Only App's durable-save success path and verified session reopening call this.
        record=dict(record)
        queued=time.monotonic()
        self.jobs.append((record,self.pool.submit(self.thumbnail,record,queued),queued))

    def thumbnail(self, record, queued=None):
        started=time.monotonic()
        queued=started if queued is None else queued
        read_started=time.monotonic()
        path=Path(self.root)/f"{record['sequence']:06d}"/'original.jpg'
        data=path.read_bytes()
        read_finished=time.monotonic()
        if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
            raise ValueError('Saved original changed since verification; review refused.')
        hash_finished=time.monotonic()
        image=decode(data)
        decoded=time.monotonic()
        image.thumbnail((130,98),Image.Resampling.LANCZOS)
        finished=time.monotonic()
        timing=dict(thumbnail_queue_delay_seconds=started-queued,
                    thumbnail_read_seconds=read_finished-read_started,
                    thumbnail_hash_seconds=hash_finished-read_finished,
                    thumbnail_full_decode_seconds=decoded-hash_finished,
                    thumbnail_resize_seconds=finished-decoded,
                    thumbnail_total_seconds=finished-started,
                    thumbnail_ready_monotonic=finished)
        return image,timing

    def poll(self):
        changed=False
        while self.jobs and self.jobs[0][1].done():
            record,future,queued=self.jobs.pop(0)
            try:
                thumbnail,timing=future.result();self.last_timing=timing
                self.items=sorted([item for item in self.items if item[0]['sequence']!=record['sequence']]+[(record,thumbnail)],key=lambda item:item[0]['sequence'])[-3:]
                self.error=None;changed=True
            except Exception as exc:
                self.error=str(exc);changed=True
        return changed

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)


class Viewport:
    def __init__(self,root,record):
        self.root,self.record=root,record
        self.image=None

    def render(self, request):
        serial,w,h,zoom,center=request
        if self.image is None:self.image=load_original(self.root,self.record)
        iw,ih=self.image.size
        scale=min(w/iw,h/ih) if zoom=='Fit' else int(zoom[:-1])/100
        cw,ch=min(iw,w/scale),min(ih,h/scale)
        x=max(0,min(iw-cw,center[0]*iw-cw/2));y=max(0,min(ih-ch,center[1]*ih-ch/2))
        image=self.image.crop((round(x),round(y),round(x+cw),round(y+ch)))
        image=image.resize((max(1,round(cw*scale)),max(1,round(ch*scale))),Image.Resampling.BILINEAR)
        return serial,image,scale,(iw,ih)


class ReviewWindow:
    """Zoom/pan viewport, not a giant upscaled Tk bitmap. All image work is off UI."""
    def __init__(self, parent, root, record):
        import tkinter as tk
        from tkinter import ttk
        self.tk=tk;self.root=root;self.record=dict(record)
        self.window=tk.Toplevel(parent);self.window.title('Saved original — '+caption(record).replace('\n',' · '))
        self.window.geometry('950x760');self.closed=False
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='review-zoom')
        self.viewport=Viewport(root,self.record);self.future=None;self.request=None;self.serial=0
        self.center=(.5,.5);self.drag=None
        row=ttk.Frame(self.window,padding=6);row.pack(fill='x')
        ttk.Label(row,text=caption(record).replace('\n',' · ')).pack(side='left')
        self.zoom=tk.StringVar(value='Fit')
        combo=ttk.Combobox(row,textvariable=self.zoom,values=['Fit','50%','100%','200%'],state='readonly',width=7)
        combo.pack(side='right');combo.bind('<<ComboboxSelected>>',lambda event:self.refresh())
        self.status=tk.StringVar(value='Loading verified saved original…')
        ttk.Label(self.window,textvariable=self.status).pack(fill='x')
        ttk.Label(self.window,text='Choose zoom; drag to pan. Mouse wheel zooms. Review has no positioning guides.').pack(fill='x')
        self.canvas=tk.Canvas(self.window,background='#202020',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',lambda event:self.refresh())
        self.canvas.bind('<ButtonPress-1>',self.pan_start);self.canvas.bind('<B1-Motion>',self.pan)
        self.canvas.bind('<MouseWheel>',lambda event:self.wheel(1 if event.delta>0 else -1))
        self.canvas.bind('<Button-4>',lambda event:self.wheel(1));self.canvas.bind('<Button-5>',lambda event:self.wheel(-1))
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.refresh();self.poll_after=self.window.after(50,self.poll)

    def refresh(self):
        if self.closed:return
        self.serial+=1
        self.request=(self.serial,max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height()),self.zoom.get(),self.center)

    def poll(self):
        if self.closed:return
        if self.future and self.future.done():
            try:
                serial,image,self.scale,self.original_size=self.future.result()
                if serial==self.serial:
                    from PIL import ImageTk
                    self.photo=ImageTk.PhotoImage(image,master=self.window)
                    self.canvas.delete('all');self.canvas.create_image(self.canvas.winfo_width()/2,self.canvas.winfo_height()/2,image=self.photo)
                    self.status.set(f"Verified {self.original_size[0]} × {self.original_size[1]} colour original · {self.scale*100:.0f}%")
            except Exception as exc:
                self.status.set('Review failed: '+str(exc));self.request=None
            self.future=None
        if self.future is None and self.request:
            request,self.request=self.request,None
            self.future=self.pool.submit(self.viewport.render,request)
        self.poll_after=self.window.after(50,self.poll)

    def pan_start(self,event):self.drag=(event.x,event.y,self.center)

    def pan(self,event):
        if self.drag and hasattr(self,'scale'):
            x,y,center=self.drag;iw,ih=self.original_size
            self.center=(max(0,min(1,center[0]+(x-event.x)/(self.scale*iw))),max(0,min(1,center[1]+(y-event.y)/(self.scale*ih))))
            self.refresh()

    def wheel(self,direction):
        levels=['Fit','50%','100%','200%'];i=levels.index(self.zoom.get())
        self.zoom.set(levels[max(0,min(len(levels)-1,i+direction))]);self.refresh()

    def close(self):
        self.closed=True;self.window.after_cancel(self.poll_after)
        self.pool.shutdown(wait=False,cancel_futures=True);self.window.destroy()
