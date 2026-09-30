"""Building blocks for adding NEW songs (used by mcla_model.Project.build_*; nothing here touches the game folder).

A song consists of
  game.dat     song object (genre id, hash of its SND_ object, 'Music_...' key) + entry in the base music manager
  sounds.dat   SND_<obj> (+2 wave slots), SND_<genre>_GARAGE_<name> (+2 wave slots), entry in GARAGE_MUSIC_MASTER,
               stream path 'MUSIC\\<wave>' in the string table
  text bank    display title (6 languages) + key in the key list
  music.rpf / audlo.rpf   track file whose stream ids are joaat(<wave>_LEFT / _RIGHT)
The objects of an existing base-game song of the same genre are used as the template.
End-of-race clip (when the template has one): EOR_<wave> + 2 slots in sounds.dat, entry in EOR_<GENRE>_RAND, stream path
'SPECIAL_EFFECTS_STREAM\\EOR_<wave>', a 30 s file in audio.rpf and audlo.rpf (dir 8fd900e5).
"""
import re
import struct

import mcla_model as M
import rpf3

MUSIC_DIR = 0xc94649cc
EOR_DIR = 0x8fd900e5            # audio/x360/sfx/8fd900e5 in audio.rpf and audlo.rpf: end-of-race clips
GARAGE_LIST = 'GARAGE_MUSIC_MASTER'
EOR_MASTER = 'END_OF_RACE_MUSIC_MASTER'      # 7 lists, one per genre
EOR_SINGLES = ('END_OF_RACE_MUSIC_TEMP',)     # point to one clip directly (class 6)
EOR_LIST_OF = {'ECLECTIC': 'EOR_ECLECTIC_RAND', 'ELECTRONIC': 'EOR_ELECTRONIC_RAND', 'HARD_ROCK': 'EOR_HARD_ROCK_RAND',
               'HIPHOP': 'EOR_HIP_HOP_RAND', 'ROCK': 'EOR_ROCK_RAND', 'TECHNO': 'EOR_TECHNO_RAND',
               'WESTCOASTRAP': 'EOR_WEST_COAST_RAP_RAND'}
WEIGHT_1 = struct.pack('>f', 1.0)
WEIGHT_BOOST = struct.pack('>f', 5000.0)     # test builds: makes the new entries (almost) always win the random pick


_CYR = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                ['a', 'b', 'v', 'g', 'd', 'e', 'e', 'zh', 'z', 'i', 'y', 'k', 'l', 'm', 'n', 'o', 'p', 'r', 's', 't', 'u', 'f',
                 'kh', 'ts', 'ch', 'sh', 'shch', '', 'y', '', 'e', 'yu', 'ya']))


def translit_cyr(s):
    "Cyrillic -> Latin letters (the game font has no Cyrillic glyphs); everything else is kept"
    return ''.join(_CYR.get(c.lower(), c) if c.lower() in _CYR else c for c in s).replace('  ', ' ')


def ascii_fold(s):
    """object / wave / key names are ASCII: transliterate Cyrillic, drop accents"""
    import unicodedata
    s = ''.join(_CYR.get(c, _CYR.get(c.lower(), c)) if not c.isascii() else c for c in s)
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')


def alnum_upper(s):
    return re.sub('[^A-Z0-9]', '', ascii_fold(s).upper())


def camel(s):
    return ''.join(w[:1].upper() + w[1:] for w in re.findall('[A-Za-z0-9]+', ascii_fold(s)))


def u32(d, p, e='<'):
    return struct.unpack_from(e + 'I', d, p)[0]


def wave_name(artist, title):
    return alnum_upper(artist) + '_' + alnum_upper(title)


def object_name(genre, artist, title):
    return '%s_%s' % (genre, wave_name(artist, title))


def music_key(tmpl, artist, title):
    return re.match(r'(Music_[A-Za-z0-9]+_)', tmpl.music_key).group(1) + camel(artist) + '_' + camel(title)


