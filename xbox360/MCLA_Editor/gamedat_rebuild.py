"""game.dat as a list of objects that can be resized / removed / added, then written back with a fresh directory.

Layout (all big-endian, verified on the original: parse -> serialize gives identical bytes):
  [0:9)      prefix: u32 0x16, u32 end-of-objects (in directory coordinates), 1 byte
  objects    contiguous; directory offset of an object = its real start - 8, directory size = real length - 1
  tail       15 bytes: 00 00 04 00000000 (7 bytes), u32 number of directory records, u32 sum of (1 + len(name))
  directory  [u8 len][name][NUL][u24 offset][u32 size] per object, in offset order
  suffix     8 zero bytes
Objects refer to each other by joaat hash, not by offset (no absolute offsets found inside objects).
"""
import re, struct

SHIFT = 8            # directory offset = real start - 8
PREFIX = 9


class GameDatError(Exception):
    pass


def _parse_dir(D, p):
    out = []
    while p < len(D):
        n = D[p]
        if n == 0 or p + 1 + n + 1 + 7 > len(D):
            break
        name = D[p + 1:p + 1 + n]
        if D[p + 1 + n] != 0 or not re.fullmatch(rb'[\x20-\x7e]+', name):
            break
        out.append((name.decode(), int.from_bytes(D[p + n + 2:p + n + 5], 'big'), int.from_bytes(D[p + n + 5:p + n + 9], 'big')))
        p += 1 + n + 1 + 7
    return out, p


class GameDat:
    def __init__(self, plain):
        D = bytes(plain)
        anchor = D.find(b'MUSIC_0_MANAGER\x00') - 1
        best = None
        for start in range(anchor - 60000, anchor):        # the directory starts with the first object record
            if start < 0 or D[start] == 0:
                continue
            ents, end = _parse_dir(D, start)
            if len(ents) > 1000 and end == len(D) - 8 and ents[0][1] == 1:
                best = (start, ents)
                break
        if not best:
            raise GameDatError('game.dat directory not found')
        dstart, ents = best
        self.header_end = struct.unpack('>I', D[4:8])[0]
        n_end = ents[-1][1] + ents[-1][2] + 1
        if self.header_end != n_end - 1:
            raise GameDatError('header end word does not match the last object')
        self.prefix = D[:PREFIX]
        self.order = [n for n, _, _ in ents]
        self.obj = {}
        for i, (n, off, size) in enumerate(ents):
            nxt = ents[i + 1][1] if i + 1 < len(ents) else n_end
            if nxt - off != size + 1:
                raise GameDatError('object %s is not contiguous' % n)
            self.obj[n] = D[off + SHIFT:nxt + SHIFT]
        objs_end = n_end + SHIFT
        self.tail = D[objs_end:dstart]
        if len(self.tail) != 15:
            raise GameDatError('unexpected bytes between the objects and the directory')
        cnt, names = struct.unpack('>II', self.tail[7:15])
        if cnt != len(ents) or names != sum(len(n) + 1 for n in self.order):
            raise GameDatError('tail counters do not match the directory')
        self.suffix = D[len(D) - 8:]

    def serialize(self):
        out = bytearray(self.prefix)
        recs = bytearray()
        for n in self.order:
            real = self.obj[n]
            start = len(out)
            if start - SHIFT >= 1 << 24:
                raise GameDatError('object offsets no longer fit into 24 bits')
            out += real
            nb = n.encode()
            recs += bytes([len(nb)]) + nb + b'\0' + (start - SHIFT).to_bytes(3, 'big') + struct.pack('>I', len(real) - 1)
        struct.pack_into('>I', out, 4, len(out) - SHIFT - 1)
        tail = self.tail[:7] + struct.pack('>II', len(self.order), sum(len(n) + 1 for n in self.order))
        return bytes(out) + tail + bytes(recs) + self.suffix

    # ---- edits -------------------------------------------------------------------------------
    def replace(self, name, real):
        if name not in self.obj:
            raise GameDatError('no object ' + name)
        self.obj[name] = bytes(real)

    def remove(self, name):
        del self.obj[name]
        self.order.remove(name)

    def add(self, name, real, after=None):
        if name in self.obj:
            raise GameDatError('object exists: ' + name)
        self.obj[name] = bytes(real)
        self.order.insert(self.order.index(after) + 1 if after else len(self.order), name)


if __name__ == '__main__':
    import sys
    D = open(sys.argv[1] if len(sys.argv) > 1 else 'out_cfg/game.dat.bin', 'rb').read()
    g = GameDat(D)
    out = g.serialize()
    print(len(g.order), 'objects; identity rebuild:', 'IDENTICAL' if out == D else 'DIFFERENT')

