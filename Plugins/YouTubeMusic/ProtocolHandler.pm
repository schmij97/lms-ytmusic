package Plugins::YouTubeMusic::ProtocolHandler;

# Implements the ytm:// protocol scheme for Lyrion Music Server.
#
# Flow:
#   1. LMS calls getNextTrack() when a ytm://VIDEO_ID URL is about to play.
#   2. We point the track at our local Python proxy's /stream/VIDEO_ID
#      endpoint (http://127.0.0.1:PORT/stream/VIDEO_ID), which internally
#      pipes yt-dlp's output through ffmpeg to produce a clean, sequential
#      MP3 stream. This avoids moov-atom-at-end-of-file issues that prevent
#      hardware Squeezebox decoders from playing raw YouTube CDN MP4/WebM.
#   3. Since the proxy is local plain HTTP, we extend the base HTTP handler
#      (no TLS needed) and just substitute the stream URL in new().

use strict;
use warnings;
use base qw(Slim::Player::Protocols::HTTP);

use Scalar::Util       qw(blessed);

use Slim::Utils::Log;
use Slim::Utils::Prefs;
use Slim::Player::ProtocolHandlers;
use Slim::Player::Source;
use Slim::Player::Playlist;

use Plugins::YouTubeMusic::API;

my $prefs = preferences('plugin.youtubemusic');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubemusic',
    defaultLevel => 'INFO',
    description  => 'PLUGIN_YOUTUBEMUSIC',
});

# ── Capability declarations ──────────────────────────────────────────────────

sub isRemote        { 1 }
sub isAudio         { 1 }
sub isAudioURL      { 1 }
sub canSeek         { 0 }
sub canDirectStream { 0 }
sub songBytes       {}

# Audio format — determined at startup by querying the proxy's /codec
# endpoint, which probes ffmpeg for available encoders. Defaults to mp3
# but falls back to aac on platforms like piCorePlayer where libmp3lame
# is not available.
my $_audio_format = 'mp3';
my %_radio_active;  # tracks which clients have radio running

# Required for scrobbling. Slim::Plugin::AudioScrobbler::Plugin::canScrobble
# rejects any remote track whose handler does not implement this, so without
# it YouTube Music plays are silently never submitted to Last.fm/ListenBrainz.
# 'P' = chosen by the user; 'R' = internet radio, which AudioScrobbler filters
# out unless the include_radio pref is enabled.
sub audioScrobblerSource {
    my ($class, $client) = @_;
    my $client_id = ($client && $client->id) // '';
    return ($client_id && $_radio_active{$client_id}) ? 'R' : 'P';
}

sub _init_audio_format {
    my $port = preferences('plugin.youtubemusic')->get('proxy_port') || 9876;
    Slim::Networking::SimpleAsyncHTTP->new(
        sub {
            my $http = shift;
            my $data = eval { JSON::XS::decode_json($http->content) };
            if ($data && $data->{format}) {
                my $fmt = $data->{format};
                # Normalise format strings to what LMS expects
                $fmt = 'aac' if $fmt eq 'adts';
                $fmt = 'flc' if $fmt eq 'flac';
                $_audio_format = $fmt;
                $log->info("Audio format set to: $fmt");
            }
        },
        sub { $log->warn("Could not query codec from proxy, defaulting to mp3") },
    )->get("http://127.0.0.1:$port/codec");
}

sub formatOverride  { $_audio_format }
sub getFormatForURL { $_audio_format }
sub contentType     { $_audio_format }

