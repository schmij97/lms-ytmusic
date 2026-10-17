#!/usr/bin/env python3
import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

YTM_BASE = "https://music.youtube.com/youtubei/v1/"
API_KEY  = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-KLET5YdCE"

_CLIENT = {
    "clientName":    "WEB_REMIX",
    "clientVersion": "1.20240918.01.00",
    "hl": "en",
    "gl": "US",
}

_HEADERS = {
    "Content-Type":             "application/json",
    "X-Goog-Api-Key":           API_KEY,
    "X-YouTube-Client-Name":    "67",
    "X-YouTube-Client-Version": _CLIENT["clientVersion"],
    "User-Agent":               (
        "Mozilla/5.0 (X11; Linux armv7l) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin":          "https://music.youtube.com",
    "Referer":         "https://music.youtube.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

import time
import tempfile
import os
import threading

# ── ytmusicapi (optional, for authenticated YouTube Music access) ─────────────
_ytm_auth = None   # YTMusic instance, None if not authenticated
_ytm_cookie = None # current cookie string

def _get_ytm_auth():
    """Return authenticated YTMusic instance or None."""
    return _ytm_auth

# Pinned so a new release can't change behaviour underneath us. ytmusicapi's
# only dependency is requests; requests and its dependencies are fetched too
# when the system Python doesn't have them (e.g. piCorePlayer). All are
# pure-Python wheels, so the same files work on every platform.
_YTMUSICAPI_VERSION = "1.12.3"
_REQUESTS_WHEELS = (
    ("requests",           "2.34.2"),
    ("urllib3",            "2.8.0"),
    ("idna",               "3.20"),
    ("charset-normalizer", "3.5.2"),
    ("certifi",            "2026.7.22"),
)

def _install_pinned_wheel(name, version):
    """Download the pure-Python wheel of name==version from PyPI into BIN_DIR,
    verified against the sha256 PyPI publishes. Returns True on success."""
    import hashlib, io, zipfile
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=20) as resp:
            meta = json.loads(resp.read())
        wheel = next(u for u in meta["urls"] if u["filename"].endswith("-py3-none-any.whl"))
        with urllib.request.urlopen(wheel["url"], timeout=60) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != wheel["digests"]["sha256"]:
            logging.warning("%s wheel checksum mismatch, not installing", name)
            return False
        os.makedirs(BIN_DIR, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(BIN_DIR)
        logging.warning("%s %s installed to %s", name, version, BIN_DIR)
        return True
    except Exception as e:
        logging.warning("%s wheel install failed: %s", name, e)
        return False

def _install_ytmusicapi_wheel():
    """Install ytmusicapi (and requests, if missing) into BIN_DIR without pip.
    Returns True on success."""
    import importlib
    if BIN_DIR not in sys.path:
        sys.path.insert(0, BIN_DIR)
    try:
        import requests  # noqa: F401  (ytmusicapi's only dependency)
    except ImportError:
        logging.warning("Python 'requests' not found; installing it into %s", BIN_DIR)
        if not all(_install_pinned_wheel(n, v) for n, v in _REQUESTS_WHEELS):
            return False
        importlib.invalidate_caches()
        try:
            import requests  # noqa: F401,F811
        except ImportError as e:
            logging.warning("requests still not importable: %s", e)
            return False
    if not _install_pinned_wheel("ytmusicapi", _YTMUSICAPI_VERSION):
        return False
    importlib.invalidate_caches()
    return True

def _init_ytm_auth(headers_raw):
    """Initialize ytmusicapi with raw browser headers string."""
    global _ytm_auth, _ytm_cookie
    if not headers_raw or not headers_raw.strip():
        _ytm_auth = None
        _ytm_cookie = None
        return False
    try:
        # Auto-install ytmusicapi if not available. Prefer a verified wheel
        # in BIN_DIR (no pip needed, system Python untouched); pip is the
        # fallback.
        try:
            from ytmusicapi import YTMusic, setup
        except ImportError:
            if BIN_DIR not in sys.path:
                sys.path.insert(0, BIN_DIR)
        try:
            from ytmusicapi import YTMusic, setup
        except ImportError:
            _install_ytmusicapi_wheel()
        try:
            from ytmusicapi import YTMusic, setup
        except ImportError:
            logging.info("ytmusicapi not found, installing...")
            # Try with --break-system-packages first (Python 3.11+), fall back without it
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "ytmusicapi",
                     "--break-system-packages", "--quiet"],
                    check=True, timeout=120
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "ytmusicapi",
                     "--user", "--quiet"],
                    check=True, timeout=120
                )
            from ytmusicapi import YTMusic, setup
            logging.info("ytmusicapi installed successfully")
        from ytmusicapi import YTMusic, setup
        # Reduce whatever was pasted to the headers ytmusicapi actually needs
        cleaned = _normalize_pasted_headers(headers_raw)
        # setup() converts raw headers to the format ytmusicapi needs
        auth_str = setup(headers_raw=cleaned)
        _ytm_auth = YTMusic(auth_str)
        _ytm_cookie = headers_raw.strip()
        _STABLE.clear()
        logging.info("ytmusicapi authenticated successfully")
        return True
    except Exception as e:
        logging.warning("ytmusicapi auth failed: %s", e)
        _ytm_auth = None
        _ytm_cookie = None
        return False



def _parse_curl_command(raw):
    """Extract cookie, x-goog-authuser, x-goog-pageid and authorization from a curl command string.

    LMS stores the ytm_cookie as a Windows curl command with ^" quoting.
    Returns dict with cookie/x-goog-authuser/x-goog-pageid/authorization keys, or None if
    the input doesn't look like a curl command.
    x-goog-pageid is present when a brand account (YouTube channel) is selected;
    without it YouTube returns the main account's library, which is typically empty.
    """
    stripped = raw.strip()
    if not stripped.lower().startswith("curl"):
        return None
    # Normalise Windows ^" quoting and line-continuation whitespace
    normalized = stripped.replace("^\"", "\"").replace("^\n", " ")
    normalized = re.sub(r'\n\s+', ' ', normalized)
    # Percent-decode %3D -> = (Windows URL-encoded equals in cookie values)
    normalized = normalized.replace("%3D", "=").replace("^%^3D", "=")
    wanted = {"cookie": None, "x-goog-authuser": None, "x-goog-pageid": None,
              "authorization": None}
    # Extract -H "name: value" headers
    for hdr in re.findall(r'-H\s+"([^"]+)"', normalized):
        m = re.match(r'^([A-Za-z0-9_-]+):\s*(.+)$', hdr)
        if m:
            name = m.group(1).lower()
            if name in wanted and wanted[name] is None:
                wanted[name] = m.group(2).strip()
    # Extract cookie from -b "..." if not already found via -H
    if not wanted["cookie"]:
        bm = re.search(r'-b\s+"([^"]+)"', normalized)
        if bm:
            wanted["cookie"] = bm.group(1).strip()
    return wanted if wanted["cookie"] else None


def _normalize_pasted_headers(raw):
    """Turn pasted browser request headers into the three ytmusicapi needs.

    Browsers copy headers in different shapes: "Name: value" per line
    (Firefox raw view, older Chrome "Copy request headers"), or - when the
    current Chrome DevTools table is selected with the mouse - the name and
    the value on separate lines, including HTTP/2 pseudo-headers such as
    ":authority". Passed through as-is, ytmusicapi turns the pseudo-header
    values into bogus header names and keeps Chrome's capitalised
    Accept-Encoding (br, zstd) / Content-Length, so YouTube's replies can't be
    decoded ("Expecting value: line 1 column 1").

    Also handles Windows curl command format (stored in LMS prefs as ytm_cookie).

    Only cookie, x-goog-authuser, x-goog-pageid and authorization matter:
    ytmusicapi recognises browser auth by an authorization header containing
    SAPISIDHASH and regenerates its value from the cookie on every request.
    x-goog-pageid is only present when a brand account (a channel separate
    from the main Google account) is selected; without it YouTube answers for
    the main account, whose library is typically empty.
    Returns the normalised block, or the input unchanged if no cookie is found
    (so ytmusicapi reports what is missing).
    """
    # Handle curl command format (LMS passes ytm_cookie as Windows curl command)
    curl_parsed = _parse_curl_command(raw or "")
    if curl_parsed:
        wanted = curl_parsed
    else:
        wanted = {"cookie": None, "x-goog-authuser": None, "x-goog-pageid": None,
                  "authorization": None}
        pending = None
        for line in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if pending is not None:                      # value line of a "name" line
                if pending in wanted and wanted[pending] is None:
                    wanted[pending] = line
                pending = None
                continue
            m = re.match(r"^(:?[A-Za-z0-9-]+):(?:\s+(.*))?$", line)
            if m and m.group(2):                         # "Name: value"
                name = m.group(1).lower()
                if name in wanted and wanted[name] is None:
                    wanted[name] = m.group(2).strip()
            elif m or re.match(r"^:?[A-Za-z0-9-]+$", line):  # "Name:" or "Name" alone
                pending = (m.group(1) if m else line).lower()
    if not wanted["cookie"]:
        return (raw or "").strip()
    auth = wanted["authorization"] or ""
    if "SAPISIDHASH" not in auth:
        auth = "SAPISIDHASH 0_0"                     # marker only; value is regenerated
    logging.warning("YouTube Music headers: using cookie, x-goog-authuser%s%s",
                    "" if wanted["x-goog-authuser"] else " (defaulted to 0)",
                    ", x-goog-pageid (brand account)" if wanted.get("x-goog-pageid") else "")
    lines = [
        f"cookie: {wanted['cookie']}",
        f"x-goog-authuser: {wanted['x-goog-authuser'] or '0'}",
        f"authorization: {auth}",
    ]
    if wanted.get("x-goog-pageid"):
        lines.append(f"x-goog-pageid: {wanted['x-goog-pageid']}")
    return "\n".join(lines)


_CACHE = {}
_CACHE_TTL = 120  # seconds

def _cache_get(key):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None

# Personal lists (home/Speed dial, liked songs, library...) are held for a
# while. LMS menus navigate by position and re-fetch the parent list on every
# step, and YouTube returns these lists in a slightly different order on each
# request (the home page reshuffles its sections), so without this a tap could
# open a different tile than the one tapped. Errors aren't kept, and a new
# login clears it.
_STABLE = {}
_STABLE_TTL = 600  # seconds

def _stable(key, fetch):
    entry = _STABLE.get(key)
    if entry and (time.time() - entry[0]) < _STABLE_TTL:
        return entry[1]
    value = fetch()
    if isinstance(value, dict) and not value.get("error"):
        _STABLE[key] = (time.time(), value)
    return value

def _cache_set(key, value):
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > 200:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:50]
        for k, _ in oldest:
            _CACHE.pop(k, None)


