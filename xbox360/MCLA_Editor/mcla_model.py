"""Data model of the Midnight Club: LA tracklist editor (no GUI code here).

Everything is read straight from the game folder (xarchive_cache.rpf, xarchive_music.rpf):
  * game.dat        - song objects (genre id byte) + music manager objects (per-genre arrays of song hashes)
  * text banks      - display titles (UTF-16LE records keyed by joaat of 'Music_<Genre>_<Artist>_<Title>')
  * sounds.dat      - wave names 'MUSIC\\ARTIST_TITLE'; wave file hash in music.rpf = joaat(wave name)
Findings behind this (see the project notes): a song shows up in the genre list of its own genre id byte; the
order inside a list is the order of the manager's flat array; the game merges MUSIC_0_MANAGER (base) and
MUSIC_0_MANAGER_SC (South Central songs, always after the base ones). Edits keep every object the same size,
so no offsets have to be rebuilt.
"""
import difflib
import json
import os
import re
import shutil
import struct
import tempfile
import zlib

import cache_patch
import gamedat_rebuild
import rpf3
import sounds_rebuild

CHANGES_SUFFIX = '.mcla_changes.json'


def _changes_path(folder):
    """Sidecar file next to (not inside) a game/build folder that records audio/end-of-race clip replacements -
    nothing in the archives themselves says a clip is not the original one, so this is the only place that survives
    a reopen. Best-effort bookkeeping only (not verified against the actual file bytes): if the archives are edited
    behind the tool's back, this can go stale."""
    folder = os.path.abspath(folder.rstrip('\\/'))
    return os.path.join(os.path.dirname(folder), os.path.basename(folder) + CHANGES_SUFFIX)


DRAFT_SUFFIX = '.mcla_draft.json'


def _draft_path(folder):
    """the draft of a game folder is kept next to it (like the .mcla_changes.json), never inside"""
    folder = os.path.abspath(folder.rstrip('\\/'))
    return os.path.join(os.path.dirname(folder), os.path.basename(folder) + DRAFT_SUFFIX)


