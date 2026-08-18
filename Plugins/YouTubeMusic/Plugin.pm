package Plugins::YouTubeMusic::Plugin;

use strict;
use Time::HiRes;
use warnings;
use base qw(Slim::Plugin::OPMLBased);

use POSIX          qw(SIGTERM);
use File::Spec     ();
use Scalar::Util   qw(blessed);

use Slim::Utils::Log;
use Slim::Menu::GlobalSearch;
use Slim::Utils::Prefs;
use Slim::Utils::Strings qw(cstring string);
use Slim::Player::ProtocolHandlers;

use Plugins::YouTubeMusic::API;
use Plugins::YouTubeMusic::PlaylistProtocolHandler;
use Plugins::YouTubeMusic::ProtocolHandler;

my $prefs = preferences('plugin.youtubemusic');
my $log   = Slim::Utils::Log->addLogCategory({
    category     => 'plugin.youtubemusic',
    defaultLevel => 'INFO',
    description  => 'PLUGIN_YOUTUBEMUSIC',
});

my $PROXY_PID;

sub initPlugin {
    my $class = shift;

    $prefs->init({
        proxy_port    => 9876,
        my_playlists  => [],
    });

    $class->_start_proxy();

    Slim::Player::ProtocolHandlers->registerHandler(
        'ytm', 'Plugins::YouTubeMusic::ProtocolHandler'
    );
    # Query proxy for available audio codec (handles piCorePlayer where
    # ffmpeg lacks libmp3lame and must fall back to aac)
    Slim::Utils::Timers::setTimer(
        undef, Time::HiRes::time() + 5,
        sub { Plugins::YouTubeMusic::ProtocolHandler::_init_audio_format() }
    );
    Slim::Player::ProtocolHandlers->registerHandler(
        'ytmplaylist', 'Plugins::YouTubeMusic::PlaylistProtocolHandler'
    );
    # Handle real YouTube Music URLs pasted directly into LMS



    $class->SUPER::initPlugin(
        feed   => \&_top_level,
        tag    => 'youtubemusic',
        menu   => 'radios',
        is_app => 1,
        weight => 10,
    );

    # Register CLI handler for Jivelite/SB Radio playlist play
    Slim::Control::Request::addDispatch(
        ['youtubemusic', 'playlist', '_method'],
        [1, 1, 1, \&_cliPlaylistCmd]
    );

    if (main::WEBUI) {
        require Plugins::YouTubeMusic::Settings;
        Plugins::YouTubeMusic::Settings->new($class);
    }

    $log->info("YouTube Music plugin initialised");
}

sub shutdownPlugin {
    my $class = shift;
    if ($PROXY_PID) {
        $log->info("Stopping YouTube Music proxy (PID $PROXY_PID)");
        if ($^O eq 'MSWin32') {
            # On Windows, use taskkill to terminate the process tree
            system("taskkill /F /T /PID $PROXY_PID >nul 2>&1");
        } else {
            eval { kill SIGTERM, $PROXY_PID };
            waitpid($PROXY_PID, 0);
        }
        $PROXY_PID = undef;
    }
}

sub getDisplayName { 'PLUGIN_YOUTUBEMUSIC' }
sub playerMenu     { undef }

sub _start_proxy {
    my $class  = shift;
    my $port   = $prefs->get('proxy_port') || 9876;
    my $script = File::Spec->catfile($class->_pluginDataFor('basedir'), 'ytmproxy.py');

    unless (-f $script) {
        $log->error("Proxy script not found at $script");
        return;
    }

    my $python = _find_python();
    unless ($python) {
        $log->error("python3 not found in PATH");
        return;
    }

    $log->info("Starting YouTube Music proxy: $python $script --port $port");

    my $is_windows = $^O eq 'MSWin32';
    if ($is_windows) {
        # fork() is emulated on Windows via threads which conflicts with LMS
        # Use system(1,...) instead which spawns a true background process
        my $codec = $prefs->get('codec') || 'auto';
        my $pid = system(1, $python, $script, '--port', $port, '--log-level', 'WARNING', '--codec', $codec);
        if (!$pid) {
            $log->error("system(1,...) failed: $!");
            return;
        }
        $PROXY_PID = $pid;
        $log->info("Proxy started (PID $pid) via system(1,...) on Windows");
    } else {
        my $pid = fork();
        if (!defined $pid) {
            $log->error("fork() failed: $!");
            return;
        }
        if ($pid == 0) {
            my $codec = $prefs->get('codec') || 'auto';
            exec($python, $script, '--port', $port, '--log-level', 'WARNING', '--codec', $codec) or do {
                $log->error("exec failed: $!");
                exit 1;
            };
        }
        $PROXY_PID = $pid;
        $log->info("Proxy started (PID $pid)");
    }
}