# --------------------------------------------------------------------------------------------- text bank
def bank_add(plain, key, text, template_key):
    """Add a text record (all 6 language blocks) + the key name; the template record supplies the record format."""
    d = bytearray(plain)
    n = u32(d, 0x34)
    p, keys = 0x38, []
    for _ in range(n):
        L = u32(d, p)
        keys.append((d[p + 4:p + 4 + L].decode('latin1'), p))
        p += 5 + L
    pos = p
    for k, kp in keys:                                            # the key list is sorted case-insensitively
        if k.lower() > key.lower():
            pos = kp
            break
    ins = struct.pack('<I', len(key)) + key.encode('latin1') + b'\0'
    d[pos:pos] = ins
    for i in range(1, 12):
        w = u32(d, 4 * i)
        if w >= pos:
            struct.pack_into('<I', d, 4 * i, w + len(ins))
    struct.pack_into('<I', d, 0x34, n + 1)

    starts = sorted({u32(d, 4 * i) for i in range(1, 12) if 0 < u32(d, 4 * i) < len(d)})
    bounds = list(zip(starts, starts[1:] + [len(d)]))
    if len(bounds) != 6:
        raise M.EditError('unexpected text bank layout (%d language blocks)' % len(bounds))
    hk, th = rpf3.joaat(key), rpf3.joaat(template_key)
    for s, e in reversed(bounds):
        blk = M.scan_block(d, s, e)
        if hk in blk:
            raise M.EditError('text key already exists')
        off, nch = blk[th]
        rec = bytes(d[off - 20:off + 2 * nch + 10])
        if rec[4:16] != b'\x01\x00\x06\x00\x00\x00nofont' or rec[-10:] != b'\x00\x00\x80\x3f\x00\x00\x80\x3f\x00\x00':
            raise M.EditError('unexpected text record format')
        new = struct.pack('<I', hk) + rec[4:16] + struct.pack('<I', len(text) + 1) + (text + '\0').encode('utf-16-le') + rec[-10:]
        d[e:e] = new
        struct.pack_into('<I', d, s, u32(d, s) + 1)               # record count of the block
        for i in range(1, 12):
            w = u32(d, 4 * i)
            if w >= e:
                struct.pack_into('<I', d, 4 * i, w + len(new))
    return bytes(d)


# --------------------------------------------------------------------------------------------- game.dat
def snd_hash_of(p, obj):
    off, size = p.dirs[obj]
    chunk = p.gd0[off + 8:off + 8 + size + 1]
    return struct.unpack('>I', re.search(rb'(.{4})\xe3\x8f\xcf\x16', chunk, re.S).group(1))[0]


def gamedat_add_object(g, tmpl_obj, obj, key, snd_name):
    """Append the song object (copy of the template's layout) to a GameDat."""
    tmpl = g.obj[tmpl_obj]
    m = re.search(rb'(.{4})\xe3\x8f\xcf\x16(.)((?:SC_)?Music_\w+)\x00', tmpl, re.S)
    if not m or m.end() != len(tmpl) or tmpl[0] != 0x23:
        raise M.EditError('unexpected song object layout')
    new = bytearray(tmpl[:m.start()])
    new[1:5] = struct.pack('>I', sum(len(n) + 1 for n in g.order))      # name-table offset of the new object
    new += struct.pack('>I', rpf3.joaat(snd_name)) + b'\xe3\x8f\xcf\x16' + bytes([len(key)]) + key.encode() + b'\0'
    g.add(obj, bytes(new))


