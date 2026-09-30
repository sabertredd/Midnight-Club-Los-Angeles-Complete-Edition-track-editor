"""Minimal pure-Python AES (decrypt + encrypt), 128/256-bit keys, ECB/CBC helpers."""

def _build():
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) & 0xFF ^ ((q << 2) | (q >> 6)) & 0xFF \
            ^ ((q << 3) | (q >> 5)) & 0xFF ^ ((q << 4) | (q >> 4)) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV = _build()


def _xt(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


M9 = [_mul(i, 9) for i in range(256)]
M11 = [_mul(i, 11) for i in range(256)]
M13 = [_mul(i, 13) for i in range(256)]
M14 = [_mul(i, 14) for i in range(256)]
M2 = [_mul(i, 2) for i in range(256)]
M3 = [_mul(i, 3) for i in range(256)]


def expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xt(rcon)
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    rks = []
    for r in range(nr + 1):
        rk = []
        for c in range(4):
            rk += w[4 * r + c]
        rks.append(rk)
    return rks


def decrypt_block(rks, blk):
    nr = len(rks) - 1
    s = [blk[i] ^ rks[nr][i] for i in range(16)]
    for r in range(nr - 1, -1, -1):
        # inv shift rows
        s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
             s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
        s = [INV[b] for b in s]
        s = [s[i] ^ rks[r][i] for i in range(16)]
        if r:
            o = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                o += [M14[a0] ^ M11[a1] ^ M13[a2] ^ M9[a3],
                      M9[a0] ^ M14[a1] ^ M11[a2] ^ M13[a3],
                      M13[a0] ^ M9[a1] ^ M14[a2] ^ M11[a3],
                      M11[a0] ^ M13[a1] ^ M9[a2] ^ M14[a3]]
            s = o
    return bytes(s)


def encrypt_block(rks, blk):
    nr = len(rks) - 1
    s = [blk[i] ^ rks[0][i] for i in range(16)]
    for r in range(1, nr + 1):
        s = [SBOX[b] for b in s]
        s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
             s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
        if r != nr:
            o = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                o += [M2[a0] ^ M3[a1] ^ a2 ^ a3,
                      a0 ^ M2[a1] ^ M3[a2] ^ a3,
                      a0 ^ a1 ^ M2[a2] ^ M3[a3],
                      M3[a0] ^ a1 ^ a2 ^ M2[a3]]
            s = o
        s = [s[i] ^ rks[r][i] for i in range(16)]
    return bytes(s)


def _ok(status):
    if status:
        raise OSError('CNG error 0x%08x' % (status & 0xFFFFFFFF))


_PROVIDER = []


def _provider():
    """(ctypes, bcrypt.dll, AES-ECB algorithm handle) of Windows CNG, opened once; OSError / AttributeError elsewhere"""
    if not _PROVIDER:
        import ctypes
        from ctypes import wintypes
        bc = ctypes.WinDLL('bcrypt')
        H, P, U = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.ULONG
        bc.BCryptOpenAlgorithmProvider.argtypes = [P, wintypes.LPCWSTR, wintypes.LPCWSTR, U]
        bc.BCryptSetProperty.argtypes = [H, wintypes.LPCWSTR, ctypes.c_void_p, U, U]
        bc.BCryptGenerateSymmetricKey.argtypes = [H, P, ctypes.c_void_p, U, ctypes.c_char_p, U, U]
        for f in (bc.BCryptEncrypt, bc.BCryptDecrypt):
            f.argtypes = [H, ctypes.c_void_p, U, ctypes.c_void_p, ctypes.c_void_p, U, ctypes.c_void_p, U,
                          ctypes.POINTER(U), U]
        bc.BCryptDestroyKey.argtypes = [H]
        alg = H()
        _ok(bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'AES', None, 0))
        mode = ctypes.create_unicode_buffer('ChainingModeECB')
        _ok(bc.BCryptSetProperty(alg, 'ChainingMode', mode, ctypes.sizeof(mode), 0))
        _PROVIDER.append((ctypes, bc, alg))
    return _PROVIDER[0]


class _Cng:
    """AES-ECB through Windows CNG (bcrypt.dll): the same result as the pure-Python code, ~1000x faster"""
    def __init__(self, key):
        self.ct, self.bc, alg = _provider()
        self.key = self.ct.c_void_p()
        _ok(self.bc.BCryptGenerateSymmetricKey(alg, self.ct.byref(self.key), None, 0, key, len(key), 0))

    def run(self, data, rounds, encrypt):
        ct, n = self.ct, len(data)
        a, b = ct.create_string_buffer(bytes(data), n), ct.create_string_buffer(n)
        got = ct.c_ulong()
        f = self.bc.BCryptEncrypt if encrypt else self.bc.BCryptDecrypt
        for _ in range(rounds):
            _ok(f(self.key, a, n, None, None, 0, b, n, ct.byref(got), 0))
            a, b = b, a
        return a.raw

    def __del__(self):
        try:
            self.bc.BCryptDestroyKey(self.key)
        except Exception:
            pass


_CNG = {}


def ecb_rounds(key, data, rounds=1, encrypt=False, cache=True):
    """AES-ECB applied `rounds` times to every 16-byte block of data (length a multiple of 16).
    cache=False: do not keep the key object (for trying many candidate keys)."""
    key = bytes(key)
    eng = _CNG.get(key, 0)
    if eng == 0:
        try:
            eng = _Cng(key)
        except (OSError, AttributeError, ImportError):  # not Windows / no CNG: pure Python
            eng = None
        if cache:
            _CNG[key] = eng
    if eng is not None:
        return eng.run(data, rounds, encrypt)
    rks = expand_key(key)
    f = encrypt_block if encrypt else decrypt_block
    out = bytearray()
    for i in range(0, len(data) - 15, 16):
        x = data[i:i + 16]
        for _ in range(rounds):
            x = f(rks, x)
        out += x
    return bytes(out)


def ecb_decrypt(key, data):
    rks = expand_key(key)
    return b''.join(decrypt_block(rks, data[i:i + 16]) for i in range(0, len(data) - 15, 16))


def cbc_decrypt(key, iv, data):
    rks = expand_key(key)
    out = []
    prev = iv
    for i in range(0, len(data) - 15, 16):
        blk = data[i:i + 16]
        d = decrypt_block(rks, blk)
        out.append(bytes(a ^ b for a, b in zip(d, prev)))
        prev = blk
    return b''.join(out)


if __name__ == '__main__':
    # FIPS-197 test vectors
    k = bytes(range(16)); pt = bytes.fromhex('00112233445566778899aabbccddeeff')
    assert encrypt_block(expand_key(k), pt).hex() == '69c4e0d86a7b0430d8cdb78070b4c55a'
    assert decrypt_block(expand_key(k), bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')) == pt
    k = bytes(range(32))
    assert encrypt_block(expand_key(k), pt).hex() == '8ea2b7ca516745bfeafc49904b496089'
    assert decrypt_block(expand_key(k), bytes.fromhex('8ea2b7ca516745bfeafc49904b496089')) == pt
    assert ecb_rounds(k, pt, 1, True).hex() == '8ea2b7ca516745bfeafc49904b496089'
    assert ecb_rounds(k, bytes.fromhex('8ea2b7ca516745bfeafc49904b496089')) == pt
    print('AES self-test OK', '(CNG)' if _CNG.get(k) else '(pure Python)')