sub _find_python {
    # On Windows use "where", on Unix use "which"
    my $is_windows = $^O eq 'MSWin32';
    my $finder = $is_windows ? 'where' : 'which';
    # Check python3 first (Unix), then python, then py (Windows launcher)
    my @candidates = $is_windows ? qw(python3 python py) : qw(python3 python);
    # On macOS, also check versioned binaries and common install paths
    # to avoid picking up Xcode's old Python 3.9 stub
    unless ($is_windows) {
        unshift @candidates, qw(python3.13 python3.12 python3.11 python3.10);
        push @candidates, '/usr/local/bin/python3', '/opt/homebrew/bin/python3',
                          '/usr/local/opt/python3/bin/python3';
    }
    my $best_path = '';
    my $best_ver  = 0;
    for my $py (@candidates) {
        my $null = $is_windows ? '2>nul' : '2>/dev/null';
        my $path = `$finder $py $null 2>/dev/null || echo $py`; chomp $path;
        # For absolute paths, skip the finder call
        if ($py =~ m{^/}) {
            $path = $py;
        }
        next unless defined $path && $path ne '';
        # "where" may return multiple lines — skip WindowsApps stub
        if ($is_windows) {
            my @paths = split /\n/, $path;
            my ($real) = grep { !/WindowsApps/i } @paths;
            $path = $real || $paths[0] || '';
        }
        $path =~ s/\r//g;  # strip carriage returns on any platform
        next unless $path && -e $path;
        $path =~ s/^"(.*)"$/$1/;  # strip surrounding quotes if any
        # Check version — require 3.10+ for yt-dlp-ejs compatibility
        my $ver_str = `"$path" -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)" 2>/dev/null`;
        chomp $ver_str;
        my $ver = int($ver_str || 0);
        if ($ver >= 310 && $ver > $best_ver) {
            $best_ver  = $ver;
            $best_path = $path;
        } elsif ($ver > 0 && $best_ver == 0) {
            # Accept any Python if nothing 3.10+ found yet
            $best_path = $path;
        }
    }
    return $best_path if $best_path;
    # Last resort on Windows: check registry and common install paths
    if ($is_windows) {
        # Try registry first (works for all accounts including SYSTEM)
        eval {
            require Win32::TieRegistry;
            Win32::TieRegistry->import(Delimiter => "/");
            for my $ver (qw(3.13 3.12 3.11 3.10 3.9 3.8)) {
                my $key = "HKEY_LOCAL_MACHINE/SOFTWARE/Python/PythonCore/$ver/InstallPath//";
                my $p = $Win32::TieRegistry::Registry->{$key};
                if ($p) {
                    $p =~ s/\\$//;
                    $p .= "\\python.exe";
                    return $p if -e $p;
                }
            }
        };
        # Also scan all drives and all users
        for my $drive ('C', 'D', 'E', 'F') {
            # System-wide install
            for my $ver (qw(313 312 311 310 39 38)) {
                my $p = "$drive:\\Program Files\\Python$ver\\python.exe";
                return $p if -e $p;
                $p = "$drive:\\Program Files (x86)\\Python$ver\\python.exe";
                return $p if -e $p;
            }
            # Per-user installs - scan all user profiles
            for my $users_dir ("$drive:\\Users") {
                next unless -d $users_dir;
                opendir(my $dh, $users_dir) or next;
                my @users = grep { !/^\./ } readdir($dh);
                closedir($dh);
                for my $user (@users) {
                    for my $ver (qw(313 312 311 310 39 38)) {
                        my $p = "$users_dir\\$user\\AppData\\Local\\Programs\\Python\\Python$ver\\python.exe";
                        return $p if -e $p;
                    }
                }
            }
        }
    }
    return undef;
}