def _post(endpoint, body):
    url     = YTM_BASE + endpoint + "?prettyPrint=false"
    payload = json.dumps({"context": {"client": _CLIENT}, **body}).encode()
    req     = urllib.request.Request(url, data=payload, headers=_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

def _text(obj, key="title"):
    val = obj.get(key, {})
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        runs = val.get("runs", [])
        if runs:
            return "".join(r.get("text", "") for r in runs)
        return val.get("simpleText", "")
    return ""

def _thumbnail(thumb_list):
    if not thumb_list:
        return ""
    return thumb_list[-1].get("url", "")

def _thumb_from_renderer(renderer):
    return _thumbnail(
        renderer.get("thumbnail", {})
                .get("musicThumbnailRenderer", {})
                .get("thumbnail", {})
                .get("thumbnails", [])
    )

def _col(renderer, col_idx, run_idx=0):
    try:
        col  = renderer["flexColumns"][col_idx]
        runs = col["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        return runs[run_idx].get("text", "") if runs else ""
    except (IndexError, KeyError):
        return ""

def _fixed_col(renderer, col_idx=0, run_idx=0):
    """Extract text from fixedColumns (used for duration in playlists)."""
    try:
        col  = renderer["fixedColumns"][col_idx]
        runs = col["musicResponsiveListItemFixedColumnRenderer"]["text"]["runs"]
        return runs[run_idx].get("text", "") if runs else ""
    except (IndexError, KeyError):
        return ""

def _page_type(renderer):
    return (
        renderer.get("navigationEndpoint", {})
                .get("browseEndpoint", {})
                .get("browseEndpointContextSupportedConfigs", {})
                .get("browseEndpointContextMusicConfig", {})
                .get("pageType", "")
    )

def _video_id_from_overlay(renderer):
    return (
        renderer.get("overlay", {})
                .get("musicItemThumbnailOverlayRenderer", {})
                .get("content", {})
                .get("musicPlayButtonRenderer", {})
                .get("playNavigationEndpoint", {})
                .get("watchEndpoint", {})
                .get("videoId", "")
    )

def _duration_to_secs(d):
    """Convert duration string like '3:45' or '1:02:34' to seconds."""
    if not d:
        return 0
    if isinstance(d, int):
        return d
    try:
        parts = str(d).strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return 0

_DURATION_RE = re.compile(r"^\d+:\d{2}(:\d{2})?$")
_PLAYS_RE    = re.compile(r"\b(plays|views)$", re.I)

def _col_texts(renderer, col_idx):
    """All run texts of a flex column, without the " • " separators."""
    try:
        runs = renderer["flexColumns"][col_idx]["musicResponsiveListItemFlexColumnRenderer"]["text"].get("runs", [])
    except (IndexError, KeyError):
        return []
    return [t for t in (x.get("text", "").strip() for x in runs) if t and t != "\u2022"]

def _parse_song(r):
    video_id = _video_id_from_overlay(r)
    # Search results put "Artist • Album • 3:52" in the second column and
    # the play count in the third; album pages and charts use other layouts.
    second   = _col_texts(r, 1)
    album    = _col(r, 2)
    duration = _fixed_col(r, 0) or _col(r, 3) or _col(r, 4)
    if _PLAYS_RE.search(album):
        album = ""
    if second and _DURATION_RE.match(second[-1]):
        duration = duration or second[-1]
        if not album and len(second) >= 3:
            album = second[-2]
    return {
        "type":      "song",
        "title":     _col(r, 0),
        "artist":    _col(r, 1),
        "album":     album,
        "duration":  _duration_to_secs(duration),
        "videoId":   video_id,
        "thumbnail": _thumb_from_renderer(r),
        "url":       f"ytm://{video_id}" if video_id else "",
    }

def _parse_album(r):
    browse_ep = r.get("navigationEndpoint", {}).get("browseEndpoint", {})
    return {
        "type":      "album",
        "title":     _col(r, 0),
        "artist":    _col(r, 1),
        "year":      _col(r, 2),
        "browseId":  browse_ep.get("browseId", ""),
        "thumbnail": _thumb_from_renderer(r),
    }

def _parse_artist(r):
    browse_ep = r.get("navigationEndpoint", {}).get("browseEndpoint", {})
    return {
        "type":      "artist",
        "name":      _col(r, 0),
        "browseId":  browse_ep.get("browseId", ""),
        "thumbnail": _thumb_from_renderer(r),
    }

def _parse_podcast_episode(r):
    """Parse a musicMultiRowListItemRenderer (podcast episode)."""
    nav      = r.get("onTap", {}).get("watchEndpoint", {})
    video_id = nav.get("videoId", "")
    if not video_id:
        return None
    title    = _text(r.get("title",    {}), "runs[0].text") or _text_runs(r.get("title",    {}))
    subtitle = _text_runs(r.get("subtitle", {}))
    thumb    = _thumbnail(
        r.get("thumbnail", {})
         .get("musicThumbnailRenderer", {})
         .get("thumbnail", {})
         .get("thumbnails", [])
    )
    return {
        "type":      "song",
        "videoId":   video_id,
        "title":     title or f"Episode",
        "artist":    subtitle,
        "thumbnail": thumb,
    }


def _text_runs(obj):
    """Extract text from a runs array directly."""
    if not obj:
        return ""
    runs = obj.get("runs", [])
    if runs:
        return "".join(r.get("text", "") for r in runs)
    return obj.get("simpleText", "")


def _parse_playlist(r):
    browse_ep = r.get("navigationEndpoint", {}).get("browseEndpoint", {})
    return {
        "type":      "playlist",
        "title":     _col(r, 0),
        "count":     _col(r, 1),
        "browseId":  browse_ep.get("browseId", ""),
        "thumbnail": _thumb_from_renderer(r),
    }

def _classify_and_parse(r):
    pt        = _page_type(r)
    has_video = bool(_video_id_from_overlay(r))
    # Check browse-based types first (before video check)
    if pt == "MUSIC_PAGE_TYPE_ALBUM":
        return _parse_album(r)
    if pt == "MUSIC_PAGE_TYPE_ARTIST":
        return _parse_artist(r)
    if "PLAYLIST" in pt:
        return _parse_playlist(r)
    if "PODCAST" in pt:
        return _parse_playlist(r)
    # Fall back to song/video
    if has_video or pt == "MUSIC_PAGE_TYPE_TRACK":
        return _parse_song(r)
    return None

def _shelf_items(shelf):
    results = []
    for entry in shelf.get("contents", []):
        r = entry.get("musicResponsiveListItemRenderer")
        if r:
            item = _classify_and_parse(r)
            if item:
                results.append(item)
    return results

def _single_col_sections(data):
    return (
        data.get("contents", {})
            .get("singleColumnBrowseResultsRenderer", {})
            .get("tabs", [{}])[0]
            .get("tabRenderer", {})
            .get("content", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
    )

_SEARCH_PARAMS = {
    "songs":     "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D",
    "albums":    "EgWKAQIYAWoKEAkQChADEAQQBQ%3D%3D",
    "artists":   "EgWKAQIgAWoKEAkQChADEAQQBQ%3D%3D",
    # Community playlists (ytmusicapi's current filter); the previous value
    # returned no playlist shelf at all, so results were leftovers from other
    # sections (top card, albums, songs).
    "playlists": "EgeKAQQoAEABagwQDhAKEAMQBBAJEAU%3D",
    "videos":    "EgWKAQIQAWoKEAkQChADEAQQBQ%3D%3D",
}

def search(query, type_filter="songs"):
    cache_key = f"search:{type_filter}:{query}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = _SEARCH_PARAMS.get(type_filter, _SEARCH_PARAMS["songs"])
    data   = _post("search", {"query": query, "params": params})
    tabs = (
        data.get("contents", {})
            .get("tabbedSearchResultsRenderer", {})
            .get("tabs", [{}])[0]
            .get("tabRenderer", {})
            .get("content", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
    )
    if not tabs:
        tabs = (
            data.get("contents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
        )
    results = []
    for section in tabs:
        # Standard song/artist/album shelf
        shelf = section.get("musicShelfRenderer", {})
        if shelf:
            results.extend(_shelf_items(shelf))
            continue

        # Playlist/album top result card
        card = section.get("musicCardShelfRenderer", {})
        if card:
            nav = card.get("onTap", {})
            browse_ep = nav.get("browseEndpoint", {})
            watch_ep  = nav.get("watchEndpoint", {})
            thumb = _thumbnail(
                card.get("thumbnail", {})
                    .get("musicThumbnailRenderer", {})
                    .get("thumbnail", {})
                    .get("thumbnails", [])
            )
            # _text(obj, key) reads obj[key]; passing card["title"] itself
            # returned "" (an unnamed top result, shown as "Unknown Artist").
            title = _text(card) or _text(card, "subtitle")
            if browse_ep.get("browseId"):
                pt = (
                    browse_ep.get("browseEndpointContextSupportedConfigs", {})
                             .get("browseEndpointContextMusicConfig", {})
                             .get("pageType", "")
                )
                results.append({
                    "type":      "album" if "ALBUM" in pt else "artist" if "ARTIST" in pt else "playlist",
                    "title":     title,
                    "name":      title,
                    "browseId":  browse_ep["browseId"],
                    "thumbnail": thumb,
                })
            elif watch_ep.get("videoId"):
                results.append({
                    "type":      "song",
                    "videoId":   watch_ep["videoId"],
                    "title":     title,
                    "thumbnail": thumb,
                })
            continue

        # itemSectionRenderer — individual playlist/artist results
        item_section = section.get("itemSectionRenderer", {})
        if item_section:
            for entry in item_section.get("contents", []):
                r = entry.get("musicResponsiveListItemRenderer")
                if r:
                    item = _classify_and_parse(r)
                    if item:
                        results.append(item)

    _cache_set(cache_key, results)
    return results

# ── Authenticated library endpoints ──────────────────────────────────────────

def get_liked_songs(limit=100):
    """Get user's liked songs (requires auth)."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        result = yt.get_liked_songs(limit=limit)
        items = []
        for track in result.get("tracks", []):
            vid = track.get("videoId")
            if not vid:
                continue
            items.append({
                "type": "song",
                "title": track.get("title", ""),
                "artist": track.get("artists", [{}])[0].get("name", "") if track.get("artists") else "",
                "album": track.get("album", {}).get("name", "") if track.get("album") else "",
                "duration": track.get("duration_seconds", 0),
                "videoId": vid,
                "thumbnail": track.get("thumbnails", [{}])[-1].get("url", "") if track.get("thumbnails") else "",
                "url": f"ytm://{vid}",
            })
        return {"items": items}
    except Exception as e:
        logging.warning("get_liked_songs failed: %s", e)
        return {"error": str(e), "items": []}

def get_library_playlists():
    """Get user's personal playlists (requires auth)."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        playlists = yt.get_library_playlists(limit=50)
        items = []
        for pl in playlists:
            bid = pl.get("playlistId")
            if not bid:
                continue
            items.append({
                "type": "playlist",
                "title": pl.get("title", ""),
                "artist": "",
                "count": pl.get("count", ""),
                "thumbnail": pl.get("thumbnails", [{}])[-1].get("url", "") if pl.get("thumbnails") else "",
                "browseId": f"VL{bid}",
                "url": f"ytmplaylist://VL{bid}",
            })
        return {"items": items}
    except Exception as e:
        logging.warning("get_library_playlists failed: %s", e)
        return {"error": str(e), "items": []}

def get_history():
    """Get user's listening history (requires auth)."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        result = yt.get_history()
        items = []
        for track in result:
            vid = track.get("videoId")
            if not vid:
                continue
            items.append({
                "type": "song",
                "title": track.get("title", ""),
                "artist": track.get("artists", [{}])[0].get("name", "") if track.get("artists") else "",
                "album": track.get("album", {}).get("name", "") if track.get("album") else "",
                "duration": track.get("duration_seconds", 0),
                "videoId": vid,
                "thumbnail": track.get("thumbnails", [{}])[-1].get("url", "") if track.get("thumbnails") else "",
                "url": f"ytm://{vid}",
            })
        return {"items": items}
    except Exception as e:
        logging.warning("get_history failed: %s", e)
        return {"error": str(e), "items": []}

def _ytm_thumb(x):
    thumbs = x.get("thumbnails") or []
    return thumbs[-1].get("url", "") if thumbs else ""

def _ytm_artists(x):
    return ", ".join(a.get("name", "") for a in (x.get("artists") or []) if a.get("name"))

def _ytm_item(x):
    """Map a ytmusicapi home/library entry onto the plugin's item format."""
    if not isinstance(x, dict):
        return None
    thumb = _ytm_thumb(x)
    bid   = x.get("browseId") or ""
    if x.get("videoId"):
        album = x.get("album")
        return {
            "type": "song", "videoId": x["videoId"], "url": f"ytm://{x['videoId']}",
            "title": x.get("title", ""), "artist": _ytm_artists(x),
            "album": album.get("name", "") if isinstance(album, dict) else "",
            "duration": x.get("duration_seconds", 0), "thumbnail": thumb,
        }
    if bid.startswith("MPRE"):
        return {"type": "album", "browseId": bid, "title": x.get("title", ""),
                "artist": _ytm_artists(x), "year": x.get("year", "") or x.get("type", ""),
                "thumbnail": thumb}
    if bid.startswith("UC"):
        name = x.get("title") or x.get("artist") or ""
        return {"type": "artist", "browseId": bid, "name": name, "title": name, "thumbnail": thumb}
    pid = x.get("playlistId") or (bid[2:] if bid.startswith("VL") else "")
    if pid:
        return {"type": "playlist", "browseId": f"VL{pid}", "title": x.get("title", ""),
                "count": x.get("count", ""), "thumbnail": thumb, "url": f"ytmplaylist://VL{pid}"}
    return None

def _ytm_items(rows):
    return [i for i in (_ytm_item(x) for x in rows or []) if i]

def get_personal_home(limit=20):
    """The signed-in home page: Speed dial, Listen again, Quick picks, mixes..."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "sections": []}
    try:
        sections = []
        for sec in yt.get_home(limit=limit):
            items = _ytm_items(sec.get("contents"))
            if items:
                sections.append({"title": sec.get("title") or "", "items": items})
        return {"sections": sections}
    except Exception as e:
        logging.warning("get_home failed: %s", e)
        return {"error": str(e), "sections": []}

def get_library_albums(limit=500):
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        return {"items": _ytm_items(yt.get_library_albums(limit=limit))}
    except Exception as e:
        logging.warning("get_library_albums failed: %s", e)
        return {"error": str(e), "items": []}

def get_library_artists(limit=500):
    """Artists you subscribe to - the closest thing to 'liked artists'."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        return {"items": _ytm_items(yt.get_library_subscriptions(limit=limit))}
    except Exception as e:
        logging.warning("get_library_subscriptions failed: %s", e)
        return {"error": str(e), "items": []}

def get_library_songs(limit=100):
    """Get user's library songs (requires auth)."""
    yt = _get_ytm_auth()
    if not yt:
        return {"error": "not_authenticated", "items": []}
    try:
        result = yt.get_library_songs(limit=limit)
        items = []
        for track in result.get("tracks", []):
            vid = track.get("videoId")
            if not vid:
                continue
            items.append({
                "type": "song",
                "title": track.get("title", ""),
                "artist": track.get("artists", [{}])[0].get("name", "") if track.get("artists") else "",
                "album": track.get("album", {}).get("name", "") if track.get("album") else "",
                "duration": track.get("duration_seconds", 0),
                "videoId": vid,
                "thumbnail": track.get("thumbnails", [{}])[-1].get("url", "") if track.get("thumbnails") else "",
                "url": f"ytm://{vid}",
            })
        return {"items": items}
    except Exception as e:
        logging.warning("get_library_songs failed: %s", e)
        return {"error": str(e), "items": []}

def browse_home():
    cached = _cache_get("home")
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": "FEmusic_home"})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        carousel = section.get("musicCarouselShelfRenderer", {})
        if not carousel:
            continue
        title = _text(
            carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
        )
        items = []
        for entry in carousel.get("contents", []):
            r = entry.get("musicTwoRowItemRenderer", {})
            if not r:
                continue
            nav       = r.get("navigationEndpoint", {})
            browse_ep = nav.get("browseEndpoint", {})
            watch_ep  = nav.get("watchEndpoint", {})
            thumb     = _thumbnail(
                r.get("thumbnailRenderer", {})
                 .get("musicThumbnailRenderer", {})
                 .get("thumbnail", {})
                 .get("thumbnails", [])
            )
            item = {
                "title":     _text(r),
                "subtitle":  _text(r, "subtitle"),
                "thumbnail": thumb,
            }
            if browse_ep.get("browseId"):
                pt = (
                    browse_ep.get("browseEndpointContextSupportedConfigs", {})
                             .get("browseEndpointContextMusicConfig", {})
                             .get("pageType", "")
                )
                item["browseId"]  = browse_ep["browseId"]
                item["pageType"]  = pt
                item["type"] = (
                    "album"  if "ALBUM"  in pt else
                    "artist" if "ARTIST" in pt else
                    "playlist"
                )
            elif watch_ep.get("videoId"):
                item["videoId"] = watch_ep["videoId"]
                item["url"]     = f"ytm://{watch_ep['videoId']}"
                item["type"]    = "song"
            items.append(item)
        if items:
            result.append({"title": title or "Featured", "items": items})
    _cache_set("home", result)
    return result

def browse_charts():
    cached = _cache_get("charts")
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": "FEmusic_charts"})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        # Charts page uses both plain shelves and carousel shelves
        shelf    = section.get("musicShelfRenderer", {})
        carousel = section.get("musicCarouselShelfRenderer", {})
        target   = shelf or carousel
        if not target:
            continue
        # _text(obj) reads obj["title"]; passing the title dict itself
        # returned "" and every section ended up named "Charts".
        if carousel:
            header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
            title  = _text(header)
        else:
            title = _text(shelf)
        # "Video charts" is a carousel of musicTwoRowItemRenderer playlist
        # cards, which _shelf_items (list rows only) dropped - leaving just
        # the Top artists list.
        items = _shelf_items(target)
        if not items and carousel:
            items = _parse_two_row_items(carousel)
        if items:
            result.append({"title": title or "Charts", "items": items})
    _cache_set("charts", result)
    return result

def _parse_two_row_items(carousel):
    """Parse musicTwoRowItemRenderer entries from a carousel or grid."""
    items = []
    for entry in carousel.get("contents", []):
        r = entry.get("musicTwoRowItemRenderer", {})
        if not r:
            continue
        nav       = r.get("navigationEndpoint", {})
        browse_ep = nav.get("browseEndpoint", {})
        watch_ep  = nav.get("watchEndpoint", {})
        thumb     = _thumbnail(
            r.get("thumbnailRenderer", {})
             .get("musicThumbnailRenderer", {})
             .get("thumbnail", {})
             .get("thumbnails", [])
        )
        item = {
            "title":     _text(r),
            "subtitle":  _text(r, "subtitle"),
            "thumbnail": thumb,
        }
        if browse_ep.get("browseId"):
            pt = (
                browse_ep.get("browseEndpointContextSupportedConfigs", {})
                         .get("browseEndpointContextMusicConfig", {})
                         .get("pageType", "")
            )
            item["browseId"] = browse_ep["browseId"]
            item["pageType"] = pt
            item["type"] = (
                "album"  if "ALBUM"  in pt else
                "artist" if "ARTIST" in pt else
                "playlist"
            )
        elif watch_ep.get("videoId"):
            item["videoId"] = watch_ep["videoId"]
            item["url"]     = f"ytm://{watch_ep['videoId']}"
            item["type"]    = "song"
        items.append(item)
    return items


def _parse_nav_button_items(grid):
    """Parse musicNavigationButtonRenderer entries (used by Moods and Genres)."""
    items = []
    for entry in grid.get("items", []):
        r = entry.get("musicNavigationButtonRenderer", {})
        if not r:
            continue
        title     = _text(r, "buttonText")
        cmd       = r.get("clickCommand", {})
        browse_ep = cmd.get("browseEndpoint", {})
        browse_id = browse_ep.get("browseId", "")
        params    = browse_ep.get("params", "")
        if title and browse_id:
            items.append({
                "title":    title,
                "browseId": browse_id,
                "params":   params,
                "type":     "mood_category",
            })
    return items


def browse_new_releases():
    cached = _cache_get("new_releases")
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": "FEmusic_new_releases"})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        carousel = section.get("musicCarouselShelfRenderer", {})
        grid     = section.get("gridRenderer", {})
        if carousel:
            header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
            title  = _text(header.get("title", {}))
            items  = _parse_two_row_items(carousel)
        elif grid:
            items  = _parse_two_row_items({"contents": grid.get("items", [])})
            title  = ""
        else:
            continue
        if items:
            result.append({"title": title or "New Releases", "items": items})
    _cache_set("new_releases", result)
    return result


def browse_moods():
    cached = _cache_get("moods")
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": "FEmusic_moods_and_genres"})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        grid = section.get("gridRenderer", {})
        if not grid:
            continue
        items = _parse_nav_button_items(grid)
        if items:
            result.append({"title": "Moods and Genres", "items": items})
    _cache_set("moods", result)
    return result


def browse_mood_category(browse_id, params):
    cache_key = f"mood:{browse_id}:{params}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": browse_id, "params": params})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        carousel = section.get("musicCarouselShelfRenderer", {})
        grid     = section.get("gridRenderer", {})
        if carousel:
            header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
            title  = _text(header.get("title", {}))
            items  = _parse_two_row_items(carousel)
        elif grid:
            items  = _parse_two_row_items({"contents": grid.get("items", [])})
            title  = ""
        else:
            continue
        if items:
            result.append({"title": title or "Playlists", "items": items})
    _cache_set(cache_key, result)
    return result


def browse_podcasts():
    cached = _cache_get("podcasts")
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": "FEmusic_podcasts"})
    sections = _single_col_sections(data)
    result   = []
    for section in sections:
        carousel = section.get("musicCarouselShelfRenderer", {})
        if not carousel:
            continue
        header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
        title  = _text(header.get("title", {}))
        items  = _parse_two_row_items(carousel)
        if items:
            result.append({"title": title or "Podcasts", "items": items})
    _cache_set("podcasts", result)
    return result


def browse_radio(video_id):
    """Return a list of song items for a YouTube Music radio seeded by video_id."""
    cache_key = f"radio:{video_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _post("next", {
        "videoId":    video_id,
        "playlistId": f"RDAMVM{video_id}",
        "isAudioOnly": True,
        "watchEndpointMusicSupportedConfigs": {
            "watchEndpointMusicConfig": {
                "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"
            }
        }
    })

    items = (
        data.get("contents", {})
            .get("singleColumnMusicWatchNextResultsRenderer", {})
            .get("tabbedRenderer", {})
            .get("watchNextTabbedResultsRenderer", {})
            .get("tabs", [])[0]
            .get("tabRenderer", {})
            .get("content", {})
            .get("musicQueueRenderer", {})
            .get("content", {})
            .get("playlistPanelRenderer", {})
            .get("contents", [])
    )

    result = []
    for item in items:
        r = item.get("playlistPanelVideoRenderer", {})
        vid = r.get("videoId", "")
        if not vid:
            continue
        thumb = _thumbnail(
            r.get("thumbnail", {}).get("thumbnails", [])
        )
        result.append({
            "type":      "song",
            "videoId":   vid,
            "title":     _text_runs(r.get("title", {})) or f"YouTube Music - {vid}",
            "artist":    _text_runs(r.get("longBylineText", {})).split(" • ")[0].split(" • ")[0],
            "thumbnail": thumb,
        })

    _cache_set(cache_key, result)
    return result


def browse_olak_playlist(playlist_id):
    """Resolve OLAK5uy_ album/playlist IDs from YouTube Music browser URLs.
    Uses the YouTube WEB client — no authentication required."""
    import urllib.request as _req
    cache_key = f"olak:{playlist_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    yt_headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20260811.01.00",
    }
    yt_client = {"clientName": "WEB", "clientVersion": "2.20260811.01.00", "hl": "en", "gl": "US"}
    payload = json.dumps({"context": {"client": yt_client}, "browseId": "VL" + playlist_id}).encode()
    req = _req.Request(
        "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
        data=payload, headers=yt_headers, method="POST"
    )
    with _req.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    items = []
    seen = set()

    def _get_text(o):
        if isinstance(o, str): return o
        if isinstance(o, dict):
            if "content" in o: return o["content"]
            runs = o.get("runs", [])
            if runs: return "".join(r.get("text","") for r in runs)
            return o.get("simpleText", "")
        return ""

    def _extract_lockup(lockup):
        vid = lockup.get("contentId", "")
        if not vid or vid in seen: return
        seen.add(vid)
        meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = _get_text(meta.get("title", {}))
        rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        artist = ""
        if rows and rows[0].get("metadataParts"):
            parts = rows[0]["metadataParts"]
            if parts: artist = _get_text(parts[0].get("text", {}))
        duration = ""
        try:
            for badge in lockup["contentImage"]["thumbnailViewModel"]["overlays"][0]["thumbnailBottomOverlayViewModel"]["badges"]:
                t = badge.get("thumbnailBadgeViewModel", {}).get("text", "")
                if t and ":" in t:
                    # Convert "m:ss" or "h:mm:ss" to seconds for LMS
                    duration = _duration_to_secs(t)
                    break
        except (KeyError, IndexError, TypeError): pass
        thumbnail = ""
        try:
            sources = lockup["contentImage"]["thumbnailViewModel"]["image"]["sources"]
            if sources: thumbnail = sources[-1].get("url", "")
        except (KeyError, IndexError, TypeError): pass
        items.append({"type": "song", "title": title, "artist": artist, "album": "",
                      "duration": duration, "videoId": vid, "thumbnail": thumbnail, "url": "ytm://" + vid})

    def walk(obj):
        if isinstance(obj, dict):
            if "lockupViewModel" in obj:
                _extract_lockup(obj["lockupViewModel"])
            else:
                for v in obj.values():
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(data)
    result = {"browseId": playlist_id, "items": items}
    _cache_set(cache_key, result)
    return result

