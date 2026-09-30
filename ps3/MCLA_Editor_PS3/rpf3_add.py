"""Add NEW plain files to directories of an RPF3 archive (music.rpf / audlo.rpf / audio.rpf style: raw, no compression).

The TOC is a flat list of 16-byte entries; the children of a directory are contiguous and sorted by name hash, so a new
entry is inserted at its sorted place and the `first` index of every directory that lies behind it moves by one.
File data is appended at the end of the archive (0x8000 aligned).

TOC size: the header stores toc_size = count*16 rounded up to 0x800, and the whole region is encrypted. While the new
entries fit into toc_size nothing else changes. With grow=True the TOC may also grow (toc_size is raised to the next multiple
of 0x800 and the added area is filled with encrypted zeros) as long as it stays below the first file data of the archive
(0x8000 in all game archives, i.e. room for 1920 entries). grow=False keeps the original limit. (Growing was verified on a
real Xbox 360: 40 new songs, music.rpf 2 KB -> 4 KB and audlo.rpf 18 KB -> 20 KB.)

Appender collects any number of files and writes (and encrypts) the TOC once in close(); add_entry() adds a single file.
"""
import os
import shutil
import struct

import rpf3

ALIGN = 0x8000
SECTOR = 0x800


class RpfAddError(Exception):
    pass


def toc_room(path, grow=False):
    """How many more entries fit into the TOC of an archive (without / with growing it)."""
    r = rpf3.RPF3(path)
    try:
        with open(path, 'rb') as f:
            _, toc_size, count = struct.unpack('<4sII', f.read(12))
        first = min(e.data_off for e in r.files() if e.data_size)
    finally:
        r.f.close()
    return ((first - SECTOR) // 16 if grow else toc_size // 16) - count


class Appender:
    def __init__(self, src, dst, grow=False):
        r = rpf3.RPF3(src)
        self.first_data = min(e.data_off for e in r.files() if e.data_size)
        r.f.close()
        with open(src, 'rb') as f:
            head = f.read(0x20)
            _, self.toc_size, self.count, _, self.enc = struct.unpack('<4sIIII', head[:20])
            f.seek(0x800)
            raw = f.read(self.count * 16)
        toc = rpf3.decrypt_toc(raw + bytes(-len(raw) % 16)) if self.enc else raw
        self.ents = [struct.unpack_from('<IIII', toc, i * 16) for i in range(self.count)]
        self.grow = grow
        if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
            if os.path.exists(dst):
                os.remove(dst)
            shutil.copyfile(src, dst)
        self.f = open(dst, 'r+b')
        self.f.seek(0, 2)
        self.added = 0

    def add(self, dir_hash, file_hash, data):
        ents = self.ents
        di = [i for i, e in enumerate(ents) if e[2] & 0x80000000 and e[0] == dir_hash]
        if len(di) != 1:
            raise RpfAddError('directory %08x: %d matches' % (dir_hash, len(di)))
        di = di[0]
        first, cnt = ents[di][2] & 0x7FFFFFFF, ents[di][3]
        children = ents[first:first + cnt]
        if any(c[2] & 0x80000000 for c in children):
            raise RpfAddError('directory %08x contains sub-directories' % dir_hash)
        if any(c[0] == file_hash for c in children):
            raise RpfAddError('file %08x already exists in directory %08x' % (file_hash, dir_hash))
        if [c[0] for c in children] != sorted(c[0] for c in children):
            raise RpfAddError('children of the directory are not sorted by hash')
        pos = first + sum(1 for c in children if c[0] < file_hash)
        new_count = len(ents) + 1
        if new_count * 16 > (-(-new_count * 16 // SECTOR) * SECTOR if self.grow else self.toc_size):
            raise RpfAddError('no spare room in the TOC (%d entries)' % new_count)
        if self.grow and SECTOR + (-(-new_count * 16 // SECTOR) * SECTOR) > self.first_data:
            raise RpfAddError('the TOC cannot grow beyond the first file data at 0x%x' % self.first_data)
        end = self.f.tell()
        off = (end + ALIGN - 1) // ALIGN * ALIGN
        self.f.write(bytes(off - end))
        self.f.write(data + bytes(-len(data) % SECTOR))
        for i, (h, a, b, c) in enumerate(ents):
            if b & 0x80000000:                                   # directory: (hash, 0, 0x80000000|first, count)
                f0 = b & 0x7FFFFFFF
                if i == di:
                    c += 1
                elif f0 >= pos:
                    f0 += 1
                ents[i] = (h, a, 0x80000000 | f0, c)
        ents.insert(pos, (file_hash, len(data), off, len(data)))
        self.added += 1

    def close(self):
        n = len(self.ents)
        new_size = max(self.toc_size, -(-n * 16 // SECTOR) * SECTOR)
        toc = b''.join(struct.pack('<IIII', *e) for e in self.ents)
        if new_size > self.toc_size:                             # grown TOC: the added area is encrypted zeros like the rest
            toc += bytes(new_size - len(toc))
        self.f.seek(0x800)
        self.f.write(rpf3.encrypt_toc(toc) if self.enc else toc)
        self.f.seek(4)
        self.f.write(struct.pack('<I', new_size))
        self.f.seek(8)
        self.f.write(struct.pack('<I', n))
        self.f.close()


def add_entry(src, dst, dir_hash, file_hash, data, grow=False):
    a = Appender(src, dst, grow)
    a.add(dir_hash, file_hash, data)
    a.close()
    return None
