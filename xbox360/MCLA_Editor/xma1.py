"""XMA1 packet / frame structure (bit level, MSB first).

Packet (2048 bytes): 32-bit header = seq(4) | 2 bits | prev_bits(15) | ... ; the first `prev_bits` bits after the header
finish the frame that started in the previous packet; new frames follow, each starting with a 15-bit length in bits
(the length includes these 15 bits); a frame may run over into the next packet (its remainder = next packet's prev_bits).
"""
import struct

PK = 2048
PK_BITS = PK * 8
HDR_BITS = 32


class Bits:
    def __init__(self, data):
        self.v = int.from_bytes(data, 'big')
        self.n = len(data) * 8

    def get(self, pos, n):
        return (self.v >> (self.n - pos - n)) & ((1 << n) - 1)


def parse_packet(pk):
    """-> dict(seq, prev_bits, frames=[(start_bit, length_bits)], tail_bits) ; tail_bits = bits of the last frame that run into the next packet."""
    b = Bits(pk)
    seq = b.get(0, 4)
    x = b.get(4, 2)
    prev = b.get(6, 15)
    rest = b.get(21, 11)
    pos = HDR_BITS + prev
    frames = []
    tail = 0
    while pos < PK_BITS - 15:
        ln = b.get(pos, 15)
        if ln == 0:
            break
        frames.append((pos, ln))
        pos += ln
        if pos > PK_BITS:
            tail = pos - PK_BITS
            break
    return {'seq': seq, 'x': x, 'prev': prev, 'rest': rest, 'frames': frames, 'tail': tail, 'end': pos}


def parse_stream(data):
    pks = [data[i:i + PK] for i in range(0, len(data) - PK + 1, PK)]
    return [parse_packet(p) for p in pks]


if __name__ == '__main__':
    import sys
    d = open(sys.argv[1], 'rb').read()
    i = d.find(b'data')
    size = struct.unpack('<I', d[i + 4:i + 8])[0]
    ps = parse_stream(d[i + 8:i + 8 + size])
    bad = 0
    for k in range(len(ps) - 1):
        if ps[k]['tail'] != ps[k + 1]['prev']:
            bad += 1
            if bad < 6:
                print('mismatch at packet', k, 'tail', ps[k]['tail'], 'next prev', ps[k + 1]['prev'], 'end', ps[k]['end'])
    print('packets', len(ps), 'chain mismatches', bad)
    print('first packets:', [(p['seq'], p['x'], p['prev'], len(p['frames']), p['tail']) for p in ps[:6]])
    print('frames total (starting in packets):', sum(len(p['frames']) for p in ps))