# --------------------------------------------------------------------------------------------- sounds.dat
def eor_entry_ok(sd, H, en):
    """The end-of-race object `en` follows the usual naming scheme (EOR_<X>, EOR_<X>_EOR_<X>_LEFT / _RIGHT, wave path
    'SPECIAL_EFFECTS_STREAM/EOR_<X>', stream ids joaat(EOR_<X>_LEFT/RIGHT)); returns {'main', 'slots'} or None."""
    if not en or en not in sd.body or sd.body[en][0] != 8:
        return None
    es = [H[u32(sd.body[en], q, '>')] for q in sorted(sd.refs[en]) if u32(sd.body[en], q, '>') in H]
    if es != [en + '_' + en + '_LEFT', en + '_' + en + '_RIGHT']:
        return None
    for slot, side in zip(es, ('_LEFT', '_RIGHT')):
        b, w = sd.body[slot], sd.wrefs[slot]
        if len(w) != 1 or u32(b, w[0], '>') != rpf3.joaat('SPECIAL_EFFECTS_STREAM/' + en) \
                or u32(b, w[0] + 4, '>') != rpf3.joaat(en + side):
            return None
    return {'main': en, 'slots': es}


def find_eor_template(sd, G):
    """An end-of-race entry of the genre list EOR_<GENRE>_RAND that follows the naming scheme; it does not have to belong
    to the song that serves as template for the music objects."""
    lst = EOR_LIST_OF.get(G)
    if lst not in sd.body:
        return None
    H = {rpf3.joaat(n): n for n in sd.order}
    body, lrefs = sd.body[lst], sorted(sd.refs[lst])
    if body[lrefs[0] - 1] != len(lrefs) or any(b - a != 8 for a, b in zip(lrefs, lrefs[1:])):
        return None
    for r in lrefs:
        ok = eor_entry_ok(sd, H, H.get(u32(body, r, '>')))
        if ok:
            return {'main': ok['main'], 'slots': ok['slots'], 'list': lst}
    return None


def eor_names_for(sd, songs):
    """{song object: name of its end-of-race object} for the base songs that have a clip (names differ from the wave names,
    so the entries of the genre lists are matched by their letters and digits)."""
    import re
    norm = lambda s: re.sub('[^a-z0-9]', '', s.lower())
    H = {rpf3.joaat(n): n for n in sd.order}
    entries = {}
    for en in sd.order:                    # all end-of-race objects, also those no list points to (clips of removed songs)
        if en.startswith('EOR_') and eor_entry_ok(sd, H, en):
            entries[norm(en[4:])] = en
    import difflib
    out = {}
    later = []
    for s in songs:
        keys = []
        if s.wave:
            keys.append(norm(s.wave[3:] if s.wave.startswith('SC_') else s.wave))
        m = M.GENRE_RX.match(s.obj)
        if m:
            keys.append(norm(s.obj[len(m.group(1)) + 1:]))      # the song object without its genre (some songs have no wave)
        keys = [k for k in keys if k]
        en = next((entries[k] for k in keys if k in entries), None) or next(
            (v for key in keys for k, v in entries.items() if len(k) >= 10 and (key.startswith(k[:10]) or k.startswith(key[:10]))),
            None)
        if en:
            out[s.obj] = en
        elif keys:
            later.append((s, keys))
    # the rest by similarity (typos in the game data like EOR_THE_EQULIZERS_WIDE_AWAKE), each clip used once
    used = set(out.values())
    for s, keys in later:
        best, score = None, 0.0
        for k, v in entries.items():
            if v in used:
                continue
            r = max(difflib.SequenceMatcher(None, key, k).ratio() for key in keys)
            if r > score:
                best, score = v, r
        if best and score >= 0.85:
            out[s.obj] = best
            used.add(best)
    return out


def eor_list_members(sd):
    """names of the objects that the seven EOR_<GENRE>_RAND lists point to"""
    H = {rpf3.joaat(n): n for n in sd.order}
    return {H.get(u32(sd.body[lst], r, '>')) for lst in EOR_LIST_OF.values() if lst in sd.body for r in sd.refs[lst]}


