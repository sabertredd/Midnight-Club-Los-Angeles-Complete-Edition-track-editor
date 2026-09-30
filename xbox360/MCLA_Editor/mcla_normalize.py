#!/usr/bin/env python3
"""
mcla_normalize.py - loudness normalizer for the Midnight Club: Los Angeles music (companion of mcla_gui.py).

  py mcla_normalize.py

Two tabs:
  * Audio files - brings mp3 / wav / flac / m4a ... to the level of the game (EBU R128, default -8 LUFS, true peaks at most
    -1 dBTP) and writes WAV files that you then add with the editor. Nothing of the game is touched, no encoder needed.
  * Game folder - the songs whose audio you replaced / added with the editor (recorded in <folder>.mcla_changes.json next
    to the folder) are measured; the ones you tick are encoded again at the game level - from your source file when it
    still exists, else from the audio in the archive - together with their end-of-race clips, and written like the editor
    does it (a new test folder, or into the folder itself, the replaced archives kept once as .bak). The original songs of
    the game are not offered: they are already even and re-encoding them from XMA would only cost quality.
Settings (XMA encoder, ffmpeg, "start the encoder through") are the ones of the editor (mcla_gui_settings.json).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import keysetup
import loudness as LN
import mcla_gui as G
import mcla_model as M

CHECK_ON, CHECK_OFF = '☑', '☐'


def fnum(v, fmt='%.1f'):
    return '' if v is None else fmt % v


class NormApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('MCLA Loudness Normalizer')
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry('%dx%d' % (min(1100, sw - 40), min(640, sh - 90)))
        self.minsize(min(760, sw - 40), min(420, sh - 90))
        self.st = G.load_settings()
        self.st.setdefault('norm_target', LN.DEFAULT_TARGET)
        self.st.setdefault('norm_ceiling', LN.DEFAULT_CEILING)
        self.st.setdefault('norm_threshold', 1.5)
        self.st.setdefault('norm_format', 'FLAC')
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()             # set by Stop; every long job checks it between steps
        self.files = {}            # iid -> {'path', 'm', 'res', 'status'}
        self.p = None              # game folder project
        self.rows = {}             # song obj -> {'m', 'status'}
        self.checked = set()
        self._build()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ layout
    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        self._build_files(nb)
        self._build_game(nb)
        for v in (self.v_target, self.v_thr):
            v.trace_add('write', lambda *_: self._refresh_all())
        bottom = ttk.Frame(self, padding=(6, 0, 6, 6))
        bottom.pack(fill='x')
        ttk.Button(bottom, text='Settings...', command=self._on_settings).pack(side='left')
        self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=150)
        self.progress.pack(side='right')
        self.btn_stop = ttk.Button(bottom, text='■ Stop', command=self._on_stop, state='disabled')
        self.btn_stop.pack(side='right', padx=6)
        self.status = tk.StringVar(value='Target: the level of the game, about -8 LUFS (median of the 106 original songs: '
                                         '%.1f LUFS).' % LN.GAME_LUFS)
        ttk.Label(bottom, textvariable=self.status, foreground='#444').pack(side='left', padx=10)

    def _level_fields(self, parent, with_genre=False):
        f = ttk.Frame(parent)
        self.v_target = getattr(self, 'v_target', None) or tk.StringVar(value=str(self.st['norm_target']))
        self.v_ceiling = getattr(self, 'v_ceiling', None) or tk.StringVar(value=str(self.st['norm_ceiling']))
        ttk.Label(f, text='Target (LUFS):').pack(side='left')
        ttk.Entry(f, textvariable=self.v_target, width=6).pack(side='left', padx=(4, 12))
        ttk.Label(f, text='Peaks at most (dBTP):').pack(side='left')
        ttk.Entry(f, textvariable=self.v_ceiling, width=6).pack(side='left', padx=(4, 12))
        return f

    def _build_files(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Audio files (before adding them)')
        top = ttk.Frame(tab)
        top.pack(fill='x')
        ttk.Button(top, text='Add files...', command=self._f_add).pack(side='left')
        ttk.Button(top, text='Remove selected', command=self._f_remove).pack(side='left', padx=6)
        self._level_fields(top).pack(side='left', padx=(12, 0))
        out = ttk.Frame(tab)
        out.pack(fill='x', pady=(6, 0))
        ttk.Label(out, text='Save the new files to:').pack(side='left')
        self.v_out = tk.StringVar()
        ttk.Entry(out, textvariable=self.v_out, width=60).pack(side='left', padx=4, fill='x', expand=True)
        ttk.Button(out, text='Browse...', command=lambda: self.v_out.set(filedialog.askdirectory() or self.v_out.get())).pack(side='left')
        ttk.Label(out, text='Format:').pack(side='left', padx=(12, 4))
        self.v_fmt = tk.StringVar(value=self.st['norm_format'] if self.st['norm_format'] in ('FLAC', 'WAV') else 'FLAC')
        ttk.Combobox(out, textvariable=self.v_fmt, values=('FLAC', 'WAV'), state='readonly', width=6).pack(side='left')
        cols = ('file', 'lufs', 'peak', 'gain', 'after', 'status')
        heads = {'file': 'File', 'lufs': 'LUFS', 'peak': 'Peak dBTP', 'gain': 'Gain dB', 'after': 'Result', 'status': ''}
        tf = ttk.Frame(tab)
        tf.pack(fill='both', expand=True, pady=6)
        self.ftree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='extended')
        for c, w in zip(cols, (420, 70, 80, 70, 150, 200)):
            self.ftree.heading(c, text=heads[c])
            self.ftree.column(c, width=w, anchor='w' if c in ('file', 'status', 'after') else 'center', stretch=c == 'file')
        vsb = ttk.Scrollbar(tf, orient='vertical', command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=vsb.set)
        self.ftree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.ftree.bind('<Delete>', lambda e: self._f_remove())
        b = ttk.Frame(tab)
        b.pack(fill='x')
        ttk.Label(b, foreground='#666', wraplength=700, justify='left',
                  text='The files are only read. The new files keep the names and the tags (artist, title, album...; FLAC also '
                       'the cover picture), so the editor reads artist / title from them. FLAC: lossless, half the size of '
                       'WAV. Add them with "Add song..." / "Replace audio..." as usual.'
                  ).pack(side='left')
        self.btn_fnorm = ttk.Button(b, text='Normalize', command=self._f_normalize)
        self.btn_fnorm.pack(side='right')

    def _build_game(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Game folder (your songs)')
        top = ttk.Frame(tab)
        top.pack(fill='x')
        ttk.Button(top, text='Open game folder...', command=self._g_open).pack(side='left')
        self.v_folder = tk.StringVar(value='(no folder)')
        ttk.Label(top, textvariable=self.v_folder, foreground='#555').pack(side='left', padx=8)
        opts = ttk.Frame(tab)
        opts.pack(fill='x', pady=(6, 0))
        self._level_fields(opts).pack(side='left')
        self.v_genre = tk.BooleanVar(value=bool(self.st.get('norm_by_genre', False)))
        ttk.Checkbutton(opts, variable=self.v_genre, text='target = level of the genre in the original game',
                        command=self._g_refresh).pack(side='left')
        opts2 = ttk.Frame(tab)
        opts2.pack(fill='x', pady=(4, 0))
        ttk.Label(opts2, text='Tick the songs that differ by more than (dB):').pack(side='left')
        self.v_thr = tk.StringVar(value=str(self.st['norm_threshold']))
        ttk.Entry(opts2, textvariable=self.v_thr, width=5).pack(side='left', padx=4)
        ttk.Button(opts2, text='Tick those', command=self._g_tick_deviating).pack(side='left', padx=4)
        ttk.Button(opts2, text='All', command=lambda: self._g_tick(all_=True)).pack(side='left', padx=2)
        ttk.Button(opts2, text='None', command=lambda: self._g_tick(all_=False)).pack(side='left', padx=2)
        self.v_clips = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts2, variable=self.v_clips, text='also their end-of-race clips').pack(side='left', padx=(16, 0))
        cols = ('on', 'title', 'genre', 'lufs', 'peak', 'target', 'diff', 'source', 'eor', 'status')
        heads = {'on': '', 'title': 'Title', 'genre': 'Genre', 'lufs': 'LUFS', 'peak': 'Peak', 'target': 'Target',
                 'diff': 'Diff', 'source': 'Source', 'eor': 'EOR clip', 'status': ''}
        tf = ttk.Frame(tab)
        tf.pack(fill='both', expand=True, pady=6)
        self.gtree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='browse')
        for c, w in zip(cols, (28, 360, 110, 60, 60, 60, 60, 110, 90, 160)):
            self.gtree.heading(c, text=heads[c])
            self.gtree.column(c, width=w, anchor='w' if c in ('title', 'genre', 'source', 'status') else 'center',
                              stretch=c == 'title')
        self.gtree.tag_configure('dev', foreground='#b00000')
        vsb = ttk.Scrollbar(tf, orient='vertical', command=self.gtree.yview)
        self.gtree.configure(yscrollcommand=vsb.set)
        self.gtree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.gtree.bind('<Button-1>', self._g_click)
        self.gtree.bind('<space>', lambda e: self._g_toggle(self.gtree.focus()))
        b = ttk.Frame(tab)
        b.pack(fill='x')
        ttk.Label(b, foreground='#666', wraplength=560, justify='left',
                  text='Source "file": your original file (from .mcla_changes.json) - best quality. "archive": the file is '
                       'gone, the audio in the game is decoded and encoded again. Needs the XMA encoder (Settings...).'
                  ).pack(side='left')
        self.btn_gsave = ttk.Button(b, text='Save to this folder...', command=lambda: self._g_run(in_place=True))
        self.btn_gsave.pack(side='right')
        self.btn_gbuild = ttk.Button(b, text='Build test game folder...', command=lambda: self._g_run(in_place=False))
        self.btn_gbuild.pack(side='right', padx=6)

    # ------------------------------------------------------------------ common
    def _levels(self):
        try:
            t, c = float(self.v_target.get().replace(',', '.')), float(self.v_ceiling.get().replace(',', '.'))
            if not (-30 <= t <= -3 and -9 <= c <= 0):
                raise ValueError
        except ValueError:
            messagebox.showwarning('Levels', 'Target: -30 ... -3 LUFS, peaks: -9 ... 0 dBTP.')
            return None
        self.st.update({'norm_target': t, 'norm_ceiling': c, 'norm_by_genre': bool(self.v_genre.get())})
        G.save_settings(self.st)
        return t, c

    def _ffmpeg(self):
        ff = self.st.get('ffmpeg') or G.find_ffmpeg()
        if not ff or not os.path.isfile(ff):
            messagebox.showwarning('ffmpeg', 'ffmpeg not found - set its path in Settings...')
            return None
        return ff

    def _set_busy(self, on, msg=None):
        self.busy = on
        if on:
            self.cancel.clear()
        (self.progress.start if on else self.progress.stop)(*((12,) if on else ()))
        self.btn_stop.configure(state='normal' if on else 'disabled')
        if msg:
            self.status.set(msg)

    def _on_stop(self):
        if self.busy and not self.cancel.is_set():
            self.cancel.set()
            self.btn_stop.configure(state='disabled')
            self.status.set('Stopping after the current step...')

    def _measure_all(self, jobs, report, workers):
        """jobs {key: callable} run in a pool; report(key, result, status) per job; Stop drops the jobs not started yet.
        Returns False when stopped."""
        with cf.ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(fn): k for k, fn in jobs.items()}
            for n, fu in enumerate(cf.as_completed(futs), 1):
                if self.cancel.is_set():
                    for f in futs:
                        f.cancel()
                    for f, k in futs.items():
                        if f.cancelled():
                            report(k, None, 'stopped')
                    return False
                try:
                    report(futs[fu], fu.result(), '')
                except Exception as e:
                    report(futs[fu], None, 'error: %s' % e)
                self.q.put(('status', 'Measuring %d/%d...' % (n, len(futs))))
        return True

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                try:
                    getattr(self, '_on_' + kind)(*rest)
                except Exception as e:                      # never leave the window without a message
                    import traceback
                    traceback.print_exc()
                    self._set_busy(False, 'Internal error: %s' % e)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_status(self, msg):
        self.status.set(msg)

    def _on_error(self, msg):
        self._set_busy(False, msg)
        messagebox.showerror('Error', msg)

    def _on_done(self, msg, box=None):
        self._set_busy(False, msg)
        if box:
            messagebox.showinfo('Done', box)

    # ------------------------------------------------------------------ tab 1: files
    def _f_show(self, iid):
        r = self.files[iid]
        m, res = r['m'], r['res']
        t = self._levels_quiet()
        gain = (t[0] - m['lufs']) if (m and t) else None
        after = ('%.1f LUFS, peak %.1f' % (res['after']['lufs'], res['after']['peak'])) if res else ''
        self.ftree.item(iid, values=(os.path.basename(r['path']), fnum(m and m['lufs']), fnum(m and m['peak']),
                                     fnum(gain, '%+.1f'), after, r['status']))

    def _levels_quiet(self):
        try:
            return float(self.v_target.get().replace(',', '.')), float(self.v_ceiling.get().replace(',', '.'))
        except ValueError:
            return None

    def _f_add(self):
        paths = filedialog.askopenfilenames(title='Audio files to bring to the level of the game', filetypes=G.App.AUDIO_TYPES)
        known = {r['path'] for r in self.files.values()}
        paths = [p for p in paths if p not in known]
        if not paths:
            return
        ff = self._ffmpeg()
        if not ff:
            return
        if not self.v_out.get():
            self.v_out.set(os.path.join(os.path.dirname(paths[0]), 'normalized'))
        new = []
        for p in paths:
            iid = 'f%d' % (len(self.files) + 1 + len(new))
            self.files[iid] = {'path': p, 'm': None, 'res': None, 'status': 'measuring...'}
            self.ftree.insert('', 'end', iid=iid)
            self._f_show(iid)
            new.append(iid)
        self._set_busy(True, 'Measuring %d file(s)...' % len(new))

        def run():
            jobs = {i: (lambda p=self.files[i]['path']: LN.measure_file(ff, p)) for i in new}
            done = self._measure_all(jobs, lambda k, m, st: self.q.put(('fmeasured', k, m, st)), 4)
            self.q.put(('done', 'Measured. Set the target and press Normalize.' if done else
                        'Stopped - the files not measured cannot be normalized (remove and add them again).'))
        threading.Thread(target=run, daemon=True).start()

    def _on_fmeasured(self, iid, m, status):
        if iid in self.files:
            self.files[iid].update(m=m, status=status)
            self._f_show(iid)

    def _f_remove(self):
        if self.busy:
            return
        for iid in self.ftree.selection():
            self.files.pop(iid, None)
            self.ftree.delete(iid)

    def _f_normalize(self):
        if self.busy:
            return
        lv, ff = self._levels(), self._ffmpeg()
        todo = [i for i, r in self.files.items() if r['m']]
        if not lv or not ff or not todo:
            return
        out = self.v_out.get().strip()
        if not out:
            messagebox.showwarning('Normalize', 'Choose the folder for the new files.')
            return
        ext = '.flac' if self.v_fmt.get() == 'FLAC' else '.wav'
        self.st['norm_format'] = self.v_fmt.get()
        G.save_settings(self.st)
        os.makedirs(out, exist_ok=True)
        self._set_busy(True, 'Normalizing %d file(s)...' % len(todo))

        def run():
            ok = 0
            for n, iid in enumerate(todo, 1):
                r = self.files[iid]
                if self.cancel.is_set():
                    self.q.put(('fnormalized', iid, None, 'stopped'))
                    continue
                stem = os.path.splitext(os.path.basename(r['path']))[0]
                dst = os.path.join(out, stem + ext)
                if os.path.abspath(dst).lower() == os.path.abspath(r['path']).lower():
                    dst = os.path.join(out, stem + ' (normalized)' + ext)
                self.q.put(('status', 'Normalizing %d/%d: %s' % (n, len(todo), os.path.basename(r['path']))))
                try:
                    res = LN.normalize(ff, r['path'], dst, lv[0], lv[1], measured=r['m'], cancel=self.cancel)
                    self.q.put(('fnormalized', iid, res, 'saved' + (' (limited)' if res['limited'] else '')))
                    ok += 1
                except LN.Cancelled:
                    if os.path.isfile(dst):                  # a half-finished file is not left behind
                        os.remove(dst)
                    self.q.put(('fnormalized', iid, None, 'stopped'))
                except Exception as e:
                    self.q.put(('fnormalized', iid, None, 'error: %s' % e))
            self.q.put(('done', ('Stopped. ' if self.cancel.is_set() else '') +
                        '%d of %d file(s) saved to %s' % (ok, len(todo), out)))
        threading.Thread(target=run, daemon=True).start()

    def _on_fnormalized(self, iid, res, status):
        if iid in self.files:
            self.files[iid].update(res=res, status=status)
            self._f_show(iid)

    # ------------------------------------------------------------------ tab 2: game folder
    def _target_of(self, s, lv):
        return LN.GENRE_LUFS.get(s.genre, lv[0]) if self.v_genre.get() else lv[0]

    def _g_open(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title='Game folder (contains xarchive_*.rpf) built / saved with the editor')
        if d:
            self._g_load(d)

    def _g_load(self, folder):
        ff = self._ffmpeg()
        if not ff or not keysetup.ensure_key(self, folder):
            return
        self._set_busy(True, 'Reading %s...' % folder)
        self.gtree.delete(*self.gtree.get_children())
        self.rows, self.checked, self.p = {}, set(), None

        def run():
            try:
                p = M.Project().load(folder, progress=lambda m: self.q.put(('status', m)))
            except Exception as e:
                self.q.put(('error', 'Loading failed: %s' % e))
                return
            songs = [s for s in p.songs.values() if s.audio_loaded and not s.removed and not s.audio_missing]
            self.q.put(('gloaded', p, [s.obj for s in songs]))
            if not songs:
                return
            lock = threading.Lock()

            def one(s):
                with lock:                                 # one file handle: read one song at a time
                    data = p.song_bytes(s)
                return LN.measure_track(ff, data)
            done = self._measure_all({s.obj: (lambda s=s: one(s)) for s in songs},
                                     lambda k, m, st: self.q.put(('gmeasured', k, m, st)), 6)
            self.q.put(('gall', done))
        threading.Thread(target=run, daemon=True).start()

    def _on_gloaded(self, p, objs):
        self.p = p
        self.v_folder.set(p.game_dir)
        if not objs:
            self._set_busy(False, 'No songs of yours are recorded for this folder.')
            messagebox.showinfo('Game folder', 'This folder has no songs whose audio you replaced or added with the editor '
                                '(the list is in "%s", which is written when you build / save with the editor).'
                                % os.path.basename(M._changes_path(p.game_dir)))
            return
        for obj in sorted(objs, key=lambda o: (p.songs[o].genre, p.songs[o].title)):
            self.rows[obj] = {'m': None, 'status': 'measuring...'}
            self.gtree.insert('', 'end', iid=obj)
            self._g_show(obj)

    def _on_gmeasured(self, obj, m, status):
        if obj in self.rows:
            self.rows[obj].update(m=m, status=status)
            self._g_show(obj)

    def _on_gall(self, done=True):
        self._g_tick_deviating(quiet=True)
        n = len(self.checked)
        self._set_busy(False, ('' if done else 'Stopped - songs not measured are not ticked. ') +
                       '%d song(s) of yours, %d differ from the target by more than %s dB (ticked).'
                       % (len(self.rows), n, self.v_thr.get()))

    def _g_show(self, obj):
        s, r = self.p.songs[obj], self.rows[obj]
        m, lv = r['m'], self._levels_quiet()
        tgt = self._target_of(s, lv) if lv else None
        diff = (m['lufs'] - tgt) if (m and tgt is not None) else None
        src = 'file' if s.audio_loaded and os.path.isfile(s.audio_loaded) else 'archive'
        eor = s.eor_loaded[0] if s.eor_loaded else ''
        try:
            thr = float(self.v_thr.get().replace(',', '.'))
        except ValueError:
            thr = 1.5
        self.gtree.item(obj, values=(CHECK_ON if obj in self.checked else CHECK_OFF, s.title, self.p.genre_ui(s.genre),
                                     fnum(m and m['lufs']), fnum(m and m['peak']), fnum(tgt), fnum(diff, '%+.1f'), src, eor,
                                     r['status']), tags=('dev',) if diff is not None and abs(diff) > thr else ())

    def _g_refresh(self):
        for obj in self.rows:
            self._g_show(obj)

    def _refresh_all(self):
        for iid in self.files:
            self._f_show(iid)
        if self.p:
            self._g_refresh()

    def _g_click(self, ev):
        if self.gtree.identify_column(ev.x) == '#1' and self.gtree.identify_region(ev.x, ev.y) == 'cell':
            self._g_toggle(self.gtree.identify_row(ev.y))

    def _g_toggle(self, obj):
        if obj in self.rows and not self.busy:
            self.checked ^= {obj}
            self._g_show(obj)

    def _g_tick(self, all_):
        if not self.busy:
            self.checked = set(self.rows) if all_ else set()
            self._g_refresh()

    def _g_tick_deviating(self, quiet=False):
        lv = self._levels_quiet()
        try:
            thr = float(self.v_thr.get().replace(',', '.'))
        except ValueError:
            thr = None
        if not lv or thr is None:
            if not quiet:
                messagebox.showwarning('Levels', 'Target, peaks and the difference must be numbers.')
            return
        self.checked = {o for o, r in self.rows.items() if r['m'] and abs(r['m']['lufs'] - self._target_of(self.p.songs[o], lv)) > thr}
        self._g_refresh()

    def _g_run(self, in_place):
        if self.busy or not self.p or not self.checked:
            if self.p and not self.checked and not self.busy:
                messagebox.showinfo('Normalize', 'Tick the songs to normalize first.')
            return
        lv, ff = self._levels(), self._ffmpeg()
        if not lv or not ff:
            return
        p = self.p
        p.settings.update(self.st)
        try:
            p._check_encoder()
        except M.EditError as e:
            messagebox.showwarning('XMA encoder', str(e))
            return
        songs = [p.songs[o] for o in self.rows if o in self.checked]
        targets = {s.obj: self._target_of(s, lv) for s in songs}      # Tk variables are read here, not in the worker
        clips = bool(self.v_clips.get())
        what = '%d song(s)%s at %s' % (len(songs), ' and their end-of-race clips' if clips else '',
                                       'the level of their genre' if self.v_genre.get() else '%.1f LUFS' % lv[0])
        if in_place:
            out = p.game_dir
            if not messagebox.askyesno('Save to this folder', 'Encode %s again and REPLACE the changed archives in\n%s\n\n'
                                       'The first time, each replaced archive is kept next to it as <name>.bak.\n'
                                       'Encoding takes a while (about half a minute per song).\n\nContinue?' % (what, out),
                                       icon='warning'):
                return
        else:
            out = filedialog.askdirectory(title='New folder for the test game (not the opened one)')
            if not out:
                return
            if os.path.abspath(out).lower() == os.path.abspath(p.game_dir).lower():
                messagebox.showwarning('Build', 'Choose another folder (use "Save to this folder..." for the opened one).')
                return
        self._set_busy(True, 'Preparing %s...' % what)
        threading.Thread(target=self._g_worker, args=(p, songs, targets, lv, ff, clips, in_place, out), daemon=True).start()

    def _g_worker(self, p, songs, targets, lv, ff, clips, in_place, out):
        import eor_clip
        tmp = tempfile.mkdtemp(prefix='mcla_norm_')
        notes = {}
        new_folder = not in_place and not (os.path.isdir(out) and os.listdir(out))   # made by us: removed again on Stop
        committing = [False]

        def prog(m):
            # Stop is honoured while encoding / building (the archives are written to a temporary place or a new folder);
            # once "Save to this folder" starts replacing the archives it runs to the end, so the folder stays consistent
            if m.startswith(('Replacing', 'Copying')):
                committing[0] = True
            if self.cancel.is_set() and not committing[0]:
                raise LN.Cancelled()
            self.q.put(('status', m))
        try:
            p.settings['eor_follow_audio'] = False       # clips are made here, so an untouched original clip stays as it is
            p.settings['hangout_follow'] = False         # louder / quieter songs do not change the hangout music clips
            for n, s in enumerate(songs, 1):
                if self.cancel.is_set():
                    raise LN.Cancelled()
                tgt = targets[s.obj]
                self.q.put(('status', 'Normalizing %d/%d: %s' % (n, len(songs), s.title)))
                src = s.audio_loaded if s.audio_loaded and os.path.isfile(s.audio_loaded) else None
                if not src:
                    src = LN.decode_track(ff, p.song_bytes(s), os.path.join(tmp, s.obj + '_src.wav'), name=s.wave)
                dst = os.path.join(tmp, s.obj + '.wav')
                res = LN.normalize(ff, src, dst, tgt, lv[1], cancel=self.cancel)
                p.set_audio(s.obj, dst)
                note = {'target_lufs': round(tgt, 1), 'ceiling_dbtp': lv[1], 'gain_db': round(res['gain'], 1)}
                if clips and s.eor_loaded and p.eor_name_of(s):
                    kind, csrc = s.eor_loaded
                    cdst = os.path.join(tmp, s.obj + '_eor.wav')
                    if kind == 'custom':
                        if not (csrc and os.path.isfile(csrc)):
                            data, name = p.eor_original_bytes(s)
                            csrc = LN.decode_track(ff, data, os.path.join(tmp, s.obj + '_eorsrc.wav'), name=name)
                        cres = LN.normalize(ff, csrc, cdst, tgt, lv[1], cancel=self.cancel)
                        note['eor_gain_db'] = round(cres['gain'], 1)
                    else:                                  # automatic excerpt: made again from the normalized audio
                        start, length = eor_clip.auto_excerpt(ff, dst, float(p.settings.get('eor_seconds') or eor_clip.DEFAULT_SECONDS))
                        eor_clip.make_wav(ff, dst, cdst, start, length)
                    p.set_eor_audio(s.obj, cdst)
                notes[s.obj] = note
            self.q.put(('status', 'Encoding and writing the archives (this takes a while)...'))
            if in_place:
                p.save_in_place(progress=prog)
            else:
                p.build_folder(out, progress=prog)
            self._fix_changes(out, p, songs, notes)
            self.q.put(('gsaved', out, in_place, len(songs)))
            return
        except LN.Cancelled:
            msg = 'Stopped. Nothing was written: %s is as before.' % (out if in_place else p.game_dir)
            if new_folder and os.path.isdir(out):
                shutil.rmtree(out, ignore_errors=True)
                msg += ' The unfinished new folder was removed.' if not os.path.exists(out) else \
                       ' The unfinished new folder %s could not be removed completely - delete it.' % out
            elif not in_place:
                msg += ' The chosen folder %s may hold unfinished files - delete them.' % out
            self.q.put(('done', msg, msg))
        except Exception as e:
            self.q.put(('error', 'Failed: %s' % e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for s in songs:                       # not written: the draft must not keep the deleted temporary files
            s.new_audio = s.eor_audio = None

    @staticmethod
    def _fix_changes(folder, p, songs, notes):
        """The editor recorded the temporary normalized files as sources; put your real source files back and note the
        normalization, so that the next run starts again from your originals."""
        path = M._changes_path(folder)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for s in songs:
            e = data['songs'].get(p.game_obj(s))
            if e is None:
                continue
            if s.audio_loaded:
                e['audio_source'] = s.audio_loaded
            if s.eor_loaded:
                kind, src = s.eor_loaded
                e['eor'] = {'kind': kind, 'source': src}
            if s.obj in notes:
                e['normalized'] = notes[s.obj]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

    def _on_gsaved(self, out, in_place, n):
        self._set_busy(False, 'Done: %d song(s) written to %s' % (n, out))
        messagebox.showinfo('Done', '%d song(s) normalized and written to\n%s' % (n, out) +
                            ('\n\nThe folder is read again to show the new levels.' if in_place else ''))
        self._g_load(out)

    # ------------------------------------------------------------------ settings
    def _on_settings(self):
        win = tk.Toplevel(self)
        win.title('Settings')
        win.transient(self)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=10)
        f.pack()
        keys = (('XMA encoder (.exe):', 'encoder', True), ('ffmpeg:', 'ffmpeg', True),
                ('Start the encoder through (macOS/Linux: wine):', 'encoder_runner', False),
                ('Quality for music.rpf (1-100):', 'q_music', False), ('Quality for audlo.rpf (1-100):', 'q_lo', False))
        vs = {k: tk.StringVar(value=str(self.st.get(k, ''))) for _, k, _ in keys}
        for i, (label, k, browse) in enumerate(keys):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky='w', pady=3)
            ttk.Entry(f, textvariable=vs[k], width=55 if browse else 20).grid(row=i, column=1, sticky='w', padx=6)
            if browse:
                ttk.Button(f, text='Browse...', command=lambda k=k: vs[k].set(
                    filedialog.askopenfilename(parent=win, filetypes=G.EXE_TYPES) or vs[k].get())).grid(row=i, column=2)
        ttk.Label(f, foreground='#666', text='The same settings file as the editor (%s).' % G.SETTINGS_PATH,
                  wraplength=600).grid(row=len(keys), column=0, columnspan=3, sticky='w', pady=(6, 0))

        def ok():
            try:
                qm, ql = int(vs['q_music'].get()), int(vs['q_lo'].get())
                if not (1 <= qm <= 100 and 1 <= ql <= 100):
                    raise ValueError
            except ValueError:
                messagebox.showwarning('Settings', 'Quality must be 1-100.', parent=win)
                return
            self.st.update({'encoder': vs['encoder'].get().strip(), 'ffmpeg': vs['ffmpeg'].get().strip(),
                            'encoder_runner': vs['encoder_runner'].get().strip(), 'q_music': qm, 'q_lo': ql})
            G.save_settings(self.st)
            win.destroy()
        b = ttk.Frame(win, padding=(10, 0, 10, 10))
        b.pack(fill='x')
        ttk.Button(b, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(b, text='Save', command=ok).pack(side='right', padx=6)
        win.grab_set()


if __name__ == '__main__':
    NormApp().mainloop()
