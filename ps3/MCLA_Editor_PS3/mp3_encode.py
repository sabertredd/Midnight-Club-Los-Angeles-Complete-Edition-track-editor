"""Encode audio into a PS3 MCLA music track (two mono MP3 streams without bit reservoir) and decode tracks back.

  encode_track(ffmpeg, src, channel_ids, quality) -> track bytes
  decode_track(ffmpeg, track_bytes, wav_path, channel_ids=None)
"""
import os
import subprocess
import tempfile

import mp3_track as M

NOWIN = 0x08000000 if os.name == 'nt' else 0


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, creationflags=NOWIN)
    if p.returncode:
        raise RuntimeError('ffmpeg failed: %s' % p.stderr.decode('utf8', 'replace')[-600:])
    return p


def mdb(f):
    """main_data_begin of a frame (MPEG-1, no CRC): 0 = the frame does not use the bit reservoir."""
    return (f[4] << 1) | (f[5] >> 7)


def encode_channels(ffmpeg, src, quality=4, seconds=None):
    """-> [left frames, right frames]; mono MPEG-1 Layer III 44.1 kHz VBR, no reservoir, no Xing/ID3.
    One ffmpeg run: the source is read and decoded once, split into the two channels and encoded to two files."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        dsts = [os.path.join(td, ch + '.mp3') for ch in ('FL', 'FR')]
        enc = ['-ac', '1', '-ar', '44100', '-c:a', 'libmp3lame', '-q:a', str(quality), '-reservoir', '0',
               '-write_xing', '0', '-id3v2_version', '0', '-write_id3v1', '0', '-f', 'mp3']

        def cmd(right_channel):
            c = [ffmpeg, '-v', 'error', '-y', '-i', src]
            if seconds:
                c += ['-t', str(seconds)]
            return c + ['-filter_complex', '[0:a]aresample=44100,asplit=2[a][b];[a]pan=mono|c0=c0[L];[b]pan=mono|c0=%s[R]'
                        % right_channel, '-map', '[L]'] + enc + [dsts[0], '-map', '[R]'] + enc + [dsts[1]]
        try:
            _run(cmd('c1'))
        except RuntimeError:
            _run(cmd('c0'))                     # a mono source has no c1: it is used for both channels
        for dst in dsts:
            d = open(dst, 'rb').read()
            i = d.find(b'\xff\xfb')
            frames, end = M.split_frames(d, i)
            if any(mdb(f) for f in frames):
                raise RuntimeError('encoder used the bit reservoir')
            if any(f[3] >> 6 != 3 for f in frames):
                raise RuntimeError('encoder did not produce mono frames')
            out.append(frames)
    n = min(len(out[0]), len(out[1]))
    return [out[0][:n], out[1][:n]]


def encode_track(ffmpeg, src, channel_ids, quality=4, seconds=None):
    """channel_ids = (id of the LEFT stream, id of the RIGHT stream); streams are stored in ascending id order."""
    left, right = encode_channels(ffmpeg, src, quality, seconds)
    idl, idr = channel_ids
    if idl <= idr:
        ids, frames = (idl, idr), [left, right]
    else:
        ids, frames = (idr, idl), [right, left]
    return M.build_track(ids, frames, M.SPF * len(left))


def encode_mono(ffmpeg, src, start=None, length=None, quality=3, rate=48000, mono_source=False):
    """mono (left + right) MPEG-1 Layer III frames without bit reservoir - the format of the positional ambience sounds
    (e.g. the music at the hangouts: 48 kHz). mono_source: src is mono already - taken as it is (the stereo mix below
    would halve it: ffmpeg's pan gives a missing second channel as silence)."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'm.mp3')
        cmd = [ffmpeg, '-v', 'error', '-y']
        if start:
            cmd += ['-ss', '%.3f' % start]
        if length:
            cmd += ['-t', '%.3f' % length]
        cmd += ['-i', src]
        enc = ['-ac', '1', '-ar', str(rate), '-c:a', 'libmp3lame', '-q:a', str(quality), '-reservoir', '0',
               '-write_xing', '0', '-id3v2_version', '0', '-write_id3v1', '0', '-f', 'mp3', out]
        if mono_source:
            _run(cmd + ['-af', 'aresample=%d,pan=mono|c0=c0' % rate] + enc)
        else:
            try:
                _run(cmd + ['-af', 'aresample=%d,pan=mono|c0=0.5*c0+0.5*c1' % rate] + enc)
            except RuntimeError:
                _run(cmd + ['-af', 'aresample=%d,pan=mono|c0=c0' % rate] + enc)    # mono source
        d = open(out, 'rb').read()
    frames, _ = M.split_frames(d, d.find(b'\xff\xfb'))
    if not frames or any(mdb(f) for f in frames):
        raise RuntimeError('encoder produced no frames or used the bit reservoir')
    return frames


_SILENCE = {}


def silence_track(ffmpeg, channel_ids, seconds=1.0, quality=4):
    """a short silent track (stub of a removed song / clip) with the given (LEFT id, RIGHT id); the silence is encoded
    once, every stub only gets its own stream ids"""
    key = (ffmpeg, seconds, quality)
    if key not in _SILENCE:
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, 'silence.wav')
            _run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '%.3f' % seconds,
                  '-c:a', 'pcm_s16le', wav])
            _SILENCE[key] = encode_channels(ffmpeg, wav, quality)
    left, right = _SILENCE[key]
    idl, idr = channel_ids
    ids, frames = ((idl, idr), [left, right]) if idl <= idr else ((idr, idl), [right, left])
    return M.build_track(ids, frames, M.SPF * len(left))


def decode_track(ffmpeg, data, wav, channel_ids=None):
    """Decode a track into a stereo WAV. channel_ids = (LEFT id, RIGHT id) to put the streams on the right speakers
    (default: file order)."""
    P = M.parse(data)
    order = (0, 1)
    if channel_ids and P['ids'][0] == channel_ids[1]:
        order = (1, 0)
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for k, s in enumerate(order):
            p = os.path.join(td, '%d.mp3' % k)
            with open(p, 'wb') as f:
                f.write(b''.join(P['frames'][s]))
            paths.append(p)
        _run([ffmpeg, '-v', 'error', '-y', '-i', paths[0], '-i', paths[1], '-filter_complex',
              '[0:a][1:a]join=inputs=2:channel_layout=stereo[a]', '-map', '[a]', '-t', '%.6f' % (P['total'] / float(P.get('rate') or 44100)),
              wav])
    return P
