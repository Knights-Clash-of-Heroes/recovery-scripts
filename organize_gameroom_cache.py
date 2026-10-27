#!/usr/bin/env python3
"""
Organize a Facebook Gameroom (Chromium blockfile) cache folder.

Usage:
    python organize_gameroom_cache.py CACHE_DIR OUTPUT_DIR [--keep-going]

What it does:
  1. Looks for the game's resource manifest inside the cache (a JSON document with a
     "resources" map of  name -> {md5, size, type}). The manifest itself is usually stored
     compressed, so every cache file is unwrapped before being inspected.
  2. Creates OUTPUT_DIR (never inside CACHE_DIR) and copies the cache files to OUTPUT_DIR/_raw.
  3. Decodes every file: gzip, zlib, raw deflate (Flash ByteArray.inflate), Brotli (needs the
     optional "brotli" package) and compressed SWFs (CWS/ZWS -> FWS). Up to MAX_LAYERS layers.
  4. Detects the real format from the magic bytes (not from the file name).
  5. Renames each file after its manifest entry (md5 of the cache file or of the decoded
     bytes), or after a name embedded in the file (KCMC sprite names, SWF symbols), or keeps the
     cache file name when nothing better is known.
  6. Writes the result to OUTPUT_DIR/<kind>_<extension>/<name>.<extension>
     (e.g. image_png, audio_mp3, flash_swf, data_json, sprite_kcmc) plus mapping.json.

Nothing in CACHE_DIR is modified. No encryption is handled because this cache has none: data
that looks random is almost always compressed.
"""

import argparse
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import sys
import zlib

try:
    import brotli
except ImportError:
    brotli = None

MAX_LAYERS = 5

# extension -> kind (first half of the "<kind>_<extension>" folder name)
KINDS = {
    "png": "image", "jpg": "image", "gif": "image", "webp": "image", "bmp": "image",
    "ico": "image", "atf": "image",
    "mp3": "audio", "ogg": "audio", "wav": "audio",
    "flv": "video", "mp4": "video",
    "swf": "flash",
    "kcmc": "sprite", "kc3d": "model",
    "json": "data", "xml": "data", "txt": "data", "csv": "data",
    "js": "script", "css": "style", "html": "document",
    "woff": "font", "ttf": "font", "otf": "font",
    "zip": "archive", "v8cache": "cache",
    "bin": "unknown",
}


def is_zlib(head: bytes) -> bool:
    return len(head) >= 2 and head[0] == 0x78 and (head[0] * 256 + head[1]) % 31 == 0


def detect_ext(data: bytes) -> str:
    """Return the file extension that matches the content of `data`."""
    head = data[:16]
    if len(data) < 4:
        return "bin"
    if head[:4] == b"KCMC":
        return "kcmc"
    if head[:4] == b"KC3D":
        return "kc3d"
    if head[:3] == b"ATF":
        return "atf"
    if head[:3] in (b"FWS", b"CWS", b"ZWS"):
        return "swf"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if head[:2] == b"BM" and len(data) > 14 and data[6:10] == b"\x00\x00\x00\x00":
        return "bmp"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:3] == b"FLV":
        return "flv"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"wOFF":
        return "woff"
    if head[:4] == b"OTTO":
        return "otf"
    if head[:4] == b"\x00\x01\x00\x00":
        return "ttf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if is_zlib(head):
        return "zlib"
    if len(head) >= 8 and head[6:8] == b"\xde\xc0":   # V8 code cache header
        return "v8cache"

    text = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    low = text.lower()
    if low.startswith(b"<!doctype html") or b"<html" in low[:1024]:
        return "html"
    if text[:5] == b"<?xml" or text[:1] == b"<":
        return "xml"
    if text[:1] in (b"{", b"["):
        return "json"
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return "bin"
    if b"function" in text or b"var " in text or b"=>" in text:
        return "js"
    if b"{" in text and b":" in text and b";" in text:
        return "css"
    return "txt"


def try_raw_codecs(data: bytes):
    """For unrecognised data: try raw deflate and Brotli. Only accept a result whose content is
    recognisable, otherwise random-looking data would produce false positives."""
    candidates = []
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(data)
        if d.eof:
            candidates.append((out, "deflate"))
    except zlib.error:
        pass
    if brotli is not None:
        try:
            candidates.append((bytes(brotli.decompress(data)), "brotli"))
        except Exception:
            pass
    for out, name in candidates:
        if len(out) >= 16 and detect_ext(out) != "bin":
            return out, name
    return None


def unpack_swf(data: bytes) -> bytes:
    """CWS (zlib) / ZWS (LZMA) -> FWS (uncompressed)."""
    if data[:3] == b"CWS":
        body = zlib.decompress(data[8:])
    elif data[:3] == b"ZWS":
        flt = lzma._decode_filter_properties(lzma.FILTER_LZMA1, data[12:17])
        body = lzma.decompress(data[17:], format=lzma.FORMAT_RAW, filters=[flt])
    else:
        return data
    return b"FWS" + data[3:4] + struct.pack("<I", len(body) + 8) + body


def decode(data: bytes):
    """Strip every compression layer. Returns (decoded bytes, extension, [layers removed])."""
    layers = []
    for _ in range(MAX_LAYERS):
        ext = detect_ext(data)
        try:
            if ext == "gz":
                data, step = gzip.decompress(data), "gzip"
            elif ext == "zlib":
                data, step = zlib.decompress(data), "zlib"
            elif ext == "swf" and data[:3] in (b"CWS", b"ZWS"):
                data, step = unpack_swf(data), "swf-" + data[:3].decode().lower()
            elif ext == "bin":
                found = try_raw_codecs(data)
                if not found:
                    break
                data, step = found
            else:
                break
        except Exception:
            break
        layers.append(step)
    return data, detect_ext(data), layers


