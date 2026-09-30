"""Single-stream MCLA track files (xarchive_audio.rpf, e.g. the hangout music clips HNG_LM_MUSIC_*): one mono XMA1 stream
at 48 kHz in blocks of 0x8000 - the same layout family as the two-stream music tracks (xma_track.py):

  file header   [0, H, blocks, 0x8000, 0, 0, 0x30, 0, H, 1, 0, first block] + one entry [0, section offset 0, id, section size]
  section       [0, 0, id, packets*0x800, total samples, ffffffff, rate<<16|low, 0, 0, 0x38, 0, 0, 0, packets]
                + seek table [0] + 512*(frames completed + 1) for every packet but the last
  block table   (start sample, rate) per block, then zero padding up to the first block (a multiple of 0x800)
  blocks        0x800 header [0, 0x18, 0, 0x28, 0, 0x28, 0, count, skip 0, samples] + [base] + (c-1, c) pairs + [end-1],
                then 14 packets (the last block fewer), zero padding to 0x8000

Every block ends on a frame boundary (no repeated packet, skip 0), the last one at the total sample count.

  parse(data) -> {'id', 'rate', 'low', 'total', 'packets'}
  build(packets, total, sid, rate=48000, low=0xff5f) -> bytes
"""
import struct

import xma_layout as X

PK = 0x800
BLOCK = 0x8000
PER_BLOCK = BLOCK // PK - 2          # 14 packets per block (the first 2 KB is the block header, one slot stays free)
LOW_DEFAULT = 0xff5f


def parse(d):
    w = struct.unpack_from('>12I', d, 0)
    if w[9] != 1:
        raise ValueError('not a single-stream track (%d streams)' % w[9])
    nblocks, bsize, first = w[2], w[3], w[11]
    sec = struct.unpack_from('>14I', d, 0x40)
    sid, total, rate, low, n = sec[2], sec[4], sec[6] >> 16, sec[6] & 0xffff, sec[13]
    packets = []
    for b in range(nblocks):
        o = first + b * bsize
        cnt = struct.unpack_from('>I', d, o + 0x1c)[0]
        packets += [d[o + PK + k * PK:o + PK + (k + 1) * PK] for k in range(cnt)]
    if len(packets) != n:
        raise ValueError('packet count %d != %d of the stream section' % (len(packets), n))
    return {'id': sid, 'rate': rate, 'low': low, 'total': total, 'packets': packets, 'block_size': bsize}


def to_wav(d, wav, ffmpeg):
    """decode a single-stream track to a WAV file with ffmpeg (its xma2 decoder, like mcla_music.py)"""
    import os
    import subprocess
    import tempfile
    P = parse(d)
    data = b''.join(P['packets'])
    fmt = struct.pack('<HHIIHHH', 0x166, 1, P['rate'], 16000, PK, 16, 34)
    fmt += struct.pack('<HIIIIIIIBBH', 1, 4, P['total'], PK, 0, P['total'], 0, 0, 0, 4, len(data) // PK)
    body = b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(data)) + data
    fd, xma = tempfile.mkstemp(suffix='.xma')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
        r = subprocess.run([ffmpeg, '-hide_banner', '-v', 'error', '-y', '-i', xma, '-af',
                            'atrim=end_sample=%d' % P['total'], '-c:a', 'pcm_s16le', wav], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or 'ffmpeg failed')
    finally:
        os.remove(xma)
    return P


def build(packets, total, sid, rate=48000, low=LOW_DEFAULT):
    n = len(packets)
    if not n:
        raise ValueError('no packets')
    cum = X.cumulative(packets)
    # the audio may end in an earlier packet than the last one (the encoder's silent tail): the last block must still play
    # something, so the length is at least one frame into it
    nb = -(-n // PER_BLOCK)
    last_base = 512 * (cum[(nb - 1) * PER_BLOCK - 1] + 1) if nb > 1 else 0
    if total <= last_base:
        total = last_base + 512
    blocks, starts, prev = [], [], b''
    for b in range(nb):
        first, cnt = b * PER_BLOCK, min(PER_BLOCK, n - b * PER_BLOCK)
        base = 512 * (cum[first - 1] + 1) if first else 0
        end = total if b == nb - 1 else 512 * (cum[first + cnt - 1] + 1)
        if end <= base:
            raise ValueError('block %d holds no complete frame' % b)
        t = [base]
        for k in range(cnt - 1):
            c = 512 * (cum[first + k] + 1)
            t += [c - 1, c]
        t.append(end - 1)
        hdr = [0, 0x18, 0, 0x28, 0, 0x28, 0, cnt, 0, end - base] + t
        body = b''.join(packets[first:first + cnt])
        # the game's tool reused one header buffer: behind a shorter table the previous block's header bytes remain
        hb = struct.pack('>%dI' % len(hdr), *hdr)
        prev = hb + prev[len(hb):] if blocks else hb.ljust(PK, b'\0')
        blocks.append(prev + body + bytes(BLOCK - PK - len(body)))
        starts.append(base)
    sec_size = 0x38 + 4 * n
    hdr_size = 0x40 + sec_size
    table = b''.join(struct.pack('>II', s, rate) for s in starts)
    first_block = (hdr_size + len(table) + PK - 1) // PK * PK
    h = [0, hdr_size, nb, BLOCK, 0, 0, 0x30, 0, hdr_size, 1, 0, first_block, 0, 0, sid, sec_size]
    s = [0, 0, sid, n * PK, total, 0xffffffff, (rate << 16) | low, 0, 0, 0x38, 0, 0, 0, n]
    s += [0] + [512 * (cum[k] + 1) for k in range(n - 1)]
    out = struct.pack('>%dI' % len(h), *h) + struct.pack('>%dI' % len(s), *s)
    assert len(out) == hdr_size
    return (out + table).ljust(first_block, b'\0') + b''.join(blocks)