# Substitute the local proxy stream URL before the base class opens the
# socket, same pattern used by RadioParadise's protocol handler.
sub new {
    my $class  = shift;
    my $args   = shift;
    my $client = $args->{client};
    my $song   = $args->{song};

    my $streamUrl = $song ? $song->streamUrl() : undef;
    unless ($streamUrl) {
        $log->error('No resolved stream URL available for ' . ($args->{url} // 'unknown'));
        return undef;
    }

    if ($args->{url} && $args->{redir}) {
        if ($args->{redir} ne $args->{url}) {
            $streamUrl = $args->{url};
        } else {
            $log->error("Redirection loop for url: $streamUrl");
        }
    }

    $log->info("Opening local proxy stream: $streamUrl");

    return $class->SUPER::new({
        url    => $streamUrl,
        song   => $song,
        client => $client,
    });
}

# ── Main resolution ───────────────────────────────────────────────────────────

# Extract a YouTube video ID from any of the URL shapes we handle:
#   ytm://VIDEO_ID                         (our native scheme)
#   youtube://VIDEO_ID                     (philippe44 compat)
#   youtube://www.youtube.com/v/VIDEO_ID   (philippe44 compat)
#   https://www.youtube.com/watch?v=ID     (plain YouTube URL)
#   https://youtu.be/VIDEO_ID             (short URL)
sub _extract_video_id {
    my ($url) = @_;
    my ($vid);
    ($vid) = $url =~ m{^ytm://([A-Za-z0-9_\-]+)}                    and return $vid;
    ($vid) = $url =~ m{^youtube://([A-Za-z0-9_\-]+)$}               and return $vid;
    ($vid) = $url =~ m{youtube://(?:www\.)?youtube\.com/v/([A-Za-z0-9_\-]+)} and return $vid;
    ($vid) = $url =~ m{[?&]v=([A-Za-z0-9_\-]+)}                     and return $vid;
    ($vid) = $url =~ m{youtu\.be/([A-Za-z0-9_\-]+)}                and return $vid;
    return undef;
}

sub getNextTrack {
    my ($class, $song, $successCb, $errorCb) = @_;

    my $url   = $song->currentTrack()->url;
    my $vid = _extract_video_id($url);

    unless ($vid) {
        $log->error("Unrecognised URL: $url");
        $errorCb->('Invalid YouTube Music URL');
        return;
    }


    my $port       = $prefs->get('proxy_port') || 9876;
    # Use the real LMS server address rather than 127.0.0.1 so that
    # players on separate machines can fetch the stream directly when
    # LMS decides to use direct streaming instead of proxying.
    my $server_ip  = Slim::Utils::Network::serverAddr() || '127.0.0.1';
    my $streamUrl  = "http://$server_ip:$port/stream/$vid";

    $log->info("Routing playback through local proxy: $streamUrl");

    $song->streamUrl($streamUrl);

    # Kick off a non-blocking metadata fetch (title/artist/artwork)
    _fetch_metadata($vid, $song);


    # Delay prefetch check by 3s so radio addtracks completes first
    my $prefetch_client = eval { $song->master() };
    _prefetch_with_client($prefetch_client) if $prefetch_client;
    $successCb->();
}

sub _prefetch_with_client {
    my ($client) = @_;
    return unless $client;
    my $current_index = eval { Slim::Player::Source::playingSongIndex($client) };
    return unless defined $current_index;
    my $next_index = $current_index + 1;
    my $count      = eval { Slim::Player::Playlist::count($client) } || 0;
    $log->info("Prefetch: current=$current_index next=$next_index count=$count");
    if ($next_index >= $count || ($count - $next_index) <= 3) {
        my $cur_track = eval { Slim::Player::Playlist::track($client, $current_index) };
        if ($cur_track) {
            my $cur_url = eval { $cur_track->url } // '';
            my ($cur_vid) = $cur_url =~ m{^ytm://([A-Za-z0-9_\-]+)};
            _start_radio($client, $cur_vid) if $cur_vid;
        }
        return;
    }
    my $next_track = eval { Slim::Player::Playlist::track($client, $next_index) };
    return unless $next_track;
    my $next_url = eval { $next_track->url };
    return unless $next_url;
    my ($next_vid) = $next_url =~ m{^ytm://([A-Za-z0-9_\-]+)};
    return unless $next_vid;
    $log->info("Prefetching next track: $next_vid");
    Plugins::YouTubeMusic::API->prefetch($next_vid, sub {});
}

sub _prefetch_next_track {
    my ($song) = @_;
    $log->info("_prefetch_next_track called");

    my $client = eval { $song->master() };
    unless ($client) { $log->debug("Prefetch: no client"); return; }

    my $current_index = eval { Slim::Player::Source::playingSongIndex($client) };
    unless (defined $current_index) { $log->debug("Prefetch: no current_index, err=$@"); return; }

    my $next_index = $current_index + 1;
    my $count      = eval { Slim::Player::Playlist::count($client) } || 0;
    if ($next_index >= $count || ($count - $next_index) <= 3) {
        # Queue is empty — auto-continue with radio based on current track
        $log->debug("Prefetch: queue empty, starting radio from current track");
        my $cur_track = eval { Slim::Player::Playlist::track($client, $current_index) };
        if ($cur_track) {
            my $cur_url = eval { $cur_track->url } // '';
            my ($cur_vid) = $cur_url =~ m{^ytm://([A-Za-z0-9_\-]+)};
            _start_radio($client, $cur_vid) if $cur_vid;
        }
        return;
    }

    my $next_track = eval { Slim::Player::Playlist::track($client, $next_index) };
    unless ($next_track) { $log->debug("Prefetch: no next_track at index $next_index, err=$@"); return; }

    my $next_url = eval { $next_track->url };
    unless ($next_url) { $log->debug("Prefetch: no next_url"); return; }

    $log->debug("Prefetch: next_url is $next_url");

    my ($next_vid) = $next_url =~ m{^ytm://([A-Za-z0-9_\-]+)};
    unless ($next_vid) { $log->debug("Prefetch: next_url didn't match ytm:// pattern"); return; }

    $log->debug("Prefetching next track: $next_vid");
    Plugins::YouTubeMusic::API->prefetch($next_vid, sub {
        my $result = shift;
        if ($result && ref $result eq 'HASH') {
            $log->debug("Prefetch status for $next_vid: " . ($result->{status} // 'unknown'));
        }
    });
}

# ── Metadata ──────────────────────────────────────────────────────────────────

# In-memory metadata cache keyed by video ID. We avoid writing to the
# track's DB relations (artist is a Contributor object, not a plain
# string, and fighting that plumbing for remote/ephemeral tracks isn't
# worth it) and instead let getMetadataFor read straight from here.
my %_metadata_cache;

# Called synchronously by Plugin.pm/PlaylistProtocolHandler.pm with data we
# already have on hand from search/browse results, so the cache is warm
# before LMS ever needs it - avoids a placeholder-text flash while the
# normal async getSongInfo fetch would otherwise still be in flight.
sub primeMetadata {
    my ($class, $video_id, $info) = @_;
    return unless $video_id && $info;

    $_metadata_cache{$video_id} ||= {
        title    => $info->{title}     || '',
        artist   => $info->{artist}    || '',
        album    => $info->{album}     || '',
        duration => $info->{duration}  || 0,
        cover    => $info->{thumbnail} || '',
    };
}


sub reset_radio {
    my ($client) = @_;
    return unless $client;
    my $client_id = $client->id // '';
    delete $_radio_active{$client_id} if $client_id;
    $log->debug("Radio flag reset for $client_id");
}

sub _start_radio {
    my ($client, $video_id) = @_;
    return unless $client && $video_id;

    # Don't trigger radio if already running for this client
    my $client_id = $client->id // '';
    if ($_radio_active{$client_id}) {
        $log->debug("Radio already active for $client_id, skipping");
        return;
    }
    $_radio_active{$client_id} = 1;

    $log->info("Starting radio from videoId: $video_id");
    $log->info("Radio client: " . ($client->id // "unknown") . " name: " . ($client->name // "unknown"));

    Plugins::YouTubeMusic::API->browseRadio($video_id, sub {
        my $data = shift;
        unless ($data && ref $data eq 'ARRAY' && @$data) {
            $log->warn("Radio returned no tracks for $video_id");
            return;
        }

        # Skip the first track if it's the same as current
        my @tracks = grep { $_->{videoId} && $_->{videoId} ne $video_id } @$data;
        unless (@tracks) {
            $log->warn("Radio returned no new tracks for $video_id");
            return;
        }

        # Prime metadata cache for radio tracks so queue shows
        # correct titles/artwork instead of raw video IDs
        for my $track (@tracks) {
            next unless $track->{videoId};
            Plugins::YouTubeMusic::ProtocolHandler->primeMetadata(
                $track->{videoId}, $track
            );
        }
        my @urls = map { "ytm://$_->{videoId}" } @tracks;
        $log->info("Adding " . scalar(@urls) . " radio tracks to queue");

        # Add tracks to the end of the current playlist
        # Add tracks individually — addtracks listRef can fail for some player types
        for my $url (@urls) {
            $client->execute(['playlist', 'add', $url]);
        }
        # Re-run prefetch now that queue has tracks
        _prefetch_with_client($client);
        # Reset radio flag after a delay so radio can fire again
        # when the added tracks eventually run out
        Slim::Utils::Timers::setTimer(
            $client, Time::HiRes::time() + 300,
            sub { delete $_radio_active{$client_id}; }
        );
    });
}

sub _fetch_metadata {
    my ($video_id, $song) = @_;

    Plugins::YouTubeMusic::API->getSongInfo($video_id, sub {
        my $info = shift;
        return unless $info && ref $info eq 'HASH';

        $_metadata_cache{$video_id} = {
            title    => $info->{title}     || '',
            artist   => $info->{artist}    || '',
            album    => $info->{album}     || '',
            duration => $info->{duration}  || 0,
            cover    => $info->{thumbnail} || '',
        };

        my $track = $song->currentTrack();
        $track->title($info->{title})        if $info->{title};
        $track->secs($info->{duration})      if $info->{duration};
        $track->coverurl($info->{thumbnail}) if $info->{thumbnail};
        $track->update();

        Slim::Control::Request::notifyFromArray(undef, ['newmetadata']);
        $log->debug("Metadata updated for $video_id: " . ($info->{title} // '?') . " by " . ($info->{artist} // '?'));
    });
}

# ── getMetadataFor (called when building Now Playing info) ────────────────────

sub getMetadataFor {
    my ($class, $client, $url) = @_;

    my ($vid) = $url =~ m{^ytm://([A-Za-z0-9_\-]+)};
    return {} unless $vid;

    my $cached = $_metadata_cache{$vid};

    my %meta  = (
        title   => ($cached && $cached->{title})  ? $cached->{title}  : "YouTube Music - $vid",
        artist  => ($cached && $cached->{artist}) ? $cached->{artist} : '',
        album   => ($cached && $cached->{album}) ? $cached->{album} : ' | YouTube Music',
        cover   => ($cached && $cached->{cover})  ? $cached->{cover}
                    : Plugins::YouTubeMusic::Plugin->_pluginDataFor('icon'),
        type    => 'YouTube Music',
        bitrate => '192k CBR',
        duration => ($cached && $cached->{duration}) ? $cached->{duration} : undef,
    );

    return \%meta;
}

# ── Icon ──────────────────────────────────────────────────────────────────────

sub getIcon {
    return Plugins::YouTubeMusic::Plugin->_pluginDataFor('icon');
}

1;
