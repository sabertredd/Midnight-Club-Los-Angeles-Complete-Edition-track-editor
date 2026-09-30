"""PS3 Midnight Club LA music track (CARCHIVE_MUSIC.RPF entry): two mono MPEG-1 Layer III streams (no bit reservoir).

Layout (big-endian), same scheme as the X360 XMA tracks:
  file header 0x50: [0, hdr_size, nblocks, 0x20000, 0, 0, 0x30, 0, hdr_size, 2, 0, first_block, 0, 0,
                     id0, sec0, 0, sec0, id1, sec1]
  per stream section (0x38 + 2*nframes): [0, 0, id, stream bytes, total samples, 0xffffffff, 44100<<16|low, 0x100,
                     0, 0x38, 1152, nframes, w12, w13] + u16 frame sizes
  block table: one (start sample, 44100) per block, padded to 0x800 -> first block
  blocks of 0x20000: 0x800 header + stream-0 region (cnt0 * 0x800) + stream-1 region (cnt1 * 0x800)
    header words: [0, 0x18, 0, 0x48, 0, 0x48, 0, cnt0, skip0, n0, frames0, bytes0, cnt0, cnt1, skip1, n1, frames1, bytes1]
                  + per stream table [base, c1-1, c1, ..., E-1]
    a 'packet' = greedy group of whole frames of at most 0x800 bytes; a region holds cnt packets' frames back to back
    (zero padded to cnt*0x800); a block holds 62 packets in total.
"""
import math
import struct

SPF = 1152
PK = 0x800
BLOCK = 0x20000
SLOTS = 62
RATE = 44100
BR = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


SR = (44100, 48000, 32000)          # a few end-of-race clips are 48 kHz


def frame_len(h):
    if h[0] != 0xFF or (h[1] & 0xFE) != 0xFA or (h[2] >> 2) & 3 == 3:
        raise ValueError('not an MPEG-1 Layer III frame: %s' % bytes(h[:4]).hex())
    return 144000 * BR[h[2] >> 4] // SR[(h[2] >> 2) & 3] + ((h[2] >> 1) & 1)


def split_frames(d, off=0, n=None):
    out = []
    while (n is None and off + 4 <= len(d) and d[off] == 0xFF) or (n is not None and len(out) < n):
        ln = frame_len(d[off:off + 4])
        out.append(bytes(d[off:off + ln]))
        off += ln
    return out, off


def packets(frames):
    """Greedy grouping of whole frames into packets of <= 0x800 bytes -> list of (first frame, count)."""
    out = []
    i = 0
    while i < len(frames):
        size, k = 0, i
        while k < len(frames) and size + len(frames[k]) <= PK:
            size += len(frames[k])
            k += 1
        if k == i:
            raise ValueError('frame larger than a packet')
        out.append((i, k - i))
        i = k
    return out


# ---------------------------------------------------------------------------------------------------------- parsing
def parse(d):
    """-> dict(ids, total, low, words (stream header words 7..13), frames [s0, s1], blocks [...])."""
    be = lambda o, n=1: struct.unpack_from('>%dI' % n, d, o)
    hdr_size, nblocks, bsize = be(4, 3)
    first = be(0x2c)[0]
    streams = []
    o = 0x50
    secs = be(0x3c)[0], be(0x4c)[0]
    for s in range(be(0x24)[0]):
        w = be(o, 14)
        # most tracks append a u16 frame-size table to the stream section; the South Central ones (and one other)
        # have a bare 0x38-byte section
        sizes = struct.unpack_from('>%dH' % w[11], d, o + 0x38) if secs[s] > 0x38 else None
        streams.append(dict(w=w, sizes=sizes))
        o += secs[s]
    bt = [be(o + 8 * i, 2) for i in range(nblocks)]
    frames = [{}, {}]
    blocks = []
    for b in range(nblocks):
        off = first + b * bsize
        w = be(off, 18)
        p = off + PK
        info = []
        for s in range(2):
            cnt, skip, n, nfr, nby = (w[7], w[8], w[9], w[10], w[11]) if s == 0 else (w[13], w[14], w[15], w[16], w[17])
            if s == 1:
                p = off + PK + w[7] * PK
            fs, _ = split_frames(d, p, nfr)
            tbl = be(off + 0x48 + (0 if s == 0 else 8 * w[7]), 2 * cnt)
            start = tbl[0] // SPF
            for i, f in enumerate(fs):
                frames[s].setdefault(start + i, f)
            info.append(dict(cnt=cnt, skip=skip, n=n, start=start, nfr=nfr, nby=nby))
        blocks.append(info)
    fr = [[f[k] for k in range(len(f))] for f in frames]
    for s in range(2):
        if streams[s]['sizes'] is not None:
            assert [len(x) for x in fr[s]] == list(streams[s]['sizes']), 'size table mismatch'
        assert len(fr[s]) == streams[s]['w'][11], 'frame count mismatch'
    return dict(ids=(streams[0]['w'][2], streams[1]['w'][2]), total=streams[0]['w'][4],
                low=tuple(st['w'][6] & 0xffff for st in streams), rate=streams[0]['w'][6] >> 16,
                words=[st['w'][7:14] for st in streams], frames=fr, blocks=blocks, bt=bt,
                size_table=streams[0]['sizes'] is not None)


# ---------------------------------------------------------------------------------------------------------- layout
def default_delta(t):
    """How much later the ahead channel ends than the behind one: T * 1.00001 in float32, floored. Matches ~90 % of
    the original block ends exactly, the rest differ by one sample (the original tool's exact arithmetic is unknown)."""
    f32 = lambda x: struct.unpack('<f', struct.pack('<f', x))[0]
    # float32 * float32 is exact in a double; rounding that to float32 is the single-precision product
    return int(math.floor(f32(f32(t) * f32(1.00001)))) - t


