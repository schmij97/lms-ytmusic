package Plugins::YouTubeMusic::API;

use strict;
use warnings;

use JSON::XS          qw(decode_json);
use Scalar::Util      qw(blessed);
use URI::Escape       qw(uri_escape_utf8);

use Slim::Utils::Log;
use Slim::Utils::Prefs;
use Slim::Networking::SimpleAsyncHTTP;

my $prefs = preferences('plugin.youtubemusic');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubemusic',
    defaultLevel => 'INFO',
    description  => 'PLUGIN_YOUTUBEMUSIC',
});

sub _proxy_url {
    my $port = $prefs->get('proxy_port') || 9876;
    return "http://127.0.0.1:$port";
}

sub _get {
    my ($path, $cb) = @_;

    my $url = _proxy_url() . $path;
    $log->debug("API GET: $url");

    Slim::Networking::SimpleAsyncHTTP->new(
        sub {
            my $http = shift;
            my $data = eval { decode_json($http->content) };
            if ($@) {
                $log->error("JSON decode error for $url: $@");
                $cb->(undef);
            } else {
                $cb->($data);
            }
        },
        sub {
            my ($http, $err) = @_;
            $log->error("Proxy request failed ($url): $err");
            $cb->(undef);
        },
        { timeout => 30 }
    )->get($url);
}

sub search {
    my ($class, $query, $type, $cb) = @_;
    my $q = uri_escape_utf8($query);
    _get("/search?q=$q&type=$type", $cb);
}

sub browseHome {
    my ($class, $cb) = @_;
    _get('/browse/home', $cb);
}

sub browseCharts {
    my ($class, $cb) = @_;
    _get('/browse/charts', $cb);
}

sub browsePlaylist {
    my ($class, $browse_id, $cb) = @_;
    _get("/playlist?browseId=$browse_id", $cb);
}

sub browseAlbum {
    my ($class, $browse_id, $cb) = @_;
    _get("/album?browseId=$browse_id", $cb);
}

sub browseArtist {
    my ($class, $browse_id, $cb) = @_;
    _get("/artist?browseId=$browse_id", $cb);
}

sub getSongInfo {
    my ($class, $video_id, $cb) = @_;
    _get("/song?videoId=$video_id", $cb);
}

sub prefetch {
    my ($class, $video_id, $cb) = @_;
    $cb ||= sub {};
    _get("/prefetch/$video_id", $cb);
}

sub browseNewReleases {
    my ($class, $cb) = @_;
    _get("/browse/new_releases", $cb);
}
sub browseMoods {
    my ($class, $cb) = @_;
    _get("/browse/moods", $cb);
}
sub browseMoodCategory {
    my ($class, $browse_id, $params, $cb) = @_;
    _get("/browse/mood_category?browseId=$browse_id&params=" . uri_escape_utf8($params), $cb);
}
sub browsePodcasts {
    my ($class, $cb) = @_;
    _get("/browse/podcasts", $cb);
}

1;
