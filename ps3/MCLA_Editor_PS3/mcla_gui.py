#!/usr/bin/env python3
"""
mcla_gui.py - Tkinter tracklist editor for Midnight Club: Los Angeles (PlayStation 3 edition; the Xbox 360 editor is a
separate program).

Usage:
    py mcla_gui.py [path\\to\\game\\folder]        (the extracted disc folder that holds PS3_GAME)

Pure standard library (Python 3.10+ with tkinter). Same working principles as the Midnight Club 3 XWB tool:
  * Edits are staged as a draft (yellow rows); nothing is written until you press a Build / Save button and confirm.
  * Build writes NEW files (a new folder); "Save to the current folder" replaces the changed archives of the open folder
    (the previous versions are kept once as <name>.bak).
What can be edited (same features as the X360 editor; tested in RPCS3):
  * genre of a song (drag it onto a genre or use the Move menu), order inside a genre, display titles (any length);
  * remove songs from the playlists (Removed list = undo);
  * replace the audio of a song (Replace audio...), ADD new songs (Add song...: audio file, artist, title, genre) -
    the audio is encoded to MP3 by ffmpeg (LAME, no bit reservoir, like the game) and all game entries are created at
    build time, including the garage music entry and an end-of-race clip.
Preview/Export decode the original tracks (ffmpeg needed).
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import keysetup
import mcla_model as M
import mcla_platform as PF

try:
    import winsound
except ImportError:                                     # macOS / Linux: an external player is used (see Player)
    winsound = None

WINDOWS = sys.platform == 'win32'
MACOS = sys.platform == 'darwin'
FFMPEG_CANDIDATES = [
    os.environ.get('FFMPEG', ''),
    '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/usr/bin/ffmpeg',   # apps started from Finder get no Homebrew PATH
]
EXE_TYPES = [('Executable', '*.exe'), ('All', '*.*')] if WINDOWS else [('All files', '*')]
APP_TITLE = 'MCLA Tracklist Editor - PlayStation 3'
CHANGED_BG = '#fff3b0'
ALL_KEY = 'ALL'
REMOVED_KEY = 'REMOVED'
# settings live next to the program (next to the .exe when the editor is packaged with PyInstaller); a macOS .app must not
# be written into (that breaks its signature), so there they go to Application Support
if MACOS and getattr(sys, 'frozen', False):
    APP_DIR = os.path.expanduser('~/Library/Application Support/MCLA Editor PS3')
    os.makedirs(APP_DIR, exist_ok=True)
else:
    APP_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, 'mcla_ps3_gui_settings.json')      # own file: never shared with the X360 editor
DEFAULT_ENCODER_GUESSES = []


class Player:
    """Plays a WAV file (optionally looped): winsound on Windows; elsewhere an external player - ffplay (next to ffmpeg or on
    PATH), afplay (built into macOS), paplay / aplay (Linux). Looping restarts the player each time it ends."""

    def __init__(self, ffmpeg_path):
        self.ffmpeg_path = ffmpeg_path            # callable -> current ffmpeg path
        self._proc = None
        self._gen = 0

    def command(self):
        import shutil
        ff = self.ffmpeg_path() or ''
        near = [os.path.join(os.path.dirname(ff), n) for n in ('ffplay', 'ffplay.exe')] if ff else []
        for c in [n for n in near if os.path.isfile(n)] + [shutil.which('ffplay')]:
            if c:
                return [c, '-nodisp', '-autoexit', '-loglevel', 'quiet']
        for name in ('afplay', 'paplay', 'aplay'):
            c = shutil.which(name) or (('/usr/bin/' + name) if os.path.isfile('/usr/bin/' + name) else None)
            if c:
                return [c]
        return None

    def available(self):
        return winsound is not None or self.command() is not None

    def play(self, path, loop=False):
        self.stop()
        if winsound is not None:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | (winsound.SND_LOOP if loop else 0))
            return
        cmd = self.command()
        if not cmd:
            raise RuntimeError('No audio player found (install ffmpeg with ffplay; on macOS afplay is built in).')
        gen = self._gen

        def run():
            while self._gen == gen:
                p = subprocess.Popen(cmd + [path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                self._proc = p
                if self._gen != gen:                  # stopped while it was starting
                    p.terminate()
                p.wait()
                if not loop:
                    break
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._gen += 1
        if winsound is not None:
            winsound.PlaySound(None, winsound.SND_PURGE)
            return
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()


class Flow(ttk.Frame):
    """A row of widgets that wraps onto further lines when the window is too narrow (large system fonts / scaling)."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._items = []
        self._last = None
        self.bind('<Configure>', lambda e: self._reflow())

    def add(self, widget, gap=4):
        self._items.append((widget, gap))
        self.after_idle(self._reflow)
        return widget

    def _reflow(self):
        width = self.winfo_width()
        if width <= 1:                                   # not laid out yet
            width = self.winfo_toplevel().winfo_reqwidth() or 10 ** 6
        x = y = row_h = 0
        spots = []
        for w, gap in self._items:
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x and x + ww > width:
                x, y, row_h = 0, y + row_h + 4, 0
            spots.append((w, x, y))
            x += ww + gap
            row_h = max(row_h, wh)
        if spots == self._last:
            return
        self._last = spots
        for w, px, py in spots:
            w.place(x=px, y=py)
        self.configure(height=y + row_h)


def load_settings():
    s = {'encoder': '', 'ffmpeg': '', 'q_music': 80, 'q_lo': 70, 'q_mp3': 3, 'eor': True, 'eor_seconds': 20.0,
         'eor_follow_audio': True, 'hangout_follow': True, 'hangout_seconds': 60.0,
         'hangout_normalize': True, 'hangout_lufs': -15.5, 'last_dir': '', 'encoder_runner': ''}
    try:
        s.update(json.load(open(SETTINGS_PATH, encoding='utf-8')))
    except (OSError, ValueError):
        pass
    s.pop('test_boost', None)                            # test-build switch, never persisted
    if not s['ffmpeg'] or not os.path.isfile(s['ffmpeg']):
        s['ffmpeg'] = find_ffmpeg() or ''
    return s


def save_settings(s):
    try:
        json.dump(s, open(SETTINGS_PATH, 'w', encoding='utf-8'), indent=1)
    except OSError:
        pass


def find_ffmpeg():
    import shutil
    for c in FFMPEG_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return shutil.which('ffmpeg')


def fmt_time(sec):
    if sec is None:
        return ''
    m, s = divmod(int(round(sec)), 60)
    return '%d:%02d' % (m, s)


def fmt_mb(n):
    return '' if n is None else '%.1f MB' % (n / 1048576)


