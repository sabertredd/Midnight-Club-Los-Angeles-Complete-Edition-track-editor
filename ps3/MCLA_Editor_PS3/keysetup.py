"""Asks for the archive key when it is not set up yet (see rpf3.KEY_FILE): the user types it in, or the program finds it
in the game's own decrypted executable. The key itself is never part of this program."""
import glob
import os
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import rpf3

HELP = ('The game archives (.rpf) are encrypted. The key belongs to the game and is not part of this program - it is '
        'set up once and then kept in %s next to the program.\n\n'
        'Either enter the key (64 hex digits) or let the program find it in the game\'s own executable. The executable '
        'must be decrypted and not compressed:\n'
        '  Xbox 360: default.xex after e.g. "xextool -e u -c u default.xex"\n'
        '  PlayStation 3: EBOOT.BIN decrypted with RPCS3 (Utilities > Decrypt PS3 Binaries -> EBOOT.elf)' % rpf3.KEY_FILE)


def an_archive(folder):
    """an encrypted .rpf archive of a game folder (Xbox 360 or PS3 layout), or None"""
    cands = []
    for pat in ('xarchive_*.rpf', os.path.join('PS3_GAME', 'USRDIR', 'carchive_*.rpf'), 'carchive_*.rpf',
                os.path.join('USRDIR', 'carchive_*.rpf'), '*.rpf'):
        cands += sorted(glob.glob(os.path.join(folder or '', pat)))
    for c in cands:
        try:
            if rpf3._toc_block(c) is not None:
                return c
        except (OSError, ValueError, struct.error):
            pass
    return None


def xex_problem(data):
    """why an .xex file cannot be searched (still encrypted / compressed), or None"""
    if data[:4] != b'XEX2':
        return None
    try:
        cnt = struct.unpack('>I', data[20:24])[0]
        for i in range(cnt):
            k, v = struct.unpack('>II', data[24 + i * 8:32 + i * 8])
            if k == 0x3FF:
                enc, comp = struct.unpack('>HH', data[v + 4:v + 8])
                if enc:
                    return 'this default.xex is still encrypted'
                if comp > 1:
                    return 'this default.xex is still compressed'
    except struct.error:
        return 'this .xex file could not be read'
    return None


def ensure_key(parent, folder):
    """True when the archive key is available - asks for it (modal) when it is not"""
    if rpf3.load_key():
        return True
    archive = an_archive(folder)
    if not archive:
        return True                     # nothing encrypted to open here: let the caller report what is missing
    dlg = _KeyDialog(parent, archive)
    parent.wait_window(dlg)
    return dlg.ok


class _KeyDialog(tk.Toplevel):
    def __init__(self, parent, archive):
        super().__init__(parent)
        self.archive, self.ok, self.stop = archive, False, threading.Event()
        self.title('Archive key needed')
        self.transient(parent)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text=HELP, wraplength=520, justify='left').pack(anchor='w')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(10, 0))
        ttk.Label(row, text='Key:').pack(side='left')
        self.v_key = tk.StringVar()
        ttk.Entry(row, textvariable=self.v_key, width=70).pack(side='left', padx=6, fill='x', expand=True)
        self.b_use = ttk.Button(row, text='Use this key', command=self._use)
        self.b_use.pack(side='left')
        self.status = tk.StringVar()
        ttk.Label(f, textvariable=self.status, foreground='#a05000', wraplength=520).pack(anchor='w', pady=(8, 0))
        self.bar = ttk.Progressbar(f, mode='determinate', maximum=1000)
        self.bar.pack(fill='x', pady=(4, 0))
        bot = ttk.Frame(f)
        bot.pack(fill='x', pady=(10, 0))
        self.b_find = ttk.Button(bot, text='Find it in the game executable...', command=self._find)
        self.b_find.pack(side='left')
        ttk.Button(bot, text='Cancel', command=self._cancel).pack(side='right')
        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.grab_set()

    def _done(self, key):
        try:
            rpf3.set_key(key)
        except OSError as e:
            rpf3.set_key(key, save=False)
            messagebox.showwarning('Archive key', 'The key works but could not be saved (%s) - it has to be set up '
                                   'again next time.' % e, parent=self)
        self.ok = True
        self.destroy()

    def _use(self):
        k = rpf3._parse_key(self.v_key.get())
        if not k:
            self.status.set('The key has to be 64 hex digits (0-9, a-f).')
        elif not rpf3.check_key(k, self.archive):
            self.status.set('This key does not open %s.' % os.path.basename(self.archive))
        else:
            self._done(k)

    def _find(self):
        path = filedialog.askopenfilename(parent=self, title='Decrypted game executable',
                                          filetypes=[('Game executable', '*.xex *.elf *.pe *.exe *.bin'),
                                                     ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as e:
            self.status.set(str(e))
            return
        why = xex_problem(data)
        if why:
            self.status.set('%s - decrypt and decompress it first (see above).' % why)
            return
        self.b_find.state(['disabled'])
        self.b_use.state(['disabled'])
        self.status.set('Searching %s...' % os.path.basename(path))
        res = {}

        def run():
            try:
                res['key'] = rpf3.find_key(data, self.archive, lambda d, t: res.__setitem__('pos', d / max(1, t)),
                                           self.stop.is_set)
            except Exception as e:
                res['error'] = str(e)
            res['end'] = True
        threading.Thread(target=run, daemon=True).start()
        self._poll(res)

    def _poll(self, res):
        if not self.winfo_exists():
            return
        self.bar['value'] = int(res.get('pos', 0) * 1000)
        if not res.get('end'):
            self.after(150, lambda: self._poll(res))
            return
        self.b_find.state(['!disabled'])
        self.b_use.state(['!disabled'])
        if res.get('key'):
            self._done(res['key'])
        elif not self.stop.is_set():
            self.bar['value'] = 0
            self.status.set(res.get('error') or 'No key found in this file - is it the decrypted executable of this '
                                                 'game (see above)?')

    def _cancel(self):
        self.stop.set()
        self.destroy()