def retarget_eor(sd, drop, missing):
    """Removed songs must not play at the end of a race. Every entry of the EOR_<GENRE>_RAND lists that belongs to a removed
    song (`drop` = names of their end-of-race objects) gets the hash of a kept clip instead - the lists keep their size
    (the game never sees a shorter list), a clip of the same list is preferred, else any kept clip. `missing` =
    [(EOR object name, genre)] of kept songs that have no entry any more (brought back after an earlier removal): they
    are added to the list of their genre. Returns True when something changed."""
    H = {rpf3.joaat(n): n for n in sd.order}
    lists = {g: lst for g, lst in EOR_LIST_OF.items() if lst in sd.body}
    changed = False
    present = eor_list_members(sd)
    for en, g in missing:
        if en not in present and g in lists and en in sd.body:
            _list_add(sd, lists[g], en, WEIGHT_1)
            present.add(en)
            changed = True
    pool = sorted(n for n in present if n and n not in drop)
    for g, lst in lists.items():
        body, refs = bytearray(sd.body[lst]), sorted(sd.refs[lst])
        names = [H.get(u32(body, r, '>')) for r in refs]
        cand = [n for n in names if n and n not in drop] or pool
        if not cand:
            continue
        i = 0
        for r, n in zip(refs, names):
            if n in drop:
                body[r:r + 4] = struct.pack('>I', rpf3.joaat(cand[i % len(cand)]))
                i += 1
                changed = True
        sd.body[lst] = bytes(body)
    # END_OF_RACE_MUSIC_TEMP points to one clip directly (it plays e.g. when the race is lost)
    for name in EOR_SINGLES:
        if name in sd.body and pool:
            body = bytearray(sd.body[name])
            for r in sd.refs[name]:
                if H.get(u32(body, r, '>')) in drop:
                    body[r:r + 4] = struct.pack('>I', rpf3.joaat(pool[0]))
                    changed = True
            sd.body[name] = bytes(body)
    return changed


def eor_single_targets(sd):
    """clips that the single-clip end-of-race objects (END_OF_RACE_MUSIC_TEMP) point to"""
    H = {rpf3.joaat(n): n for n in sd.order}
    return {H.get(u32(sd.body[n], r, '>')) for n in EOR_SINGLES if n in sd.body for r in sd.refs[n]} - {None}


def garage_list_members(sd):
    """names of the garage objects GARAGE_MUSIC_MASTER points to"""
    H = {rpf3.joaat(n): n for n in sd.order}
    return {H.get(u32(sd.body[GARAGE_LIST], r, '>')) for r in sd.refs[GARAGE_LIST]} - {None} if GARAGE_LIST in sd.body else set()


def garage_names_for(sd, songs):
    """{song object: name of its garage sound object SND_<GENRE>_GARAGE_<rest>}; <rest> is the song object without its
    genre, or (songs added by the editor) the wave name"""
    by_rest = {}
    for n in sd.order:
        if '_GARAGE_' in n and n.startswith('SND_') and sd.body[n][0] == 8:
            by_rest.setdefault(n.split('_GARAGE_', 1)[1], n)
    out = {}
    for s in songs:
        m = M.GENRE_RX.match(s.obj)
        keys = [s.obj[len(m.group(1)) + 1:]] if m else []
        if s.wave:
            keys += [s.wave, s.wave[3:] if s.wave.startswith('SC_') else s.wave]
        g = next((by_rest[k] for k in keys if k in by_rest), None)
        if g:
            out[s.obj] = g
    return out


def retarget_garage(sd, drop, back):
    """Garage list: entries of removed songs (`drop` = their garage objects) get kept songs instead - the list keeps its
    size, the kept songs are spread evenly; `back` = garage objects of songs that are in the playlists again, added to
    the list. Returns True when something changed."""
    if GARAGE_LIST not in sd.body:
        return False
    H = {rpf3.joaat(n): n for n in sd.order}
    changed = False
    present = garage_list_members(sd)
    for g in back:
        if g not in present and g in sd.body:
            _list_add(sd, GARAGE_LIST, g, WEIGHT_1)
            present.add(g)
            changed = True
    body, refs = bytearray(sd.body[GARAGE_LIST]), sorted(sd.refs[GARAGE_LIST])
    names = [H.get(u32(body, r, '>')) for r in refs]
    pool = [n for n in names if n and n not in drop]
    i = 0
    for r, n in zip(refs, names):
        if n in drop and pool:
            body[r:r + 4] = struct.pack('>I', rpf3.joaat(pool[i % len(pool)]))
            i += 1
            changed = True
    sd.body[GARAGE_LIST] = bytes(body)
    return changed


