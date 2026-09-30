"""WAV -> MCLA music track file.

  encode_track(name, wav, encoder_exe, quality=80, ffmpeg=None, workdir=None) -> bytes   (runs xmaencode2008.exe)
  track_from_xma(name, left_xma, right_xma, total_samples) -> bytes                      (no encoder needed)

The Microsoft XMAENCODE tool (2007/2008) is started with:  xmaencode.exe "<mono wav>" /Q <level> /T "<out.xma>"
(one mono file per channel: the game stores the left and right channel as two independent XMA1 streams).
"""
import os
import shutil
import struct
import subprocess
import tempfile
import wave

import xma_track

XMA1_TAG = 0x0165


class XmaError(Exception):
    pass


def read_xma1(path):
    """RIFF XMA1 file -> (list of 2 KB packets, info dict)."""
    d = open(path, 'rb').read()
    if d[:4] != b'RIFF' or d[8:12] != b'WAVE':
        raise XmaError('%s is not a RIFF file' % path)
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(d):
        cid, size = d[pos:pos + 4], struct.unpack('<I', d[pos + 4:pos + 8])[0]
        body = d[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            fmt = body
        elif cid == b'data':
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise XmaError('fmt/data chunk missing in ' + path)
    tag, bits, opts, skip, nstreams, loop, ver = struct.unpack('<HHHHHBB', fmt[:12])
    if tag != XMA1_TAG:
        raise XmaError('%s is not XMA1 (format tag 0x%04x); do not use /B, /P or /S' % (path, tag))
    rate, chans = struct.unpack('<I', fmt[16:20])[0], fmt[29]
    if nstreams != 1 or chans != 1:
        raise XmaError('expected a mono single-stream file, got %d stream(s), %d channel(s)' % (nstreams, chans))
    if len(data) % 2048:
        raise XmaError('data size %d is not a multiple of 2048' % len(data))
    pks = [data[i:i + 2048] for i in range(0, len(data), 2048)]
    return pks, {'rate': rate, 'loop_count': loop, 'version': ver, 'packets': len(pks)}


def track_from_xma(name, left_xma, right_xma, total_samples, **kw):
    lp, li = read_xma1(left_xma)
    rp, ri = read_xma1(right_xma)
    if li['rate'] != 44100 or ri['rate'] != 44100:
        raise XmaError('sample rate must be 44100 Hz')
    for i in (li, ri):
        if i['loop_count']:
            raise XmaError('the file was encoded with looping (/L); encode without it')
    return xma_track.build_track(name, lp, rp, total_samples, **kw)


def split_to_mono_wavs(src, outdir, ffmpeg):
    """Any audio file -> 44.1 kHz 16-bit left.wav / right.wav (mono); returns (left, right, sample_count)."""
    left, right = os.path.join(outdir, 'left.wav'), os.path.join(outdir, 'right.wav')
    r = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', src, '-ar', '44100', '-ac', '2', '-c:a', 'pcm_s16le',
                        os.path.join(outdir, 'stereo.wav')], capture_output=True, text=True)
    if r.returncode:
        raise XmaError('ffmpeg failed: ' + r.stderr.strip())
    for ch, dst in ((0, left), (1, right)):
        r = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', os.path.join(outdir, 'stereo.wav'), '-af',
                            'pan=mono|c0=c%d' % ch, '-c:a', 'pcm_s16le', dst], capture_output=True, text=True)
        if r.returncode:
            raise XmaError('ffmpeg failed: ' + r.stderr.strip())
    with wave.open(left) as w:
        n = w.getnframes()
    return left, right, n


RUNNER = []          # program the encoder is started through, e.g. ['wine'] on macOS / Linux (it is a Windows program)


def _arg_path(path):
    """A path as the encoder sees it. Under Wine a Unix path like /tmp/x.wav would be taken for a /T-style switch, so it is
    given as Z:\\tmp\\x.wav (in the default Wine setup drive Z: is the Unix root)."""
    return 'Z:' + os.path.abspath(path).replace('/', '\\') if RUNNER else path


def run_encoder(exe, wav, out_xma, quality):
    if os.path.exists(out_xma):
        os.remove(out_xma)                              # the tool asks "overwrite? (y/n)" otherwise
    env = dict(os.environ, WINEDEBUG='-all') if RUNNER else None
    r = subprocess.run(RUNNER + [exe, _arg_path(wav), '/Q', str(quality), '/T', _arg_path(out_xma)], capture_output=True,
                       text=True, errors='replace', stdin=subprocess.DEVNULL, cwd=os.path.dirname(os.path.abspath(wav)),
                       env=env)
    if not os.path.exists(out_xma):
        raise XmaError('XMAENCODE produced no output.\n' + (r.stdout + r.stderr).strip())
    return r


def silence_packets(encoder_exe, ffmpeg, seconds=1.0):
    """XMA1 packets of `seconds` of mono silence (encoded once; the same packets serve every silent stub track)
    -> (packets, sample count)"""
    wd = tempfile.mkdtemp(prefix='mcla_silence_')
    try:
        wav = os.path.join(wd, 'silence.wav')
        r = subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t',
                            '%.3f' % seconds, '-c:a', 'pcm_s16le', wav], capture_output=True, text=True)
        if r.returncode:
            raise XmaError('ffmpeg failed: ' + r.stderr.strip())
        with wave.open(wav) as w:
            n = w.getnframes()
        out = os.path.join(wd, 'silence.xma')
        run_encoder(encoder_exe, wav, out, 80)
        return read_xma1(out)[0], n
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def encode_track(name, src_audio, encoder_exe, quality=80, ffmpeg=None, workdir=None, progress=lambda m: None,
                 channel_ids=None):
    ffmpeg = ffmpeg or shutil.which('ffmpeg')
    if not ffmpeg:
        raise XmaError('ffmpeg not found')
    if not os.path.isfile(encoder_exe):
        raise XmaError('XMA encoder not found: %s' % encoder_exe)
    wd = workdir or tempfile.mkdtemp(prefix='mcla_xma_')
    try:
        progress('Converting to 44.1 kHz mono channels...')
        left, right, n = split_to_mono_wavs(src_audio, wd, ffmpeg)
        xmas = {}
        for ch, wav in (('L', left), ('R', right)):
            progress('Encoding %s channel (quality %d)...' % ('left' if ch == 'L' else 'right', quality))
            xmas[ch] = os.path.join(wd, ch + '.xma')
            run_encoder(encoder_exe, wav, xmas[ch], quality)
        progress('Building the track file...')
        return track_from_xma(name, xmas['L'], xmas['R'], n, channel_ids=channel_ids), n
    finally:
        if not workdir:
            shutil.rmtree(wd, ignore_errors=True)
