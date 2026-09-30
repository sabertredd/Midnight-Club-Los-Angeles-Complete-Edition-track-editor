"""Midnight Club: Los Angeles (Xbox 360) RPF3 unpacker.

  py mcla_unpack.py list    [-g GAMEDIR] [archive ...]
  py mcla_unpack.py extract [-g GAMEDIR] [-o OUTDIR] [--only SUBSTR] [archive ...]

archive = music | audio | audlo | cache (default: all). Output layout:
  OUTDIR/<archive>/<dir hash>/.../<file name>.<ext>
  OUTDIR/<archive>/manifest.json   (TOC index, hash, offset, size, magic, path - for repacking)
Names in the RPF are joaat hashes; put known names (one per line) into names.txt
next to this script to have them resolved.
"""
import argparse
import json
import os
import sys
import time
import zlib

import lzx
import rpf3

DEFAULT_GAME = '.'          # the game folder (xarchive_*.rpf)
ARCHIVES = ('music', 'audio', 'audlo', 'cache')
HERE = os.path.dirname(os.path.abspath(__file__))
MAGIC_EXT = {b'RIFF': '.wav', b'OggS': '.ogg', b'BIKi': '.bik', b'SR\0\0': '.sr', b'\0\0\0\0': '.bin'}


def open_archive(game, name):
    p = os.path.join(game, 'xarchive_%s.rpf' % name)
    if not os.path.isfile(p):
        sys.exit('not found: ' + p)
    t = time.time()
    r = rpf3.RPF3(p)
    print('[%s] TOC decrypted: %d entries (%.1fs)' % (name, r.count, time.time() - t), flush=True)
    return r


def payload(r, e, decode_rsc):
    """Return (bytes, extension, extra manifest fields) for an entry."""
    raw = r.read(e)
    if e.kind == 'zlib':
        out = zlib.decompressobj(-15).decompress(raw)
        return out, '.dat', {}
    if e.kind == 'rsc':
        extra = {'rsc_type': e.offset & 0xFF, 'rsc_flags': '%08x' % e.disk}
        if decode_rsc and raw[:4] == b'\x05CSR' and raw[12:16] == bytes.fromhex('0ff512ef'):
            return lzx.decompress_frames(raw[20:], 17), '.rsc', extra
        return raw, '.rsc.z', extra
    return raw, MAGIC_EXT.get(raw[:4], '.dat'), {}


def entry_record(r, e):
    return {'index': e.idx, 'hash': '%08x' % e.hash, 'path': e.path, 'kind': e.kind, 'data_offset': e.data_off,
            'data_size': e.data_size, 'size': e.size, 'flags': '%08x' % e.disk}


def out_path(outdir, name, e, ext):
    p = e.path.replace('/', os.sep)
    return os.path.join(outdir, name, p if os.path.splitext(p)[1] else p + ext)


def cmd_list(args):
    for name in args.archive:
        r = open_archive(args.game, name)
        fs = r.files()
        print('[%s] %d files, %.1f MB' % (name, len(fs), sum(e.size for e in fs) / 1048576))
        for e in fs:
            if args.only.lower() in e.path.lower():
                print('  %5d %-50s %10d  @0x%08x' % (e.idx, e.path, e.size, e.offset))


def cmd_extract(args):
    total = 0
    for name in args.archive:
        r = open_archive(args.game, name)
        size_all = os.path.getsize(r.path)
        manifest, done = [], 0
        files = [e for e in r.files() if args.only.lower() in e.path.lower()]
        for n, e in enumerate(files, 1):
            if e.data_off + e.data_size > size_all:
                print('  !! entry %d (%s) exceeds archive, skipped' % (e.idx, e.path))
                continue
            try:
                data, ext, extra = payload(r, e, args.rsc)
            except Exception as ex:
                print('  !! entry %d (%s) %s: %s' % (e.idx, e.path, type(ex).__name__, ex))
                continue
            dst = out_path(args.out, name, e, ext)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as o:
                o.write(data)
            if e.kind != 'rsc' and len(data) != e.size:
                print('  !! size mismatch:', dst, len(data), e.size)
            rec = entry_record(r, e)
            rec.update(extra)
            rec['magic'] = data[:4].hex()
            rec['file'] = os.path.relpath(dst, os.path.join(args.out, name)).replace(os.sep, '/')
            manifest.append(rec)
            done += len(data)
            if n % 100 == 0 or n == len(files):
                print('  [%s] %d/%d files, %.0f MB' % (name, n, len(files), done / 1048576), flush=True)
        os.makedirs(os.path.join(args.out, name), exist_ok=True)
        with open(os.path.join(args.out, name, 'manifest.json'), 'w') as m:
            json.dump({'archive': os.path.basename(r.path), 'entries': manifest}, m, indent=1)
        total += done
    print('done, %.0f MB extracted' % (total / 1048576))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('list', 'extract'))
    ap.add_argument('archive', nargs='*', help='music audio audlo cache (default: all)')
    ap.add_argument('-g', '--game', default=DEFAULT_GAME)
    ap.add_argument('-o', '--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--only', default='', help='only paths containing this substring (hash or name)')
    ap.add_argument('--rsc', action='store_true', help='also LZX-decompress RSC5 resources (slow, pure Python)')
    args = ap.parse_args()
    args.archive = args.archive or list(ARCHIVES)
    bad = [a for a in args.archive if a not in ARCHIVES]
    if bad:
        ap.error('unknown archive: %s' % bad)
    (cmd_list if args.cmd == 'list' else cmd_extract)(args)


if __name__ == '__main__':
    main()
