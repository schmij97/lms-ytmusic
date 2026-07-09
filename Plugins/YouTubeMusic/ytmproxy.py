#!/usr/bin/env python3
import argparse
import json
import logging
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
import os
import threading

_CACHE = {}
_CACHE_TTL = 120  # seconds

def _cache_get(key):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None

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

def _parse_song(r):
    video_id = _video_id_from_overlay(r)
    return {
        "type":      "song",
        "title":     _col(r, 0),
        "artist":    _col(r, 1),
        "album":     _col(r, 2),
        "duration":  _col(r, 3) or _col(r, 4),
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
    "playlists": "EgeKAQQoAEABahAQDhAKEAMQBBAJEAUQCw%3D%3D",
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
        shelf = section.get("musicShelfRenderer", {})
        if shelf:
            results.extend(_shelf_items(shelf))
    _cache_set(cache_key, results)
    return results

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
        if carousel:
            header = carousel.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
            title  = _text(header.get("title", {}))
        else:
            title = _text(shelf.get("title", {}))
        items = _shelf_items(target)
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
                    items.extend(_shelf_items(target))
    else:
        sections = _single_col_sections(data)
        for section in sections:
            for key in ("musicShelfRenderer", "musicCarouselShelfRenderer", "musicPlaylistShelfRenderer"):
                target = section.get(key, {})
                if target:
                    items.extend(_shelf_items(target))

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
        )
        items = _shelf_items(target)
        if items:
            result["sections"].append({"title": sec_title, "items": items})
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


# ---- Audio streaming via yt-dlp | ffmpeg ----

# ---- Prefetch cache (disk-backed) ----
# To mask yt-dlp's ~8-10 second resolution latency between tracks, the Perl
# protocol handler asks us to start resolving the *next* track in the
# background while the current one is still playing. We write the fully
# resolved MP3 to a temp file; if /stream/<id> is requested before this
# finishes, it falls back to live (uncached) resolution as before.

PREFETCH_DIR = "/tmp/ytmproxy_prefetch"
_prefetch_started = set()
_prefetch_lock = threading.Lock()

def _prefetch_paths(video_id):
    os.makedirs(PREFETCH_DIR, exist_ok=True)
    tmp_path  = os.path.join(PREFETCH_DIR, f"{video_id}.mp3.part")
    done_path = os.path.join(PREFETCH_DIR, f"{video_id}.mp3")
    return tmp_path, done_path


def _cleanup_old_prefetch(max_age=600):
    try:
        now = time.time()
        for name in os.listdir(PREFETCH_DIR):
            full = os.path.join(PREFETCH_DIR, name)
            if os.path.isfile(full) and (now - os.path.getmtime(full)) > max_age:
                os.remove(full)
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("Prefetch cleanup error")


def _prefetch_worker(video_id):
    tmp_path, done_path = _prefetch_paths(video_id)
    try:
        with open(tmp_path, "wb") as f:
            for chunk in stream_audio(video_id):
                f.write(chunk)
        os.replace(tmp_path, done_path)
        logging.info("Prefetch complete for %s", video_id)
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
def _find_ytdlp():
    for name in ("yt-dlp", "yt_dlp", "youtube-dl"):
        path = shutil.which(name)
        if path:
            return path
    return None


def stream_audio(video_id):
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

    ytdlp_cmd = [
        ytdlp,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f", "bestaudio",
        "--add-header", "User-Agent:com.google.android.youtube/17.29.34",
        "-o", "-",
        url,
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vn",
        "-map_metadata", "-1",
        "-id3v2_version", "0",
        "-write_id3v1", "0",
        "-f", "mp3",
        "-codec:a", "libmp3lame",
        "-b:a", "192k",
        "pipe:1",
    ]

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

    try:
        while True:
            chunk = ffmpeg_proc.stdout.read(65536)
            if not chunk:
                break
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


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logging.debug(fmt, *args)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
            elif path == "/search":
                q = p("q")
                if not q:
                    return self._error("Missing q parameter", 400)
                self._send_json(search(q, p("type", "songs")))
            elif path == "/browse/home":
                self._send_json(browse_home())
            elif path == "/browse/charts":
                self._send_json(browse_charts())
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
            elif path == "/song":
                vid = p("videoId")
                if not vid:
                    return self._error("Missing videoId", 400)
                self._send_json(get_song_info(vid))

            elif path.startswith("/stream/"):
                vid = path[len("/stream/"):]
                if not vid:
                    return self._error("Missing videoId", 400)

                cached_path = get_prefetched_path(vid)
                if cached_path:
                    try:
                        size = os.path.getsize(cached_path)
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/mpeg")
                        self.send_header("Content-Length", str(size))
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        with open(cached_path, "rb") as f:
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

                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for chunk in stream_audio(vid):
                        size_hdr = ("%x\r\n" % len(chunk)).encode()
                        self.wfile.write(size_hdr)
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    logging.info("Client disconnected during stream for %s", vid)
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

def run(port=9876, log_level="INFO"):
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    logging.info("YTMusic proxy listening on 127.0.0.1:%d", port)
    server.serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",      type=int, default=9876)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    run(args.port, args.log_level)