def plan_blocks(fr0, fr1, total, delta=default_delta):
    """Which packets go to which block; rules as the X360 planner (see xma_layout.plan_blocks):
    fill 62 packet slots, always adding to the channel with fewer frames so far (ties -> stream 0); the channel that
    is ahead repeats its last packet at the start of the next block; behind channel ends at its frame border T,
    ahead channel at T + T//100000."""
    pk = [packets(fr0), packets(fr1)]
    nxt = [0, 0]
    dup = [False, False]
    blocks = []
    while True:
        first = [nxt[c] - 1 if dup[c] else nxt[c] for c in (0, 1)]
        cnt = [1 if dup[c] else 0 for c in (0, 1)]
        used = sum(cnt)

        def done(c):          # frames through the last packet included so far
            k = first[c] + cnt[c] - 1
            return pk[c][k][0] + pk[c][k][1] if k >= 0 else 0
        while used < SLOTS:
            avail = [c for c in (0, 1) if first[c] + cnt[c] < len(pk[c])]
            if not avail:
                break
            c = min(avail, key=lambda x: (done(x), x))
            cnt[c] += 1
            used += 1
        last = [first[c] + cnt[c] - 1 for c in (0, 1)]
        final = last[0] >= len(pk[0]) - 1 and last[1] >= len(pk[1]) - 1
        fend = [done(c) for c in (0, 1)]
        blk = dict(first=first, cnt=cnt, pk=pk, fend=fend)
        blocks.append(blk)
        if final:
            blk['E'] = [total, total]
            break
        ahead = 0 if fend[0] > fend[1] else 1
        behind = 1 - ahead
        T = SPF * fend[behind]
        E = [0, 0]
        E[behind] = T
        E[ahead] = T + max(delta(T), 1)
        blk['E'] = E
        for c in (0, 1):
            dup[c] = c == ahead
            nxt[c] = last[c] + 1
    return blocks


def build_track(ids, frames, total, low=0xff41, words=None, ends=None, size_table=True, rate=RATE):
    """ids: stream ids in file order (ascending); frames: [stream0 frames, stream1 frames] (bytes, no reservoir).
    words: optional per-stream header words 7..13 (to reproduce originals); ends: optional {block: [E0, E1]};
    size_table=False writes the bare section variant (South Central tracks)."""
    plan = plan_blocks(frames[0], frames[1], total)
    starts = [0, 0]
    blocks = []
    bstarts = []
    prev_hdr = bytes(PK)
    prevE = None
    for bi, pl in enumerate(plan):
        E = list(ends[bi]) if ends and bi in ends else list(pl['E'])
        bstarts.append(0 if bi == 0 else SPF * (min(prevE) // SPF))
        prevE = E
        head = [0, 0x18, 0, 0x48, 0, 0x48, 0]
        tables = []
        regions = []
        for c in (0, 1):
            pk = pl['pk'][c][pl['first'][c]:pl['first'][c] + pl['cnt'][c]]
            f0 = pk[0][0]
            base = SPF * f0
            skip = starts[c] - base
            n = E[c] - base
            if skip < 0 or n <= skip:
                raise ValueError('cannot lay out block %d (stream %d: skip %d, samples %d)' % (bi, c, skip, n))
            starts[c] = E[c]
            fs = frames[c][f0:pk[-1][0] + pk[-1][1]]
            data = b''.join(fs)
            t = [base]
            for k in range(1, len(pk)):
                cpos = SPF * pk[k][0]
                t += [cpos - 1, cpos]
            t.append(E[c] - 1)
            tables += t
            vals = [skip, n, len(fs), len(data)]
            head += ([len(pk)] + vals) if c == 0 else ([len(pk)] + vals)
            regions.append(data.ljust(len(pk) * PK, b'\0'))
        head = head[:7] + head[7:12] + [head[7]] + head[12:]
        hb = struct.pack('>%dI' % (len(head) + len(tables)), *(head + tables))
        hdr = hb + prev_hdr[len(hb):]            # the rest of the header buffer keeps the previous block's bytes
        prev_hdr = hdr
        body = hdr + b''.join(regions)
        blocks.append(body.ljust(BLOCK, b'\0'))
    secs = [0x38 + (2 * len(frames[c]) if size_table else 0) for c in (0, 1)]
    hdr_size = 0x50 + sum(secs)
    first_block = (hdr_size + 8 * len(blocks) + PK - 1) // PK * PK
    h = [0, hdr_size, len(blocks), BLOCK, 0, 0, 0x30, 0, hdr_size, 2, 0, first_block, 0, 0,
         ids[0], secs[0], 0, secs[0], ids[1], secs[1]]
    out = bytearray(struct.pack('>20I', *h))
    for c in (0, 1):
        w = list(words[c]) if words else [0x100, 0, 0x38, SPF, len(frames[c]), 0x10000000, 0]
        lo = low[c] if isinstance(low, (tuple, list)) else low
        s = [0, 0, ids[c], sum(len(f) for f in frames[c]), total, 0xffffffff, (rate << 16) | lo] + w
        out += struct.pack('>14I', *s)
        if size_table:
            out += struct.pack('>%dH' % len(frames[c]), *[len(f) for f in frames[c]])
    out += b''.join(struct.pack('>II', s, rate) for s in bstarts)
    return bytes(out).ljust(first_block, b'\0') + b''.join(blocks)
