const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function main() {
    const source = fs.readFileSync(path.join(__dirname, '../../pages/dashboard/archive-upload.js'), 'utf8');
    const { uploadArchive } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
    const bytes = Buffer.alloc(1200000, 42);
    const file = new File([bytes], 'backup.zip');
    const chunks = [], progress = [];
    let received = 0, lostResponse = true, lostFinishResponse = true;
    const bridge = { async apiPost(endpoint, payload) {
        assert.equal(endpoint, 'sources/upload/chunked');
        assert.ok(Buffer.byteLength(JSON.stringify(payload)) < 1024 * 1024);
        if (payload.action === 'start') return { success: true, upload_id: 'id', chunk_size: 524288 };
        if (payload.action === 'chunk') {
            if (payload.offset === received) {
                const chunk = Buffer.from(payload.data, 'base64');
                received += chunk.length; chunks.push(chunk);
            }
            if (lostResponse) { lostResponse = false; throw new Error('Network error'); }
            return { success: true, received };
        }
        assert.equal(payload.action, 'finish');
        if (lostFinishResponse) { lostFinishResponse = false; throw new Error('Network error'); }
        return { success: true, path: '/staged/backup.zip' };
    } };
    const encode = async blob => 'data:application/octet-stream;base64,' + Buffer.from(await blob.arrayBuffer()).toString('base64');
    assert.equal(await uploadArchive(bridge, file, encode, n => progress.push(n)), '/staged/backup.zip');
    assert.deepEqual(Buffer.concat(chunks), bytes);
    assert.equal(progress.at(-1), 100);
    assert.ok(progress.length > 2);
    let cancelled = false;
    bridge.apiPost = async (_, payload) => {
        if (payload.action === 'start') return { success: true, upload_id: 'id', chunk_size: 524288 };
        if (payload.action === 'cancel') { cancelled = true; return { success: true }; }
        return { success: false, error: 'disk full' };
    };
    await assert.rejects(uploadArchive(bridge, file, encode), /disk full/);
    assert.equal(cancelled, true);
    console.log('Archive upload: bounded requests, exact bytes, retry, progress and failure cleanup passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