sub _register_proxy_handlers {
    for my $ep (qw(ytdlp_status ffmpeg_status codec ping)) {
        my $endpoint = $ep;
        Slim::Web::Pages->addRawFunction(
            "plugins/YouTubeMusic/proxy/$endpoint",
            sub {
                my ($httpClient, $response) = @_;
                my $port = $prefs->get('proxy_port') || 9876;
                Slim::Networking::SimpleAsyncHTTP->new(
                    sub {
                        my $http = shift;
                        $response->code(200);
                        $response->header('Content-Type', 'application/json');
                        $response->header('Access-Control-Allow-Origin', '*');
                        $response->content($http->content);
                        $httpClient->send_response($response);
                        Slim::Web::HTTP::closeHTTPSocket($httpClient);
                    },
                    sub {
                        $response->code(500);
                        $response->content('{"error":"proxy failed"}');
                        $httpClient->send_response($response);
                        Slim::Web::HTTP::closeHTTPSocket($httpClient);
                    },
                    { timeout => 35 }
                )->get("http://127.0.0.1:$port/$endpoint");
            }
        );
    }
}

sub postinitPlugin {
    my $class = shift;
    # Register youtube:// scheme only if nothing else has claimed it yet.
    # This runs after all plugins have initialised, so we can safely check
    # whether philippe44's LMS-YouTube (or another plugin) is already
    # handling it. If it is, we leave it alone; if not, we take it over
    # so existing Favorites and integrations keep working.
    if (!Slim::Player::ProtocolHandlers->handlerForURL('youtube://x')) {
        $log->info("Registering youtube:// compatibility shim");
        Slim::Player::ProtocolHandlers->registerHandler(
            'youtube', 'Plugins::YouTubeMusic::ProtocolHandler'
        );
        # Also register URL handler for real YouTube Music URLs
        if (Slim::Player::ProtocolHandlers->can('registerURLHandler')) {
            Slim::Player::ProtocolHandlers->registerURLHandler(
                qr{^https?://(?:(?:www|m|music)\.youtube\.com/(?:watch\?|playlist\?|channel/)|youtu\.be/)}i,
                'Plugins::YouTubeMusic::ProtocolHandler'
            );
            $log->info("Registered YouTube Music URL handler for music.youtube.com URLs");
        } else {
            $log->warn("registerURLHandler not available in this LMS version");
        }
    } else {
        $log->info("youtube:// already handled by another plugin, skipping URL handler registration");
    }

    # Register global search provider (closures capture query+type)
    Slim::Menu::GlobalSearch->registerInfoProvider( 'YouTube' => (
            after         => 'top',
            remote_search => 1,
            func          => sub {
                    my ( $client, $tags ) = @_;
                    my $q = $tags->{search} || '';
                    my $make = sub {
                            my ($type, $stringid) = @_;
                            return {
                                    name => cstring($client, $stringid),
                                    url  => sub {
                                            my ($client, $callback, $params) = @_;
                                            _globalSearch($client, $callback, { search => $q, type => $type });
                                    },
                            };
                    };
                    return {
                            name  => 'YouTube',
                            items => [
                                    $make->('songs',     'PLUGIN_YOUTUBEMUSIC_SONGS'),
                                    $make->('albums',    'PLUGIN_YOUTUBEMUSIC_ALBUMS'),
                                    $make->('artists',   'PLUGIN_YOUTUBEMUSIC_ARTISTS'),
                                    $make->('playlists', 'PLUGIN_YOUTUBEMUSIC_PLAYLISTS'),
                            ],
                    };
            },
    ) );
    $log->info("Registered YouTube Music global search provider");
    _register_proxy_handlers();

    # Subscribe to player stop events so we can trigger radio
    # when the queue empties and the player stops naturally
    Slim::Control::Request::subscribe(
        \&_on_playlist_stop,
        [['playlist'], ['stop']]
    );
}


sub _globalSearch {
    my ( $client, $callback, $params ) = @_;
    $params = {} unless ref($params) eq 'HASH';

    my $query = $params->{search} || $params->{searchTerm} || $params->{q} || $params->{query} || '';
    my $type  = $params->{type} || 'all';

    if (!$query) {
        $callback->({ items => [] });
        return;
    }

    Plugins::YouTubeMusic::API->search($query, $type, sub {
        my $results = shift || [];
        my $items = eval { _items_to_menu($client, $results) } || [];
        $callback->({ items => $items });
    });
}
sub _cliPlaylistCmd {
    my $request = shift;
    my $client  = $request->client;
    my $method  = $request->getParam('_method') || 'play';
    my $item_id = $request->getParam('item_id') || '';
    my $touch   = $request->getParam('touchToPlay') || '';

    $log->info("_cliPlaylistCmd: method=$method item_id=$item_id touchToPlay=$touch");

    # Only handle touchToPlay requests from Jivelite/SB Radio/SqueezePlay
    # For normal Material skin play commands, delegate to XMLBrowser
    unless ($touch) {
        Slim::Control::XMLBrowser::cliQuery('youtubemusic', Plugins::YouTubeMusic::Plugin->feed($request->client), $request);
        return;
    }

    # Strip session ID prefix if present (e.g. "abc123.3.0" -> "3.0")
    $item_id =~ s/^[a-f0-9\-]{8,}\.//;
    $touch   =~ s/^[a-f0-9\-]{8,}\.//;

    # Use touchToPlay for navigation if available (it has the full path)
    my $nav_id = $touch || $item_id;

    # Split into path components e.g. "3.0.0" -> (3, 0, 0)
    my @path = split /\./, $nav_id;

    $log->info("_cliPlaylistCmd: navigating path=" . join('.', @path));

    # Get top-level feed
    _top_level($client, sub {
        my $feed = shift;
        my $items = ref $feed eq 'HASH' ? $feed->{items} : $feed;
        unless ($items && ref $items eq 'ARRAY') {
            $log->warn("_cliPlaylistCmd: no top-level items");
            $request->setStatusDone();
            return;
        }

        # Navigate to the target item
        my $level0_idx = $path[0];
        my $item = $items->[$level0_idx];
        unless ($item) {
            $log->warn("_cliPlaylistCmd: no item at index $level0_idx");
            $request->setStatusDone();
            return;
        }

        $log->info("_cliPlaylistCmd: level0 item=" . ($item->{name} || 'unknown'));

        # If path has more components, navigate deeper
        if (scalar @path >= 2 && ref $item->{url} eq 'CODE') {
            my $pt = $item->{passthrough} || [{}];
            $item->{url}->($client, sub {
                my $subfeed = shift;
                my $subitems = ref $subfeed eq 'HASH' ? $subfeed->{items} : $subfeed;
                unless ($subitems && ref $subitems eq 'ARRAY') {
                    $request->setStatusDone();
                    return;
                }
                my $level1_idx = $path[1];
                my $subitem = $subitems->[$level1_idx];
                unless ($subitem) {
                    $log->warn("_cliPlaylistCmd: no item at index $level1_idx");
                    $request->setStatusDone();
                    return;
                }
                $log->info("_cliPlaylistCmd: level1 item=" . ($subitem->{name} || 'unknown'));

                # Play the playlist URL
                my $play_url = $subitem->{play} || $subitem->{url};
                if ($play_url && !ref($play_url)) {
                    $log->info("_cliPlaylistCmd: playing $play_url");
                    if ($play_url =~ /^ytmplaylist:\/\//) {
                        # Explode playlist then start from the right track index
                        my $start_index = defined $path[2] ? $path[2] : 0;
                        Plugins::YouTubeMusic::PlaylistProtocolHandler->explodePlaylist(
                            $client, $play_url, sub {
                                my $urls = shift;
                                if ($urls && @$urls) {
                                    if ($method eq 'play') {
                                        $client->execute(['playlist', 'loadtracks', 'listref', $urls, undef, $start_index]);
                                        $client->execute(['play']);
                                    } else {
                                        $client->execute(['playlist', 'addtracks', 'listref', $urls]);
                                    }
                                }
                                $request->setStatusDone();
                            }
                        );
                        return;
                    } else {
                        $client->execute(['playlist', $method, $play_url]);
                    }
                }
                $request->setStatusDone();
            }, {}, @{$pt});
        } else {
            my $play_url = $item->{play} || $item->{url};
            if ($play_url && !ref($play_url)) {
                $log->info("_cliPlaylistCmd: playing $play_url");
                $client->execute(['playlist', $method, $play_url]);
            }
            $request->setStatusDone();
        }
    }, {});

    $request->setStatusProcessing();
}

sub _on_playlist_stop {
    my $request = shift;
    my $client  = $request->client() or return;

    # Only act on ytm:// tracks
    my $current = eval { Slim::Player::Playlist::track($client,
        Slim::Player::Source::playingSongIndex($client)) };
    return unless $current;
    my $url = eval { $current->url } // '';
    return unless $url =~ m{^ytm://};

    # Only trigger if queue is empty or nearly empty
    my $count = eval { Slim::Player::Playlist::count($client) } || 0;
    my $index = eval { Slim::Player::Source::playingSongIndex($client) } // 0;
    return unless ($count - $index) <= 1;

    my ($vid) = $url =~ m{^ytm://([A-Za-z0-9_\-]+)};
    return unless $vid;

    # Delay to avoid firing during track transitions — check player is
    # still stopped after 5 seconds before triggering radio
    Slim::Utils::Timers::setTimer(
        $client, Time::HiRes::time() + 5,
        sub {
            my $mode = Slim::Player::Source::playmode($client);
            return unless $mode eq 'stop';
            return unless $prefs->get('autoplay') // 1;
            $log->info("Player genuinely stopped — triggering radio");
            Plugins::YouTubeMusic::ProtocolHandler::reset_radio($client);
            Plugins::YouTubeMusic::ProtocolHandler::_start_radio($client, $vid);
            $client->execute(['play']);
        }
    );
}


sub _top_level {
    my ($client, $callback, $args) = @_;

    my @items = (
        {
            name   => cstring($client, 'PLUGIN_YOUTUBEMUSIC_SEARCH'),
            url    => \&_search_dispatch,
            type   => 'search',
            search => '',
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_HOME'),
            url   => \&_home_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_CHARTS'),
            url   => \&_charts_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_MY_PLAYLISTS'),
            url   => \&_my_playlists_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_NEW_RELEASES'),
            url   => \&_new_releases_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_MOODS'),
            url   => \&_moods_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_PODCASTS'),
            url   => \&_podcasts_menu,
        },
    );

    $callback->({ items => \@items });
}

sub _search_dispatch {
    my ($client, $callback, $args) = @_;

    my $query = $args->{search} // '';

    unless ($query) {
        $callback->({ items => [] });
        return;
    }

    my @type_menus = map {
        my ($key, $label) = @$_;
        {
            name        => cstring($client, $label),
            url         => \&_search_results,
            passthrough => [{ query => $query, type => $key }],
        }
    } (
        [ songs     => 'PLUGIN_YOUTUBEMUSIC_SONGS'     ],
        [ albums    => 'PLUGIN_YOUTUBEMUSIC_ALBUMS'    ],
        [ artists   => 'PLUGIN_YOUTUBEMUSIC_ARTISTS'   ],
        [ playlists => 'PLUGIN_YOUTUBEMUSIC_PLAYLISTS' ],
    );

    $callback->({ items => \@type_menus });
}

sub _search_results {
    my ($client, $callback, $args, $params) = @_;

    Plugins::YouTubeMusic::API->search(
        $params->{query},
        $params->{type},
        sub {
            my $results = shift;
            unless ($results && ref $results eq 'ARRAY') {
                return $callback->({ items => [], error => 'Search failed' });
            }
            $callback->({ items => _items_to_menu($client, $results) });
        }
    );
}

sub _home_menu {
    my ($client, $callback) = @_;

    Plugins::YouTubeMusic::API->browseHome(sub {
        my $sections = shift;
        unless ($sections && ref $sections eq 'ARRAY') {
            return $callback->({ items => [] });
        }

        my @items = map {
            my $section = $_;
            {
                name => $section->{title} || cstring($client, 'PLUGIN_YOUTUBEMUSIC_HOME'),
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items} // [], { playall => 1 }), playall => 1 });
                },
            }
        } @$sections;

        $callback->({ items => \@items });
    });
}

sub _charts_menu {
    my ($client, $callback) = @_;

    Plugins::YouTubeMusic::API->browseCharts(sub {
        my $sections = shift;
        unless ($sections && ref $sections eq 'ARRAY') {
            return $callback->({ items => [] });
        }

        my @items = map {
            my $section = $_;
            {
                name => $section->{title} || cstring($client, 'PLUGIN_YOUTUBEMUSIC_CHARTS'),
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items} // [], { playall => 1 }), playall => 1 });
                },
            }
        } @$sections;

        $callback->({ items => \@items });
    });
}

