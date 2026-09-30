"""Extract a (decrypted) PS3 ISO9660 image to a folder, multi-extent files joined.

Usage: py iso_extract.py <image.iso> <out_dir>
"""
import os
import struct
import sys


def list_files(f, joliet=True):
    """path -> extents. Uses the Joliet tree when present: it has the real (mixed / lower case) names, the ISO9660 tree
    only upper case ones, and RPCS3 looks names up case-sensitively like the console."""
    def sec(n, cnt=1):
        f.seek(n * 2048)
        return f.read(2048 * cnt)

    vd_sector, enc = 16, 'ascii'
    if joliet:
        for s in range(17, 32):
            d = sec(s)
            if d[1:6] != b'CD001' or d[0] == 255:
                break
            if d[0] == 2 and d[88:91] in (b'%/@', b'%/C', b'%/E'):
                vd_sector, enc = s, 'utf-16-be'
                break

    files = {}   # path -> [(lba, size), ...] in extent order

    def walk(lba, size, path):
        data = sec(lba, (size + 2047) // 2048)
        i = 0
        while i < len(data):
            ln = data[i]
            if ln == 0:
                i = (i // 2048 + 1) * 2048
                continue
            r = data[i:i + ln]
            i += ln
            elba, esz = struct.unpack('<I', r[2:6])[0], struct.unpack('<I', r[10:14])[0]
            flags, nl = r[25], r[32]
            name = r[33:33 + nl]
            if name in (b'\x00', b'\x01'):
                continue
            p = path + '/' + name.decode(enc, 'replace').split(';')[0]
            if flags & 2:
                walk(elba, esz, p)
            else:
                files.setdefault(p, []).append((elba, esz))

    root = sec(vd_sector)[156:190]
    walk(struct.unpack('<I', root[2:6])[0], struct.unpack('<I', root[10:14])[0], '')
    return files


class Cancelled(Exception):
    pass


CACHE = 'PS3_GAME/USRDIR/carchive_cache.rpf'


def inspect(iso):
    """Look at an image before extracting it -> (error text or None, total bytes of its files). Checks that it is an ISO
    of Midnight Club: Los Angeles for PS3 and that the game files are not encrypted (a raw disc dump)."""
    try:
        with open(iso, 'rb') as f:
            f.seek(16 * 2048)
            if f.read(6)[1:6] != b'CD001':
                return 'This is not a disc image (ISO 9660).', 0
            files = list_files(f)
            total = sum(s for exts in files.values() for _, s in exts)
            cache = next((v for k, v in files.items() if k.strip('/').lower() == CACHE.lower()), None)
            if not cache:
                return ('%s is not in this image - is it the PlayStation 3 disc of Midnight Club: Los Angeles?'
                        % CACHE.replace('/', '\\')), total
            f.seek(cache[0][0] * 2048)
            if f.read(4) != b'RPF3':
                return ('The game files are ENCRYPTED in this image (a raw disc dump). Decrypt the ISO first (e.g. with '
                        'its disc key), then extract it.'), total
            return None, total
    except (OSError, struct.error, IndexError, ValueError) as e:
        return 'Cannot read the image: %s' % e, 0


def extract(iso, out, progress=None, cancel=None):
    """progress(done bytes, total bytes, current file) is called while copying; cancel: threading.Event - stops between
    chunks (the unfinished file is removed; files already extracted stay and are skipped by a later run)"""
    with open(iso, 'rb') as f:
        files = list_files(f)
        total = sum(s for exts in files.values() for _, s in exts)
        done = 0
        for p, exts in files.items():
            dst = os.path.join(out, *p.strip('/').split('/'))
            size_all = sum(s for _, s in exts)
            if os.path.exists(dst) and os.path.getsize(dst) == size_all:
                done += size_all
                if progress:
                    progress(done, total, p)
                else:
                    print('skip', p)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            part = dst + '.part'
            try:
                with open(part, 'wb') as o:
                    for lba, size in exts:
                        f.seek(lba * 2048)
                        left = size
                        while left:
                            if cancel is not None and cancel.is_set():
                                raise Cancelled()
                            chunk = f.read(min(left, 1 << 24))
                            if not chunk:
                                raise OSError('the image ends early (%s)' % p)
                            o.write(chunk)
                            left -= len(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total, p)
            except BaseException:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
            os.replace(part, dst)
            if not progress:
                print('ok', p, size_all, flush=True)


def check(out):
    """the game archives must start with 'RPF3' - an encrypted disc image (e.g. a plain redump dump) gives garbage"""
    p = os.path.join(out, 'PS3_GAME', 'USRDIR', 'carchive_cache.rpf')
    if not os.path.isfile(p):
        return 'PS3_GAME/USRDIR/carchive_cache.rpf not found - is this the Midnight Club: Los Angeles disc?'
    with open(p, 'rb') as f:
        if f.read(4) != b'RPF3':
            return ('The game files are ENCRYPTED in this image (a raw disc dump). Decrypt the ISO first '
                    '(e.g. with the disc key), then extract it again.')
    return None


def default_target(iso):
    """<folder of the image>\\<name of the image>_extracted"""
    return os.path.join(os.path.dirname(os.path.abspath(iso)), os.path.splitext(os.path.basename(iso))[0] + '_extracted')


if __name__ == '__main__':
    err, _ = inspect(sys.argv[1])
    if err:
        print('ERROR: ' + err)
        sys.exit(1)
    extract(sys.argv[1], sys.argv[2])
    err = check(sys.argv[2])
    print('\nERROR: ' + err if err else '\nDone: %s\nOpen this folder in the editor (and in RPCS3: File > Boot Game).'
          % sys.argv[2])
    sys.exit(1 if err else 0)
