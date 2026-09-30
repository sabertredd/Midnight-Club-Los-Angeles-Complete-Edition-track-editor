# Midnight Club: Los Angeles — soundtrack editor

Unofficial tools for editing the soundtrack of **Midnight Club: Los Angeles**:

* **Xbox 360** — Complete Edition (`xbox360/`)
* **PlayStation 3** — BLES00652 (`ps3/`)

No game files, no encryption keys and no Microsoft tools are included. You need your own copy of the game.

## What it can do

* change the song lists: order, genre, remove songs, add your own songs (any format ffmpeg reads)
* replace the audio of any song, edit song titles
* rename the 7 genres (6 languages)
* end-of-race clips (the music on the victory screen): automatic excerpt or your own file, with looped preview
* garage music and end-of-race lists follow the playlists, so removed songs do not play there any more
* hangout music (6 clips) can be replaced
* free the space of removed songs (the archives are limited to 2 GB each)
* loudness normalizer (separate program), matching the level of the original songs
* backup cleaner for the `.rpf.bak` files the editor leaves behind
* a draft is saved automatically, and a sidecar file next to the game folder records what was changed

## Folders

| Folder | Contents |
|---|---|
| `bin/` | ready-to-run programs (Windows): editor, normalizer, backup cleaner (Xbox 360) and the PS3 editor |
| `xbox360/MCLA_Editor/` | Python sources of the Xbox 360 programs, `README.txt` = user manual (Russian), `mcla_unpack.py` = archive unpacker (command line) |
| `ps3/MCLA_Editor_PS3/` | Python sources of the PS3 editor, `README.txt` = manual (Russian) |
| `docs/` | file formats found while making the tools (RPF3 archives, `game.dat`, `sounds.dat`, text banks, XMA / MP3 track layout) |
| `names/` | archive file names found so far and statistics |

## Requirements

* Windows 10/11. The programs in `bin/` need nothing else; the sources need Python 3.10+ with tkinter.
* **ffmpeg** (any recent build) for reading audio files, previews and loudness.
* **Xbox 360 audio:** Microsoft's XMA encoder XMAENCODE from the Xbox 360 SDK. It is not included and may not be
  redistributed. Any file name works (`xmaencode.exe`, `xmaencode2008.exe`, ...). The editor looks for `*xma*enc*.exe`
  next to itself and in the SDK folder, or you set the path in *Audio settings*. It must write XMA1:
  `xma2encode.exe` writes XMA2 and does not work.
* **PS3 audio:** MP3 is encoded by ffmpeg (LAME), nothing else is needed.

## First start: the archive key

The game's archives (`.rpf`) are encrypted. The key belongs to the game and is **not part of these tools**. When a
game folder is opened for the first time, the program asks for it once, and there are two ways to give it:

1. **Enter it** — 64 hex digits.
2. **Find it** — point the program to the game's own executable, decrypted and not compressed. It searches the file
   and checks every candidate against your archives.
   * Xbox 360: `default.xex` after e.g. `xextool -e u -c u default.xex`
   * PS3: `EBOOT.BIN` decrypted with RPCS3 (*Utilities → Decrypt PS3 Binaries* → `EBOOT.elf`)

The key is then kept in `mcla_rpf_key.txt` next to the program. Do not share that file.

## Before you start

* **Work on a copy of the game folder.** *Save to the current folder* keeps the first original of every changed
  archive as `*.rpf.bak`. *Build test game folder* writes a new folder with hard links to the unchanged files.
* **Keep at least one song in every genre.** If the game's save last played a genre that is now empty, the game
  freezes while loading on a real Xbox 360 (PS3/RPCS3: the game quits).
* The game uses either the archive set `audio + music + cache` or `audlo + cache`. The editor therefore changes
  `music` and `audlo` together.
* Xenia uses `audlo + cache`, so the hangout music (only in `audio.rpf`) does not play there, not even in the
  original game. Check it on a console.
* The sidecar files next to the game folder (`<folder>.mcla_changes.json`, `<folder>.mcla_draft.json`) are for the
  editor only. Do not copy them to the console.

## Tested

* Xbox 360: in Xenia and on a real console. That covers playlists, titles, genre names, added / replaced songs,
  garage, end-of-race clips, hangout music, compact archive rebuild, freed songs and loudness.
* PS3: in RPCS3 and on a real PS3 (playlists, genre names, replaced and added songs, hangout music). End-of-race
  clips are not tested on PS3 yet.

## Legal

This is a fan project, not affiliated with or endorsed by Rockstar Games or Take-Two Interactive. *Midnight Club* is
their trademark. The tools contain no game content. Do not distribute game files, audio or keys made with them.
