#!/usr/bin/env python3
"""
mcla_cleanup.py - removes the backup files the MCLA Tracklist Editor (PlayStation 3 edition) leaves in a game folder.

  py mcla_cleanup.py [game folder]

"Save to the current folder" keeps each replaced archive once as PS3_GAME\\USRDIR\\carchive_<name>.rpf.bak (0.7-2 GB each),
the leftovers of an interrupted save are <game folder>\\_mcla_save_tmp. This lists them
(size, date) with the leftovers of an interrupted save (_mcla_save_tmp), and deletes the ticked ones after a confirmation.
Safety: a .bak can only be deleted while the archive it belongs to is there and opens as an RPF3 archive (otherwise the
.bak may be the only copy). A file that is a hard link shared with another folder frees no space - that is shown.
Deleting is permanent (files of this size do not fit into the recycle bin).
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import keysetup
import mcla_gui as G
import mcla_platform as PF
import rpf3

CHECK_ON, CHECK_OFF = '☑', '☐'


def _size(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs)
    return os.path.getsize(path)


def _remove(path):
    """delete a file even when it is read-only (a hard link to a protected file of another folder)"""
    import stat
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def scan(folder):
    """[{'path', 'name', 'size', 'date', 'deletable', 'note', 'shared'}] of the backups / leftovers in a game folder
    (the disc folder with PS3_GAME; the .bak files are next to the archives in PS3_GAME\\USRDIR)"""
    root, _ = PF.detect(folder)
    if not root:
        raise OSError('%s is not a PS3 Midnight Club: Los Angeles folder (no PS3_GAME\\USRDIR\\carchive_cache.rpf)' % folder)
    usr = os.path.join(root, PF.PS3.USRDIR)
    out = []
    for d, name in sorted([(root, n) for n in os.listdir(root)] + [(usr, n) for n in os.listdir(usr)]):
        path = os.path.join(d, name)
        if d == usr and name.lower().endswith('.rpf.bak') and os.path.isfile(path):
            arch = path[:-4]
            note, ok = '', True
            if not os.path.isfile(arch):
                ok, note = False, 'KEEP: %s is missing - this backup is the only copy' % os.path.basename(arch)
            else:
                try:
                    r = rpf3.RPF3(arch)
                    r.f.close()
                    note = 'backup of %s (%s)' % (os.path.basename(arch), time.strftime('%d.%m.%Y %H:%M',
                                                                                      time.localtime(os.path.getmtime(arch))))
                except Exception as e:
                    ok, note = False, 'KEEP: %s does not open (%s) - this backup may be the only good copy' % (
                        os.path.basename(arch), e)
            st = os.stat(path)
            out.append({'path': path, 'name': name, 'size': st.st_size, 'date': st.st_mtime, 'deletable': ok,
                        'note': note, 'shared': st.st_nlink > 1})
        elif d == root and name == '_mcla_save_tmp' and os.path.isdir(path):
            out.append({'path': path, 'name': name + os.sep, 'size': _size(path), 'date': os.path.getmtime(path),
                        'deletable': True, 'note': 'leftover of an interrupted "Save to the current folder"', 'shared': False})
    return out


class CleanApp(tk.Tk):
    def __init__(self, folder=''):
        super().__init__()
        self.title('MCLA Backup Cleaner - PlayStation 3')
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry('%dx%d' % (min(980, sw - 40), min(460, sh - 90)))
        self.minsize(min(700, sw - 40), min(320, sh - 90))
        self.st = G.load_settings()
        self.items = {}
        self.checked = set()
        self.folder = tk.StringVar()
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        ttk.Button(top, text='Open game folder...', command=self._choose).pack(side='left')
        ttk.Label(top, textvariable=self.folder, foreground='#555').pack(side='left', padx=8)
        ttk.Button(top, text='Refresh', command=self._scan).pack(side='right')
        cols = ('on', 'name', 'size', 'date', 'note')
        heads = {'on': '', 'name': 'File', 'size': 'Size', 'date': 'Date', 'note': ''}
        tf = ttk.Frame(self, padding=(8, 0))
        tf.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='browse')
        for c, w in zip(cols, (28, 230, 90, 130, 460)):
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=w, anchor='center' if c in ('on', 'size', 'date') else 'w', stretch=c == 'note')
        self.tree.tag_configure('keep', foreground='#b00000')
        vsb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<Button-1>', self._click)
        self.tree.bind('<space>', lambda e: self._toggle(self.tree.focus()))
        b = ttk.Frame(self, padding=8)
        b.pack(fill='x')
        self.status = tk.StringVar()
        ttk.Label(b, textvariable=self.status, foreground='#444').pack(side='left')
        self.btn_del = ttk.Button(b, text='Delete ticked...', command=self._delete, state='disabled')
        self.btn_del.pack(side='right')
        ttk.Button(b, text='All', command=lambda: self._tick(True)).pack(side='right', padx=4)
        ttk.Button(b, text='None', command=lambda: self._tick(False)).pack(side='right')
        ttk.Label(self, foreground='#666', wraplength=940, justify='left', padding=(8, 0, 8, 8),
                  text='A .bak file is the previous version of an archive, kept by "Save to the current folder" - your way '
                       'back if the saved folder has a problem. Delete them once the saved folder works (in RPCS3 / on the '
                       'PS3). Never copy .bak files to the PS3. Red rows are kept on purpose.').pack(fill='x')
        start = folder or self.st.get('last_dir', '')
        if start and os.path.isdir(start):
            self.folder.set(start)
            self.after(50, self._scan)
        else:
            self.status.set('Open the PS3 game folder (the one with PS3_GAME).')

    def _choose(self):
        d = filedialog.askdirectory(title='PS3 game folder (with PS3_GAME)', initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)
            self._scan()

    def _scan(self):
        folder = self.folder.get()
        if not folder or not os.path.isdir(folder):
            return
        if not keysetup.ensure_key(self, folder):
            return
        self.tree.delete(*self.tree.get_children())
        self.items, self.checked = {}, set()
        self.status.set('Checking the archives...')
        self.config(cursor='watch')
        self.update()
        try:
            found = scan(folder)
        except OSError as e:
            messagebox.showerror('Backup Cleaner', str(e))
            found = []
        finally:
            self.config(cursor='')
        for i, it in enumerate(found):
            iid = 'i%d' % i
            self.items[iid] = it
            if it['deletable']:
                self.checked.add(iid)
            self.tree.insert('', 'end', iid=iid)
            self._show(iid)
        self._update()

    def _show(self, iid):
        it = self.items[iid]
        note = it['note'] + ('  (hard link shared with another folder: frees no space)' if it['shared'] else '')
        self.tree.item(iid, values=(CHECK_ON if iid in self.checked else CHECK_OFF, it['name'],
                                    '%.2f GB' % (it['size'] / 2 ** 30) if it['size'] >= 2 ** 27 else '%.0f MB' % (it['size'] / 2 ** 20),
                                    time.strftime('%d.%m.%Y %H:%M', time.localtime(it['date'])), note),
                       tags=() if it['deletable'] else ('keep',))

    def _update(self):
        sel = [self.items[i] for i in self.checked]
        free = sum(it['size'] for it in sel if not it['shared'])
        if not self.items:
            self.status.set('No backups in this folder.')
        else:
            self.status.set('%d file(s) found, %d ticked - frees %.2f GB.' % (len(self.items), len(sel), free / 2 ** 30))
        self.btn_del.configure(state='normal' if sel else 'disabled')

    def _click(self, ev):
        if self.tree.identify_column(ev.x) == '#1' and self.tree.identify_region(ev.x, ev.y) == 'cell':
            self._toggle(self.tree.identify_row(ev.y))

    def _toggle(self, iid):
        if iid in self.items and self.items[iid]['deletable']:
            self.checked ^= {iid}
            self._show(iid)
            self._update()

    def _tick(self, on):
        self.checked = {i for i, it in self.items.items() if it['deletable']} if on else set()
        for i in self.items:
            self._show(i)
        self._update()

    def _delete(self):
        sel = [(i, self.items[i]) for i in self.checked if self.items[i]['deletable']]
        if not sel:
            return
        free = sum(it['size'] for _, it in sel if not it['shared'])
        if not messagebox.askyesno('Delete backups', 'Delete %d file(s) permanently (%.2f GB)?\n\n%s\n\nThe previous versions '
                                   'of these archives are then gone - only do this when the saved folder works.'
                                   % (len(sel), free / 2 ** 30, '\n'.join('  ' + it['name'] for _, it in sel)),
                                   icon='warning'):
            return
        failed = []
        for iid, it in sel:
            # checked again right before deleting: the archive must still be there and open
            if it['name'].lower().endswith('.rpf.bak'):
                try:
                    rpf3.RPF3(it['path'][:-4]).f.close()
                except Exception as e:
                    failed.append('%s: kept, its archive does not open (%s)' % (it['name'], e))
                    continue
            try:
                if os.path.isdir(it['path']):
                    for dp, dns, fns in os.walk(it['path'], topdown=False):      # (read-only files too)
                        for f in fns:
                            _remove(os.path.join(dp, f))
                        for dn in dns:
                            os.rmdir(os.path.join(dp, dn))
                    os.rmdir(it['path'])
                else:
                    _remove(it['path'])
            except OSError as e:
                failed.append('%s: %s' % (it['name'], e))
        self._scan()
        if failed:
            messagebox.showwarning('Delete backups', 'Not deleted:\n\n' + '\n'.join(failed))
        else:
            messagebox.showinfo('Delete backups', 'Deleted %d file(s), %.2f GB freed.' % (len(sel), free / 2 ** 30))


if __name__ == '__main__':
    CleanApp(sys.argv[1] if len(sys.argv) > 1 else '').mainloop()
