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
use Slim::Menu::TrackInfo;
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
        proxy_port      => 9876,
        my_playlists    => [],
        recent_searches => [],
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

    # Track menu for songs in the plugin's own lists (see _song_actions). One
    # word: XMLBrowser only accepts single-verb queries.
    Slim::Control::Request::addDispatch(
        ['youtubemusictrackinfo', 'items', '_index', '_quantity'],
        [1, 1, 1, \&_cliTrackInfo]
    );
    Slim::Control::Request::addDispatch(
        ['youtubemusictrackinfo', 'playlist', '_method'],
        [1, 1, 1, \&_cliTrackInfo]
    );

    # Delete entries from the recent-searches list (info menu on players/apps)
    Slim::Control::Request::addDispatch(
        ['youtubemusic', 'recentsearches'],
        [0, 0, 1, \&_cliRecentSearches]
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

    my $python_override = $prefs->get('path_python') || '';
    my $python = ($python_override && -f $python_override) ? $python_override : _find_python();
    if ($python_override && !-f $python_override) {
        $log->warn("Python override path not found: $python_override, falling back to auto-detect");
    }
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
        my @win_args;
        push @win_args, '--ytdlp',    $prefs->get('path_ytdlp')  if $prefs->get('path_ytdlp');
        push @win_args, '--ffmpeg',   $prefs->get('path_ffmpeg') if $prefs->get('path_ffmpeg');
        push @win_args, '--node',     $prefs->get('path_node')   if $prefs->get('path_node');
        push @win_args, '--log-file',        $prefs->get('log_path')         if $prefs->get('log_path');
        push @win_args, '--no-node-worker'                                          if $prefs->get('disable_node_worker');
        push @win_args, '--ytm-cookie', $prefs->get('ytm_cookie')  if $prefs->get('ytm_cookie');
        my $pid = system(1, $python, $script, '--port', $port, '--log-level', 'WARNING', '--codec', $codec, '--bind', '0.0.0.0', @win_args);
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
            my @extra_args;
            push @extra_args, '--ytdlp',    $prefs->get('path_ytdlp')  if $prefs->get('path_ytdlp');
            push @extra_args, '--ffmpeg',   $prefs->get('path_ffmpeg') if $prefs->get('path_ffmpeg');
            push @extra_args, '--node',     $prefs->get('path_node')   if $prefs->get('path_node');
            push @extra_args, '--log-file',        $prefs->get('log_path')         if $prefs->get('log_path');
            push @extra_args, '--no-node-worker'                                          if $prefs->get('disable_node_worker');
            push @extra_args, '--ytm-cookie', $prefs->get('ytm_cookie')  if $prefs->get('ytm_cookie');
            exec($python, $script, '--port', $port, '--log-level', 'WARNING', '--codec', $codec, @extra_args) or do {
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

# The settings page reaches the proxy only through these LMS routes (so they
# sit behind LMS's own web access control); the proxy itself listens on
# loopback only. Downloads and updates can take minutes, hence the timeouts.
my %_proxy_endpoint_timeout = (
    ytdlp_status => 35, ffmpeg_status => 35, codec => 35, ping => 35, paths => 35,
    download_ytdlp => 600, update_ytdlp => 600, download_ffmpeg => 600,
);

sub _register_proxy_handlers {
    for my $ep (sort keys %_proxy_endpoint_timeout) {
        my $endpoint = $ep;
        Slim::Web::Pages->addRawFunction(
            "plugins/YouTubeMusic/proxy/$endpoint",
            sub {
                my ($httpClient, $response) = @_;
                my $port  = $prefs->get('proxy_port') || 9876;
                my $query = eval { $response->request->uri->query } // '';
                Slim::Networking::SimpleAsyncHTTP->new(
                    sub {
                        my $http = shift;
                        $response->code(200);
                        $response->header('Content-Type', 'application/json');
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
                    { timeout => $_proxy_endpoint_timeout{$endpoint} }
                )->get("http://127.0.0.1:$port/$endpoint" . ($query ne '' ? "?$query" : ''));
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

    # "Go to artist" and "Go to album" in the track menu (long press / More /
    # info) of every YouTube Music track, wherever it is listed - and in the
    # queue.
    Slim::Menu::TrackInfo->registerInfoProvider( youtubemusic_artist => (
        after => 'playitem',
        func  => \&_trackinfo_artist,
    ));
    Slim::Menu::TrackInfo->registerInfoProvider( youtubemusic_album => (
        after => 'youtubemusic_artist',
        func  => \&_trackinfo_album,
    ));

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

    my $is_playall = ($method eq 'playall');
    $method = 'play' if $is_playall;
    $log->info("_cliPlaylistCmd: method=$method item_id=$item_id touchToPlay=$touch is_playall=$is_playall");
    # Only handle touchToPlay or playall requests from Jivelite/SB Radio/SqueezePlay
    # For normal Material skin play commands, delegate to XMLBrowser
    unless ($touch || $is_playall) {
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

    # Walk the menu along the whole path - Speed dial, My Library, artist
    # discographies etc. are three or four levels deep, and stopping after two
    # levels (as before) left nothing to play.
    # Search text the app typed into a search box on the way down; LMS passes
    # it to search-type levels the same way (Slim::Control::XMLBrowser).
    my $search = $request->getParam('search');

    _top_level($client, sub {
        _walk_menu($client, shift, [@path], $search, sub {
            my ($item, $siblings) = @_;
            unless ($item) {
                $request->setStatusDone();
                return;
            }
            _play_menu_item($client, $method, $item, $siblings, sub { $request->setStatusDone() });
        });
    }, {});

    $request->setStatusProcessing();
}

# Follow @$path (item indexes) through the feed, opening each menu level via
# its url callback. Calls $done->($item, $siblings) with the target item and
# the list it is in, or $done->() if the path doesn't resolve.
sub _walk_menu {
    my ($client, $feed, $path, $search, $done) = @_;
    my $items = ref $feed eq 'HASH' ? $feed->{items} : $feed;
    unless ($items && ref $items eq 'ARRAY') {
        $log->warn("_cliPlaylistCmd: menu level has no items");
        return $done->();
    }
    my $idx  = shift @$path;
    my $item = defined $idx ? $items->[$idx] : undef;
    unless ($item) {
        $log->warn("_cliPlaylistCmd: no item at index " . ($idx // '?'));
        return $done->();
    }
    $log->info("_cliPlaylistCmd: item=" . ($item->{name} || 'unknown'));

    # Target reached, or nothing deeper to open
    return $done->($item, $items) unless @$path && ref $item->{url} eq 'CODE';

    my %args = (defined $search && ($item->{type} // '') eq 'search') ? (search => $search) : ();
    $item->{url}->($client, sub { _walk_menu($client, shift, $path, $search, $done) },
                   \%args, @{ $item->{passthrough} || [{}] });
}

# Play/add a menu item the way LMS does for a tap: a track inside a list
# plays the list's tracks starting at that one; an album/playlist is
# expanded; anything else is handed to the playlist command as is.
sub _play_menu_item {
    my ($client, $method, $item, $siblings, $done) = @_;
    my $play = $item->{play} // $item->{url};
    $play = undef if ref $play;

    if ($play && $play =~ m{^ytmplaylist://}) {
        $log->info("_cliPlaylistCmd: expanding $play");
        Plugins::YouTubeMusic::PlaylistProtocolHandler->explodePlaylist($client, $play, sub {
            my $urls = shift;
            if ($urls && @$urls) {
                if ($method eq 'play') {
                    $client->execute(['playlist', 'loadtracks', 'listref', $urls]);
                    $client->execute(['play']);
                } else {
                    $client->execute(['playlist', 'addtracks', 'listref', $urls]);
                }
            }
            $done->();
        });
        return;
    }

    if ($play && $play =~ m{^ytm://} && $method eq 'play') {
        my @tracks = grep { !ref($_->{play}) && ($_->{play} // '') =~ m{^ytm://} } @{ $siblings || [] };
        my ($start) = grep { $tracks[$_] == $item } 0 .. $#tracks;
        if (@tracks > 1 && defined $start) {
            $log->info("_cliPlaylistCmd: playing " . scalar(@tracks) . " tracks from #$start");
            $client->execute(['playlist', 'loadtracks', 'listref', [ map { $_->{play} } @tracks ], undef, $start]);
            $client->execute(['play']);
            return $done->();
        }
    }

    if ($play) {
        $log->info("_cliPlaylistCmd: $method $play");
        $client->execute(['playlist', $method, $play]);
    } else {
        $log->warn("_cliPlaylistCmd: item has nothing to play");
    }
    $done->();
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

    my $signed_in = $prefs->get('ytm_cookie') ? 1 : 0;

    # Personal entries first, then discovery - the order of the apps.
    my @items = (
        {
            # With a search history, Search opens "New search" plus the
            # previous searches; without one it is the search box itself.
            name   => cstring($client, 'PLUGIN_YOUTUBEMUSIC_SEARCH'),
            @{ $prefs->get('recent_searches') || [] }
                ? ( url => \&_recent_searches_menu, type => 'link' )
                : ( url => \&_search_dispatch, type => 'search', search => '' ),
        },
        ( $signed_in ? (
            {
                name => 'Speed dial',
                url  => \&_speed_dial_menu,
            },
            {
                name => 'My Library',
                url  => \&_my_library_menu,
            },
        ) : () ),
        # Hand-saved playlists (settings page); hidden until one is saved.
        # Signed-in users see their account's playlists under My Library.
        ( _saved_playlists() ? {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_MY_PLAYLISTS'),
            url   => \&_my_playlists_menu,
        } : () ),
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_HOME'),
            url   => \&_home_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_NEW_RELEASES'),
            url   => \&_new_releases_menu,
        },
        {
            name  => cstring($client, 'PLUGIN_YOUTUBEMUSIC_CHARTS'),
            url   => \&_charts_menu,
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

use constant MAX_RECENT_SEARCHES => 50;

# Previous searches, newest first, with "New search" on top.
sub _recent_searches_menu {
    my ($client, $callback) = @_;

    my $i = 0;
    my @items = map {
        {
            name        => $_,
            type        => 'link',
            url         => \&_search_dispatch,
            passthrough => [{ query => $_ }],
            itemActions => {
                info => {
                    command     => ['youtubemusic', 'recentsearches'],
                    fixedParams => { deleteMenu => $i++ },
                },
            },
        }
    } @{ $prefs->get('recent_searches') || [] };

    unshift @items, {
        name   => cstring($client, 'PLUGIN_YOUTUBEMUSIC_NEW_SEARCH'),
        url    => \&_search_dispatch,
        type   => 'search',
        search => '',
    };

    $callback->({ items => \@items });
}

# New searches go on top. A search already in the history stays where it is:
# LMS doesn't cache these menus (they are built by code), so it rebuilds the
# path by position on every step, and moving entries would make a stale list
# in an app open the neighbouring search.
sub _add_recent_search {
    my $query = shift;
    return unless defined $query && $query =~ /\S/;
    my @list = @{ $prefs->get('recent_searches') || [] };
    return if grep { lc($_) eq lc($query) } @list;
    unshift @list, $query;
    splice(@list, MAX_RECENT_SEARCHES) if @list > MAX_RECENT_SEARCHES;
    $prefs->set('recent_searches', \@list);
}

# youtubemusic recentsearches deleteMenu:<i> | delete:<i> | deleteAll:1
sub _cliRecentSearches {
    my $request = shift;
    my $client  = $request->client;

    if ($request->isNotCommand([['youtubemusic'], ['recentsearches']])) {
        $request->setStatusBadDispatch();
        return;
    }

    my $list = $prefs->get('recent_searches') || [];

    if ($request->getParam('deleteAll')) {
        $prefs->set('recent_searches', []);
        $request->setStatusDone();
        return;
    }

    my $idx = $request->getParam('deleteMenu') // $request->getParam('delete');
    if (!defined $idx || $idx !~ /^\d+$/ || $idx >= @$list) {
        $request->setStatusBadParams();
        return;
    }

    if (defined $request->getParam('deleteMenu')) {
        my @items = ({
            text    => cstring($client, 'DELETE') . cstring($client, 'COLON') . ' "' . $list->[$idx] . '"',
            actions => { go => { player => 0, cmd => ['youtubemusic', 'recentsearches'], params => { delete => $idx } } },
            nextWindow => 'parent',
        }, {
            text    => cstring($client, 'PLUGIN_YOUTUBEMUSIC_CLEAR_SEARCH_HISTORY'),
            actions => { go => { player => 0, cmd => ['youtubemusic', 'recentsearches'], params => { deleteAll => 1 } } },
            nextWindow => 'grandParent',
        });
        $request->addResult('offset', 0);
        $request->addResult('count', scalar @items);
        $request->addResult('item_loop', \@items);
    }
    else {
        my @copy = @$list;
        splice(@copy, $idx, 1);
        $prefs->set('recent_searches', \@copy);
    }

    $request->setStatusDone();
}

sub _search_dispatch {
    my ($client, $callback, $args, $params) = @_;

    my $typed = $args->{search} // '';
    my $query = $typed || ($params && $params->{query}) || '';

    unless ($query) {
        $callback->({ items => [] });
        return;
    }

    # Only newly typed searches change the history. Re-running one from the
    # history must not move it: LMS menus navigate by position and re-fetch
    # the list on each step, so reordering it would open another search's
    # results (as Spotty also avoids).
    _add_recent_search($query) if $typed;

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

    # Opened from the history: offer removing it, last so the positions of
    # the result menus above don't change. Removes by text, not position.
    push @type_menus, {
        name => cstring($client, 'PLUGIN_YOUTUBEMUSIC_REMOVE_SEARCH'),
        url  => sub {
            my ($c, $cb) = @_;
            _remove_recent_search($query);
            $cb->({ items => [{
                name => sprintf(cstring($c, 'PLUGIN_YOUTUBEMUSIC_SEARCH_REMOVED'), $query),
                type => 'text',
            }] });
        },
    } unless $typed;

    $callback->({ items => \@type_menus });
}

sub _remove_recent_search {
    my $query = shift;
    $prefs->set('recent_searches',
        [ grep { lc($_) ne lc($query) } @{ $prefs->get('recent_searches') || [] } ]);
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

# [name, browseId] pairs of the playlists saved on the settings page.
sub _saved_playlists {
    my $saved = $prefs->get('my_playlists') || [];
    $saved = [$saved] unless ref $saved eq 'ARRAY';
    return grep { $_->[0] && $_->[1] } map { [ split /\|/, ($_ // ''), 2 ] } @$saved;
}

sub _my_playlists_menu {
    my ($client, $callback) = @_;

    my @items;

    # Saved playlist entries
    for my $pair (_saved_playlists()) {
        my ($name, $browse_id) = @$pair;
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

sub _my_library_menu {
    my ($client, $callback) = @_;
    $callback->({ items => [
        {
            name => 'Liked Songs',
            url  => \&_liked_songs_menu,
        },
        {
            name => 'My Playlists',
            url  => \&_library_playlists_menu,
        },
        {
            name => 'Library Songs',
            url  => \&_library_songs_menu,
        },
        {
            name => 'History',
            url  => \&_history_menu,
        },
        {
            name => 'Albums',
            url  => \&_library_albums_menu,
        },
        {
            name => 'Artists',
            url  => \&_library_artists_menu,
        },
        {
            name => 'Home',
            url  => \&_personal_home_menu,
        },
    ]});
}

# The signed-in home page, as YouTube Music shows it: Speed dial, Listen
# again, Quick picks, mixes... Each section opens as its own list.
sub _personal_home_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryHome(sub {
        my $sections = _library_home_sections(shift);
        return $callback->($sections) if ref $sections eq 'HASH';

        $callback->({ items => [ map {
            my $section = $_;
            {
                name => $section->{title} || 'More',
                url  => sub {
                    my ($c, $cb) = @_;
                    $cb->({ items => _items_to_menu($c, $section->{items}, { playall => 1 }) });
                },
            }
        } @$sections ] });
    });
}

# Just the Speed dial section of the signed-in home page. The phone app calls
# it "Speed dial"; the web client the plugin talks to calls the same section
# "Listen again", so accept either (Speed dial first).
sub _speed_dial_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryHome(sub {
        my $sections = _library_home_sections(shift);
        return $callback->($sections) if ref $sections eq 'HASH';

        my ($dial) = (grep({ ($_->{title} // '') =~ /speed\s*dial/i } @$sections),
                      grep({ ($_->{title} // '') =~ /listen\s*again/i } @$sections));
        return $callback->({ items => [{ name => 'Speed dial is not available for this account yet', type => 'text' }] })
            unless $dial;
        $callback->({ items => _items_to_menu($client, $dial->{items}, { playall => 1 }) });
    });
}

# Returns the section list, or a ready-made menu explaining why there is none.
sub _library_home_sections {
    my $data = shift;
    return { items => [{ name => 'Could not load YouTube Music home', type => 'text' }] }
        unless $data && ref $data eq 'HASH';
    my $error = $data->{error} // '';
    return { items => [{ name => 'Not authenticated - please add your YouTube Music headers in plugin settings', type => 'text' }] }
        if $error eq 'not_authenticated';
    return { items => [{ name => "Could not load YouTube Music home: $error", type => 'text' }] }
        if $error;
    return $data->{sections} || [];
}

sub _library_albums_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryAlbums(sub {
        my $data = shift;
        return $callback->({ items => [] }) unless $data && ref $data eq 'HASH';
        $callback->({ items => _items_to_menu($client, $data->{items} // []) });
    });
}

sub _library_artists_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryArtists(sub {
        my $data = shift;
        return $callback->({ items => [] }) unless $data && ref $data eq 'HASH';
        $callback->({ items => _items_to_menu($client, $data->{items} // []) });
    });
}

sub _liked_songs_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryLiked(sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }
        if ($data->{error} && $data->{error} eq 'not_authenticated') {
            return \$callback->({ items => [{ name => 'Not authenticated - please add your YouTube Music headers in plugin settings', url => sub { \$_[1]->({ items => [] }) } }] });
        }
        $callback->({ items => _items_to_menu($client, $data->{items} // [], { playall => 1 }), playall => 1 });
    });
}

sub _library_playlists_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibraryPlaylists(sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }
        $callback->({ items => _items_to_menu($client, $data->{items} // []) });
    });
}

sub _library_songs_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getLibrarySongs(sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }
        $callback->({ items => _items_to_menu($client, $data->{items} // [], { playall => 1 }), playall => 1 });
    });
}

sub _history_menu {
    my ($client, $callback) = @_;
    Plugins::YouTubeMusic::API->getHistory(sub {
        my $data = shift;
        unless ($data && ref $data eq 'HASH') {
            return $callback->({ items => [] });
        }
        $callback->({ items => _items_to_menu($client, $data->{items} // [], { playall => 1 }), playall => 1 });
    });
}

# In the plugin's own lists LMS gives a song only a basic context menu (play,
# add, favourite, title, URL) that track info providers don't reach. Point
# "more" at the full track menu instead - the one the queue shows, with "Go to
# artist" and "Go to album". The other actions stay as they are.
sub _song_actions {
    my $url = shift;
    my ($vid) = $url =~ m{^ytm://(.+)$};
    return {
        info => {
            command     => ['youtubemusictrackinfo', 'items'],
            # The ID, not the ytm:// URL: the web UI puts this unescaped into
            # a link path, where "://" breaks it.
            fixedParams => { videoId => $vid },
        },
    };
}

# youtubemusictrackinfo items <index> <quantity> videoId:<id> | url:ytm://<id>
# youtubemusictrackinfo playlist <method> videoId:<id> | url:ytm://<id> item_id:<n>
# The core track menu for a YouTube Music track. It needs a track object,
# and LMS keeps those for remote tracks only while something holds on to
# them (the queue does), so create one and keep the last few here.
my @_menu_tracks;

sub _cliTrackInfo {
    my $request = shift;
    my $client  = $request->client;
    # The first request has videoId (see _song_actions); the entries' own
    # actions carry url instead, the only one of the two XMLBrowser passes on.
    my $vid = $request->getParam('videoId');
    ($vid) = ($request->getParam('url') // '') =~ m{^ytm://(.+)$} unless defined $vid;
    $vid //= '';
    if ($vid !~ m{^[A-Za-z0-9_\-]+$}) {
        $request->setStatusBadParams();
        return;
    }
    my $url = "ytm://$vid";
    $request->addParam('url', $url);

    my $track = Slim::Schema->objectForUrl({ url => $url, create => 1 });
    unless (blessed($track)) {
        $request->setStatusBadParams();
        return;
    }
    @_menu_tracks = ($track, grep { $_ != $track } @_menu_tracks);
    splice(@_menu_tracks, 20) if @_menu_tracks > 20;

    my $feed = Slim::Menu::TrackInfo->menu($client, $url, $track, {
        menuMode    => $request->getParam('menu') || 0,
        menuContext => 'normal',
    });
    unless ($feed) {
        $request->setStatusBadParams();
        return;
    }
    Slim::Control::XMLBrowser::cliQuery('youtubemusictrackinfo', $feed, $request);
}

sub _trackinfo_artist {
    my ($client, $url, $track, $remoteMeta) = @_;
    my ($vid) = ($url // '') =~ m{^ytm://([A-Za-z0-9_\-]+)};
    return unless $vid;
    return {
        type        => 'link',
        name        => cstring($client, 'PLUGIN_YOUTUBEMUSIC_GO_TO_ARTIST'),
        url         => \&_song_artist_menu,
        passthrough => [{ videoId => $vid }],
        favorites   => 0,
    };
}

sub _trackinfo_album {
    my ($client, $url, $track, $remoteMeta) = @_;
    my ($vid) = ($url // '') =~ m{^ytm://([A-Za-z0-9_\-]+)};
    return unless $vid;
    return {
        type        => 'link',
        name        => cstring($client, 'PLUGIN_YOUTUBEMUSIC_GO_TO_ALBUM'),
        url         => \&_song_album_menu,
        passthrough => [{ videoId => $vid }],
        favorites   => 0,
    };
}

# Title and artist as LMS knows them, to find the current copy of a song
# whose saved video ID is dead.
sub _song_meta {
    my ($client, $vid) = @_;
    my $meta = Plugins::YouTubeMusic::ProtocolHandler->getMetadataFor($client, "ytm://$vid") || {};
    return { title => $meta->{title}, artist => $meta->{artist} };
}

sub _song_album_menu {
    my ($client, $callback, $args, $params) = @_;

    Plugins::YouTubeMusic::API->getSongLinks($params->{videoId}, _song_meta($client, $params->{videoId}), sub {
        my $album = $_[0] && ref $_[0] eq 'HASH' && $_[0]->{album};
        if (!$album || !$album->{browseId}) {
            return $callback->({ items => [{
                type => 'text',
                name => cstring($client, 'PLUGIN_YOUTUBEMUSIC_NO_ALBUM'),
            }] });
        }
        _playlist_menu($client, $callback, $args, {
            browseId    => $album->{browseId},
            browse_type => 'album',
            album_title => $album->{title},
        });
    });
}

# The song's artist page; a choice first when it has several artists.
sub _song_artist_menu {
    my ($client, $callback, $args, $params) = @_;

    Plugins::YouTubeMusic::API->getSongLinks($params->{videoId}, _song_meta($client, $params->{videoId}), sub {
        my $artists = ($_[0] && ref $_[0] eq 'HASH' && $_[0]->{artists}) || [];
        if (!@$artists) {
            return $callback->({ items => [{
                type => 'text',
                name => cstring($client, 'PLUGIN_YOUTUBEMUSIC_NO_ARTIST'),
            }] });
        }
        if (@$artists == 1) {
            return _artist_menu($client, $callback, $args, { browseId => $artists->[0]{browseId} });
        }
        $callback->({ items => [ map {
            {
                name        => $_->{name},
                url         => \&_artist_menu,
                passthrough => [{ browseId => $_->{browseId} }],
            }
        } @$artists ] });
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
                    my $show = sub {
                        $cb->({ items => _items_to_menu($c, shift // [], { playall => 1 }), playall => 1 });
                    };
                    my $more = $section->{more};
                    return $show->($section->{items}) unless $more && $more->{browseId};

                    # Albums / Singles & EPs: fetch the full list behind the
                    # section's "More" link, falling back to the preview cards.
                    Plugins::YouTubeMusic::API->browseArtistDiscography($more->{browseId}, $more->{params}, sub {
                        my $data = shift;
                        my $full = ($data && ref $data eq 'HASH') ? $data->{items} : undef;
                        $show->($full && @$full ? $full : $section->{items});
                    });
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
                itemActions => _song_actions($ytm_url),
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
                itemActions => _song_actions($ytm_url),
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
