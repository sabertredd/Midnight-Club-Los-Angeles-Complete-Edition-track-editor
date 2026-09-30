"""Pure-Python LZX decoder (Microsoft LZX / XMemCompress framing), history kept in memory."""
import struct

EXTRA = []
BASE = []


def _tables():
    eb = []
    for i in range(51):
        eb.append(0 if i < 2 else (i // 2 - 1 if i // 2 - 1 < 17 else 17))
    base = [0]
    for i in range(50):
        base.append(base[-1] + (1 << eb[i]))
    return eb, base


EXTRA, BASE = _tables()
SLOTS = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}


class LZXError(Exception):
    pass


class Huff:
    def __init__(self, lens):
        self.table = None
        maxl = max(lens) if lens else 0
        self.bits = max(maxl, 1)
        tbl = [None] * (1 << self.bits)
        code = 0
        for l in range(1, maxl + 1):
            for sym, sl in enumerate(lens):
                if sl == l:
                    start = code << (self.bits - l)
                    n = 1 << (self.bits - l)
                    if start + n > len(tbl):
                        raise LZXError('over-subscribed huffman tree')
                    tbl[start:start + n] = [(sym, l)] * n
                    code += 1
            code <<= 1
        self.tbl = tbl


class LZX:
    def __init__(self, wbits):
        self.wbits = wbits
        self.nslots = SLOTS[wbits]
        self.main_n = 256 + self.nslots * 8
        self.main_lens = [0] * self.main_n
        self.len_lens = [0] * 249
        self.hist = bytearray()
        self.emitted = 0
        self.R = [1, 1, 1]
        self.block_type = 0
        self.block_rem = 0
        self.started = False
        self.intel = 0

    # ---- bit reader over 16-bit LE words
    def _init_bits(self, data):
        self.d = data
        self.pos = 0
        self.buf = 0
        self.nb = 0

    def _fill(self, n):
        while self.nb < n:
            if self.pos + 1 < len(self.d) + 1 and self.pos < len(self.d):
                w = self.d[self.pos] | ((self.d[self.pos + 1] if self.pos + 1 < len(self.d) else 0) << 8)
            else:
                w = 0
            self.pos += 2
            self.buf = (self.buf << 16) | w
            self.nb += 16

    def bits(self, n):
        if n == 0:
            return 0
        self._fill(n)
        v = (self.buf >> (self.nb - n)) & ((1 << n) - 1)
        self.nb -= n
        self.buf &= (1 << self.nb) - 1
        return v

    def sym(self, h):
        self._fill(h.bits)
        v = (self.buf >> (self.nb - h.bits)) & ((1 << h.bits) - 1)
        e = h.tbl[v]
        if e is None:
            raise LZXError('bad huffman code')
        s, l = e
        self.nb -= l
        self.buf &= (1 << self.nb) - 1
        return s

    def _read_lens(self, lens, first, last):
        pl = [self.bits(4) for _ in range(20)]
        pre = Huff(pl)
        x = first
        while x < last:
            z = self.sym(pre)
            if z == 17:
                y = self.bits(4) + 4
                for _ in range(y):
                    lens[x] = 0; x += 1
            elif z == 18:
                y = self.bits(5) + 20
                for _ in range(y):
                    lens[x] = 0; x += 1
            elif z == 19:
                y = self.bits(1) + 4
                z = self.sym(pre)
                v = (lens[x] - z + 17) % 17
                for _ in range(y):
                    lens[x] = v; x += 1
            else:
                lens[x] = (lens[x] - z + 17) % 17
                x += 1

    def _block_header(self):
        if not self.started:
            self.started = True
            if self.bits(1):
                hi = self.bits(16); lo = self.bits(16)
                self.intel = (hi << 16) | lo
        bt = self.bits(3)
        hi = self.bits(16); lo = self.bits(8)
        self.block_rem = (hi << 8) | lo
        self.block_type = bt
        if bt in (1, 2):
            if bt == 2:
                al = [self.bits(3) for _ in range(8)]
                self.aligned = Huff(al)
            self._read_lens(self.main_lens, 0, 256)
            self._read_lens(self.main_lens, 256, self.main_n)
            self.main = Huff(self.main_lens)
            self._read_lens(self.len_lens, 0, 249)
            self.lenh = Huff(self.len_lens)
        elif bt == 3:
            # align to 16 bits, then R0..R2 as raw LE dwords
            self.nb -= self.nb % 16
            self.buf &= (1 << self.nb) - 1
            self.pos -= (self.nb // 16) * 2
            self.buf = 0; self.nb = 0
            self.R = list(struct.unpack('<3I', self.d[self.pos:self.pos + 12]))
            self.pos += 12
        else:
            raise LZXError('bad block type %d' % bt)

    def decode_frame(self, data, out_len):
        """Decode one chunk yielding out_len bytes (state persists across calls)."""
        self._init_bits(data)
        need = self.emitted + out_len
        hist = self.hist
        while len(hist) < need:
            if self.block_rem == 0:
                self._block_header()
                continue
            if self.block_type == 3:
                n = min(self.block_rem, need - len(hist))
                hist += self.d[self.pos:self.pos + n]
                self.pos += n
                self.block_rem -= n
                if self.block_rem == 0 and n & 1:
                    self.pos += 1
                continue
            main, lenh = self.main, self.lenh
            aligned = self.block_type == 2
            R = self.R
            while self.block_rem > 0 and len(hist) < need:
                m = self.sym(main)
                if m < 256:
                    hist.append(m)
                    self.block_rem -= 1
                    continue
                m -= 256
                mlen = m & 7
                slot = m >> 3
                if mlen == 7:
                    mlen += self.sym(lenh)
                mlen += 2
                if slot > 2:
                    eb = EXTRA[slot]
                    if aligned and eb >= 3:
                        v = self.bits(eb - 3) << 3
                        v += self.sym(self.aligned)
                    else:
                        v = self.bits(eb)
                    off = BASE[slot] - 2 + v
                    R[2] = R[1]; R[1] = R[0]; R[0] = off
                elif slot == 0:
                    off = R[0]
                elif slot == 1:
                    R[0], R[1] = R[1], R[0]; off = R[0]
                else:
                    R[0], R[2] = R[2], R[0]; off = R[0]
                if off > len(hist) or off <= 0:
                    raise LZXError('bad match offset %d at %d' % (off, len(hist)))
                src = len(hist) - off
                if off >= mlen:
                    hist += hist[src:src + mlen]
                else:
                    for i in range(mlen):
                        hist.append(hist[src + i])
                self.block_rem -= mlen
            if self.block_rem < 0:
                raise LZXError('block overrun')
        start = self.emitted
        self.emitted = need
        return bytes(hist[start:need])


def decompress_frames(data, wbits, out_size=None):
    """XMemCompress-style frames: 'FF uu uu cc cc' or 'cc cc' (uu=0x8000 implied)."""
    d = LZX(wbits)
    pos = 0
    out = bytearray()
    while pos < len(data) and (out_size is None or len(out) < out_size):
        if data[pos] == 0xFF:
            uu, cc = struct.unpack('>HH', data[pos + 1:pos + 5])
            pos += 5
        else:
            uu = 0x8000
            cc = struct.unpack('>H', data[pos:pos + 2])[0]
            pos += 2
        if cc == 0:
            break
        out += d.decode_frame(data[pos:pos + cc], uu)
        pos += cc
    return bytes(out)
