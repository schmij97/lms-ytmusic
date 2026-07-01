package Plugins::YouTubeMusic::Settings;

use strict;
use warnings;
use base qw(Slim::Web::Settings);

use Slim::Utils::Log;
use Slim::Utils::Prefs;

my $prefs = preferences('plugin.youtubemusic');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubemusic',
    defaultLevel => 'INFO',
    description  => 'PLUGIN_YOUTUBEMUSIC',
});

sub name { 'PLUGIN_YOUTUBEMUSIC' }

sub page { 'plugins/YouTubeMusic/settings/basic.html' }

sub prefs { return ($prefs, qw(proxy_port)) }

sub handler {
    my ($class, $client, $params) = @_;

    if ($params->{saveSettings}) {
        # Save proxy port
        my $port = int($params->{proxy_port} || 9876);
        $port = 9876 unless $port >= 1024 && $port <= 65535;
        $prefs->set('proxy_port', $port);

        # Save playlists — collect all name/browseId pairs
        my @playlists;
        my $names = $params->{playlist_name};
        my $ids   = $params->{playlist_id};
        $names = [$names] if $names && !ref $names;
        $ids   = [$ids]   if $ids   && !ref $ids;

        if ($names && $ids) {
            for my $i (0 .. $#$names) {
                my $name = $names->[$i] // '';
                my $id   = $ids->[$i]   // '';
                $name =~ s/^\s+|\s+$//g;
                $id   =~ s/^\s+|\s+$//g;
                next unless $name && $id;
                push @playlists, "$name|$id";
            }
        }
        $prefs->set('my_playlists', \@playlists);
        $log->info("Saved " . scalar(@playlists) . " playlists");
    }

    $params->{my_playlists} = $prefs->get('my_playlists') || [];

    return $class->SUPER::handler($client, $params);
}

1;
