// Execute the real fetchImages closure with a minimal Vue-independent harness.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../pages/dashboard/app.js'), 'utf8');
const start = source.indexOf('        let imageRequestId = 0;');
const end = source.indexOf('        const fetchEmotions =', start);
assert.ok(start > 0 && end > start, 'Gallery request implementation not found');

function harness() {
    const requests = [];
    const apiFetch = (url) => new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
    const create = new Function('apiFetch', `
        const ref = value => ({ value });
        const loading = ref(false), pageSize = ref(24), searchQuery = ref('');
        const selectedCategory = ref('happy'), sortBy = ref('newest'), selectedCharacter = ref('');
        const currentPage = ref(1), images = ref([]), total = ref(0), categories = ref([]), characters = ref([]);
        const unassignedCharacterCount = ref(0), favoriteCount = ref(0), selectedImages = ref(new Set());
        const imageDataUrls = {}, originalDataUrls = {};
        const normalizeCategories = x => x || [], nextTick = () => {}, observeImages = () => {};
        ${source.slice(start, end)}
        return { fetchImages, selectedCategory, images, total, loading, currentPage };
    `);
    return { ...create(apiFetch), requests };
}

const complete = (request, id, total = 1) => request.resolve({ json: async () => ({ images: [{ hash: id }], total }) });

async function main() {
    // Both response orders must preserve the latest selection and loading flag.
    for (const newerFirst of [true, false]) {
        const h = harness();
        const old = h.fetchImages();
        h.selectedCategory.value = 'sad';
        const latest = h.fetchImages();
        assert.equal(h.requests.length, 2, 'Latest request must not be dropped');
        assert.ok(h.requests[1].url.includes('category=sad'));
        if (newerFirst) {
            complete(h.requests[1], 'new'); await latest;
            complete(h.requests[0], 'old'); await old;
        } else {
            complete(h.requests[0], 'old'); await old;
            assert.equal(h.loading.value, true);
            assert.deepEqual(h.images.value, []);
            complete(h.requests[1], 'new'); await latest;
        }
        assert.equal(h.images.value[0].hash, 'new');
        assert.equal(h.loading.value, false);
    }

    // Page correction is awaited and an old completion cannot stop its spinner.
    const h = harness();
    const pending = h.fetchImages(4);
    complete(h.requests[0], 'unused', 25);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.requests.length, 2);
    assert.ok(h.requests[1].url.includes('page=2'));
    assert.equal(h.loading.value, true);
    complete(h.requests[1], 'last-page', 25);
    await pending;
    assert.equal(h.currentPage.value, 2);
    assert.equal(h.loading.value, false);
    console.log('Gallery request race checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
