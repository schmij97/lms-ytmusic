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
            title = _text(card.get("title", {})) or _text(card.get("subtitle", {}))
            if browse_ep.get("browseId"):
                pt = (
                    browse_ep.get("browseEndpointContextSupportedConfigs", {})
                             .get("browseEndpointContextMusicConfig", {})
                             .get("pageType", "")
                )
                results.append({
                    "type":      "album" if "ALBUM" in pt else "artist" if "ARTIST" in pt else "playlist",
                    "title":     title,
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

# ---- Audio codec detection ----
# Probe ffmpeg at startup to find the best available audio encoder.
# piCorePlayer's ffmpeg lacks libmp3lame so we fall back to aac.
def _detect_audio_codec():
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders", "-v", "quiet"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        if "libmp3lame" in output:
            logging.info("ffmpeg codec: libmp3lame (MP3)")
            return "libmp3lame", "mp3", "audio/mpeg"
        elif "flac" in output:
            # FLAC is universally supported by Squeezebox hardware and
            # is a better fallback than AAC (which hardware decoders
            # often can't handle). piCorePlayer's ffmpeg has flac.
            logging.info("ffmpeg codec: flac (FLAC fallback - hardware compatible)")
            return "flac", "flac", "audio/flac"
        elif "aac" in output:
            logging.info("ffmpeg codec: aac (AAC fallback)")
            return "aac", "adts", "audio/aac"
        else:
            logging.warning("No suitable ffmpeg codec found, defaulting to mp3")
            return "libmp3lame", "mp3", "audio/mpeg"
    except Exception as e:
        logging.warning("ffmpeg codec detection failed: %s", e)
        return "libmp3lame", "mp3", "audio/mpeg"

_AUDIO_CODEC, _AUDIO_FORMAT, _AUDIO_MIME = _detect_audio_codec()

PREFETCH_DIR = "/tmp/ytmproxy_prefetch"
_prefetch_started = set()
_prefetch_lock = threading.Lock()

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
# Plugin directory — yt-dlp binary stored in Bin/ subdir per LMS convention
# LMS automatically adds <plugin>/Bin to PATH so it will be found system-wide
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR    = os.path.join(PLUGIN_DIR, "Bin")
YTDLP_BIN  = os.path.join(BIN_DIR, "yt-dlp")

def _platform_ytdlp_asset():
    """Return the yt-dlp GitHub asset name for the current platform."""
    import platform
    machine = platform.machine().lower()
    system  = platform.system().lower()
    if system == "windows":
        return "yt-dlp.exe", False
    if system == "darwin":
        return "yt-dlp_macos", False
    # Linux
    if machine in ("aarch64", "arm64"):
        return "yt-dlp_linux_aarch64", False
    if machine in ("armv7l", "armv6l"):
        # Note: yt-dlp dropped armv7l binary builds after Sept 2025
        # Fall back to pip install for this platform
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

        # If no binary available for this platform, use pip
        if asset_name is None:
            logging.info("No binary available for this platform, using pip")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "yt-dlp",
                 "--upgrade", "--break-system-packages",
                 "--target", PLUGIN_DIR, "-q"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                # Create wrapper script in Bin/ directory
                os.makedirs(BIN_DIR, exist_ok=True)
                wrapper = os.path.join(BIN_DIR, "yt-dlp")
                with open(wrapper, "w") as f:
                    f.write(f"#!/bin/sh\nexec {sys.executable} -m yt_dlp \"$@\"\n")
                os.chmod(wrapper, 0o755)
                return True, version
            return False, result.stderr.strip() or "pip install failed"

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
                (n for n in names if n.endswith("yt-dlp") and "/" not in n.rstrip("/")),
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


def _find_ytdlp():
    # Check plugin directory first (no sudo needed, always found)
    if os.path.isfile(YTDLP_BIN) and os.access(YTDLP_BIN, os.X_OK):
        return YTDLP_BIN
    # Fall back to system PATH
    for name in ("yt-dlp", "yt_dlp", "youtube-dl"):
        p = shutil.which(name)
        if p:
            return p
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
        "--no-check-certificates",
        "--socket-timeout", "10",
        "--retries", "2",
        "--extractor-retries", "2",
        "--no-part",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio",
        "--js-runtimes", "nodejs",
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
        "-f", _AUDIO_FORMAT,
        "-codec:a", _AUDIO_CODEC,
    ]
    if _AUDIO_CODEC not in ("flac", "pcm_s16le"):
        ffmpeg_cmd += ["-b:a", "192k"]
    ffmpeg_cmd.append("pipe:1")

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
            elif path == "/update_ytdlp":
                try:
                    ytdlp = _find_ytdlp()
                    if not ytdlp:
                        self._send_json({"status": "error", "message": "yt-dlp not found"})
                    else:
                        # Try yt-dlp -U first (works on piCorePlayer and
                        # systems where yt-dlp is a standalone binary)
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
                        self.send_header("Content-Type", _AUDIO_MIME)
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
                self.send_header("Content-Type", _AUDIO_MIME)
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
        force=True,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    logging.info("YTMusic proxy listening on 0.0.0.0:%d", port)
    server.serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",      type=int, default=9876)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    run(args.port, args.log_level)
