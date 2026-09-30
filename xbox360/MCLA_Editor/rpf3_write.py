"""Replace a plain (non-compressed) file inside an RPF3 archive.

  py rpf3_write.py replace <src.rpf> <dst.rpf> <hash|path-substring> <newfile> [--append]

In place (default): new data is written over the old file's slot (must fit, size field is updated).
--append: new data is appended at the end of the archive (aligned to 0x8000) and the TOC offset is redirected;
the old slot is left as unused space (--wipe-old zeroes it). The encrypted TOC (entries only, key from rpf3)
is rewritten.
"""
import os
import shutil
import struct
import sys

import rpf3

ALIGN = 0x8000
SECTOR = 0x800


def find_entry(r, key):
    key = key.lower()
    hits = [e for e in r.files() if ('%08x' % e.hash) == key or key in e.path.lower()]
    if len(hits) != 1:
        raise SystemExit('need exactly one match for %r, got %d' % (key, len(hits)))
    return hits[0]


def replace(src, dst, key, newfile, append=False, wipe_old=False):
    r = rpf3.RPF3(src)
    e = find_entry(r, key)
    if e.kind != 'raw':
        raise SystemExit('only plain (uncompressed) entries are supported, got %s' % e.kind)
    data = open(newfile, 'rb').read()
    padded = data + bytes(-len(data) % SECTOR)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    with open(dst, 'r+b') as f:
        if append:
            f.seek(0, 2)
            end = f.tell()
            new_off = (end + ALIGN - 1) // ALIGN * ALIGN
            f.write(bytes(new_off - end))
        else:
            if len(padded) > e.size + (-e.size % SECTOR):
                raise SystemExit('new file (%d) does not fit the old slot (%d); use --append' % (len(padded), e.size))
            new_off = e.data_off
        f.seek(new_off)
        f.write(padded)
        if wipe_old and append:
            # zero the old slot so nothing can still be read from there (proves the TOC redirect works)
            f.seek(e.data_off)
            left = e.size + (-e.size % SECTOR)
            while left:
                n = min(left, 1 << 22)
                f.write(bytes(n))
                left -= n
        # rewrite the TOC entry
        toc_entry_pos = 0x800 + e.idx * 16
        # decrypt the whole entry table, patch one entry, re-encrypt
        f.seek(0x800)
        raw = f.read(r.count * 16)
        dec = bytearray(rpf3.decrypt_toc(raw + bytes(-len(raw) % 16)))
        dec[e.idx * 16:e.idx * 16 + 16] = struct.pack('<IIII', e.hash, len(data), new_off, len(data))
        enc = rpf3.encrypt_toc(bytes(dec))[:len(raw)]
        f.seek(0x800)
        f.write(enc)
    return e, new_off, len(data)


if __name__ == '__main__':
    if len(sys.argv) < 6 or sys.argv[1] != 'replace':
        sys.exit(__doc__)
    e, off, n = replace(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], '--append' in sys.argv, '--wipe-old' in sys.argv)
    print('replaced %08x (%s): offset 0x%x -> 0x%x, size %d -> %d' % (e.hash, e.path, e.data_off, off, e.size, n))
