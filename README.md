# Recovery Scripts

Small tools for recovering game assets from old caches.

## organize_gameroom_cache.py

Decodes, renames and sorts the files of a Facebook Gameroom cache folder (Chromium blockfile
cache, files named `f_xxxxxx`).

### Requirements

- Python 3.8+ (uses only the standard library)
- Optional: `pip install brotli` to decode Brotli-compressed files (e.g. the Gameroom JS bundles)

### Usage

```
python organize_gameroom_cache.py CACHE_DIR OUTPUT_DIR
```

| Argument     | Description                                                              |
|--------------|--------------------------------------------------------------------------|
| `CACHE_DIR`  | Gameroom cache folder containing the `f_*` files (never modified)        |
| `OUTPUT_DIR` | Folder to create; it must not be inside `CACHE_DIR`                      |

### What it does

1. Looks for the game's resource manifest: a JSON document with a `resources` map of
   `name -> {md5, size, type}`. Every cache file is unwrapped before being inspected.
2. Copies the original cache files to `OUTPUT_DIR/_raw`.
3. Decodes each file: gzip, zlib, raw deflate, Brotli and compressed SWF (CWS/ZWS to FWS),
   up to 5 nested layers.
4. Detects the real format from the magic bytes, ignoring the file name.
5. Names each file after, in order of preference:
   the manifest entry (md5 of the cache file or of the decoded bytes), a name embedded in the
   file (KCMC model name, first SWF symbol), or the original cache file name.
6. Writes the result to `OUTPUT_DIR/<kind>_<extension>/<name>.<extension>` and a
   `mapping.json` describing every file.

### Output layout

```
OUTPUT_DIR/
  _raw/                 untouched copies of the cache files
  flash_swf/            kc_gui_001_loading.swf, ...
  data_json/            lang.json, heroes.json, ...
  sprite_kcmc/          kc_buildings_001_castle_level_7_sw.kcmc, ...
  script_js/
  mapping.json          cache file -> output path, name source, layers removed, sizes, md5
```

### Limitations

- Names depend on the manifest being present in the cache. Otherwise only embedded names
  (KCMC, SWF) are recovered.
- It does not read the Chromium index (`index`, `data_*`), so original URLs are not recovered.
- KCMC and ATF textures are kept as-is, not converted to PNG.
- There is no decryption step: this cache is not encrypted, only compressed.
