"""RPF3 (RAGE Package File, Xbox 360 Midnight Club LA) reader / extractor.

TOC entries are 16 bytes little-endian: name_hash, size, offset, size_on_disk
(directories: name_hash, 0, 0x80000000|first_child, child_count).
Names are not stored, only RAGE joaat hashes; supply a names.txt to resolve them.

Usage:
  py rpf3.py list    <archive.rpf>
  py rpf3.py extract <archive.rpf> <outdir> [hex_hash_or_name_substring]
"""
import functools
import os
import struct
import sys
from aes_pure import ecb_rounds

TOC_ROUNDS = 16
HERE = os.path.dirname(os.path.abspath(__file__))

# The AES-256 key of the archive tables belongs to the game and is NOT part of this program. It is read from
# mcla_rpf_key.txt (64 hex digits) next to the program (the exe when frozen) or from the environment variable
# MCLA_RPF_KEY. A key can be checked against any archive (check_key) and found in the game's own decrypted
# executable (find_key).
KEY = None
KEY_FILE = 'mcla_rpf_key.txt'


class KeyMissing(Exception):
    pass


def key_dir():
    """folder the key file is saved to: next to the exe when frozen, else next to this module"""
    return os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else HERE


def _parse_key(text):
    t = ''.join((text or '').split()).lower()
    if len(t) == 64 and all(c in '0123456789abcdef' for c in t):
        return bytes.fromhex(t)
    return None


def load_key():
    """the key from MCLA_RPF_KEY or a key file (None when there is none); sets KEY"""
    global KEY
    if KEY is None:
        k = _parse_key(os.environ.get('MCLA_RPF_KEY'))
        for d in (key_dir(), HERE):
            if k:
                break
            try:
                with open(os.path.join(d, KEY_FILE), encoding='ascii', errors='replace') as f:
                    k = _parse_key(f.read())
            except OSError:
                pass
        KEY = k
    return KEY


def get_key():
    k = load_key()
    if k is None:
        raise KeyMissing('the archive key is not set up: put %s (64 hex digits) next to the program - see README'
                         % KEY_FILE)
    return k


def set_key(key, save=True):
    """use this key from now on and (save=True) store it in the key file"""
    global KEY
    KEY = bytes(key)
    if save:
        with open(os.path.join(key_dir(), KEY_FILE), 'w', encoding='ascii') as f:
            f.write(KEY.hex() + '\n')


def _toc_block(archive):
    """first 16 encrypted bytes of an archive's table, or None for an unencrypted archive"""
    with open(archive, 'rb') as f:
        magic, _, count, _, enc = struct.unpack('<4sIIII', f.read(20))
        if magic != b'RPF3':
            raise ValueError('%s is not an RPF3 archive' % archive)
        f.seek(0x800)
        return f.read(16) if enc else None


def _root_ok(plain):
    """the first table entry is the root folder: hash 0, n, 0x80000001, n"""
    h, n, first, n2 = struct.unpack('<IIII', plain)
    return h == 0 and first == 0x80000001 and n == n2 and 0 < n < 0x100000


def check_key(key, archive):
    """True when key decrypts the table of this archive"""
    blk = _toc_block(archive)
    return blk is None or _root_ok(ecb_rounds(key, blk, TOC_ROUNDS, cache=False))


def find_key(data, archive, progress=lambda done, total: None, cancel=lambda: False):
    """Search a decrypted executable of the game (bytes) for the archive key: every 32-byte window that looks random
    enough is tried on the first table block of the archive. Returns the key or None."""
    blk = _toc_block(archive)
    if blk is None:
        raise ValueError('%s is not encrypted' % archive)
    n = max(0, len(data) - 32)
    for s0 in range(4):                                  # word-aligned windows first, then the other offsets
        for i in range(s0, n, 4):
            if i % 0x40000 < 4:
                if cancel():
                    return None
                progress(s0 * n + i, 4 * n)
            w = data[i:i + 32]
            if len(set(w)) >= 28 and _root_ok(ecb_rounds(w, blk, TOC_ROUNDS, cache=False)):
                return bytes(w)
    return None


