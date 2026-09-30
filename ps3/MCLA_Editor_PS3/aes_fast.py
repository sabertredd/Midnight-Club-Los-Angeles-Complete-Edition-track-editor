"""AES-256 ECB over whole buffers, repeated `rounds` times (the RPF3 TOC cipher: 16 x AES per 16-byte block).

Uses the AES of Windows (BCrypt, through ctypes - standard library only): the whole buffer in one call per round, about a
thousand times faster than aes_pure. Anywhere else, or if BCrypt fails, the pure-Python aes_pure is used (same result).
"""
import ctypes
import sys

_ctx = {}


def _bcrypt_key(key):
    """(bcrypt dll, key handle) for this key, created once; None when BCrypt is not available"""
    if key in _ctx:
        return _ctx[key]
    res = None
    if sys.platform == 'win32':
        try:
            bc = ctypes.WinDLL('bcrypt.dll')
            alg, hkey = ctypes.c_void_p(), ctypes.c_void_p()
            ok = bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), ctypes.c_wchar_p('AES'), None, 0) == 0
            mode = ctypes.create_unicode_buffer('ChainingModeECB')
            ok = ok and bc.BCryptSetProperty(alg, ctypes.c_wchar_p('ChainingMode'), mode,
                                             ctypes.sizeof(mode), 0) == 0
            kb = ctypes.create_string_buffer(key, len(key))
            ok = ok and bc.BCryptGenerateSymmetricKey(alg, ctypes.byref(hkey), None, 0, kb, len(key), 0) == 0
            if ok:
                res = (bc, hkey, alg)
        except (OSError, AttributeError):
            res = None
    _ctx[key] = res
    return res


def _bcrypt_run(key, data, rounds, decrypt):
    k = _bcrypt_key(key)
    if k is None:
        return None
    bc, hkey, _ = k
    fn = bc.BCryptDecrypt if decrypt else bc.BCryptEncrypt
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    out = ctypes.create_string_buffer(len(data))
    n = ctypes.c_ulong()
    for _ in range(rounds):
        if fn(hkey, buf, len(data), None, None, 0, out, len(data), ctypes.byref(n), 0) != 0 or n.value != len(data):
            return None
        buf, out = out, buf
    return buf.raw


def _pure_run(key, data, rounds, decrypt):
    import aes_pure
    rks = aes_pure.expand_key(key)
    f = aes_pure.decrypt_block if decrypt else aes_pure.encrypt_block
    out = bytearray()
    for i in range(0, len(data), 16):
        x = data[i:i + 16]
        for _ in range(rounds):
            x = f(rks, x)
        out += x
    return bytes(out)


def ecb(key, data, rounds=1, decrypt=True):
    """data: a multiple of 16 bytes"""
    if len(data) % 16:
        raise ValueError('AES-ECB needs a multiple of 16 bytes')
    if not data:
        return b''
    return _bcrypt_run(key, data, rounds, decrypt) or _pure_run(key, data, rounds, decrypt)
