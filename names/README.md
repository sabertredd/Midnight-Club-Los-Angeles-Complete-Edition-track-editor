# Archive file names - statistics

File names are not stored in RPF3 archives, only their joaat hashes. A name counts as found when its hash
matches an entry. Folders that are still unnamed show their hash.

## Xbox 360 (Complete Edition)

| Archive | Files | Named | % |
|---|---:|---:|---:|
| xarchive_music.rpf | 108 | 107 | 99% |
| xarchive_audio.rpf | 1788 | 1780 | 100% |
| xarchive_audlo.rpf | 1054 | 1046 | 99% |
| xarchive_cache.rpf | 22068 | 19051 | 86% |
| **total** | **25018** | **21984** | **88%** |

## PlayStation 3 (BLES00652)

| Archive | Files | Named | % |
|---|---:|---:|---:|
| carchive_music.rpf | 108 | 107 | 99% |
| carchive_audio.rpf | 1512 | 1503 | 99% |
| carchive_cache.rpf | 21962 | 18582 | 85% |
| carchive_cache2.rpf | 416 | 406 | 98% |

## Xbox 360: folders with the most unnamed files

| Archive | Folder | Unnamed |
|---|---|---:|
| cache | `tune/garage` | 695 |
| cache | `script_obj/game/race/Hollywood/Ordered` | 79 |
| cache | `tune/vehicle/licensePlateColors` | 78 |
| cache | `resources/ui/textures/garage/dyntextures` | 59 |
| cache | `tune/camera` | 56 |
| cache | `script_obj/game/CineScripts/generated` | 52 |
| cache | `resources/ui/meshes` | 49 |
| cache | `resources/ui/textures/garage/dyntextures/thumb` | 47 |
| cache | `script_obj/game/CineScripts/generated/RaceStart` | 45 |
| cache | `script_obj/game/race/Hollywood/Other` | 38 |
| cache | `shaders/city` | 35 |
| cache | `script_obj/game/CineScripts/generated/hangout` | 35 |
| cache | `script_obj/SC/Career/missions` | 22 |
| cache | `script_obj/game/CineScripts/generated/story` | 21 |
| cache | `script_obj/game/Career/missions` | 18 |

## How the names were found

* names that the game itself hashes at run time, logged with a small RPCS3 patch on the PlayStation 3 version
  (the same names are used on the Xbox 360; only resource extensions differ: `.ctd` -> `.xtd` etc.)
* strings from the game's own configuration files (sounds.dat, game.dat, ...), with their path parts
* templates per folder: numbered series, car / district / race-type combinations, suffix families
* resource extensions guessed from resources of the same type in the same archive

Files: `x360_<archive>_paths.txt` - every named entry with its full path inside the archive;
`x360_known_names.txt` - all distinct names (file and folder names); `ps3_known_names.txt` - PS3 names.