def sounds_plan(sd, tmpl, snd_hash):
    """Validate that the template song has the expected 6 objects + garage list entry; returns their names."""
    G = M.GENRE_RX.match(tmpl.obj).group(1)
    W, rest_src = tmpl.wave, tmpl.obj[len(G) + 1:]
    H = {rpf3.joaat(n): n for n in sd.order}
    main = H.get(snd_hash)
    if not main:
        raise M.EditError('SND object of the song not found in sounds.dat')

    def targets(name):
        return [H[u32(sd.body[name], r, '>')] for r in sorted(sd.refs[name]) if u32(sd.body[name], r, '>') in H]
    slots = targets(main)
    if len(slots) != 2 or not slots[0].endswith('_LEFT') or not slots[1].endswith('_RIGHT'):
        raise M.EditError('unexpected slots of %s: %s' % (main, slots))
    gl = [n for n in sd.order if n.endswith('MUSIC_%s_%s_LEFT' % (W, W)) and n not in slots and 'GARAGE' in n]
    if len(gl) != 1:
        raise M.EditError('garage slot of %s not found' % W)
    gh = rpf3.joaat(gl[0])
    gsnd = [n for n in sd.order if sd.body[n][0] == 8 and any(u32(sd.body[n], r, '>') == gh for r in sd.refs[n])]
    if len(gsnd) != 1 or rest_src not in gsnd[0]:
        raise M.EditError('garage SND object of %s not usable' % W)
    gslots = targets(gsnd[0])
    if len(gslots) != 2:
        raise M.EditError('unexpected garage slots of %s' % W)
    gm = sd.body[GARAGE_LIST]
    refs = sorted(sd.refs[GARAGE_LIST])
    if gm[refs[0] - 1] != len(refs) or any(b - a != 8 for a, b in zip(refs, refs[1:])):
        raise M.EditError('unexpected layout of %s' % GARAGE_LIST)
    if rpf3.joaat(gsnd[0]) not in {u32(gm, r, '>') for r in refs}:
        raise M.EditError('the garage object of %s is not in %s' % (W, GARAGE_LIST))
    eor = find_eor_template(sd, G)                      # end-of-race objects (optional, taken from the genre list)
    return {'main': main, 'slots': slots, 'gsnd': gsnd[0], 'gslots': gslots, 'W': W, 'rest_src': rest_src, 'eor': eor}


def _append(sd, plan, hmap):
    """Append copies of the objects in plan = [(old name, new name)]; hashes are rewritten with hmap {old: new}."""
    for old, new in plan:
        if new == old or new in sd.body:
            raise M.EditError('bad new sounds.dat name %s' % new)
    total = sum(len(n) + 1 for n in sd.order)
    for old, new in plan:
        b = sd.body[old]
        for oh, nh in hmap.items():
            b = b.replace(struct.pack('>I', oh), struct.pack('>I', nh))
        b = bytearray(b)
        b[1:5] = struct.pack('>I', total)
        total += len(new) + 1
        sd.order.append(new)
        sd.body[new] = bytes(b)
        sd.refs[new] = list(sd.refs[old])
        sd.wrefs[new] = list(sd.wrefs[old])


def _list_add(sd, list_name, entry_name, weight):
    """random list: one more entry [hash][weight] behind the last one, count byte + 1"""
    lb = bytearray(sd.body[list_name])
    refs = sorted(sd.refs[list_name])
    at = refs[-1] + 8
    lb[at:at] = struct.pack('>I', rpf3.joaat(entry_name)) + weight
    lb[refs[0] - 1] += 1
    sd.body[list_name] = bytes(lb)
    sd.refs[list_name] = refs + [at]


