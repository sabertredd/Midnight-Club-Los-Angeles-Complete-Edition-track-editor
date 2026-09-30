"""Layout rules of MCLA music tracks, derived from the original files (see NOTES.md).

Given the two mono XMA1 packet streams (left / right), compute for every 0x20000-byte block: how many packets each
channel gets, which channel repeats its last packet (dup), the block base sample, the skip and the sample count.
"""
import xma1

PAY = xma1.PK_BITS - xma1.HDR_BITS
BLOCK_SLOTS = 62            # packets per block (both channels together); the 63rd 2 KB slot is padding


def frames_ending(pks):
    """frames whose last bit lies in each packet (global chain, first packet starts a frame)."""
    n = len(pks)
    ends = [0] * n
    carry = 0
    for i, p in enumerate(pks):
        v = int.from_bytes(p, 'big')
        prev = (v >> (xma1.PK_BITS - 21)) & 0x7fff      # bits of the frame that started in the previous packet
        e, carry = carry, 0
        if prev:
            e += 1                                       # ... it ends in this packet
        pos = xma1.HDR_BITS + prev
        while pos + 15 <= xma1.PK_BITS:
            ln = (v >> (xma1.PK_BITS - pos - 15)) & 0x7fff
            if ln == 0:                                  # zero padding (silent packets hold at most 171 frames)
                break
            end = pos + ln
            if end < xma1.PK_BITS:
                e += 1
                pos = end
            elif end == xma1.PK_BITS:
                carry = 1                                # a frame ending exactly on the packet border counts in the next one
                break
            else:
                break                                    # runs into the next packet, counted there via its 'prev' bits
        ends[i] += e
    ends[-1] += carry
    return ends


def cumulative(pks):
    c, out = 0, []
    for e in frames_ending(pks):
        c += e
        out.append(c)
    return out                      # frames completed through packet i


def plan_blocks(fl, fr, delta=lambda t: t // 100000):
    """fl / fr = cumulative completed frames per packet of each channel.
    Returns a list of dicts describing the blocks. Rules (all verified on the original tracks):
      * both channels are filled packet by packet, always adding to the channel that is behind (fewer completed
        frames; ties -> left) until 62 slots are used; a dup packet occupies one slot of its channel;
      * the channel that is ahead at the end repeats its last packet at the start of the next block;
      * behind channel: block end T = completed*512 + 512 ; ahead channel: E = T + T//100000."""
    ch = {'L': fl, 'R': fr}
    nxt = {'L': 0, 'R': 0}         # next new packet index
    dup = {'L': False, 'R': False}
    end_prev = {'L': 0, 'R': 0}    # playable end of the previous block per channel (global sample)
    blocks = []
    while nxt['L'] < len(fl) or nxt['R'] < len(fr):
        first = {c: (nxt[c] - 1 if dup[c] else nxt[c]) for c in 'LR'}
        cnt = {c: (1 if dup[c] else 0) for c in 'LR'}
        used = cnt['L'] + cnt['R']

        def done(c):                                          # frames completed through the last packet included so far
            k = first[c] + cnt[c] - 1
            return ch[c][k] if k >= 0 else 0
        while used < BLOCK_SLOTS:
            avail = [c for c in 'LR' if first[c] + cnt[c] < len(ch[c])]
            if not avail:
                break
            c = min(avail, key=lambda x: (done(x), x != 'L'))
            cnt[c] += 1
            used += 1
        last = {c: first[c] + cnt[c] - 1 for c in 'LR'}
        F0 = {c: (ch[c][first[c] - 1] if first[c] > 0 else 0) for c in 'LR'}
        Fend = {c: ch[c][last[c]] if last[c] >= 0 else 0 for c in 'LR'}
        base = {c: 512 * (F0[c] + 1) if first[c] > 0 or F0[c] else 0 for c in 'LR'}
        base = {c: (512 * (F0[c] + 1) if first[c] > 0 else 0) for c in 'LR'}
        final = last['L'] >= len(fl) - 1 and last['R'] >= len(fr) - 1
        blocks.append({'first': first, 'count': cnt, 'last': last, 'dup_in': dict(dup), 'base': base,
                       'F0': F0, 'Fend': Fend, 'final': final})
        if final:
            break
        ahead = 'L' if Fend['L'] > Fend['R'] else 'R'
        behind = 'R' if ahead == 'L' else 'L'
        T = 512 * Fend[behind] + 512
        E = {behind: T, ahead: T + delta(T)}
        blocks[-1]['E'] = E
        blocks[-1]['ahead'] = ahead
        for c in 'LR':
            dup[c] = c == ahead
            nxt[c] = last[c] + 1
    return blocks
