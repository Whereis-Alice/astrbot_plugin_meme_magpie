// Small JSON requests work through AstrBot's authenticated plugin-page bridge.
export async function uploadArchive(bridge, file, toBase64, onProgress = () => {}) {
    const endpoint = 'sources/upload/chunked';
    const request = async (payload, retries = 0) => {
        let result;
        for (let attempt = 0; ; attempt++) {
            try {
                result = await bridge.apiPost(endpoint, payload);
                break;
            } catch (error) {
                if (attempt >= retries) throw error;
            }
        }
        if (!result?.success) throw new Error(result?.error || 'Archive upload failed');
        return result;
    };
    let uploadId = '';
    try {
        const started = await request({ action: 'start', filename: file.name, size: file.size });
        uploadId = started.upload_id;
        const chunkSize = Number(started.chunk_size);
        if (!uploadId || !Number.isSafeInteger(chunkSize) || chunkSize < 1 || chunkSize > 512 * 1024) {
            throw new Error('Invalid archive upload session');
        }
        onProgress(0);
        for (let offset = 0; offset < file.size; offset += chunkSize) {
            const end = Math.min(offset + chunkSize, file.size);
            const encoded = await toBase64(file.slice(offset, end));
            const data = String(encoded).split(',').pop();
            const result = await request({ action: 'chunk', upload_id: uploadId, offset, data }, 2);
            if (result.received !== end) throw new Error('Archive upload offset mismatch');
            onProgress(Math.floor(end * 100 / file.size));
        }
        const completed = await request({ action: 'finish', upload_id: uploadId }, 2);
        if (!completed.path) throw new Error('Archive upload returned no file path');
        uploadId = '';
        return completed.path;
    } finally {
        if (uploadId) {
            try { await request({ action: 'cancel', upload_id: uploadId }); } catch (_) { /* TTL cleanup */ }
        }
    }
}
