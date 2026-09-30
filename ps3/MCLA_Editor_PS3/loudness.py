"""Loudness (EBU R128) measuring and normalizing with ffmpeg - pure standard library.

The level of the game: the 106 songs of the original Midnight Club: LA soundtrack measure -8.1 LUFS (median; -10.6 ... -5.9,
loudness range ~2.4 LU, true peaks around 0 dBTP). GENRE_LUFS are the medians of each genre.
normalize() applies a plain gain when the peaks allow it, else the gain plus a 4x oversampled limiter (ceiling in dBTP).
"""
import os
import re
import subprocess
import tempfile

GAME_LUFS = -8.1
GENRE_LUFS = {'ECLECTIC': -8.4, 'ELECTRONIC': -8.2, 'HARD_ROCK': -7.3, 'HIPHOP': -8.6, 'ROCK': -7.5, 'TECHNO': -9.1,
              'WESTCOASTRAP': -8.3}
DEFAULT_TARGET = -8.0
DEFAULT_CEILING = -1.0


class LoudnessError(Exception):
    pass


class Cancelled(Exception):
    """the user pressed Stop"""


def _summary(stderr):
    s = stderr[stderr.rfind('Summary:'):]
    try:
        g = lambda rx: float(re.search(rx, s).group(1))
        return {'lufs': g(r'I:\s+(-?[\d.]+) LUFS'), 'lra': g(r'LRA:\s+(-?[\d.]+) LU'),
                'peak': g(r'Peak:\s+(-?(?:[\d.]+|inf)) dBFS')}
    except (AttributeError, ValueError):
        raise LoudnessError('could not measure: ' + stderr.strip()[-300:])


def measure_file(ffmpeg, path):
    """{'lufs', 'peak' (true peak, dBTP), 'lra'} of any file ffmpeg can read"""
    r = subprocess.run([ffmpeg, '-hide_banner', '-nostats', '-i', path, '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace')
    if r.returncode:
        raise LoudnessError('ffmpeg could not read %s: %s' % (os.path.basename(path), r.stderr.strip()[-300:]))
    return _summary(r.stderr)


def measure_track(ffmpeg, data):
    """the same for a music track file of the game (the bytes of an entry of carchive_music.rpf / carchive_audio.rpf):
    the two MP3 streams are decoded to a temporary WAV first (the channel order does not change the loudness)"""
    import mp3_track
    total = mp3_track.parse(data)
    with tempfile.TemporaryDirectory(prefix='mcla_ps3_loud_') as td:
        wav = decode_track(ffmpeg, data, os.path.join(td, 't.wav'))
        out = measure_file(ffmpeg, wav)
    out['sec'] = total['total'] / float(total.get('rate') or 44100)
    return out


def decode_track(ffmpeg, data, wav, name=None):
    """a music track file of the game -> WAV; name = wave name or (left id, right id): which stream is the left channel"""
    import mp3_encode
    import rpf3
    ids = tuple(name) if isinstance(name, (tuple, list)) else \
        ((rpf3.joaat(name + '_LEFT'), rpf3.joaat(name + '_RIGHT')) if name else None)
    try:
        mp3_encode.decode_track(ffmpeg, data, wav, ids)
    except (RuntimeError, AssertionError, ValueError) as e:
        raise LoudnessError('cannot decode the track: %s' % e)
    return wav


def normalize(ffmpeg, src, dst, target=DEFAULT_TARGET, ceiling=DEFAULT_CEILING, measured=None, cancel=None):
    """src (any format) -> dst (44.1 kHz, 16 bit, stereo) at `target` LUFS with true peaks <= `ceiling`; dst ending in
    .flac is FLAC (tags incl. non-Latin ones and the cover picture kept), else WAV (tags kept, no cover).
    Returns {'gain', 'limited', 'before', 'after'}. When the limiter is needed it takes a little loudness away, so the gain
    is corrected a few times until the result is within 0.3 LU of the target. cancel: threading.Event, checked between
    the passes."""
    import audio_meta
    flac = dst.lower().endswith('.flac')
    # ogg / opus carry their tags on the audio stream: then those become the tags of the new file
    g = audio_meta.read_tags(ffmpeg, src, stream_fallback=False)
    meta = ['-map_metadata', '0' if (g.get('artist') or g.get('title')) else '0:s:a:0']
    maps = ['-map', '0:a:0'] + (['-map', '0:v:0?', '-c:v', 'copy', '-disposition:v:0', 'attached_pic'] if flac else [])
    codec = ['-c:a', 'flac', '-sample_fmt', 's16'] if flac else ['-c:a', 'pcm_s16le']
    before = measured or measure_file(ffmpeg, src)
    gain = target - before['lufs']
    lim = 10 ** (ceiling / 20.0)
    after, limited = None, False
    for _ in range(5):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        limited = before['peak'] + gain > ceiling
        if limited:
            af = ('aresample=176400,volume=%.2fdB,alimiter=limit=%.4f:attack=5:release=60:level=0:latency=1,aresample=44100'
                  % (gain, lim))
        else:
            af = 'volume=%.2fdB' % gain
        r = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', src] + maps + meta + ['-af', af, '-ar', '44100', '-ac', '2']
                           + codec + [dst], capture_output=True, text=True, errors='replace')
        if r.returncode or not os.path.isfile(dst):
            raise LoudnessError('ffmpeg failed on %s: %s' % (os.path.basename(src), r.stderr.strip()[-300:]))
        after = measure_file(ffmpeg, dst)
        if not limited or abs(after['lufs'] - target) <= 0.3 or gain > 18:
            break
        gain += target - after['lufs']
    return {'gain': gain, 'limited': limited, 'before': before, 'after': after}