def _duration(ffmpeg, path):
    """length of an audio file in seconds (ffmpeg's 'Duration:' line), None if unknown"""
    import subprocess
    if not ffmpeg or not path or not os.path.isfile(ffmpeg):
        return None
    try:
        r = subprocess.run([ffmpeg, '-hide_banner', '-i', path], capture_output=True, text=True, errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def _read_changes(folder, key='songs'):
    try:
        with open(_changes_path(folder), encoding='utf-8') as f:
            return json.load(f).get(key, {}) or {}
    except (OSError, ValueError):
        return {}


def _by_hash(files, prefer=()):
    """{'%08x' % file hash: entry}; the same file hash can be in two directories of an archive (xarchive_audio.rpf has
    57 such pairs) - then the entry of a directory in `prefer` wins, else the first one. Build keys are
    (entry.parent, entry.hash), so exactly the entry found here gets replaced."""
    out = {}
    for e in files:
        k = '%08x' % e.hash
        if k not in out or (e.parent in prefer and out[k].parent not in prefer):
            out[k] = e
    return out


# Music at the hangouts (street racer meeting places): 12 random lists in sounds.dat (HNG_LM_<area>_<LOCAL|WAGER>_HANGOUT*
# _MUSICSHOT_MASTER) all play the same 6 positional clips - excerpts of soundtrack songs, separate files in
# xarchive_audio.rpf AMBIENCE_LANDMARKS (one mono XMA stream, 48 kHz, 0x8000 blocks; see xma_single.py). They do not follow
# the playlists: without this, a removed / replaced song would still play there.
HANGOUT_DIR = 0x01a2e516                    # joaat('AMBIENCE_LANDMARKS')
HANGOUT_SLOTS = (  # (clip file, song object of the original excerpt)
    ('HNG_LM_MUSIC_ANTIFORM', 'ECLECTIC_ANTIFORM_BOOMBOXSCREWFACEREMIX'),
    ('HNG_LM_MUSIC_DRIVINGDOWNTHEBLOCKHANGOUT', 'HIPHOP_KIDZINTHEHALL_DRIVINGDOWNTHEBLOCK'),
    ('HNG_LM_MUSIC_GETCOOL', 'HIPHOP_GETCOOL_GO'),
    ('HNG_LM_MUSIC_JMILL', 'HIPHOP_JMILL_LIKEDAT_CLEAN'),
    ('HNG_LM_MUSIC_RANDOMLUCK', 'WESTCOASTRAP_RANDAMLUCK_12HITEM'),
    ('HNG_LM_MUSIC_TECHN9NE', 'HIPHOP_TECHN9NE_EVERYBODYMOVE'),
)
HANGOUT_SECONDS = 60.0                      # length of a new excerpt (the originals are 34-76 s)
HANGOUT_RATE = 48000

GENRES = [  # (key, genre id byte, UI name) - alphabetical, like the in-game menu
    ('ECLECTIC', 0, 'Eclectic'), ('ELECTRONIC', 1, 'Electronic'), ('HARD_ROCK', 2, 'Hard Rock'),
    ('HIPHOP', 3, 'Hip Hop'), ('ROCK', 4, 'Rock'), ('TECHNO', 5, 'Techno'), ('WESTCOASTRAP', 6, 'West Coast Rap')]
GENRE_ID = {k: i for k, i, _ in GENRES}
GENRE_UI = {k: n for k, _, n in GENRES}
# display names of the 7 genres are ordinary records of the text bank (6 languages: EN ES FR DE IT JA); the 'VE_Techno' /
# 'preset_techno' records with the same word belong to the vinyl editor and must NOT be touched
GENRE_LABEL_KEY = {'ECLECTIC': 'MUSIC_GENRE_ECLECTIC', 'ELECTRONIC': 'MUSIC_GENRE_ELECTRONIC', 'HARD_ROCK': 'MUSIC_GENRE_HARDROCK',
                   'HIPHOP': 'MUSIC_GENRE_HIPHOP', 'ROCK': 'MUSIC_GENRE_ROCK', 'TECHNO': 'MUSIC_GENRE_TECHNO',
                   'WESTCOASTRAP': 'MUSIC_GENRE_WEST_RAP'}
LANGUAGES = ('English', 'Spanish', 'French', 'German', 'Italian', 'Japanese')
# the names of the ORIGINAL game (a folder that was saved earlier may already carry other names)
GENRE_NAMES_ORIG = {
    'ECLECTIC': ['ECLECTIC', 'ECLÉCTICA', 'VARIÉ', 'DIVERS', 'ECLETTICA', 'エクレックティック'],
    'ELECTRONIC': ['ELECTRONIC', 'ELECTRÓNICA', 'ELECTRO', 'ELEKTRONIK', 'ELETTRONICA', 'エレクトロニック・ミュージック'],
    'HARD_ROCK': ['HARD ROCK', 'ROCK DURO', 'HARD ROCK', 'HARD ROCK', 'HARD ROCK', 'ハードロック'],
    'HIPHOP': ['HIP HOP', 'HIPHOP', 'HIP HOP', 'HIPHOP', 'HIP HOP', 'ヒップホップ'],
    'ROCK': ['ROCK', 'ROCK', 'ROCK', 'ROCK', 'ROCK', 'ロック'],
    'TECHNO': ['TECHNO', 'TECNO', 'TECHNO', 'TECHNO', 'TECHNO', 'テクノ'],
    'WESTCOASTRAP': ['WEST COAST RAP', 'RAP COSTA OESTE', 'RAP WEST COAST', 'WEST COAST RAP', 'WEST COAST RAP', 'ウエストコーストラップ'],
}
GENRE_NAME_MAX = 24
GENRE_RX = re.compile(r'^(HARD_ROCK|ROCK|TECHNO|HIPHOP|ELECTRONIC|ECLECTIC|WESTCOASTRAP)_')
MANAGERS = ('MUSIC_0_MANAGER', 'MUSIC_0_MANAGER_SC')          # base songs first, SC songs appended
FREED_MAX_SECONDS = 2.0       # a track this short is the silent stub of a removed song whose space was freed
STUB_SECONDS = 1.0
STUB_BYTES = 0x22000          # about the size of such a stub (header + one 128 KB block)
MANAGER_UI = {'MUSIC_0_MANAGER': 'Base', 'MUSIC_0_MANAGER_SC': 'SC'}

GAME_DAT_HASH = 0x8017a51b
SOUNDS_DAT_HASH = 0x9d1db627
TEXT_MAIN = (0xa4c1e8fa, 0xac5297d5)      # identical copies of the main text bank
TEXT_SC = (0xda0b8b80, 0xf5560436)        # identical copies of the SC text bank
TEXT_DIR_HASH = 0x1b2d32db

REC = re.compile(rb'(?s)(.{4})\x01\x00\x06\x00\x00\x00nofont(.{4})')


def _lcs(a, b):
    """Items of one longest common subsequence of two lists (each item unique)."""
    n, m = len(a), len(b)
    t = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            t[i][j] = t[i + 1][j + 1] + 1 if a[i] == b[j] else max(t[i + 1][j], t[i][j + 1])
    out, i, j = set(), 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            out.add(a[i]); i += 1; j += 1
        elif t[i + 1][j] >= t[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


class EditError(Exception):
    """A requested edit is not possible / not supported."""


def _norm(s):
    return re.sub('[^a-z0-9]', '', s.lower())


def _parse_dir_from(D, p):
    out = []
    while p < len(D):
        n = D[p]
        if n == 0 or p + 1 + n + 1 + 7 > len(D):
            break
        name = D[p + 1:p + 1 + n]
        if D[p + 1 + n] != 0 or not re.fullmatch(rb'[\x20-\x7e]+', name):
            break
        off = int.from_bytes(D[p + n + 2:p + n + 5], 'big')
        size = int.from_bytes(D[p + n + 5:p + n + 9], 'big')
        out.append((name.decode(), off, size))
        p += 1 + n + 1 + 7
    return out, p


def parse_directory(D):
    """game.dat object directory -> {name: (offset, size)}."""
    anchor = D.find(b'MUSIC_0_MANAGER\x00') - 1
    best = None
    for start in range(anchor - 4000, anchor):
        ents, _ = _parse_dir_from(D, start)
        if not ents or (best is not None and len(ents) <= len(best)):
            continue
        p = start                                   # a valid parse must run through the anchor record exactly
        for name, _, _ in ents:
            if p == anchor:
                best = ents
                break
            p += 1 + len(name) + 1 + 7
    if not best:
        raise EditError('game.dat object directory not found')
    return {n: (o, s) for n, o, s in best}


TITLE_MAX = 75        # records can be any length; the longest original title (73 chars) fills ~80% of the menu row

GROUP_ORDER = ('HARD_ROCK', 'ROCK', 'ELECTRONIC', 'TECHNO', 'HIPHOP', 'WESTCOASTRAP', 'ECLECTIC')


def parse_groups(blob, songs_by_hash, genre_of):
    """Manager object -> (start, [(genre, [song object names])], end) for the 7 consecutive groups
    [u8 count][count x u32 BE joaat(song object)] in GROUP_ORDER (any group may be empty: count 0, also the first one).
    The start is the first position from which 7 groups parse completely (every hash is a song, every song has the genre of
    its group) and end where the object ends (at most one trailing byte)."""
    last_error = None
    for first in range(1, len(blob)):
        c, groups, ok = first, [], True
        for g in GROUP_ORDER:
            if c >= len(blob):
                ok = False
                break
            cnt = blob[c]
            if c + 1 + 4 * cnt > len(blob):
                ok = False
                break
            names = []
            for k in range(cnt):
                h = struct.unpack('>I', blob[c + 1 + 4 * k:c + 5 + 4 * k])[0]
                n = songs_by_hash.get(h)
                if n is None:
                    ok = False
                    break
                if genre_of[n] != g:                      # genre = the id byte of the song object, not its name
                    ok, last_error = False, 'group %s contains songs of another genre' % g
                    break
                names.append(n)
            if not ok:
                break
            groups.append((g, names))
            c += 1 + 4 * cnt
        if ok and len(blob) - c <= 1 and sum(len(n) for _, n in groups) > 0:
            return first, groups, c
    if len(blob) >= 8 and blob[-7:] == bytes(7):
        # every group empty (e.g. all South Central songs removed): the 7 zero counts end the object
        return len(blob) - 7, [(g, []) for g in GROUP_ORDER], len(blob)
    raise EditError(last_error or 'no song groups found in manager object')


def scan_block(d, lo, hi):
    """text bank block -> {key hash: (text offset, nchars incl. NUL)}"""
    out, pos = {}, lo
    while True:
        m = REC.search(d, pos, hi)
        if not m:
            break
        h = struct.unpack('<I', m.group(1))[0]
        n = struct.unpack('<I', m.group(2))[0]
        if 0 < n < 5000 and m.end() + 2 * n <= hi:
            out[h] = (m.end(), n)
            pos = m.end() + 2 * n
        else:
            pos = m.start() + 1
    return out


class TextBank:
    def __init__(self, plain, entry_idxs):
        self.plain = bytearray(plain)
        self.entries = entry_idxs                      # cache TOC indexes that hold identical copies
        hdr = struct.unpack('<24I', bytes(self.plain[:96]))
        starts = sorted({x for x in hdr[1:12] if 0 < x < len(self.plain)})
        bounds = starts + [len(self.plain)]
        self.blocks = [scan_block(self.plain, bounds[i], bounds[i + 1]) for i in range(len(starts))]

    def get(self, key, block=0):
        r = self.blocks[block].get(key)
        return self.plain[r[0]:r[0] + 2 * r[1]].decode('utf-16-le').rstrip('\0') if r else None

    def capacity(self, key):
        r = self.blocks[0].get(key)
        return r[1] - 1 if r else 0

    def _rescan(self):
        hdr = struct.unpack('<24I', bytes(self.plain[:96]))
        starts = sorted({x for x in hdr[1:12] if 0 < x < len(self.plain)})
        bounds = starts + [len(self.plain)]
        self.blocks = [scan_block(self.plain, bounds[i], bounds[i + 1]) for i in range(len(starts))]

    def add(self, key, text, template_key):
        import addsong
        self.plain = bytearray(addsong.bank_add(bytes(self.plain), key, text, template_key))
        self._rescan()

    def set(self, key, text, per_block=None):
        """Write text into every language block (per_block: a different text for each of the 6 blocks). The record is
        resized: the bytes behind it move and the block offsets in the file header (words 1..11, all are absolute
        offsets) are shifted; nothing else refers to them."""
        blocks = list(self.blocks)
        for bi in range(len(blocks) - 1, -1, -1):       # from the end, so offsets of the earlier blocks stay valid
            r = blocks[bi].get(key)
            if not r:
                continue
            off, n = r
            orig = self.plain[off:off + 2 * n].decode('utf-16-le').rstrip('\0')
            t = per_block[bi] if per_block else text
            if '\xa0' in orig:
                t = t.replace(' ', '\xa0')
            if '（' in orig:
                t = t.replace('(', '（').replace(')', '）')
            if t == orig:
                continue
            self.plain[off - 4:off + 2 * n] = struct.pack('<I', len(t) + 1) + (t + '\0').encode('utf-16-le')
            delta = 2 * (len(t) + 1 - n)
            if delta:
                for i in range(1, 12):
                    w = struct.unpack_from('<I', self.plain, 4 * i)[0]
                    if w > off:
                        struct.pack_into('<I', self.plain, 4 * i, w + delta)
            self._rescan()


class Song:
    def __init__(self, obj):
        self.obj = obj                # directory object name, e.g. TECHNO_KAVINSKY_WAYFARER
        self.manager = None
        self.music_key = None         # 'Music_Techno_Kavinsky_Wayfarer' / 'SC_Music_...'
        self.gid_pos = None           # absolute position of the genre id byte in game.dat
        self.genre = self.orig_genre = None
        self.title = self.orig_title = ''
        self.title_max = 0
        self.wave = None
        self.stream_ids = None        # (left, right) stream ids sounds.dat plays (not always named after the wave)
        self.file_hash = None
        self.file_size = None
        self.seconds = None
        self.bank = None
        self.new_audio = None         # path of a replacement audio file (staged, encoded at build time)
        self.removed = False          # taken out of the manager arrays (the song object itself stays in game.dat)
        self.orig_removed = False     # already not in any playlist when the folder was loaded (removed by an earlier build)
        self.added = False            # a NEW song (does not exist in the game yet; built from a template song)
        self.eor = False              # set at build time: an end-of-race clip is created for this new song
        self.eor_audio = None         # file chosen by the user for the end-of-race clip (else: automatic excerpt of the song)
        self.artist = ''
        self.name = ''
        self.audio_missing = False    # the song is in game.dat/sounds.dat but its audio file is not in xarchive_music.rpf
        self.audio_freed = False      # its audio is a 1 s silent stub (the space of a removed song was freed)
        self.copy_from = None         # draft: take the audio (and clip) from this other game folder (e.g. the original)
        self.audio_loaded = None      # source file its audio was replaced from in an earlier save (info only, from the
                                       # sidecar .mcla_changes.json - the archives themselves don't record this)
        self.eor_loaded = None        # ('custom'|'auto', source file or None) - same, for the end-of-race clip

    @property
    def key_hash(self):
        return rpf3.joaat(self.music_key)


class Project:
    def __init__(self):
        self.game_dir = None
        self.songs = {}
        self.order = {}               # manager -> genre -> [song objects]
        self.orig_order = {}
        self.genre_loaded = {}        # genre key -> its 6 display names in the opened folder (already saved names included)
        self.genre_new = {}           # genre key -> 6 new display names of the draft (only genres changed in this session)
        self.free_removed = False     # draft: free the space of the removed songs (their audio becomes 1 s of silence)
        self.hangout_new = {}         # draft: hangout music slot -> chosen source ({'kind': 'song'|'file'|'keep', ...})
        self.hangout_loaded = {}      # what the slots hold in the opened folder (from the .mcla_changes.json sidecar)
        self.group_order = {}         # manager -> [genre keys in file order]
        self.group_span = {}          # manager -> (start, end) absolute range of the groups in game.dat
        self.music = None             # RPF3 of xarchive_music.rpf (optional)
        self.cache = None
        # audio replacement settings (filled by the GUI): XMAENCODE path, quality for music.rpf / audlo.rpf, ffmpeg
        self.settings = {'encoder': '', 'ffmpeg': '', 'q_music': 80, 'q_lo': 70,
                         'eor': True,          # new songs also get an end-of-race clip (automatic excerpt or a chosen file)
                         'eor_seconds': 20.0,  # length of an automatic excerpt (originals: 10-51 s, median 21 s)
                         'eor_follow_audio': True,   # replacing the audio of a song also replaces its end-of-race clip
                         'hangout_follow': True,     # hangout music follows replaced / removed songs automatically
                         'hangout_seconds': HANGOUT_SECONDS,
                         'hangout_normalize': True,  # hangout music clips at the loudness of the original ones
                         'hangout_lufs': -15.5,      # (loudness.HANGOUT_LUFS)
                         'grow_toc': True,    # archive tables (TOC) may grow beyond the original size (tested on a real console): more new songs
                         'test_boost': False}  # test builds only: new garage / end-of-race entries almost always win the random pick

    # ---------------------------------------------------------------- loading
    def load(self, game_dir, progress=lambda msg: None):
        cache_path = os.path.join(game_dir, 'xarchive_cache.rpf')
        if not os.path.isfile(cache_path):
            raise EditError('xarchive_cache.rpf not found in ' + game_dir)
        self.game_dir = game_dir
        progress('Decrypting the cache.rpf directory (about 15 s)...')
        self.cache = rpf3.RPF3(cache_path)
        files = self.cache.files()

        def inflate(e):
            return zlib.decompressobj(-15).decompress(self.cache.read(e))

        progress('Reading game.dat...')
        ge = next((e for e in files if e.hash == GAME_DAT_HASH and e.kind == 'zlib'), None)
        if not ge:
            raise EditError('game.dat not found in cache.rpf')
        self.game_dat_idx = ge.idx
        self.gd0 = inflate(ge)
        D = self.gd0
        dirs = parse_directory(D)
        self.dirs = dirs
        songs_by_hash = {rpf3.joaat(n): n for n in dirs if GENRE_RX.match(n)}
        self.songs = {n: Song(n) for n in songs_by_hash.values()}

        # genre id byte + Music_ key of every song object
        gid = {}
        for m in re.finditer(rb'(.)(.{4})\xe3\x8f\xcf\x16(.)((?:SC_)?Music_[A-Za-z0-9]+_[A-Za-z0-9_]+)\x00', D, re.S):
            if m.group(3)[0] == len(m.group(4)):
                gid[m.group(4).decode()] = (m.start(), m.group(1)[0])
        id2genre = {i: k for k, i, _ in GENRES}
        for n, s in self.songs.items():
            off, size = dirs[n]
            w = D[off - 12:off + size + 12]
            m = re.search(rb'(?:SC_)?Music_[A-Za-z0-9]+_[A-Za-z0-9_]+', w)
            if not m or m.group().decode() not in gid:
                raise EditError('song object %s: Music_ key not found' % n)
            s.music_key = m.group().decode()
            s.gid_pos, gi = gid[s.music_key]
            s.genre = s.orig_genre = id2genre[gi]

        # manager groups
        self.order, self.group_order, self.group_span = {}, {}, {}
        for mgr in MANAGERS:
            off, size = dirs[mgr]
            blob = D[off:off + size + 8]
            start, groups, end = parse_groups(blob, songs_by_hash, {n: s.genre for n, s in self.songs.items()})
            self.order[mgr] = {}
            self.group_order[mgr] = []
            for g, names in groups:
                self.order[mgr][g] = list(names)
                self.group_order[mgr].append(g)
                for n in names:
                    self.songs[n].manager = mgr
            self.group_span[mgr] = (off + start, off + end)
        self.orig_order = {m: {g: list(v) for g, v in d.items()} for m, d in self.order.items()}
        for s in self.songs.values():                      # song objects that no manager lists: removed by an earlier build
            if s.manager is None:
                s.manager = MANAGERS[1] if s.music_key.startswith('SC_') else MANAGERS[0]
                s.removed = s.orig_removed = True

        # text banks / titles
        progress('Reading text banks...')
        by_hash = {}
        for e in files:
            by_hash.setdefault(e.hash, []).append(e)
        self.banks = []
        for group in (TEXT_MAIN, TEXT_SC):
            es = [x for h in group for x in by_hash.get(h, []) if x.path.split('/')[-2] == '%08x' % TEXT_DIR_HASH]
            if es:
                self.banks.append(TextBank(inflate(es[0]), [x.idx for x in es]))
        if not self.banks:
            raise EditError('text banks not found')
        self.genre_loaded, self.genre_new = {}, {}
        for g, k in GENRE_LABEL_KEY.items():
            names = [self.banks[0].get(rpf3.joaat(k), b) for b in range(len(self.banks[0].blocks))]
            if len(names) == len(LANGUAGES) and all(names):
                self.genre_loaded[g] = names
        for s in self.songs.values():
            for b in self.banks:
                t = b.get(s.key_hash)
                if t is not None:
                    s.bank, s.title, s.orig_title = b, t, t
                    s.title_max = TITLE_MAX
                    break

        # wave names -> file hash -> duration
        progress('Matching songs with audio files...')
        se = next((e for e in files if e.hash == SOUNDS_DAT_HASH and e.kind == 'zlib'), None)
        waves, streams = {}, {}
        if se:
            plain = inflate(se)
            for m in re.finditer(rb'MUSIC\\([A-Za-z0-9_\.\-]+)', plain):
                w = m.group(1).decode()
                waves.setdefault(w, '%08x' % rpf3.joaat(w))
            streams = self._song_streams(plain)
        mpath = os.path.join(game_dir, 'xarchive_music.rpf')
        self.music = rpf3.RPF3(mpath) if os.path.isfile(mpath) else None
        mfiles = {'%08x' % e.hash: e for e in self.music.files()} if self.music else {}
        self._match_waves(waves, mfiles, streams)

        by_wave = {s.wave: s for s in self.songs.values() if s.wave}
        for obj, info in _read_changes(game_dir).items():
            # a song added in the editor was recorded as ADDED_<wave> (its name before the build); in the game it is a
            # normal song object, found again by its wave name
            s = self.songs.get(obj) or (by_wave.get(obj[6:]) if obj.startswith('ADDED_') else None)
            if not s:
                continue
            s.audio_loaded = info.get('audio_source') or None
            eor = info.get('eor')
            if eor:
                s.eor_loaded = (eor.get('kind'), eor.get('source'))
        slots = {f for f, _ in HANGOUT_SLOTS}
        self.hangout_loaded = {k: v for k, v in _read_changes(game_dir, 'hangout').items()
                               if k in slots and isinstance(v, dict)}
        self.hangout_new = {}
        return self

    # ---------------------------------------------------------------- hangout music
    def hangout_owner(self, slot):
        """(kind, value) of what a hangout slot holds in the opened folder: ('song', obj) / ('file', path)"""
        cur = self.hangout_loaded.get(slot)
        if cur and cur.get('kind') == 'file':
            return 'file', cur.get('path')
        if cur and cur.get('kind') == 'song' and cur.get('obj'):
            return 'song', cur['obj']
        return 'song', dict(HANGOUT_SLOTS)[slot]

    def set_hangout(self, slot, src):
        """Manual choice for a slot: {'kind': 'song', 'obj': .., 'start': s|None} / {'kind': 'file', 'path': .., 'start':
        s|None} / {'kind': 'keep'} (leave what the folder has); None = automatic again."""
        if slot not in dict(HANGOUT_SLOTS):
            raise EditError('Unknown hangout slot %s' % slot)
        if src is None:
            self.hangout_new.pop(slot, None)
            return
        kind = src.get('kind')
        if kind == 'song':
            s = self.songs.get(src.get('obj'))
            if not s or s.removed:
                raise EditError('Choose a song that is in the playlists')
            if not self._hangout_usable(s):
                raise EditError('"%s" has no audio' % s.title)
        elif kind == 'file':
            if not os.path.isfile(src.get('path') or ''):
                raise EditError('File not found: %s' % src.get('path'))
        elif kind != 'keep':
            raise EditError('Unknown source kind %s' % kind)
        start = src.get('start')
        if start is not None and (not isinstance(start, (int, float)) or start < 0):
            raise EditError('The start must be a number of seconds')
        self.hangout_new[slot] = dict(src)

    def _hangout_usable(self, s):
        return s and not s.removed and (s.new_audio or (s.file_hash and not s.audio_missing and not s.audio_freed))

    def hangout_plan(self):
        """{slot: source} of the slots that are (re)built by the next build: the manual choices, plus - with
        settings['hangout_follow'] - the slots whose song got new audio (a new excerpt of it) or left the playlists (an
        excerpt of another kept song, preferably of the same genre and not playing at the hangouts already)."""
        plan = {slot: dict(src) for slot, src in self.hangout_new.items() if src.get('kind') != 'keep'}
        if not self.settings.get('hangout_follow', True):
            return plan
        # songs heard at the hangouts after this build, so that an automatic pick takes another one
        used = {src['obj'] for src in plan.values() if src.get('kind') == 'song'}
        used |= {self.hangout_owner(slot)[1] for slot, _ in HANGOUT_SLOTS
                 if slot not in plan and self.hangout_owner(slot)[0] == 'song'}
        for slot, _ in HANGOUT_SLOTS:
            if slot in plan or slot in self.hangout_new:
                continue
            kind, val = self.hangout_owner(slot)
            if kind != 'song':
                continue
            s = self.songs.get(val)
            if s and not s.removed and s.new_audio:
                plan[slot] = {'kind': 'song', 'obj': s.obj, 'start': None, 'auto': 'new audio'}
            elif not self._hangout_usable(s):
                genre = s.genre if s else None
                pool = [x for g in ([genre] if genre else []) + [g for g, _, _ in GENRES if g != genre]
                        for x in self.genre_list(g) if self._hangout_usable(x)]
                pick = next((x for x in pool if x.obj not in used), pool[0] if pool else None)
                if pick:
                    used.add(pick.obj)
                    plan[slot] = {'kind': 'song', 'obj': pick.obj, 'start': None, 'auto': 'song removed'}
        return plan

    def _hangout_audio(self, src, tmpdir):
        """source audio file of a slot's excerpt (a song's new audio, the song decoded from the archive, or a file)"""
        if src['kind'] == 'file':
            return src['path']
        s = self.songs[src['obj']]
        if s.new_audio:
            return s.new_audio
        wav = os.path.join(tmpdir, 'hng_src_%s.wav' % s.obj)
        if not os.path.isfile(wav):
            import mcla_music
            ff = self.settings.get('ffmpeg')
            if not ff or not os.path.isfile(ff):
                raise EditError('ffmpeg not found (Audio settings...)')
            mcla_music.FFMPEG = ff
            raw = wav[:-4] + '.bin'
            with open(raw, 'wb') as f:
                f.write(self.song_bytes(s))
            try:
                mcla_music.convert(raw, wav, 'wav', name=tuple(s.stream_ids or ()) or s.wave)
            finally:
                os.remove(raw)
        return wav

    def _hangout_cut(self, src, audio):
        """(start, length) of the excerpt"""
        import eor_clip
        secs = float(self.settings.get('hangout_seconds') or HANGOUT_SECONDS)
        dur = _duration(self.settings.get('ffmpeg'), audio) or secs
        if src.get('start') is not None:
            start = float(src['start'])
            if start >= dur - 1:
                start = max(0.0, dur - secs)
            return start, min(secs, dur - start)
        return eor_clip.auto_excerpt(self.settings['ffmpeg'], audio, secs)

    def _hangout_excerpt(self, src, wav, tmpdir):
        """the excerpt of a slot as a mono 48 kHz WAV, at the loudness of the original clips (settings['hangout_normalize'],
        target settings['hangout_lufs']) -> its length in seconds"""
        import loudness
        audio = self._hangout_audio(src, tmpdir)
        start, length = self._hangout_cut(src, audio)
        st = self.settings
        target = float(st.get('hangout_lufs') or loudness.HANGOUT_LUFS) if st.get('hangout_normalize', True) else None
        try:
            loudness.excerpt(st['ffmpeg'], audio, wav, start, length, HANGOUT_RATE, target)
        except loudness.LoudnessError as e:
            raise EditError(str(e))
        return length

    def _hangout_data(self, slot, src, orig, tmpdir):
        """the new clip file: one mono 48 kHz XMA stream with the stream id / rate word of the file it replaces"""
        import xma_encode
        import xma_single
        P = xma_single.parse(orig)
        self._check_encoder()
        st = self.settings
        wav = os.path.join(tmpdir, 'hng_%s.wav' % slot)
        try:
            length = self._hangout_excerpt(src, wav, tmpdir)
            packets, n = xma_encode.encode_mono(wav, st['encoder'], 0, length + 1, st['q_music'], st['ffmpeg'],
                                                HANGOUT_RATE)
            return xma_single.build(packets, n, P['id'], HANGOUT_RATE, P['low'])
        except (xma_encode.XmaError, ValueError) as e:
            raise EditError('Hangout music %s: %s' % (slot, e))

    def hangout_label(self, src):
        if not src:
            return ''
        if src.get('kind') == 'file':
            return 'file %s' % os.path.basename(src.get('path') or '')
        if src.get('kind') == 'keep':
            return 'kept as it is'
        s = self.songs.get(src.get('obj'))
        return (s.title if s else src.get('obj') or '?') + ('' if src.get('start') is None else ' @%d:%02d' % divmod(
            int(src['start']), 60))

    def hangout_preview_wav(self, slot, ffmpeg=None, planned=True):
        """WAV of a slot: the excerpt the next build would make (planned), else the clip the folder has now"""
        tmpdir = os.path.join(tempfile.gettempdir(), 'mcla_gui')
        os.makedirs(tmpdir, exist_ok=True)
        wav = os.path.join(tmpdir, 'hangout_%s.wav' % slot)
        src = self.hangout_plan().get(slot) if planned else None
        ff = ffmpeg or self.settings.get('ffmpeg')
        if not ff or not os.path.isfile(ff):
            raise EditError('ffmpeg not found (Audio settings...)')
        if src:
            if not self.settings.get('ffmpeg'):
                self.settings['ffmpeg'] = ff
            self._hangout_excerpt(src, wav, tmpdir)          # exactly what the build encodes (loudness included)
            return wav
        import xma_single
        r = rpf3.RPF3(os.path.join(self.game_dir, 'xarchive_audio.rpf'))
        try:
            e = next((x for x in r.files() if x.hash == rpf3.joaat(slot) and x.parent == HANGOUT_DIR), None)
            if not e:
                raise EditError('Hangout music file %s not found in xarchive_audio.rpf' % slot)
            data = r.read(e)
        finally:
            r.f.close()
        try:
            xma_single.to_wav(data, wav, ff)
        except (RuntimeError, ValueError) as ex:
            raise EditError('Hangout music %s: %s' % (slot, ex))
        return wav

    def _song_streams(self, plain):
        """{song object: (wave name, left stream id, right stream id)} as sounds.dat plays them: the song's SND object
        refers to two slot objects (..._LEFT, ..._RIGHT) that end with [joaat('MUSIC\\<wave>')][stream id]. The names do
        not always follow the song: Wolfgang Gartner - Squares plays MUSIC\\JOEYYOUNGMAN_SQUARES, and the stream ids of
        Antiform - Boombox and John Acquaviva - Good are named after other strings."""
        import addsong
        try:
            sd = sounds_rebuild.SoundsDat(plain)
        except Exception:
            return {}
        H = {rpf3.joaat(n): n for n in sd.order}
        paths = {rpf3.joaat(m.group().decode()): m.group(1).decode()
                 for m in re.finditer(rb'MUSIC\\([A-Za-z0-9_\.\-]+)', plain)}
        out = {}
        for s in self.songs.values():
            try:
                main = H[addsong.snd_hash_of(self, s.obj)]
                body = sd.body[main]
                slots = [H.get(struct.unpack('>I', body[r:r + 4])[0], '') for r in sorted(sd.refs[main])]
                if len(slots) != 2 or not slots[0].endswith('_LEFT') or not slots[1].endswith('_RIGHT'):
                    continue
                (wl, il), (wr, ir) = [struct.unpack('>II', sd.body[t][-8:]) for t in slots]
            except (KeyError, AttributeError, struct.error):
                continue
            if wl == wr and wl in paths:
                out[s.obj] = (paths[wl], il, ir)
        return out

    def _match_waves(self, waves, mfiles, streams=None):
        def rest(s):
            m = re.match(r'(?:SC_)?Music_[A-Za-z0-9]+_(.*)', s.music_key)
            return _norm(m.group(1))
        unmatched = dict(waves)
        songs = list(self.songs.values())
        for s in songs:                           # the wave sounds.dat really plays
            st = (streams or {}).get(s.obj)
            if st and st[0] in waves:
                s.wave, s.stream_ids = st[0], st[1:]
                unmatched.pop(st[0], None)
        pending = []
        for s in songs:
            if s.wave:
                continue
            hit = next((w for w in unmatched if _norm(w[3:] if w.startswith('SC_') else w) == rest(s)), None)
            if hit:
                s.wave = hit
                unmatched.pop(hit)
            else:
                pending.append(s)
        for s in pending:
            names = {_norm(w[3:] if w.startswith('SC_') else w): w for w in unmatched}
            c = difflib.get_close_matches(rest(s), list(names), n=1, cutoff=0.75)
            if c:
                s.wave = names[c[0]]
                unmatched.pop(names[c[0]])
        for s in songs:
            if s.wave:
                s.file_hash = '%08x' % rpf3.joaat(s.wave)
            e = mfiles.get(s.file_hash)
            if e:
                s.file_size = e.size
                w = struct.unpack('>32I', self.music.read(e, 0x80))
                s.seconds = w[24] / (w[26] >> 16)
                s.audio_freed = s.seconds < FREED_MAX_SECONDS
            elif self.music and s.wave and s.file_hash:
                s.audio_missing = True

    # ---------------------------------------------------------------- queries
    def genre_list(self, genre):
        """Songs of a genre in game order: base manager first, then SC."""
        out = []
        for mgr in MANAGERS:
            out += [self.songs[n] for n in self.order[mgr].get(genre, [])]
        return out

    def genre_texts(self, key):
        """the 6 names the genre has now (draft, else the opened folder, else the original game)"""
        return list(self.genre_new.get(key) or self.genre_loaded.get(key) or GENRE_NAMES_ORIG[key])

    def genre_ui(self, key):
        """short name of a genre as shown in the editor: its current English name"""
        cur = self.genre_texts(key)[0]
        return GENRE_UI[key] if cur == GENRE_NAMES_ORIG[key][0] else cur.title()

    def genre_label(self, key):
        """name for lists and menus: a renamed genre also shows which of the 7 built-in genres it is"""
        n = self.genre_ui(key)
        return n if n == GENRE_UI[key] else '%s (%s)' % (n, GENRE_UI[key])

    def genre_renamed(self, key):
        """the genre does not have its original names (in the draft or already in the opened folder)"""
        return self.genre_texts(key) != GENRE_NAMES_ORIG[key]

    def set_genre_name(self, key, texts):
        """Rename a genre (its display name in the game menus). texts: one string for all 6 languages or a list of 6."""
        if key not in self.genre_loaded:
            raise EditError('The display name of this genre was not found in the text bank')
        if isinstance(texts, str):
            texts = [texts] * len(LANGUAGES)
        if len(texts) != len(LANGUAGES):
            raise EditError('Need a name for each of the %d languages' % len(LANGUAGES))
        out = []
        for i, x in enumerate(texts):
            x = ' '.join(x.split())
            x = x.upper() if x.isascii() or i < 5 else x
            if not x:
                raise EditError('The %s name must not be empty' % LANGUAGES[i])
            if len(x) > GENRE_NAME_MAX:
                raise EditError('The %s name is too long (%d characters, at most %d): it has to fit into the game menu'
                                % (LANGUAGES[i], len(x), GENRE_NAME_MAX))
            out.append(x)
        if out == self.genre_loaded[key]:
            self.genre_new.pop(key, None)
        else:
            self.genre_new[key] = out

    def revert_genre_name(self, key):
        self.genre_new.pop(key, None)

    def empty_genres(self):
        """genres without any song in the playlists - shown as a warning before a build: the menu only shows an empty list,
        but mcMusicManager::SetGenre quits (Disc Read Error + endless loop = freeze) when a save restores an empty genre
        (tested on a real Xbox 360)"""
        return [g for g, _, _ in GENRES if not self.genre_list(g)]

    def counts(self):
        return {g: len(self.genre_list(g)) for g, _, _ in GENRES}

    def orig_counts(self):
        return {g: sum(len(self.orig_order[m].get(g, [])) for m in MANAGERS) for g, _, _ in GENRES}

    def state(self, s):
        """'' | set of {'genre','order','title'}"""
        st = set()
        if s.removed:
            return set() if s.orig_removed else {'removed'}
        if s.added:
            return {'added'}
        if s.orig_removed:                               # brought back into the playlists
            st.add('restored')
        elif s.genre != s.orig_genre:
            st.add('genre')
        elif True:  # a song counts as re-ordered when it is not part of the longest run that kept its order
            cur = [n for n in self.order[s.manager][s.genre] if self.songs[n].orig_genre == s.genre]
            orig = [n for n in self.orig_order[s.manager][s.genre] if self.songs[n].genre == s.genre]
            if s.obj not in _lcs(cur, orig):
                st.add('order')
        if s.title != s.orig_title:
            st.add('title')
        if s.new_audio:
            st.add('audio')
        if s.copy_from:
            st.add('copy')
        if s.eor_audio:
            st.add('clip')
        return st

    def changes(self):
        out = []
        for s in self.songs.values():
            st = self.state(s)
            if 'added' in st:
                out.append('Added: %s (%s) <- %s' % (s.title, self.genre_ui(s.genre), os.path.basename(s.new_audio or '')))
            if 'removed' in st:
                out.append('Removed: %s (%s)' % (s.title, self.genre_ui(s.orig_genre)))
            if 'restored' in st:
                out.append('Back in the playlists: %s (%s)' % (s.title, self.genre_ui(s.genre)))
            if 'genre' in st:
                out.append('%s: %s -> %s' % (s.title, self.genre_ui(s.orig_genre), self.genre_ui(s.genre)))
            if 'order' in st:
                out.append('%s: new position in %s' % (s.title, self.genre_ui(s.genre)))
            if 'title' in st:
                out.append('Title: "%s" -> "%s"' % (s.orig_title, s.title))
            if 'audio' in st:
                out.append('%s: %s <- %s' % ('Audio restored' if (s.audio_missing or s.audio_freed) else 'Audio', s.title,
                                             os.path.basename(s.new_audio)))
            if 'copy' in st:
                out.append('Audio from %s: %s' % (s.copy_from, s.title))
            if 'clip' in st and not s.added:
                out.append('End-of-race clip: %s <- %s' % (s.title, os.path.basename(s.eor_audio)))
        for g, names in self.genre_new.items():
            out.append('Genre name: %s -> %s' % (self.genre_loaded[g][0], names[0] if len(set(names)) == 1 else ' / '.join(names)))
        plan = self.eor_lists_plan()
        if plan:
            if plan[0]:
                out.append('End-of-race lists: %d clip(s) of songs not in the playlists are replaced by clips of kept songs'
                           % len(plan[0]))
            if plan[1]:
                out.append('End-of-race lists: the clips of %d restored song(s) are put back' % len(plan[1]))
        gplan = self.garage_plan()
        if gplan:
            if gplan[0]:
                out.append('Garage music: %d removed song(s) are replaced by kept songs' % len(gplan[0]))
            if gplan[1]:
                out.append('Garage music: %d restored song(s) are put back' % len(gplan[1]))
        if self.free_removed and self.freeable():
            out.append('Free the space of %d removed song(s): their audio and end-of-race clips become 1 s of silence'
                       % len(self.freeable()))
        hplan = self.hangout_plan()
        for i, (slot, _) in enumerate(HANGOUT_SLOTS, 1):
            src = hplan.get(slot)
            if src:
                out.append('Hangout music %d: %s%s' % (i, self.hangout_label(src),
                                                       ' (automatic: %s)' % src['auto'] if src.get('auto') else ''))
        return sorted(out)

    def removed_songs(self):
        return [s for s in self.songs.values() if s.removed]

    def audio_songs(self):
        return [s for s in self.songs.values() if s.new_audio]

    def is_dirty(self):
        return bool(self.changes())

    # ---------------------------------------------------------------- edits
    def move_to_genre(self, obj, genre, index=None):
        """Move a song to another genre. index = position inside the target genre list (combined view)."""
        s = self.songs[obj]
        if genre == s.genre and not s.removed:
            return
        mgr = s.manager
        # a genre may become empty here; building / saving warns about it (empty_genres)
        base = sum(len(self.order[m].get(genre, [])) for m in MANAGERS[:MANAGERS.index(mgr)])
        lst = self.order[mgr][genre]
        pos = len(lst) if index is None else max(0, min(len(lst), index - base))
        if s.removed:                                   # bring a removed song back, straight into the target genre
            s.removed = False
        else:
            self.order[mgr][s.genre].remove(obj)
        lst.insert(pos, obj)
        s.genre = genre

    def move_within(self, obj, delta):
        """Move a song up (-1) / down (+1) inside its genre list (never across the base/SC boundary)."""
        s = self.songs[obj]
        if s.removed:
            return False
        lst = self.order[s.manager][s.genre]
        i = lst.index(obj)
        j = i + delta
        if not 0 <= j < len(lst):
            return False
        lst[i], lst[j] = lst[j], lst[i]
        return True

    def move_to_index(self, obj, index):
        """Put a song at a position (0-based, combined list) inside its genre; stays within its own base/SC block."""
        s = self.songs[obj]
        if s.removed:
            return
        lst = self.order[s.manager][s.genre]
        base = sum(len(self.order[m].get(s.genre, [])) for m in MANAGERS[:MANAGERS.index(s.manager)])
        new = max(0, min(len(lst) - 1, index - base))
        lst.remove(obj)
        lst.insert(new, obj)

    def remove_song(self, obj):
        """Take a song out of the playlists (its objects stay in game.dat, only the manager array entry goes)."""
        s = self.songs[obj]
        if s.removed:
            return
        if s.added:
            self.revert(obj)
            return
        # the last song of a genre may go too; building / saving warns about empty genres (empty_genres)
        self.order[s.manager][s.genre].remove(obj)
        s.removed = True

    def set_title(self, obj, text):
        s = self.songs[obj]
        text = ' '.join(text.split()).upper()
        if not text:
            raise EditError('Title must not be empty')
        if len(text) > s.title_max:
            raise EditError('Title is too long: %d characters, at most %d for this song '
                            '(the game menu has limited room)' % (len(text), s.title_max))
        if any(ord(c) > 0xFFFF for c in text):
            raise EditError('Only characters from the basic multilingual plane are supported')
        s.title = text

    def revert(self, obj):
        s = self.songs[obj]
        m = s.manager
        if s.added:                                      # undo the addition: the song disappears completely
            self.order[m][s.genre].remove(obj)
            del self.songs[obj]
            return
        if s.orig_removed:                               # it was not in a playlist to begin with: back to that state
            if not s.removed:
                self.order[m][s.genre].remove(obj)
            s.removed = True
            s.genre, s.title, s.new_audio, s.eor_audio, s.copy_from = s.orig_genre, s.orig_title, None, None, None
            return
        if not s.removed:
            self.order[m][s.genre].remove(obj)           # take it out of wherever it is now ...
        s.removed = False
        lst = self.order[m][s.orig_genre]
        lst.insert(min(self.orig_order[m][s.orig_genre].index(obj), len(lst)), obj)   # ... and back to its old slot
        s.genre, s.title = s.orig_genre, s.orig_title
        s.new_audio = s.eor_audio = s.copy_from = None

    def revert_all(self):
        self.genre_new = {}
        self.free_removed = False
        self.hangout_new = {}
        for o in [o for o, s in self.songs.items() if s.added]:
            del self.songs[o]
        self.order = {m: {g: list(v) for g, v in d.items()} for m, d in self.orig_order.items()}
        for s in self.songs.values():
            s.genre, s.title, s.new_audio, s.removed = s.orig_genre, s.orig_title, None, s.orig_removed
            s.eor_audio = s.copy_from = None

    # ---------------------------------------------------------------- freeing the space of removed songs
    def freeable(self):
        """removed songs whose audio or end-of-race clip still takes space in the archives (see free_removed)"""
        out = []
        for s in self.songs.values():
            if not s.removed or s.added or not s.file_hash:
                continue
            en = self.eor_name_of(s)
            if (not s.audio_freed and not s.audio_missing) or (en and self._clip_seconds(en) >= FREED_MAX_SECONDS):
                out.append(s)
        return out

    def _clip_seconds(self, en):
        """length of an end-of-race clip (the longer one of audio.rpf / audlo.rpf; 0 when it is in neither)"""
        c = self.__dict__.get('_clip_len')
        if c is None:
            c = self._clip_len = {}
            import addsong
            for arch in ('audlo', 'audio'):
                path = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
                if not os.path.isfile(path):
                    continue
                r = rpf3.RPF3(path)
                try:
                    for e in r.files():
                        if e.path.split('/')[-2] == '%08x' % addsong.EOR_DIR:
                            w = struct.unpack('>32I', r.read(e, 0x80))
                            sec = w[24] / (w[26] >> 16) if w[26] >> 16 else 0.0
                            c[e.hash] = max(c.get(e.hash, 0.0), sec)
                finally:
                    r.f.close()
        return c.get(rpf3.joaat(en), 0.0)

    def free_estimate(self):
        """{archive: bytes that freeing the removed songs gives back} (their tracks and end-of-race clips)"""
        out = {}
        songs = self.freeable()
        hashes = {int(s.file_hash, 16) for s in songs}
        clips = {rpf3.joaat(en) for s in songs if (en := self.eor_name_of(s))}
        for arch in ('music', 'audlo', 'audio'):
            path = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
            if not os.path.isfile(path):
                continue
            r = rpf3.RPF3(path)
            try:
                out[arch] = sum(max(0, e.data_size - STUB_BYTES) for e in r.files() if e.hash in hashes or e.hash in clips)
            finally:
                r.f.close()
        return out

    def set_copy_from(self, folder):
        """Songs in the playlists without audio (lost, or freed): take their audio and end-of-race clips from another game
        folder, e.g. the original game (no encoding: the files are copied). Returns (songs staged, songs not found there)."""
        if os.path.abspath(folder).lower() == os.path.abspath(self.game_dir).lower():
            raise EditError('Choose another game folder (e.g. the original game), not the opened one')
        path = os.path.join(folder, 'xarchive_music.rpf')
        if not os.path.isfile(path):
            raise EditError('xarchive_music.rpf not found in %s' % folder)
        r = rpf3.RPF3(path)
        try:
            ok = {}
            for e in r.files():
                w = struct.unpack('>32I', r.read(e, 0x80))
                if w[26] >> 16 and w[24] / (w[26] >> 16) >= FREED_MAX_SECONDS:       # a real track, not a stub
                    ok['%08x' % e.hash] = True
        finally:
            r.f.close()
        staged, missing = [], []
        for s in self.missing_audio():
            if s.new_audio:
                continue
            if s.file_hash in ok:
                s.copy_from = folder
                staged.append(s)
            else:
                missing.append(s)
        return staged, missing

    # ---------------------------------------------------------------- draft file (survives a crash / closing the editor)
    def has_edits(self):
        """the user changed something (the automatic end-of-race / garage list fixes alone do not count)"""
        return bool(self.genre_new) or self.free_removed or bool(self.hangout_new) or self.order != self.orig_order or any(
            s.added or s.genre != s.orig_genre or s.title != s.orig_title or s.removed != s.orig_removed or s.new_audio
            or s.eor_audio or s.copy_from for s in self.songs.values())

    def save_draft(self):
        """Write the draft to <folder>.mcla_draft.json next to the game folder; without edits the file is removed.
        Returns the time it was saved, or None."""
        import time
        path = _draft_path(self.game_dir)
        if not self.has_edits():
            if os.path.isfile(path):
                os.remove(path)
            return None
        songs = {}
        for s in self.songs.values():
            if s.added:
                continue
            d = {}
            if s.genre != s.orig_genre:
                d['genre'] = s.genre
            if s.title != s.orig_title:
                d['title'] = s.title
            if s.removed != s.orig_removed:
                d['removed'] = s.removed
            if s.new_audio:
                d['new_audio'] = s.new_audio
            if s.eor_audio:
                d['eor_audio'] = s.eor_audio
            if s.copy_from:
                d['copy_from'] = s.copy_from
            if d:
                songs[s.obj] = d
        added = [{'obj': s.obj, 'artist': s.artist, 'name': s.name, 'genre': s.genre, 'title': s.title,
                  'audio': s.new_audio, 'eor_audio': s.eor_audio} for s in self.songs.values() if s.added]
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        data = {'version': 1, 'saved': now, 'changes': len(self.changes()), 'songs': songs, 'added': added,
                'order': self.order, 'genre_new': self.genre_new, 'free_removed': self.free_removed,
                'hangout_new': self.hangout_new}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)                                # never a half-written draft
        return now

    def load_draft(self):
        """the draft file of this folder as a dict, or None"""
        try:
            with open(_draft_path(self.game_dir), encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) and d.get('version') == 1 else None
        except (OSError, ValueError):
            return None

    def discard_draft(self):
        """the draft file is not used: kept once as <folder>.mcla_draft.old.json, in case it was declined by mistake"""
        path = _draft_path(self.game_dir)
        if os.path.isfile(path):
            os.replace(path, path[:-len('.json')] + '.old.json')

    def delete_draft(self):
        path = _draft_path(self.game_dir)
        if os.path.isfile(path):
            os.remove(path)

    def apply_draft(self, d):
        """Put a saved draft back onto the freshly loaded folder. Returns warnings (files that are gone, songs that no
        longer exist...); what cannot be restored is left out."""
        warn = []
        ok = lambda p: bool(p) and os.path.isfile(p)
        objmap = {}
        for a in d.get('added', []):
            if not ok(a.get('audio')):
                warn.append('New song "%s": audio file not found (%s)' % (a.get('title'), a.get('audio')))
                continue
            try:
                s = self.add_song(a['artist'], a['name'], a['genre'], a['audio'], a.get('eor_audio') if ok(a.get('eor_audio')) else None)
            except (EditError, KeyError) as e:
                warn.append('New song "%s": %s' % (a.get('title'), e))
                continue
            if a.get('eor_audio') and not ok(a['eor_audio']):
                warn.append('New song "%s": end-of-race clip file not found, automatic clip used' % s.title)
            if a.get('title') and a['title'] != s.title and len(a['title']) <= s.title_max:
                s.title = a['title']
            objmap[a.get('obj')] = s.obj
        for obj, c in d.get('songs', {}).items():
            s = self.songs.get(obj)
            if not s:
                warn.append('Song %s is not in this folder any more' % obj)
                continue
            if c.get('genre') in GENRE_ID:
                s.genre = c['genre']
            if c.get('title') and len(c['title']) <= s.title_max:
                s.title = c['title']
            if 'removed' in c:
                s.removed = bool(c['removed'])
            if c.get('new_audio'):
                if ok(c['new_audio']):
                    s.new_audio = c['new_audio']
                else:
                    warn.append('"%s": replacement audio not found (%s)' % (s.title, c['new_audio']))
            if c.get('eor_audio'):
                if ok(c['eor_audio']):
                    s.eor_audio = c['eor_audio']
                else:
                    warn.append('"%s": end-of-race clip file not found (%s)' % (s.title, c['eor_audio']))
            if c.get('copy_from'):
                if os.path.isfile(os.path.join(c['copy_from'], 'xarchive_music.rpf')):
                    s.copy_from = c['copy_from']
                else:
                    warn.append('"%s": game folder to take the audio from not found (%s)' % (s.title, c['copy_from']))
        self.free_removed = bool(d.get('free_removed'))
        for slot, src in (d.get('hangout_new') or {}).items():
            src = dict(src or {})
            if src.get('kind') == 'song':
                src['obj'] = objmap.get(src.get('obj'), src.get('obj'))
            try:
                self.set_hangout(slot, src)
            except EditError as e:
                warn.append('Hangout music %s: %s' % (slot, e))
        for g, names in (d.get('genre_new') or {}).items():
            if g in GENRE_UI and isinstance(names, list) and len(names) == len(LANGUAGES):
                self.genre_new[g] = [str(n)[:GENRE_NAME_MAX] for n in names]
        # order: the saved lists, keeping only what is consistent now; any song left over goes to the end of its list
        new = {m: {g: [] for g in self.order[m]} for m in self.order}
        placed = set()
        for m, gd in (d.get('order') or {}).items():
            for g, lst in (gd or {}).items():
                for obj in lst:
                    obj = objmap.get(obj, obj)
                    s = self.songs.get(obj)
                    if s and not s.removed and s.manager == m and s.genre == g and g in new.get(m, {}) and obj not in placed:
                        new[m][g].append(obj)
                        placed.add(obj)
        for s in self.songs.values():
            if not s.removed and s.obj not in placed:
                new[s.manager][s.genre].append(s.obj)
        self.order = new
        return warn

    def add_capacity(self):
        """How many more NEW songs fit into the archive tables. A song needs 1 entry in music.rpf and 1 in audlo.rpf, plus
        1 + 1 (audlo.rpf + audio.rpf) for its end-of-race clip while audio.rpf has room for it (later songs simply get none)."""
        m, l, a = self._toc_room('music'), self._toc_room('audlo'), self._toc_room('audio')
        if m is None or l is None:
            return 0
        if self.settings.get('eor', True) and a:
            n = l // 2 if l // 2 <= a else l - a            # n songs, min(n, a) of them with a clip: n + min(n, a) <= l
        else:
            n = l
        return max(0, min(n, m) - len([s for s in self.songs.values() if s.added]))

    def _toc_room(self, arch):
        """free entries in the table of an archive (None: archive missing); with settings['grow_toc'] the table may grow"""
        path = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
        if not os.path.isfile(path):
            return None
        import rpf3_add
        grow = bool(self.settings.get('grow_toc'))
        cache = self.__dict__.setdefault('_toc_room_cache', {})
        if (path, grow) not in cache:
            cache[(path, grow)] = rpf3_add.toc_room(path, grow)
        return cache[(path, grow)]

    def add_song(self, artist, title, genre, audio, eor_audio=None):
        """Stage a NEW song at the end of a genre list. Audio is encoded and all game objects are created at build time."""
        import addsong
        artist, title = ' '.join(artist.split()), ' '.join(title.split())
        if not addsong.alnum_upper(artist) or not addsong.alnum_upper(title):
            raise EditError('Artist and title must contain letters or digits')
        if genre not in GENRE_ID:
            raise EditError('Unknown genre')
        if not os.path.isfile(audio):
            raise EditError('File not found: %s' % audio)
        if eor_audio and not os.path.isfile(eor_audio):
            raise EditError('File not found: %s' % eor_audio)
        if self.add_capacity() <= 0:
            raise EditError('No room left in the archive tables of xarchive_music.rpf / xarchive_audlo.rpf for another song')
        wave = addsong.wave_name(artist, title)
        if any(s.added and s.wave == wave for s in self.songs.values()) or \
                any(addsong.object_name(g, artist, title) in self.dirs for g, _, _ in GENRES) or \
                ('%08x' % rpf3.joaat(wave)) in {'%08x' % e.hash for e in (self.music.files() if self.music else [])}:
            raise EditError('A song with this artist and title already exists')
        s = Song('ADDED_' + wave)
        s.added, s.artist, s.name, s.manager = True, artist, title, MANAGERS[0]
        s.genre, s.orig_genre = genre, None
        s.title = s.orig_title = (addsong.translit_cyr(artist) + ' - ' + addsong.translit_cyr(title)).upper()[:TITLE_MAX]
        s.title_max = TITLE_MAX
        s.wave, s.file_hash, s.new_audio = wave, '%08x' % rpf3.joaat(wave), audio
        s.eor_audio = eor_audio or None
        s.bank = self.banks[0]
        self.songs[s.obj] = s
        self.order[MANAGERS[0]][genre].append(s.obj)
        return s

    def game_obj(self, s):
        """Name of the song object in game.dat (new songs: derived from genre / artist / title)."""
        if s.added:
            import addsong
            return addsong.object_name(s.genre, s.artist, s.name)
        return s.obj

    def eor_name_of(self, s):
        """name of the end-of-race object of a song (None: the song has no end-of-race clip)"""
        if s.added:
            return 'EOR_' + s.wave
        m = self.__dict__.get('_eor_map')
        if m is None:
            self._sounds_maps()
            m = self._eor_map
        return m.get(s.obj)

    def _sounds_maps(self):
        """read sounds.dat once: end-of-race objects + list members, garage objects + garage list members"""
        import addsong
        _, plain = self._sounds_entry()
        sd = sounds_rebuild.SoundsDat(plain)
        base = [x for x in self.songs.values() if not x.added]
        self._eor_map = addsong.eor_names_for(sd, base)
        self._eor_members = addsong.eor_list_members(sd)
        self._garage_map = addsong.garage_names_for(sd, base)
        self._garage_members = addsong.garage_list_members(sd)
        self._eor_singles = addsong.eor_single_targets(sd)

    def garage_plan(self):
        """(garage objects of removed songs to take out of GARAGE_MUSIC_MASTER, garage objects of kept songs to put back)
        or None. Like the end-of-race lists, the garage list still names removed songs (and would play them, or the
        silence they get when their space is freed). The list keeps its size (entries get kept songs instead) - only done
        while more than 64 different songs stay in it (the list remembers the last 64 it played)."""
        songs = [s for s in self.songs.values() if not s.added]
        if not any(s.removed or s.orig_removed for s in songs):
            return None
        if '_garage_map' not in self.__dict__:
            self._sounds_maps()
        gm, members = self._garage_map, self._garage_members
        drop = {g for s in songs if s.removed and (g := gm.get(s.obj)) and g in members}
        back = [g for s in songs if not s.removed and (g := gm.get(s.obj)) and g not in members]
        kept = (members - drop) | set(back)
        new_songs = sum(1 for s in self.songs.values() if s.added)
        if drop and len(kept) + new_songs <= 64:
            drop = set()                                  # too few songs left for the list's memory: leave it as it is
        return (drop, back) if drop or back else None

    def eor_lists_plan(self):
        """(names of end-of-race objects to drop from the EOR lists, [(name, genre)] to add) or None: the clips of removed
        songs stay in the game's end-of-race lists (removing a song only edits the playlists), so they would keep playing."""
        songs = [s for s in self.songs.values() if not s.added]
        if not any(s.removed or s.orig_removed for s in songs):
            return None
        if '_eor_map' not in self.__dict__:
            self._sounds_maps()
        members = self._eor_members
        # only clips of songs that are in the playlists stay - also clips no song could be matched to (they belong to
        # removed songs: the names of the clips do not always follow the songs, e.g. EOR_THE_EQULIZERS_WIDE_AWAKE)
        kept = {en for s in songs if not s.removed and (en := self.eor_name_of(s))}
        drop = {n for n in members | self._eor_singles if n and n not in kept} if kept else set()
        missing = [(en, s.genre) for s in songs if not s.removed and (en := self.eor_name_of(s)) and en not in members]
        return (drop, missing) if drop or missing else None

    def eor_original_bytes(self, s):
        """(raw XMA stream data, object name) of the end-of-race clip a song has right now in the archives (before any
        draft change); EditError if it has none there (a new, not yet built song, or a base song without a clip)."""
        en = self.eor_name_of(s)
        if not en:
            raise EditError('This song has no end-of-race clip')
        h = rpf3.joaat(en)
        for arch in ('audlo', 'audio'):
            path = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
            if not os.path.isfile(path):
                continue
            r = rpf3.RPF3(path)
            try:
                e = next((x for x in r.files() if x.hash == h), None)
                if e:
                    return r.read(e), en
            finally:
                r.f.close()
        raise EditError('End-of-race clip "%s" not found in the archives' % en)

    def set_eor_audio(self, obj, path):
        """Use a file as the end-of-race clip of a song (None: automatic / original). Existing songs need to have a clip."""
        s = self.songs[obj]
        if path is not None:
            if not os.path.isfile(path):
                raise EditError('File not found: %s' % path)
            if not s.added and not self.eor_name_of(s):
                raise EditError('This song has no end-of-race clip that could be replaced')
        s.eor_audio = path

    def _eor_source(self, s):
        """(file, chosen by the user?) the end-of-race clip is made from, or None (keep the original clip)"""
        if s.eor_audio:
            return s.eor_audio, True
        if s.new_audio and not s.audio_missing and (s.added or s.audio_freed or self.settings.get('eor_follow_audio', True)):
            return s.new_audio, False              # (a lost song whose audio is restored keeps its clip; a freed one's is silent)
        return None

    def needs_encoder(self):
        """encoding is needed (new / replaced audio or clips, or the 1 s of silence for freed songs)"""
        return any(s.new_audio or s.eor_audio for s in self.songs.values()) or bool(self.free_removed and self.freeable()) \
            or bool(self.hangout_plan())

    def set_audio(self, obj, path):
        s = self.songs[obj]
        if not s.file_hash or not self.music:
            raise EditError('This song has no audio file in xarchive_music.rpf, its audio cannot be replaced')
        if not os.path.isfile(path):
            raise EditError('File not found: %s' % path)
        s.new_audio = path
        s.copy_from = None

    # ---------------------------------------------------------------- build
    def _sounds_entry(self):
        se = next((e for e in self.cache.files() if e.hash == SOUNDS_DAT_HASH and e.kind == 'zlib'), None)
        if not se:
            raise EditError('sounds.dat not found in cache.rpf')
        return se, zlib.decompressobj(-15).decompress(self.cache.read(se))

    def build_patches(self):
        """-> {cache TOC index: new plain bytes} for everything that changed (the project itself is not modified)."""
        gd = bytearray(self.gd0)
        added = [s for s in self.songs.values() if s.added]
        resized = {}
        for mgr in MANAGERS:
            new = bytearray()
            for g in self.group_order[mgr]:
                lst = self.order[mgr][g]
                new.append(len(lst))
                for n in lst:
                    new += struct.pack('>I', rpf3.joaat(self.game_obj(self.songs[n])))
            a, b = self.group_span[mgr]
            if len(new) == b - a:
                gd[a:b] = new
            else:
                resized[mgr] = (a, b, bytes(new))
        for s in self.songs.values():
            if not s.added:
                gd[s.gid_pos] = GENRE_ID[s.genre]
        patches, tmpl = {}, {}
        lists_plan = self.eor_lists_plan()
        garage = self.garage_plan()
        if added or lists_plan or garage:
            import addsong
            se, sounds_plain = self._sounds_entry()
            sd = sounds_rebuild.SoundsDat(sounds_plain)
            if added:
                tmpl = addsong.pick_templates(self, sd, {s.genre for s in added})
        if resized or added:                            # object sizes change -> lay the whole file out again
            g = gamedat_rebuild.GameDat(bytes(gd))
            for mgr, (a, b, new) in resized.items():
                start = self.dirs[mgr][0] + gamedat_rebuild.SHIFT     # real start of the object in the original
                real = g.obj[mgr]
                g.replace(mgr, real[:a - start] + new + real[b - start:])
            eor_left = (self._toc_room('audio') or 0) if self.settings.get('eor', True) else 0
            for s in added:
                ts, info = tmpl[s.genre]
                obj, V = self.game_obj(s), s.wave
                key = addsong.music_key(ts, s.artist, s.name)
                names, s.eor = addsong.sounds_apply(sd, info, obj, V, V, boost=bool(self.settings.get('test_boost')),
                                                    want_eor=eor_left > 0)
                eor_left -= 1 if s.eor else 0
                addsong.gamedat_add_object(g, ts.obj, obj, key, names[0])
                s.music_key = key
                s.gid_pos = None
            gd = bytearray(g.serialize())
        changed = bool(lists_plan) and addsong.retarget_eor(sd, lists_plan[0], lists_plan[1])
        changed = (bool(garage) and addsong.retarget_garage(sd, garage[0], garage[1])) or changed
        if changed or added:
            patches[se.idx] = sd.serialize()
        if bytes(gd) != self.gd0:
            patches[self.game_dat_idx] = bytes(gd)
        banks = {id(b): TextBank(bytes(b.plain), b.entries) for b in self.banks}      # work on copies
        touched = set()
        for s in self.songs.values():
            if s.added:
                ts = tmpl[s.genre][0]
                banks[id(s.bank)].add(s.music_key, s.title, ts.music_key)
                touched.add(id(s.bank))
            elif s.title != s.orig_title:
                banks[id(s.bank)].set(s.key_hash, s.title)
                touched.add(id(s.bank))
        for g, names in self.genre_new.items():
            banks[id(self.banks[0])].set(rpf3.joaat(GENRE_LABEL_KEY[g]), None, per_block=names)
            touched.add(id(self.banks[0]))
        for b in self.banks:
            if id(b) in touched:
                for idx in b.entries:
                    patches[idx] = bytes(banks[id(b)].plain)
        return patches

    def build_cache(self, out_path, progress=lambda m: None):
        src = os.path.join(self.game_dir, 'xarchive_cache.rpf')
        if os.path.abspath(out_path).lower() == os.path.abspath(src).lower():
            raise EditError('Refusing to overwrite the original xarchive_cache.rpf')
        patches = self.build_patches()
        if not patches:
            raise EditError('There are no changes to build')
        progress('Writing %s (%d file(s) replaced)...' % (os.path.basename(out_path), len(patches)))
        cache_patch.patch_cache(src, out_path, patches, log=lambda m: None)
        return patches

    # ---- audio replacement ------------------------------------------------------------------
    def _channel_ids(self, s, orig_header):
        """(id_left, id_right) of the original track: as sounds.dat refers to them, else taken from its header, the side
        decided by the wave name (the header lists the two ids sorted, not left first)."""
        if s.stream_ids and not s.added:
            return tuple(s.stream_ids)
        if s.added or orig_header is None:         # new file (a new song, or restored audio): ids follow from the wave name
            return rpf3.joaat(s.wave + '_LEFT'), rpf3.joaat(s.wave + '_RIGHT')
        w = struct.unpack('>64I', orig_header[:0x100])
        ids = {w[14], w[18]}
        names = [n for n in (s.wave, (s.wave or '').replace('SC_', '', 1)) if n]
        for n in names:
            l, r = rpf3.joaat(n + '_LEFT'), rpf3.joaat(n + '_RIGHT')
            if {l, r} == ids:
                return l, r
            if r in ids:                       # e.g. only one of them is recognised
                return next(iter(ids - {r})), r
            if l in ids:
                return l, next(iter(ids - {l}))
        return min(ids), max(ids)              # unknown naming: assume the smaller id is the left channel

    def _check_encoder(self):
        """encoder / ffmpeg / (macOS, Linux) wine are there; sets the program the encoder is started through"""
        import shlex
        import xma_encode
        st = self.settings
        if not st.get('encoder') or not os.path.isfile(st['encoder']):
            raise EditError('Set the path of the XMA encoder (.exe) first (Audio settings...)')
        if not st.get('ffmpeg') or not os.path.isfile(st['ffmpeg']):
            raise EditError('ffmpeg not found (Audio settings...)')
        runner = shlex.split(st.get('encoder_runner') or '')
        if runner and not (os.path.isfile(runner[0]) or shutil.which(runner[0])):
            raise EditError('"%s" not found - the XMA encoder is a Windows program; on macOS / Linux it needs Wine '
                            '(Audio settings..., "Start the encoder through")' % runner[0])
        xma_encode.RUNNER = runner

    def _encode(self, s, quality, orig_header, progress):
        import xma_encode
        st = self.settings
        self._check_encoder()
        try:
            data, n = xma_encode.encode_track(s.wave or s.obj, s.new_audio, st['encoder'], quality, st['ffmpeg'],
                                              progress=progress, channel_ids=self._channel_ids(s, orig_header))
        except (xma_encode.XmaError, ValueError) as e:                 # the error names the song
            raise EditError('%s: %s' % (s.title, e))
        return data

    def _eor_ids(self, name, orig_header):
        l, r = rpf3.joaat(name + '_LEFT'), rpf3.joaat(name + '_RIGHT')
        if orig_header:
            w = struct.unpack('>64I', orig_header[:0x100])
            if {w[14], w[18]} != {l, r}:
                return min(w[14], w[18]), max(w[14], w[18])
        return l, r

    def _eor_data(self, s, progress, orig_header=None):
        """Encoded end-of-race clip of a song: a file chosen by the user, else the loudest excerpt of its (new) audio.
        The same data goes into audio.rpf and audlo.rpf (like the originals)."""
        import eor_clip
        import xma_encode
        st = self.settings
        src, user = self._eor_source(s)
        name = self.eor_name_of(s)
        key = (name, src, user, st['q_music'], st.get('eor_seconds'))
        cache = self.__dict__.setdefault('_eor_cache', {})
        if key in cache:
            return cache[key]
        self._check_encoder()
        tmp =os.path.join(tempfile.gettempdir(), 'mcla_eor_%s.wav' % name)
        try:
            if user:
                progress('End-of-race clip of "%s": file %s' % (s.title, os.path.basename(src)))
                eor_clip.make_wav(st['ffmpeg'], src, tmp, user_clip=True)
            else:
                start, length = eor_clip.auto_excerpt(st['ffmpeg'], src, float(st.get('eor_seconds') or eor_clip.DEFAULT_SECONDS))
                progress('End-of-race clip of "%s": automatic excerpt, %.0f s from %d:%02d' % (s.title, length, start // 60, start % 60))
                eor_clip.make_wav(st['ffmpeg'], src, tmp, start, length, fade_out=s.added)
            data, _ = xma_encode.encode_track(name, tmp, st['encoder'], st['q_music'], st['ffmpeg'], progress=progress,
                                              channel_ids=self._eor_ids(name, orig_header))
        except (xma_encode.XmaError, ValueError) as e:
            raise EditError('%s (end-of-race clip): %s' % (s.title, e))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        cache[key] = data
        return data

    def _restore_eor(self, songs, audlo_entries, progress, keep):
        """Songs whose audio is being restored: their end-of-race clips may be missing from audlo too. xarchive_audio.rpf
        holds identical clips, so they are copied from there (a clip that is being replaced is encoded instead).
        Returns [(dir hash, file hash, path)] to add to audlo.rpf."""
        import addsong
        out = []
        apath = os.path.join(self.game_dir, 'xarchive_audio.rpf')
        ra = rpf3.RPF3(apath) if os.path.isfile(apath) else None
        try:
            ae = {'%08x' % e.hash: e for e in ra.files()} if ra else {}
            for s in songs:
                en = self.eor_name_of(s)
                h = '%08x' % rpf3.joaat(en) if en else None
                if not h or h in audlo_entries:
                    continue
                if self._eor_source(s):
                    out.append((addsong.EOR_DIR, int(h, 16), keep(self._eor_data(s, progress), 'relinkeor_%s.bin' % h)))
                elif h in ae:
                    progress('audlo: copying the end-of-race clip of "%s" from xarchive_audio.rpf' % s.title)
                    out.append((addsong.EOR_DIR, int(h, 16), keep(ra.read(ae[h]), 'relinkeor_%s.bin' % h)))
        finally:
            if ra:
                ra.f.close()
        return out

    def _audio_jobs(self):
        """(songs to encode, new songs, clips to replace, removed songs to free, songs to copy from another folder)"""
        freeing = self.freeable() if self.free_removed else []
        skip = {s.obj for s in freeing}
        songs = [s for s in self.audio_songs() if not s.added and s.obj not in skip]
        added = [s for s in self.songs.values() if s.added]
        eor_repl = [s for s in self.songs.values() if not s.added and s.obj not in skip and self._eor_source(s)
                    and self.eor_name_of(s)]
        copies = [s for s in self.songs.values() if s.copy_from and not s.removed and not s.new_audio]
        return songs, added, eor_repl, freeing, copies

    def _stub(self, name, ids):
        """a 1 s silent track (song or end-of-race clip) with the stream ids of the file it replaces"""
        import xma_encode
        import xma_track
        pk = self.__dict__.get('_silence')
        if pk is None:
            self._check_encoder()
            pk = self._silence = xma_encode.silence_packets(self.settings['encoder'], self.settings['ffmpeg'], STUB_SECONDS)
        return xma_track.build_track(name, pk[0], pk[0], pk[1], channel_ids=ids)

    def _other_entries(self, folder, arch):
        """{file hash: entry} of an archive of another game folder (read once)"""
        key = (os.path.abspath(folder).lower(), arch)
        c = self.__dict__.setdefault('_other_cache', {})
        if key not in c:
            c[key] = {}
            path = os.path.join(folder, 'xarchive_%s.rpf' % arch)
            if os.path.isfile(path):
                r = rpf3.RPF3(path)
                try:
                    c[key] = {e.hash: e for e in r.files()}
                finally:
                    r.f.close()
        return c[key]

    def _copy_jobs(self, s, arch):
        """[(dir hash, file hash, entry of the other archive)] of a song that takes its audio from another folder"""
        import addsong
        ents = self._other_entries(s.copy_from, arch)
        out = []
        if arch != 'audio' and int(s.file_hash, 16) in ents:
            out.append((addsong.MUSIC_DIR, int(s.file_hash, 16), ents[int(s.file_hash, 16)]))
        en = self.eor_name_of(s)
        if arch != 'music' and en and rpf3.joaat(en) in ents:
            out.append((addsong.EOR_DIR, rpf3.joaat(en), ents[rpf3.joaat(en)]))
        return out

    def _copy_into(self, copies, arch, have, repl, adds, keep, progress):
        """copy the files of `copies` (songs with copy_from) out of the other folders' `arch` archive"""
        by_folder = {}
        for s in copies:
            by_folder.setdefault(s.copy_from, []).append(s)
        for folder, lst in by_folder.items():
            path = os.path.join(folder, 'xarchive_%s.rpf' % arch)
            if not os.path.isfile(path):
                continue
            r = rpf3.RPF3(path)
            try:
                for s in lst:
                    for d, h, e in self._copy_jobs(s, arch):
                        progress('%s: copying "%s" from %s' % (arch, s.title, folder))
                        p = keep(r.read(e), 'copy_%s_%08x.bin' % (arch, h))
                        if (d, h) in have:                   # have: (dir hash, file hash) of the archive written
                            repl[(d, h)] = p
                        else:
                            adds.append((d, h, p))
            finally:
                r.f.close()

    def check_sizes(self, progress=lambda m: None):
        """Before the long encoding: estimate music / audlo / audio.rpf after the build (they are written compactly, see
        rpf3_rebuild) from the length of the new audio and the bytes per second of the tracks already in each archive.
        EditError when one would not fit into the 2 GB of the RPF3 format; the exact check is made again when writing.
        Returns {archive: estimated bytes}."""
        import addsong
        import rpf3_rebuild
        progress('Checking the size of the archives...')
        ff = self.settings.get('ffmpeg')
        durs = {}

        def dur(path, cap=None):
            if path not in durs:
                durs[path] = _duration(ff, path) or 240.0
            return min(durs[path], cap) if cap else durs[path]
        eor_len = float(self.settings.get('eor_seconds') or 20)

        def eor_secs(s):
            src, user = self._eor_source(s)
            return dur(src, 60.0) if user else min(eor_len, dur(src))
        songs, added, eor_repl, freeing, copies = self._audio_jobs()
        grow = bool(self.settings.get('grow_toc'))
        rate, out, errors = {}, {}, []
        for arch in ('music', 'audlo', 'audio'):
            src = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
            if not os.path.isfile(src):
                continue
            r = rpf3.RPF3(src)
            try:
                have = {e.hash: e for e in _by_hash(r.files(), (addsong.MUSIC_DIR, addsong.EOR_DIR)).values()}
            finally:
                r.f.close()
            key = lambda h: (have[h].parent, h)                     # the entry the build replaces (see _by_hash)
            pairs = [(have[int(s.file_hash, 16)].data_size, s.seconds) for s in self.songs.values()
                     if s.file_hash and s.seconds and int(s.file_hash, 16) in have]
            if pairs:
                rate[arch] = 1.05 * sum(a for a, _ in pairs) / sum(b for _, b in pairs)      # 5 % margin
            song_rate = rate.get(arch, rate.get('music', 45000))
            clip_rate = rate.get('music', song_rate)                 # clips are encoded at the music quality
            repl, adds = {}, []
            if arch != 'audio':
                for s in songs:
                    h = int(s.file_hash, 16)
                    size = int(dur(s.new_audio) * song_rate)
                    if h in have:
                        repl[key(h)] = size
                    else:
                        adds.append((addsong.MUSIC_DIR, rpf3.joaat(s.wave), size))
                for s in added:
                    adds.append((addsong.MUSIC_DIR, rpf3.joaat(s.wave), int(dur(s.new_audio) * song_rate)))
            if arch != 'music':
                for s in eor_repl:
                    h = rpf3.joaat(self.eor_name_of(s))
                    if h in have:
                        repl[key(h)] = int(eor_secs(s) * clip_rate)
                for s in added:
                    if s.eor:
                        adds.append((addsong.EOR_DIR, rpf3.joaat('EOR_' + s.wave), int(eor_secs(s) * clip_rate)))
            for s in freeing:                                    # removed songs: 1 s of silence instead of their audio
                if arch != 'audio' and int(s.file_hash, 16) in have:
                    repl[key(int(s.file_hash, 16))] = STUB_BYTES
                en = self.eor_name_of(s) if arch != 'music' else None
                if en and rpf3.joaat(en) in have:
                    repl[key(rpf3.joaat(en))] = STUB_BYTES
            for s in copies:
                for d, h, e in self._copy_jobs(s, arch):
                    if h in have and have[h].parent == d:
                        repl[(d, h)] = e.data_size
                    else:
                        adds.append((d, h, e.data_size))
            if arch == 'audio':                                  # hangout music: ~17 KB per second of mono 48 kHz
                secs = float(self.settings.get('hangout_seconds') or HANGOUT_SECONDS)
                for slot in self.hangout_plan():
                    repl[(HANGOUT_DIR, rpf3.joaat(slot))] = int(secs * 17000) + 0x8000
            if not repl and not adds:
                continue
            try:
                size = rpf3_rebuild.planned_size(src, repl, adds, grow)
            except rpf3_rebuild.RpfRebuildError as e:
                raise EditError('xarchive_%s.rpf: %s' % (arch, e))
            out[arch] = size
            progress('xarchive_%s.rpf: about %.2f of at most 2.00 GB' % (arch, size / 2 ** 30))
            if size > rpf3_rebuild.LIMIT:
                per = int(240 * (song_rate if arch != 'audio' else 0) + (eor_len * clip_rate if arch != 'music' else 0)) or 1
                errors.append('xarchive_%s.rpf would be about %.2f GB (at most 2.00 GB fit) - about %d song(s) too many '
                              '(one new song takes about %.1f MB there)' % (arch, size / 2 ** 30,
                                                                            -(-(size - rpf3_rebuild.LIMIT) // per), per / 2 ** 20))
        if errors:
            raise EditError('The archives would be too big for the game (RPF3 format limit):\n\n' + '\n'.join(errors) +
                            '\n\nAdd or replace fewer songs, remove some you added (their audio stays in the archive until '
                            'then), or lower the quality for audlo.rpf in Audio settings.')
        return out

    def build_archives(self, out_dir, progress=lambda m: None):
        """Write every archive that has changes into out_dir (never the original folder). Returns the file names."""
        if os.path.abspath(out_dir).lower() == os.path.abspath(self.game_dir).lower():
            raise EditError('Refusing to write into the original game folder')
        os.makedirs(out_dir, exist_ok=True)
        written = []
        if self.build_patches():
            self.build_cache(os.path.join(out_dir, 'xarchive_cache.rpf'), progress)
            written.append('xarchive_cache.rpf')
        songs, added, eor_repl, freeing, copies = self._audio_jobs()
        hplan = self.hangout_plan()
        if songs or added or eor_repl or freeing or copies or hplan:
            import addsong
            import rpf3_rebuild
            self.check_sizes(progress)                   # before the long encoding: would an archive exceed 2 GB?
            grow = bool(self.settings.get('grow_toc'))
            tmp = tempfile.mkdtemp(prefix='mcla_tracks_')

            def keep(data, name):
                p = os.path.join(tmp, name)
                with open(p, 'wb') as f:
                    f.write(data)
                return p

            def write(src, name, repl, adds):
                # the archive is written anew and compactly (old slots of replaced files are not carried over)
                dst = os.path.join(out_dir, name)
                progress('Writing %s...' % name)
                try:
                    rpf3_rebuild.rebuild(src, dst, repl, adds, grow, progress)
                except rpf3_rebuild.RpfTooBig as e:
                    raise EditError('%s: %s. Add or replace fewer songs (or lower the quality for audlo.rpf in Audio '
                                    'settings).' % (name, e))
                except rpf3_rebuild.RpfRebuildError as e:
                    raise EditError('%s: %s' % (name, e))
                written.append(name)
            try:
                for arch, q in (('music', self.settings['q_music']), ('audlo', self.settings['q_lo'])):
                    src = os.path.join(self.game_dir, 'xarchive_%s.rpf' % arch)
                    if not os.path.isfile(src):
                        continue
                    repl, adds, relink = {}, [], []
                    r = rpf3.RPF3(src)
                    try:
                        entries = _by_hash(r.files(), (addsong.MUSIC_DIR, addsong.EOR_DIR))
                        for s in songs:
                            e = entries.get(s.file_hash)
                            if not e:
                                relink.append(s)                   # audio whose file is not in this archive (lost)
                                continue
                            progress('%s: encoding "%s" (%s, quality %d)...' % (arch, s.title, os.path.basename(s.new_audio), q))
                            repl[(e.parent, e.hash)] = keep(self._encode(s, q, r.read(e, 0x100), progress), '%s_%s.bin' % (arch, s.file_hash))
                        if arch == 'audlo':                        # existing end-of-race clips that are replaced
                            for s in eor_repl:
                                e = entries.get('%08x' % rpf3.joaat(self.eor_name_of(s)))
                                if e:
                                    repl[(e.parent, e.hash)] = keep(self._eor_data(s, progress, r.read(e, 0x100)), 'eor_%s_%08x.bin' % (arch, e.hash))
                        if freeing:
                            progress('%s: freeing the space of %d removed song(s)...' % (arch, len(freeing)))
                        for s in freeing:                          # removed songs: 1 s of silence instead of their audio
                            e = entries.get(s.file_hash) if not s.audio_freed else None
                            if e:
                                repl[(e.parent, e.hash)] = keep(self._stub(s.wave, self._channel_ids(s, r.read(e, 0x100))),
                                                    'stub_%s_%s.bin' % (arch, s.file_hash))
                            en = self.eor_name_of(s) if arch == 'audlo' else None
                            e = entries.get('%08x' % rpf3.joaat(en)) if en and self._clip_seconds(en) >= FREED_MAX_SECONDS else None
                            if e:
                                repl[(e.parent, e.hash)] = keep(self._stub(en, self._eor_ids(en, r.read(e, 0x100))),
                                                    'stubeor_%s_%08x.bin' % (arch, e.hash))
                    finally:
                        r.f.close()
                    self._copy_into(copies, arch, {(e.parent, e.hash) for e in entries.values()}, repl, adds, keep, progress)
                    for s in added:
                        progress('%s: encoding the new song "%s" (%s, quality %d)...' % (arch, s.title, os.path.basename(s.new_audio), q))
                        adds.append((addsong.MUSIC_DIR, rpf3.joaat(s.wave), keep(self._encode(s, q, None, progress), 'new_%s_%s.bin' % (arch, s.wave))))
                    if arch == 'audlo':
                        for s in added:
                            if s.eor:
                                adds.append((addsong.EOR_DIR, rpf3.joaat('EOR_' + s.wave), keep(self._eor_data(s, progress), 'neweor_%s.bin' % s.wave)))
                    for s in relink:
                        progress('%s: restoring the audio of "%s" (%s, quality %d)...' % (arch, s.title, os.path.basename(s.new_audio), q))
                        adds.append((addsong.MUSIC_DIR, rpf3.joaat(s.wave), keep(self._encode(s, q, None, progress), 'relink_%s_%s.bin' % (arch, s.wave))))
                    if arch == 'audlo' and relink:
                        adds += self._restore_eor(relink, entries, progress, keep)
                    if repl or adds:
                        write(src, 'xarchive_%s.rpf' % arch, repl, adds)
                eor_songs = [s for s in added if s.eor]
                asrc = os.path.join(self.game_dir, 'xarchive_audio.rpf')
                if (eor_songs or eor_repl or freeing or copies or hplan) and os.path.isfile(asrc):
                    repl = {}
                    ra = rpf3.RPF3(asrc)
                    try:
                        entries = _by_hash(ra.files(), (addsong.EOR_DIR,))
                        hng = {e.hash: e for e in ra.files() if e.parent == HANGOUT_DIR}
                        for i, (slot, src) in enumerate(sorted(hplan.items())):
                            e = hng.get(rpf3.joaat(slot))
                            if not e:
                                raise EditError('Hangout music file %s not found in xarchive_audio.rpf' % slot)
                            progress('Hangout music %d/%d: %s' % (i + 1, len(hplan), self.hangout_label(src)))
                            repl[(HANGOUT_DIR, e.hash)] = keep(self._hangout_data(slot, src, ra.read(e), tmp),
                                                               'hng_%s.bin' % slot)
                        for s in eor_repl:
                            e = entries.get('%08x' % rpf3.joaat(self.eor_name_of(s)))
                            if e:
                                repl[(e.parent, e.hash)] = keep(self._eor_data(s, progress, ra.read(e, 0x100)), 'eor_audio_%08x.bin' % e.hash)
                        for s in freeing:
                            en = self.eor_name_of(s)
                            e = entries.get('%08x' % rpf3.joaat(en)) if en and self._clip_seconds(en) >= FREED_MAX_SECONDS else None
                            if e:
                                repl[(e.parent, e.hash)] = keep(self._stub(en, self._eor_ids(en, ra.read(e, 0x100))), 'stubeor_audio_%08x.bin' % e.hash)
                    finally:
                        ra.f.close()
                    adds = [(addsong.EOR_DIR, rpf3.joaat('EOR_' + s.wave), keep(self._eor_data(s, progress), 'neweor_%s.bin' % s.wave))
                            for s in eor_songs]
                    self._copy_into(copies, 'audio', {(e.parent, e.hash) for e in entries.values()}, repl, adds, keep, progress)
                    if repl or adds:
                        write(asrc, 'xarchive_audio.rpf', repl, adds)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if not written:
            raise EditError('There are no changes to build')
        return written

    def _collect_changes(self):
        """{song object: {'audio_source':.., 'eor': {'kind':.., 'source':..}}} for every song whose audio and/or
        end-of-race clip is not the original one - the current draft, plus whatever a previous save already recorded
        for songs not touched again. Written next to a built/saved folder so a later reopen can still show it (see
        `_read_changes` / `_changes_path` - nothing in the archives themselves says a clip is not the original)."""
        out = {}
        freed = {s.obj for s in self.freeable()} if self.free_removed else set()
        for s in self.songs.values():
            if (s.copy_from and not s.new_audio) or s.obj in freed:
                continue                             # original audio again / silent stub: nothing of yours left in it
            entry = {}
            audio_src = s.new_audio or s.audio_loaded
            if audio_src:
                entry['audio_source'] = audio_src
            eor_src = self._eor_source(s)
            if eor_src:
                entry['eor'] = {'kind': 'custom' if eor_src[1] else 'auto',
                                 'source': eor_src[0] if eor_src[1] else None}
            elif s.eor_loaded:
                kind, src = s.eor_loaded
                entry['eor'] = {'kind': kind, 'source': src}
            if entry:
                out[self.game_obj(s)] = entry                  # the name the song has in the game after this build
        return out

    def _collect_hangout(self):
        """{slot: source} of the hangout slots that do not hold their original excerpt after this build"""
        out = {k: dict(v) for k, v in self.hangout_loaded.items()}
        for slot, src in self.hangout_plan().items():
            src = {k: v for k, v in src.items() if k != 'auto'}
            s = self.songs.get(src.get('obj')) if src.get('kind') == 'song' else None
            if s:
                src['obj'] = self.game_obj(s)
            if s and src['obj'] == dict(HANGOUT_SLOTS)[slot] and not s.new_audio and src.get('start') is None:
                out.pop(slot, None)                             # the original song again (from the archive)
            else:
                out[slot] = src
        return out

    def _write_changes(self, out_dir):
        songs = self._collect_changes()
        hangout = self._collect_hangout()
        path = _changes_path(out_dir)
        if songs or hangout:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'songs': songs, 'hangout': hangout}, f, ensure_ascii=False, indent=2,
                          sort_keys=True)
        elif os.path.isfile(path):
            os.remove(path)

    def save_in_place(self, progress=lambda m: None, keep_backup=True):
        """Write the changes into the folder that is open: the changed archives are built next to the originals
        (in a temporary sub-folder) and then replace them. The first time an archive is replaced, the previous file is kept
        as <name>.bak (a rename, instant). Files of the folder that are hard links to another game folder stay safe: a
        replaced file gets a new directory entry, the other folder keeps its own data. Returns the names of the archives."""
        tmp = os.path.join(self.game_dir, '_mcla_save_tmp')
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            written = self.build_archives(tmp, progress)
            for r in (self.cache, self.music):               # open handles would block the replacement on Windows
                if r is not None:
                    r.f.close()
            for name in written:
                src, dst = os.path.join(tmp, name), os.path.join(self.game_dir, name)
                bak = dst + '.bak'
                progress('Replacing %s...' % name)
                moved = False
                if keep_backup and os.path.exists(dst) and not os.path.exists(bak):
                    os.replace(dst, bak)
                    moved = True
                try:
                    os.replace(src, dst)
                except OSError:
                    if moved:                                # put the old file back
                        os.replace(bak, dst)
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._write_changes(self.game_dir)
        return written

    def build_folder(self, out_dir, progress=lambda m: None):
        """Complete game folder: hard links to every original file (copies when that is impossible) + patched cache."""
        if os.path.abspath(out_dir).lower() == os.path.abspath(self.game_dir).lower():
            raise EditError('Refusing to write into the original game folder')
        os.makedirs(out_dir, exist_ok=True)
        written = [w.lower() for w in self.build_archives(out_dir, progress)]
        linked = copied = 0
        for dp, _, fs in os.walk(self.game_dir):
            for f in fs:
                srcp = os.path.join(dp, f)
                rel = os.path.relpath(srcp, self.game_dir)
                if rel.lower() in written:
                    continue
                dst = os.path.join(out_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    os.remove(dst)
                try:
                    os.link(srcp, dst)
                    linked += 1
                except OSError:
                    progress('Copying %s...' % rel)
                    shutil.copy2(srcp, dst)
                    copied += 1
        self._write_changes(out_dir)
        return linked, copied

    # ---------------------------------------------------------------- audio helpers
    def song_bytes(self, s):
        if not (self.music and s.file_hash):
            raise EditError('No audio file for this song (xarchive_music.rpf missing or song not matched)')
        e = next((x for x in self.music.files() if '%08x' % x.hash == s.file_hash), None)
        if not e:
            raise EditError('The audio file of this song (%s) is missing from xarchive_music.rpf - the song is listed in the '
                            'game data but its sound was lost. Use "Restore missing audio..." to give it its audio again.'
                            % s.file_hash)
        return self.music.read(e)

    def missing_audio(self):
        """songs in the playlists that have no real audio: the file is not in xarchive_music.rpf (lost), or it is the
        silent stub of a freed removed song that was brought back (removed songs without audio do not matter)"""
        return [s for s in self.songs.values() if (s.audio_missing or s.audio_freed) and not s.removed]

    def match_audio_files(self, infos):
        """infos: [(path, artist, title)] -> ({song obj: path}, [paths without a song]). Matches files to the songs whose
        audio is missing: by the wave name the artist/title would give, else by the letters/digits of the title (also of the
        file name), else by close similarity; every song and every file is used at most once."""
        import addsong
        norm = lambda t: re.sub(r'[^A-Z0-9]', '', addsong.translit_cyr(t).upper())
        songs = [s for s in self.missing_audio() if not s.new_audio and not s.copy_from]
        cand = []                                            # (score, song obj, path)
        for path, artist, title in infos:
            fname = os.path.splitext(os.path.basename(path))[0]
            keys = {norm(artist + title), norm(fname), norm(title)} - {''}
            try:
                wave = addsong.wave_name(artist, title)
            except Exception:
                wave = None
            for s in songs:
                sk = norm(s.title)
                if wave and wave == s.wave:
                    score = 2.0
                elif sk in keys:
                    score = 1.5
                else:
                    score = max((difflib.SequenceMatcher(None, sk, k).ratio() for k in keys), default=0)
                    if any(len(k) > 8 and (k in sk or sk in k) for k in keys):
                        score = max(score, 0.9)
                    if score < 0.8:
                        continue
                cand.append((score, s.obj, path))
        used_s, used_p, out = set(), set(), {}
        for score, obj, path in sorted(cand, key=lambda c: -c[0]):
            if obj not in used_s and path not in used_p:
                out[obj] = path
                used_s.add(obj)
                used_p.add(path)
        return out, [p for p, _, _ in infos if p not in used_p]

    def eor_preview_wav(self, s, ffmpeg):
        """WAV file of the end-of-race clip the song has right now: kept original, a file chosen for it, or the
        automatic excerpt of its new/replaced audio - decoded/cut with ffmpeg only, no XMA encoder needed."""
        import eor_clip
        if not ffmpeg or not os.path.isfile(ffmpeg):
            raise EditError('ffmpeg not found (Audio settings...)')
        tmpdir = os.path.join(tempfile.gettempdir(), 'mcla_gui')
        os.makedirs(tmpdir, exist_ok=True)
        wav = os.path.join(tmpdir, 'eor_preview_%s.wav' % s.obj)
        src, user = self._eor_source(s) or (None, None)
        if src is None:
            data, name = self.eor_original_bytes(s)
            raw = os.path.join(tmpdir, 'eor_preview_%s.bin' % s.obj)
            with open(raw, 'wb') as f:
                f.write(data)
            try:
                import mcla_music
                mcla_music.FFMPEG = ffmpeg
                mcla_music.convert(raw, wav, 'wav', name=name)
            finally:
                os.remove(raw)
        elif user:
            eor_clip.make_wav(ffmpeg, src, wav, user_clip=True)
        else:
            start, length = eor_clip.auto_excerpt(ffmpeg, src, float(self.settings.get('eor_seconds') or eor_clip.DEFAULT_SECONDS))
            eor_clip.make_wav(ffmpeg, src, wav, start, length, fade_out=s.added)
        return wav

    def export_csv(self, path):
        import csv
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['genre', 'position', 'source', 'title', 'song_object', 'wave', 'file_hash', 'seconds'])
            for g, _, _ in GENRES:
                ui = self.genre_ui(g)
                for i, s in enumerate(self.genre_list(g), 1):
                    w.writerow([ui, i, MANAGER_UI[s.manager], s.title, s.obj, s.wave or '', s.file_hash or '',
                                round(s.seconds, 1) if s.seconds else ''])
