const net = require('net');
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const SOCK_PATH = process.argv[2] || '/tmp/ytmproxy-node.sock';
const BIN_DIR = process.argv[3] || null;

if (!BIN_DIR) {
    process.stderr.write('ERROR: BIN_DIR required as argument\n');
    process.exit(1);
}

// Load lib + core from yt-dlp-ejs package
const LIB_PATH = path.join(BIN_DIR, 'yt_dlp_ejs', 'yt', 'solver', 'lib.min.js');
const CORE_PATH = path.join(BIN_DIR, 'yt_dlp_ejs', 'yt', 'solver', 'core.min.js');

if (!fs.existsSync(LIB_PATH) || !fs.existsSync(CORE_PATH)) {
    process.stderr.write(`ERROR: solver scripts not found in ${BIN_DIR}\n`);
    process.exit(1);
}

const solverCode = fs.readFileSync(LIB_PATH, 'utf8') + '\n' +
                   'Object.assign(globalThis, lib);\n' +
                   fs.readFileSync(CORE_PATH, 'utf8');

process.stderr.write(`WORKER: Loaded solver (${solverCode.length} bytes)\n`);

const playerCache = {};

function getContext(playerVersion, playerData) {
    if (playerCache[playerVersion]) {
        return playerCache[playerVersion];
    }
    const context = vm.createContext({
        console, process, Buffer,
        setTimeout, clearTimeout,
        setImmediate, clearImmediate,
    });
    vm.runInContext(solverCode, context);
    const entry = { context, playerData, warmed: false };
    playerCache[playerVersion] = entry;
    if (playerData) {
        process.stderr.write(`WORKER: Warming player ${playerVersion}...\n`);
        for (let i = 0; i < 5; i++) {
            try {
                const t = process.hrtime.bigint();
                context.jsc({
                    type: "preprocessed",
                    preprocessed_player: playerData,
                    requests: [{type: "n", challenges: ["warmup_" + i]}, {type: "sig", challenges: []}]
                });
                const ms = Number(process.hrtime.bigint() - t) / 1e6;
                process.stderr.write(`WORKER: warmup ${i+1}: ${ms.toFixed(0)}ms\n`);
            } catch(e) {}
        }
        entry.warmed = true;
        process.stderr.write(`WORKER: Player ${playerVersion} ready\n`);
    }
    return entry;
}

if (fs.existsSync(SOCK_PATH)) fs.unlinkSync(SOCK_PATH);

const server = net.createServer((socket) => {
    let buffer = '';
    socket.on('data', (data) => {
        buffer += data.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const req = JSON.parse(line);
                if (req.probe) {
                    socket.write(JSON.stringify({
                        ok: true,
                        has_player: !!playerCache[req.player_version]
                    }) + '\n');
                    continue;
                }
                const entry = getContext(req.player_version, req.player_data);
                const player = entry.playerData || req.player_data;
                const t0 = process.hrtime.bigint();
                const result = entry.context.jsc({
                    type: "preprocessed",
                    preprocessed_player: player,
                    requests: req.requests
                });
                const ms = Number(process.hrtime.bigint() - t0) / 1e6;
                process.stderr.write(`WORKER: solved in ${ms.toFixed(0)}ms\n`);
                socket.write(JSON.stringify({ok: true, result}) + '\n');
            } catch(e) {
                process.stderr.write(`WORKER ERROR: ${e.message}\n`);
                socket.write(JSON.stringify({ok: false, error: e.message}) + '\n');
            }
        }
    });
    socket.on('error', () => {});
});

server.listen(SOCK_PATH, () => {
    fs.chmodSync(SOCK_PATH, 0o777);
    process.stderr.write(`WORKER_READY sock=${SOCK_PATH}\n`);
});

process.on('SIGTERM', () => {
    server.close();
    if (fs.existsSync(SOCK_PATH)) fs.unlinkSync(SOCK_PATH);
    process.exit(0);
});
