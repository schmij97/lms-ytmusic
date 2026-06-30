package Plugins::YouTubeMusic::PlaylistProtocolHandler;

# Lightweight pseudo-protocol handler for ytmplaylist://BROWSE_ID URLs.
# This handler never streams audio itself — it only implements
# explodePlaylist, which LMS calls when the user chooses "Play" or "Add"
# on a playlist/album menu item. We fetch every track in the playlist via
# the proxy and hand back the full list of ytm:// track URLs, which LMS
# then loads into the real playback queue (enabling prefetch, track
# advance, etc. exactly as if each song had been added individually).

use strict;
use warnings;

use Slim::Utils::Log;
use Plugins::YouTubeMusic::API;

my $log = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubemusic',
    defaultLevel => 'INFO',
    description  => 'PLUGIN_YOUTUBEMUSIC',
});

sub explodePlaylist {
    my ($class, $client, $uri, $callback) = @_;

    my ($browse_id) = $uri =~ m{^ytmplaylist://(.+)$};
    unless ($browse_id) {
        $log->error("Malformed ytmplaylist:// URL: $uri");
        $callback->([]);
        return;
    }

    $log->info("Exploding playlist browseId=$browse_id");

    Plugins::YouTubeMusic::API->browsePlaylist($browse_id, sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH' && $data->{items}) {
            $log->error("Failed to explode playlist $browse_id");
            $callback->([]);
            return;
        }

        my @urls = map {
            $_->{videoId} ? "ytm://$_->{videoId}" : ()
        } @{ $data->{items} };

        $log->info("Exploded playlist $browse_id into " . scalar(@urls) . " tracks");
        $callback->(\@urls);
    });
}

1;