@functools.lru_cache(maxsize=None)
def joaat(s):
    h = 0
    for c in s.lower().replace('\\', '/').encode('latin1'):
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    return h


def load_names():
    names = {}
    p = os.path.join(HERE, 'names.txt')
    if os.path.exists(p):
        for line in open(p, encoding='utf8', errors='replace'):
            line = line.strip()
            if line:
                names[joaat(line)] = line
                names[joaat(os.path.splitext(line)[0])] = names.get(joaat(os.path.splitext(line)[0]), line)
    return names


def decrypt_toc(data):
    """16 x AES-256 ECB per 16-byte block; a trailing partial block is kept."""
    n = len(data) // 16 * 16
    return ecb_rounds(get_key(), data[:n], TOC_ROUNDS) + bytes(data[n:])


def encrypt_toc(data):
    """Inverse of decrypt_toc (16 x AES-256 ECB per 16-byte block); trailing partial block is kept."""
    n = len(data) // 16 * 16
    return ecb_rounds(get_key(), data[:n], TOC_ROUNDS, encrypt=True) + bytes(data[n:])


class Entry:
    def __init__(self, idx, h, f2, f3, f4):
        self.idx, self.hash = idx, h
        self.is_dir = bool(f3 & 0x80000000)
        if self.is_dir:
            self.first, self.count = f3 & 0x7FFFFFFF, f4
            self.size = self.offset = 0
        else:
            self.size, self.offset, self.disk = f2, f3, f4
            if f4 >> 31:      # resource: offset&0x7FFFFF00, low byte = type; data = RSC5 header + LZX frames
                self.kind, self.data_off, self.data_size = 'rsc', f3 & 0x7FFFFF00, f2
            elif f4 >> 30:    # deflate-compressed plain file: exact offset, compressed size in low 30 bits
                self.kind, self.data_off, self.data_size = 'zlib', f3, f4 & 0x3FFFFFFF
            else:
                self.kind, self.data_off, self.data_size = 'raw', f3, f2
        self.path = ''
        self.parent = None    # hash of the directory holding the entry (the same file hash can be in two directories)


class RPF3:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        magic, toc_size, count, _, enc = struct.unpack('<4sIIII', self.f.read(20))
        assert magic == b'RPF3', magic
        self.count = count
        self.f.seek(0x800)
        raw = self.f.read(count * 16)
        toc = decrypt_toc(raw + bytes(-len(raw) % 16)) if enc else raw
        self.names = load_names()
        self.entries = [Entry(i, *struct.unpack('<IIII', toc[i * 16:i * 16 + 16])) for i in range(count)]
        self.entries[0].path = ''
        self._walk(self.entries[0])

    def label(self, e):
        return self.names.get(e.hash, '%08x' % e.hash)

    def _walk(self, d):
        for i in range(d.first, d.first + d.count):
            e = self.entries[i]
            e.path = (d.path + '/' if d.path else '') + self.label(e)
            e.parent = d.hash
            if e.is_dir:
                self._walk(e)

    def files(self):
        return [e for e in self.entries if not e.is_dir and e.path]

    def read(self, e, n=None):
        """Raw bytes as stored in the archive (compressed for 'zlib' / 'rsc' entries)."""
        self.f.seek(e.data_off)
        return self.f.read(e.data_size if n is None else min(n, e.data_size))


if __name__ == '__main__':
    cmd, p = sys.argv[1], sys.argv[2]
    r = RPF3(p)
    if cmd == 'list':
        for e in r.entries[1:]:
            print('%4d %-60s %s' % (e.idx, e.path, 'DIR (%d)' % e.count if e.is_dir else '%10d @ 0x%08x  %-4s %s' % (
                e.size, e.data_off, e.kind, r.read(e, 4))))
    elif cmd == 'extract':
        out = sys.argv[3]
        flt = sys.argv[4].lower() if len(sys.argv) > 4 else ''
        for e in r.files():
            if flt in e.path.lower():
                dst = os.path.join(out, e.path.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, 'wb') as o:
                    r.f.seek(e.data_off)
                    left = e.data_size
                    while left:
                        chunk = r.f.read(min(left, 1 << 22)); o.write(chunk); left -= len(chunk)
                print('extracted', dst, e.size)