def _album_header(data):
    """Return the musicResponsiveHeaderRenderer of an album page, or {}."""
    tabs = (
        data.get("contents", {})
            .get("twoColumnBrowseResultsRenderer", {})
            .get("tabs", [])
    )
    for tab in tabs:
        sections = (
            tab.get("tabRenderer", {})
               .get("content", {})
               .get("sectionListRenderer", {})
               .get("contents", [])
        )
        for section in sections:
            header = section.get("musicResponsiveHeaderRenderer")
            if header:
                return header
    return {}

def _fill_album_track_metadata(data, items):
    """Album track rows carry only title, play count and duration: the artist
    column is empty on single-artist albums, the third column (parsed as
    "album") is the play count, and there is no per-track artwork. Take the
    album title, artist and cover from the page header instead."""
    header = _album_header(data)
    if not header:
        return
    album  = _text(header, "title")
    artist = _text(header, "straplineTextOne")
    thumb  = _thumb_from_renderer(header)
    for item in items:
        if item.get("type") != "song":
            continue
        if album:
            item["album"] = album
        if artist and not item.get("artist"):
            item["artist"] = artist
        if thumb and not item.get("thumbnail"):
            item["thumbnail"] = thumb

def _album_track_rows(data):
    """The raw track rows of an album page, in album order."""
    sections = (
        data.get("contents", {})
            .get("twoColumnBrowseResultsRenderer", {})
            .get("secondaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
    )
    rows = []
    for section in sections:
        for entry in section.get("musicShelfRenderer", {}).get("contents", []):
            r = entry.get("musicResponsiveListItemRenderer")
            if r:
                rows.append(r)
    return rows

def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "").lower()).strip()

# Title/artist/duration of every track the proxy has listed to LMS, so a
# track that turns out to be unplayable can be swapped for a current copy.
_TRACK_META = {}
_TRACK_META_MAX = 20000

