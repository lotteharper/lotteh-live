const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function socketScript() {
    const template = fs.readFileSync('golivevideo.html', 'utf8');
    const start = template.indexOf('var mediaSocket;');
    const end = template.indexOf("var form = document.getElementById('live-form');", start);
    return template.slice(start, end)
        .replace(/{% if use_websocket %}|{% endif %}/g, '')
        .replace(/{{ reload_time\|default:10000 }}/g, '10')
        .replace(/{{[^}]+}}/g, 'camera');
}

function setup() {
    const sockets = [];
    class Socket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSED = 3;
        constructor() {
            this.readyState = Socket.CONNECTING;
            this.listeners = {};
            sockets.push(this);
        }
        addEventListener(type, handler) {
            this.listeners[type] = handler;
        }
        open() {
            this.readyState = Socket.OPEN;
            this.listeners.open({});
        }
        close(code) {
            this.readyState = Socket.CLOSED;
            this.listeners.close({code, reason: ''});
        }
    }
    const context = vm.createContext({
        WebSocket: Socket, window: {location: {protocol: 'https:', host: 'example.test'}},
        console: {log() {}, error() {}}, setTimeout, clearTimeout, Date,
    });
    vm.runInContext(socketScript(), context);
    return {context, sockets};
}

test('waits for a reconnect before sending a segment', async () => {
    const {context, sockets} = setup();
    sockets[0].close(1006);
    const pending = context.waitForMediaSocket();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(sockets.length, 2);
    sockets[1].open();
    assert.equal(await pending, sockets[1]);
});

test('does not reconnect after an authorization rejection', async () => {
    const {context, sockets} = setup();
    sockets[0].close(4403);
    await assert.rejects(context.waitForMediaSocket(), /not authorized/);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(sockets.length, 1);
});

test('uploads large video in separate bounded binary messages', async () => {
    const template = fs.readFileSync('golivevideo.html', 'utf8');
    let script = template.slice(template.indexOf('let uploadQueue = Promise.resolve();'),
        template.indexOf("{% include 'live/remote.js' %}"));
    script = script.replace(/{% if should_compress_live %}[\s\S]*?{% endif %}/g, '')
        .replace(/{% if use_websocket %}/, '')
        .replace(/{% else %}[\s\S]*?{% endif %}/, '');
    const messages = [];
    const listeners = {};
    const socket = {
        bufferedAmount: 0,
        addEventListener(type, handler) { listeners[type] = handler; },
        removeEventListener(type) { delete listeners[type]; },
        send(message) {
            messages.push(message);
            if (typeof message === 'string') {
                const data = JSON.parse(message);
                const response = data.type === 'segment_start' ? {ready: data.confirmation_id}
                    : {confirmation_id: 'segment1'};
                queueMicrotask(() => listeners.message({data: JSON.stringify(response)}));
            }
        },
    };
    const context = vm.createContext({
        Blob, FormData, Date, setTimeout, clearTimeout, console,
        recordedChunks: [new Blob([new Uint8Array(2 * 1024 * 1024 + 17)])],
        mimeType: 'video/webm', CAMERA_ID: 'stream1',
        crypto: {randomUUID: () => 'segment1'},
        waitForMediaSocket: async () => socket,
        document: {getElementById: () => ({classList: {add() {}, remove() {}}})},
        stopRecordingFn: () => { throw new Error('recording stopped'); },
    });
    vm.runInContext(script, context);
    context.download();
    await vm.runInContext('uploadQueue', context);
    const chunks = messages.filter(message => typeof message !== 'string');
    assert.ok(chunks.every(chunk => chunk.size <= 65536));
    assert.equal(chunks.reduce((sum, chunk) => sum + chunk.size, 0), 2 * 1024 * 1024 + 17);
    assert.equal(JSON.parse(messages.at(-1)).type, 'segment_end');
});