sub _my_playlists_menu {
    my ($client, $callback) = @_;

    my $saved = $prefs->get('my_playlists') || [];
    $saved = [$saved] unless ref $saved eq 'ARRAY';

    my @items;

    # Saved playlist entries
    for my $entry (@$saved) {
        my ($name, $browse_id) = split /\|/, $entry, 2;
        next unless $name && $browse_id;
        push @items, {
            name        => $name,
            url         => \&_playlist_menu,
            play        => "ytmplaylist://$browse_id",
            playlist    => "ytmplaylist://$browse_id",
            type        => 'playlist',
            passthrough => [{ browseId => $browse_id, browse_type => 'playlist' }],
        };
    }

    # Always show an "Add Current Playlist" hint and management options
    push @items, {
        name => '+ Save a playlist (see Settings)',
        type => 'text',
    };

    $callback->({ items => \@items });
}

sub _new_releases_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->browseNewReleases(sub {
        my $sections = shift;
        unless ($sections && ref $sections eq 'ARRAY') {
            return $callback->({ items => [] });
        }
        my @items = map {
            my $section = $_;
            {
                name => $section->{title} || cstring($client, 'PLUGIN_YOUTUBEMUSIC_NEW_RELEASES'),
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items} // [], { playall => 1 }), playall => 1 });
                },
            }
        } @$sections;
        $callback->({ items => \@items });
    });
}