HANGOUT_LUFS = -15.5          # the 6 original hangout music clips: -12.5...-17.5 LUFS, median -15.4 (both platforms)


def excerpt(ffmpeg, src, dst, start, length, rate, target=None, ceiling=DEFAULT_CEILING):
    """a mono excerpt of any audio file (both channels mixed) -> dst WAV (16 bit, `rate` Hz); with `target` (LUFS) it is
    brought to that loudness, true peaks <= `ceiling` (limiter when needed, corrected like normalize()).
    Returns {'before', 'after', 'gain'} (None values without target)."""
    cut = ['-ss', '%.3f' % start, '-t', '%.3f' % length, '-i', src]
    r = subprocess.run([ffmpeg, '-y', '-v', 'error'] + cut + ['-ac', '1', '-ar', str(rate), '-c:a', 'pcm_s16le', dst],
                       capture_output=True, text=True, errors='replace')
    if r.returncode or not os.path.isfile(dst):
        raise LoudnessError('ffmpeg failed on %s: %s' % (os.path.basename(src), r.stderr.strip()[-300:]))
    if target is None:
        return {'before': None, 'after': None, 'gain': None}
    before = measure_file(ffmpeg, dst)
    if before['lufs'] < -60:                                   # (near) silence: nothing to bring up
        return {'before': before, 'after': before, 'gain': 0.0}
    gain = target - before['lufs']
    lim = 10 ** (ceiling / 20.0)
    after = None
    for _ in range(5):
        limited = before['peak'] + gain > ceiling
        af = ('aresample=%d,volume=%.2fdB,alimiter=limit=%.4f:attack=5:release=60:level=0:latency=1,aresample=%d'
              % (rate * 4, gain, lim, rate)) if limited else 'volume=%.2fdB' % gain
        r = subprocess.run([ffmpeg, '-y', '-v', 'error'] + cut + ['-af', af, '-ac', '1', '-ar', str(rate), '-c:a',
                            'pcm_s16le', dst], capture_output=True, text=True, errors='replace')
        if r.returncode or not os.path.isfile(dst):
            raise LoudnessError('ffmpeg failed on %s: %s' % (os.path.basename(src), r.stderr.strip()[-300:]))
        after = measure_file(ffmpeg, dst)
        if not limited or abs(after['lufs'] - target) <= 0.3 or gain > 18:
            break
        gain += target - after['lufs']
    return {'before': before, 'after': after, 'gain': gain}