def _remember_tracks(obj):
    """Record song metadata from any JSON payload about to be sent to LMS."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            vid = o.get("videoId")
            if vid and o.get("title") and o.get("type", "song") == "song":
                if len(_TRACK_META) >= _TRACK_META_MAX:
                    _TRACK_META.clear()
                _TRACK_META[vid] = {
                    "title":    o.get("title", ""),
                    "artist":   o.get("artist", ""),
                    "album":    o.get("album", ""),
                    "duration": o.get("duration") or 0,
                }
            stack.extend(v for v in o.values() if isinstance(v, (dict, list)))
        elif isinstance(o, list):
            stack.extend(v for v in o if isinstance(v, (dict, list)))

def _replacement_for_unplayable(video_id):
    """Video ID of a playable copy of a track we listed, or None."""
    meta = _TRACK_META.get(video_id)
    if not meta:
        return None
    rep = _find_replacement_track(meta["artist"], meta["title"],
                                  meta["duration"], meta["album"])
    if rep and rep.get("videoId") and rep["videoId"] != video_id:
        return rep["videoId"]
    return None

def _find_replacement_track(artist, title, duration, album=""):
    """A playable copy of an album track that YouTube greys out.

    When a release is superseded (re-issue, artist renamed, ...), the old
    album page keeps listing its tracks but greys them out; their video IDs
    are dead ("Video unavailable"). The YouTube Music apps quietly play the
    current release's copy instead. Find it by searching for the song and
    accepting only a result with the same title (optionally prefixed
    "Artist - ") and a duration within 5 seconds, which rules out live
    versions, edits and remixes."""
    want = _norm_title(title)
    try:
        results = search(f"{artist} {title}".strip(), "songs")
    except Exception as e:
        logging.warning("replacement search failed for %s: %s", title, e)
        return None
    candidates = []
    for r in results:
        if r.get("type") != "song" or not r.get("videoId"):
            continue
        got = _norm_title(r.get("title"))
        if got != want and not got.endswith(" - " + want):
            continue
        if duration and r.get("duration") and abs(r["duration"] - duration) > 5:
            continue
        candidates.append(r)
    # Prefer a copy on an album of the same name (the re-issue usually is)
    same_album = [r for r in candidates if album and _norm_title(r.get("album")) == _norm_title(album)]
    return (same_album or candidates or [None])[0]

def _with_replaced_album_tracks(data, items):
    """Album items in album order, with greyed-out tracks swapped for a
    playable copy where one can be found (see _find_replacement_track).
    Albums without greyed-out tracks are returned unchanged."""
    rows = _album_track_rows(data)
    missing = [i for i, r in enumerate(rows) if not _video_id_from_overlay(r)]
    if not missing:
        return items
    header = _album_header(data)
    artist = _text(header, "straplineTextOne")
    album  = _text(header, "title")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = dict(zip(missing, pool.map(
            lambda i: _find_replacement_track(
                artist, _col(rows[i], 0),
                _duration_to_secs(_fixed_col(rows[i], 0)), album),
            missing)))
    by_vid = {it.get("videoId"): it for it in items if it.get("videoId")}
    out = []
    for i, r in enumerate(rows):
        vid = _video_id_from_overlay(r)
        if vid:
            if vid in by_vid:
                out.append(by_vid[vid])
            continue
        rep = found.get(i)
        if rep:
            out.append({
                "type": "song", "videoId": rep["videoId"], "url": f"ytm://{rep['videoId']}",
                "title": _col(r, 0), "artist": "", "album": "",
                "duration": _duration_to_secs(_fixed_col(r, 0)) or rep.get("duration", 0),
                "thumbnail": "",
            })
    logging.info("album: %d greyed-out track(s), %d replaced", len(missing), sum(1 for v in found.values() if v))
    # keep anything the page had outside the track shelf (none expected)
    seen = {it.get("videoId") for it in out}
    out.extend(it for it in items if it.get("videoId") not in seen)
    return out

def _signed_in_playlist_items(playlist_id):
    """Tracks of a playlist fetched with the user's login, or [] if not
    signed in or it fails."""
    yt = _get_ytm_auth()
    if not yt:
        return []
    try:
        tracks = yt.get_playlist(playlist_id, limit=None).get("tracks") or []
    except Exception as e:
        logging.warning("signed-in get_playlist failed for %s: %s", playlist_id, e)
        return []
    return [i for i in _ytm_items(tracks) if i.get("type") == "song"]

def browse_playlist(browse_id):
    cache_key = f"playlist:{browse_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = _post("browse", {"browseId": browse_id})
    items = []

    # Playlists/albums use a two-column layout with the track list in
    # secondaryContents, not the single-column layout used by browse_home.
    two_col = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {})
    if two_col:
        sections = (
            two_col.get("secondaryContents", {})
                   .get("sectionListRenderer", {})
                   .get("contents", [])
        )
        for section in sections:
            for key in ("musicShelfRenderer", "musicPlaylistShelfRenderer", "musicCarouselShelfRenderer"):
                target = section.get(key, {})
                if target:
                    # Check for podcast episodes (musicMultiRowListItemRenderer)
                    for entry in target.get("contents", []):
                        if "musicMultiRowListItemRenderer" in entry:
                            ep = _parse_podcast_episode(entry["musicMultiRowListItemRenderer"])
                            if ep:
                                items.append(ep)
                        else:
                            items.extend(_shelf_items({"contents": [entry]}))
    else:
        sections = _single_col_sections(data)
        for section in sections:
            for key in ("musicShelfRenderer", "musicCarouselShelfRenderer", "musicPlaylistShelfRenderer"):
                target = section.get(key, {})
                if target:
                    for entry in target.get("contents", []):
                        if "musicMultiRowListItemRenderer" in entry:
                            ep = _parse_podcast_episode(entry["musicMultiRowListItemRenderer"])
                            if ep:
                                items.append(ep)
                        else:
                            items.extend(_shelf_items({"contents": [entry]}))

    if browse_id.startswith("MPREb_"):
        items = _with_replaced_album_tracks(data, items)
        _fill_album_track_metadata(data, items)

    # Private playlists (most of a user's own) come back empty to the
    # anonymous request above. When signed in, fetch them through ytmusicapi.
    if not items and browse_id.startswith("VL"):
        items = _signed_in_playlist_items(browse_id[2:])

    result = {"browseId": browse_id, "items": items}
    _cache_set(cache_key, result)
    return result

def browse_artist(browse_id):
    cache_key = f"artist:{browse_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data     = _post("browse", {"browseId": browse_id})
    sections = _single_col_sections(data)
    artist_name = _text(
        data.get("header", {}).get("musicImmersiveHeaderRenderer", {})
    ) or _text(
        data.get("header", {}).get("musicVisualHeaderRenderer", {})
    )
    result = {"name": artist_name, "sections": []}
    for section in sections:
        carousel = section.get("musicCarouselShelfRenderer", {})
        shelf    = section.get("musicShelfRenderer", {})
        target   = carousel or shelf
        if not target:
            continue
        sec_title = _text(
            target.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
        ) or _text(target)
        # Top songs is a list shelf; Albums, Singles, Videos, Fans might also
        # like etc. are carousels of musicTwoRowItemRenderer cards.
        items = _shelf_items(target) if shelf else _parse_two_row_items(target)
        for item in items:
            if item.get("type") == "artist":
                item.setdefault("name", item.get("title", ""))
            elif item.get("type") == "album":
                item.setdefault("year", item.get("subtitle", ""))
        if items:
            sec = {"title": sec_title, "items": items}
            more = _artist_section_more(target)
            if more:
                sec["more"] = more
            result["sections"].append(sec)
    _cache_set(cache_key, result)
    return result

def _artist_section_more(carousel):
    """The Albums and Singles & EPs carousels show ~10 cards; their header
    links to the artist's full discography grid (an MPAD... browse ID)."""
    header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
    for ep in (
        header.get("title", {}).get("runs", [{}])[0].get("navigationEndpoint", {}),
        header.get("moreContentButton", {}).get("buttonRenderer", {}).get("navigationEndpoint", {}),
    ):
        browse_ep = ep.get("browseEndpoint", {})
        if browse_ep.get("browseId", "").startswith("MPAD"):
            return {"browseId": browse_ep["browseId"], "params": browse_ep.get("params", "")}
    return None

def browse_artist_discography(browse_id, params):
    """Full album or singles list behind an artist page's "More" link."""
    cache_key = f"discography:{browse_id}:{params}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data  = _post("browse", {"browseId": browse_id, "params": params})
    items = []
    for section in _single_col_sections(data):
        grid = section.get("gridRenderer", {})
        if grid:
            items.extend(_parse_two_row_items({"contents": grid.get("items", [])}))
    for item in items:
        if item.get("type") == "album":
            item.setdefault("year", item.get("subtitle", ""))
    result = {"items": items}
    _cache_set(cache_key, result)
    return result

def get_song_info(video_id):
    data = _post("player", {
        "videoId":    video_id,
        "playlistId": f"RDAMVM{video_id}",
    })
    vd = data.get("videoDetails", {})
    return {
        "videoId":   vd.get("videoId", video_id),
        "title":     vd.get("title", ""),
        "artist":    vd.get("author", ""),
        "duration":  int(vd.get("lengthSeconds", 0)),
        "thumbnail": _thumbnail(
            vd.get("thumbnail", {}).get("thumbnails", [])
        ),
        "playable":  data.get("playabilityStatus", {}).get("status") == "OK",
    }


def get_song_links(video_id, title="", artist="", _replacing=False):
    """The artist(s) and album a song links to, as the YouTube Music player
    shows them under the title - for "Go to artist" in the track menu.

    A dead video ID (lists such as Liked Songs keep IDs saved years ago)
    links to nothing; then use the current copy of the song, as playback
    does. title/artist are what LMS shows, for when we haven't listed it."""
    key = f"song_links:{video_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _post("next", {"videoId": video_id, "isAudioOnly": True})

    def renderers(o):
        if isinstance(o, dict):
            if "playlistPanelVideoRenderer" in o:
                yield o["playlistPanelVideoRenderer"]
            for v in o.values():
                yield from renderers(v)
        elif isinstance(o, list):
            for v in o:
                yield from renderers(v)

    r = next((r for r in renderers(data) if r.get("videoId") == video_id), {})
    result = {"artists": [], "album": None}
    for run in r.get("longBylineText", {}).get("runs", []):
        bid = run.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
        if bid.startswith("UC"):
            result["artists"].append({"name": run.get("text", ""), "browseId": bid})
        elif bid.startswith("MPRE") and not result["album"]:
            result["album"] = {"title": run.get("text", ""), "browseId": bid}
    if not result["artists"] and not _replacing:
        meta = _TRACK_META.get(video_id) or {"title": title, "artist": artist, "duration": 0, "album": ""}
        rep = meta.get("title") and _find_replacement_track(meta["artist"], meta["title"],
                                                            meta["duration"], meta["album"])
        if rep and rep.get("videoId") and rep["videoId"] != video_id:
            logging.info("song links for %s: using current copy %s", video_id, rep["videoId"])
            result = get_song_links(rep["videoId"], _replacing=True)
    _cache_set(key, result)
    return result


# ---- Audio streaming via yt-dlp | ffmpeg ----

# ---- Prefetch cache (disk-backed) ----
# To mask yt-dlp's ~8-10 second resolution latency between tracks, the Perl
# protocol handler asks us to start resolving the *next* track in the
# background while the current one is still playing. We write the fully
# resolved MP3 to a temp file; if /stream/<id> is requested before this
# finishes, it falls back to live (uncached) resolution as before.

# ---- Audio codec detection ----
# Probe ffmpeg at startup to find the best available audio encoder.
# piCorePlayer's ffmpeg lacks libmp3lame so we fall back to aac.
def _detect_audio_codec():
    try:
        result = subprocess.run(
            [_find_ffmpeg(), "-encoders", "-v", "quiet"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        if "libmp3lame" in output:
            logging.info("ffmpeg codec: libmp3lame (MP3)")
            return "libmp3lame", "mp3", "audio/mpeg"
        elif "flac" in output:
            logging.info("ffmpeg codec: flac (FLAC fallback)")
            return "flac", "flac", "audio/flac"
        elif "aac" in output:
            # AAC preferred over FLAC — LMS won't need to transcode AAC
            # for most players, avoiding double-transcoding stuttering
            logging.info("ffmpeg codec: aac (AAC fallback)")
            return "aac", "adts", "audio/aac"
        else:
            logging.warning("No suitable ffmpeg codec found, defaulting to mp3")
            return "libmp3lame", "mp3", "audio/mpeg"
    except Exception as e:
        logging.warning("ffmpeg codec detection failed: %s", e)
        return "libmp3lame", "mp3", "audio/mpeg"

_AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = _detect_audio_codec()

PREFETCH_DIR = os.path.join(tempfile.gettempdir(), "ytmproxy_prefetch")
_prefetch_started = set()
_prefetch_lock = threading.Lock()
_prefetch_semaphore = threading.Semaphore(2)  # Max 2 concurrent prefetch downloads
_urgent_running = set()  # videos being urgently streamed (bypass semaphore)

def _prefetch_paths(video_id):
    os.makedirs(PREFETCH_DIR, exist_ok=True)
    ext = _AUDIO_FORMAT if _AUDIO_FORMAT != "adts" else "aac"
    tmp_path  = os.path.join(PREFETCH_DIR, f"{video_id}.{ext}.part")
    done_path = os.path.join(PREFETCH_DIR, f"{video_id}.{ext}")
    return tmp_path, done_path


def _cleanup_old_prefetch(max_age=600):
    # On Linux/Mac, deleting an open file is safe — the inode stays alive
    # until the last file handle closes so streaming continues uninterrupted.
    # On Windows, open files are locked and deletion will raise PermissionError;
    # we catch that and skip the file — it will be cleaned up on next restart.
    try:
        now = time.time()
        for name in os.listdir(PREFETCH_DIR):
            full = os.path.join(PREFETCH_DIR, name)
            if not os.path.isfile(full):
                continue
            size = os.path.getsize(full)
            # Allow ~1 second per 32KB as a rough track-length estimate
            # so a 30MB file (approx 15-20 min) gets ~960s grace period
            age_limit = max(max_age, size // 32768)
            if (now - os.path.getmtime(full)) > age_limit:
                try:
                    os.remove(full)
                except PermissionError:
                    logging.debug("Cannot delete %s (file in use on Windows)", full)
                except OSError as e:
                    logging.debug("Cannot delete %s: %s", full, e)
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("Prefetch cleanup error")

def _urgent_stream_worker(video_id):
    """Download video_id WITHOUT the semaphore — used when LMS needs it NOW.
    Writes to the same tmp/done paths as the normal prefetch so the stream
    handler's wait loop picks it up transparently.
    Registers in _urgent_running so _prefetch_worker skips this video."""
    tmp_path, done_path = _prefetch_paths(video_id)
    if os.path.exists(done_path) and os.path.getsize(done_path) > 0:
        return  # already done by normal prefetch that beat us here
    with _prefetch_lock:
        _urgent_running.add(video_id)
    t0 = time.time()
    logging.warning("URGENT_STREAM %s starting (bypassing semaphore)", video_id)
    try:
        with open(tmp_path, "wb") as f:
            for chunk in stream_audio(video_id):
                f.write(chunk)
                f.flush()
        os.replace(tmp_path, done_path)
        logging.warning("URGENT_STREAM %s complete after %.2fs", video_id, time.time() - t0)
    except Exception:
        logging.exception("Urgent stream failed for %s", video_id)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    finally:
        with _prefetch_lock:
            _urgent_running.discard(video_id)
            _prefetch_started.discard(video_id)


def _prefetch_worker(video_id):
    tmp_path, done_path = _prefetch_paths(video_id)
    with _prefetch_semaphore:
        # If urgent stream already handled this (bypassed semaphore while we waited),
        # don't overwrite tmp_path — that would corrupt an in-progress stream.
        with _prefetch_lock:
            if video_id in _urgent_running:
                logging.warning("PREFETCH skip %s — urgent stream already running", video_id)
                return
        if os.path.exists(done_path) and os.path.getsize(done_path) > 0:
            return  # urgent stream completed while we waited for semaphore
        t0 = time.time()
        logging.warning("PREFETCH_TIMING %s started", video_id)
        try:
            logged_first = False
            logged_128k = False
            with open(tmp_path, "wb") as f:
                for chunk in stream_audio(video_id):
                    if not logged_first:
                        logging.warning("PREFETCH_TIMING %s first byte after %.2fs", video_id, time.time()-t0)
                        logged_first = True
                    f.write(chunk)
                    f.flush()
                    if not logged_128k and os.path.getsize(tmp_path) >= 131072:
                        logging.warning("PREFETCH_TIMING %s 128KB after %.2fs", video_id, time.time()-t0)
                        logged_128k = True
            os.replace(tmp_path, done_path)
            logging.warning("PREFETCH_TIMING %s complete after %.2fs", video_id, time.time()-t0)
        except Exception:
            logging.exception("Prefetch failed for %s", video_id)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        finally:
            with _prefetch_lock:
                _prefetch_started.discard(video_id)
def start_prefetch(video_id):
    _, done_path = _prefetch_paths(video_id)
    if os.path.exists(done_path):
        return "already_cached"

    with _prefetch_lock:
        if video_id in _prefetch_started:
            return "in_progress"
        _prefetch_started.add(video_id)

    _cleanup_old_prefetch()
    t = threading.Thread(target=_prefetch_worker, args=(video_id,), daemon=True)
    t.start()
    return "started"


def get_prefetched_path(video_id):
    """Return the path to a fully-cached file for video_id, or None.
    Returns None if the file is missing or empty (failed prefetch)."""
    _, done_path = _prefetch_paths(video_id)
    if os.path.exists(done_path) and os.path.getsize(done_path) > 0:
        return done_path
    # Clean up zero-byte files so they don't block future prefetch attempts
    if os.path.exists(done_path):
        try:
            os.remove(done_path)
        except OSError:
            pass
    return None
# Plugin directory — yt-dlp binary stored in Bin/ subdir per LMS convention
# LMS automatically adds <plugin>/Bin to PATH so it will be found system-wide
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# Store binaries in a persistent location outside the plugin directory
# so they survive plugin updates on all platforms.
# Plugin dir is: <cache>/InstalledPlugins/Plugins/YouTubeMusic
# We store binaries at: <cache>/YouTubeMusic/Bin
_cache_dir = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_DIR)))
BIN_DIR = os.path.join(_cache_dir, "YouTubeMusic", "Bin")
_ytdlp_exe = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
YTDLP_BIN  = os.path.join(BIN_DIR, _ytdlp_exe)

