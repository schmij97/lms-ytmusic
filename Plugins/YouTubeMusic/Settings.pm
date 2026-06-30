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
        my $port = int($params->{proxy_port} || 9876);
        $port = 9876 unless $port >= 1024 && $port <= 65535;
        $prefs->set('proxy_port', $port);
        $log->info("Proxy port set to $port");
    }

    return $class->SUPER::handler($client, $params);
}

1;
