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

sub _init_audio_format {
    my $port = preferences('plugin.youtubemusic')->get('proxy_port') || 9876;
    Slim::Networking::SimpleAsyncHTTP->new(
        sub {
            my $http = shift;
            my $data = eval { JSON::XS::decode_json($http->content) };
            if ($data && $data->{format}) {
                my $fmt = $data->{format} eq 'adts' ? 'aac' : $data->{format};
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
    my $streamUrl  = "http://127.0.0.1:$port/stream/$vid";

    $log->info("Routing playback through local proxy: $streamUrl");

    $song->streamUrl($streamUrl);

    # Kick off a non-blocking metadata fetch (title/artist/artwork)
    _fetch_metadata($vid, $song);

    _prefetch_next_track($song);
    $successCb->();
}

sub _prefetch_next_track {
    my ($song) = @_;

    my $client = eval { $song->master() };
    unless ($client) { $log->debug("Prefetch: no client"); return; }

    my $current_index = eval { Slim::Player::Source::streamingSongIndex($client) };
    unless (defined $current_index) { $log->debug("Prefetch: no current_index, err=$@"); return; }

    my $next_index = $current_index + 1;
    my $count      = eval { Slim::Player::Playlist::count($client) } || 0;
    if ($next_index >= $count) { $log->debug("Prefetch: next_index($next_index) >= count($count)"); return; }

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
        duration => $info->{duration}  || 0,
        cover    => $info->{thumbnail} || '',
    };
}

sub _fetch_metadata {
    my ($video_id, $song) = @_;

    Plugins::YouTubeMusic::API->getSongInfo($video_id, sub {
        my $info = shift;
        return unless $info && ref $info eq 'HASH';

        $_metadata_cache{$video_id} = {
            title    => $info->{title}     || '',
            artist   => $info->{artist}    || '',
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
        album   => 'YouTube Music',
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