def _platform_ytdlp_asset():
    """Return the yt-dlp GitHub asset name for the current platform."""
    import platform
    machine = platform.machine().lower()
    system  = platform.system().lower()
    if system == "windows":
        return "yt-dlp.exe", False
    if system == "darwin":
        return "yt-dlp_macos.zip", True  # onedir version avoids Gatekeeper rescan on every run
    # Linux
    if machine in ("aarch64", "arm64"):
        return "yt-dlp_linux_aarch64", False
    if machine == "armv7l":
        return "yt-dlp_linux_armv7l.zip", True
    if machine == "armv6l":
        # No binary available for armv6l, fall back to pip
        return None, False
    if machine == "x86_64":
        return "yt-dlp_linux", False
    # fallback — generic Python wheel
    return "yt-dlp", False


def download_ytdlp():
    """Download the latest yt-dlp binary into the plugin directory.
    Returns (ok, message) tuple."""
    import urllib.request, zipfile, io
    try:
        # Get latest release info
        api_url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            release = json.loads(resp.read())

        version  = release["tag_name"]
        asset_name, is_zip = _platform_ytdlp_asset()

        # If no binary available for this platform, use system yt-dlp or pip
        if asset_name is None:
            logging.info("No binary available for this platform, checking system yt-dlp")
            os.makedirs(BIN_DIR, exist_ok=True)
            wrapper = os.path.join(BIN_DIR, "yt-dlp")
            # Check if yt-dlp is already installed system-wide
            system_ytdlp = shutil.which("yt-dlp") or shutil.which("yt_dlp")
            # Always download wheel for ARM so it can be updated later
            # Download wheel and install to BIN_DIR
            logging.info("Downloading yt-dlp wheel for this platform")
            wheel_url = None
            for asset in release["assets"]:
                if asset["name"].endswith(".whl"):
                    wheel_url = asset["browser_download_url"]
                    break
            if not wheel_url:
                # Try PyPI
                # Convert tag format (2026.07.04) to PyPI format (2026.7.4)
                pypi_ver = ".".join(str(int(x)) for x in version.split("."))
                wheel_url = f"https://files.pythonhosted.org/packages/py3/y/yt-dlp/yt_dlp-{pypi_ver}-py3-none-any.whl"
            with urllib.request.urlopen(wheel_url, timeout=60) as resp:
                wheel_data = resp.read()
            import zipfile, io
            ytdlp_pkg = os.path.join(BIN_DIR, "yt_dlp")
            with zipfile.ZipFile(io.BytesIO(wheel_data)) as zf:
                zf.extractall(BIN_DIR)
            # Create wrapper script
            with open(wrapper, "w") as f:
                f.write(f"#!/bin/sh\nPYTHONPATH={BIN_DIR}:$PYTHONPATH exec {sys.executable} -m yt_dlp \"$@\"\n")
            os.chmod(wrapper, 0o755)
            return True, version

        # Find download URL
        dl_url = None
        for asset in release["assets"]:
            if asset["name"] == asset_name:
                dl_url = asset["browser_download_url"]
                break

        if not dl_url:
            return False, f"No asset found for {asset_name}"

        # Ensure Bin directory exists
        os.makedirs(BIN_DIR, exist_ok=True)
        logging.info("Downloading yt-dlp %s (%s)", version, asset_name)

        with urllib.request.urlopen(dl_url, timeout=120) as resp:
            data = resp.read()

        if is_zip:
            # Extract entire ZIP to Bin/ directory (includes _internal libs)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(BIN_DIR)
            # Find the main binary in the extracted files
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
            bin_name = next(
                (n for n in names if (n.endswith("yt-dlp") or n.startswith("yt-dlp")) and "/" not in n.rstrip("/")),
                None
            )
            if not bin_name:
                bin_name = next(
                    (n for n in names if not n.endswith("/") and "/" not in n),
                    None
                )
            extracted_bin = os.path.join(BIN_DIR, bin_name) if bin_name else YTDLP_BIN
            os.chmod(extracted_bin, 0o755)
            # Create symlink to standard YTDLP_BIN path if different
            if extracted_bin != YTDLP_BIN:
                if os.path.exists(YTDLP_BIN):
                    os.remove(YTDLP_BIN)
                os.symlink(extracted_bin, YTDLP_BIN)
        else:
            # Write single binary to plugin directory
            with open(YTDLP_BIN, "wb") as f:
                f.write(data)
            os.chmod(YTDLP_BIN, 0o755)

        logging.info("yt-dlp %s installed to %s", version, YTDLP_BIN)
        return True, version

    except Exception as e:
        logging.exception("Failed to download yt-dlp")
        return False, str(e)


def download_ffmpeg():
    """Download ffmpeg binary into BIN_DIR. Returns (ok, message)."""
    if os.name != "nt":
        return False, "ffmpeg download only supported on Windows — use your package manager on Linux/Mac"
    try:
        import urllib.request, zipfile, io
        os.makedirs(BIN_DIR, exist_ok=True)
        api_url = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            release = json.loads(resp.read())
        # Find the win64 gpl build (non-shared, smallest)
        dl_url = None
        for asset in release["assets"]:
            name = asset["name"]
            if "win64" in name and "gpl.zip" in name and "shared" not in name:
                dl_url = asset["browser_download_url"]
                break
        if not dl_url:
            return False, "Could not find ffmpeg Windows build"
        logging.info("Downloading ffmpeg from %s", dl_url)
        with urllib.request.urlopen(dl_url, timeout=300) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Find ffmpeg.exe in the zip
            ffmpeg_entry = next((n for n in zf.namelist() if n.endswith("/ffmpeg.exe") or n.endswith("/bin/ffmpeg.exe")), None)
            if not ffmpeg_entry:
                return False, "ffmpeg.exe not found in zip"
            with zf.open(ffmpeg_entry) as src, open(os.path.join(BIN_DIR, "ffmpeg.exe"), "wb") as dst:
                dst.write(src.read())
        logging.info("ffmpeg installed to %s", BIN_DIR)
        return True, "ok"
    except Exception as e:
        logging.exception("Failed to download ffmpeg")
        return False, str(e)

NODE_BIN = os.path.join(BIN_DIR, "node22", "node")
NODE_MIN_VERSION = (22, 0, 0)

def _check_node_version(candidate):
    """Return True if candidate Node binary is v22+."""
    try:
        result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
        ver_str = result.stdout.strip().lstrip("v")
        parts = tuple(int(x) for x in ver_str.split(".")[:3])
        return parts >= NODE_MIN_VERSION
    except Exception:
        return False

def _find_node():
    _node_override = os.environ.get("YTM_NODE_OVERRIDE")
    if _node_override and os.path.isfile(_node_override):
        return _node_override
    """Find a suitable Node.js binary (v22+). Returns path or None."""
    # Check plugin's own node22 first
    if os.path.exists(NODE_BIN) and os.access(NODE_BIN, os.X_OK):
        if _check_node_version(NODE_BIN):
            return NODE_BIN
    # Check system node
    win_candidates = []
    if os.name == 'nt':
        # Common Windows Node.js install locations
        for drive in ['C', 'D', 'E']:
            win_candidates += [
                f"{drive}:\Program Files\nodejs\node.exe",
                f"{drive}:\Program Files (x86)\nodejs\node.exe",
            ]
        # Per-user installs
        appdata = os.environ.get('APPDATA', '')
        localappdata = os.environ.get('LOCALAPPDATA', '')
        if appdata:
            win_candidates.append(os.path.join(appdata, 'nvm', 'current', 'node.exe'))
        if localappdata:
            win_candidates.append(os.path.join(localappdata, 'Programs', 'node', 'node.exe'))
        # Scan all user profiles
        for drive in ['C', 'D']:
            users_dir = drive + ':\\Users'
            if os.path.isdir(users_dir):
                try:
                    for user in os.listdir(users_dir):
                        win_candidates.append(users_dir + '\\' + user + '\\AppData\\Roaming\\nvm\\current\\node.exe')
                        win_candidates.append(users_dir + '\\' + user + '\\AppData\\Local\\Programs\\node\\node.exe')
                except Exception:
                    pass
    for candidate in ["/usr/bin/node", "/usr/local/bin/node", shutil.which("node")] + win_candidates:
        if candidate and os.path.exists(candidate):
            try:
                result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=5)
                ver_str = result.stdout.strip().lstrip("v")
                parts = tuple(int(x) for x in ver_str.split(".")[:3])
                if parts >= NODE_MIN_VERSION:
                    return candidate
            except Exception:
                pass
    return None

