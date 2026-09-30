"""Write an RPF3 archive anew, compactly: replaced files, added files and all the other files of the source archive are laid
out one after the other (no unused space is carried over, e.g. the old slots of replaced files).

  rebuild(src, dst, replace={file_hash or (dir_hash, file_hash): path}, add=[(dir_hash, file_hash, path)], grow=False) -> size of dst

Every file keeps the alignment class of its original offset (0x8000 / 0x2000 / 0x800 / ... - the game archives use these),
new files get 0x8000 like the music tracks of the game; the files keep their order, added ones come last. Resource entries
keep the type byte in the low byte of their offset field. Offsets are 31-bit in RPF3 (bit 31 marks a directory), so an
archive cannot hold data beyond 2 GB: RpfTooBig is raised BEFORE anything is written.
"""
import os
import struct

import rpf3

SECTOR = 0x800
NEW_ALIGN = 0x8000
LIMIT = 0x80000000                       # offsets must stay below 2 GB
CHUNK = 1 << 22


class RpfRebuildError(Exception):
    pass


class RpfTooBig(RpfRebuildError):
    def __init__(self, size):
        super().__init__('the archive would be %.2f GB, but the RPF3 format holds at most 2.00 GB' % (size / 2 ** 30))
        self.size = size


def _align_of(off):
    for a in (0x8000, 0x2000, 0x800, 0x100, 0x10):
        if off % a == 0:
            return a
    return 1


def _read_toc(path):
    with open(path, 'rb') as f:
        head = f.read(SECTOR)
        _, toc_size, count, _, enc = struct.unpack('<4sIIII', head[:20])
        f.seek(SECTOR)
        raw = f.read(count * 16)
    toc = rpf3.decrypt_toc(raw + bytes(-len(raw) % 16)) if enc else raw
    return head, toc_size, enc, [list(struct.unpack_from('<IIII', toc, i * 16)) for i in range(count)]


def _dirs(ents):
    """indices of the directory entries: bit 31 of the offset field (valid for a source archive, whose data is below 2 GB;
    the second field of a directory is not always 0)"""
    return {i for i, e in enumerate(ents) if e[2] & 0x80000000}


def _size(x):
    return os.path.getsize(x) if isinstance(x, str) else int(x)


def planned_size(src, replace=None, add=None, grow=False):
    """size the archive would have after rebuild(); replace / add may give sizes (int) instead of file paths"""
    return _plan(src, replace, add, grow)['end']


