"""Generalised parser for MCLA PS3 MP3 tracks: any number of streams, any block size (analysis).

File header: [0, hdr, nblocks, bsize, 0, 0, 0x30, 0, hdr, ns, 0, first] + per stream a 16-byte entry
             [0, section offset (from the first section), id, section size]; sections follow at 0x30 + 16*ns:
             [0, 0, id, bytes, samples, ffffffff, rate<<16|low, 0x100, 0, 0x38, 1152, nframes, w12, w13] (+ u16 sizes)
             then (start sample, rate) per block.
Block:       [0, 0x18, 0, H, 0, H] + per stream [page offset, pages, skip, samples, frames, bytes] (H = 0x18 + 0x18*ns)
             + per stream table [base, c-1, c, ..., E-1] (2*pages words), then the stream regions (pages*0x800 each).
"""
import struct
import mp3_track as M

PK = 0x800


def parse(d):
    be = lambda o, n=1: struct.unpack_from('>%dI' % n, d, o)
    hdr, nblocks, bsize = be(4, 3)
    ns, first = be(0x24)[0], be(0x2c)[0]
    ents = [be(0x30 + 16 * i, 4) for i in range(ns)]
    sec0 = 0x30 + 16 * ns
    streams = []
    for i, (_, soff, sid, ssize) in enumerate(ents):
        o = sec0 + soff
        w = be(o, 14)
        assert w[2] == sid, ('stream id mismatch', hex(w[2]), hex(sid))
        sizes = struct.unpack_from('>%dH' % w[11], d, o + 0x38) if ssize > 0x38 else None
        streams.append(dict(id=sid, w=w, sizes=sizes, rate=w[6] >> 16, total=w[4]))
    bt = [be(sec0 + sum(e[3] for e in ents) + 8 * i, 2) for i in range(nblocks)]
    frames = [{} for _ in range(ns)]
    blocks = []
    for b in range(nblocks):
        off = first + b * bsize
        H = be(off + 12)[0]
        assert H == 0x18 + 0x18 * ns, ('block header size', b, hex(H))
        info = []
        tab = off + H
        for s in range(ns):
            pgoff, cnt, skip, n, nfr, nby = be(off + 0x18 + 0x18 * s, 6)
            t = be(tab, 2 * cnt)
            tab += 8 * cnt
            p = off + PK + pgoff * PK
            fs, end = M.split_frames(d, p, nfr)
            assert end - p == nby, ('bytes', b, s)
            start = t[0] // 1152
            for i, f in enumerate(fs):
                frames[s].setdefault(start + i, f)
            info.append(dict(pgoff=pgoff, cnt=cnt, skip=skip, n=n, start=start, nfr=nfr, nby=nby, table=t))
        blocks.append(info)
    fr = [[f[k] for k in range(len(f))] for f in frames]
    for s in range(ns):
        if streams[s]['sizes'] is not None:
            assert [len(x) for x in fr[s]] == list(streams[s]['sizes']), 'size table mismatch'
    return dict(ns=ns, bsize=bsize, first=first, hdr=hdr, streams=streams, frames=fr, blocks=blocks, bt=bt)


def build_single(sid, frames, total, rate, low, words, bsize=0x8000):
    """One-stream track (hangout / ambience layout): greedy packets of whole frames <= 0x800 bytes, bsize/0x800 - 2 packets
    per block, no repeated packet, each block ends on a frame border (the last one at `total`), skip 0."""
    pk = M.packets(frames)
    slots = bsize // PK - 2
    blocks, bstarts = [], []
    prev_hdr = bytes(PK)
    start = 0
    for bi in range(0, len(pk), slots):
        grp = pk[bi:bi + slots]
        f0 = grp[0][0]
        f1 = grp[-1][0] + grp[-1][1]
        base = M.SPF * f0
        E = total if bi + slots >= len(pk) else M.SPF * f1
        bstarts.append(start)
        start = E
        data = b''.join(frames[f0:f1])
        t = [base]
        for k in range(1, len(grp)):
            c = M.SPF * grp[k][0]
            t += [c - 1, c]
        t.append(E - 1)
        H = 0x18 + 0x18
        head = [0, 0x18, 0, H, 0, H, 0, len(grp), 0, E - base, f1 - f0, len(data)]
        hb = struct.pack('>%dI' % (len(head) + len(t)), *(head + t))
        hdr = hb + prev_hdr[len(hb):]
        prev_hdr = hdr
        blocks.append((hdr + data.ljust(len(grp) * PK, b'\0')).ljust(bsize, b'\0'))
    sec = 0x38 + 2 * len(frames)
    hdr_size = 0x30 + 16 + sec
    first = (hdr_size + 8 * len(blocks) + PK - 1) // PK * PK
    out = bytearray(struct.pack('>16I', 0, hdr_size, len(blocks), bsize, 0, 0, 0x30, 0, hdr_size, 1, 0, first,
                                0, 0, sid, sec))
    words = list(words)
    words[4] = len(frames)                    # [0x100, 0, 0x38, 1152, frame count, w12, w13]: the count is this track's
    out += struct.pack('>14I', 0, 0, sid, sum(len(f) for f in frames), total, 0xffffffff, (rate << 16) | low, *words)
    out += struct.pack('>%dH' % len(frames), *[len(f) for f in frames])
    out += b''.join(struct.pack('>II', s, rate) for s in bstarts)
    return bytes(out).ljust(first, b'\0') + b''.join(blocks)


if __name__ == '__main__':
    import glob
    import os
    for path in sorted(glob.glob(os.path.join('hangout', '*.bin'))):
        d = open(path, 'rb').read()
        P = parse(d)
        s = P['streams'][0]
        g = build_single(s['id'], P['frames'][0], s['total'], s['rate'], s['w'][6] & 0xffff, s['w'][7:14], P['bsize'])
        diff = next((i for i, (x, y) in enumerate(zip(g, d)) if x != y), None)
        print('REBUILD %-42s %s' % (os.path.basename(path), 'EXACT' if g == d else 'differs at %s (len %d/%d)' % (
            hex(diff) if diff is not None else '-', len(g), len(d))))
    for path in sorted(glob.glob(os.path.join('hangout', '*.bin'))):
        d = open(path, 'rb').read()
        P = parse(d)
        s = P['streams'][0]
        print('%-45s ns %d  %.1f s  %d Hz  frames %d  blocks %d' % (
            os.path.basename(path), P['ns'], s['total'] / s['rate'], s['rate'], len(P['frames'][0]), len(P['blocks'])))
        for b, info in enumerate(P['blocks'][:4]):
            i = info[0]
            print('    blk %d pages %d start %d frames %d skip %d n %d  (end sample %d, bt %d)' % (
                b, i['cnt'], i['start'], i['nfr'], i['skip'], i['n'], 1152 * i['start'] + i['n'], P['bt'][b][0]))
