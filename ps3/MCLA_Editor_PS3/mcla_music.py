"""Decode Midnight Club LA (Xbox 360) music stream files (from xarchive_music.rpf) to WAV/FLAC/MP3.

  py mcla_music.py info    <file|dir>
  py mcla_music.py convert <file|dir> [-o OUTDIR] [-f wav|flac|mp3] [-j JOBS]

File layout (all big-endian):
  0x00 header: w11 = offset of first block, w23 = stream data size, w24 = total samples,
               w26>>16 = sample rate
  blocks of 0x20000 from w11: 0x800 header (w7 = packets of channel L, w10/w11 = packets
  of L/R, w9 = samples...) followed by the L packets, then the R packets (2 KB XMA2 packets,
  mono each). The last packet of a channel in a block is repeated as the first packet of
  the same channel in the next block; the copy is dropped when re-assembling.
Requires ffmpeg (xma2 decoder).
"""
import argparse
import concurrent.futures as cf
import os
import struct
import subprocess
import sys
import tempfile

PK = 0x800
BLOCK = 0x20000
SIG = bytes.fromhex('00000000000000180000000000000038')
FFMPEG = os.environ.get('FFMPEG') or 'ffmpeg'


class Track:
    def __init__(self, data):
        w = struct.unpack('>32I', data[:0x80])
        self.first_block = w[11]
        self.id_first = w[14]                    # joaat(<wave name> + '_LEFT' | '_RIGHT') of the first stream
        self.data_size = w[23]
        self.samples = w[24]
        self.rate = w[26] >> 16
        self.channels = w[9]
        if self.channels != 2 or data[self.first_block:self.first_block + 16] != SIG:
            raise ValueError('unsupported layout (channels=%d)' % self.channels)
        self.streams = [bytearray(), bytearray()]
        prev = [None, None]
        pos = self.first_block
        self.blocks = 0
        while data[pos:pos + 16] == SIG:
            h = struct.unpack('>12I', data[pos:pos + 48])
            na, nb = h[10], h[11]
            base = pos + 0x800
            for s, (cnt, off) in enumerate(((na, base), (nb, base + na * PK))):
                pk = [data[off + i * PK:off + (i + 1) * PK] for i in range(cnt)]
                if pk and prev[s] is not None and pk[0][0] >> 4 == prev[s][0] >> 4 and pk[0] == prev[s]:
                    pk = pk[1:]
                for x in pk:
                    self.streams[s] += x
                if cnt:
                    prev[s] = data[off + (cnt - 1) * PK:off + cnt * PK]
            pos += BLOCK
            self.blocks += 1

    def riff(self, i):
        data = bytes(self.streams[i])
        fmt = struct.pack('<HHIIHHH', 0x166, 1, self.rate, 16000, PK, 16, 34)
        fmt += struct.pack('<HIIIIIIIBBH', 1, 4, self.samples, PK, 0, self.samples, 0, 0, 0, 4, len(data) // PK)
        body = b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(data)) + data
        return b'RIFF' + struct.pack('<I', len(body)) + body


def convert(src, dst, fmt='wav', name=None):
    """name = wave name of the song (e.g. 'NAS_SLYFOX'); the stream ids tell which stream is the left channel.
    Without it the file name is used, and if that does not match the streams are taken as left, right."""
    import rpf3
    t = Track(open(src, 'rb').read())
    stem = name or os.path.splitext(os.path.basename(src))[0]
    order = (1, 0) if t.id_first == rpf3.joaat(stem + '_RIGHT') else (0, 1)
    with tempfile.TemporaryDirectory() as td:
        ins = []
        for i in order:
            p = os.path.join(td, 's%d.xma' % i)
            open(p, 'wb').write(t.riff(i))
            ins += ['-i', p]
        codec = {'wav': ['-c:a', 'pcm_s16le'], 'flac': ['-c:a', 'flac'], 'mp3': ['-c:a', 'libmp3lame', '-q:a', '0']}[fmt]
        cmd = [FFMPEG, '-hide_banner', '-v', 'error', '-y'] + ins + [
            '-filter_complex', '[0:a][1:a]join=inputs=2:channel_layout=stereo,atrim=end_sample=%d' % t.samples] + codec + [dst]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode or r.stderr.strip():
            raise RuntimeError(r.stderr.strip() or 'ffmpeg failed')
    return t


def files_in(path):
    if os.path.isdir(path):
        return sorted(os.path.join(dp, f) for dp, _, fs in os.walk(path) for f in fs if f.lower().endswith(('.bin', '.dat')))
    return [path]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('info', 'convert'))
    ap.add_argument('path')
    ap.add_argument('-o', '--out', default='out_wav')
    ap.add_argument('-f', '--format', default='wav', choices=('wav', 'flac', 'mp3'))
    ap.add_argument('-j', '--jobs', type=int, default=4)
    a = ap.parse_args()
    files = files_in(a.path)
    if a.cmd == 'info':
        for f in files:
            try:
                t = Track(open(f, 'rb').read())
                print('%-16s %6.1fs %5d Hz blocks=%d L=%d R=%d %s' % (
                    os.path.basename(f), t.samples / t.rate, t.rate, t.blocks, len(t.streams[0]), len(t.streams[1]),
                    '' if len(t.streams[0]) == t.data_size else '(L size != header %d)' % t.data_size))
            except Exception as e:
                print('%-16s ERROR %s' % (os.path.basename(f), e))
        return
    os.makedirs(a.out, exist_ok=True)

    def job(f):
        name = os.path.splitext(os.path.basename(f))[0]
        try:
            t = convert(f, os.path.join(a.out, name + '.' + a.format), a.format)
            return name, '%.1fs' % (t.samples / t.rate), None
        except Exception as e:
            return name, None, e

    bad = 0
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for name, dur, err in ex.map(job, files):
            print(name, dur if not err else 'FAILED: %s' % err, flush=True)
            bad += bool(err)
    print('done, %d files, %d failed' % (len(files), bad))


if __name__ == '__main__':
    main()
