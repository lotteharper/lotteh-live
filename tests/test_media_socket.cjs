const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function socketScript(templateName) {
    const template = fs.readFileSync(templateName, 'utf8');
    const start = template.indexOf('var mediaSocket;');
    const end = template.indexOf("var form = document.getElementById('live-form');", start);
    return template.slice(start, end)
        .replace(/{% if use_websocket %}|{% endif %}/g, '')
        .replace(/{{ reload_time\|default:10000 }}/g, '10')
        .replace(/{{[^}]+}}/g, 'camera');
}

test('screencast posts segments over HTTP and reports upload failures', async () => {
    const template = fs.readFileSync('screencast.html', 'utf8');
    let script = template.slice(template.indexOf('let uploadQueue = Promise.resolve();'),
        template.indexOf('function stopRecordingFn()', template.indexOf('let uploadQueue = Promise.resolve();')));
    script = script.replace("{% if should_compress_live %}'zip'{% else %}extension{% endif %}", 'extension')
        .replace(/{% if should_compress_live %}[\s\S]*?{% endif %}/g, '')
        .replace(/{% if use_websocket %}[\s\S]*?{% else %}/, '')
        .replace(/{% endif %}/g, '');
    const messages = [];
    let stopped = false;
    const context = vm.createContext({
        Blob, FormData, Date, console,
        recordedChunks: [new Blob(['video'])], mimeType: 'video/webm', CAMERA_ID: 'stream1',
        crypto: {randomUUID: () => 'segment1'}, form: {action: '/screencast/?camera=private'},
        fetch: async (url, options) => {
            assert.equal(url, '/screencast/?camera=private');
            assert.equal(options.body.get('viduid'), 'stream1');
            assert.equal(options.body.get('confirmation_id'), 'segment1');
            assert.equal(await options.body.get('frame').text(), 'video');
            return {ok: false, status: 500};
        },
        document: {getElementById: () => ({
            set textContent(value) { messages.push(value); },
            classList: {add() {}, remove() {}},
        })},
        stopRecordingFn: () => { stopped = true; },
    });
    vm.runInContext(script, context);
    context.download();
    await vm.runInContext('uploadQueue', context);
    assert.equal(stopped, true);
    assert.match(messages[0], /Video upload failed: 500/);
});

function setup(templateName) {
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
    vm.runInContext(socketScript(templateName), context);
    return {context, sockets};
}

for (const templateName of ['golivevideo.html', 'screencast.html']) {
test(`${templateName}: waits for a reconnect before sending a segment`, async () => {
    const {context, sockets} = setup(templateName);
    sockets[0].close(1006);
    const pending = context.waitForMediaSocket();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(sockets.length, 2);
    sockets[1].open();
    assert.equal(await pending, sockets[1]);
});

test(`${templateName}: does not reconnect after an authorization rejection`, async () => {
    const {context, sockets} = setup(templateName);
    sockets[0].close(4403);
    await assert.rejects(context.waitForMediaSocket(), /not authorized/);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(sockets.length, 1);
});

test(`${templateName}: uploads large video in separate bounded binary messages`, async () => {
    const template = fs.readFileSync(templateName, 'utf8');
    const end = templateName === 'screencast.html' ? 'function stopRecordingFn()' : "{% include 'live/remote.js' %}";
    let script = template.slice(template.indexOf('let uploadQueue = Promise.resolve();'),
        template.indexOf(end, template.indexOf('let uploadQueue = Promise.resolve();')));
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
}