sub _moods_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->browseMoods(sub {
        my $sections = shift;
        unless ($sections && ref $sections eq 'ARRAY') {
            return $callback->({ items => [] });
        }
        my @items;
        for my $section (@$sections) {
            for my $item (@{ $section->{items} // [] }) {
                next unless $item->{browseId};
                my $bid    = $item->{browseId};
                my $params = $item->{params} // '';
                push @items, {
                    name => $item->{title} || 'Unknown',
                    url  => sub {
                        my ($c, $cb) = @_;
                        Plugins::YouTubeMusic::API->browseMoodCategory($bid, $params, sub {
                            my $cat_sections = shift;
                            my @cat_items;
                            for my $cat_section (@{ $cat_sections // [] }) {
                                push @cat_items, @{ _items_to_menu($c, $cat_section->{items} // []) };
                            }
                            $cb->({ items => \@cat_items });
                        });
                    },
                };
            }
        }
        $callback->({ items => \@items });
    });
}

sub _podcasts_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->browsePodcasts(sub {
        my $sections = shift;
        unless ($sections && ref $sections eq 'ARRAY') {
            return $callback->({ items => [] });
        }
        my @items = map {
            my $section = $_;
            {
                name => $section->{title} || cstring($client, 'PLUGIN_YOUTUBEMUSIC_PODCASTS'),
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items} // [], { playall => 1 }), playall => 1 });
                },
            }
        } @$sections;
        $callback->({ items => \@items });
    });
}