def sounds_apply(sd, info, obj_new, V, rest_new, boost=False, want_eor=True):
    """Append the objects of a new song (+ garage list entry, + end-of-race objects/list entry when the template has
    them). Returns (new names with the main SND first, True when end-of-race objects were created)."""
    W, rest_src = info['W'], info['rest_src']
    slots, gslots = info['slots'], info['gslots']
    plan = [(info['main'], 'SND_' + obj_new), (slots[0], slots[0].replace(W, V)), (slots[1], slots[1].replace(W, V)),
            (info['gsnd'], info['gsnd'].replace(rest_src, rest_new)), (gslots[0], gslots[0].replace(W, V)),
            (gslots[1], gslots[1].replace(W, V))]
    sd.add_stream_path('MUSIC\\' + V)             # the wave-path hashes of the slots are resolved against this table
    hmap = {rpf3.joaat(o): rpf3.joaat(n) for o, n in plan}
    hmap[rpf3.joaat('MUSIC/' + W)] = rpf3.joaat('MUSIC/' + V)
    hmap[rpf3.joaat(W + '_LEFT')] = rpf3.joaat(V + '_LEFT')
    hmap[rpf3.joaat(W + '_RIGHT')] = rpf3.joaat(V + '_RIGHT')
    _append(sd, plan, hmap)
    _list_add(sd, GARAGE_LIST, plan[3][1], WEIGHT_BOOST if boost else WEIGHT_1)
    names = [n for _, n in plan]
    eor = info.get('eor')
    if not (eor and want_eor):
        return names, False
    en, e_new = eor['main'], 'EOR_' + V
    e_plan = [(en, e_new), (eor['slots'][0], eor['slots'][0].replace(en, e_new)),
              (eor['slots'][1], eor['slots'][1].replace(en, e_new))]
    sd.add_stream_path('SPECIAL_EFFECTS_STREAM\\' + e_new)
    eh = {rpf3.joaat(o): rpf3.joaat(n) for o, n in e_plan}
    eh[rpf3.joaat('SPECIAL_EFFECTS_STREAM/' + en)] = rpf3.joaat('SPECIAL_EFFECTS_STREAM/' + e_new)
    eh[rpf3.joaat(en + '_LEFT')] = rpf3.joaat(e_new + '_LEFT')
    eh[rpf3.joaat(en + '_RIGHT')] = rpf3.joaat(e_new + '_RIGHT')
    _append(sd, e_plan, eh)
    _list_add(sd, eor['list'], 'EOR_' + V, WEIGHT_BOOST if boost else WEIGHT_1)
    if boost:                                          # make the end-of-race master pick this genre's list
        mb = bytearray(sd.body[EOR_MASTER])
        h = struct.pack('>I', rpf3.joaat(eor['list']))
        for r in sorted(sd.refs[EOR_MASTER]):
            if bytes(mb[r:r + 4]) == h:
                mb[r + 4:r + 8] = WEIGHT_BOOST
        sd.body[EOR_MASTER] = bytes(mb)
    return names + [n for _, n in e_plan], True


def pick_templates(p, sd, genres):
    """genre -> (template song, plan): the first base-game song of that genre whose sounds.dat objects are usable."""
    out = {}
    for g in genres:
        best = None
        for s in p.songs.values():
            if s.added or s.manager != 'MUSIC_0_MANAGER' or s.orig_genre != g or not s.wave or s.wave.startswith('SC_'):
                continue
            try:
                plan = sounds_plan(sd, s, snd_hash_of(p, s.obj))
            except M.EditError:
                continue
            if plan['eor']:                                   # best: also has end-of-race objects
                best = (s, plan)
                break
            if best is None:
                best = (s, plan)
        if best is None:
            raise M.EditError('No usable template song for %s' % M.GENRE_UI[g])
        out[g] = best
    return out
