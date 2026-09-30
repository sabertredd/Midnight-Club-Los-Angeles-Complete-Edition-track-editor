"""End-of-race clips: automatic excerpt of a song, or a user-chosen file, prepared as a stereo 44.1 kHz WAV for the encoder.

The original clips are 10-51 s long (median 21 s), cut by hand from the song. The automatic choice is the loudest
stretch of the song (default 20 s), taken from the middle / final part, never from the very beginning or the last 3 s.
Pure standard library; ffmpeg does all the decoding.
"""
import array
import os
import subprocess

DEFAULT_SECONDS = 20.0
MAX_USER_SECONDS = 60.0
FADE_IN = 0.15                 # short fade-in to avoid a click on the cut-in
FADE_OUT = 1.5                 # fade-out at the tail - only for a brand-new song's clip (see make_wav's fade_out);
                                # an EXISTING song's clip loops in the game until the player presses A, and fading to
                                # silence at the tail made every loop dip audibly, so that case skips it
RATE = 4000                    # analysis rate (mono)
STEP = 0.5                     # analysis window in seconds


def _pcm(ffmpeg, path):
    r = subprocess.run([ffmpeg, '-v', 'error', '-i', path, '-f', 's16le', '-ac', '1', '-ar', str(RATE), '-'], capture_output=True)
    if r.returncode or not r.stdout:
        raise ValueError('ffmpeg could not read %s: %s' % (path, r.stderr.decode('utf-8', 'replace').strip()[-200:]))
    a = array.array('h')
    a.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    return a


def auto_excerpt(ffmpeg, path, seconds=DEFAULT_SECONDS):
    """-> (start seconds, length seconds): the loudest `seconds` of the song (whole song when it is that short)."""
    a = _pcm(ffmpeg, path)
    dur = len(a) / RATE
    if dur <= seconds + 1.0:
        return 0.0, dur
    n = int(RATE * STEP)
    e = [sum(x * x for x in a[i:i + n]) / n for i in range(0, len(a) - n + 1, n)]
    pre = [0.0]
    for v in e:
        pre.append(pre[-1] + v)
    w = int(seconds / STEP)
    lo = int(max(dur * 0.08, 3.0) / STEP)                 # not the very beginning ...
    hi = len(e) - w - int(3.0 / STEP)                      # ... and not the last 3 seconds
    if hi < lo:
        lo, hi = 0, len(e) - w
    best = max(range(lo, hi + 1), key=lambda i: pre[i + w] - pre[i])
    return best * STEP, seconds


def make_wav(ffmpeg, src, out_wav, start=None, length=None, user_clip=False, fade_out=False):
    """Write the clip as 44.1 kHz stereo WAV. user_clip: a file the user chose - used as it is (at most MAX_USER_SECONDS);
    otherwise `start`/`length` select the excerpt, with a short fade-in and (only when `fade_out`) a fade-out."""
    cmd = [ffmpeg, '-y', '-v', 'error']
    if user_clip:
        cmd += ['-i', src, '-t', '%.2f' % MAX_USER_SECONDS]
    else:
        af = 'afade=t=in:st=0:d=%.2f' % min(FADE_IN, length)
        if fade_out:
            af += ',afade=t=out:st=%.2f:d=%.2f' % (max(0.0, length - FADE_OUT), min(FADE_OUT, length))
        cmd += ['-ss', '%.2f' % start, '-i', src, '-t', '%.2f' % length, '-af', af]
    cmd += ['-ar', '44100', '-ac', '2', out_wav]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not os.path.isfile(out_wav):
        raise ValueError('ffmpeg failed on %s: %s' % (src, r.stderr.strip()[-200:]))
    return out_wav
