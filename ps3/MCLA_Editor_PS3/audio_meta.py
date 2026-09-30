"""Artist / title of audio files for the 'Add songs' dialog: tags first (read with ffmpeg, any format), otherwise guessed
from the file name (junk like a leading track number or a trailing random id such as 'PEE8C5' is removed).

  read_tags(ffmpeg, path)          -> {'artist': ..., 'title': ...} (lower-case keys, may be empty)
  guess_batch(paths, ffmpeg)       -> [{'artist', 'title', 'source'}] one per path; files of one artist in the same batch
                                      help each other ('Klaus-Veen-A', 'Klaus-Veen-B' -> artist 'Klaus Veen')
"""
import os
import re
import subprocess

TRACK_NO = re.compile(r'^\s*\d{1,3}\s*[-_.)]+\s*')
# random ids appended by download sites: 5-6 upper-case letters/digits mixed, e.g. PEE8C5, D6B21O
JUNK_ID = re.compile(r'[-_ .]+(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,6}$')
STOP = {'the', 'a', 'dj', 'mc', 'lil', 'big', 'young', 'dr', 'mr'}


PROMO = re.compile(r'\s*[\(\[][^\)\]]*\b(?:www\.|[\w-]+\.(?:net|com|ru|org|info|ua|by|kz|fm))[^\)\]]*[\)\]]', re.I)


def tidy(s):
    """remove promo brackets like '(Muzpro.net)' and squeeze spaces"""
    return ' '.join(PROMO.sub('', s).split())


def read_tags(ffmpeg, path, stream_fallback=True):
    """tags of the file; ogg / opus keep them on the audio stream instead of the file, those are read when the file has
    none (stream_fallback)"""
    out = _tags(ffmpeg, path, [])
    if stream_fallback and not (out.get('artist') or out.get('title')):   # ('encoder' alone does not count)
        for k, v in _tags(ffmpeg, path, ['-map_metadata', '0:s:a:0']).items():
            out.setdefault(k, v)
    return out


def _tags(ffmpeg, path, extra):
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return {}
    try:
        r = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', path] + extra + ['-f', 'ffmetadata', '-'],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.decode('utf-8', 'replace').splitlines():
        if '=' in line and not line.startswith(';'):
            k, v = line.split('=', 1)
            k = k.strip().lower()
            if tidy(v) and k not in out:
                out[k] = tidy(v)
    return out


def clean_words(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = TRACK_NO.sub('', stem)
    stem = JUNK_ID.sub('', stem)
    stem = re.sub(r'-{2,}', ' ', stem)                 # runs of hyphens replaced brackets / dots in the original names
    stem = re.sub(r'[-_]+', ' ', stem)
    return stem.split()


def explicit_split(path):
    """'Artist - Title' written with spaces around the hyphen -> (artist, title) else None"""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = JUNK_ID.sub('', TRACK_NO.sub('', stem))
    if ' - ' not in stem:
        return None
    a, b = stem.split(' - ', 1)
    f = lambda s: ' '.join(re.sub(r'[-_]+', ' ', s).split())
    return (f(a), f(b)) if f(a) and f(b) else None


def guess_batch(paths, ffmpeg=None):
    tags = [read_tags(ffmpeg, p) for p in paths]
    words = [clean_words(p) for p in paths]
    out = []
    for i, p in enumerate(paths):
        t = tags[i]
        artist = t.get('artist') or t.get('album_artist') or ''
        title = t.get('title') or ''
        if artist and title:
            out.append({'artist': artist, 'title': title, 'source': 'tags'})
            continue
        ex = explicit_split(p)
        if ex:
            out.append({'artist': artist or ex[0], 'title': title or ex[1], 'source': 'tags+name' if (artist or title) else 'file name'})
            continue
        w = words[i]
        # artist shared with other files of the batch = their longest common leading words
        k = 0
        for j, o in enumerate(words):
            if j == i:
                continue
            c = 0
            while c < len(w) - 1 and c < len(o) and w[c].lower() == o[c].lower():
                c += 1
            k = max(k, c)
        if k == 1 and w and w[0].lower() in STOP and len(w) > 2:
            k = 2
        if not k:
            k = 1 if len(w) > 1 else 0
        g_artist, g_title = ' '.join(w[:k]), ' '.join(w[k:]) if k else ' '.join(w)
        out.append({'artist': artist or g_artist, 'title': title or g_title,
                    'source': 'tags+name' if (artist or title) else 'file name'})
    return out
