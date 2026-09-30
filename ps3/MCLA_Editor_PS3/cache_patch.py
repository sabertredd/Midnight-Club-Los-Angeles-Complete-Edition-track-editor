"""Write replaced (raw-deflate) files into a copy of xarchive_cache.rpf.

patch_cache(src, dst, {entry_index: new_plain_bytes}): copies src to dst, appends the re-deflated data at the end
(sector aligned), redirects the TOC entries (size = uncompressed, offset exact, disk = 0x40000000 | compressed size)
and re-encrypts the TOC. dst must not be open in another program (e.g. close Xenia first).
"""
import os, shutil, struct, zlib
import rpf3

SECTOR = 0x800


def deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    out = c.compress(b) + c.flush()
    assert zlib.decompressobj(-15).decompress(out) == b
    return out


def patch_cache(src, dst, patches, log=print):
    r = rpf3.RPF3(src)
    ent = {e.idx: e for e in r.files()}
    for idx in patches:
        assert ent[idx].kind == 'zlib', (idx, ent[idx].kind)
    if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copyfile(src, dst)
    with open(dst, 'r+b') as f:
        f.seek(0, 2)
        end = f.tell()
        f.seek(0x800)
        raw = f.read(r.count * 16)
        dec = bytearray(rpf3.decrypt_toc(raw + bytes(-len(raw) % 16)))
        pos = (end + SECTOR - 1) // SECTOR * SECTOR
        f.seek(end)
        f.write(bytes(pos - end))
        for idx, plain in patches.items():
            comp = deflate(plain)
            e = ent[idx]
            f.seek(pos)
            f.write(comp)
            nxt = (pos + len(comp) + SECTOR - 1) // SECTOR * SECTOR
            f.write(bytes(nxt - pos - len(comp)))
            dec[idx * 16:idx * 16 + 16] = struct.pack('<IIII', e.hash, len(plain), pos, 0x40000000 | len(comp))
            log('entry %d %s: %d -> %d bytes deflated, now at 0x%x' % (idx, e.path, len(plain), len(comp), pos))
            pos = nxt
        f.seek(0x800)
        f.write(rpf3.encrypt_toc(bytes(dec))[:len(raw)])
    return dst
