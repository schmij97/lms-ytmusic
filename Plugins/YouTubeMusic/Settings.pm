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

sub prefs { return ($prefs, qw(proxy_port autoplay codec path_python path_ytdlp path_ffmpeg path_node log_path)) }

sub handler {
    my ($class, $client, $params) = @_;

    if ($params->{saveSettings}) {
        # Save proxy port
        my $port = int($params->{proxy_port} || 9876);
        $port = 9876 unless $port >= 1024 && $port <= 65535;
        $prefs->set('proxy_port', $port);

        # Save autoplay setting
        $prefs->set('autoplay', ($params->{pref_autoplay} // $params->{autoplay}) ? 1 : 0);
        # Save codec setting
        my $codec = $params->{pref_codec} || $params->{codec} || 'auto';
        $codec = 'auto' unless grep { $_ eq $codec } qw(auto mp3 flac aac);
        $prefs->set('codec', $codec);
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
        # Save path overrides
        $prefs->set('path_python', $params->{pref_path_python} // $params->{path_python} // '');
        $prefs->set('path_ytdlp',  $params->{pref_path_ytdlp}  // $params->{path_ytdlp}  // '');
        $prefs->set('path_ffmpeg', $params->{pref_path_ffmpeg} // $params->{path_ffmpeg} // '');
        $prefs->set('path_node',   $params->{pref_path_node}   // $params->{path_node}   // '');
        $prefs->set('log_path',    $params->{pref_log_path}    // $params->{log_path}    // '');
        $log->info("Saved " . scalar(@playlists) . " playlists");
    }

    $params->{my_playlists} = $prefs->get('my_playlists') || [];
    $params->{autoplay} = $prefs->get('autoplay') // 1;
    $params->{pref_autoplay} = $params->{autoplay};
    $params->{codec} = $prefs->get('codec') || 'auto';
    $params->{pref_codec} = $params->{codec};

    $params->{path_python} = $prefs->get('path_python') || '';
    $params->{path_ytdlp}  = $prefs->get('path_ytdlp')  || '';
    $params->{path_ffmpeg} = $prefs->get('path_ffmpeg') || '';
    $params->{path_node}   = $prefs->get('path_node')   || '';
    $params->{log_path}    = $prefs->get('log_path')    || '';
    return $class->SUPER::handler($client, $params);
}

1;
