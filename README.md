# YouTube Music for Lyrion Music Server (LMS)

Browse, search, and stream YouTube Music to your Squeezebox players through
Lyrion Music Server (formerly Logitech Media Server).

## Disclaimer

This is an independent, unofficial project. It is not affiliated with,
endorsed by, or sponsored by Google LLC or YouTube. "YouTube" and
"YouTube Music" are trademarks of Google LLC, referenced here only to
describe the service this tool interacts with.

This plugin works by calling YouTube Music's internal (InnerTube) web API,
which is undocumented and not intended for third-party use. It may stop
working at any time if Google changes that API, and its continued
functionality is not guaranteed. Use it at your own risk, and in
accordance with YouTube's Terms of Service.

> This plugin uses YouTube Music's unofficial InnerTube API. It may stop
> working if Google changes that API without notice. See
> [If the API changes](#if-the-api-changes) below for how to fix it.

## Features

- Browse Home, Charts & Trending, New Releases, Moods & Genres, Podcasts
- Search (Songs, Albums, Artists, Playlists)
- My Playlists — save favourite playlists so they always appear in the menu
- Play individual songs or explode a full playlist/album into your queue
- Background prefetching of the next track so transitions are near-instant
- Works on real Squeezebox hardware (tested on Squeezebox Radio) and
  software players (squeezeslave)
- Correct title, artist, and artwork metadata
- Compatible with philippe44's LMS-YouTube plugin — existing `youtube://` 
  Favorites continue to work if switching from that plugin to this one

> **Note:** When searching for podcasts, individual episodes appear under
> the **Songs** category. This is expected — they play correctly as audio
> tracks. To browse full podcast shows, use the **Podcasts** menu section.

## Requirements

- Lyrion Music Server 9.0+
- Python 3.9+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- ffmpeg
- `libio-socket-ssl-perl` (Perl SSL support)

On Raspberry Pi OS / Debian:

```bash
sudo apt install -y python3 python3-pip ffmpeg libio-socket-ssl-perl libnet-ssleay-perl
sudo pip3 install yt-dlp --break-system-packages
```
### piCorePlayer notes

- Install `pcp-ffmpeg.tcz` from the pCP extension manager — **not** `ffmpeg.tcz` (the standard one lacks MP3 support and won't work)
- The plugin will automatically detect available codecs and fall back to AAC if MP3 is unavailable
- yt-dlp should be installed via pip: `pip3 install yt-dlp`

> **Tip:** Once installed, you can update yt-dlp at any time from **Settings → Advanced → YouTube Music → Update yt-dlp** without needing command line access.

## Installation (recommended)

1. Make sure the requirements above are installed.
2. In the LMS web interface, go to **Settings → Manage Plugins**.
3. Scroll to the bottom to **Additional Repositories** and paste:
https://raw.githubusercontent.com/schmij97/lms-ytmusic/main/repo.xml

4. Click **Apply**.
5. Refresh the page. A new section, **"YouTube Music for LMS,"** appears
   with the plugin listed. Check the box next to **YouTube Music** and
   click **Apply** again.
6. Restart LMS when prompted.
7. **YouTube Music** will now appear under **My Apps** in your LMS menu.

## Manual installation (fallback)

If you can't use the repository method, download the latest release ZIP
from the [Releases page](https://github.com/schmij97/lms-ytmusic/releases)
and extract it directly into your LMS plugins directory:

```bash
unzip YouTubeMusic-X.Y.Z.zip
sudo cp -r Plugins/YouTubeMusic /var/lib/squeezeboxserver/cache/InstalledPlugins/Plugins/
sudo chown -R squeezeboxserver /var/lib/squeezeboxserver/cache/InstalledPlugins/Plugins/YouTubeMusic
sudo systemctl restart logitechmediaserver
```

If the plugin doesn't appear after restarting, you may need to manually
enable it:

```bash
sudo nano /var/lib/squeezeboxserver/prefs/plugin/state.prefs
```

Add a line `YouTubeMusic: enabled` right after the `---` at the top, save,
and restart LMS again.

## Configuration

In LMS, go to **Settings → Advanced → YouTube Music** to change the local
proxy port (default `9876`) if it conflicts with something else on your
system.

## Troubleshooting

**Plugin doesn't show up after adding the repository.** LMS caches
repository data for 5 minutes. Wait a few minutes and refresh the
Manage Plugins page. If you've already manually installed the plugin
(see above), it won't show as "available to install" since it's already
active — that's expected.

**No audio plays / decoder errors on hardware Squeezeboxes.** Make sure
`ffmpeg` is installed; the plugin pipes `yt-dlp` output through `ffmpeg`
to produce a clean MP3 stream, since raw YouTube CDN files aren't
reliably playable by hardware decoders.

**Wrong song plays after selecting one from search/browse.** This was a
bug in earlier versions related to non-deterministic search ordering;
should not occur in 1.1.0+. If it does, file an issue.

**"yt-dlp not found."** Confirm it's in your PATH: `which yt-dlp`. If
not, reinstall: `sudo pip3 install yt-dlp --break-system-packages`.

**Check the proxy is alive:**

```bash
curl http://127.0.0.1:9876/ping
```

Should return `{"status": "ok"}`.

**Enable debug logging:** In LMS, go to **Settings → Advanced → Logging**,
set `plugin.youtubemusic` to **Debug**, and check
`/var/log/squeezeboxserver/server.log`.

## If the API changes

YouTube occasionally updates internal values used by their web client.
This plugin's only YouTube-specific code lives in `ytmproxy.py`. Two
constants near the top are the most likely things to need updating:

```python
API_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-KLET5YdCE"
_CLIENT = {
    "clientName":    "WEB_REMIX",
    "clientVersion": "1.20240918.01.00",
    ...
}
```

To find current values: open `music.youtube.com` in a browser, open dev
tools → Network tab, inspect any request's payload/headers for
`clientVersion` and `X-Goog-Api-Key`.

If YouTube changes the *structure* of search/browse responses (rather
than just version strings), the parsing functions in `ytmproxy.py`
(`_parse_song`, `_shelf_items`, `_classify_and_parse`, etc.) may need
updates to match the new JSON shape. Inspecting a raw response and
comparing it to the current parsing logic is the way to find what
changed.

## Architecture

| Component | Role |
|-----------|------|
| `Plugin.pm` | Menus, lifecycle, proxy management |
| `API.pm` | Async HTTP calls to local proxy |
| `ProtocolHandler.pm` | `ytm://` scheme, streaming, metadata |
| `PlaylistProtocolHandler.pm` | `ytmplaylist://` scheme — play whole playlist/album |
| `Settings.pm` | Web settings page |
| `ytmproxy.py` | Python sidecar on `127.0.0.1:9876` — InnerTube API, streaming, prefetch |

**Request flow:**
```
Device → LMS → ytmproxy.py → yt-dlp | ffmpeg → MP3 → Device
                    ↘ /prefetch/<id>  (background, next track)
```

## License

GPL-2.0, consistent with the LMS plugin ecosystem.

## Acknowledgements

- [philippe44/LMS-YouTube](https://github.com/philippe44/LMS-YouTube) —
  streaming architecture reference
- [paul-1/plugin-SiriusXM](https://github.com/paul-1/plugin-SiriusXM) —
  local proxy pattern reference
- [OuterTune/OuterTune](https://github.com/OuterTune/OuterTune) —
  InnerTube API research