def _find_ffmpeg():
    """Find ffmpeg binary, respecting override environment variable."""
    override = os.environ.get('YTM_FFMPEG_OVERRIDE')
    if override and os.path.isfile(override):
        return override
    # Check plugin Bin directory first
    bin_ffmpeg = os.path.join(BIN_DIR, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if os.path.isfile(bin_ffmpeg):
        return bin_ffmpeg
    # Check common paths
    for candidate in [
        shutil.which('ffmpeg'),
        '/usr/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        '/opt/homebrew/bin/ffmpeg',
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return 'ffmpeg'  # fallback, may fail

def _platform_node_asset():
    """Return (filename, url_template) for the Node 22 ARMv7/x64/arm64 binary."""
    import platform as _platform
    machine = _platform.machine().lower()
    system = _platform.system().lower()
    if system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return f"node-v22.23.2-darwin-{arch}.tar.gz"
    if system == "windows" or os.name == "nt":
        return "node-v22.23.2-win-x64.zip"
    # Linux
    if machine in ("aarch64", "arm64"):
        return "node-v22.23.2-linux-arm64.tar.xz"
    if machine in ("armv7l", "armv6l", "armhf"):
        return "node-v22.23.2-linux-armv7l.tar.xz"
    return "node-v22.23.2-linux-x64.tar.xz"

def download_node():
    """Download Node.js v22 into the plugin Bin directory. Returns (ok, message)."""
    import urllib.request, tarfile, io
    try:
        filename = _platform_node_asset()
        url = f"https://nodejs.org/dist/v22.23.2/{filename}"
        logging.info("Downloading Node.js from %s", url)
        os.makedirs(os.path.join(BIN_DIR, "node22"), exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        if filename.endswith(".tar.xz") or filename.endswith(".tar.gz"):
            mode = "r:xz" if filename.endswith(".xz") else "r:gz"
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                # Extract just the node binary
                for member in tf.getmembers():
                    if member.name.endswith("/bin/node"):
                        member.name = "node.tmp"
                        tf.extract(member, os.path.join(BIN_DIR, "node22"))
                        break
        elif filename.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.endswith("/node.exe"):
                        with zf.open(name) as src:
                            with open(os.path.join(BIN_DIR, "node22", "node.exe"), "wb") as dst:
                                dst.write(src.read())
                        break
        node_path = os.path.join(BIN_DIR, "node22", "node")
        node_tmp = node_path + ".tmp"
        if os.path.exists(node_tmp):
            os.chmod(node_tmp, 0o755)
            # Verify before making permanent
            try:
                result = subprocess.run([node_tmp, "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and _check_node_version(node_tmp):
                    os.rename(node_tmp, node_path)
                    logging.info("Node.js %s installed to %s", result.stdout.strip(), node_path)
                    return True, result.stdout.strip()
                else:
                    os.remove(node_tmp)
                    return False, "downloaded Node failed version check"
            except Exception as e:
                try: os.remove(node_tmp)
                except: pass
                return False, f"Node verification failed: {e}"
        if os.path.exists(node_path):
            os.chmod(node_path, 0o755)
            logging.info("Node.js v22 installed to %s", node_path)
            return True, "v22.23.2"
        return False, "node binary not found after extraction"
    except Exception as e:
        logging.exception("Failed to download Node.js")
        return False, str(e)

def _install_ytdlp_ejs():
    """Install yt-dlp-ejs into BIN_DIR if not already present."""
    ejs_marker = os.path.join(BIN_DIR, "yt_dlp_ejs")
    if os.path.exists(ejs_marker):
        # Verify it's actually importable (Windows permission check)
        try:
            sys.path.insert(0, BIN_DIR)
            import yt_dlp_ejs
            return True
        except ImportError:
            logging.warning("yt_dlp_ejs directory exists but not importable, reinstalling")
            import shutil
            try:
                shutil.rmtree(ejs_marker)
            except Exception:
                pass
    # Try pip first
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "yt-dlp-ejs==0.8.0",
             "--target", BIN_DIR, "--quiet", "--break-system-packages"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            logging.info("yt-dlp-ejs installed to %s", BIN_DIR)
            return True
        logging.warning("yt-dlp-ejs install failed: %s", result.stderr)
    except Exception as e:
        logging.warning("pip install failed: %s", e)
    # Fallback: download wheel directly from PyPI
    try:
        import urllib.request, zipfile, io
        wheel_url = "https://files.pythonhosted.org/packages/py3/y/yt_dlp_ejs/yt_dlp_ejs-0.8.0-py3-none-any.whl"
        logging.info("Attempting direct wheel download for yt-dlp-ejs...")
        with urllib.request.urlopen(wheel_url, timeout=60) as resp:
            wheel_data = resp.read()
        with zipfile.ZipFile(io.BytesIO(wheel_data)) as zf:
            zf.extractall(BIN_DIR)
        if os.path.exists(ejs_marker):
            logging.info("yt-dlp-ejs installed via direct wheel download")
            return True
        logging.warning("yt-dlp-ejs wheel extraction did not produce expected directory")
        return False
    except Exception as e:
        logging.warning("Failed to install yt-dlp-ejs via wheel: %s", e)
        return False

_node_worker_proc = None

def _start_node_worker():
    """Start the persistent Node worker process."""
    global _node_worker_proc
    node = _find_node()
    if not node:
        logging.warning("Node not found — persistent worker disabled")
        return
    worker_js = os.path.join(os.path.dirname(__file__), "yt-node-worker.js")
    if not os.path.exists(worker_js):
        logging.warning("yt-node-worker.js not found — persistent worker disabled")
        return
    sock_path = os.path.join(tempfile.gettempdir(), 'ytmproxy-node.sock')
    # Kill any existing node worker processes to avoid accumulation
    try:
        import signal as _signal
        if os.name == 'nt':
            # Windows: use taskkill
            subprocess.run(
                ['taskkill', '/F', '/IM', 'node.exe', '/FI', 'WINDOWTITLE eq yt-node-worker*'],
                capture_output=True
            )
        else:
            result = subprocess.run(
                ['pgrep', '-f', 'node.*yt-node-worker.js'],
                capture_output=True, text=True
            )
            for pid_str in result.stdout.strip().split():
                try:
                    os.kill(int(pid_str), _signal.SIGTERM)
                    logging.info("Killed old Node worker PID=%s", pid_str)
                except Exception:
                    pass
    except Exception:
        pass
    # Remove stale socket
    try:
        if os.path.exists(sock_path):
            os.remove(sock_path)
    except Exception:
        try:
            subprocess.run(['rm', '-f', sock_path])
        except Exception:
            pass
    try:
        _node_worker_proc = subprocess.Popen(
            [node, worker_js, sock_path, BIN_DIR],
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(tempfile.gettempdir(), 'yt-node-worker.log'), 'w'),
        )
        logging.info("Started Node worker PID=%d", _node_worker_proc.pid)
    except Exception as e:
        logging.warning("Failed to start Node worker: %s", e)

# Persistent YoutubeDL instance — initialized once, reused for every extraction
import threading as _threading
_ydl_instance = None
_ydl_lock = _threading.Lock()
_ydl_available = None  # None=unknown, True=available, False=not available

def _get_ydl():
    """Get or create the persistent YoutubeDL instance."""
    global _ydl_instance, _ydl_available
    if _ydl_available is False:
        return None
    with _ydl_lock:
        if _ydl_instance is None:
            try:
                if BIN_DIR not in sys.path:
                    sys.path.insert(0, BIN_DIR)
                # Check if yt_dlp Python package is actually importable
                # (standalone yt-dlp binary doesn't provide the Python package)
                try:
                    import yt_dlp as _yt_dlp_test
                except ImportError:
                    _ydl_available = False
                    logging.info("yt_dlp Python package not available (standalone binary install), persistent YDL disabled")
                    return None
                import yt_dlp
                # Apply node provider patch so YDL uses persistent worker socket
                _patch_node_provider()
                # Force reload of node module so patch takes effect
                import importlib as _importlib
                try:
                    import yt_dlp.extractor.youtube.jsc._builtin.node as _node_mod
                    _importlib.reload(_node_mod)
                    logging.info("Reloaded node module after patch")
                except Exception as _e:
                    logging.warning("Could not reload node module: %s", _e)
                node_path = _find_node()
                js_runtimes = {'node': {'path': node_path}} if node_path else {'node': {}}
                ydl_opts = {
                    'format': 'bestaudio',
                    'quiet': True,
                    'no_warnings': True,
                    'no_check_certificates': True,
                    'socket_timeout': 10,
                    'retries': 2,
                    'extractor_retries': 2,
                    'extractor_args': {'youtube': {'player_client': ['web_embedded']}},
                    'cachedir': os.path.join(BIN_DIR, 'ytdlp_cache'),
                    'js_runtimes': js_runtimes,
                }
                _ydl_instance = yt_dlp.YoutubeDL(ydl_opts)
                _ydl_available = True
                logging.info("Persistent YoutubeDL instance created")
            except Exception as e:
                logging.warning("Failed to create persistent YoutubeDL: %s", e)
                _ydl_instance = None
    return _ydl_instance

def _get_audio_url(video_id):
    """Use persistent YoutubeDL to extract audio URL. Returns URL or None."""
    try:
        ydl = _get_ydl()
        if ydl is None:
            return None
        url = f"https://music.youtube.com/watch?v={video_id}"
        info = ydl.extract_info(url, download=False)
        if not info:
            return None
        # Get the best audio URL
        if 'url' in info:
            return info['url']
        # Try formats
        formats = info.get('formats', [])
        if formats:
            return formats[-1].get('url')
        return None
    except Exception as e:
        logging.warning("YDL extract failed for %s: %s", video_id, e)
        return None

def _compile_ytdlp_bytecode():
    """Pre-compile yt-dlp Python source to .pyc bytecode for faster startup on slow ARM CPUs."""
    ytdlp_dir = os.path.join(BIN_DIR, "yt_dlp")
    marker = os.path.join(ytdlp_dir, ".compiled")
    if not os.path.exists(ytdlp_dir) or os.path.exists(marker):
        return
    try:
        import compileall
        if compileall.compile_dir(ytdlp_dir, quiet=True, force=True):
            with open(marker, "w") as f:
                f.write("compiled\n")
            logging.info("Pre-compiled yt-dlp Python bytecode for faster startup")
    except Exception as e:
        logging.warning("Could not pre-compile yt-dlp bytecode: %s", e)

def _patch_node_provider():
    """Patch yt-dlp node.py to use persistent Node worker for faster JS challenge solving."""
    try:
        node_path = os.path.join(BIN_DIR, "yt_dlp", "extractor", "youtube", "jsc", "_builtin", "node.py")
        if not os.path.exists(node_path):
            return
        with open(node_path, "r") as f:
            content = f.read()
        if "_try_persistent_worker" in content:
            return  # Already patched
        new_method = (
            "\n    def _try_persistent_worker(self, stdin: str):\n"
            "        import socket as _socket, json as _json, hashlib as _hashlib, os as _os\n"
            "        SOCK_PATH = __import__('os').path.join(__import__('tempfile').gettempdir(), 'ytmproxy-node.sock')\n"
            "        if not _os.path.exists(SOCK_PATH):\n"
            "            return None\n"
            "        try:\n"
            "            jsc_pos = stdin.rfind('console.log(JSON.stringify(jsc(')\n"
            "            if jsc_pos < 0:\n"
            "                return None\n"
            "            arg_start = jsc_pos + len('console.log(JSON.stringify(jsc(')\n"
            "            arg_end = stdin.rfind(')));')\n"
            "            jsc_arg = _json.loads(stdin[arg_start:arg_end])\n"
            "            if jsc_arg.get('type') != 'preprocessed':\n"
            "                return None\n"
            "            player_data = jsc_arg['preprocessed_player']\n"
            "            player_version = _hashlib.md5(player_data[:2000].encode()).hexdigest()[:8]\n"
            "            probe = _json.dumps({'player_version': player_version, 'probe': True}) + '\\n'\n"
            "            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)\n"
            "            sock.settimeout(5.0)\n"
            "            sock.connect(SOCK_PATH)\n"
            "            sock.sendall(probe.encode())\n"
            "            resp = b''\n"
            "            while True:\n"
            "                chunk = sock.recv(4096)\n"
            "                if not chunk: break\n"
            "                resp += chunk\n"
            "                if b'\\n' in resp: break\n"
            "            probe_result = _json.loads(resp.decode().strip())\n"
            "            if probe_result.get('has_player'):\n"
            "                req = _json.dumps({'player_version': player_version, 'requests': jsc_arg['requests']}) + '\\n'\n"
            "            else:\n"
            "                req = _json.dumps({'player_version': player_version, 'player_data': player_data, 'requests': jsc_arg['requests']}) + '\\n'\n"
            "            sock.sendall(req.encode())\n"
            "            response = b''\n"
            "            while True:\n"
            "                chunk = sock.recv(65536)\n"
            "                if not chunk: break\n"
            "                response += chunk\n"
            "                if b'\\n' in response: break\n"
            "            sock.close()\n"
            "            result = _json.loads(response.decode().strip())\n"
            "            if result.get('ok'):\n"
            "                return _json.dumps(result['result'])\n"
            "            return None\n"
            "        except Exception as e:\n"
            "            self.logger.debug(f'Persistent worker error: {e}')\n"
            "            return None\n"
        )
        worker_call = (
            "        # Try persistent Node worker first\n"
            "        _worker_result = self._try_persistent_worker(stdin)\n"
            "        if _worker_result is not None:\n"
            "            return _worker_result\n"
        )
        content = content.replace(
            "    def _run_js_runtime(self, stdin: str, /) -> str:",
            new_method + "    def _run_js_runtime(self, stdin: str, /) -> str:"
        )
        content = content.replace(
            "    def _run_js_runtime(self, stdin: str, /) -> str:\n        args = []",
            "    def _run_js_runtime(self, stdin: str, /) -> str:\n" + worker_call + "        args = []"
        )
        with open(node_path, "w") as f:
            f.write(content)
        import py_compile
        py_compile.compile(node_path, doraise=True)
        logging.info("Patched node.py with persistent worker support")
    except Exception as e:
        logging.warning("Could not patch node provider: %s", e)

def _enable_preprocessed_player_cache():
    """Enable preprocessed player caching in yt-dlp-ejs for faster subsequent extractions."""
    try:
        ejs_path = os.path.join(BIN_DIR, "yt_dlp", "extractor", "youtube", "jsc", "_builtin", "ejs.py")
        if not os.path.exists(ejs_path):
            return
        with open(ejs_path, "r") as f:
            content = f.read()
        if "_ENABLE_PREPROCESSED_PLAYER_CACHE = False" in content:
            content = content.replace(
                "_ENABLE_PREPROCESSED_PLAYER_CACHE = False",
                "_ENABLE_PREPROCESSED_PLAYER_CACHE = True"
            )
            with open(ejs_path, "w") as f:
                f.write(content)
            logging.info("Enabled preprocessed player cache in yt-dlp-ejs")
    except Exception as e:
        logging.warning("Could not enable preprocessed player cache: %s", e)

def _find_ytdlp():
    _ytdlp_override = os.environ.get("YTM_YTDLP_OVERRIDE")
    if _ytdlp_override and os.path.isfile(_ytdlp_override):
        return _ytdlp_override
    # Check plugin directory first (no sudo needed, always found)
    if os.path.isfile(YTDLP_BIN) and os.access(YTDLP_BIN, os.X_OK):
        return YTDLP_BIN
    # Fall back to system PATH
    for name in ("yt-dlp", "yt_dlp", "youtube-dl"):
        p = shutil.which(name)
        if p:
            return p
    return None
def _ensure_bin_in_path():
    """On Windows, add the plugin Bin directory to PATH so ffmpeg can be found."""
    if os.name == "nt" and BIN_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ.get("PATH", "")

def stream_audio(video_id, _replacing=None):
    _ensure_bin_in_path()
    """
    Yield MP3 audio bytes for the given video ID by piping yt-dlp's stdout
    directly into ffmpeg, avoiding any temp files. ffmpeg re-muxes into a
    simple sequential MP3 stream (moov-atom positioning in raw MP4/WebM from
    the YouTube CDN makes direct streaming unreliable on hardware decoders).
    """
    ytdlp = _find_ytdlp()
    if not ytdlp:
        raise RuntimeError("yt-dlp not found in PATH")

    url = f"https://music.youtube.com/watch?v={video_id}"

    # ytdlp_cmd is built after _get_audio_url() so we can use the CDN URL directly
    # This avoids making yt-dlp extract the YouTube Music watch URL a second time

    ffmpeg_cmd = [
        _find_ffmpeg(),
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vn",
        "-map_metadata", "-1",
        "-id3v2_version", "0",
        "-write_id3v1", "0",
        "-f", _AUDIO_FORMAT,
        "-codec:a", _AUDIO_CODEC,
    ]
    if _AUDIO_CODEC not in ("flac", "pcm_s16le"):
        ffmpeg_cmd += ["-b:a", "192k"]
    if _AUDIO_CODEC == "flac":
        ffmpeg_cmd += ["-sample_fmt", "s16"]
    ffmpeg_cmd.append("pipe:1")

    # Try persistent YDL first (faster — no subprocess startup)
    import time as _time
    _t0 = _time.monotonic()
    audio_url = _get_audio_url(video_id)
    _t1 = _time.monotonic()
    _log_fn = logging.warning if audio_url else (logging.info if _ydl_available is False else logging.warning)
    _log_fn("PREFETCH_YDL videoId=%s extraction=%.2fs url=%s", video_id, _t1-_t0, "OK" if audio_url else "FAIL")
    _NODE_PATH = _find_node()
    ytdlp_cmd = [
        ytdlp,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--no-check-certificates",
        "--socket-timeout", "10",
        "--retries", "2",
        "--extractor-retries", "2",
        "--no-part",
        "--http-chunk-size", "1M",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio",
        *(["--js-runtimes", f"node:{_NODE_PATH}"] if _NODE_PATH else ["--js-runtimes", "node"]),
        "--cache-dir", os.path.join(BIN_DIR, "ytdlp_cache"),
        "--add-header", "User-Agent:com.google.android.youtube/17.29.34",
        "-o", "-",
        url,
    ]
    # Only use direct ffmpeg URL approach on non-ARM platforms.
    # On ARM (Pi), YouTube's CDN silently times out ffmpeg connections,
    # causing ffmpeg to hang indefinitely with no timeout and produce 0 bytes.
    # HLS manifests also hang ffmpeg regardless of platform.
    # yt-dlp subprocess handles both cases correctly.
    import platform as _platform
    _is_arm = _platform.machine().startswith(("arm", "aarch"))
    _is_hls = audio_url and (".m3u8" in audio_url or "manifest.googlevideo.com" in audio_url)
    if audio_url and not _is_hls and not _is_arm:
        ffmpeg_url_cmd = [
            _find_ffmpeg(), "-loglevel", "error",
            "-user_agent", "Mozilla/5.0 (Linux; Android 6.0; Nexus 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
            "-headers", "Accept: */*\r\nAccept-Language: en-us,en;q=0.5\r\n",
            "-i", audio_url,
            "-vn", "-map_metadata", "-1",
            "-id3v2_version", "0", "-write_id3v1", "0",
            "-f", _AUDIO_FORMAT, "-codec:a", _AUDIO_CODEC,
        ]
        if _AUDIO_CODEC not in ("flac", "pcm_s16le"):
            ffmpeg_url_cmd += ["-b:a", "192k"]
        if _AUDIO_CODEC == "flac":
            ffmpeg_url_cmd += ["-sample_fmt", "s16"]
        ffmpeg_url_cmd.append("pipe:1")
        ffmpeg_proc = subprocess.Popen(ffmpeg_url_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bytes_sent = 0
        try:
            while True:
                chunk = ffmpeg_proc.stdout.read(65536)
                if not chunk:
                    break
                bytes_sent += len(chunk)
                yield chunk
        finally:
            ffmpeg_proc.stdout.close()
            stderr_out = ffmpeg_proc.stderr.read().decode("utf-8", errors="replace").strip()
            ffmpeg_proc.wait()
            if stderr_out and bytes_sent == 0:
                logging.warning("ffmpeg URL stderr: %s", stderr_out[:500])
        if bytes_sent > 0:
            return
        # Fallback: subprocess yt-dlp
        logging.warning("ffmpeg URL produced 0 bytes for %s, falling back to subprocess", video_id)
    if _ydl_available is not False:
        logging.warning("Persistent YDL failed for %s, falling back to subprocess", video_id)
    else:
        logging.info("Persistent YDL not available for %s, using subprocess", video_id)
    logging.info("Streaming videoId=%s", video_id)

    ytdlp_proc = subprocess.Popen(
        ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=ytdlp_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    ytdlp_proc.stdout.close()

    sent = 0
    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(65536)
            if not chunk:
                break
            sent += len(chunk)
            yield chunk
    finally:
        for proc in (ffmpeg_proc, ytdlp_proc):
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # Nothing playable under this ID (private, removed, blocked...). Lists
    # such as Liked Songs keep the ID that was saved years ago; the apps
    # quietly play the current copy. Do the same when we know the track.
    if not sent and _replacing is None:
        rep = _replacement_for_unplayable(video_id)
        if rep:
            logging.warning("%s is unplayable; streaming replacement %s instead", video_id, rep)
            yield from stream_audio(rep, _replacing=video_id)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logging.debug(fmt, *args)

    def _send_json(self, obj, code=200):
        _remember_tracks(obj)
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=500):
        self._send_json({"error": msg}, code)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        def p(key, default=""):
            return qs.get(key, [default])[0]

        path = parsed.path.rstrip("/")
        try:
            if path == "/ping":
                self._send_json({"status": "ok"})
            elif path == "/globalsearch":
                q = p("q")
                t = p("type", "songs")
                if not q:
                    return self._error("Missing q parameter", 400)
                results = search(q, t)
                # Wrap in OPML format for LMS XMLBrowser
                items = []
                for r in results:
                    if r.get("type") == "song" and r.get("videoId"):
                        items.append({
                            "title": r.get("title", ""),
                            "line1": r.get("title", ""),
                            "line2": r.get("artist", ""),
                            "image": r.get("thumbnail", ""),
                            "url": "ytm://" + r["videoId"],
                            "play": "ytm://" + r["videoId"],
                            "type": "audio",
                            "on_select": "play",
                        })
                    elif r.get("type") == "album" and r.get("browseId"):
                        items.append({
                            "title": r.get("title", ""),
                            "line1": r.get("title", ""),
                            "line2": r.get("artist", ""),
                            "image": r.get("thumbnail", ""),
                            "url": "ytmplaylist://" + r["browseId"],
                            "type": "playlist",
                        })
                    elif r.get("type") == "artist" and r.get("browseId"):
                        items.append({
                            "title": r.get("name", r.get("title", "")),
                            "image": r.get("thumbnail", ""),
                            "url": "ytmplaylist://" + r["browseId"],
                        })
                    elif r.get("type") == "playlist" and r.get("browseId"):
                        items.append({
                            "title": r.get("title", ""),
                            "image": r.get("thumbnail", ""),
                            "url": "ytmplaylist://" + r["browseId"],
                        })
                self._send_json({"title": "YouTube Music", "items": items})
            elif path == "/ytdlp_status":
                ytdlp_path = _find_ytdlp()
                if ytdlp_path:
                    import subprocess as _sp
                    try:
                        ver = _sp.run([ytdlp_path, "--version"], capture_output=True, text=True, timeout=30,
                                      env={**os.environ, "PYTHONPATH": BIN_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")})
                        self._send_json({"installed": True, "version": ver.stdout.strip(), "path": ytdlp_path})
                    except Exception:
                        self._send_json({"installed": True, "version": "unknown", "path": ytdlp_path})
                else:
                    self._send_json({"installed": False, "version": None, "path": None})
            elif path == "/search":
                q = p("q")
                if not q:
                    return self._error("Missing q parameter", 400)
                self._send_json(search(q, p("type", "songs")))
            elif path == "/browse/home":
                self._send_json(browse_home())
            elif path == "/browse/charts":
                self._send_json(browse_charts())
            elif path == "/codec":
                self._send_json({
                    "codec":  _AUDIO_CODEC,
                    "format": _AUDIO_FORMAT,
                    "mime":   _AUDIO_MIME,
                })
            elif path == "/download_ytdlp":
                ok, msg = download_ytdlp()
                if ok:
                    self._send_json({"status": "ok", "version": msg})
                else:
                    self._send_json({"status": "error", "message": msg})
            elif path == "/download_ffmpeg":
                ok, msg = download_ffmpeg()
                if ok:
                    self._send_json({"status": "ok", "version": msg})
                else:
                    self._send_json({"status": "error", "message": msg})
            elif path == "/ffmpeg_status":
                import shutil as _shutil
                ffmpeg = _shutil.which("ffmpeg") or os.path.join(BIN_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                if os.path.isfile(ffmpeg):
                    try:
                        ver = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=5)
                        version = ver.stdout.split("\n")[0].split("version ")[1].split(" ")[0] if "version" in ver.stdout else "unknown"
                        self._send_json({"installed": True, "version": version, "path": ffmpeg})
                    except Exception:
                        self._send_json({"installed": True, "version": "unknown", "path": ffmpeg})
                else:
                    self._send_json({"installed": False, "version": None, "path": None})
            elif path == "/paths":
                import shutil as _shutil2
                node = _find_node() or ''
                python = sys.executable or ''
                ytdlp = _find_ytdlp() or ''
                ffmpeg_path = _shutil2.which("ffmpeg") or os.path.join(BIN_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                ffmpeg = ffmpeg_path if os.path.isfile(ffmpeg_path) else ''
                # Validate override paths passed as query params
                from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse2
                _qs = _parse_qs(_urlparse2(self.path).query)
                overrides = {
                    'python': _qs.get('python', [''])[0],
                    'ytdlp':  _qs.get('ytdlp',  [''])[0],
                    'ffmpeg': _qs.get('ffmpeg', [''])[0],
                    'node':   _qs.get('node',   [''])[0],
                }
                override_valid = {k: (os.path.isfile(v) if v else None) for k, v in overrides.items()}
                # Find current log file
                import logging as _logging
                log_file_path = ''
                for handler in _logging.root.handlers:
                    if hasattr(handler, 'baseFilename'):
                        log_file_path = handler.baseFilename
                        break
                self._send_json({
                    'python': python,
                    'ffmpeg': ffmpeg,
                    'ytdlp': ytdlp,
                    'node': node,
                    'log_file': log_file_path,
                    'override_valid': override_valid,
                })

            elif path == "/update_ytdlp":
                try:
                    ytdlp = _find_ytdlp()
                    if not ytdlp:
                        self._send_json({"status": "error", "message": "yt-dlp not found"})
                    else:
                        # Try yt-dlp -U first (works on piCorePlayer and
                        # systems where yt-dlp is a standalone binary)
                        # Check if yt-dlp is a wrapper script pointing to system install
                        with open(ytdlp, "rb") as _f:
                            _header = _f.read(2)
                        _is_script = _header == b"#!"
                        _ytdlp_pkg = os.path.join(BIN_DIR, "yt_dlp")
                        _has_local_pkg = os.path.isdir(_ytdlp_pkg)
                        if _is_script and not _has_local_pkg:
                            self._send_json({"status": "error", "message": "yt-dlp is installed as a system package. Use Download yt-dlp to install a local copy that can be updated, or run: sudo pip3 install yt-dlp --upgrade --break-system-packages"})
                            return
                        if _is_script and _has_local_pkg:
                            # Re-download the wheel to update
                            ok, msg = download_ytdlp()
                            if ok:
                                ver2 = subprocess.run([ytdlp, "--version"], capture_output=True, text=True, timeout=30)
                                self._send_json({"status": "ok", "version": ver2.stdout.strip() or msg})
                            else:
                                self._send_json({"status": "error", "message": msg})
                            return
                        # For onedir/frozen installs (macOS zip, Windows exe),
                        # yt-dlp -U doesn't work — re-download instead
                        _asset, _is_zip = _platform_ytdlp_asset()
                        _is_exe = ytdlp.endswith('.exe') if ytdlp else False
                        if _is_zip or _is_exe:
                            ok, msg = download_ytdlp()
                            if ok:
                                self._send_json({"status": "ok", "version": msg})
                            else:
                                self._send_json({"status": "error", "message": msg})
                            return
                        result = subprocess.run(
                            [ytdlp, "-U"],
                            capture_output=True, text=True, timeout=120
                        )
                        if result.returncode != 0:
                            # Try pipx upgrade (Ubuntu/Debian pipx installs)
                            pipx = shutil.which("pipx")
                            if pipx:
                                result = subprocess.run(
                                    [pipx, "upgrade", "yt-dlp"],
                                    capture_output=True, text=True, timeout=120
                                )
                        if result.returncode != 0:
                            # Fall back to pip upgrade
                            result = subprocess.run(
                                [sys.executable, "-m", "pip", "install", "yt-dlp",
                                 "--upgrade", "--break-system-packages", "-q"],
                                capture_output=True, text=True, timeout=120
                            )
                        if result.returncode == 0:
                            ver = subprocess.run(
                                [ytdlp, "--version"],
                                capture_output=True, text=True, timeout=10
                            )
                            self._send_json({"status": "ok", "version": ver.stdout.strip()})
                        else:
                            self._send_json({"status": "error", "message": result.stderr.strip() or result.stdout.strip()})
                except Exception as e:
                    self._send_json({"status": "error", "message": str(e)})
            elif path == "/radio":
                vid = p("videoId")
                if not vid:
                    return self._error("Missing videoId", 400)
                self._send_json(browse_radio(vid))
            elif path == "/browse/new_releases":
                self._send_json(browse_new_releases())
            elif path == "/browse/moods":
                self._send_json(browse_moods())
            elif path == "/browse/mood_category":
                bid    = p("browseId")
                params = p("params", "")
                if not bid:
                    return self._error("Missing browseId", 400)
                self._send_json(browse_mood_category(bid, params))
            elif path == "/browse/podcasts":
                self._send_json(browse_podcasts())
            elif path == "/playlist":
                bid = p("browseId")
                if not bid:
                    return self._error("Missing browseId", 400)
                # Handle OLAK5uy_ IDs (YouTube Music browser URLs) differently
                if bid.startswith("VLOLAK5uy_"):
                    self._send_json(browse_olak_playlist(bid[2:]))
                elif bid.startswith("OLAK5uy_"):
                    self._send_json(browse_olak_playlist(bid))
                else:
                    self._send_json(browse_playlist(bid))
            elif path == "/album":
                bid = p("browseId")
                if not bid:
                    return self._error("Missing browseId", 400)
                self._send_json(browse_playlist(bid))
            elif path == "/artist":
                bid = p("browseId")
                if not bid:
                    return self._error("Missing browseId", 400)
                self._send_json(browse_artist(bid))
            elif path == "/artist/discography":
                bid = p("browseId")
                if not bid:
                    return self._error("Missing browseId", 400)
                self._send_json(browse_artist_discography(bid, p("params")))
            elif path == "/song/links":
                vid = p("videoId")
                if not vid:
                    return self._error("Missing videoId", 400)
                self._send_json(get_song_links(vid, p("title"), p("artist")))
            elif path == "/song":
                vid = p("videoId")
                if not vid:
                    return self._error("Missing videoId", 400)
                self._send_json(get_song_info(vid))

            elif path == "/library/liked":
                self._send_json(_stable("liked", get_liked_songs))

            elif path == "/library/playlists":
                self._send_json(_stable("playlists", get_library_playlists))

            elif path == "/library/songs":
                self._send_json(_stable("songs", get_library_songs))

            elif path == "/library/history":
                self._send_json(_stable("history", get_history))

            elif path == "/library/home":
                self._send_json(_stable("home", get_personal_home))

            elif path == "/library/albums":
                self._send_json(_stable("albums", get_library_albums))

            elif path == "/library/artists":
                self._send_json(_stable("artists", get_library_artists))

            elif path == "/auth/status":
                yt = _get_ytm_auth()
                self._send_json({"authenticated": yt is not None})

            elif path == "/auth/init":
                cookie = p("cookie") or ""
                ok = _init_ytm_auth(cookie)
                self._send_json({"success": ok})

            elif path == "/auth/reload":
                # Reload auth from query param - GET request
                cookie = p("headers") or p("cookie") or ""
                ok = _init_ytm_auth(cookie)
                self._send_json({"success": ok, "authenticated": ok})

            elif path.startswith("/stream/"):
                vid = path[len("/stream/"):]
                if not vid:
                    return self._error("Missing videoId", 400)

                # LMS seeks with "Range: bytes=N-" (it always sends one,
                # usually bytes=0-). Both paths below honour it.
                m     = re.match(r"bytes=(\d+)-", self.headers.get("Range", ""))
                start = int(m.group(1)) if m else 0

                cached_path = get_prefetched_path(vid)
                if cached_path:
                    try:
                        size = os.path.getsize(cached_path)
                        if start >= size:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            return
                        self.send_response(206 if start else 200)
                        self.send_header("Content-Type", _AUDIO_MIME)
                        self.send_header("Content-Length", str(size - start))
                        if start:
                            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        with open(cached_path, "rb") as f:
                            f.seek(start)
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                        logging.info("Served %s from prefetch cache", vid)
                        return
                    except (BrokenPipeError, ConnectionResetError):
                        logging.info("Client disconnected during cached stream for %s", vid)
                        return
                    except Exception:
                        logging.exception("Cached stream error for %s, falling back to live", vid)

                # Progressive raw streaming: no chunked encoding, raw MP3 bytes
                tmp_path, done_path = _prefetch_paths(vid)
                cached = get_prefetched_path(vid)
                if not cached:
                    start_prefetch(vid)
                # Send HTTP headers immediately — don't make LMS wait for yt-dlp to start
                # For a seek into a track that is still downloading, start at
                # the requested offset; the loop below waits for the download
                # (which runs far faster than real time) to get there. The
                # total length isn't known yet, so Content-Range carries only
                # the start - which is the part LMS checks.
                try:
                    self.send_response(206 if start else 200)
                    self.send_header("Content-Type", _AUDIO_MIME)
                    if start:
                        self.send_header("Content-Range", f"bytes {start}-")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("icy-metaint", "0")
                    self.end_headers()
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                # Wait for first bytes — timeout only applies to this initial wait
                # not to the stream itself (a long song should stream indefinitely)
                first_byte_deadline = time.time() + 60
                urgent_triggered = False
                while time.time() < first_byte_deadline:
                    if os.path.exists(done_path) and os.path.getsize(done_path) > 0:
                        break
                    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                        break
                    # If no bytes after 5s the prefetch is likely semaphore-blocked;
                    # start an urgent download that bypasses the semaphore so LMS
                    # doesn't have to wait for an unrelated background prefetch to finish.
                    if not urgent_triggered and time.time() > first_byte_deadline - 55:
                        logging.warning("URGENT_STREAM %s triggering after 5s wait", vid)
                        urgent_triggered = True
                        threading.Thread(
                            target=_urgent_stream_worker, args=(vid,), daemon=True
                        ).start()
                    time.sleep(0.1)
                sz = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                logging.warning("Starting stream %s with %d bytes buffered", vid, sz)
                open_path = done_path if os.path.exists(done_path) else tmp_path
                if not os.path.exists(open_path) or os.path.getsize(open_path) == 0:
                    logging.warning("Stream %s produced 0 bytes — sending error to trigger skip", vid)
                    try:
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    return
                if os.path.exists(open_path) and os.path.getsize(open_path) > 0:
                    try:
                        position = start
                        while True:
                            current = tmp_path if os.path.exists(tmp_path) else done_path
                            if not os.path.exists(current):
                                if os.path.exists(done_path):
                                    break
                                time.sleep(0.1)
                                continue
                            with open(current, "rb") as fp:
                                fp.seek(position)
                                chunk = fp.read(65536)
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                                position += len(chunk)
                                continue
                            if os.path.exists(done_path):
                                break
                            time.sleep(0.1)
                        logging.info("Progressive raw stream complete for %s", vid)
                        return
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception:
                        logging.exception("Progressive stream error for %s", vid)
                # Fallback: live stream if progressive failed
                # Fallback: live stream if cache failed
                # Fallback: live stream if cache failed
                # Fallback: live stream if cache failed
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", _AUDIO_MIME)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("icy-metaint", "0")
                    self.end_headers()
                    for chunk in stream_audio(vid):
                        size_hdr = ("%x\r\n" % len(chunk)).encode()
                        self.wfile.write(size_hdr)
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    logging.info("Served %s via live stream fallback", vid)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    logging.exception("Stream error for %s", vid)
            elif path.startswith("/prefetch/"):
                vid = path[len("/prefetch/"):]
                if not vid:
                    return self._error("Missing videoId", 400)
                status = start_prefetch(vid)
                self._send_json({"videoId": vid, "status": status})

            else:
                self._error(f"Unknown endpoint: {path}", 404)
        except urllib.error.HTTPError as exc:
            logging.error("Upstream HTTP %s: %s", exc.code, exc.reason)
            self._error(f"Upstream HTTP {exc.code}: {exc.reason}", 502)
        except Exception:
            logging.exception("Proxy error on %s", self.path)
            self._error("Internal proxy error", 500)

def run(port=9876, log_level="INFO", codec="auto", log_file="", no_node_worker=False, bind="127.0.0.1"):
    global _AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME
    if codec == "mp3":
        _AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = "libmp3lame", "mp3", "audio/mpeg"
    elif codec == "flac":
        _AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = "flac", "flac", "audio/flac"
    elif codec == "aac":
        _AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = "aac", "adts", "audio/aac"
    else:
        # Re-run auto-detection now that ffmpeg override may be set
        _AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = _detect_audio_codec()
    if codec != "auto":
        logging.info("Codec overridden to: %s", codec)
    else:
        logging.info("Codec auto-detected: %s", _AUDIO_CODEC)
    log_file = log_file or os.path.join(BIN_DIR, 'ytmproxy.log')
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handlers.append(fh)
    except Exception:
        pass
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.info("ytmproxy log file: %s", log_file)
    # Auto-download yt-dlp on first startup if not already installed
    if not _find_ytdlp():
        logging.info("yt-dlp not found — attempting auto-download")
        try:
            ok, msg = download_ytdlp()
            if ok:
                logging.info("yt-dlp auto-downloaded successfully: %s", msg)
            else:
                logging.warning("yt-dlp auto-download failed: %s", msg)
        except Exception as e:
            logging.warning("yt-dlp auto-download error: %s", e)
    # Auto-install yt-dlp-ejs for JS challenge support
    _install_ytdlp_ejs()
    # Create yt-dlp cache dir for preprocessed player cache
    os.makedirs(os.path.join(BIN_DIR, "ytdlp_cache"), exist_ok=True)
    # Enable preprocessed player cache in yt-dlp-ejs for faster subsequent extractions
    _enable_preprocessed_player_cache()
    # Patch node.py to use persistent Node worker
    _patch_node_provider()
    # Pre-compile yt-dlp Python source to bytecode for faster startup on slow ARM CPUs
    _compile_ytdlp_bytecode()
    # Auto-download Node 22 if no suitable node found
    if not _find_node():
        logging.info("Node 22+ not found — attempting auto-download")
        try:
            ok, msg = download_node()
            if ok:
                logging.info("Node.js auto-downloaded successfully: %s", msg)
            else:
                logging.warning("Node.js auto-download failed: %s", msg)
        except Exception as e:
            logging.warning("Node.js auto-download error: %s", e)
    # Start persistent Node worker for fast JS challenge solving
    if not no_node_worker:
        _start_node_worker()
    else:
        logging.info("Node worker disabled (--no-node-worker)")

    # Pre-warm persistent YDL instance in background so first song starts faster
    def _warmup_ydl():
        import time as _t
        _t.sleep(3)  # Wait for proxy to fully start
        try:
            logging.info("Pre-warming persistent YoutubeDL instance...")
            t0 = _t.monotonic()
            ydl = _get_ydl()
            if ydl:
                try:
                    ydl.extract_info('https://music.youtube.com/watch?v=dQw4w9WgXcQ', download=False)
                except Exception:
                    pass
                logging.info("YDL warmup complete in %.1fs", _t.monotonic() - t0)
        except Exception as e:
            logging.warning("YDL warmup failed: %s", e)
    import threading as _wt
    _wt.Thread(target=_warmup_ydl, daemon=True).start()

    # Loopback only: LMS reaches the proxy itself (players never connect to
    # it directly - canDirectStream is 0), and the settings page goes through
    # LMS's /plugins/YouTubeMusic/proxy/ routes. Listening on all interfaces
    # exposed library, history, cookie replacement and the yt-dlp
    # download/update endpoints to the whole network without authentication.
    server = ThreadingHTTPServer((bind, port), _Handler)
    logging.info("YTMusic proxy listening on %s:%d", bind, port)
    server.serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",      type=int, default=9876)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--codec",     default="auto", choices=["auto", "mp3", "flac", "aac"])
    ap.add_argument("--ytdlp",     default="", help="Override path to yt-dlp binary")
    ap.add_argument("--ffmpeg",    default="", help="Override path to ffmpeg binary")
    ap.add_argument("--node",      default="", help="Override path to node binary")
    ap.add_argument("--log-file",      default="", help="Path to log file (default: BIN_DIR/ytmproxy.log)")
    ap.add_argument("--no-node-worker", action="store_true", help="Disable persistent Node worker (saves ~140MB RAM)")
    ap.add_argument("--ytm-cookie", default="", help="YouTube Music browser cookie for authenticated access")
    ap.add_argument("--bind",       default="0.0.0.0", help="Address to listen on (default: all interfaces)")
    args = ap.parse_args()
    # Apply path overrides before run()
    if args.ytdlp and os.path.isfile(args.ytdlp):
        os.environ['YTM_YTDLP_OVERRIDE'] = args.ytdlp
        logging.info("yt-dlp path override: %s", args.ytdlp)
    if args.ffmpeg and os.path.isfile(args.ffmpeg):
        os.environ['YTM_FFMPEG_OVERRIDE'] = args.ffmpeg
        logging.info("ffmpeg path override: %s", args.ffmpeg)
    if args.node and os.path.isfile(args.node):
        os.environ['YTM_NODE_OVERRIDE'] = args.node
        logging.info("node path override: %s", args.node)
    if args.ytm_cookie:
        _init_ytm_auth(args.ytm_cookie)
    run(args.port, args.log_level, args.codec, args.log_file, args.no_node_worker, args.bind)
