"""The two versions of Midnight Club: Los Angeles the editor works with. Everything the rest of the editor does (game.dat,
sounds.dat, text banks, playlists, garage / end-of-race lists) is the same on both; they differ in:

  * where the archives are: X360 <folder>/xarchive_<name>.rpf, PS3 <folder>/PS3_GAME/USRDIR/carchive_<name>.rpf
    (lower case - RPCS3 and the console look the names up case-sensitively)
  * which archives hold the audio: X360 songs in music + audlo (a smaller copy), end-of-race clips in audlo + audio;
    PS3 songs in music only, clips in audio only
  * the text bank file hashes
  * the audio codec: X360 two mono XMA streams (external XMA encoder), PS3 two mono MP3 streams without bit reservoir
    (LAME through ffmpeg) - the track / block layout around them is the same
"""
import os
import struct


class PlatformError(ValueError):
    """encoding / decoding failed (a ValueError, like the layout errors of the track builders)"""


class X360:
    key = 'x360'
    label = 'Xbox 360'
    # (archive, settings key of its encoder quality)
    song_archives = (('music', 'q_music'), ('audlo', 'q_lo'))
    clip_archives = ('audlo', 'audio')
    clip_quality = 'q_music'
    audio_archives = ('music', 'audlo', 'audio')
    text_main = (0xa4c1e8fa, 0xac5297d5)      # identical copies of the main text bank
    text_sc = (0xda0b8b80, 0xf5560436)        # identical copies of the SC text bank
    needs_exe = True                          # the XMA encoder is a separate program
    codec = 'XMA'

    def rel(self, arch):
        return 'xarchive_%s.rpf' % arch

    def path(self, folder, arch):
        return os.path.join(folder, self.rel(arch))

    @classmethod
    def root_of(cls, folder):
        return folder if os.path.isfile(os.path.join(folder, 'xarchive_cache.rpf')) else None

    def check_encoder(self, st):
        import shlex
        import shutil
        import xma_encode
        if not st.get('encoder') or not os.path.isfile(st['encoder']):
            raise PlatformError('Set the path of the XMA encoder (.exe) first (Audio settings...)')
        if not st.get('ffmpeg') or not os.path.isfile(st['ffmpeg']):
            raise PlatformError('ffmpeg not found (Audio settings...)')
        runner = shlex.split(st.get('encoder_runner') or '')
        if runner and not (os.path.isfile(runner[0]) or shutil.which(runner[0])):
            raise PlatformError('"%s" not found - the XMA encoder is a Windows program; on macOS / Linux it needs Wine '
                                '(Audio settings..., "Start the encoder through")' % runner[0])
        xma_encode.RUNNER = runner

    def encode(self, st, name, src, quality, channel_ids, progress=lambda m: None):
        import xma_encode
        try:
            data, _ = xma_encode.encode_track(name, src, st['encoder'], quality, st['ffmpeg'], progress=progress,
                                              channel_ids=channel_ids)
        except xma_encode.XmaError as e:
            raise PlatformError(str(e))
        return data

    def silence(self, st, name, ids, seconds, cache):
        """silent track of `seconds` (the XMA packets are encoded once and reused for every stub)"""
        import xma_encode
        import xma_track
        pk = cache.get('x360_silence')
        if pk is None:
            try:
                pk = cache['x360_silence'] = xma_encode.silence_packets(st['encoder'], st['ffmpeg'], seconds)
            except xma_encode.XmaError as e:
                raise PlatformError(str(e))
        return xma_track.build_track(name, pk[0], pk[0], pk[1], channel_ids=ids)

    def decode(self, ffmpeg, data, wav, name=None):
        """track bytes -> stereo WAV (name: wave / clip name, puts the channels on the right speakers)"""
        import tempfile
        import mcla_music
        mcla_music.FFMPEG = ffmpeg
        fd, raw = tempfile.mkstemp(suffix='.bin', prefix='mcla_dec_')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            mcla_music.convert(raw, wav, 'wav', name=name)
        finally:
            try:
                os.remove(raw)
            except OSError:
                pass


class PS3:
    key = 'ps3'
    label = 'PlayStation 3'
    song_archives = (('music', 'q_mp3'),)
    clip_archives = ('audio',)
    clip_quality = 'q_mp3'
    audio_archives = ('music', 'audio')
    text_main = (0x5fd0e138, 0xe7781e54)
    text_sc = (0x306ae60f, 0xaf1b1257)
    needs_exe = False                         # MP3 is encoded by ffmpeg (LAME)
    codec = 'MP3'
    USRDIR = os.path.join('PS3_GAME', 'USRDIR')

    def rel(self, arch):
        return os.path.join(self.USRDIR, 'carchive_%s.rpf' % arch)

    def path(self, folder, arch):
        return os.path.join(folder, self.rel(arch))

    @classmethod
    def root_of(cls, folder):
        """the disc folder (the one holding PS3_GAME) for the disc folder itself, PS3_GAME or USRDIR"""
        f = os.path.abspath(folder)
        for cand in (f, os.path.dirname(f), os.path.dirname(os.path.dirname(f))):
            if os.path.isfile(os.path.join(cand, cls.USRDIR, 'carchive_cache.rpf')):
                return cand
        return None

    def check_encoder(self, st):
        if not st.get('ffmpeg') or not os.path.isfile(st['ffmpeg']):
            raise PlatformError('ffmpeg not found (Audio settings...)')

    def encode(self, st, name, src, quality, channel_ids, progress=lambda m: None):
        import mp3_encode
        progress('Encoding MP3 (LAME VBR %s, no bit reservoir)...' % quality)
        try:
            return mp3_encode.encode_track(st['ffmpeg'], src, channel_ids, quality)
        except RuntimeError as e:
            raise PlatformError(str(e))

    def silence(self, st, name, ids, seconds, cache):
        import mp3_encode
        try:
            return mp3_encode.silence_track(st['ffmpeg'], ids, seconds)
        except RuntimeError as e:
            raise PlatformError(str(e))

    def decode(self, ffmpeg, data, wav, name=None):
        import mp3_encode
        import rpf3
        if isinstance(name, tuple):                  # (left id, right id) as sounds.dat plays them
            ids = name
        else:
            ids = (rpf3.joaat(name + '_LEFT'), rpf3.joaat(name + '_RIGHT')) if name else None
        try:
            mp3_encode.decode_track(ffmpeg, data, wav, ids)
        except (RuntimeError, AssertionError) as e:
            raise PlatformError('cannot decode this track: %s' % e)


PLATFORMS = (PS3(),)          # this is the PlayStation 3 edition of the editor (the X360 editor is a separate program)


def detect(folder):
    """(game root folder, platform) or (None, None)"""
    for p in PLATFORMS:
        root = p.root_of(folder)
        if root:
            return root, p
    return None, None


def track_seconds(header):
    """length of a track from the first 0x80 bytes of its file (same header layout on both platforms)"""
    w = struct.unpack('>32I', header[:0x80])
    return w[24] / (w[26] >> 16) if w[26] >> 16 else 0.0