sub _artist_menu {
    my ($client, $callback, $args, $params) = @_;

    Plugins::YouTubeMusic::API->browseArtist($params->{browseId}, sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }

        my @items = map {
            my $section = $_;
            {
                name => $section->{title} || 'Tracks',
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items} // [], { playall => 1 }), playall => 1 });
                },
            }
        } @{ $data->{sections} // [] };

        $callback->({ items => \@items });
    });
}

sub _playlist_menu {
    my ($client, $callback, $args, $params) = @_;
my $type = $params->{browse_type} // 'playlist';

    my $api_method = ($type eq 'album') ? 'browseAlbum' : 'browsePlaylist';

    Plugins::YouTubeMusic::API->$api_method($params->{browseId}, sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }
        # playall => 1 tells LMS to queue all tracks from the selected position
        # forward when tapped — prevents radio triggering on single track selection.
        my $album_title = $params->{album_title} // '';
        my $items = $data->{items} // [];
        if ($album_title) {
            # Inject album title into each track so it shows correctly in queue
            for my $item (@$items) {
                $item->{album} = $album_title if $item->{type} && $item->{type} eq 'song';
            }
        }
        # Prefetch track 1 immediately so it is ready when the user presses
        # Play — eliminates the 20-second yt-dlp resolution delay on first play.
        my $first_vid = (grep { $_->{videoId} } @$items)[0]->{videoId} if @$items;
        Plugins::YouTubeMusic::API->prefetch($first_vid, sub {}) if $first_vid;

        $callback->({ items => _items_to_menu($client, $items, { playall => 1 }), playall => 1 });
    });
}