class App(tk.Tk):
    def __init__(self, initial_dir='', script=''):
        super().__init__()
        self.title(APP_TITLE)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()     # large scaling: keep the window on the screen
        self.geometry('%dx%d' % (min(1240, sw - 40), min(720, sh - 90)))
        self.minsize(min(900, sw - 40), min(500, sh - 90))
        self.p: M.Project | None = None
        self.q: 'queue.Queue[tuple]' = queue.Queue()
        self.busy = False
        self.cur = ALL_KEY
        self.settings = load_settings()
        self.ffmpeg = self.settings['ffmpeg'] or find_ffmpeg()
        self.player = Player(lambda: self.ffmpeg)
        self._drag = None
        self._autosave_ok = False          # the draft file is written only after the question about an old draft
        self._autosave_job = None
        self._build_widgets()
        self._entry_clipboard()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(100, self._poll)
        last = self.settings.get('last_dir', '')
        if initial_dir:
            self.after(50, lambda: self._load(initial_dir))
        elif last and PF.detect(last)[0]:
            self.after(50, lambda: self._load(last))
        if script:
            self.after(200, lambda: self._run_script(script))

    # ------------------------------------------------------------------ text fields
    def _entry_clipboard(self):
        """Ctrl+C / V / X / A in every text field also with a non-Latin keyboard layout (Tk binds the shortcuts by the
        typed letter, so they do nothing e.g. on a Russian layout), plus a right-click menu."""
        def key(ev):
            if not ev.state & 0x4 or ev.keysym.lower() in ('c', 'v', 'x', 'a'):    # Latin layout: Tk handles it itself
                return None
            w = ev.widget
            virt = {67: '<<Copy>>', 86: '<<Paste>>', 88: '<<Cut>>'}.get(ev.keycode)
            if virt:
                w.event_generate(virt)
                return 'break'
            if ev.keycode == 65:
                w.select_range(0, 'end')
                w.icursor('end')
                return 'break'
            return None

        def menu(ev):
            w = ev.widget
            if str(w.cget('state')) == 'disabled':
                return
            w.focus_set()
            m = tk.Menu(w, tearoff=False)
            for label, virt in (('Cut', '<<Cut>>'), ('Copy', '<<Copy>>'), ('Paste', '<<Paste>>')):
                m.add_command(label=label, command=lambda v=virt: w.event_generate(v))
            m.add_separator()
            m.add_command(label='Select all', command=lambda: (w.select_range(0, 'end'), w.icursor('end')))
            m.tk_popup(ev.x_root, ev.y_root)
        self._clip_key = key
        for cls in ('TEntry', 'Entry', 'TCombobox'):
            if WINDOWS:                                  # the key codes above are Windows ones; macOS uses Cmd+C/V natively
                self.bind_class(cls, '<Control-KeyPress>', key, add='+')
            for b in self.RIGHT_CLICK:
                self.bind_class(cls, b, menu, add='+')

    RIGHT_CLICK = ('<Button-3>', '<Button-2>') if MACOS else ('<Button-3>',)     # Tk on macOS reports right-click as 2

    # ------------------------------------------------------------------ widgets
    def _build_widgets(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill='x')
        self.btn_open = ttk.Button(top, text='Open game folder...', command=self._on_open)
        self.btn_open.pack(side='left')
        self.btn_iso = ttk.Button(top, text='Extract ISO...', command=self._on_extract_iso)
        self.btn_iso.pack(side='left', padx=(4, 0))
        self.path_var = tk.StringVar(value='(no game folder open)')
        ttk.Label(top, textvariable=self.path_var, foreground='#666').pack(side='left', padx=10)
        self.btn_csv = ttk.Button(top, text='Export list (CSV)...', command=self._on_csv, state='disabled')
        self.btn_csv.pack(side='right')

        body = ttk.PanedWindow(self, orient='horizontal')
        body.pack(fill='both', expand=True, padx=6)

        left = ttk.Frame(body)
        self.gtree = ttk.Treeview(left, columns=('n',), show='tree', selectmode='browse', height=10)
        self.gtree.column('#0', width=250)
        self.gtree.column('n', width=105, anchor='e', stretch=False)
        self.gtree.tag_configure('changed', background=CHANGED_BG)
        self.gtree.tag_configure('empty', foreground='#c00000')
        self.gtree.pack(fill='both', expand=True)
        self.gtree.bind('<<TreeviewSelect>>', self._on_genre_select)
        self.gtree.bind('<Double-1>', self._on_genre_double)
        for b in self.RIGHT_CLICK:
            self.gtree.bind(b, self._on_genre_menu)
        ttk.Label(left, text='Drag songs onto a genre to move them. Double-click a genre to rename it (its name in the game menus).', foreground='#777',
                  wraplength=230, justify='left').pack(anchor='w', pady=(6, 0))
        body.add(left, weight=0)

        right = ttk.Frame(body)
        flt = ttk.Frame(right)
        flt.pack(fill='x', pady=(0, 4))
        ttk.Label(flt, text='Filter:').pack(side='left')
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add('write', lambda *_: self.refresh())
        ttk.Entry(flt, textvariable=self.filter_var, width=32).pack(side='left', padx=4)
        ttk.Label(flt, text='(title / object substring)', foreground='#777').pack(side='left')

        cols = ('pos', 'title', 'len', 'genre', 'eor', 'src', 'time', 'size', 'file')
        heads = {'pos': '#', 'title': 'Title (as shown in the game)', 'len': 'Chars', 'genre': 'Genre', 'src': 'Src',
                 'time': 'Time', 'size': 'Size', 'file': 'Audio file', 'eor': 'EOR clip'}
        widths = {'pos': 40, 'title': 470, 'len': 60, 'genre': 100, 'src': 45, 'time': 55, 'size': 70, 'file': 80, 'eor': 65}
        tf = ttk.Frame(right)
        tf.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='extended')
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor='w' if c in ('title', 'genre') else 'center',
                             stretch=(c == 'title'))
        self.tree.tag_configure('changed', background=CHANGED_BG)
        vsb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._update_buttons())
        self.tree.bind('<Double-1>', self._on_double)
        for b in self.RIGHT_CLICK:
            self.tree.bind(b, self._on_right_click)
        self.tree.bind('<F2>', lambda e: self._on_rename())
        self.tree.bind('<Delete>', lambda e: self._on_remove())
        self.tree.bind('<BackSpace>', lambda e: self._on_revert())
        self.tree.bind('<Control-KeyPress>', lambda e: self._ctrl_a(e, self.tree), add='+')
        self.gtree.bind('<Control-KeyPress>', lambda e: self._ctrl_a(e, self.tree, focus=True), add='+')
        self.tree.bind('<Control-Up>', lambda e: self._on_move_within(-1))
        self.tree.bind('<Control-Down>', lambda e: self._on_move_within(+1))
        self.tree.bind('<ButtonPress-1>', self._drag_start, add='+')
        self.tree.bind('<B1-Motion>', self._drag_motion)
        self.tree.bind('<ButtonRelease-1>', self._drag_end)
        body.add(right, weight=1)

        b1 = Flow(self)                                  # wraps to a second line when the window is too narrow
        b1.pack(fill='x', padx=6, pady=(6, 0))
        b1.add(ttk.Label(b1, text='Selected:', foreground='#666'), gap=8)
        self.btn_up = b1.add(ttk.Button(b1, text='Move up', command=lambda: self._on_move_within(-1), state='disabled'))
        self.btn_down = b1.add(ttk.Button(b1, text='Move down', command=lambda: self._on_move_within(+1), state='disabled'))
        self.mb_move = b1.add(ttk.Menubutton(b1, text='Move to genre', state='disabled'))
        self.move_menu = tk.Menu(self.mb_move, tearoff=False)
        self._rebuild_move_menu()
        self.mb_move['menu'] = self.move_menu
        self.btn_rename = b1.add(ttk.Button(b1, text='Rename...', command=self._on_rename, state='disabled'))
        self.btn_revert = b1.add(ttk.Button(b1, text='Revert', command=self._on_revert, state='disabled'))
        self.btn_remove = b1.add(ttk.Button(b1, text='Remove song', command=self._on_remove, state='disabled'), gap=20)
        self.btn_play = b1.add(ttk.Button(b1, text='\u25b6 Preview', command=self._on_preview, state='disabled'))
        self.btn_stop = b1.add(ttk.Button(b1, text='\u25a0 Stop', command=self._on_stop, state='disabled'))
        self.btn_wav = b1.add(ttk.Button(b1, text='Export WAV...', command=self._on_export_wav, state='disabled'), gap=20)
        self.btn_audio = b1.add(ttk.Button(b1, text='Replace audio...', command=self._on_replace_audio, state='disabled'))
        self.btn_eor = b1.add(ttk.Button(b1, text='End-of-race clip...', command=self._on_eor_clip, state='disabled'))
        self.btn_hangout = b1.add(ttk.Button(b1, text='Hangout music...', command=self._on_hangout, state='disabled'))
        self.btn_settings = b1.add(ttk.Button(b1, text='Audio settings...', command=self._on_settings))
        self.btn_add = b1.add(ttk.Button(b1, text='Add song...', command=self._on_add_song, state='disabled'))
        self.btn_relink = b1.add(ttk.Button(b1, text='Restore missing audio...', command=self._on_relink, state='disabled'))

        b2 = ttk.Frame(self, padding=6)
        b2.pack(fill='x')
        ttk.Label(b2, text='Draft:', foreground='#666').pack(side='left')
        self.btn_revert_all = ttk.Button(b2, text='Revert all', command=self._on_revert_all, state='disabled')
        self.btn_revert_all.pack(side='left', padx=(8, 0))
        self.btn_build = ttk.Button(b2, text='Build changed archives...', command=self._on_build_archives,
                                    state='disabled')
        self.btn_build.pack(side='left', padx=(18, 4))
        self.btn_folder = ttk.Button(b2, text='Build test game folder...', command=self._on_build_folder,
                                     state='disabled')
        self.btn_folder.pack(side='left', padx=4)
        self.btn_save = ttk.Button(b2, text='Save to the current folder...', command=self._on_save_here, state='disabled')
        self.btn_save.pack(side='left', padx=(18, 4))
        self.btn_free = ttk.Button(b2, text='Free space of removed songs...', command=self._on_free, state='disabled')
        self.btn_free.pack(side='left', padx=(18, 4))
        self.progress = ttk.Progressbar(b2, mode='indeterminate', length=150)
        self.progress.pack(side='right', padx=6)

        st = ttk.Frame(self, padding=(6, 0, 6, 6))
        st.pack(fill='x')
        self.status_var = tk.StringVar(value='Open the PS3 game folder (the extracted disc folder with PS3_GAME) to begin.')
        ttk.Label(st, textvariable=self.status_var, foreground='#444').pack(side='left')

        self.ctx = tk.Menu(self, tearoff=False)

    # ------------------------------------------------------------------ helpers
    def _set_status(self, msg):
        self.status_var.set(msg)

    def _busy(self, on, msg=None):
        self.busy = on
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()
        if msg:
            self._set_status(msg)
        self._update_buttons()

    @staticmethod
    def _ctrl_a(ev, tree, focus=False):
        """Ctrl+A: select every row of the list shown (by key code, so it works with any keyboard layout, e.g. Ctrl+Ф)"""
        if ev.keysym.lower() != 'a' and not (WINDOWS and ev.keycode == 65):
            return None
        rows = tree.get_children()
        if rows:
            tree.selection_set(rows)
            if focus:
                tree.focus_set()
            tree.focus(rows[0])
        return 'break'

    def _selected(self):
        return [i for i in self.tree.selection() if self.p and i in self.p.songs]

    def _update_buttons(self):
        for b in (self.btn_open, self.btn_iso):
            b.configure(state='disabled' if self.busy else 'normal')
        has = self.p is not None and not self.busy
        sel = self._selected() if self.p else []
        one_genre = self.cur not in (ALL_KEY, REMOVED_KEY)
        dirty = bool(self.p and self.p.is_dirty())
        st = lambda b, ok: b.configure(state='normal' if ok else 'disabled')
        st(self.btn_up, has and len(sel) == 1 and one_genre)
        st(self.btn_down, has and len(sel) == 1 and one_genre)
        st(self.btn_rename, has and len(sel) == 1)
        st(self.btn_revert, has and bool(sel))
        st(self.btn_remove, has and any(not self.p.songs[o].removed for o in sel))
        st(self.btn_play, has and len(sel) == 1 and not self.p.songs[sel[0]].added and bool(self.ffmpeg) and self.player.available() and self.p.music is not None)
        st(self.btn_stop, has and self.player.available())
        st(self.btn_wav, has and len(sel) == 1 and not self.p.songs[sel[0]].added and bool(self.ffmpeg) and self.p.music is not None)
        st(self.btn_audio, has and len(sel) == 1 and bool(self.p.songs[sel[0]].file_hash) and self.p.music is not None)
        st(self.btn_revert_all, has and dirty)
        st(self.btn_build, has and dirty)
        st(self.btn_folder, has and dirty)
        st(self.btn_save, has and dirty)
        st(self.btn_csv, has)
        st(self.btn_eor, has and len(sel) == 1)
        st(self.btn_hangout, has)
        st(self.btn_add, has)
        st(self.btn_relink, has and any(not s.new_audio and not s.copy_from for s in self.p.missing_audio()))
        st(self.btn_free, has and (self.p.free_removed or bool(self.p.freeable())))
        if self.p:
            self.btn_free.configure(text='Keep the removed songs\' audio' if self.p.free_removed else 'Free space of removed songs...')
        self.mb_move.configure(state='normal' if (has and sel) else 'disabled')

    # ------------------------------------------------------------------ loading
    def _on_open(self):
        d = filedialog.askdirectory(title='PS3 game folder (the extracted disc folder with PS3_GAME)')
        if d:
            self._load(d)

    # ------------------------------------------------------------------ ISO extraction
    def _on_extract_iso(self):
        """Extract the (decrypted) disc image into a folder next to it (or one you choose) and open that folder."""
        import iso_extract
        if self.busy:
            return
        iso = filedialog.askopenfilename(title='PS3 disc image of Midnight Club: Los Angeles',
                                         initialdir=self.settings.get('last_iso_dir') or None,
                                         filetypes=[('Disc image', '*.iso'), ('All files', '*.*')])
        if not iso:
            return
        self.config(cursor='watch')
        self.update()
        try:
            err, total = iso_extract.inspect(iso)
        finally:
            self.config(cursor='')
        if err:
            messagebox.showerror('Extract ISO', '%s\n\n%s' % (iso, err))
            return
        self.settings['last_iso_dir'] = os.path.dirname(iso)
        save_settings(self.settings)
        out = self._ask_iso_target(iso, total)
        if out:
            self._start_iso(iso, out, total)

    @staticmethod
    def _free_bytes(path):
        import shutil
        p = os.path.abspath(path)
        while not os.path.isdir(p) and os.path.dirname(p) != p:      # the nearest folder that exists
            p = os.path.dirname(p)
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None

    @staticmethod
    def _existing_bytes(out):
        """bytes already in the target (a run that was stopped: its complete files are skipped)"""
        if not os.path.isdir(out):
            return 0
        return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(out) for f in fs
                   if not f.endswith('.part'))

    def _ask_iso_target(self, iso, total):
        """dialog: the folder to extract into (default: <image folder>\\<image name>_extracted) -> path or None"""
        import iso_extract
        win = tk.Toplevel(self)
        win.title('Extract ISO')
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack(fill='both')
        ttk.Label(f, text='Image: %s  (%.2f GB)' % (iso, total / 2 ** 30)).grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(f, text='Extract to:').grid(row=1, column=0, sticky='w', pady=(10, 0))
        var = tk.StringVar(value=iso_extract.default_target(iso))
        ent = ttk.Entry(f, textvariable=var, width=80)
        ent.grid(row=1, column=1, sticky='we', padx=6, pady=(10, 0))
        info = tk.StringVar()

        def browse():
            d = filedialog.askdirectory(parent=win, title='Folder to extract the game into', mustexist=False,
                                        initialdir=os.path.dirname(var.get()) or os.path.dirname(iso))
            if d:
                d = os.path.normpath(d)
                # a folder that holds other things (e.g. the one with the image): the game gets its own sub-folder in it
                if os.path.isdir(d) and os.listdir(d) and not os.path.isdir(os.path.join(d, 'PS3_GAME')):
                    d = os.path.join(d, os.path.basename(iso_extract.default_target(iso)))
                var.set(d)
        ttk.Button(f, text='Browse...', command=browse).grid(row=1, column=2, pady=(10, 0))

        def upd(*_):
            out = var.get().strip()
            free = self._free_bytes(out) if out else None
            need = max(0, total - self._existing_bytes(out)) if out else total
            info.set('Needs %.2f GB%s. Free there: %s. The folder is opened in the editor when it is done.' % (
                need / 2 ** 30, ' (part of it is already extracted there)' if out and need < total else '',
                '%.1f GB' % (free / 2 ** 30) if free is not None else '?'))
        var.trace_add('write', upd)
        upd()
        ttk.Label(f, textvariable=info, foreground='#555', wraplength=640, justify='left').grid(
            row=2, column=0, columnspan=3, sticky='w', pady=(8, 0))
        result = []

        def ok():
            out = os.path.normpath(os.path.abspath(var.get().strip())) if var.get().strip() else ''
            if not out:
                return
            if os.path.abspath(out).lower() == os.path.abspath(iso).lower() or os.path.isfile(out):
                messagebox.showwarning('Extract ISO', 'Choose a folder, not a file.', parent=win)
                return
            need = max(0, total - self._existing_bytes(out))
            free = self._free_bytes(out)
            if free is not None and free < need + 50 * 2 ** 20:
                messagebox.showerror('Extract ISO', 'Not enough free space: %.2f GB needed, %.2f GB free.'
                                     % (need / 2 ** 30, free / 2 ** 30), parent=win)
                return
            if os.path.isdir(out) and os.listdir(out) and not os.path.isdir(os.path.join(out, 'PS3_GAME')):
                if not messagebox.askyesno('Extract ISO', 'The folder\n%s\nis not empty. Put the game files (PS3_GAME, '
                                           'PS3_UPDATE, PS3_DISC.SFB) into it anyway?' % out, parent=win):
                    return
            result.append(out)
            win.destroy()
        b = ttk.Frame(win, padding=(12, 0, 12, 12))
        b.pack(fill='x')
        ttk.Button(b, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(b, text='Extract', command=ok).pack(side='right', padx=6)
        ent.focus_set()
        win.bind('<Return>', lambda e: ok())
        win.grab_set()
        self.wait_window(win)
        return result[0] if result else None

    def _start_iso(self, iso, out, total):
        import iso_extract
        self._iso_cancel = threading.Event()
        win = self._iso_win = tk.Toplevel(self)
        win.title('Extracting %s' % os.path.basename(iso))
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack(fill='both')
        self._iso_label = tk.StringVar(value='Starting...')
        ttk.Label(f, textvariable=self._iso_label, width=80).pack(anchor='w')
        self._iso_bar = ttk.Progressbar(f, length=560, maximum=max(total, 1))
        self._iso_bar.pack(fill='x', pady=8)
        ttk.Button(f, text='Stop', command=self._iso_cancel.set).pack(side='right')
        win.protocol('WM_DELETE_WINDOW', self._iso_cancel.set)
        self._busy(True, 'Extracting %s into %s...' % (os.path.basename(iso), out))
        last = [-1, None]

        def prog(done, tot, name):
            pc = int(done * 1000 / max(tot, 1))
            if pc != last[0] or name != last[1]:             # at most ~1000 updates, plus one per file
                last[0], last[1] = pc, name
                self.q.put(('xprogress', done, tot, name))

        def run():
            try:
                iso_extract.extract(iso, out, prog, self._iso_cancel)
                self.q.put(('extracted', out, iso_extract.check(out), False))
            except iso_extract.Cancelled:
                self.q.put(('extracted', out, None, True))
            except Exception as e:
                self.q.put(('extracted', out, 'Extraction failed: %s' % e, False))
        threading.Thread(target=run, daemon=True).start()

    def _iso_progress(self, done, total, name):
        if getattr(self, '_iso_win', None):
            self._iso_bar['value'] = done
            self._iso_label.set('%.0f %%  (%.2f of %.2f GB)  %s' % (100.0 * done / max(total, 1), done / 2 ** 30,
                                                                  total / 2 ** 30, name.strip('/')))

    def _iso_finished(self, out, err, cancelled):
        if getattr(self, '_iso_win', None):
            self._iso_win.destroy()
            self._iso_win = None
        self._busy(False)
        if cancelled:
            self._set_status('Extraction stopped. The files already extracted stay in %s; extracting into the same folder '
                             'again continues from there.' % out)
            return
        if err:
            self._set_status(err)
            messagebox.showerror('Extract ISO', err)
            return
        self._set_status('Extracted to %s' % out)
        self._load(out)

    def _load(self, folder, force=False):
        if self.busy:
            return
        if not force and self.p and self.p.has_edits() and not messagebox.askyesno(
                'Open another folder?', 'The current draft (unbuilt changes) is kept in its draft file and offered again '
                'when you open %s. Open another folder?' % self.p.game_dir):
            return
        if not keysetup.ensure_key(self, folder):
            return
        if not force:
            self._autosave_now()
        self._stop_autosave()
        self._busy(True, 'Loading...')

        def run():
            try:
                p = M.Project().load(folder, progress=lambda m: self.q.put(('status', m)))
                self.q.put(('loaded', p))
            except Exception as e:
                self.q.put(('error', 'Loading failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                try:
                    self._handle(kind, rest)
                except Exception as e:                      # never leave a half-filled window without a message
                    import traceback
                    traceback.print_exc()
                    self._busy(False, 'Internal error: %s' % e)
                    messagebox.showerror('Internal error', '%s\n\n%s' % (type(e).__name__, e))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, kind, rest):
        if True:
            if True:
                if kind == 'status':
                    self._set_status(rest[0])
                elif kind == 'loaded':
                    self.p = rest[0]
                    self.p.settings.update(self.settings)
                    self.path_var.set(self.p.game_dir)
                    self.settings['last_dir'] = self.p.game_dir
                    save_settings(self.settings)
                    self.cur = ALL_KEY
                    self._busy(False, 'Loaded %d songs. Drag songs onto a genre, double-click a title to rename it.'
                               % len(self.p.songs))
                    self._fill_genres()
                    self.refresh()
                    self._offer_draft()
                    lost = [s for s in self.p.missing_audio() if not s.new_audio and not s.copy_from]
                    if lost:
                        messagebox.showwarning(
                            'Songs without audio',
                            '%d song(s) in the playlists of this folder have no audio: the file is missing from '
                            'carchive_music.rpf, or it is the 1 s of silence of a removed song whose space was freed '
                            '(shown as NO AUDIO! in the list). They would be silent / may crash in the game.\n\nUse '
                            '"Restore missing audio..." (your source files, or the original game folder).' % len(lost))
                elif kind == 'error':
                    self._busy(False, rest[0])
                    messagebox.showerror('Error', rest[0])
                elif kind == 'done':
                    self._busy(False, rest[0])
                    messagebox.showinfo('Done', rest[1])
                elif kind == 'saved':
                    self._stop_autosave()                           # the draft is in the folder now: its file goes
                    self.p.delete_draft()
                    self._busy(False, rest[0])
                    messagebox.showinfo('Saved', rest[1])
                    self._load(self.p.game_dir, force=True)         # show the saved state (the draft is empty now)
                elif kind == 'xprogress':
                    self._iso_progress(*rest)
                elif kind == 'extracted':
                    self._iso_finished(*rest)
                elif kind == 'play':
                    self._busy(False, 'Playing: %s' % rest[1])
                    try:
                        self.player.play(rest[0])
                    except Exception as e:
                        messagebox.showerror('Preview', str(e))

    # ------------------------------------------------------------------ genre list / song list
    def _gname(self, key):
        return self.p.genre_ui(key) if self.p else M.GENRE_UI[key]

    def _glabel(self, key):
        return self.p.genre_label(key) if self.p else M.GENRE_UI[key]

    def _eor_mark(self, s):
        """'' = nothing will change about the song's end-of-race clip (it has none, or keeps the original one); 'custom' =
        a file was chosen for it; 'auto' = an automatic excerpt will be made (new song, or its audio was replaced).
        A '(saved)' suffix means this is not a pending change - a previous build/save already applied it (recorded in
        the .mcla_changes.json next to the folder, since the game files themselves don't say a clip isn't the original)."""
        src = self.p._eor_source(s)
        if src:
            if s.added:
                if not self.p.settings.get('eor', True):
                    return ''                      # new songs are set to not get a clip at all
            elif not self.p.eor_name_of(s):
                return ''                          # this song has no end-of-race object - nothing to update
            return 'custom' if src[1] else 'auto'
        if s.eor_loaded:
            return '%s (saved)' % (s.eor_loaded[0] or 'auto')
        return ''

    def _audio_mark(self, s):
        """'Audio file' column: the current draft's replacement (if any), else a previous save's (marked '(saved)',
        see `_eor_mark`), else the file hash of the (still original) song file."""
        if s.new_audio:
            return 'NEW: ' + os.path.basename(s.new_audio)
        if s.copy_from:
            return 'FROM: ' + os.path.basename(s.copy_from.rstrip('\\/'))
        if s.audio_missing or (s.audio_freed and not s.removed):
            return 'NO AUDIO!'
        if self.p.free_removed and s.removed and s in self.p.freeable():
            return 'to be freed'
        if s.audio_freed:
            return 'freed (silent)'
        if s.audio_loaded:
            return '%s (saved)' % os.path.basename(s.audio_loaded)
        return s.file_hash or ''

    def _rebuild_move_menu(self):
        self.move_menu.delete(0, 'end')
        for key, _, _ in M.GENRES:
            self.move_menu.add_command(label=self._glabel(key), command=lambda k=key: self._move_selected_to(k))

    def _fill_genres(self):
        self.gtree.delete(*self.gtree.get_children())
        self.gtree.insert('', 'end', iid=ALL_KEY, text='All songs', values=('',))
        for key, _, _ in M.GENRES:
            self.gtree.insert('', 'end', iid=key, text=self._glabel(key), values=('',))
        self._rebuild_move_menu()
        self.gtree.insert('', 'end', iid=REMOVED_KEY, text='Removed', values=('',))
        self.gtree.selection_set(self.cur)

    def _update_genre_counts(self):
        cnt, orig = self.p.counts(), self.p.orig_counts()
        total = sum(cnt.values())
        self.gtree.set(ALL_KEY, 'n', str(total))
        nrem = len(self.p.removed_songs())
        self.gtree.set(REMOVED_KEY, 'n', str(nrem) if nrem else '')
        self.gtree.item(REMOVED_KEY, tags=('changed',) if nrem else ())
        for key, _, _ in M.GENRES:
            n = cnt[key]
            self.gtree.set(key, 'n', str(n) if n == orig[key] else '%d (was %d)' % (n, orig[key]))
            changed = n != orig[key] or key in self.p.genre_new or any(self.p.state(s) for s in self.p.genre_list(key))
            self.gtree.item(key, text=self._glabel(key) + ('  - EMPTY' if n == 0 else ''))
            self.gtree.item(key, tags=(('changed',) if changed else ()) + (('empty',) if n == 0 else ()))

    def _rows(self):
        p = self.p
        if self.cur == REMOVED_KEY:
            rows = list(enumerate(p.removed_songs(), 1))
        elif self.cur == ALL_KEY:
            rows = [(i, s) for g, _, _ in M.GENRES for i, s in enumerate(p.genre_list(g), 1)]
        else:
            rows = list(enumerate(p.genre_list(self.cur), 1))
        f = self.filter_var.get().strip().lower()
        if f:
            rows = [(i, s) for i, s in rows if f in s.title.lower() or f in s.obj.lower()]
        return rows

    def refresh(self, select=None):
        if not self.p:
            return
        keep = select if select is not None else self._selected()
        self.tree.delete(*self.tree.get_children())
        for i, s in self._rows():
            self.tree.insert('', 'end', iid=s.obj, tags=('changed',) if self.p.state(s) else (), values=(
                i, s.title, '%d/%d' % (len(s.title), s.title_max), self._gname(s.genre), self._eor_mark(s),
                M.MANAGER_UI[s.manager], fmt_time(s.seconds), fmt_mb(s.file_size), self._audio_mark(s)))
        keep = [k for k in keep if self.tree.exists(k)]
        if keep:
            self.tree.selection_set(keep)
            self.tree.see(keep[0])
        self._update_genre_counts()
        n = len(self.p.changes())
        self._set_status('%d song(s) shown. Draft: %d change(s).' % (len(self.tree.get_children()), n)
                         if n else '%d song(s) shown. No changes yet.' % len(self.tree.get_children()))
        self._update_buttons()
        if self._autosave_ok:                            # every edit ends with refresh(): save the draft shortly after
            if self._autosave_job:
                self.after_cancel(self._autosave_job)
            self._autosave_job = self.after(700, self._autosave_now)

    # ------------------------------------------------------------------ draft file
    def _autosave_now(self):
        """write the draft to <folder>.mcla_draft.json next to the game folder (removed when there are no edits)"""
        self._autosave_job = None
        if not self.p or not self._autosave_ok:
            return
        try:
            t = self.p.save_draft()
            self.title(APP_TITLE + (' - draft saved %s' % t[11:] if t else ''))
        except OSError as e:
            self._set_status('Could not save the draft file: %s' % e)

    def _stop_autosave(self):
        self._autosave_ok = False
        if self._autosave_job:
            self.after_cancel(self._autosave_job)
            self._autosave_job = None

    def _offer_draft(self):
        """right after loading: a draft file left from the last session (closed without building, or a crash)"""
        d = self.p.load_draft()
        if d:
            if messagebox.askyesno(
                    'Unsaved draft', 'A draft of this folder was saved on %s with %s change(s) and has not been built / saved '
                    '(the editor was closed or crashed).\n\nRestore it?\n\n"No" sets it aside as %s.'
                    % (d.get('saved', '?'), d.get('changes', '?'),
                       os.path.basename(M._draft_path(self.p.game_dir))[:-len('.json')] + '.old.json')):
                warn = self.p.apply_draft(d)
                self._fill_genres()
                self._autosave_ok = True
                self.refresh()
                self._set_status('Draft restored: %d change(s).' % len(self.p.changes()))
                if warn:
                    messagebox.showwarning('Draft restored', 'Restored, except:\n\n' + '\n'.join(warn[:20]) +
                                           ('\n... and %d more' % (len(warn) - 20) if len(warn) > 20 else ''))
                return
            self.p.discard_draft()
        self._autosave_ok = True

    def _on_genre_double(self, ev):
        key = self.gtree.identify_row(ev.y)
        if key in M.GENRE_UI:
            self._rename_genre(key)
            return 'break'

    def _on_genre_menu(self, ev):
        key = self.gtree.identify_row(ev.y)
        if key not in M.GENRE_UI or not self.p or self.busy:
            return
        m = tk.Menu(self, tearoff=False)
        m.add_command(label='Rename genre...', command=lambda: self._rename_genre(key))
        if key in self.p.genre_new:
            m.add_command(label='Undo the change in this session', command=lambda: self._undo_genre(key))
        if self.p.genre_renamed(key):
            m.add_command(label='Restore the original names of the game', command=lambda: self._restore_genre(key))
        m.tk_popup(ev.x_root, ev.y_root)

    def _undo_genre(self, key):
        self.p.revert_genre_name(key)
        self._fill_genres()
        self.refresh(select=[])

    def _restore_genre(self, key):
        """back to the names of the original game (a change of the draft: applied when you build / save)"""
        self.p.set_genre_name(key, list(M.GENRE_NAMES_ORIG[key]))
        self._fill_genres()
        self.refresh(select=[])

    def _rename_genre(self, key):
        """Change how a genre is called in the game menus (7 genres are built into the game: they can be renamed, not added)."""
        if not self.p or self.busy:
            return
        win = tk.Toplevel(self)
        win.title('Rename genre')
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack()
        cur = self.p.genre_texts(key)
        orig = M.GENRE_NAMES_ORIG[key]
        head = 'Genre: %s   (original names of the game: %s)' % (M.GENRE_UI[key], ' / '.join(orig))
        if self.p.genre_loaded.get(key) and self.p.genre_loaded[key] != orig:
            head += '\nSaved in this folder: %s' % ' / '.join(self.p.genre_loaded[key])
        ttk.Label(f, text=head, font=('TkDefaultFont', 10, 'bold'), wraplength=520, justify='left').grid(row=0, column=0, columnspan=2, sticky='w')
        vars_ = [tk.StringVar(value=x) for x in cur]
        same = tk.BooleanVar(value=len(set(cur)) == 1 or True)
        ents = []
        for i, lang in enumerate(M.LANGUAGES):
            ttk.Label(f, text=lang + ':').grid(row=i + 2, column=0, sticky='w', pady=2)
            e = ttk.Entry(f, textvariable=vars_[i], width=34)
            e.grid(row=i + 2, column=1, sticky='w', padx=6)
            ents.append(e)

        def sync(*_):
            if same.get():
                for i in range(1, len(vars_)):
                    vars_[i].set(vars_[0].get())
                    ents[i].configure(state='disabled')
            else:
                for e in ents:
                    e.configure(state='normal')
        vars_[0].trace_add('write', sync)
        ttk.Checkbutton(f, variable=same, text='The same text in all languages', command=sync).grid(row=1, column=0, columnspan=2, sticky='w', pady=(6, 2))
        if len(set(cur)) > 1:
            same.set(False)
        sync()
        ttk.Label(f, foreground='#666', wraplength=520, justify='left',
                  text='Only the name shown in the game menus changes (at most %d characters, capital letters). The genre keeps its '
                       'place in the menu and its songs; the game has exactly 7 genres, so genres can be renamed but not added or '
                       'removed.' % M.GENRE_NAME_MAX).grid(row=9, column=0, columnspan=2, sticky='w', pady=(8, 2))

        def ok():
            texts = [v.get() for v in vars_]
            try:
                self.p.set_genre_name(key, texts)
            except M.EditError as e:
                messagebox.showwarning('Rename genre', str(e), parent=win)
                return
            win.destroy()
            self._fill_genres()
            self.refresh(select=[])
            self._set_status('Genre renamed: %s. It is applied when you build.' % self.p.genre_texts(key)[0])
        b = ttk.Frame(win, padding=(12, 0, 12, 12))
        b.pack(fill='x')
        ttk.Button(b, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(b, text='OK', command=ok).pack(side='right', padx=6)
        if self.p.genre_renamed(key):
            ttk.Button(b, text='Restore the original names', command=lambda: (win.destroy(), self._restore_genre(key))).pack(side='left')
        win.grab_set()
        ents[0].focus_set()

    def _on_genre_select(self, _):
        sel = self.gtree.selection()
        if sel and sel[0] != self.cur:
            self.cur = sel[0]
            self.refresh(select=[])

    def _goto(self, genre, select):
        self.cur = genre
        self.gtree.selection_set(genre)
        self.refresh(select=select)

    # ------------------------------------------------------------------ edits
    def _guard(self, fn, *a):
        try:
            return fn(*a)
        except M.EditError as e:
            messagebox.showwarning('Cannot do that', str(e))
            return None

    def _move_selected_to(self, genre, index=None):
        sel = self._selected()
        if not sel or self.busy:
            return
        moved = 0
        for obj in sel:
            if self.p.songs[obj].genre == genre and not self.p.songs[obj].removed:
                continue
            self._guard(self.p.move_to_genre, obj, genre, None if index is None else index + moved)
            if self.p.songs[obj].genre != genre or self.p.songs[obj].removed:        # refused (message already shown)
                break
            moved += 1
        if moved:
            self._goto(genre, sel)
            self._set_status('Moved %d song(s) to %s.' % (moved, self._gname(genre)))

    def _on_move_within(self, delta):
        sel = self._selected()
        if len(sel) != 1 or self.cur in (ALL_KEY, REMOVED_KEY) or self.filter_var.get().strip():
            return 'break'
        if not self.p.move_within(sel[0], delta):
            self._set_status('Already at the %s of its group (base and SC songs stay in their own block).'
                             % ('top' if delta < 0 else 'bottom'))
        self.refresh(select=sel)
        return 'break'

    def _on_rename(self):
        sel = self._selected()
        if len(sel) == 1:
            self._begin_rename(sel[0])

    def _on_double(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != 'cell' or self.busy:
            return
        iid = self.tree.identify_row(ev.y)
        if iid and self.tree.identify_column(ev.x) == '#2':
            self._begin_rename(iid)

    def _begin_rename(self, obj):
        bb = self.tree.bbox(obj, 'title')
        if not bb:
            return
        s = self.p.songs[obj]
        x, y, w, h = bb
        var = tk.StringVar(value=s.title)
        ent = ttk.Entry(self.tree, textvariable=var)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, 'end')
        state = {'done': False}

        def counter(*_):
            n = len(' '.join(var.get().split()))
            self._set_status('Title length %d / %d%s' % (n, s.title_max, '  - TOO LONG' if n > s.title_max else ''))
        var.trace_add('write', counter)
        counter()

        def finish(commit, quiet):
            if state['done']:
                return
            if commit:
                try:
                    self.p.set_title(obj, var.get())
                except M.EditError as e:
                    if quiet:
                        commit = False
                    else:
                        messagebox.showwarning('Cannot rename', str(e))
                        ent.focus_set()
                        return
            state['done'] = True
            ent.destroy()
            self.refresh(select=[obj])
        ent.bind('<Return>', lambda e: finish(True, False))
        ent.bind('<Escape>', lambda e: finish(False, True))
        ent.bind('<FocusOut>', lambda e: finish(True, True))

    def _on_remove(self):
        sel = [o for o in self._selected() if not self.p.songs[o].removed]
        if self.busy or not sel:
            return 'break'
        done = 0
        for obj in sel:
            self._guard(self.p.remove_song, obj)
            if obj in self.p.songs and not self.p.songs[obj].removed:     # refused (message already shown)
                break
            done += 1
        self.refresh(select=[])
        if done:
            self._set_status('Removed %d song(s) from the playlists (see "Removed" on the left; Revert or drag back to restore).' % done)
        return 'break'

    def _on_revert(self):
        sel = self._selected()
        for obj in sel:
            self.p.revert(obj)
        if sel:
            self.refresh(select=sel)

    def _on_revert_all(self):
        if self.p and messagebox.askyesno('Revert all', 'Discard every staged change?'):
            self.p.revert_all()
            self.refresh(select=[])

    def _on_right_click(self, ev):
        iid = self.tree.identify_row(ev.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = self.ctx
        m.delete(0, 'end')
        sub = tk.Menu(m, tearoff=False)
        for key, _, _ in M.GENRES:
            sub.add_command(label=self._glabel(key), command=lambda k=key: self._move_selected_to(k))
        m.add_cascade(label='Move to genre', menu=sub)
        m.add_command(label='Rename...  (F2)', command=self._on_rename)
        m.add_command(label='Remove song  (Del)', command=self._on_remove)
        m.add_command(label='Revert  (Backspace)', command=self._on_revert)
        m.add_separator()
        m.add_command(label='End-of-race clip...', command=self._on_eor_clip)
        m.add_command(label='Preview', command=self._on_preview)
        m.tk_popup(ev.x_root, ev.y_root)

    # ------------------------------------------------------------------ drag & drop (inside the window)
    def _drag_start(self, ev):
        iid = self.tree.identify_row(ev.y)
        if not iid or self.tree.identify_region(ev.x, ev.y) != 'cell':
            self._drag = None
            return
        held = iid in self.tree.selection() and not (ev.state & 0x0005)    # shift/ctrl not pressed
        self._drag = {'iid': iid, 'x': ev.x, 'y': ev.y, 'moved': False, 'held': held}
        if held:
            return 'break'              # keep a multi-selection while dragging

    def _drag_motion(self, ev):
        d = self._drag
        if not d:
            return
        if not d['moved'] and (abs(ev.x - d['x']) > 6 or abs(ev.y - d['y']) > 6):
            d['moved'] = True
            self.tree.configure(cursor='hand2')
        if d['moved']:
            w = self.winfo_containing(ev.x_root, ev.y_root)
            if w is self.gtree:
                row = self.gtree.identify_row(ev.y_root - self.gtree.winfo_rooty())
                if row and row != ALL_KEY:
                    self._set_status('Drop to move to %s' % self._gname(row))
            return 'break' if d['held'] else None

    def _drag_end(self, ev):
        d, self._drag = self._drag, None
        self.tree.configure(cursor='')
        if not d:
            return
        if not d['moved']:
            if d['held']:                       # plain click on an already selected row
                self.tree.selection_set(d['iid'])
            return
        w = self.winfo_containing(ev.x_root, ev.y_root)
        if w is self.gtree:
            row = self.gtree.identify_row(ev.y_root - self.gtree.winfo_rooty())
            if row and row != ALL_KEY:
                self._move_selected_to(row)
        elif w is self.tree and self.cur != ALL_KEY and not self.filter_var.get().strip():
            row = self.tree.identify_row(ev.y_root - self.tree.winfo_rooty())
            sel = self._selected()
            if row and len(sel) == 1 and row != sel[0]:
                self.p.move_to_index(sel[0], self.tree.index(row))
                self.refresh(select=sel)

    # ------------------------------------------------------------------ audio
    def _wav_for(self, s):
        tmp = os.path.join(tempfile.gettempdir(), M.TMP_DIR)
        os.makedirs(tmp, exist_ok=True)
        # the file hashes are the same as in the X360 game: this cache lives in its own folder (M.TMP_DIR)
        wav = os.path.join(tmp, '%s_%d.wav' % (s.file_hash, s.file_size or 0))
        if not os.path.exists(wav):
            part = wav + '.part.wav'
            self.p.decode_wav(self.p.song_bytes(s), part, tuple(s.stream_ids or ()) or s.wave, self.ffmpeg)
            os.replace(part, wav)
        return wav

    def _on_preview(self):
        sel = self._selected()
        if len(sel) != 1 or self.busy:
            return
        s = self.p.songs[sel[0]]
        self._busy(True, 'Decoding %s...' % s.title)

        def run():
            try:
                self.q.put(('play', self._wav_for(s), s.title))
            except Exception as e:
                self.q.put(('error', 'Preview failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    def _on_stop(self):
        self.player.stop()

    def _on_export_wav(self):
        sel = self._selected()
        if len(sel) != 1 or self.busy:
            return
        s = self.p.songs[sel[0]]
        name = re.sub(r'[<>:"/\\|?*]', '_', s.title) + '.wav'
        dst = filedialog.asksaveasfilename(title='Export WAV', initialfile=name, defaultextension='.wav',
                                           filetypes=[('WAV audio', '*.wav')])
        if not dst:
            return
        self._busy(True, 'Decoding %s...' % s.title)

        def run():
            import shutil
            try:
                shutil.copyfile(self._wav_for(s), dst)
                self.q.put(('done', 'Exported.', 'Saved %s' % dst))
            except Exception as e:
                self.q.put(('error', 'Export failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    def _on_csv(self):
        dst = filedialog.asksaveasfilename(title='Export list', initialfile='mcla_tracklist.csv',
                                           defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if dst:
            self.p.export_csv(dst)
            self._set_status('Saved %s' % dst)

    # ------------------------------------------------------------------ build
    def _confirm_text(self):
        ch = self.p.changes()
        lines = ch[:18] + (['... and %d more' % (len(ch) - 18)] if len(ch) > 18 else [])
        text = '%d change(s):\n\n%s' % (len(ch), '\n'.join('  ' + l for l in lines))
        empty = self.p.empty_genres()
        if empty:
            text += ('\n\nWARNING: %d genre(s) have no songs: %s. A save that was last playing one of these genres makes '
                     'the game QUIT while it loads (tested on a real PS3 and in RPCS3; the Xbox 360 version freezes the same '
                     'way). The music menu itself only shows an empty list. Keep at least one song in every genre.'
                     % (len(empty), ', '.join(self._gname(g) for g in empty)))
        silent = [s for s in self.p.missing_audio() if not s.new_audio and not s.copy_from]
        if silent:
            text += ('\n\nWARNING: %d song(s) in the playlists have no audio (NO AUDIO! in the list) and would be silent: '
                     '%s. Use "Restore missing audio..." first.' % (len(silent), ', '.join(s.title for s in silent[:5])
                                                                     + (' ...' if len(silent) > 5 else '')))
        return text

    def _audio_ready(self):
        """ffmpeg is needed when the draft contains replaced audio (it encodes the MP3 too)."""
        if not self.p.needs_encoder():
            return True
        st = self.p.settings
        if not st.get('ffmpeg') or not os.path.isfile(st['ffmpeg']):
            messagebox.showwarning('Audio settings', 'ffmpeg was not found. Open "Audio settings...".')
            return False
        return True

    def _on_build_archives(self):
        if not self.p or self.busy or not self._audio_ready():
            return
        dst = filedialog.askdirectory(title='Folder for the changed archives (new or empty)', mustexist=False,
                                      initialdir=os.path.dirname(self.p.game_dir))
        if not dst:
            return
        if os.path.abspath(dst).lower() == os.path.abspath(self.p.game_dir).lower():
            messagebox.showerror('Not allowed', 'The original game folder is never modified.\nChoose another folder.')
            return
        audio = self.p.needs_encoder()
        if not messagebox.askyesno('Build changed archives', self._confirm_text() +
                                   '\n\nOnly the archives that changed are written (carchive_cache.rpf ~2 GB'
                                   + (', carchive_music.rpf ~0.7 GB, carchive_audio.rpf ~1.7 GB' if audio else '') +
                                   ', inside PS3_GAME\\USRDIR); the originals are not touched.\nContinue?',
                                   icon='warning' if self.p.empty_genres() else 'question'):
            return
        self._busy(True, 'Building...')

        def run():
            try:
                written = self.p.build_archives(dst, progress=lambda m: self.q.put(('status', m)))
                self.q.put(('done', 'Built %s' % dst,
                            'Written to:\n%s\n\n%s\n\nCopy these files into the same places of the game folder (keep '
                            'backups of the originals). The file names must stay lower case.'
                            % (dst, '\n'.join('  ' + w for w in written))))
            except Exception as e:
                self.q.put(('error', 'Build failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ audio replacement
    def _on_replace_audio(self):
        sel = self._selected()
        if len(sel) != 1 or self.busy:
            return
        s = self.p.songs[sel[0]]
        path = filedialog.askopenfilename(
            title='Audio file that replaces "%s"' % s.title,
            filetypes=[('Audio files', '*.wav *.mp3 *.flac *.ogg *.m4a *.wma *.aac'), ('All files', '*.*')])
        if not path:
            return
        self._guard(self.p.set_audio, s.obj, path)
        self.refresh(select=[s.obj])
        if s.new_audio:
            self._set_status('Staged: "%s" will get the audio of %s (encoded when you build).'
                             % (s.title, os.path.basename(path)))

    AUDIO_TYPES = [('Audio files', '*.wav *.mp3 *.flac *.ogg *.opus *.m4a *.aac *.wma *.aiff'), ('All files', '*.*')]

    def _on_free(self):
        """stage (or undo) freeing the space the removed songs still take in the archives"""
        if not self.p or self.busy:
            return
        if self.p.free_removed:
            if messagebox.askyesno('Keep the removed songs\' audio', 'Do not free the space of the removed songs '
                                   '(their audio stays in the archives)?'):
                self.p.free_removed = False
                self.refresh()
            return
        songs = self.p.freeable()
        if not songs:
            return
        self.config(cursor='watch')
        self.update()
        try:
            est = self.p.free_estimate()
        finally:
            self.config(cursor='')
        mb = lambda a: est.get(a, 0) / 2 ** 20
        if not messagebox.askyesno(
                'Free space of removed songs',
                '%d removed song(s) still take %.0f MB in carchive_music.rpf and %.0f MB in carchive_audio.rpf '
                '(about %d new songs\' worth in carchive_music.rpf).\n\nFree this space?\n\n'
                '- Their audio and end-of-race clips become 1 s of silence when you build / save. The game data keeps '
                'working: nothing points to a missing file.\n'
                '- They stay in the Removed list. The garage stops playing them (it did so far).\n'
                '- To put such a song back into a playlist later it needs audio again: "Restore missing audio..." '
                '(songs of the original soundtrack are copied from the original game folder).'
                % (len(songs), mb('music'), mb('audio'), mb('music') / 7.5)):
            return
        self.p.free_removed = True
        self.refresh()
        self._set_status('Staged: the space of %d removed song(s) is freed when you build / save.' % len(songs))

    def _on_relink(self):
        """Songs that are in the game data but whose audio is missing from carchive_music.rpf: choose the source files
        (any number at once); each file is matched to a song by its artist/title (tags or file name)."""
        if not self.p or self.busy:
            return
        lost = [s for s in self.p.missing_audio() if not s.new_audio and not s.copy_from]
        if not lost:
            return
        choice = messagebox.askyesnocancel(
            'Restore missing audio', '%d song(s) in the playlists have no audio.\n\nYes: take their audio (and end-of-race '
            'clips) from another game folder, e.g. your ORIGINAL game - for songs of the original soundtrack, no encoding.\n'
            'No: choose audio files (your sources), matched to the songs by artist / title.' % len(lost))
        if choice is None:
            return
        if choice:
            folder = filedialog.askdirectory(title='Game folder to take the audio from (e.g. the original game)')
            if not folder:
                return
            self.config(cursor='watch')
            self.update()
            try:
                res = self._guard(self.p.set_copy_from, folder)
            finally:
                self.config(cursor='')
            if res:
                staged, missing = res
                self.refresh(select=[s.obj for s in staged])
                msg = '%d song(s) will get their audio from\n%s\n(copied when you build).' % (len(staged), folder)
                if missing:
                    msg += '\n\nNot in that folder (%d) - use "No" (your files) or "Replace audio...":\n  ' % len(missing) + \
                           '\n  '.join(s.title for s in missing[:15]) + ('\n  ...' if len(missing) > 15 else '')
                messagebox.showinfo('Restore missing audio', msg)
            return
        paths = filedialog.askopenfilenames(
            title='Source audio files of the songs without audio (%d song(s) need audio)' % len(lost), filetypes=self.AUDIO_TYPES)
        if not paths:
            return
        import audio_meta
        self.config(cursor='watch')
        self._set_status('Reading tags of %d file(s)...' % len(paths))
        self.update()
        try:
            info = audio_meta.guess_batch(list(paths), self.ffmpeg)
            found, unmatched = self.p.match_audio_files([(p, i['artist'], i['title']) for p, i in zip(paths, info)])
        finally:
            self.config(cursor='')
        for obj, path in found.items():
            self._guard(self.p.set_audio, obj, path)
        self.refresh(select=list(found))
        left = [s.title for s in lost if s.obj not in found]
        msg = '%d of %d file(s) matched a song and are staged (the audio is encoded when you build).' % (len(found), len(paths))
        if unmatched:
            msg += '\n\nNo song found for:\n  ' + '\n  '.join(os.path.basename(p) for p in unmatched[:15]) + (
                '\n  ... and %d more' % (len(unmatched) - 15) if len(unmatched) > 15 else '')
        if left:
            msg += '\n\nStill without audio (%d):\n  ' % len(left) + '\n  '.join(left[:15]) + (
                '\n  ... and %d more' % (len(left) - 15) if len(left) > 15 else '')
            msg += '\n\nFor these, select the song and use "Replace audio...", or run this again with more files.'
        messagebox.showinfo('Restore missing audio', msg)

    def _on_add_song(self):
        """Add songs dialog: several audio files at once (any format ffmpeg reads); artist / title come from the tags, else
        from the file name (track numbers and random ids are dropped); everything can be edited before adding."""
        if not self.p or self.busy:
            return
        room = self.p.add_capacity()
        if room <= 0:
            messagebox.showwarning('Add songs', 'No room left in the archive table of carchive_music.rpf.')
            return
        import audio_meta
        win = tk.Toplevel(self)
        win.title('Add songs')
        win.transient(self)
        win.geometry('1120x480')
        win.minsize(860, 320)
        ui_names = [self._glabel(k) for k, _, _ in M.GENRES]
        key_of = {self._glabel(k): k for k, _, _ in M.GENRES}
        rows = {}                                        # iid -> dict(path, artist, title, genre, src)
        counter = [0]

        top = ttk.Frame(win, padding=(8, 8, 8, 0))
        top.pack(fill='x')
        v_genre = tk.StringVar(value=self._glabel(self.cur) if self.cur in M.GENRE_UI else ui_names[0])
        cols = ('file', 'artist', 'title', 'genre', 'clip', 'note', 'src')
        heads = {'file': 'File', 'artist': 'Artist', 'title': 'Title', 'genre': 'Genre', 'clip': 'End-of-race clip',
                 'note': '', 'src': 'Read from'}
        tf = ttk.Frame(win, padding=8)
        tf.pack(fill='both', expand=True)
        tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='extended')
        for c, w in zip(cols, (220, 170, 220, 95, 140, 160, 65)):
            tree.heading(c, text=heads[c])
            tree.column(c, width=w, anchor='w', stretch=(c in ('file', 'title')))
        tree.tag_configure('dup', background='#ffe1a8')
        tree.bind('<Control-KeyPress>', lambda e: self._ctrl_a(e, tree), add='+')
        vsb = ttk.Scrollbar(tf, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        status = tk.StringVar()

        def title_key(t):
            return re.sub(r'\s+', ' ', t).strip().upper()

        existing_titles = {}          # normalized song title (without the "ARTIST - " part) -> a full title that has it
        for sng in self.p.songs.values():
            t = sng.title or ''
            part = t.split(' - ', 1)[1] if ' - ' in t else t
            existing_titles.setdefault(title_key(part), t)

        def refresh_notes():
            """Mark rows whose title already belongs to a song in the game, or repeats another row of this batch."""
            from collections import Counter
            cnt = Counter(title_key(r['title']) for r in rows.values())
            for iid, r in rows.items():
                key = title_key(r['title'])
                if key in existing_titles:
                    note = 'a song is already called "%s"' % existing_titles[key]
                elif cnt[key] > 1:
                    note = 'same title as another row here'
                else:
                    note = ''
                vals = list(tree.item(iid, 'values'))
                if vals:
                    vals[5] = note
                    tree.item(iid, values=vals, tags=(('dup',) if note else ()))

        def show(iid):
            r = rows[iid]
            tree.item(iid, values=(os.path.basename(r['path']), r['artist'], r['title'], r['genre'],
                                   os.path.basename(r['clip']) if r['clip'] else 'automatic', '', r['src']))

        def update_status():
            n = len(rows)
            status.set('%d file(s) selected, %d more song(s) fit into the archive tables.%s' % (
                n, room, '   TOO MANY - remove at least %d row(s) (Delete key or "Remove selected").' % (n - room) if n > room else ''))
            btn_add.configure(state='normal' if rows and n <= room else 'disabled', text='Add %d song(s)' % n if n else 'Add')

        def add_files():
            paths = filedialog.askopenfilenames(parent=win, title='Audio files of the new songs (several can be selected)',
                                                filetypes=self.AUDIO_TYPES)
            paths = [p for p in paths if p not in {r['path'] for r in rows.values()}]
            if not paths:
                return
            win.configure(cursor='watch')
            status.set('Reading tags of %d file(s)...' % len(paths))
            win.update()
            try:
                info = audio_meta.guess_batch(paths, self.ffmpeg)
            finally:
                win.configure(cursor='')
            for pth, i in zip(paths, info):
                counter[0] += 1
                iid = 'r%d' % counter[0]
                rows[iid] = {'path': pth, 'artist': i['artist'], 'title': i['title'], 'genre': v_genre.get(), 'src': i['source'],
                             'clip': None}
                tree.insert('', 'end', iid=iid)
                show(iid)
            refresh_notes()
            update_status()

        def remove_sel():
            for iid in tree.selection():
                rows.pop(iid, None)
                tree.delete(iid)
            refresh_notes()
            update_status()

        def apply_all():
            for iid in rows:
                rows[iid]['genre'] = v_genre.get()
                show(iid)

        ttk.Button(top, text='Add files...', command=add_files).pack(side='left')
        ttk.Button(top, text='Remove selected', command=remove_sel).pack(side='left', padx=6)
        ttk.Label(top, text='Genre:').pack(side='left', padx=(18, 4))
        ttk.Combobox(top, textvariable=v_genre, values=ui_names, state='readonly', width=16).pack(side='left')
        ttk.Button(top, text='Set for all rows', command=apply_all).pack(side='left', padx=6)

        def edit_cell(ev):
            iid, col = tree.identify_row(ev.y), tree.identify_column(ev.x)
            if not iid or tree.identify_region(ev.x, ev.y) != 'cell':
                return
            name = cols[int(col[1:]) - 1]
            if name == 'genre':
                m = tk.Menu(win, tearoff=False)
                for ui in ui_names:
                    m.add_command(label=ui, command=lambda u=ui: (rows[iid].update(genre=u), show(iid)))
                m.tk_popup(ev.x_root, ev.y_root)
            elif name == 'clip':
                m = tk.Menu(win, tearoff=False)

                def choose(iid=iid):
                    f = filedialog.askopenfilename(parent=win, title='Audio file for the end-of-race clip of this song',
                                                   filetypes=self.AUDIO_TYPES)
                    if f:
                        rows[iid]['clip'] = f
                        show(iid)
                m.add_command(label='Choose a file...', command=choose)
                m.add_command(label='Automatic (loudest %d s of the song)' % int(self.settings.get('eor_seconds') or 20),
                              command=lambda: (rows[iid].update(clip=None), show(iid)))
                m.tk_popup(ev.x_root, ev.y_root)
            elif name in ('artist', 'title'):
                bb = tree.bbox(iid, col)
                if not bb:
                    return
                x, y, w, h = bb
                var = tk.StringVar(value=rows[iid][name])
                ent = ttk.Entry(tree, textvariable=var)
                ent.place(x=x, y=y, width=w, height=h)
                ent.focus_set()
                ent.select_range(0, 'end')
                done = [False]

                def finish(commit):
                    if done[0]:
                        return
                    done[0] = True
                    if commit:
                        rows[iid][name] = ' '.join(var.get().split())
                        rows[iid]['src'] = 'edited'
                        show(iid)
                        refresh_notes()
                    ent.destroy()
                ent.bind('<Return>', lambda e: finish(True))
                ent.bind('<Escape>', lambda e: finish(False))
                ent.bind('<FocusOut>', lambda e: finish(True))
        tree.bind('<Double-1>', edit_cell)
        tree.bind('<Delete>', lambda e: remove_sel())

        bottom = ttk.Frame(win, padding=(8, 0, 8, 8))
        bottom.pack(fill='x')
        ttk.Label(bottom, textvariable=status, foreground='#444').pack(side='left')
        ttk.Button(bottom, text='Cancel', command=win.destroy).pack(side='right')
        btn_add = ttk.Button(bottom, text='Add', state='disabled')
        btn_add.pack(side='right', padx=6)
        ttk.Label(win, foreground='#666', wraplength=1080, justify='left', padding=(8, 0, 8, 8),
                  text='Double-click Artist / Title / Genre / End-of-race clip to edit. Rows highlighted in orange have a title '
                       'that already belongs to a song in the game (or to another row here) - not blocked, just worth checking. '
                       'The songs are appended to the end of their genre lists; you can still rename, move or remove them. The audio '
                       'is encoded and all game entries (playlist, garage music, end-of-race clip) are created when you build '
                       '(MP3 quality: Audio settings...).'
                  ).pack(fill='x')

        def do_add():
            done_objs, last_genre = [], None
            for iid in list(rows):
                r = rows[iid]
                try:
                    s = self.p.add_song(r['artist'], r['title'], key_of[r['genre']], r['path'], r['clip'])
                except M.EditError as e:
                    messagebox.showwarning('Cannot add "%s"' % os.path.basename(r['path']), str(e), parent=win)
                    break
                done_objs.append(s.obj)
                last_genre = key_of[r['genre']]
                rows.pop(iid)
                tree.delete(iid)
            update_status()
            if done_objs and not rows:
                win.destroy()
            if done_objs:
                self._goto(last_genre, done_objs)
                self._set_status('Staged %d new song(s). They are created when you build.' % len(done_objs))
        btn_add.configure(command=do_add)
        update_status()
        win.grab_set()
        self.after(150, add_files)

    def _on_eor_clip(self):
        """Choose the file the end-of-race clip of the selected song is made from (or go back to automatic / original)."""
        sel = self._selected()
        if len(sel) != 1 or self.busy:
            return
        s = self.p.songs[sel[0]]
        cur = ('file %s' % os.path.basename(s.eor_audio)) if s.eor_audio else (
            'automatic excerpt of the new audio' if (s.added or (s.new_audio and self.p.settings.get('eor_follow_audio', True)))
            else 'the original clip')
        win = tk.Toplevel(self)
        win.title('End-of-race clip')
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack()
        ttk.Label(f, text=s.title, font=('TkDefaultFont', 10, 'bold')).pack(anchor='w')
        ttk.Label(f, text='Now: %s' % cur, foreground='#555').pack(anchor='w', pady=(2, 8))
        ttk.Label(f, wraplength=430, justify='left', foreground='#666',
                  text='The clip is a separate file that plays at the end of a race (originals: 10-51 s, median 21 s). '
                       'Automatic = the loudest %d s of the song\'s audio (replaced or new audio). A chosen file is used as it '
                       'is (at most 60 s) - prepare the excerpt you want in any audio editor.' % int(self.settings.get('eor_seconds') or 20)
                  ).pack(anchor='w', pady=(0, 10))

        def stop():
            self.player.stop()

        def close():
            stop()
            win.destroy()

        def choose():
            path = filedialog.askopenfilename(parent=win, title='Audio file for the end-of-race clip of "%s"' % s.title,
                                              filetypes=self.AUDIO_TYPES)
            if path:
                stop()
                self.config(cursor='watch')
                self.update()
                try:
                    self._guard(self.p.set_eor_audio, s.obj, path)
                finally:
                    self.config(cursor='')
                win.destroy()
                self.refresh(select=[s.obj])
                if s.eor_audio:
                    self._set_status('End-of-race clip of "%s" will be made from %s (encoded when you build).'
                                     % (s.title, os.path.basename(path)))

        def auto():
            stop()
            self.p.set_eor_audio(s.obj, None)
            win.destroy()
            self.refresh(select=[s.obj])
            self._set_status('End-of-race clip of "%s": %s.' % (s.title, 'automatic excerpt' if (s.added or s.new_audio) else 'original'))

        def preview():
            self.config(cursor='watch')
            win.update()
            try:
                wav = self.p.eor_preview_wav(s, self.ffmpeg)
                self.player.play(wav, loop=True)
            except Exception as e:
                messagebox.showerror('Preview', str(e), parent=win)
            finally:
                self.config(cursor='')

        pb = ttk.Frame(f)
        pb.pack(fill='x', pady=(0, 10))
        can_preview = self.player.available() and bool(self.ffmpeg)
        ttk.Button(pb, text='▶ Preview (loop)', command=preview, state='normal' if can_preview else 'disabled').pack(side='left')
        ttk.Button(pb, text='■ Stop', command=stop, state='normal' if self.player.available() else 'disabled').pack(side='left', padx=6)
        ttk.Separator(f, orient='horizontal').pack(fill='x', pady=(0, 10))
        b = ttk.Frame(f)
        b.pack(fill='x')
        ttk.Button(b, text='Choose a file...', command=choose).pack(side='left')
        ttk.Button(b, text='Automatic / original', command=auto).pack(side='left', padx=6)
        ttk.Button(b, text='Close', command=close).pack(side='right')
        win.protocol('WM_DELETE_WINDOW', close)
        win.grab_set()

    # ------------------------------------------------------------------ hangout music
    def _on_hangout(self):
        """The 6 music clips that play at the hangouts (racer meeting places): what they are now, what the next build
        makes of them; choose a song / a file for a slot, or leave it to the automatic rule."""
        if not self.p or self.busy:
            return
        p = self.p
        win = tk.Toplevel(self)
        win.title('Hangout music')
        win.transient(self)
        f = ttk.Frame(win, padding=10)
        f.pack(fill='both', expand=True)
        ttk.Label(f, wraplength=860, justify='left', foreground='#555',
                  text='At the 12 hangouts (the places where racers meet) the cars play 6 music clips, picked at random. '
                       'They are excerpts of soundtrack songs stored separately from the playlists. Automatic (Audio '
                       'settings): when a song of a clip gets new audio, the clip is made again from it; when it leaves the '
                       'playlists, the clip is taken from another song. You can also choose a song or a file for any slot.'
                  ).pack(anchor='w', pady=(0, 8))
        cols = ('n', 'orig', 'now', 'next')
        tree = ttk.Treeview(f, columns=cols, show='headings', height=6, selectmode='browse')
        for c, t, w in (('n', '#', 30), ('orig', 'Original song', 250), ('now', 'In this folder now', 260),
                        ('next', 'After the next build', 320)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor='center' if c == 'n' else 'w', stretch=c != 'n')
        tree.pack(fill='both', expand=True)

        def title_of(obj):
            s = p.songs.get(obj)
            return s.title if s else obj

        def fill():
            plan = p.hangout_plan()
            for i, (slot, orig) in enumerate(M.HANGOUT_SLOTS, 1):
                kind, val = p.hangout_owner(slot)
                now = title_of(val) if kind == 'song' else 'file %s' % os.path.basename(val or '')
                if val == orig and kind == 'song':
                    now += '  (original)'
                src = plan.get(slot)
                nxt = (p.hangout_label(src) + ('  - automatic: %s' % src['auto'] if src.get('auto') else '')) if src \
                    else ('kept (manual)' if p.hangout_new.get(slot, {}).get('kind') == 'keep' else 'unchanged')
                vals = (i, title_of(orig), now, nxt)
                if tree.exists(slot):
                    tree.item(slot, values=vals)
                else:
                    tree.insert('', 'end', iid=slot, values=vals)
            self.refresh()

        def cur():
            sel = tree.selection()
            return sel[0] if sel else None

        def parse_start(text):
            text = text.strip()
            if not text:
                return None
            m, _, s = text.rpartition(':')
            return float(m or 0) * 60 + float(s.replace(',', '.'))

        def choose():
            slot = cur()
            if not slot:
                return
            d = tk.Toplevel(win)
            d.title('Hangout music %d' % ([x for x, _ in M.HANGOUT_SLOTS].index(slot) + 1))
            d.transient(win)
            d.resizable(False, False)
            g = ttk.Frame(d, padding=10)
            g.pack()
            mode = tk.StringVar(value='song')
            songs = sorted((s for s in p.songs.values() if p._hangout_usable(s)), key=lambda s: s.title)
            ttk.Radiobutton(g, text='Song in the playlists:', variable=mode, value='song').grid(row=0, column=0, sticky='w')
            cb = ttk.Combobox(g, values=[s.title for s in songs], state='readonly', width=62)
            cb.grid(row=0, column=1, columnspan=2, sticky='w', padx=6)
            kind, val = p.hangout_owner(slot)
            if kind == 'song' and val in [s.obj for s in songs]:
                cb.current([s.obj for s in songs].index(val))
            ttk.Radiobutton(g, text='Audio file:', variable=mode, value='file').grid(row=1, column=0, sticky='w', pady=4)
            fv = tk.StringVar()
            ttk.Entry(g, textvariable=fv, width=52).grid(row=1, column=1, sticky='w', padx=6)
            ttk.Button(g, text='Browse...', command=lambda: (fv.set(filedialog.askopenfilename(
                parent=d, filetypes=self.AUDIO_TYPES) or fv.get()), mode.set('file'))).grid(row=1, column=2)
            ttk.Label(g, text='Start at (m:ss, empty = the loudest part):').grid(row=2, column=0, columnspan=2, sticky='w',
                                                                                   pady=(6, 0))
            sv = tk.StringVar()
            ttk.Entry(g, textvariable=sv, width=8).grid(row=2, column=2, sticky='w', pady=(6, 0))

            def ok():
                try:
                    start = parse_start(sv.get())
                except ValueError:
                    messagebox.showwarning('Hangout music', 'Start: seconds or m:ss.', parent=d)
                    return
                if mode.get() == 'song':
                    if cb.current() < 0:
                        messagebox.showwarning('Hangout music', 'Choose a song.', parent=d)
                        return
                    src = {'kind': 'song', 'obj': songs[cb.current()].obj, 'start': start}
                else:
                    src = {'kind': 'file', 'path': fv.get().strip(), 'start': start}
                try:
                    p.set_hangout(slot, src)
                except M.EditError as e:
                    messagebox.showwarning('Hangout music', str(e), parent=d)
                    return
                d.destroy()
                fill()
            bb = ttk.Frame(g)
            bb.grid(row=3, column=0, columnspan=3, sticky='e', pady=(10, 0))
            ttk.Button(bb, text='OK', command=ok).pack(side='left')
            ttk.Button(bb, text='Cancel', command=d.destroy).pack(side='left', padx=6)
            d.grab_set()

        def set_auto():
            if cur():
                p.set_hangout(cur(), None)
                fill()

        def keep():
            if cur():
                p.set_hangout(cur(), {'kind': 'keep'})
                fill()

        def play(planned):
            slot = cur()
            if not slot:
                return
            if planned and slot not in p.hangout_plan():
                messagebox.showinfo('Hangout music', 'This slot does not change with the next build.', parent=win)
                return
            win.config(cursor='watch')
            win.update()
            try:
                self.player.play(p.hangout_preview_wav(slot, self.ffmpeg, planned=planned))
            except Exception as e:
                messagebox.showerror('Preview', str(e), parent=win)
            finally:
                win.config(cursor='')

        def close():
            self.player.stop()
            win.destroy()
        b = ttk.Frame(f)
        b.pack(fill='x', pady=(8, 0))
        ttk.Button(b, text='Song / file...', command=choose).pack(side='left')
        ttk.Button(b, text='Automatic', command=set_auto).pack(side='left', padx=4)
        ttk.Button(b, text='Keep as it is', command=keep).pack(side='left')
        can = self.player.available() and bool(self.ffmpeg)
        ttk.Button(b, text='▶ Now', command=lambda: play(False), state='normal' if can else 'disabled').pack(
            side='left', padx=(18, 4))
        ttk.Button(b, text='▶ After build', command=lambda: play(True), state='normal' if can else 'disabled').pack(
            side='left')
        ttk.Button(b, text='■ Stop', command=self.player.stop).pack(side='left', padx=4)
        ttk.Button(b, text='Close', command=close).pack(side='right')
        tree.bind('<Double-1>', lambda e: choose())
        win.protocol('WM_DELETE_WINDOW', close)
        fill()
        tree.selection_set(M.HANGOUT_SLOTS[0][0])

    def _on_settings(self):
        win = tk.Toplevel(self)
        win.title('Audio settings')
        win.transient(self)
        win.resizable(False, False)
        st = self.settings
        vars_ = {k: tk.StringVar(value=str(st.get(k, ''))) for k in ('ffmpeg', 'q_mp3')}
        rows = (('ffmpeg:', 'ffmpeg', True), ('MP3 quality (LAME VBR 0-9, 0 = best):', 'q_mp3', False))
        f = ttk.Frame(win, padding=10)
        f.pack(fill='both')
        for i, (label, key, browse) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky='w', pady=3)
            ttk.Entry(f, textvariable=vars_[key], width=60 if browse else 8).grid(row=i, column=1, sticky='w', padx=6)
            if browse:
                ttk.Button(f, text='Browse...', command=lambda k=key: vars_[k].set(
                    filedialog.askopenfilename(title='Select file', filetypes=EXE_TYPES)
                    or vars_[k].get())).grid(row=i, column=2)
        v_eor = tk.BooleanVar(value=bool(st.get('eor', True)))
        ttk.Checkbutton(f, variable=v_eor, text='New songs also get an end-of-race clip (an excerpt of the song, or a '
                        'file you choose)').grid(row=4, column=0, columnspan=3, sticky='w', pady=(6, 0))
        v_follow = tk.BooleanVar(value=bool(st.get('eor_follow_audio', True)))
        v_secs = tk.StringVar(value=str(int(st.get('eor_seconds') or 20)))
        ttk.Checkbutton(f, variable=v_follow, text='Replacing the audio of a song also replaces its end-of-race clip '
                        '(automatic excerpt)').grid(row=5, column=0, columnspan=3, sticky='w', pady=(2, 0))
        ttk.Label(f, text='Length of automatic end-of-race clips (s, 5-60):').grid(row=6, column=0, columnspan=2, sticky='w', pady=(4, 0))
        ttk.Entry(f, textvariable=v_secs, width=6).grid(row=6, column=2, sticky='w', pady=(4, 0))
        ttk.Label(f, foreground='#666', wraplength=560, justify='left',
                  text='The audio is encoded by ffmpeg when you build: two mono MP3 streams (44.1 kHz, VBR, no bit '
                       'reservoir) like the game\'s own tracks. Quality 3 is about the size of the original music files '
                       '(~210 kbit/s); 2 is a little better and bigger, 4 smaller.').grid(row=7, column=0, columnspan=3,
                                                                                         sticky='w', pady=(8, 2))
        v_hfollow = tk.BooleanVar(value=bool(st.get('hangout_follow', True)))
        ttk.Checkbutton(f, variable=v_hfollow, text='Hangout music follows the songs: a replaced song gets a new excerpt '
                        'there, a removed song is replaced by another one').grid(row=8, column=0, columnspan=3, sticky='w',
                                                                                  pady=(6, 0))
        v_hsecs = tk.StringVar(value=str(int(st.get('hangout_seconds') or 60)))
        ttk.Label(f, text='Length of hangout music excerpts (s, 20-90):').grid(row=9, column=0, columnspan=2, sticky='w',
                                                                               pady=(4, 0))
        ttk.Entry(f, textvariable=v_hsecs, width=6).grid(row=9, column=2, sticky='w', pady=(4, 0))
        v_hnorm = tk.BooleanVar(value=bool(st.get('hangout_normalize', True)))
        ttk.Checkbutton(f, variable=v_hnorm, text='Bring hangout music to the loudness of the original clips (the '
                        'originals: about -15.5 LUFS, much quieter than the songs)').grid(row=10, column=0, columnspan=3,
                                                                                   sticky='w', pady=(4, 0))
        v_hlufs = tk.StringVar(value='%.1f' % float(st.get('hangout_lufs') or -15.5))
        ttk.Label(f, text='Loudness of hangout music (LUFS, -24...-8):').grid(row=11, column=0, columnspan=2, sticky='w',
                                                                              pady=(4, 0))
        ttk.Entry(f, textvariable=v_hlufs, width=6).grid(row=11, column=2, sticky='w', pady=(4, 0))

        def ok():
            try:
                qm = int(vars_['q_mp3'].get())
                if not 0 <= qm <= 9:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('Audio settings', 'MP3 quality must be a number from 0 to 9.', parent=win)
                return
            try:
                secs = float(v_secs.get().replace(',', '.'))
                if not 5 <= secs <= 60:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('Audio settings', 'The length of automatic clips must be 5-60 seconds.', parent=win)
                return
            try:
                hsecs = float(v_hsecs.get().replace(',', '.'))
                if not 20 <= hsecs <= 90:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('Audio settings', 'The length of hangout music excerpts must be 20-90 seconds.',
                                       parent=win)
                return
            try:
                hlufs = float(v_hlufs.get().replace(',', '.'))
                if not -24 <= hlufs <= -8:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('Audio settings', 'The loudness of hangout music must be -24...-8 LUFS.', parent=win)
                return
            st.update({'hangout_normalize': bool(v_hnorm.get()), 'hangout_lufs': hlufs})
            st.update({'ffmpeg': vars_['ffmpeg'].get().strip(), 'q_mp3': qm, 'eor': bool(v_eor.get()),
                       'eor_follow_audio': bool(v_follow.get()), 'eor_seconds': secs,
                       'hangout_follow': bool(v_hfollow.get()), 'hangout_seconds': hsecs})
            self.ffmpeg = st['ffmpeg'] or self.ffmpeg
            save_settings(st)
            if self.p:
                self.p.settings.update(st)
            win.destroy()
            self._update_buttons()
        b = ttk.Frame(win, padding=(10, 0, 10, 10))
        b.pack(fill='x')
        ttk.Button(b, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(b, text='Save', command=ok).pack(side='right', padx=6)
        win.grab_set()

    def _on_save_here(self):
        """Write the draft into the folder that is open (asks first; the replaced archives are kept as .bak once)."""
        if not self.p or self.busy or not self.p.is_dirty() or not self._audio_ready():
            return
        folder = self.p.game_dir
        audio = self.p.needs_encoder()
        if not messagebox.askyesno('Save to the current folder', self._confirm_text() +
                                   '\n\nThe changed archives in\n%s\nare REPLACED by the new versions (carchive_cache.rpf ~2 GB'
                                   % folder + (', carchive_music.rpf ~0.7 GB, carchive_audio.rpf ~1.7 GB' if audio else '') +
                                   ').\nThe first time, each replaced archive is kept next to it as <name>.bak.\n\n'
                                   'If this is your ORIGINAL game folder, it will be modified (the .bak files are your way back).\n\n'
                                   'Continue?', icon='warning'):
            return
        self._busy(True, 'Saving...')

        def run():
            try:
                written = self.p.save_in_place(progress=lambda m: self.q.put(('status', m)))
                self.q.put(('saved', 'Saved to %s' % folder,
                            'Replaced in %s:\n\n%s\n\nThe previous versions are kept as <name>.bak (delete them when you do '
                            'not need them - they are as big as the archives, and do not copy them to the PS3).'
                            % (folder, '\n'.join('  ' + w for w in written))))
            except Exception as e:
                self.q.put(('error', 'Saving failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    def _on_build_folder(self):
        if not self.p or self.busy or not self._audio_ready():
            return
        dst = filedialog.askdirectory(title='Empty/new folder for the test game folder', mustexist=False,
                                      initialdir=os.path.dirname(self.p.game_dir))
        if not dst:
            return
        if os.path.abspath(dst).lower() == os.path.abspath(self.p.game_dir).lower():
            messagebox.showerror('Not allowed', 'The original game folder is never modified.')
            return
        if not messagebox.askyesno('Build test game folder', self._confirm_text() +
                                   '\n\nEvery other game file is hard-linked (no extra space when on the same drive; '
                                   'copied otherwise). Continue?',
                                   icon='warning' if self.p.empty_genres() else 'question'):
            return
        self._busy(True, 'Building...')

        def run():
            try:
                linked, copied = self.p.build_folder(dst, progress=lambda m: self.q.put(('status', m)))
                self.q.put(('done', 'Built %s' % dst,
                            'Game folder ready:\n%s\n\n%d file(s) hard-linked, %d copied.\n'
                            'Start it in RPCS3 with File > Boot Game and this folder.' % (dst, linked, copied)))
            except Exception as e:
                self.q.put(('error', 'Build failed: %s' % e))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ misc
    def _on_close(self):
        if self.busy and not messagebox.askyesno('Quit', 'A build / extraction is running and would be cut off. Quit anyway?'):
            return
        if getattr(self, '_iso_cancel', None):
            self._iso_cancel.set()                        # the unfinished file of an extraction is removed
        if self.p and self._autosave_ok and self.p.has_edits():
            self._autosave_now()
            if not messagebox.askyesno('Quit', 'The draft (unbuilt changes) is kept in %s next to the game folder and is '
                                       'offered again when you open this folder. Quit?'
                                       % os.path.basename(M._draft_path(self.p.game_dir))):
                return
        self._on_stop()
        self.destroy()

    def _run_script(self, path):        # test hook: py mcla_gui.py <folder> --script file.py
        exec(open(path, encoding='utf-8-sig').read(), {'app': self, 'M': M})


if __name__ == '__main__':
    args = sys.argv[1:]
    script = ''
    if '--script' in args:
        i = args.index('--script')
        script = args[i + 1]
        del args[i:i + 2]
    App(args[0] if args else '', script).mainloop()
