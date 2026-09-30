"""sounds.dat (audio/x360/config/sounds.dat in cache.rpf) as objects + relocation tables that can be edited and written back.

Layout (big-endian; identity rebuild is byte-exact):
  [0:9)      prefix: u32 0x10, u32 end-of-objects (dir coordinates = file pos - 8), 1 byte
  objects    contiguous, each = body (len = dir size) + one 0 terminator; the LAST object has no terminator
             (the extra region starts right behind its body). dir offset = real start - 8.
             object body: byte 0 = class (12 = wave slot, 8 = sound, 14 = random list, ...), bytes 1..4 (for most
             classes) = u32 offset of the object's NAME inside the concatenated directory names (sum of len+1 of all
             preceding directory entries).
  extra      string table (u32 size, u32 count, offsets, strings) - copied verbatim
  dir head   u32 number of objects, u32 sum(len(name)+1)
  directory  [u8 len][name][NUL][u24 offset][u32 size] per object, in offset order
  tables     u32 nA, nA x u32 (absolute file positions of the hash references inside the objects - a relocation table),
             u32 nB, nB x u32 (absolute positions of the wave-path hash field inside class-12 objects)
Objects reference each other by joaat hash; the tables are moved along with the objects (positions are kept relative to
the object they belong to and re-based on writing).
"""
import bisect, re, struct

SHIFT = 8
PREFIX = 9
CLASS_WAVE_SLOT = 12


class SoundsError(Exception):
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


class SoundsDat:
    def __init__(self, plain):
        D = bytes(plain)
        found = None
        try:                                                 # where the layout says: behind the string table
            p = struct.unpack('>I', D[4:8])[0] + SHIFT
            p += 4 + struct.unpack('>I', D[p:p + 4])[0] + 8
            ents, end = _parse_dir(D, p)
            if len(ents) > 1000:
                found = (p, ents, end)
        except struct.error:
            pass
        p = 0x1000
        while not found and p < len(D) - 100:                # else: the directory is the long run of valid records
            ents, end = _parse_dir(D, p)
            if len(ents) > 1000:
                found = (p, ents, end)
                break
            p += 1
        if not found:
            raise SoundsError('sounds.dat directory not found')
        dstart, ents, dend = found
        self.header_end = struct.unpack('>I', D[4:8])[0]
        cnt, namesum = struct.unpack('>II', D[dstart - 8:dstart])
        if cnt != len(ents) or namesum != sum(len(n) + 1 for n, _, _ in ents):
            raise SoundsError('directory header counters do not match')
        if ents[0][1] != 1 or self.header_end != ents[-1][1] + ents[-1][2]:
            raise SoundsError('unexpected object range')
        self.prefix = D[:PREFIX]
        self.order = [n for n, _, _ in ents]
        self.body = {}
        self.term = {}                                       # object keeps a 0 terminator behind it
        starts = []
        for i, (n, off, size) in enumerate(ents):
            if i + 1 < len(ents):
                if ents[i + 1][1] != off + size + 1 or D[off + SHIFT + size] != 0:
                    raise SoundsError('object %s is not contiguous' % n)
            self.body[n] = D[off + SHIFT:off + SHIFT + size]
            starts.append(off + SHIFT)
        region_start = self.header_end + SHIFT
        self.extra = D[region_start:dstart - 8]
        # relocation tables
        t = dend
        nA = struct.unpack('>I', D[t:t + 4])[0]
        A = struct.unpack('>%dI' % nA, D[t + 4:t + 4 + 4 * nA])
        t += 4 + 4 * nA
        nB = struct.unpack('>I', D[t:t + 4])[0]
        B = struct.unpack('>%dI' % nB, D[t + 4:t + 4 + 4 * nB])
        if t + 4 + 4 * nB != len(D):
            raise SoundsError('unexpected bytes at the end of the file')
        self.refs = {n: [] for n in self.order}              # positions of hash references, relative to the body start
        for x in A:
            i = bisect.bisect_right(starts, x) - 1
            n = self.order[i]
            rel = x - starts[i]
            if not 0 <= rel < len(self.body[n]):
                raise SoundsError('reference %d lies outside its object' % x)
            self.refs[n].append(rel)
        self.wrefs = {n: [] for n in self.order}             # positions of wave-path hashes (table B), relative
        for x in B:
            i = bisect.bisect_right(starts, x) - 1
            n = self.order[i]
            rel = x - starts[i]
            if not 0 <= rel < len(self.body[n]):
                raise SoundsError('wave reference %d lies outside its object' % x)
            self.wrefs[n].append(rel)
        self.names_at = {}                                   # name -> offset inside the names blob (for the id field)
        acc = 0
        for n in self.order:
            self.names_at[n] = acc
            acc += len(n) + 1

    def add_stream_path(self, path):
        """Append a wave path ('MUSIC\\NAME') to the string table that sits between the objects and the directory
        (u32 size-of-rest, u32 count, count x u32 offsets relative to the string area, strings). The wave-path hash
        fields of the class-12 objects (table B) are resolved against it."""
        ex = self.extra
        size, n = struct.unpack('>II', ex[:8])
        if size != len(ex) - 4:
            raise SoundsError('unexpected string table size')
        base = 8 + 4 * n
        strings = ex[base:]
        paths = [strings[o:strings.index(b'\0', o)].decode('latin1') for o in struct.unpack('>%dI' % n, ex[8:base])]
        if path in paths:
            raise SoundsError('stream path exists: ' + path)
        rest = ex[8:base] + struct.pack('>I', len(strings)) + strings + path.encode('latin1') + b'\0'
        self.extra = struct.pack('>II', 0, n + 1) + rest
        self.extra = struct.pack('>I', len(self.extra) - 4) + self.extra[4:]     # size word = length of the rest

    # ------------------------------------------------------------------------------------------
    def serialize(self):
        out = bytearray(self.prefix)
        recs = bytearray()
        A, B = [], []
        last = len(self.order) - 1
        names = 0
        for i, n in enumerate(self.order):
            b = self.body[n]
            start = len(out)
            if start - SHIFT >= 1 << 24:
                raise SoundsError('object offsets no longer fit into 24 bits')
            A += [start + r for r in sorted(self.refs[n])]
            B += [start + r for r in sorted(self.wrefs[n])]
            out += b
            if i != last:
                out += b'\0'
            nb = n.encode()
            recs += bytes([len(nb)]) + nb + b'\0' + (start - SHIFT).to_bytes(3, 'big') + struct.pack('>I', len(b))
            names += len(nb) + 1
        struct.pack_into('>I', out, 4, len(out) - SHIFT)
        out += self.extra
        out += struct.pack('>II', len(self.order), names) + recs
        out += struct.pack('>I', len(A)) + b''.join(struct.pack('>I', x) for x in A)
        out += struct.pack('>I', len(B)) + b''.join(struct.pack('>I', x) for x in B)
        return bytes(out)


if __name__ == '__main__':
    import sys
    D = open(sys.argv[1] if len(sys.argv) > 1 else 'out_cfg/sounds.dat.bin', 'rb').read()
    s = SoundsDat(D)
    out = s.serialize()
    print(len(s.order), 'objects; identity rebuild:', 'IDENTICAL' if out == D else 'DIFFERENT (%d vs %d bytes)' % (len(out), len(D)))