def find_manifest(files):
    """Return {md5: (name, type)} from the first JSON document that has a 'resources' map
    whose values carry an 'md5' field, or {} when there is none."""
    for path in files:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        data, ext, _ = decode(raw)
        if ext != "json" or b'"resources"' not in data[:200000]:
            continue
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            continue
        res = doc.get("resources") if isinstance(doc, dict) else None
        if isinstance(res, dict) and res and all(
                isinstance(v, dict) and "md5" in v for v in res.values()):
            print(f"Manifest found: {os.path.basename(path)} ({len(res)} resources)")
            return {v["md5"].lower(): (k, v.get("type", "")) for k, v in res.items()}
    return {}


def embedded_name(data: bytes, ext: str):
    """Best-effort name stored inside the file itself."""
    try:
        if ext == "kcmc":
            # model names (readUTF) such as kc_buildings_003_barracks_sw, before the texture
            m = re.search(rb"[\x00-\x01][\x06-\x60](kc_[a-z]+_[\w.-]{3,90})", data[24:])
            return m.group(1).decode() if m else None
        if ext == "swf":
            return swf_first_symbol(unpack_swf(data))
    except Exception:
        pass
    return None


def swf_first_symbol(swf: bytes):
    """First name of the SymbolClass tag (tag 76), usually the main exported class."""
    body = swf[8:]
    nbits = body[0] >> 3
    pos = (5 + 4 * nbits + 7) // 8 + 4
    while pos + 2 <= len(body):
        header = struct.unpack("<H", body[pos:pos + 2])[0]
        pos += 2
        tag, length = header >> 6, header & 63
        if length == 63:
            length = struct.unpack("<I", body[pos:pos + 4])[0]
            pos += 4
        if tag == 76 and length > 4:
            end = body.index(b"\0", pos + 4)
            return body[pos + 4:end].decode("latin1") or None
        pos += length
    return None


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "unnamed"


def list_cache_files(source: str):
    entries = [e for e in os.scandir(source) if e.is_file()]
    game = [e.path for e in entries if e.name.startswith("f_")]
    return sorted(game or [e.path for e in entries])


def main():
    ap = argparse.ArgumentParser(description="Decode, rename and sort a Facebook Gameroom cache.")
    ap.add_argument("source", help="Gameroom cache folder (contains the f_xxxxxx files)")
    ap.add_argument("output", help="folder to create with the organized files")
    args = ap.parse_args()

    source, output = os.path.abspath(args.source), os.path.abspath(args.output)
    if not os.path.isdir(source):
        sys.exit(f"Cache folder not found: {source}")
    if output == source or output.startswith(source + os.sep):
        sys.exit("The output folder must not be inside the cache folder.")

    files = list_cache_files(source)
    if not files:
        sys.exit("No files found in the cache folder.")
    print(f"{len(files)} cache files found.")

    manifest = find_manifest(files)
    if not manifest:
        print("Manifest not found: files will be named from their content or cache name.")

    raw_dir = os.path.join(output, "_raw")
    os.makedirs(raw_dir, exist_ok=True)
    used, mapping, counts = set(), [], {}

    for path in files:
        cache_name = os.path.basename(path)
        shutil.copy2(path, os.path.join(raw_dir, cache_name))
        with open(path, "rb") as fh:
            raw = fh.read()

        data, ext, layers = decode(raw)
        entry = manifest.get(hashlib.md5(raw).hexdigest()) or manifest.get(
            hashlib.md5(data).hexdigest())
        if entry:
            name, source_of_name = entry[0], "manifest"
        elif (emb := embedded_name(data, ext)):
            name, source_of_name = emb, "embedded"
        else:
            name, source_of_name = cache_name, "cache"
        if ext == "swf":
            data = unpack_swf(data)

        folder = f"{KINDS.get(ext, 'unknown')}_{ext}"
        name = safe_name(name)
        candidate, i = f"{name}.{ext}", 2
        while (folder, candidate.lower()) in used:
            candidate, i = f"{name}_{i}.{ext}", i + 1
        used.add((folder, candidate.lower()))

        os.makedirs(os.path.join(output, folder), exist_ok=True)
        with open(os.path.join(output, folder, candidate), "wb") as fh:
            fh.write(data)

        counts[folder] = counts.get(folder, 0) + 1
        mapping.append({
            "cache_file": cache_name,
            "output": f"{folder}/{candidate}",
            "extension": ext,
            "name_source": source_of_name,
            "layers_removed": layers,
            "raw_size": len(raw),
            "decoded_size": len(data),
            "md5_raw": hashlib.md5(raw).hexdigest(),
        })

    with open(os.path.join(output, "mapping.json"), "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2, ensure_ascii=False)

    named = sum(1 for m in mapping if m["name_source"] != "cache")
    print(f"\nDone: {len(mapping)} files, {named} with a recovered name.")
    for folder, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {folder:16s} {n}")
    if brotli is None:
        print("\nNote: 'brotli' is not installed (pip install brotli); Brotli files stay encoded.")
    print(f"\nOutput: {output}")


if __name__ == "__main__":
    main()
