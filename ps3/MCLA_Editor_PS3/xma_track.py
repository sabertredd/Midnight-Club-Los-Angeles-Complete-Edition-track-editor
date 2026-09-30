"""Build a MCLA music track file (xarchive_music.rpf / audlo.rpf entry) from two mono XMA1 packet streams.

  build_track(name, left_packets, right_packets, total_samples) -> bytes
Layout (all values big-endian, see NOTES.md):
  file header (0x30 + 0x20 + two stream sections), padded to a multiple of 0x800 -> first block offset;
  blocks of 0x20000: 0x800 header (packet counts, base/skip/samples, seek tables), L packets, R packets, zero padding.
"""
import struct

import rpf3
import xma_layout as X

PK = 0x800
BLOCK = 0x20000
RATE = 44100
LOW_DEFAULT = 0xff41            # low half of the rate word: 76 of 108 original tracks


def _table(first, cnt, cum, base, E):
    """[base] + (c-1, c) pairs for every packet but the last + [E-1]; c = 512*(frames completed + 1)."""
    t = [base]
    for k in range(cnt - 1):
        c = 512 * (cum[first + k] + 1)
        t += [c - 1, c]
    t.append(E - 1)
    return t


def build_track(name, left, right, total, low=LOW_DEFAULT, ends=None, ids=None, first_block=None, channel_ids=None):
    """left/right: lists of 2048-byte XMA1 packets. ends: optional {block index: {'L': E, 'R': E}} overrides
    (used to reproduce original files); by default every block boundary is placed exactly on a frame boundary and
    the last block ends at `total` in both channels. order: which channel is stream 0 in the file."""
    if ids is None:
        # the two streams are stored in ascending order of their ids (all 108 originals); stream ids are looked up by
        # joaat(<wave name> + '_LEFT' / '_RIGHT'), so the channel that has the smaller id goes first
        idl, idr = channel_ids or (rpf3.joaat(name + '_LEFT'), rpf3.joaat(name + '_RIGHT'))
        if idl <= idr:
            ids = (idl, idr)
        else:
            left, right = right, left
            ids = (idr, idl)
    ch = {'L': left, 'R': right}             # 'L' / 'R' below simply mean stream 0 / stream 1 of the file
    cum = {c: X.cumulative(ch[c]) for c in 'LR'}
    # the channel that is ahead ends its block T//100000 samples later than the other one, exactly like the originals: this
    # makes `skip` of a repeated (dup) packet > 0 in every case. With delta = 0 a dup packet can get skip = 0, which real
    # hardware does not accept (that stream then stays silent from the first such block on); Xenia does not care.
    plan = X.plan_blocks(cum['L'], cum['R'])
    if not (ends and len(plan) - 1 in ends) and 'E' not in plan[-1]:
        # The audio can end exactly where the last block starts for one channel (the block before already played it up
        # to `total`; what is left of that channel is the encoder's silent tail). A block with nothing to play cannot be
        # described, so the track is made one frame longer: 512 samples (12 ms) of that silent tail are played as well.
        last_base = max(plan[-1]['base'][c] for c in 'LR')
        if total <= last_base and last_base + 512 <= min(512 * (cum[c][-1] + 1) for c in 'LR'):
            total = last_base + 512
    ids = {'L': ids[0], 'R': ids[1]}
    blocks = []
    block_starts = []                               # for the per-block table after the file header
    starts = {'L': 0, 'R': 0}                       # playable start (global sample) of the next block
    for bi, pl in enumerate(plan):
        E = dict(pl['E']) if 'E' in pl else {'L': total, 'R': total}
        if ends and bi in ends:
            E = dict(ends[bi])
        block_starts.append(0 if bi == 0 else min(prev_E.values()))   # block b starts where block b-1 stopped
        prev_E = E
        words = {}
        info = {}
        for c in 'LR':
            base = pl['base'][c]
            skip = starts[c] - base
            n = E[c] - base
            if skip < 0 or n <= 0:
                raise ValueError('cannot lay out block %d: the two channels are too different in density '
                                 '(skip %d, samples %d)' % (bi, skip, n))
            info[c] = (base, skip, n)
            starts[c] = E[c]
        first, cnt = pl['first'], pl['count']
        tl = _table(first['L'], cnt['L'], cum['L'], info['L'][0], E['L'])
        tr = _table(first['R'], cnt['R'], cum['R'], info['R'][0], E['R'])
        hdr = [0, 0x18, 0, 0x38, 0, 0x38, 0, cnt['L'], info['L'][1], info['L'][2], cnt['L'], cnt['R'],
               info['R'][1], info['R'][2]] + tl + tr
        assert len(hdr) * 4 <= 0x800
        hb = struct.pack('>%dI' % len(hdr), *hdr).ljust(0x800, b'\0')
        pk = []
        for c in ('L', 'R'):
            pk += ch[c][first[c]:first[c] + cnt[c]]
        body = b''.join(pk)
        blocks.append(hb + body + bytes(BLOCK - 0x800 - len(body)))
    # unique packets per channel = what the seek tables of the file header cover
    nL, nR = len(left), len(right)
    secL, secR = 0x38 + 4 * nL, 0x38 + 4 * nR
    hdr_size = 0x50 + secL + secR
    # after the two stream sections: one (start sample, rate) entry per block, then padding to 0x800
    block_table = b''.join(struct.pack('>II', s, RATE) for s in block_starts)
    if first_block is None:
        first_block = (hdr_size + len(block_table) + 0x7ff) // 0x800 * 0x800
    h = [0, hdr_size, len(blocks), BLOCK, 0, 0, 0x30, 0, hdr_size, 2, 0, first_block, 0, 0,
         ids['L'], secL, 0, secL, ids['R'], secR]
    out = bytearray(struct.pack('>%dI' % len(h), *h))
    for c, n, sec in (('L', nL, secL), ('R', nR, secR)):
        s = [0, 0, ids[c], n * PK, total, 0xffffffff, (RATE << 16) | low, 0, 0, 0x38, 0, 0, 0, n]
        s += [0] + [512 * (cum[c][k] + 1) for k in range(n - 1)]
        out += struct.pack('>%dI' % len(s), *s)
    assert len(out) == hdr_size, (len(out), hdr_size)
    out = (bytes(out) + block_table).ljust(first_block, b'\0')
    return out + b''.join(blocks)