def rebuild(src, dst, replace=None, add=None, grow=False, progress=lambda m: None):
    if os.path.abspath(src).lower() == os.path.abspath(dst).lower():
        raise RpfRebuildError('source and destination are the same file')
    pl = _plan(src, replace, add, grow)
    if pl['end'] > LIMIT:
        raise RpfTooBig(pl['end'])
    ents, pay, order, place = pl['ents'], pl['pay'], pl['order'], pl['place']
    toc = b''.join(struct.pack('<IIII', *e) for e in ents).ljust(pl['new_toc'], b'\0')
    head = bytearray(pl['head'])
    head[4:12] = struct.pack('<II', pl['new_toc'], len(ents))
    with open(src, 'rb') as fs, open(dst, 'wb') as fd:
        fd.write(bytes(head))
        fd.write(rpf3.encrypt_toc(toc) if pl['enc'] else toc)
        fd.write(bytes(pl['first_data'] - fd.tell()))
        for n, i in enumerate(order):
            off, size = place[i]
            fd.write(bytes(off - fd.tell()))
            p = pay[i]
            if p[0] == 'orig':
                fs.seek(p[1])
                left = size
                while left:
                    b = fs.read(min(left, CHUNK))
                    if not b:
                        raise RpfRebuildError('the source archive ends early')
                    fd.write(b)
                    left -= len(b)
            else:
                with open(p[1], 'rb') as fp:
                    while True:
                        b = fp.read(CHUNK)
                        if not b:
                            break
                        fd.write(b)
            fd.write(bytes(-size % SECTOR))
            if n % 200 == 0:
                progress('Writing %s: %d%%' % (os.path.basename(dst), 100 * n // len(order)))
    return pl['end']


def _plan(src, replace, add, grow):
    replace = dict(replace or {})
    add = list(add or [])
    head, toc_size, enc, ents = _read_toc(src)
    dirs = _dirs(ents)
    # replace keys: a file hash, or (directory hash, file hash) - the same file hash can exist in two directories
    # (carchive_audio.rpf has such pairs), a bare hash takes the first entry that has it
    parent = {}
    for d in dirs:
        first, cnt = ents[d][2] & 0x7FFFFFFF, ents[d][3]
        for k in range(first, first + cnt):
            parent[k] = ents[d][0]
    # payload per entry: None (directory) | ('orig', data_off, data_size, align) | ('path', path, align)
    pay = []
    for i, (h, f2, f3, f4) in enumerate(ents):
        if i in dirs:
            pay.append(None)
            continue
        if f4 >> 31:
            off, size = f3 & 0x7FFFFF00, f2
        elif f4 >> 30:
            off, size = f3, f4 & 0x3FFFFFFF
        else:
            off, size = f3, f2
        key = (parent.get(i), h) if (parent.get(i), h) in replace else (h if h in replace else None)
        if key is not None:
            if f4 >> 30:
                raise RpfRebuildError('file %08x is compressed / a resource and cannot be replaced' % h)
            pay.append(('path', replace.pop(key), max(_align_of(off), SECTOR), off))
        else:
            pay.append(('orig', off, size, _align_of(off)))
    if replace:
        raise RpfRebuildError('file(s) not found in the archive: ' + ', '.join(
            '%08x/%08x' % k if isinstance(k, tuple) else '%08x' % k for k in replace))
    first_data = min(p[1] if p[0] == 'orig' else p[3] for p in pay if p)
    # added files: inserted at their sorted place in the directory, directory indices behind them move by one
    for n, (dir_hash, file_hash, path) in enumerate(add):
        di = [i for i in range(len(ents)) if pay[i] is None and ents[i][0] == dir_hash]
        if len(di) != 1:
            raise RpfRebuildError('directory %08x: %d matches' % (dir_hash, len(di)))
        di = di[0]
        first, cnt = ents[di][2] & 0x7FFFFFFF, ents[di][3]
        kids = list(range(first, first + cnt))
        if any(pay[k] is None for k in kids):
            raise RpfRebuildError('directory %08x contains sub-directories' % dir_hash)
        if any(ents[k][0] == file_hash for k in kids):
            raise RpfRebuildError('file %08x already exists in directory %08x' % (file_hash, dir_hash))
        pos = first + sum(1 for k in kids if ents[k][0] < file_hash)
        for i, e in enumerate(ents):
            if pay[i] is None:
                f0 = e[2] & 0x7FFFFFFF
                if i == di:
                    e[3] += 1
                elif f0 >= pos:
                    e[2] = 0x80000000 | (f0 + 1)
        ents.insert(pos, [file_hash, 0, 0, 0])
        pay.insert(pos, ('path', path, NEW_ALIGN, None, n))
    count = len(ents)
    new_toc = max(toc_size, -(-count * 16 // SECTOR) * SECTOR)
    if new_toc > toc_size and not grow:
        raise RpfRebuildError('no spare room in the TOC (%d entries)' % count)
    if SECTOR + new_toc > first_data:
        raise RpfRebuildError('the TOC cannot grow beyond the first file data at 0x%x' % first_data)
    # layout: original order of the data, added files last
    order = sorted((i for i, p in enumerate(pay) if p), key=lambda i: (
        pay[i][1] if pay[i][0] == 'orig' else (pay[i][3] if pay[i][3] is not None else 1 << 40), pay[i][-1] if pay[i][3] is None else 0))
    pos = first_data
    place = {}
    for i in order:
        p = pay[i]
        size = p[2] if p[0] == 'orig' else _size(p[1])
        a = p[3] if p[0] == 'orig' else p[2]
        pos = -(-pos // a) * a
        place[i] = (pos, size)
        pos += size + (-size % SECTOR)
    for i, (off, size) in place.items():
        h, f2, f3, f4 = ents[i]
        if pay[i][0] == 'orig':
            ents[i][2] = (off | (f3 & 0xFF)) if f4 >> 31 else off
        else:
            ents[i] = [h, size, off, size]
    return {'head': head, 'enc': enc, 'ents': ents, 'pay': pay, 'order': order, 'place': place, 'end': pos,
            'first_data': first_data, 'new_toc': new_toc}