sub _items_to_menu {
    my ($client, $items, $opts) = @_;
    my $playall = ($opts && $opts->{playall}) ? 1 : 0;
    my @menu;

    for my $item (@{ $items // [] }) {
        my $type = $item->{type} // '';

        if ($type eq 'song' && $item->{videoId}) {
            my $ytm_url = "ytm://$item->{videoId}";
            Plugins::YouTubeMusic::ProtocolHandler->primeMetadata($item->{videoId}, $item);
            push @menu, {
                name      => $item->{title}  || 'Unknown',
                line2     => _song_line2($item),
                url       => $ytm_url,
                image     => $item->{thumbnail} || '',
                play      => $ytm_url,
                type      => 'audio',
                on_select => 'play',
                $playall ? ( playall => 1 ) : (),
            };
        }
        elsif ($type eq 'album' && $item->{browseId}) {
            push @menu, {
                name        => $item->{title}  || 'Unknown Album',
                line2       => join(' - ', grep { $_ } $item->{artist}, $item->{year}),
                image       => $item->{thumbnail} || '',
                url         => \&_playlist_menu,
                play        => "ytmplaylist://$item->{browseId}",
                type        => 'playlist',
                passthrough => [{ browseId => $item->{browseId}, browse_type => 'album', album_title => $item->{title} }],
            };
        }
        elsif ($type eq 'artist' && $item->{browseId}) {
            push @menu, {
                name        => $item->{name}   || 'Unknown Artist',
                image       => $item->{thumbnail} || '',
                url         => \&_artist_menu,
                passthrough => [{ browseId => $item->{browseId} }],
            };
        }
        elsif ($type eq 'playlist' && $item->{browseId}) {
            push @menu, {
                name        => $item->{title}  || 'Unknown Playlist',
                line2       => $item->{count}  || '',
                image       => $item->{thumbnail} || '',
                url         => \&_playlist_menu,
                play        => "ytmplaylist://$item->{browseId}",
                type        => 'playlist',
                passthrough => [{ browseId => $item->{browseId}, browse_type => 'playlist' }],
            };
        }
        elsif ($item->{browseId}) {
            my $btype = $item->{type} // 'playlist';
            my %entry = (
                name        => $item->{title}    || $item->{name} || 'Unknown',
                line2       => $item->{subtitle} || '',
                image       => $item->{thumbnail} || '',
                url         => ($btype eq 'artist') ? \&_artist_menu : \&_playlist_menu,
                passthrough => [{ browseId => $item->{browseId}, browse_type => $btype }],
            );
            unless ($btype eq 'artist') {
                $entry{play} = "ytmplaylist://$item->{browseId}";
                $entry{type} = 'playlist';
            }
            push @menu, \%entry;
        }
        elsif ($item->{videoId}) {
            my $ytm_url = "ytm://$item->{videoId}";
            Plugins::YouTubeMusic::ProtocolHandler->primeMetadata($item->{videoId}, $item);
            push @menu, {
                name      => $item->{title}    || 'Unknown',
                line2     => $item->{subtitle} || '',
                url       => $ytm_url,
                image     => $item->{thumbnail} || '',
                play      => $ytm_url,
                type      => 'audio',
                on_select => 'play',
                $playall ? ( playall => 1 ) : (),
            };
        }
    }

    return \@menu;
}

sub _song_line2 {
    my $item = shift;
    return join(' - ', grep { $_ }
        $item->{artist}   || '',
        $item->{album}    || '',
        $item->{duration} || '',
    );
}

1;
