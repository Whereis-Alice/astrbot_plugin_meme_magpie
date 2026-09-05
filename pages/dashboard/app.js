import { TEMPLATE } from './template.js';
const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

const PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

function createLRUCache(maxSize) {
    const cache = new Map();
    return {
        get(key) {
            if (!cache.has(key)) return null;
            const value = cache.get(key);
            cache.delete(key);
            cache.set(key, value);
            return value;
        },
        set(key, value) {
            if (cache.has(key)) cache.delete(key);
            else if (cache.size >= maxSize) {
                const firstKey = cache.keys().next().value;
                cache.delete(firstKey);
            }
            cache.set(key, value);
        },
        has(key) { return cache.has(key); },
        clear() { cache.clear(); }
    };
}

function hashToColor(hash) {
    if (!hash) return '#1e2230';
    const num = parseInt(hash.slice(0, 6), 16) || 0;
    const h = num % 360;
    const s = 20 + (num % 15);
    const l = 15 + (num % 10);
    return `hsl(${h}, ${s}%, ${l}%)`;
}

createApp({
    setup() {
        const activeSection = ref('library');
        const sidebarOpen = ref(false);
        const images = ref([]);
        const categories = ref([]);
        const stats = reactive({ total: 0, categories: 0, today: 0, no_desc: 0 });
        const loading = ref(true);
        const searchQuery = ref('');
        const selectedCategory = ref('');
        const selectedCharacter = ref('');
        const characters = ref([]);
        const works = ref([]);
        const unassignedCharacterCount = ref(0);
        const sortBy = ref('newest');
        const currentPage = ref(1);
        const pageSize = ref(24);
        const total = ref(0);

        const pendingImages = ref([]);
        const pendingTotal = ref(0);
        const pendingCategoryTotal = ref(0);
        const pendingCategories = ref([]);
        const pendingStats = reactive({ pending: 0, capacity: 200, paused: false });
        const hudHpPct = computed(() => `${Math.max(12, Math.min(100, Number(stats.total || 0)))}%`);
        const hudApPct = computed(() => {
            const cap = Number(pendingStats.capacity || 200) || 200;
            return `${Math.max(8, Math.min(100, (Number(pendingStats.pending || 0) / cap) * 100))}%`;
        });
        const hudXpPct = computed(() => `${Math.max(8, Math.min(100, Number(stats.today || 0) * 12))}%`);
        const pendingLoading = ref(false);
        const pendingSearchQuery = ref('');
        const pendingCategory = ref('');
        const pendingCurrentPage = ref(1);
        const pendingPageSize = ref(24);
        let pendingFetchLock = false;
        const bridge = window.AstrBotPluginPage;
        const localeVersion = ref(0);

        const getLocale = () => {
            const locale = String(bridge?.getLocale?.() || bridge?.getContext?.()?.locale || 'en-US').trim();
            return locale || 'en-US';
        };

        const resolveUiLocale = () => (getLocale().toLowerCase().startsWith('zh') ? 'zh-CN' : 'en-US');

        const getByPath = (source, key) => {
            if (!source || typeof source !== 'object' || !key) return undefined;
            return String(key).split('.').reduce((current, part) => {
                if (!current || typeof current !== 'object' || !(part in current)) return undefined;
                return current[part];
            }, source);
        };

        const t = (key, fallback) => {
            localeVersion.value;
            const locale = resolveUiLocale();
            const messages = bridge?.getI18n?.() || bridge?.getContext?.()?.i18n || {};
            const bundles = [messages?.[locale], messages?.[String(locale).replace('_', '-')], messages];
            if (locale === 'zh-CN') bundles.push(messages?.['zh-CN']);
            else bundles.push(messages?.['en-US']);
            for (const bundle of bundles) {
                const value = getByPath(bundle, key);
                if (typeof value === 'string' && value) return value;
            }
            return fallback;
        };

        const updateDocumentMeta = () => {
            document.documentElement.lang = getLocale();
            document.title = t('pages.dashboard.title', 'Sticker Dashboard');
        };

        const getHealthText = (status) => {
            if (status === 'ok') return t('pages.dashboard.health.ok', 'Healthy');
            if (status === 'slow') return t('pages.dashboard.health.slow', 'Slow');
            if (status === 'error') return t('pages.dashboard.health.error', 'Error');
            return t('pages.dashboard.health.checking', 'Checking');
        };

        const updatePageSize = () => {
            const w = window.innerWidth;
            const h = window.innerHeight;
            const isMobile = w < 768;
            const gap = isMobile ? 8 : 12;
            const sidebarWidth = isMobile ? 0 : 180;
            const chromeX = isMobile ? 32 : 64;
            const availableWidth = Math.max(200, w - sidebarWidth - chromeX);

            const targetCols = isMobile
                ? (w < 400 ? 3 : 4)
                : w < 1024 ? 5 : w < 1280 ? 6 : w < 1600 ? 8 : 10;
            const minSlot = isMobile ? 80 : 100;
            const maxSlot = isMobile ? 150 : 220;
            const slot = Math.max(
                minSlot,
                Math.min(maxSlot, Math.floor((availableWidth - gap * (targetCols - 1)) / targetCols)),
            );
            document.documentElement.style.setProperty('--slot-size', `${slot}px`);

            const headerHeight = isMobile ? 100 : 72;
            const toolbarHeight = 70;
            const paginationHeight = 56;
            const availableHeight = Math.max(180, h - headerHeight - toolbarHeight - paginationHeight);
            const rows = Math.max(2, Math.floor((availableHeight + gap) / (slot + gap)));
            pageSize.value = targetCols * Math.max(2, Math.floor(rows * 0.75));
            pendingPageSize.value = targetCols * Math.max(2, rows - 1);
        };

        const thumbnailCache = createLRUCache(300);
        const inflightThumbs = new Map();

        const previewOpen = ref(false);
        const previewItem = ref(null);
        const isEditing = ref(false);
        const editForm = reactive({ category: '', tags: '', scene: '', desc: '', overlay_text: '', character: '', work: '', scope_mode: 'public' });

        // 审核区编辑弹窗（issue #87）
        const pendingEditOpen = ref(false);
        const pendingEditId = ref(null);
        const pendingEditForm = reactive({
            hash: '',
            category: '',
            scope_mode: 'public',
            desc: '',
            tagsText: '',
            scenesText: '',
            overlay_text: '',
            character: '',
            work: '',
        });

        const isBatchMode = ref(false);
        const selectedImages = ref(new Set());
        const batchMoveOpen = ref(false);
        const batchTargetCategory = ref('');
        const batchScopeOpen = ref(false);
        const batchScopeMode = ref('public');
        const batchCharacterOpen = ref(false);
        const batchTargetCharacter = ref('');
        const batchWorkOpen = ref(false);
        const batchTargetWork = ref('');
        const charactersOpen = ref(false);
        const newCharacter = reactive({ key: '', name: '' });
        const addingCharacter = ref(false);
        const deletingCharacterKey = ref('');

        const uploadOpen = ref(false);
        const uploading = ref(false);
        const uploadFile = ref(null);
        const uploadPreviewUrl = ref(null);
        const uploadError = ref(null);
        const confirmOpen = ref(false);
        const confirmMessage = ref('');
        let confirmResolve = null;
        const showConfirm = (msg) => new Promise((resolve) => {
            confirmMessage.value = msg;
            confirmOpen.value = true;
            confirmResolve = resolve;
        });
        const onConfirmYes = () => { confirmOpen.value = false; confirmResolve?.(true); };
        const onConfirmNo = () => { confirmOpen.value = false; confirmResolve?.(false); };

        const promptOpen = ref(false);
        const promptMessage = ref('');
        const promptValue = ref('');
        let promptResolve = null;
        const showPrompt = (msg, initialValue = '') => new Promise((resolve) => {
            promptMessage.value = msg;
            promptValue.value = initialValue;
            promptOpen.value = true;
            promptResolve = resolve;
        });
        const onPromptOk = () => {
            promptOpen.value = false;
            promptResolve?.(promptValue.value);
            promptResolve = null;
        };
        const onPromptCancel = () => {
            promptOpen.value = false;
            promptResolve?.(null);
            promptResolve = null;
        };
        const toastOpen = ref(false);
        const toastMessage = ref('');
        const toastType = ref('info');
        let toastTimer = null;
        const showAlert = (msg, type = 'info') => {
            toastMessage.value = msg;
            toastType.value = ['success', 'error', 'info'].includes(type) ? type : 'info';
            toastOpen.value = true;
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => { toastOpen.value = false; }, 3000);
        };
        // ── 弹窗防误关：手滑点到窗口外，不该把刚填的内容清空 ──────────────
        // 规则：窗口里一个字都没动过 → 点遮罩、按 Esc 照旧关闭（纯看看的场景不添麻烦）；
        // 已经填过东西 → 不关，只抖一下面板 + 提示走 × 或「取消」，让「丢草稿」必须是明确动作。
        const modalTouched = reactive({});
        // 程序自己写进表单的内容（AI 识别结果、拖进来的文件）不会触发 input 事件，
        // 这里额外兜底，避免识别完手一抖就白跑一趟。
        const modalDirtyExtra = {
            preview: () => isEditing.value,
            upload: () => Boolean(uploadFile.value),
            batchUpload: () => batchFiles.value.length > 0,
            pendingEdit: () => Boolean(singleReanalyze.text),
            // 「识别失败检测」里逐行手写的描述同样是草稿，没保存就不该被手滑点没
            missingDesc: () => missingDescHasDraft(),
            // 外部源窗口里挑好的压缩包、跑完的预检结果，重来一次要再等一遍下载
            source: () => Boolean(sourceFile.value || sourceInspection.value),
        };
        // 窗口里挂着后台任务的进度：此时表单已经被进度视图替掉，关掉就再也翻不回来，同样不能手滑关
        const modalBusyExtra = {
            batchUpload: () => Boolean(batchTaskId.value),
            source: () => Boolean(sourceJob.value),
        };
        const touchModal = (key) => { if (key) modalTouched[key] = true; };
        const releaseModal = (key) => { if (key) delete modalTouched[key]; };
        const isModalBusy = (key) => Boolean(modalBusyExtra[key]?.());
        const isModalDirty = (key) => (
            isModalBusy(key) || Boolean(modalTouched[key]) || Boolean(modalDirtyExtra[key]?.())
        );
        // 抖动走 Web Animations API：不碰 CSS 的 animation 属性，就不会把入场动画重新播一遍
        const nudgeModalPanel = (overlayEl) => {
            const panel = overlayEl?.querySelector?.('.modal-panel');
            if (!panel || typeof panel.animate !== 'function') return;
            if (window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return;
            panel.animate([
                { transform: 'translateX(0)' },
                { transform: 'translateX(-7px)' },
                { transform: 'translateX(6px)' },
                { transform: 'translateX(-4px)' },
                { transform: 'translateX(2px)' },
                { transform: 'translateX(0)' },
            ], { duration: 420, easing: 'ease-in-out' });
        };
        const guardedOverlayEl = (key) => (
            key ? document.querySelector(`.modal-overlay[data-modal-guard="${key}"]`) : null
        );
        const refuseModalClose = (key, overlayEl) => {
            nudgeModalPanel(overlayEl || guardedOverlayEl(key));
            const hint = isModalBusy(key)
                ? t(
                    'pages.dashboard.modal.keep_open_busy_hint',
                    '任务进度正显示在这个窗口里，点外面不会关闭；要关掉请点右上角的 ×。',
                )
                : t(
                    'pages.dashboard.modal.keep_open_hint',
                    '窗口里有你填过、还没保存的内容，点外面不会关闭；要关掉请点右上角 × 或「取消」。',
                );
            showAlert(hint, 'info');
        };
        // 在面板里按下、拖到遮罩上才松手（选中一段文字、拖着预览图平移），不该算「点了外面」
        let overlayPressStartedOnSelf = false;
        const onOverlayPointerDown = (event) => {
            overlayPressStartedOnSelf = event.target === event.currentTarget;
        };
        const onOverlayInput = (event) => { touchModal(event.currentTarget?.dataset?.modalGuard); };
        const onOverlayClick = (event, closeFn) => {
            if (event.target !== event.currentTarget) return;
            if (!overlayPressStartedOnSelf) return;
            const key = event.currentTarget?.dataset?.modalGuard;
            if (isModalDirty(key)) { refuseModalClose(key, event.currentTarget); return; }
            closeFn();
        };

        const uploadForm = reactive({ emotion: '', tags: '', scene: '', desc: '', overlay_text: '', character: '', work: '' });
        const availableEmotions = ref([]);
        const analysisScenes = ref([]);

        const batchUploadOpen = ref(false);
        const batchUploading = ref(false);
        const batchFolderMode = ref(false);
        const batchDragActive = ref(false);
        const batchFiles = ref([]);
        const batchPreviews = ref([]);
        const batchUploadError = ref(null);
        const batchUploadForm = reactive({
            emotion: '', autoAnalyze: false, character: '', work: '',
            concurrency: 2, rpm: 20,
        });
        // 服务端下发的调速默认值 / 上限，用于预填与校验输入框
        const batchDefaults = ref({ concurrency: 2, rpm: 20, max_concurrency: 16, max_retries: 3 });
        // 批量弹窗的两种模式：upload = 批量导入，reanalyze = 对已入库表情重跑识别
        const batchMode = ref('upload');
        const reanalyzeForm = reactive({
            target: 'missing', overwrite: false, limit: 0,
            concurrency: 2, rpm: 20,
            // library = 已入库表情包，pending = 待审核池，两边共用同一个弹窗
            scope: 'library',
        });
        const reanalyzeScan = ref({
            total: 0, missing: 0, no_desc: 0,
            pending_total: 0, pending_missing: 0, pending_no_desc: 0,
            max_items: 5000,
        });
        const reanalyzeScanning = ref(false);
        // 用户手动点过档位后就不再自动兜底，免得抢走他的选择
        const reanalyzeTargetTouched = ref(false);
        const reanalyzeSwitchFrom = ref('');
        const reanalyzeSwitchTo = ref('');
        const reanalyzeScanFailed = ref(false);
        // ── 识别失败检测 ───────────────────────────────────────────────
        // 列表里显示「暂无描述」的条目，等于识别那一步没成功。描述是语义检索的主要
        // 依据，空着基本等于这张图搜不出来，所以单独开一个窗口集中处理。
        const missingDescOpen = ref(false);
        const missingDescScope = ref('library');
        const missingDescLoading = ref(false);
        const missingDescError = ref(null);
        const missingDescItems = ref([]);
        const missingDescTotal = ref(0);
        // scanned / maxItems 只用于把「这次只检查了多少条」说清楚
        const missingDescScanned = ref(0);
        const missingDescMaxItems = ref(5000);
        const missingDescTruncated = ref(false);
        const missingDescPage = ref(1);
        const missingDescPageSize = 24;
        // 两个作用范围各自的待补张数，用于标签页角标与底部按钮
        const missingDescCounts = reactive({ library: 0, pending: 0 });
        const missingDescDrafts = reactive({});
        const missingDescSaving = reactive({});
        const missingDescDone = reactive({});
        // 本次窗口内已写库的张数，按作用范围分开记：关窗时据此决定刷新哪边的列表
        const missingDescFixed = reactive({ library: 0, pending: 0 });
        const missingDescHasDraft = () => Object.entries(missingDescDrafts).some(
            ([key, value]) => !missingDescDone[key] && String(value || '').trim(),
        );
        const batchTaskId = ref(null);
        const batchTaskStatus = ref(null);
        const batchTaskTotal = ref(0);
        const batchTaskProcessed = ref(0);
        const batchTaskSuccess = ref(0);
        const batchTaskFailed = ref(0);
        const batchTaskAnalyzed = ref(0);
        const batchTaskCurrentFile = ref('');
        const batchTaskPhase = ref('');
        const batchTaskPaused = ref(false);
        const batchTaskCancelRequested = ref(false);
        const batchTaskAutoAnalyze = ref(false);
        const batchTaskEta = ref(0);
        const batchTaskRateLimited = ref(0);
        const batchTaskRetried = ref(0);
        const batchTaskConcurrency = ref(0);
        const batchTaskRpm = ref(0);
        const batchTaskResults = ref([]);
        const batchControlBusy = ref(false);
        let batchPollInterval = null;
        let imgObserver = null;

        // ── 外部表情包源：本地压缩包 / GitHub 仓库 / HTTP 目录 ──────────────
        const sourceOpen = ref(false);
        const sourceLoading = ref(false);
        // 当前在跑的动作，用来禁按钮：'' | 'upload' | 'inspect' | 'import' | 'control'
        const sourceBusy = ref('');
        const sourceList = ref([]);
        const sourceInspection = ref(null);
        const sourceFile = ref(null);
        // 上传后落在暂存区的压缩包路径，导入时要原样回传
        const sourceUploadedPath = ref('');
        const sourceError = ref(null);
        // 源里的分类名 → 本地分类名，留空表示交给后端自动对齐
        const sourceCategoryMap = reactive({});
        const sourceForm = reactive({
            endpoint: '',
            github: '',
            review: false,
            scope_mode: 'public',
            origin_target: '',
            assign_character: false,
            character: '',
        });
        const sourceDefaults = ref({
            enabled: true, review: false, review_forced: false, allow_http: false, max_items: 2000,
        });
        const sourceJob = ref(null);
        let sourcePollInterval = null;

        const observeImages = () => {
            if (!imgObserver) return;
            document.querySelectorAll('.item-image[data-hash]').forEach((el) => {
                if (!el.dataset.observed) {
                    el.dataset.observed = 'true';
                    imgObserver.observe(el);
                }
            });
        };

        const parseSceneList = (rawText) => {
            if (!rawText) return [];
            const seen = new Set();
            return String(rawText)
                .split(/[，,、;；\n\t]+/)
                .map((item) => item.trim())
                .filter((item) => {
                    if (!item || seen.has(item)) return false;
                    seen.add(item);
                    return true;
                });
        };

        const toggleScene = (scene) => {
            const sceneList = parseSceneList(uploadForm.scene);
            if (sceneList.includes(scene)) {
                uploadForm.scene = sceneList.filter((item) => item !== scene).join(', ');
                return;
            }
            uploadForm.scene = [...sceneList, scene].join(', ');
        };

        const isSceneSelected = (scene) => parseSceneList(uploadForm.scene).includes(scene);

        const formatOriginTarget = (target) => {
            const raw = String(target || '').trim();
            if (!raw) return t('pages.dashboard.messages.origin_unset', 'Not recorded');
            if (raw.startsWith('group:')) return `${t('pages.dashboard.messages.origin_group', 'Group')} ${raw.slice(6)}`;
            if (raw.startsWith('user:')) return `${t('pages.dashboard.messages.origin_user', 'User')} ${raw.slice(5)}`;
            return raw;
        };

        const getScopeLabel = (scopeMode) => (
            String(scopeMode || 'public').toLowerCase() === 'local'
                ? t('pages.dashboard.scope.local', 'Local only')
                : t('pages.dashboard.scope.public', 'Public')
        );

        const normalizeCategories = (rawCategories) => {
            if (Array.isArray(rawCategories)) {
                return rawCategories
                    .map((cat) => {
                        if (cat && typeof cat === 'object') {
                            const key = String(cat.key || cat.name || '').trim();
                            return key ? {
                                key,
                                name: String(cat.name || key),
                                count: Number(cat.count || 0),
                            } : null;
                        }
                        const key = String(cat || '').trim();
                        return key ? { key, name: key, count: 0 } : null;
                    })
                    .filter(Boolean);
            }
            if (rawCategories && typeof rawCategories === 'object') {
                return Object.entries(rawCategories).map(([key, count]) => ({
                    key,
                    name: key,
                    count: Number(count || 0),
                }));
            }
            return [];
        };

        const getCategoryName = (key) => {
            const categoryKey = String(key || '').trim();
            const category = [...categories.value, ...pendingCategories.value]
                .find((item) => item.key === categoryKey);
            return category?.name || categoryKey;
        };

        const emotionsOpen = ref(false);
        const newEmotion = reactive({ key: '', name: '', desc: '' });
        const addingEmotion = ref(false);
        const deletingEmotionKey = ref('');

        let searchTimeout = null;

        const THEME_STORAGE_KEY = 'magpie_theme_mode';
        const VIEW_STORAGE_KEY = 'magpie_view_mode';
        const readStored = (key) => {
            try { return localStorage.getItem(key); } catch (e) { return null; }
        };
        const writeStored = (key, value) => {
            try { localStorage.setItem(key, value); } catch (e) { /* 私密模式等场景忽略 */ }
        };

        const THEME_OPTIONS = [
            { value: 'auto', key: 'auto', fallback: 'Follow host', group: 'original' },
            { value: 'dark', key: 'dark', fallback: 'Dark Gold', swatch: '#161b2a,#d4a853', group: 'original' },
            { value: 'light', key: 'light', fallback: 'Light', swatch: '#faf8f3,#8b6914', group: 'original' },
            { value: 'pixel', key: 'pixel', fallback: 'Pixel', swatch: '#2a1c10,#5d9c3d', group: 'game' },
            { value: 'terminal', key: 'terminal', fallback: 'Terminal', swatch: '#021408,#1bff80', group: 'game' },
        ];
        const originalThemeOptions = computed(() => THEME_OPTIONS.filter((o) => o.group !== 'game'));
        const gameThemeOptions = computed(() => THEME_OPTIONS.filter((o) => o.group === 'game'));
        const resolveThemeValue = (raw) => {
            return THEME_OPTIONS.some((o) => o.value === raw) ? raw : 'auto';
        };
        const hostThemeFromQuery = (() => {
            try {
                const q = new URLSearchParams(location.search).get('theme');
                return q === 'light' || q === 'dark' ? q : null;
            } catch (e) { /* ignore */ }
            return null;
        })();
        // AstrBot 会始终附加 ?theme=dark/light。它只代表宿主当前明暗状态，
        // 不能当作用户选择，否则每次重开页面都会压过已保存主题。
        const themeMode = ref(resolveThemeValue(readStored(THEME_STORAGE_KEY) || 'auto'));
        const contextIsDark = ref(hostThemeFromQuery !== 'light');
        const effectiveTheme = computed(() => (
            themeMode.value !== 'auto' ? themeMode.value : (contextIsDark.value ? 'dark' : 'light')
        ));
        const applyTheme = () => {
            document.documentElement.setAttribute('data-theme', effectiveTheme.value);
        };
        let preferenceWriteQueue = Promise.resolve();
        const persistPrefs = (patch) => {
            preferenceWriteQueue = preferenceWriteQueue
                .catch(() => undefined)
                .then(() => bridge.apiPost('prefs', patch))
                .catch(() => undefined); // 无后端时仍走 localStorage
            return preferenceWriteQueue;
        };
        let themePreferenceRevision = 0;
        const setThemeMode = (mode, persist = true) => {
            if (!THEME_OPTIONS.some((o) => o.value === mode)) return;
            if (persist) themePreferenceRevision += 1;
            themeMode.value = mode;
            writeStored(THEME_STORAGE_KEY, mode);
            applyTheme();
            if (persist) persistPrefs({ theme: mode });
        };
        const themePickerOpen = ref(false);
        const closeThemePicker = () => { themePickerOpen.value = false; };

        const viewMode = ref(readStored(VIEW_STORAGE_KEY) === 'list' ? 'list' : 'grid');
        const setViewMode = (mode, persist = true) => {
            viewMode.value = mode === 'list' ? 'list' : 'grid';
            writeStored(VIEW_STORAGE_KEY, viewMode.value);
            if (persist) persistPrefs({ view: viewMode.value });
        };
        const loadDashboardPrefs = async () => {
            const requestRevision = themePreferenceRevision;
            try {
                const data = await bridge.apiGet('prefs');
                if (!data || data.success === false) return;
                // 服务端统一处理“页面偏好 / 配置默认”的优先级；localStorage 只负责
                // 首屏和 API 不可用时兜底。请求期间用户刚点选的主题不得被旧响应覆盖。
                if (data.theme && requestRevision === themePreferenceRevision) {
                    setThemeMode(resolveThemeValue(data.theme), false);
                }
                if (data.view === 'list' || data.view === 'grid') {
                    viewMode.value = data.view;
                    writeStored(VIEW_STORAGE_KEY, data.view);
                }
                if (data.batch_defaults && typeof data.batch_defaults === 'object') {
                    batchDefaults.value = { ...batchDefaults.value, ...data.batch_defaults };
                    batchUploadForm.concurrency = batchDefaults.value.concurrency;
                    batchUploadForm.rpm = batchDefaults.value.rpm;
                }
            } catch (e) { /* 保留 localStorage */ }
        };

        // 分类 accent 色：内置情绪各分配固定色相，自定义分类按 key 哈希
        const CATEGORY_HUES = {
            happy: 45, sad: 215, angry: 2, shy: 330, surprised: 52, troll: 285,
            cry: 225, confused: 200, embarrassed: 18, love: 340, disgust: 90,
            fear: 265, excitement: 30, tired: 195, sigh: 210, thank: 140, dumb: 275, other: 220,
        };
        const hashHue = (text) => {
            let h = 0;
            for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) % 360;
            return h;
        };
        const catAccent = (key) => {
            const cleanKey = String(key || '').trim();
            if (!cleanKey || cleanKey === '__favorite__') return {};
            const hue = CATEGORY_HUES[cleanKey] ?? hashHue(cleanKey);
            const isLightFamily = effectiveTheme.value === 'light';
            const light = isLightFamily ? 36 : 66;
            return {
                '--cat-accent': `hsl(${hue}, 58%, ${light}%)`,
                '--cat-accent-soft': `hsla(${hue}, 55%, ${light}%, 0.16)`,
            };
        };

        // 预览大图缩放 / 拖拽
        const previewZoom = ref(1);
        const previewPanX = ref(0);
        const previewPanY = ref(0);
        const isPanning = ref(false);
        let panStartX = 0, panStartY = 0, panBaseX = 0, panBaseY = 0;
        const clampZoom = (z) => Math.min(8, Math.max(1, z));
        const resetPreviewZoom = () => {
            previewZoom.value = 1;
            previewPanX.value = 0;
            previewPanY.value = 0;
            isPanning.value = false;
        };
        const previewTransform = computed(() => (
            `translate(${previewPanX.value}px, ${previewPanY.value}px) scale(${previewZoom.value})`
        ));
        const onPreviewWheel = (e) => {
            e.preventDefault();
            const next = clampZoom(previewZoom.value * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
            if (next === 1) { previewPanX.value = 0; previewPanY.value = 0; }
            previewZoom.value = next;
        };
        const onPanMove = (e) => {
            if (!isPanning.value) return;
            previewPanX.value = panBaseX + (e.clientX - panStartX);
            previewPanY.value = panBaseY + (e.clientY - panStartY);
        };
        const endPan = () => {
            isPanning.value = false;
            window.removeEventListener('mousemove', onPanMove);
            window.removeEventListener('mouseup', endPan);
        };
        const startPan = (e) => {
            if (previewZoom.value <= 1 || e.button !== 0) return;
            e.preventDefault();
            isPanning.value = true;
            panStartX = e.clientX;
            panStartY = e.clientY;
            panBaseX = previewPanX.value;
            panBaseY = previewPanY.value;
            window.addEventListener('mousemove', onPanMove);
            window.addEventListener('mouseup', endPan);
        };
        const toggleZoom = () => {
            if (previewZoom.value > 1) resetPreviewZoom();
            else previewZoom.value = 2.5;
        };

        // 审核区键盘焦点（A 通过 / R 拒绝 / B 拒绝+拉黑 / E 编辑）
        const focusedPendingId = ref(null);
        const movePendingFocus = (dir) => {
            const list = pendingImages.value;
            if (!list.length) return;
            const idx = list.findIndex((i) => i.id === focusedPendingId.value);
            const next = idx < 0
                ? (dir > 0 ? 0 : list.length - 1)
                : Math.min(list.length - 1, Math.max(0, idx + dir));
            focusedPendingId.value = list[next].id;
            nextTick(() => {
                document.querySelector(`[data-pending-id="${list[next].id}"]`)
                    ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            });
        };

        const imageDataUrls = reactive({});
        const originalDataUrls = reactive({});

        const loadImageData = async (hash) => {
            if (!hash) return;
            const cached = thumbnailCache.get(hash);
            if (cached) { imageDataUrls[hash] = cached; return; }
            if (imageDataUrls[hash]) return;
            if (inflightThumbs.has(hash)) {
                const pending = await inflightThumbs.get(hash);
                if (pending?.url) imageDataUrls[hash] = pending.url;
                return;
            }
            const request = (async () => {
                try {
                    return await bridge.apiGet('thumbnail', { hash, size: 300 });
                } catch (e) {
                    console.error('Failed to load thumbnail:', hash, e);
                    return null;
                } finally {
                    inflightThumbs.delete(hash);
                }
            })();
            inflightThumbs.set(hash, request);
            const data = await request;
            if (data && data.url) {
                imageDataUrls[hash] = data.url;
                thumbnailCache.set(hash, data.url);
            }
        };

        const placeMcTooltip = (event) => {
            const slot = event.currentTarget;
            if (!(slot instanceof HTMLElement) || viewMode.value === 'list') return;
            if (document.documentElement.getAttribute('data-theme') !== 'pixel') return;
            const grid = slot.closest('.inventory-grid');
            const tip = slot.querySelector('.item-info');
            if (!grid || !tip) return;
            slot.classList.remove('tooltip-below', 'tooltip-start', 'tooltip-end');
            const slotRect = slot.getBoundingClientRect();
            const gridRect = grid.getBoundingClientRect();
            const pad = 8;
            const tipH = Math.max(tip.offsetHeight, 44);
            const tipW = Math.max(tip.offsetWidth, 96);
            if (slotRect.top - gridRect.top < tipH + pad) {
                slot.classList.add('tooltip-below');
            }
            const midX = slotRect.left + slotRect.width / 2;
            if (midX - tipW / 2 < gridRect.left + pad) {
                slot.classList.add('tooltip-start');
            } else if (midX + tipW / 2 > gridRect.right - pad) {
                slot.classList.add('tooltip-end');
            }
        };
        const onItemSlotEnter = (event) => {
            placeMcTooltip(event);
        };

        const originalCache = createLRUCache(4);
        const inflightOriginals = new Map();
        const previewLoading = ref(false);

        const pruneOriginalUrls = (keepHash = '') => {
            for (const hash of Object.keys(originalDataUrls)) {
                if (hash !== keepHash && !originalCache.has(hash)) delete originalDataUrls[hash];
            }
        };

        const loadOriginalImage = async (hash) => {
            if (!hash) return null;
            const cached = originalCache.get(hash);
            if (cached) {
                originalDataUrls[hash] = cached;
                return cached;
            }
            if (originalDataUrls[hash]) return originalDataUrls[hash];
            if (inflightOriginals.has(hash)) {
                const pending = await inflightOriginals.get(hash);
                if (pending?.url) {
                    originalCache.set(hash, pending.url);
                    originalDataUrls[hash] = pending.url;
                }
                return pending?.url || null;
            }
            const request = (async () => {
                try {
                    return await bridge.apiGet('image-data', { hash });
                } catch (e) {
                    console.error('Failed to load original image:', hash, e);
                    return null;
                } finally {
                    inflightOriginals.delete(hash);
                }
            })();
            inflightOriginals.set(hash, request);
            const data = await request;
            if (data && data.url) {
                originalCache.set(hash, data.url);
                originalDataUrls[hash] = data.url;
                pruneOriginalUrls(hash);
                return data.url;
            }
            return null;
        };

        const requestOriginalForPreview = (hash) => {
            if (!hash) {
                previewLoading.value = false;
                return;
            }
            if (originalDataUrls[hash] || originalCache.has(hash)) {
                loadOriginalImage(hash);
                previewLoading.value = false;
                return;
            }
            previewLoading.value = true;
            loadOriginalImage(hash).finally(() => {
                if (previewItem.value?.hash === hash) previewLoading.value = false;
            });
        };

        const downloadImage = async (item) => {
            if (!item?.hash) return;
            if (!originalDataUrls[item.hash] && !originalCache.has(item.hash)) {
                await loadOriginalImage(item.hash);
            }
            const dataUrl = originalDataUrls[item.hash] || originalCache.get(item.hash) || imageDataUrls[item.hash];
            if (!dataUrl) return;
            const a = document.createElement('a');
            a.href = dataUrl;
            a.download = (item.desc || item.hash) + '.png';
            a.click();
        };

        const fileToBase64 = (file) => new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });

        const apiFetch = async (url, options = {}) => {
            const urlStr = String(url).replace(/^\/?api\//, '');
            const [path, queryString] = urlStr.split('?');
            const endpoint = path.replace(/\/$/, '');

            const params = {};
            if (queryString) {
                const sp = new URLSearchParams(queryString);
                for (const [k, v] of sp) { params[k] = v; }
            }

            const method = (options.method || 'GET').toUpperCase();
            let body = options.body;

            try {
                let data;

                if (method === 'POST' || method === 'PUT' || method === 'DELETE') {
                    if (body instanceof FormData) {
                        const file = body.get('file');
                        if (file instanceof File) {
                            data = await bridge.upload(endpoint, file);
                        } else {
                            const json = {};
                            const fileEntries = [];
                            for (const [k, v] of body.entries()) {
                                if (v instanceof File) {
                                    fileEntries.push({ key: k, file: v });
                                } else {
                                    json[k] = v;
                                }
                            }
                            if (fileEntries.length > 0) {
                                json._files = await Promise.all(
                                    fileEntries.map(async (entry) => ({
                                        key: entry.key,
                                        name: entry.file.name,
                                        base64: await fileToBase64(entry.file),
                                    }))
                                );
                            }
                            data = await bridge.apiPost(endpoint, json);
                        }
                    } else {
                        if (typeof body === 'string') {
                            try { body = JSON.parse(body); } catch (e) { }
                        }
                        data = await bridge.apiPost(endpoint, body || {});
                    }
                } else {
                    data = await bridge.apiGet(endpoint, Object.keys(params).length ? params : undefined);
                }

                return {
                    ok: true,
                    status: 200,
                    json: async () => data,
                    text: async () => (typeof data === 'string' ? data : JSON.stringify(data)),
                };
            } catch (e) {
                return {
                    ok: false,
                    status: 500,
                    json: async () => { throw e; },
                    text: async () => e.message,
                };
            }
        };

        const fetchStats = async () => {
            try {
                const res = await apiFetch('api/stats');
                const data = await res.json();
                Object.assign(stats, data.stats || {});
            } catch (e) {
                console.error(e);
            }
        };

        const healthStatus = ref('unknown');
        const checkHealth = async () => {
            const start = performance.now();
            try {
                const res = await apiFetch('api/health');
                healthStatus.value = (performance.now() - start) < 200 ? 'ok' : 'slow';
            } catch (e) { healthStatus.value = 'error'; }
        };

        let isFetching = false;
        const fetchImages = async (page = 1) => {
            if (isFetching) return;
            isFetching = true;
            loading.value = true;
            try {
                const params = new URLSearchParams({
                    page: page.toString(),
                    size: pageSize.value.toString(),
                    q: searchQuery.value,
                    category: selectedCategory.value === '__favorite__' ? '' : selectedCategory.value,
                    sort: sortBy.value,
                });
                if (selectedCategory.value === '__favorite__') {
                    params.set('favorite_only', 'true');
                }
                if (selectedCharacter.value) {
                    params.set('character', selectedCharacter.value);
                }
                const res = await apiFetch('api/images?' + params.toString());
                const data = await res.json();
                const nextImages = data.images || [];
                const nextTotal = Number(data.total || 0);
                const lastPage = Math.max(1, Math.ceil(nextTotal / pageSize.value));

                if (page > lastPage && nextTotal > 0) {
                    isFetching = false;
                    return await fetchImages(lastPage);
                }

                currentPage.value = page;
                images.value = nextImages;
                total.value = nextTotal;
                categories.value = normalizeCategories(data.categories);
                characters.value = Array.isArray(data.characters) ? data.characters : [];
                unassignedCharacterCount.value = Number(data.unassigned_character_count || 0);
                favoriteCount.value = Number(data.favorite_count || 0);
                const currentHashes = new Set(nextImages.map(img => img.hash));
                for (const hash of Object.keys(imageDataUrls)) {
                    if (!currentHashes.has(hash)) delete imageDataUrls[hash];
                }
                for (const hash of Object.keys(originalDataUrls)) {
                    if (!currentHashes.has(hash)) delete originalDataUrls[hash];
                }
                nextTick(() => observeImages());
                if (selectedImages.value.size > 0) {
                    const visibleHashes = new Set(nextImages.map((img) => img.hash));
                    selectedImages.value = new Set(
                        Array.from(selectedImages.value).filter((hash) => visibleHashes.has(hash))
                    );
                }
                return nextImages;
            } catch (e) {
                console.error(e);
                return [];
            } finally {
                loading.value = false;
                isFetching = false;
            }
        };

        const fetchEmotions = async () => {
            try {
                const res = await apiFetch('api/emotions');
                const data = await res.json();
                availableEmotions.value = data.emotions || [];
            } catch (e) {
                console.error(e);
            }
        };

        const fetchPendingStats = async () => {
            try {
                const res = await apiFetch('api/pending/stats');
                const data = await res.json();
                if (data.success) Object.assign(pendingStats, data.stats);
            } catch (e) { console.error(e); }
        };

        const fetchPendingImages = async (page = 1) => {
            if (pendingFetchLock) return;
            pendingFetchLock = true;
            pendingLoading.value = true;
            try {
                const params = new URLSearchParams({
                    page: page.toString(),
                    size: pendingPageSize.value.toString(),
                    q: pendingSearchQuery.value,
                    category: pendingCategory.value,
                });
                const res = await apiFetch('api/pending?' + params.toString());
                const data = await res.json();
                if (!data.success) {
                    pendingImages.value = [];
                    pendingTotal.value = 0;
                    pendingCategoryTotal.value = 0;
                    return;
                }
                const nextImages = data.images || [];
                const nextTotal = Number(data.total || 0);
                const nextCategories = normalizeCategories(data.categories);
                const nextCategoryTotal = Number(data.category_total);
                const lastPage = Math.max(1, Math.ceil(nextTotal / pendingPageSize.value));
                if (page > lastPage && nextTotal > 0) {
                    pendingFetchLock = false;
                    return await fetchPendingImages(lastPage);
                }
                pendingCurrentPage.value = page;
                pendingImages.value = nextImages;
                pendingTotal.value = nextTotal;
                pendingCategories.value = nextCategories;
                pendingCategoryTotal.value = Number.isFinite(nextCategoryTotal)
                    ? nextCategoryTotal
                    : nextCategories.reduce((sum, category) => sum + category.count, 0);
                nextImages.forEach(img => { if (img.hash) loadImageData(img.hash); });
            } catch (e) { console.error(e); }
            finally { pendingLoading.value = false; pendingFetchLock = false; }
        };

        const switchSection = (section) => {
            activeSection.value = section;
            sidebarOpen.value = false;
            if (section === 'pending') {
                fetchPendingStats();
                fetchPendingImages(1);
            } else {
                fetchImages(1);
            }
        };

        const selectLibraryCategory = (category) => {
            selectedCategory.value = category;
            sidebarOpen.value = false;
            fetchImages(1);
        };

        const selectLibraryCharacter = (character) => {
            selectedCharacter.value = character;
            sidebarOpen.value = false;
            fetchImages(1);
        };

        const selectPendingCategory = (category) => {
            pendingCategory.value = category;
            sidebarOpen.value = false;
            fetchPendingImages(1);
        };

        const pendingDebouncedSearch = () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => fetchPendingImages(1), 400);
        };

        const approvePending = async (id) => {
            try {
                const res = await apiFetch('api/pending/approve', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id }),
                });
                const data = await res.json();
                if (data.success) {
                    showAlert(t('pages.dashboard.alerts.pending_approved', 'Approved {count} item(s).').replace('{count}', data.approved), 'success');
                    await fetchPendingImages(pendingCurrentPage.value);
                    await fetchPendingStats();
                } else {
                    showAlert(`${t('pages.dashboard.alerts.approve_failed', 'Approve failed')}: ${data.error || t('pages.dashboard.messages.unknown_error', 'Unknown error')}`, 'error');
                }
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.approve_failed', 'Approve failed')}: ${e.message}`, 'error'); }
        };

        // issue #87：审核区编辑
        const parseListField = (value) => {
            if (Array.isArray(value)) {
                return value.map((v) => String(v || '').trim()).filter(Boolean);
            }
            return String(value || '')
                .split(/[,，]/)
                .map((v) => v.trim())
                .filter(Boolean);
        };

        const openPendingEdit = async (item) => {
            if (!item || item.id == null) return;
            pendingEditId.value = item.id;
            pendingEditForm.hash = item.hash || '';
            pendingEditForm.category = item.category || '';
            pendingEditForm.scope_mode = item.scope_mode || 'public';
            pendingEditForm.desc = item.desc || '';
            pendingEditForm.tagsText = (item.tags || []).join(', ');
            pendingEditForm.scenesText = (item.scenes || []).join(', ');
            pendingEditForm.overlay_text = item.overlay_text || '';
            pendingEditForm.character = item.character || '';
            pendingEditForm.work = item.work || '';
            resetSingleReanalyze();
            pendingEditOpen.value = true;
            if (item.hash && !imageDataUrls[item.hash]) {
                loadImageData(item.hash);
            }
        };

        const closePendingEdit = () => {
            resetSingleReanalyze();
            pendingEditOpen.value = false;
            pendingEditId.value = null;
        };

        const savePendingEdit = async (alsoApprove) => {
            if (!pendingEditId.value) return;
            try {
                const payload = {
                    id: pendingEditId.value,
                    category: pendingEditForm.category,
                    scope_mode: pendingEditForm.scope_mode,
                    desc: pendingEditForm.desc,
                    tags: parseListField(pendingEditForm.tagsText),
                    scenes: parseListField(pendingEditForm.scenesText),
                    overlay_text: pendingEditForm.overlay_text || '',
                    character: pendingEditForm.character || '',
                    work: pendingEditForm.work || '',
                };
                const res = await apiFetch('api/pending/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!data.success) {
                    showAlert(`${t('pages.dashboard.alerts.save_failed', 'Save failed')}: ${data.error || t('pages.dashboard.messages.unknown_error', 'Unknown error')}`, 'error');
                    return;
                }

                if (alsoApprove) {
                    await approvePending(pendingEditId.value);
                    closePendingEdit();
                    return;
                }

                // 仅保存：刷新当前页
                showAlert(t('pages.dashboard.alerts.pending_saved', 'Pending item saved.'), 'success');
                closePendingEdit();
                await fetchPendingImages(pendingCurrentPage.value);
                await fetchPendingStats();
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.save_failed', 'Save failed')}: ${e.message}`, 'error');
            }
        };

        const rejectPending = async (id, blacklist = false) => {
            try {
                const res = await apiFetch('api/pending/reject', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id, blacklist }),
                });
                const data = await res.json();
                if (data.success) {
                    const suffix = data.blacklisted ? ` ${t('pages.dashboard.alerts.and_blacklisted', '(blacklisted)')}` : '';
                    showAlert(t('pages.dashboard.alerts.pending_deleted', 'Deleted {count} item(s).').replace('{count}', data.deleted) + suffix, 'success');
                    await fetchPendingImages(pendingCurrentPage.value);
                    await fetchPendingStats();
                } else {
                    showAlert(`${t('pages.dashboard.alerts.delete_failed', 'Delete failed')}: ${data.error || t('pages.dashboard.messages.unknown_error', 'Unknown error')}`, 'error');
                }
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.delete_failed', 'Delete failed')}: ${e.message}`, 'error'); }
        };

        const approvePendingBatch = async () => {
            const ids = Array.from(pendingSelectedImages.value);
            if (!ids.length) { showAlert(t('pages.dashboard.alerts.select_pending_first', 'Select pending items first.')); return; }
            const confirmed = await showConfirm(
                t('pages.dashboard.confirm.pending_approve_batch', 'Approve {count} pending item(s)?').replace('{count}', ids.length)
            );
            if (!confirmed) return;
            try {
                const res = await apiFetch('api/pending/approve', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ids }),
                });
                const data = await res.json();
                if (data.approved) {
                    const failed = data.errors?.length
                        ? ` ${t('pages.dashboard.alerts.failed_count', '{count} failed.').replace('{count}', data.errors.length)}`
                        : '';
                    showAlert(t('pages.dashboard.alerts.pending_approved', 'Approved {count} item(s).').replace('{count}', data.approved) + failed);
                }
                pendingSelectedImages.value = new Set();
                pendingBatchMode.value = false;
                await fetchPendingImages(pendingCurrentPage.value);
                await fetchPendingStats();
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.batch_approve_failed', 'Batch approve failed')}: ${e.message}`, 'error'); }
        };

        const rejectPendingBatch = async (blacklist = false) => {
            const ids = Array.from(pendingSelectedImages.value);
            if (!ids.length) { showAlert(t('pages.dashboard.alerts.select_pending_first', 'Select pending items first.')); return; }
            const key = blacklist
                ? 'pages.dashboard.confirm.pending_delete_blacklist_batch'
                : 'pages.dashboard.confirm.pending_delete_batch';
            const fallback = blacklist
                ? 'Delete and blacklist {count} pending item(s)?'
                : 'Delete {count} pending item(s)?';
            const confirmed = await showConfirm(t(key, fallback).replace('{count}', ids.length));
            if (!confirmed) return;
            try {
                const res = await apiFetch('api/pending/reject', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ids, blacklist }),
                });
                const data = await res.json();
                if (data.success) {
                    const suffix = data.blacklisted
                        ? ` ${t('pages.dashboard.alerts.blacklisted_count', 'Blacklisted {count} item(s).').replace('{count}', data.blacklisted)}`
                        : '';
                    showAlert(t('pages.dashboard.alerts.pending_deleted', 'Deleted {count} item(s).').replace('{count}', data.deleted) + suffix, 'success');
                }
                pendingSelectedImages.value = new Set();
                pendingBatchMode.value = false;
                await fetchPendingImages(pendingCurrentPage.value);
                await fetchPendingStats();
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.batch_delete_failed', 'Batch delete failed')}: ${e.message}`, 'error'); }
        };

        const pendingBatchMode = ref(false);
        const pendingSelectedImages = ref(new Set());

        const togglePendingBatchMode = () => {
            pendingBatchMode.value = !pendingBatchMode.value;
            if (!pendingBatchMode.value) pendingSelectedImages.value = new Set();
        };

        const togglePendingSelection = (item) => {
            const s = new Set(pendingSelectedImages.value);
            s.has(item.id) ? s.delete(item.id) : s.add(item.id);
            pendingSelectedImages.value = s;
        };

        const allPendingSelected = Vue.computed(() => {
            const imgs = pendingImages.value;
            if (!imgs.length) return false;
            const selected = pendingSelectedImages.value;
            return imgs.every(img => selected.has(img.id));
        });

        const toggleSelectAllPending = () => {
            const currentIds = new Set(pendingImages.value.map(img => img.id));
            if (allPendingSelected.value) {
                pendingSelectedImages.value = new Set();
            } else {
                pendingSelectedImages.value = currentIds;
            }
        };

        // 作品名联想列表（datalist），失败时静默降级为纯手输
        const fetchWorks = async () => {
            try {
                const data = await bridge.apiGet('works');
                if (!data || data.success === false) return;
                works.value = (Array.isArray(data.works) ? data.works : [])
                    .map((item) => ({ key: String(item.name || ''), count: Number(item.count || 0) }))
                    .filter((item) => item.key);
            } catch (e) { /* 联想不可用不影响主流程 */ }
        };

        const loadAll = async () => {
            await fetchStats();
            await fetchEmotions();
            await fetchImages(1);
            fetchWorks();
        };

        const debouncedSearch = () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => fetchImages(1), 400);
        };

        const refreshView = async () => {
            await fetchImages(currentPage.value);
            await fetchStats();
        };

        const prevPage = () => currentPage.value > 1 && fetchImages(currentPage.value - 1);
        const nextPage = () => currentPage.value * pageSize.value < total.value && fetchImages(currentPage.value + 1);

        const openPreview = (img) => {
            resetSingleReanalyze();
            previewItem.value = img;
            previewOpen.value = true;
            resetPreviewZoom();
            requestOriginalForPreview(img?.hash);
        };

        const closePreview = () => {
            previewOpen.value = false;
            previewItem.value = null;
            isEditing.value = false;
            previewLoading.value = false;
            resetPreviewZoom();
            pruneOriginalUrls();
        };

        const navigateImage = (direction) => {
            if (!previewItem.value) return;
            const idx = images.value.findIndex((i) => i.hash === previewItem.value.hash);
            const nextIdx = idx + direction;
            if (nextIdx >= 0 && nextIdx < images.value.length) {
                previewItem.value = images.value[nextIdx];
                resetPreviewZoom();
                requestOriginalForPreview(previewItem.value.hash);
            }
        };
        const prevImage = () => navigateImage(-1);
        const nextImage = () => navigateImage(1);

        const isTypingTarget = (e) => {
            const target = e.target;
            return Boolean(target && (target.matches?.('input, textarea, select') || target.isContentEditable));
        };
        const anyModalOpen = () => (
            confirmOpen.value || promptOpen.value || uploadOpen.value || batchUploadOpen.value
            || emotionsOpen.value || batchMoveOpen.value || batchScopeOpen.value || pendingEditOpen.value
            || batchCharacterOpen.value || batchWorkOpen.value || charactersOpen.value
        );

        const handleKeydown = (e) => {
            if (isTypingTarget(e)) return;
            // 上面压着确认框 / 输入框时，Esc 不该穿透去关下面那层弹窗
            if (confirmOpen.value || promptOpen.value) return;
            if (previewOpen.value) {
                // 编辑态下 Esc 不直接关窗（刚填的内容会没），给个提示指向「取消」
                if (isEditing.value) {
                    if (e.key === 'Escape') { e.preventDefault(); refuseModalClose('preview'); }
                    return;
                }
                if (e.key === 'ArrowLeft') prevImage();
                else if (e.key === 'ArrowRight') nextImage();
                else if (e.key === 'Escape') closePreview();
                else if (e.key === '+' || e.key === '=') previewZoom.value = clampZoom(previewZoom.value * 1.25);
                else if (e.key === '-' || e.key === '_') previewZoom.value = clampZoom(previewZoom.value / 1.25);
                else if (e.key === '0') resetPreviewZoom();
                return;
            }
            if (e.key === 'Escape' && closeTopGuardedModal()) { e.preventDefault(); return; }
            if (anyModalOpen()) return;
            if (activeSection.value !== 'pending' || !pendingImages.value.length) return;
            switch (e.key) {
                case 'ArrowRight':
                case 'ArrowDown': e.preventDefault(); movePendingFocus(1); break;
                case 'ArrowLeft':
                case 'ArrowUp': e.preventDefault(); movePendingFocus(-1); break;
                case 'Escape': focusedPendingId.value = null; break;
                case 'a':
                case 'A':
                    if (focusedPendingId.value != null) { approvePending(focusedPendingId.value); focusedPendingId.value = null; }
                    break;
                case 'r':
                case 'R':
                    if (focusedPendingId.value != null) { rejectPending(focusedPendingId.value, e.shiftKey); focusedPendingId.value = null; }
                    break;
                case 'b':
                case 'B':
                    if (focusedPendingId.value != null) { rejectPending(focusedPendingId.value, true); focusedPendingId.value = null; }
                    break;
                case 'e':
                case 'E': {
                    const item = pendingImages.value.find((i) => i.id === focusedPendingId.value);
                    if (item) openPendingEdit(item);
                    break;
                }
                default: break;
            }
        };

        const startEdit = () => {
            if (!previewItem.value) return;
            Object.assign(editForm, {
                category: previewItem.value.category,
                tags: (previewItem.value.tags || []).join(', '),
                scene: (previewItem.value.scenes || []).join('、'),
                desc: previewItem.value.desc,
                overlay_text: previewItem.value.overlay_text || '',
                character: previewItem.value.character || '',
                work: previewItem.value.work || '',
                scope_mode: previewItem.value.scope_mode || 'public',
            });
            isEditing.value = true;
        };

        const cancelEdit = () => {
            resetSingleReanalyze();
            isEditing.value = false;
            releaseModal('preview');
        };

        const saveEdit = async () => {
            if (!previewItem.value) return;
            try {
                const res = await apiFetch('api/images/update', {
                    method: 'POST',
                    body: JSON.stringify({ ...editForm, hash: previewItem.value.hash }),
                });
                const data = await res.json();
                if (data.success) {
                    isEditing.value = false;
                    releaseModal('preview');
                    const refreshedImages = await fetchImages(currentPage.value);
                    const refreshedItem = refreshedImages.find((item) => item.hash === previewItem.value.hash);
                    if (refreshedItem) {
                        previewItem.value = refreshedItem;
                    } else {
                        previewItem.value.category = editForm.category;
                        previewItem.value.tags = editForm.tags.split(',').map((t) => t.trim()).filter((t) => t);
                        previewItem.value.scenes = parseSceneList(editForm.scene);
                        previewItem.value.desc = editForm.desc;
                        previewItem.value.overlay_text = editForm.overlay_text || '';
                        previewItem.value.character = editForm.character || '';
                        previewItem.value.work = editForm.work || '';
                        previewItem.value.scope_mode = editForm.scope_mode || 'public';
                    }
                    await fetchStats();
                    fetchWorks();
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.save_failed', 'Save failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.save_failed', 'Save failed')}: ${e.message}`, 'error');
            }
        };

        const deleteImage = async (img, blacklist = false) => {
            const msg = blacklist
                ? t(
                    'pages.dashboard.confirm.delete_and_blacklist_image',
                    'Delete and blacklist this image?\nIt will no longer be auto-collected.'
                )
                : t(
                    'pages.dashboard.confirm.delete_image',
                    'Delete this image? This action cannot be undone.'
                );
            if (!await showConfirm(msg)) return;
            try {
                const res = await apiFetch('api/images/delete', {
                    method: 'POST',
                    body: JSON.stringify({ hash: img.hash, blacklist }),
                });
                if (res.ok) {
                    closePreview();
                    if (images.value.length === 1 && currentPage.value > 1) {
                        currentPage.value--;
                    }
                    refreshView();
                } else {
                    showAlert(t('pages.dashboard.alerts.delete_failed', 'Delete failed.'));
                }
            } catch (e) {
                showAlert(t('pages.dashboard.alerts.action_failed', 'Action failed.'));
            }
        };

        const toggleBatchMode = () => {
            isBatchMode.value = !isBatchMode.value;
            selectedImages.value = new Set();
        };

        const toggleSelection = (img) => {
            const next = new Set(selectedImages.value);
            if (next.has(img.hash)) {
                next.delete(img.hash);
            } else {
                next.add(img.hash);
            }
            selectedImages.value = next;
        };

        const selectAll = () => {
            selectedImages.value = selectedImages.value.size === images.value.length
                ? new Set()
                : new Set(images.value.map(i => i.hash));
        };

        const handleBatchDelete = async () => {
            if (selectedImages.value.size === 0) return;
            if (!await showConfirm(
                t('pages.dashboard.confirm.delete_selected_images', 'Delete {count} selected image(s)?')
                    .replace('{count}', selectedImages.value.size)
            )) return;

            try {
                const res = await apiFetch('api/images/batch-delete', {
                    method: 'POST',
                    body: JSON.stringify({ hashes: Array.from(selectedImages.value) }),
                });
                const data = await res.json();
                if (data.success) {
                    selectedImages.value = new Set();
                    refreshView();
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.delete_failed', 'Delete failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const openBatchMoveModal = () => {
            if (selectedImages.value.size === 0) return;
            batchTargetCategory.value = '';
            batchMoveOpen.value = true;
        };

        const closeBatchMoveModal = () => {
            batchMoveOpen.value = false;
        };

        const openBatchCharacterModal = () => {
            if (selectedImages.value.size === 0) return;
            batchTargetCharacter.value = '';
            batchCharacterOpen.value = true;
        };

        const closeBatchCharacterModal = () => {
            batchCharacterOpen.value = false;
        };

        const confirmBatchCharacter = async () => {
            try {
                const res = await apiFetch('api/images/batch-character', {
                    method: 'POST',
                    body: JSON.stringify({
                        hashes: Array.from(selectedImages.value),
                        character: batchTargetCharacter.value || '',
                    }),
                });
                const data = await res.json();
                if (data.success) {
                    batchCharacterOpen.value = false;
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    refreshView();
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.save_failed', 'Save failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const openBatchWorkModal = () => {
            if (selectedImages.value.size === 0) return;
            batchTargetWork.value = '';
            batchWorkOpen.value = true;
        };

        const closeBatchWorkModal = () => {
            batchWorkOpen.value = false;
        };

        const confirmBatchWork = async () => {
            try {
                const res = await apiFetch('api/images/batch-work', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        hashes: Array.from(selectedImages.value),
                        work: batchTargetWork.value || '',
                    }),
                });
                const data = await res.json();
                if (data.success) {
                    batchWorkOpen.value = false;
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    refreshView();
                    fetchWorks();
                    showAlert(t('pages.dashboard.messages.work_updated', 'Work name updated.'), 'success');
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.save_failed', 'Save failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const openBatchScopeModal = () => {
            if (selectedImages.value.size === 0) return;
            batchScopeMode.value = 'public';
            batchScopeOpen.value = true;
        };

        const closeBatchScopeModal = () => {
            batchScopeOpen.value = false;
        };

        const confirmBatchMove = async () => {
            if (!batchTargetCategory.value) return;
            try {
                const res = await apiFetch('api/images/batch-move', {
                    method: 'POST',
                    body: JSON.stringify({
                        hashes: Array.from(selectedImages.value),
                        category: batchTargetCategory.value,
                    }),
                });
                const data = await res.json();
                if (data.success) {
                    batchMoveOpen.value = false;
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    refreshView();
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.move_failed', 'Move failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const confirmBatchScope = async () => {
            if (!batchScopeMode.value) return;
            try {
                const res = await apiFetch('api/images/batch-scope', {
                    method: 'POST',
                    body: JSON.stringify({
                        hashes: Array.from(selectedImages.value),
                        scope_mode: batchScopeMode.value,
                    }),
                });
                const data = await res.json();
                if (data.success) {
                    batchScopeOpen.value = false;
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    await fetchImages(currentPage.value);
                    if (Number(data.skipped || 0) > 0) {
                        showAlert(
                            t(
                                'pages.dashboard.alerts.batch_scope_partial',
                                'Updated {count} image(s). {skipped} skipped because origin group info is missing.'
                            )
                                .replace('{count}', data.count || 0)
                                .replace('{skipped}', data.skipped)
                        );
                    }
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.scope_set_failed', 'Scope update failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const toggleScope = async (img, scopeMode) => {
            if (!img) return;
            try {
                const res = await apiFetch('api/images/update', {
                    method: 'POST',
                    body: JSON.stringify({ hash: img.hash, scope_mode: scopeMode }),
                });
                const data = await res.json();
                if (data.success) {
                    if (previewItem.value && previewItem.value.hash === img.hash) {
                        previewItem.value.scope_mode = scopeMode;
                    }
                    await fetchImages(currentPage.value);
                } else if (data.error === 'Origin target missing') {
                    showAlert(t('pages.dashboard.alerts.scope_origin_missing', 'This image is missing origin group info and cannot be set to local.'));
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.scope_update_failed', 'Scope update failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            }
        };

        const favoriteCount = ref(0);

        const toggleFavorite = async (img) => {
            if (!img?.hash) return;
            const newValue = !img.is_favorite;
            try {
                const res = await apiFetch('api/images/update', {
                    method: 'POST',
                    body: JSON.stringify({ hash: img.hash, is_favorite: newValue }),
                });
                const data = await res.json();
                if (data.success) {
                    img.is_favorite = newValue;
                    favoriteCount.value += newValue ? 1 : -1;
                    if (selectedCategory.value === '__favorite__' && !newValue) {
                        await fetchImages(currentPage.value);
                    }
                } else { showAlert(data.error || t('pages.dashboard.alerts.action_failed', 'Action failed.')); }
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.favorite_failed', 'Favorite update failed')}: ${e.message}`, 'error'); }
        };

        const batchSetFavorite = async (favorite) => {
            if (selectedImages.value.size === 0) return;
            try {
                const res = await apiFetch('api/images/batch-favorite', {
                    method: 'POST',
                    body: JSON.stringify({ hashes: Array.from(selectedImages.value), favorite }),
                });
                const data = await res.json();
                if (data.success) {
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    await fetchImages(currentPage.value);
                    showAlert(
                        t(
                            favorite ? 'pages.dashboard.alerts.batch_favorite_added' : 'pages.dashboard.alerts.batch_favorite_removed',
                            favorite ? 'Favorited {count} image(s).' : 'Removed favorites from {count} image(s).'
                        ).replace('{count}', data.count || 0)
                    );
                } else { showAlert(data.error || t('pages.dashboard.alerts.batch_action_failed', 'Batch action failed.')); }
            } catch (e) { showAlert(`${t('pages.dashboard.alerts.batch_action_failed', 'Batch action failed')}: ${e.message}`, 'error'); }
        };

        const runStorageCleanup = async () => {
            try {
                const scanRes = await apiFetch('api/storage/scan');
                const scan = await scanRes.json();
                if (!scan.success) {
                    showAlert(scan.error || t('pages.dashboard.alerts.storage_scan_failed', 'Storage scan failed.'));
                    return;
                }
                const totalCount =
                    Number(scan.stale_index?.count || 0) +
                    Number(scan.orphan_files?.count || 0) +
                    Number(scan.thumb_cache?.count || 0) +
                    Number(scan.temp_files?.count || 0);
                if (totalCount <= 0) {
                    showAlert(t('pages.dashboard.alerts.storage_nothing_to_clean', 'No storage items need cleanup.'));
                    return;
                }
                const ok = await showConfirm(
                    t(
                        'pages.dashboard.confirm.storage_cleanup',
                        'Found {count} cleanable item(s). This will remove stale indexes, orphan files, thumbnail cache, and temp files. Continue?'
                    ).replace('{count}', totalCount)
                );
                if (!ok) return;
                const cleanRes = await apiFetch('api/storage/cleanup', {
                    method: 'POST',
                    body: JSON.stringify({ strategy: 'balanced' }),
                });
                const clean = await cleanRes.json();
                if (!clean.success) {
                    showAlert(clean.error || t('pages.dashboard.alerts.storage_cleanup_failed', 'Storage cleanup failed.'));
                    return;
                }
                const removed = clean.removed || {};
                const removedCount = Object.values(removed).reduce((sum, value) => sum + Number(value || 0), 0);
                await fetchImages(currentPage.value);
                await fetchStats();
                showAlert(t('pages.dashboard.alerts.storage_cleanup_done', 'Storage cleanup completed. Processed {count} item(s).').replace('{count}', removedCount), 'success');
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.storage_cleanup_failed', 'Storage cleanup failed')}: ${e.message}`, 'error');
            }
        };

        const repairSelectedScope = async () => {
            if (selectedImages.value.size === 0) return;
            const originTarget = await showPrompt(
                t(
                    'pages.dashboard.prompt.origin_scope',
                    'Enter the origin scope, for example group:123456 or user:123456.'
                )
            );
            if (!originTarget || !originTarget.trim()) return;
            try {
                const res = await apiFetch('api/images/scope-repair', {
                    method: 'POST',
                    body: JSON.stringify({
                        hashes: Array.from(selectedImages.value),
                        origin_target: originTarget.trim(),
                        scope_mode: 'local',
                        only_missing: false,
                    }),
                });
                const data = await res.json();
                if (data.success) {
                    selectedImages.value = new Set();
                    isBatchMode.value = false;
                    await fetchImages(currentPage.value);
                    showAlert(t('pages.dashboard.alerts.scope_repaired', 'Repaired origin scope for {count} image(s).').replace('{count}', data.count || 0), 'success');
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.scope_repair_failed', 'Origin scope repair failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.scope_repair_failed', 'Origin scope repair failed')}: ${e.message}`);
            }
        };

        const openUploadModal = () => {
            uploadOpen.value = true;
            uploadFile.value = null;
            uploadPreviewUrl.value = null;
            uploadError.value = null;
            Object.assign(uploadForm, {
                emotion: selectedCategory.value || '',
                tags: '',
                scene: '',
                desc: '',
                overlay_text: '',
                character: '',
                work: '',
            });
            analysisScenes.value = [];
            fetchEmotions();
        };

        const closeUploadModal = () => {
            if (uploadPreviewUrl.value) URL.revokeObjectURL(uploadPreviewUrl.value);
            uploadOpen.value = false;
            analysisScenes.value = [];
        };

        const openBatchUploadModal = () => {
            batchMode.value = 'upload';
            batchUploadOpen.value = true;
            batchFiles.value = [];
            batchPreviews.value = [];
            batchUploadError.value = null;
            batchDragActive.value = false;
            batchTaskId.value = null;
            batchTaskStatus.value = null;
            Object.assign(batchUploadForm, {
                emotion: selectedCategory.value || '',
                autoAnalyze: false,
                character: selectedCharacter.value && selectedCharacter.value !== '__none__' ? selectedCharacter.value : '',
                work: '',
                concurrency: batchDefaults.value.concurrency,
                rpm: batchDefaults.value.rpm,
            });
            fetchEmotions();
        };

        const closeBatchUploadModal = () => {
            batchUploadOpen.value = false;
            batchMode.value = 'upload';
            batchDragActive.value = false;
            stopBatchStatusPoll();
        };

        const resetBatchInput = (inputEl) => {
            if (inputEl) inputEl.value = '';
        };

        const normalizeImageFiles = (fileList) => Array.from(fileList || []).filter((file) =>
            file && String(file.type || '').startsWith('image/')
        );

        const setBatchFiles = (files) => {
            batchPreviews.value.forEach((url) => URL.revokeObjectURL(url));
            batchFiles.value = files;
            batchPreviews.value = files.map((file) => URL.createObjectURL(file));
        };

        const clearBatchFiles = () => {
            setBatchFiles([]);
            resetBatchInput(batchFileInput.value);
            resetBatchInput(batchFolderInput.value);
        };

        const batchFileInput = ref(null);
        const batchFolderInput = ref(null);
        const openNativeFilePicker = (inputEl) => {
            if (!inputEl) return;
            resetBatchInput(inputEl);
            if (typeof inputEl.showPicker === 'function') {
                try {
                    inputEl.showPicker();
                    return;
                } catch (e) {
                    console.warn('showPicker failed, falling back to click():', e);
                }
            }
            inputEl.click();
        };
        const triggerBatchFileInput = () => {
            const el = batchFolderMode.value ? batchFolderInput.value : batchFileInput.value;
            openNativeFilePicker(el);
        };

        const handleBatchFileSelect = (e) => {
            const files = normalizeImageFiles(e.target?.files);
            if (files.length === 0) return;
            batchUploadError.value = null;
            setBatchFiles(files);
            resetBatchInput(e.target);
        };

        const batchAreaContainsDragTarget = (event) => {
            const currentTarget = event.currentTarget;
            const relatedTarget = event.relatedTarget;
            return Boolean(currentTarget && relatedTarget && currentTarget.contains(relatedTarget));
        };

        const onBatchDragEnter = (event) => {
            event.preventDefault();
            batchDragActive.value = true;
        };

        const onBatchDragOver = (event) => {
            event.preventDefault();
            if (event.dataTransfer) {
                event.dataTransfer.dropEffect = 'copy';
            }
            batchDragActive.value = true;
        };

        const onBatchDragLeave = (event) => {
            if (batchAreaContainsDragTarget(event)) return;
            batchDragActive.value = false;
        };

        const onBatchDrop = (event) => {
            event.preventDefault();
            batchDragActive.value = false;
            const files = normalizeImageFiles(event.dataTransfer?.files);
            if (files.length === 0) {
                batchUploadError.value = t('pages.dashboard.alerts.no_images_dropped', 'No image files were dropped.');
                return;
            }
            batchUploadError.value = null;
            setBatchFiles(files);
        };

        const formatBatchSize = () => {
            const totalSize = batchFiles.value.reduce((sum, f) => sum + f.size, 0);
            if (totalSize < 1024) return totalSize + ' B';
            if (totalSize < 1024 * 1024) return (totalSize / 1024).toFixed(1) + ' KB';
            return (totalSize / (1024 * 1024)).toFixed(1) + ' MB';
        };

        const clampInt = (value, min, max, fallback) => {
            const num = Number.parseInt(String(value), 10);
            if (!Number.isFinite(num)) return fallback;
            return Math.min(max, Math.max(min, num));
        };

        const resetBatchThrottle = () => {
            batchUploadForm.concurrency = batchDefaults.value.concurrency;
            batchUploadForm.rpm = batchDefaults.value.rpm;
        };

        const resetReanalyzeThrottle = () => {
            reanalyzeForm.concurrency = batchDefaults.value.concurrency;
            reanalyzeForm.rpm = batchDefaults.value.rpm;
        };

        const fetchReanalyzeScan = async () => {
            reanalyzeScanning.value = true;
            reanalyzeScanFailed.value = false;
            try {
                const res = await apiFetch('api/images/reanalyze-scan');
                const data = await res.json();
                if (data.success) {
                    reanalyzeScan.value = {
                        total: Number(data.total || 0),
                        missing: Number(data.missing || 0),
                        no_desc: Number(data.no_desc || 0),
                        pending_total: Number(data.pending_total || 0),
                        pending_missing: Number(data.pending_missing || 0),
                        pending_no_desc: Number(data.pending_no_desc || 0),
                        max_items: Number(data.max_items || 5000),
                    };
                    autoPickReanalyzeTarget();
                } else {
                    reanalyzeScanFailed.value = true;
                }
            } catch (e) {
                reanalyzeScanFailed.value = true;
                console.error('Reanalyze scan error:', e);
            } finally {
                reanalyzeScanning.value = false;
            }
        };

        const reanalyzeIsPending = computed(() => reanalyzeForm.scope === 'pending');

        // 三档口径随作用范围切换：勾选的 / 缺标注的 / 全部
        const reanalyzeSelectedCount = computed(() => (
            reanalyzeIsPending.value ? pendingSelectedImages.value.size : selectedImages.value.size
        ));

        const reanalyzeMissingCount = computed(() => (
            reanalyzeIsPending.value ? reanalyzeScan.value.pending_missing : reanalyzeScan.value.missing
        ));

        const reanalyzeNoDescCount = computed(() => (
            reanalyzeIsPending.value ? reanalyzeScan.value.pending_no_desc : reanalyzeScan.value.no_desc
        ));

        const reanalyzeAllCount = computed(() => (
            reanalyzeIsPending.value ? reanalyzeScan.value.pending_total : reanalyzeScan.value.total
        ));

        const reanalyzeTargetLabel = (key) => {
            if (key === 'selected') {
                return reanalyzeIsPending.value
                    ? t('pages.dashboard.reanalyze.target_selected_pending', '当前勾选的待审核图片')
                    : t('pages.dashboard.reanalyze.target_selected', '当前勾选的表情');
            }
            if (key === 'missing') return t('pages.dashboard.reanalyze.target_missing', '只补缺失标注的');
            if (key === 'no_desc') return t('pages.dashboard.reanalyze.target_no_desc', '只补没有描述的');
            return reanalyzeIsPending.value
                ? t('pages.dashboard.reanalyze.target_all_pending', '全部待审核图片')
                : t('pages.dashboard.reanalyze.target_all', '全部表情包');
        };

        // 打开弹窗时的默认档位可能正好是 0 张，扫描结果回来后挪到第一个有货的档位，并把原因写在提示里
        const autoPickReanalyzeTarget = () => {
            if (reanalyzeTargetTouched.value) return;
            const counts = {
                selected: reanalyzeSelectedCount.value,
                missing: reanalyzeMissingCount.value,
                no_desc: reanalyzeNoDescCount.value,
                all: reanalyzeAllCount.value,
            };
            const current = reanalyzeForm.target;
            const next = ['selected', 'missing', 'no_desc', 'all'].find(
                (key) => (counts[key] || 0) > 0,
            );
            if ((counts[current] || 0) > 0 || !next || next === current) {
                reanalyzeSwitchFrom.value = '';
                reanalyzeSwitchTo.value = '';
                return;
            }
            reanalyzeForm.target = next;
            reanalyzeSwitchFrom.value = current;
            reanalyzeSwitchTo.value = next;
        };

        // 用户自己点了档位：停掉自动兜底，顺手撤掉已经过期的「已自动切到」提示
        const onReanalyzeTargetPick = () => {
            reanalyzeTargetTouched.value = true;
            reanalyzeSwitchFrom.value = '';
            reanalyzeSwitchTo.value = '';
        };

        const reanalyzeSwitchNote = computed(() => {
            if (!reanalyzeSwitchFrom.value || !reanalyzeSwitchTo.value) return '';
            return t('pages.dashboard.reanalyze.auto_switched', '「{from}」当前是 0 张，已自动切到「{to}」。')
                .replace('{from}', reanalyzeTargetLabel(reanalyzeSwitchFrom.value))
                .replace('{to}', reanalyzeTargetLabel(reanalyzeSwitchTo.value));
        });

        const reanalyzeTargetCount = computed(() => {
            if (reanalyzeForm.target === 'selected') return reanalyzeSelectedCount.value;
            if (reanalyzeForm.target === 'missing') return reanalyzeMissingCount.value;
            if (reanalyzeForm.target === 'no_desc') return reanalyzeNoDescCount.value;
            return reanalyzeAllCount.value;
        });

        // 从别处带着指定档位跳进来时（例如「识别失败检测」直接指定「只补没有描述的」），
        // 自动兜底是关掉的。这时候档位正好 0 张，得把话说明白，别让人对着灰按钮猜。
        const reanalyzeTargetEmptyNote = computed(() => {
            if (reanalyzeScanning.value || reanalyzeSwitchNote.value) return '';
            if (reanalyzeAllCount.value === 0 || reanalyzeTargetCount.value > 0) return '';
            return t('pages.dashboard.reanalyze.target_empty', '「{target}」当前是 0 张，换一个处理范围再开始。')
                .replace('{target}', reanalyzeTargetLabel(reanalyzeForm.target));
        });

        // 实际会跑的张数：受「最多处理」输入与后端硬上限双重约束
        const reanalyzePlannedCount = computed(() => {
            const cap = reanalyzeScan.value.max_items || 5000;
            const total = Math.min(reanalyzeTargetCount.value, cap);
            const limit = clampInt(reanalyzeForm.limit, 0, 100000, 0);
            return limit > 0 ? Math.min(limit, total) : total;
        });

        const reanalyzeEstimateMinutes = computed(() => {
            const count = reanalyzePlannedCount.value;
            if (count === 0) return '0';
            const maxConc = batchDefaults.value.max_concurrency || 16;
            const conc = clampInt(reanalyzeForm.concurrency, 1, maxConc, 2);
            const rpm = clampInt(reanalyzeForm.rpm, 0, 600, 0);
            const byConcurrency = conc * 10;
            const perMinute = Math.max(1, rpm > 0 ? Math.min(byConcurrency, rpm) : byConcurrency);
            const minutes = count / perMinute;
            return minutes < 1 ? '<1' : String(Math.ceil(minutes));
        });

        const reanalyzeChangedCount = computed(() =>
            batchTaskResults.value.filter((item) => item && item.success && (item.changed || []).length > 0).length
        );

        const reanalyzeSuggestions = computed(() =>
            batchTaskResults.value.filter((item) => item && item.success && item.suggested_category)
        );

        // 粗估识别耗时：并发与 RPM 取更严格的一方，单张按 6 秒计
        const batchEstimateMinutes = computed(() => {
            const count = batchFiles.value.length;
            if (count === 0) return '0';
            if (!batchUploadForm.autoAnalyze) return '<1';
            const maxConc = batchDefaults.value.max_concurrency || 16;
            const conc = clampInt(batchUploadForm.concurrency, 1, maxConc, 2);
            const rpm = clampInt(batchUploadForm.rpm, 0, 600, 0);
            const byConcurrency = conc * 10;
            const perMinute = Math.max(1, rpm > 0 ? Math.min(byConcurrency, rpm) : byConcurrency);
            const minutes = count / perMinute;
            return minutes < 1 ? '<1' : String(Math.ceil(minutes));
        });

        const batchProgressPercent = computed(() => {
            const total = batchTaskTotal.value;
            if (!total) return 0;
            return Math.min(100, Math.round((batchTaskProcessed.value / total) * 100));
        });

        const batchEtaText = computed(() => {
            const secs = Math.round(batchTaskEta.value || 0);
            if (secs <= 0) return '';
            if (secs < 60) return secs + 's';
            const mins = Math.floor(secs / 60);
            if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
            return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
        });

        const batchPhaseText = computed(() => {
            if (batchTaskPhase.value === 'analyzing') return t('pages.dashboard.batch.phase_analyzing', 'Recognizing');
            if (batchTaskPhase.value === 'storing') return t('pages.dashboard.batch.phase_storing', 'Saving');
            return t('pages.dashboard.batch.phase_idle', 'Waiting');
        });

        const batchThrottleText = computed(() => {
            const rpm = batchTaskRpm.value || 0;
            const rpmText = rpm > 0 ? rpm + ' RPM' : t('pages.dashboard.batch.rpm_unlimited', 'unlimited');
            return (batchTaskConcurrency.value || 1) + ' / ' + rpmText;
        });

        const batchStatusLabel = computed(() => {
            switch (batchTaskStatus.value) {
                case 'processing': return t('pages.dashboard.batch.processing', 'Processing...');
                case 'paused': return t('pages.dashboard.batch.paused', 'Paused');
                case 'completed': return t('pages.dashboard.batch.completed', 'Import complete');
                case 'cancelled': return t('pages.dashboard.batch.cancelled', 'Stopped');
                case 'failed': return t('pages.dashboard.batch.failed', 'Import failed');
                default: return t('pages.dashboard.batch.queued', 'Queued');
            }
        });

        const batchFailures = computed(() => (batchTaskResults.value || [])
            .filter((item) => item && item.success === false)
            .map((item) => ({
                filename: item.filename || '-',
                reason: item.error || t('pages.dashboard.messages.unknown_error', 'Unknown error'),
            })));

        const applyBatchStatus = (data) => {
            batchTaskStatus.value = data.status || null;
            batchTaskTotal.value = Number(data.total || 0);
            batchTaskProcessed.value = Number(data.processed || 0);
            batchTaskSuccess.value = Number(data.success_count || 0);
            batchTaskFailed.value = Number(data.failed_count || 0);
            batchTaskAnalyzed.value = Number(data.analyzed_count || 0);
            batchTaskCurrentFile.value = String(data.current_file || '');
            batchTaskPhase.value = String(data.phase || '');
            batchTaskPaused.value = Boolean(data.paused);
            batchTaskCancelRequested.value = Boolean(data.cancel_requested);
            batchTaskAutoAnalyze.value = Boolean(data.auto_analyze);
            batchTaskEta.value = Number(data.eta_seconds || 0);
            batchTaskRateLimited.value = Number(data.rate_limited_count || 0);
            batchTaskRetried.value = Number(data.retried_count || 0);
            batchTaskConcurrency.value = Number(data.concurrency || 0);
            batchTaskRpm.value = Number(data.rpm || 0);
            batchTaskResults.value = Array.isArray(data.results) ? data.results : [];
        };

        const stopBatchStatusPoll = () => {
            if (batchPollInterval) clearInterval(batchPollInterval);
            batchPollInterval = null;
        };

        const submitBatchUpload = async () => {
            if (batchFiles.value.length === 0) return;
            if (!batchUploadForm.emotion && !batchUploadForm.autoAnalyze) {
                batchUploadError.value = t('pages.dashboard.alerts.select_category_or_auto', 'Select a category or enable auto analyze.');
                return;
            }
            batchUploading.value = true;
            batchUploadError.value = null;
            try {
                const maxConc = batchDefaults.value.max_concurrency || 16;
                const formData = new FormData();
                for (const file of batchFiles.value) {
                    formData.append('files', file);
                }
                if (batchUploadForm.emotion) {
                    formData.append('category', batchUploadForm.emotion);
                }
                formData.append('auto_analyze', String(batchUploadForm.autoAnalyze));
                if (batchUploadForm.character) {
                    formData.append('character', batchUploadForm.character);
                }
                if (batchUploadForm.work) {
                    formData.append('work', batchUploadForm.work);
                }
                if (batchUploadForm.autoAnalyze) {
                    formData.append('concurrency', String(clampInt(batchUploadForm.concurrency, 1, maxConc, batchDefaults.value.concurrency)));
                    formData.append('rpm', String(clampInt(batchUploadForm.rpm, 0, 600, batchDefaults.value.rpm)));
                }

                const res = await apiFetch('api/images/batch-upload', { method: 'POST', body: formData });
                const data = await res.json();
                if (data.success) {
                    batchTaskId.value = data.task_id;
                    batchTaskStatus.value = 'queued';
                    batchTaskTotal.value = Number(data.total || 0);
                    batchTaskProcessed.value = 0;
                    batchTaskSuccess.value = 0;
                    batchTaskFailed.value = 0;
                    batchTaskAnalyzed.value = 0;
                    batchTaskCurrentFile.value = '';
                    batchTaskPhase.value = '';
                    batchTaskPaused.value = false;
                    batchTaskCancelRequested.value = false;
                    batchTaskAutoAnalyze.value = Boolean(batchUploadForm.autoAnalyze);
                    batchTaskEta.value = 0;
                    batchTaskRateLimited.value = 0;
                    batchTaskRetried.value = 0;
                    batchTaskConcurrency.value = Number(data.concurrency || 0);
                    batchTaskRpm.value = Number(data.rpm || 0);
                    batchTaskResults.value = [];
                    startBatchStatusPoll();
                } else {
                    batchUploadError.value = data.error || t('pages.dashboard.alerts.upload_failed', 'Upload failed.');
                }
            } catch (e) {
                batchUploadError.value = t('pages.dashboard.alerts.upload_error', 'Upload error.');
            } finally {
                batchUploading.value = false;
            }
        };
        // 打开「批量重新识别」弹窗：与批量导入共用同一个弹窗骨架，靠 batchMode 区分表单
        const openBatchReanalyzeModal = (target, scope = 'library') => {
            batchMode.value = 'reanalyze';
            batchUploadOpen.value = true;
            batchUploadError.value = null;
            batchDragActive.value = false;
            batchFiles.value = [];
            batchPreviews.value = [];
            batchTaskId.value = null;
            batchTaskStatus.value = null;
            const isPending = scope === 'pending';
            const hasSelection = isPending
                ? pendingSelectedImages.value.size > 0
                : selectedImages.value.size > 0;
            // 张数先归零并进入「统计中」，避免上一次的旧数字先闪一下
            reanalyzeScan.value = {
                total: 0, missing: 0, no_desc: 0,
                pending_total: 0, pending_missing: 0, pending_no_desc: 0,
                max_items: 5000,
            };
            reanalyzeScanning.value = true;
            // 调用方明确点名了档位就当作「用户已选」，不再自动挪走
            reanalyzeTargetTouched.value = Boolean(target);
            reanalyzeSwitchFrom.value = '';
            reanalyzeSwitchTo.value = '';
            Object.assign(reanalyzeForm, {
                scope: isPending ? 'pending' : 'library',
                target: target || (hasSelection ? 'selected' : 'missing'),
                overwrite: false,
                limit: 0,
                concurrency: batchDefaults.value.concurrency,
                rpm: batchDefaults.value.rpm,
            });
            fetchReanalyzeScan();
        };

        const submitBatchReanalyze = async () => {
            if (reanalyzePlannedCount.value === 0) return;
            batchUploading.value = true;
            batchUploadError.value = null;
            try {
                const maxConc = batchDefaults.value.max_concurrency || 16;
                const payload = {
                    scope: reanalyzeForm.scope,
                    target: reanalyzeForm.target,
                    overwrite: Boolean(reanalyzeForm.overwrite),
                    limit: clampInt(reanalyzeForm.limit, 0, 100000, 0),
                    concurrency: clampInt(reanalyzeForm.concurrency, 1, maxConc, batchDefaults.value.concurrency),
                    rpm: clampInt(reanalyzeForm.rpm, 0, 600, batchDefaults.value.rpm),
                };
                if (reanalyzeForm.target === 'selected') {
                    if (reanalyzeForm.scope === 'pending') {
                        payload.ids = Array.from(pendingSelectedImages.value);
                    } else {
                        payload.hashes = Array.from(selectedImages.value);
                    }
                }
                const res = await apiFetch('api/images/batch-reanalyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (data.success) {
                    batchTaskId.value = data.task_id;
                    batchTaskStatus.value = 'queued';
                    batchTaskTotal.value = Number(data.total || 0);
                    batchTaskProcessed.value = 0;
                    batchTaskSuccess.value = 0;
                    batchTaskFailed.value = 0;
                    batchTaskAnalyzed.value = 0;
                    batchTaskCurrentFile.value = '';
                    batchTaskPhase.value = '';
                    batchTaskPaused.value = false;
                    batchTaskCancelRequested.value = false;
                    batchTaskAutoAnalyze.value = true;
                    batchTaskEta.value = 0;
                    batchTaskRateLimited.value = 0;
                    batchTaskRetried.value = 0;
                    batchTaskConcurrency.value = Number(data.concurrency || 0);
                    batchTaskRpm.value = Number(data.rpm || 0);
                    batchTaskResults.value = [];
                    startBatchStatusPoll();
                } else {
                    batchUploadError.value = data.error || t('pages.dashboard.alerts.reanalyze_failed', 'Re-analyze failed.');
                }
            } catch (e) {
                batchUploadError.value = t('pages.dashboard.alerts.reanalyze_failed', 'Re-analyze failed.');
            } finally {
                batchUploading.value = false;
            }
        };

        // 弹窗底部按钮统一入口，按当前模式分发
        const submitBatchModal = () => (
            batchMode.value === 'reanalyze' ? submitBatchReanalyze() : submitBatchUpload()
        );

        const startBatchStatusPoll = () => {
            stopBatchStatusPoll();
            batchPollInterval = setInterval(async () => {
                if (!batchTaskId.value) return;
                try {
                    const res = await apiFetch('api/images/batch-upload-status?task_id=' + batchTaskId.value);
                    const data = await res.json();
                    if (!data.success) return;
                    applyBatchStatus(data);
                    if (['completed', 'failed', 'cancelled'].includes(data.status)) {
                        stopBatchStatusPoll();
                        if (data.status === 'failed') {
                            batchUploadError.value = data.error || t('pages.dashboard.alerts.batch_import_failed', 'Batch import failed.');
                        }
                        if (data.status !== 'failed' || Number(data.success_count || 0) > 0) {
                            // 待审核范围的重识别不碰库里的图，刷新审核区就够了
                            if (batchMode.value === 'reanalyze' && reanalyzeForm.scope === 'pending') {
                                fetchPendingImages(pendingCurrentPage.value || 1);
                                fetchPendingStats();
                            } else {
                                fetchImages(1);
                                fetchStats();
                            }
                            fetchWorks();
                        }
                    }
                } catch (e) {
                    console.error('Batch status poll error:', e);
                }
            }, 1000);
        };

        const controlBatchTask = async (action) => {
            if (!batchTaskId.value || batchControlBusy.value) return;
            if (action === 'cancel' && !confirm(t('pages.dashboard.batch.cancel_confirm', 'Stop this import? Already imported items are kept.'))) {
                return;
            }
            batchControlBusy.value = true;
            try {
                const res = await apiFetch('api/images/batch-upload-control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: batchTaskId.value, action }),
                });
                const data = await res.json();
                if (data.success) {
                    applyBatchStatus(data);
                    if (action === 'resume') startBatchStatusPoll();
                } else {
                    showAlert(data.error || t('pages.dashboard.batch.control_failed', 'Action failed.'), 'error');
                }
            } catch (e) {
                showAlert(t('pages.dashboard.batch.control_failed', 'Action failed.'), 'error');
            } finally {
                batchControlBusy.value = false;
            }
        };

        const resetBatchUpload = () => {
            stopBatchStatusPoll();
            batchTaskId.value = null;
            batchTaskStatus.value = null;
            batchTaskProcessed.value = 0;
            batchTaskSuccess.value = 0;
            batchTaskFailed.value = 0;
            batchTaskAnalyzed.value = 0;
            batchTaskCurrentFile.value = '';
            batchTaskPhase.value = '';
            batchTaskPaused.value = false;
            batchTaskCancelRequested.value = false;
            batchTaskEta.value = 0;
            batchTaskRateLimited.value = 0;
            batchTaskRetried.value = 0;
            batchTaskResults.value = [];
            batchDragActive.value = false;
            batchUploadError.value = null;
            if (batchMode.value === 'reanalyze') {
                reanalyzeTargetTouched.value = false;
                reanalyzeSwitchFrom.value = '';
                reanalyzeSwitchTo.value = '';
                fetchReanalyzeScan();
            } else {
                clearBatchFiles();
            }
        };

        const handleFileSelect = (e) => {
            const file = e.target.files[0];
            if (file && file.type.startsWith('image/')) {
                if (uploadPreviewUrl.value) URL.revokeObjectURL(uploadPreviewUrl.value);
                uploadFile.value = file;
                uploadPreviewUrl.value = URL.createObjectURL(file);
                uploadError.value = null;
                uploadForm.scene = '';
                analysisScenes.value = [];
            }
        };

        const submitUpload = async () => {
            if (!uploadFile.value) return;
            uploading.value = true;
            try {
                const base64Data = await fileToBase64(uploadFile.value);
                const uploadRes = await apiFetch('api/images/upload', {
                    method: 'POST',
                    body: JSON.stringify({
                        base64: base64Data,
                        filename: uploadFile.value.name,
                        category: uploadForm.emotion,
                        tags: uploadForm.tags,
                        scene: uploadForm.scene,
                        desc: uploadForm.desc,
                        overlay_text: uploadForm.overlay_text || '',
                        character: uploadForm.character || '',
                        work: uploadForm.work || '',
                    }),
                });
                const uploadData = await uploadRes.json();
                if (!uploadData.success || !uploadData.hash) {
                    uploadError.value = uploadData.error || t('pages.dashboard.alerts.upload_failed', 'Upload failed.');
                    return;
                }
                closeUploadModal();
                fetchImages(1);
                fetchStats();
            } catch (e) {
                uploadError.value = t('pages.dashboard.alerts.upload_error', 'Upload error.');
            } finally {
                uploading.value = false;
            }
        };

        const useImageAnalyzer = () => {
            const isAnalyzing = ref(false);

            // 通用请求。后端支持三种定位方式：hash（已入库）、pending_id（待审核）、base64（新上传）；
            // 额外带上 work / character 会作为已知信息写进提示词，明显提高识别准确率。
            const analyzePayload = async (payload) => {
                isAnalyzing.value = true;
                try {
                    const res = await apiFetch('api/analyze', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload || {}),
                    });
                    const data = await res.json();
                    if (!data.success) {
                        throw new Error(data.error || t('pages.dashboard.alerts.analyze_failed', 'Analyze failed.'));
                    }
                    return data;
                } finally {
                    isAnalyzing.value = false;
                }
            };

            const analyze = async (file) => {
                if (!file) {
                    throw new Error(t('pages.dashboard.alerts.select_image_first', 'Select an image first.'));
                }

                isAnalyzing.value = true;
                console.log('[Analyzer] Start analyzing image:', file.name);

                try {
                    const base64Data = await fileToBase64(file);
                    const data = await analyzePayload({ base64: base64Data });
                    console.log('[Analyzer] Analyze success:', data);
                    return data;
                } catch (e) {
                    console.error('[Analyzer] Analyze failed:', e);
                    throw e;
                } finally {
                    isAnalyzing.value = false;
                }
            };

            const applyToForm = (data, form, categories = []) => {
                const result = { filled: false, fields: [] };

                if (data.category) {
                    const exists = categories.some(e => e.key === data.category);
                    if (exists) {
                        form.emotion = data.category;
                        result.fields.push('category');
                    } else if (categories.length > 0) {
                        console.warn('[Analyzer] Category missing, using default:', data.category);
                        form.emotion = categories[0].key;
                        result.fields.push('category');
                    }
                }

                if (data.tags && data.tags.length > 0) {
                    const existingTags = form.tags ? form.tags.split(',').map(t => t.trim()).filter(t => t) : [];
                    const newTags = data.tags.filter(t => !existingTags.includes(t));
                    if (newTags.length > 0) {
                        form.tags = [...existingTags, ...newTags].join(', ');
                        result.fields.push('tags');
                    }
                }

                if (Array.isArray(data.scenes) && data.scenes.length > 0) {
                    form.scene = parseSceneList(data.scenes.join(', ')).join(', ');
                    result.fields.push('scenes');
                }

                if (data.description && !form.desc) {
                    form.desc = data.description;
                    result.fields.push('desc');
                }
                if (data.overlay_text && !form.overlay_text) {
                    form.overlay_text = data.overlay_text;
                    result.fields.push('overlay_text');
                }

                result.filled = result.fields.length > 0;
                console.log('[Analyzer] Form fill result:', result);
                return result;
            };

            // 重新识别用：按字段映射写进任意表单。默认 overwrite=true 覆盖旧值，
            // 因为「重新识别」本来就是要拿新结果替换旧结果；写进表单不等于入库，
            // 还得用户点保存。返回被改动的字段名，方便如实告诉用户动了什么。
            const applyToMappedForm = (data, form, fields, options = {}) => {
                const { categories: catList = [], overwrite = true } = options;
                const filled = [];
                const put = (key, value, label) => {
                    if (!key) return;
                    const next = String(value == null ? '' : value);
                    if (!next) return;
                    const current = String(form[key] == null ? '' : form[key]);
                    if (!overwrite && current.trim()) return;
                    if (current === next) return;
                    form[key] = next;
                    filled.push(label);
                };
                if (data.category && fields.category) {
                    const known = !catList.length || catList.some((c) => c.key === data.category);
                    if (known) {
                        put(fields.category, data.category, t('pages.dashboard.fields.category', 'Category'));
                    } else {
                        console.warn('[Analyzer] Unknown category, kept as-is:', data.category);
                    }
                }
                if (Array.isArray(data.tags) && data.tags.length) {
                    put(fields.tags, data.tags.join(', '), t('pages.dashboard.fields.tags', 'Tags'));
                }
                if (Array.isArray(data.scenes) && data.scenes.length) {
                    const scenes = parseSceneList(data.scenes.join(', ')).join(', ');
                    put(fields.scenes, scenes, t('pages.dashboard.fields.scenes', 'Scenes'));
                }
                put(fields.desc, data.description, t('pages.dashboard.fields.description', 'Description'));
                put(fields.overlay_text, data.overlay_text, t('pages.dashboard.fields.overlay_text', '图上文字'));
                return filled;
            };

            return {
                isAnalyzing,
                analyze,
                analyzePayload,
                applyToForm,
                applyToMappedForm,
            };
        };

        const imageAnalyzer = useImageAnalyzer();
        const analyzing = imageAnalyzer.isAnalyzing;

        const analyzeImage = async () => {
            uploadError.value = null;

            try {
                const data = await imageAnalyzer.analyze(uploadFile.value);
                analysisScenes.value = Array.isArray(data.scenes) ? data.scenes : [];
                const result = imageAnalyzer.applyToForm(data, uploadForm, availableEmotions.value);

                if (!result.filled) {
                    uploadError.value = t('pages.dashboard.alerts.no_valid_info', 'No valid information recognized.');
                }
            } catch (e) {
                uploadError.value = e.message || t('pages.dashboard.alerts.analyze_failed', 'Analyze failed.');
            }
        };

        // ── 单张重新识别（库详情 / 审核区编辑）──────────────────────
        // 只把结果填进表单，必须由人核对后点保存才写库，不会静默覆盖手工标注。
        const singleReanalyze = reactive({ text: '', tone: 'info' });

        const resetSingleReanalyze = () => {
            singleReanalyze.text = '';
            singleReanalyze.tone = 'info';
        };

        const REANALYZE_FIELDS_LIBRARY = {
            category: 'category', tags: 'tags', scenes: 'scene',
            desc: 'desc', overlay_text: 'overlay_text',
        };
        const REANALYZE_FIELDS_PENDING = {
            category: 'category', tags: 'tagsText', scenes: 'scenesText',
            desc: 'desc', overlay_text: 'overlay_text',
        };

        const knownFactsLabel = (known) => {
            const parts = [];
            if (known && known.work) parts.push(known.work);
            if (known && known.character) parts.push(known.character);
            return parts.join(' / ');
        };

        const runSingleReanalyze = async (payload, form, fieldMap) => {
            resetSingleReanalyze();
            try {
                const data = await imageAnalyzer.analyzePayload(payload);
                const filled = imageAnalyzer.applyToMappedForm(data, form, fieldMap, {
                    categories: categories.value,
                });
                let text = filled.length
                    ? t('pages.dashboard.reanalyze.single_filled', '识别结果已填进表单：{fields}。核对后点保存才会写入，直接关掉不会有任何改动。').replace('{fields}', filled.join('、'))
                    : t('pages.dashboard.reanalyze.single_same', '识别结果和表单里现有内容一致，没有需要改的字段。');
                const hints = knownFactsLabel(data.known_facts);
                if (hints) {
                    text += t('pages.dashboard.reanalyze.single_known', '（本次已把「{hints}」作为已知信息告诉模型）').replace('{hints}', hints);
                }
                singleReanalyze.text = text;
                singleReanalyze.tone = 'info';
            } catch (e) {
                singleReanalyze.text = e.message || t('pages.dashboard.alerts.analyze_failed', 'Analyze failed.');
                singleReanalyze.tone = 'error';
            }
        };

        // 库详情：先切到编辑态再填，用户能直接看到改了哪些字段
        const reanalyzePreviewItem = async () => {
            const item = previewItem.value;
            if (!item || !item.hash || analyzing.value) return;
            if (!isEditing.value) startEdit();
            await runSingleReanalyze(
                { hash: item.hash, work: editForm.work || '', character: editForm.character || '' },
                editForm,
                REANALYZE_FIELDS_LIBRARY,
            );
        };

        const reanalyzePendingItem = async () => {
            if (!pendingEditId.value || analyzing.value) return;
            await runSingleReanalyze(
                {
                    pending_id: pendingEditId.value,
                    work: pendingEditForm.work || '',
                    character: pendingEditForm.character || '',
                },
                pendingEditForm,
                REANALYZE_FIELDS_PENDING,
            );
        };

        // ── 识别失败检测：先摆清单，再决定手写还是整批重跑 ─────────────────
        const missingDescIsPending = computed(() => missingDescScope.value === 'pending');

        // 表情库用 hash 定位，待审核用自增 id，加前缀避免两边撞键
        const missingDescKey = (item) => (
            missingDescIsPending.value ? `p:${item?.id ?? ''}` : `l:${item?.hash ?? ''}`
        );

        const missingDescRemaining = computed(
            () => Math.max(0, Number(missingDescCounts[missingDescScope.value] || 0)),
        );

        const missingDescPageCount = computed(
            () => Math.max(1, Math.ceil(missingDescTotal.value / missingDescPageSize)),
        );

        // 只取张数填标签页角标。这里刻意不复用 fetchReanalyzeScan：那个会顺手改
        // reanalyzeForm 的档位，从这个窗口调用会把批量弹窗的表单搅乱。
        const fetchMissingDescCounts = async () => {
            try {
                const res = await apiFetch('api/images/reanalyze-scan');
                const data = await res.json();
                if (!data.success) return;
                missingDescCounts.library = Number(data.no_desc || 0);
                missingDescCounts.pending = Number(data.pending_no_desc || 0);
            } catch (e) {
                console.error('Missing description scan error:', e);
            }
        };

        const fetchMissingDesc = async (page = 1) => {
            missingDescLoading.value = true;
            missingDescError.value = null;
            try {
                const query = new URLSearchParams({
                    scope: missingDescScope.value,
                    page: String(page),
                    size: String(missingDescPageSize),
                });
                const res = await apiFetch(`api/images/missing-description?${query.toString()}`);
                const data = await res.json();
                if (!data.success) {
                    missingDescError.value = data.error
                        || t('pages.dashboard.missing_desc.load_failed', '清单没读出来，可能是后端或网络出了问题。');
                    return;
                }
                const items = Array.isArray(data.images) ? data.images : [];
                missingDescItems.value = items;
                missingDescTotal.value = Number(data.total || 0);
                missingDescScanned.value = Number(data.scanned || 0);
                missingDescMaxItems.value = Number(data.max_items || 5000);
                missingDescTruncated.value = Boolean(data.truncated);
                missingDescPage.value = Number(data.page || page) || 1;
                // 后端每次都重扫，返回的张数就是最新口径，直接盖掉本地计数
                missingDescCounts[missingDescScope.value] = missingDescTotal.value;
                items.forEach((item) => {
                    const key = missingDescKey(item);
                    if (missingDescDrafts[key] === undefined) missingDescDrafts[key] = '';
                    loadImageData(item.hash);
                });
            } catch (e) {
                missingDescError.value = t('pages.dashboard.missing_desc.load_failed', '清单没读出来，可能是后端或网络出了问题。');
                console.error('Missing description list error:', e);
            } finally {
                missingDescLoading.value = false;
            }
        };

        const openMissingDescModal = (scope = 'library') => {
            missingDescScope.value = scope === 'pending' ? 'pending' : 'library';
            missingDescOpen.value = true;
            missingDescItems.value = [];
            missingDescTotal.value = 0;
            missingDescScanned.value = 0;
            missingDescTruncated.value = false;
            missingDescPage.value = 1;
            missingDescError.value = null;
            [missingDescDrafts, missingDescSaving, missingDescDone].forEach((table) => {
                Object.keys(table).forEach((key) => { delete table[key]; });
            });
            missingDescFixed.library = 0;
            missingDescFixed.pending = 0;
            fetchMissingDescCounts();
            fetchMissingDesc(1);
        };

        const closeMissingDescModal = () => {
            missingDescOpen.value = false;
            // 这个窗口里写进库的改动，要反映到背后的列表和统计上
            if (missingDescFixed.library > 0) {
                fetchImages(currentPage.value || 1);
                fetchStats();
            }
            if (missingDescFixed.pending > 0) {
                fetchPendingImages(pendingCurrentPage.value || 1);
                fetchPendingStats();
            }
            missingDescFixed.library = 0;
            missingDescFixed.pending = 0;
        };

        // 切标签页只换清单，保留已填的草稿：两边的键带前缀，不会串
        const switchMissingDescScope = (scope) => {
            const next = scope === 'pending' ? 'pending' : 'library';
            if (next === missingDescScope.value) return;
            missingDescScope.value = next;
            missingDescItems.value = [];
            missingDescTotal.value = 0;
            missingDescTruncated.value = false;
            missingDescPage.value = 1;
            missingDescError.value = null;
            fetchMissingDesc(1);
        };

        const missingDescGoPage = (page) => {
            if (missingDescLoading.value) return;
            const next = Math.min(Math.max(1, Number(page) || 1), missingDescPageCount.value);
            if (next === missingDescPage.value) return;
            fetchMissingDesc(next);
        };

        const saveMissingDesc = async (item) => {
            const key = missingDescKey(item);
            if (missingDescSaving[key]) return;
            const text = String(missingDescDrafts[key] || '').trim();
            if (!text) {
                showAlert(
                    t('pages.dashboard.missing_desc.empty_draft', '描述还是空的：自己写一句，或者先点「识别这张」。'),
                    'error',
                );
                return;
            }
            missingDescSaving[key] = true;
            try {
                const isPending = missingDescIsPending.value;
                const res = await apiFetch(isPending ? 'api/pending/update' : 'api/images/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(
                        isPending ? { id: item.id, desc: text } : { hash: item.hash, desc: text },
                    ),
                });
                const data = await res.json();
                if (!data.success) {
                    showAlert(data.error || t('pages.dashboard.alerts.save_failed', 'Save failed.'), 'error');
                    return;
                }
                const scope = isPending ? 'pending' : 'library';
                if (!missingDescDone[key]) {
                    missingDescFixed[scope] += 1;
                    missingDescCounts[scope] = Math.max(0, (missingDescCounts[scope] || 0) - 1);
                }
                missingDescDone[key] = true;
                item.desc = text;
                showAlert(t('pages.dashboard.missing_desc.saved', '描述已写入。'), 'success');
                // 草稿全落库了就把窗口解锁，点外面能正常关
                if (!missingDescHasDraft()) releaseModal('missingDesc');
            } catch (e) {
                showAlert(t('pages.dashboard.alerts.save_failed', 'Save failed.'), 'error');
            } finally {
                missingDescSaving[key] = false;
            }
        };

        // 单张识别只把结果填进输入框，仍然要人点保存才写库；
        // 顺手把已填的作品 / 角色作为已知信息带给模型，识别准确率会明显好一些。
        const analyzeMissingDescRow = async (item) => {
            if (!item || analyzing.value) return;
            const key = missingDescKey(item);
            try {
                const payload = missingDescIsPending.value
                    ? { pending_id: item.id }
                    : { hash: item.hash };
                if (item.work) payload.work = item.work;
                if (item.character) payload.character = item.character;
                const data = await imageAnalyzer.analyzePayload(payload);
                const desc = String(data.description || '').trim();
                if (!desc) {
                    showAlert(
                        t('pages.dashboard.missing_desc.analyze_empty', '这次识别没给出描述。可以自己写一句，或者过一会儿再试。'),
                        'error',
                    );
                    return;
                }
                missingDescDrafts[key] = desc;
                touchModal('missingDesc');
                showAlert(
                    t('pages.dashboard.missing_desc.analyze_filled', '识别结果已填进输入框，点「仅保存」才会写入。'),
                    'success',
                );
            } catch (e) {
                showAlert(e.message || t('pages.dashboard.alerts.analyze_failed', 'Analyze failed.'), 'error');
            }
        };

        // 整批交给批量重新识别：换成那个窗口后，进度、并发和限速都能看见
        const startMissingDescBatch = () => {
            const scope = missingDescScope.value;
            closeMissingDescModal();
            openBatchReanalyzeModal('no_desc', scope);
        };

        const openEmotionsModal = () => {
            emotionsOpen.value = true;
            fetchEmotions();
        };

        const closeEmotionsModal = () => {
            emotionsOpen.value = false;
        };

        const addEmotion = async () => {
            const key = String(newEmotion.key || '').trim();
            if (!key) return;
            addingEmotion.value = true;
            try {
                const newCat = {
                    key,
                    name: String(newEmotion.name || '').trim(),
                    desc: String(newEmotion.desc || '').trim(),
                };
                const currentList = [...availableEmotions.value];
                const existingIdx = currentList.findIndex((c) => c.key === newCat.key);
                if (existingIdx >= 0) {
                    if (!await showConfirm(
                        t('pages.dashboard.confirm.update_existing_category', 'Category {key} already exists. Update it?')
                            .replace('{key}', newCat.key)
                    )) {
                        addingEmotion.value = false;
                        return;
                    }
                    currentList[existingIdx] = newCat;
                } else {
                    currentList.push(newCat);
                }

                const res = await apiFetch('api/categories', {
                    method: 'POST',
                    body: JSON.stringify({ categories: currentList }),
                });
                const data = await res.json();

                if (data.success) {
                    await fetchEmotions();
                    await fetchImages(1);
                    newEmotion.key = '';
                    newEmotion.name = '';
                    newEmotion.desc = '';
                    releaseModal('emotions');
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.add_failed', 'Add failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            } finally {
                addingEmotion.value = false;
            }
        };

        const characterLabel = (key) => {
            if (!key) return t('pages.dashboard.characters.unassigned', '未分配');
            const found = characters.value.find((item) => item.key === key);
            return found ? found.name : key;
        };

        const openCharactersModal = () => {
            charactersOpen.value = true;
        };

        const closeCharactersModal = () => {
            charactersOpen.value = false;
        };

        const addCharacter = async () => {
            const key = String(newCharacter.key || '').trim().toLowerCase();
            if (!key) return;
            addingCharacter.value = true;
            try {
                const currentList = characters.value.map((item) => ({
                    key: item.key,
                    name: item.name,
                    desc: item.desc || '',
                }));
                const existingIdx = currentList.findIndex((item) => item.key === key);
                const next = { key, name: String(newCharacter.name || '').trim() || key, desc: '' };
                if (existingIdx >= 0) currentList[existingIdx] = next;
                else currentList.push(next);
                const res = await apiFetch('api/characters', {
                    method: 'POST',
                    body: JSON.stringify({ characters: currentList }),
                });
                const data = await res.json();
                if (data.success) {
                    characters.value = data.characters || currentList;
                    newCharacter.key = '';
                    newCharacter.name = '';
                    releaseModal('characters');
                    await fetchImages(currentPage.value);
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.add_failed', 'Add failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            } finally {
                addingCharacter.value = false;
            }
        };

        const deleteCharacter = async (item) => {
            if (!item?.key) return;
            if (!await showConfirm(
                t('pages.dashboard.confirm.delete_character', '确定删除角色 {key}？表情包文件会保留，只去掉角色标记。')
                    .replace('{key}', item.key)
            )) return;
            deletingCharacterKey.value = item.key;
            try {
                const res = await apiFetch('api/characters/delete', {
                    method: 'POST',
                    body: JSON.stringify({ key: item.key }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    if (selectedCharacter.value === item.key) selectedCharacter.value = '';
                    await fetchImages(1);
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.delete_failed', 'Delete failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            } finally {
                deletingCharacterKey.value = '';
            }
        };

        const deleteEmotion = async (cat) => {
            if (!cat?.key) return;
            if (!await showConfirm(
                t(
                    'pages.dashboard.confirm.delete_category',
                    'Delete category {key}? Images in this category will be deleted permanently.'
                ).replace('{key}', cat.key)
            ))
                return;
            deletingEmotionKey.value = cat.key;
            try {
                const res = await apiFetch('api/categories/delete', {
                    method: 'POST',
                    body: JSON.stringify({ key: cat.key }),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    if (selectedCategory.value === cat.key) selectedCategory.value = '';
                    if (editForm.category === cat.key) editForm.category = '';
                    if (previewItem.value && previewItem.value.category === cat.key)
                        previewItem.value.category = 'unknown';
                    fetchEmotions();
                    refreshView();
                } else {
                    showAlert(data.error || t('pages.dashboard.alerts.delete_failed', 'Delete failed.'));
                }
            } catch (e) {
                showAlert(`${t('pages.dashboard.alerts.action_failed', 'Action failed')}: ${e.message}`);
            } finally {
                deletingEmotionKey.value = '';
            }
        };

        const formatDate = (timestamp) => {
            if (!timestamp) return t('pages.dashboard.messages.unknown', 'Unknown');
            const date = new Date(timestamp * 1000);
            return date.toLocaleString(resolveUiLocale(), {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
            });
        };

        // v5 元数据展示辅助
        const formatBytes = (bytes) => {
            if (!bytes && bytes !== 0) return '';
            const n = Number(bytes);
            if (!Number.isFinite(n) || n < 0) return '';
            if (n < 1024) return n + ' B';
            if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
            return (n / 1024 / 1024).toFixed(1) + ' MB';
        };

        const formatAddMethod = (method) => {
            const map = {
                auto: t('pages.dashboard.fields.add_method_auto', '自动收集'),
                manual: t('pages.dashboard.fields.add_method_manual', '手动入库'),
                llm: t('pages.dashboard.fields.add_method_llm', 'LLM 入库'),
                api: t('pages.dashboard.fields.add_method_api', 'API 入库'),
            };
            return map[method] || t('pages.dashboard.fields.add_method_unknown', '未知');
        };

        const syncThemeFromContext = (context = null) => {
            const nextContext = context || bridge?.getContext?.() || {};
            if (typeof nextContext?.isDark === 'boolean') {
                contextIsDark.value = nextContext.isDark;
            }
            applyTheme();
            localeVersion.value += 1;
            updateDocumentMeta();
        };

        let resizeTimer = null;
        const handleResize = () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
                updatePageSize();
                if (activeSection.value === 'library') {
                    fetchImages(1);
                }
            }, 300);
        };
        // ── 外部表情包源 ────────────────────────────────────────────────
        // 三种来源共用一套「预检 → 确认 → 后台导入」流程：预检只读清单不落库，
        // 用户看清张数/体积/分类后再决定导不导，避免一按就往库里灌几千张。
        const SOURCE_TERMINAL_STATUS = ['completed', 'failed', 'cancelled'];

        const sourceTypeLabel = (type) => {
            const key = String(type || '').toLowerCase();
            if (key === 'github') return t('pages.dashboard.sources.type_github', 'GitHub 仓库');
            if (key === 'http_json' || key === 'http') return t('pages.dashboard.sources.type_http', 'HTTP 目录');
            return t('pages.dashboard.sources.type_pack', '本地表情包');
        };

        const sourceJobActive = computed(() => {
            const status = sourceJob.value?.status;
            return ['queued', 'running', 'paused'].includes(String(status || ''));
        });

        const sourceProgressPercent = computed(() => {
            const total = Number(sourceJob.value?.total || 0);
            if (!total) return 0;
            return Math.min(100, Math.round((Number(sourceJob.value?.processed || 0) / total) * 100));
        });

        const sourceEtaText = computed(() => {
            const secs = Math.round(Number(sourceJob.value?.eta_seconds || 0));
            if (secs <= 0) return '';
            if (secs < 60) return secs + 's';
            const mins = Math.floor(secs / 60);
            if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
            return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
        });

        const sourcePhaseText = computed(() => {
            switch (String(sourceJob.value?.phase || '')) {
                case 'inspecting': return t('pages.dashboard.sources.phase_inspecting', '读取清单中');
                case 'importing': return t('pages.dashboard.sources.phase_importing', '入库中');
                case 'finalizing': return t('pages.dashboard.sources.phase_finalizing', '收尾中');
                case 'done': return t('pages.dashboard.sources.phase_done', '已结束');
                default: return t('pages.dashboard.sources.phase_queued', '排队中');
            }
        });

        const sourceStatusLabel = computed(() => {
            switch (String(sourceJob.value?.status || '')) {
                case 'running': return t('pages.dashboard.sources.running', '正在导入…');
                case 'paused': return t('pages.dashboard.sources.paused', '已暂停');
                case 'completed': return t('pages.dashboard.sources.completed', '导入完成');
                case 'cancelled': return t('pages.dashboard.sources.cancelled', '已停止');
                case 'failed': return t('pages.dashboard.sources.failed', '导入失败');
                default: return t('pages.dashboard.sources.queued', '排队中');
            }
        });

        const sourceJobErrors = computed(() => {
            const errors = sourceJob.value?.errors;
            return Array.isArray(errors) ? errors.slice(0, 30) : [];
        });

        // 后端的报错是英文技术描述（方便对照上游），这里统一包一层中文说明
        const sourceErrorText = (raw) => {
            const detail = String(raw || '').trim();
            const prefix = t('pages.dashboard.sources.error_prefix', '外部源操作失败');
            if (!detail) return prefix;
            return prefix + t('pages.dashboard.sources.error_sep', '：') + detail;
        };

        const stopSourcePoll = () => {
            if (sourcePollInterval) clearInterval(sourcePollInterval);
            sourcePollInterval = null;
        };

        const pollSourceJob = async () => {
            const jobId = sourceJob.value?.job_id;
            if (!jobId) { stopSourcePoll(); return; }
            try {
                const res = await apiFetch('api/sources/jobs?job_id=' + encodeURIComponent(jobId));
                const data = await res.json();
                if (!data.success) return;
                sourceJob.value = data.job || sourceJob.value;
                if (!SOURCE_TERMINAL_STATUS.includes(String(sourceJob.value?.status || ''))) return;
                stopSourcePoll();
                if (sourceJob.value?.status === 'failed') {
                    sourceError.value = sourceErrorText(
                        sourceJobErrors.value[0]?.error || sourceJobErrors.value[0] || '',
                    );
                }
                fetchSources();
                // 有图进库/进审核区就把背后的列表刷新，别让用户以为没导进来
                if (Number(sourceJob.value?.imported || 0) > 0) {
                    refreshView();
                    fetchWorks();
                }
                if (Number(sourceJob.value?.pending || 0) > 0) {
                    fetchPendingImages(1);
                    fetchPendingStats();
                }
            } catch (e) {
                console.error('Source job poll error:', e);
            }
        };

        const startSourcePoll = () => {
            stopSourcePoll();
            sourcePollInterval = setInterval(pollSourceJob, 1000);
        };

        const resetSourceCategoryMap = (categoryNames) => {
            Object.keys(sourceCategoryMap).forEach((key) => { delete sourceCategoryMap[key]; });
            (Array.isArray(categoryNames) ? categoryNames : []).forEach((name) => {
                const key = String(name || '').trim();
                if (!key) return;
                // 源里的分类名正好也是本地分类时直接对上，其余留空交给后端自动对齐
                sourceCategoryMap[key] = categories.value.some((cat) => cat.key === key) ? key : '';
            });
        };

        const applySourceInspection = (inspection) => {
            sourceInspection.value = inspection || null;
            sourceError.value = null;
            resetSourceCategoryMap(inspection?.categories);
        };

        const fetchSources = async () => {
            sourceLoading.value = true;
            try {
                const res = await apiFetch('api/sources');
                const data = await res.json();
                if (!data.success) {
                    sourceError.value = sourceErrorText(data.error);
                    return;
                }
                sourceList.value = Array.isArray(data.sources) ? data.sources : [];
                if (data.defaults) sourceDefaults.value = data.defaults;
                // 刷新页面丢了 job_id 也能重新接上还在跑的导入
                if (data.job) {
                    sourceJob.value = data.job;
                    if (sourceJobActive.value) startSourcePoll();
                }
            } catch (e) {
                sourceError.value = sourceErrorText(
                    t('pages.dashboard.sources.list_failed', '源列表没读出来，可能是后端或网络出了问题'),
                );
                console.error('Source list error:', e);
            } finally {
                sourceLoading.value = false;
            }
        };

        const openSourceModal = () => {
            sourceOpen.value = true;
            sourceError.value = null;
            sourceInspection.value = null;
            sourceFile.value = null;
            sourceUploadedPath.value = '';
            sourceJob.value = null;
            sourceBusy.value = '';
            resetSourceCategoryMap([]);
            Object.assign(sourceForm, {
                endpoint: '', github: '', review: false,
                scope_mode: 'public', origin_target: '',
                assign_character: false, character: '',
            });
            fetchSources().then(() => {
                // 开了内容过滤时后端会强制走审核，这里跟着勾上，别让复选框和实际行为不一致
                sourceForm.review = Boolean(sourceDefaults.value.review);
            });
        };

        const closeSourceModal = () => {
            sourceOpen.value = false;
            stopSourcePoll();
            releaseModal('source');
        };

        const runSourceInspect = async (payload) => {
            sourceBusy.value = 'inspect';
            sourceError.value = null;
            try {
                const res = await apiFetch('api/sources/inspect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!data.success) {
                    sourceError.value = sourceErrorText(data.error);
                    return false;
                }
                applySourceInspection(data.inspection);
                return true;
            } catch (e) {
                sourceError.value = sourceErrorText(
                    t('pages.dashboard.sources.inspect_failed', '预检没跑完，可能是网络或源不可达'),
                );
                console.error('Source inspect error:', e);
                return false;
            } finally {
                sourceBusy.value = '';
            }
        };

        const handleSourceFile = async (event) => {
            const file = event?.target?.files?.[0];
            if (event?.target) event.target.value = '';
            if (!file) return;
            if (!/\.(zip|meme-pack)$/i.test(file.name)) {
                sourceError.value = t('pages.dashboard.sources.bad_archive', '只认 .zip 或 .meme-pack 压缩包。');
                return;
            }
            touchModal('source');
            sourceFile.value = file;
            sourceInspection.value = null;
            sourceUploadedPath.value = '';
            sourceBusy.value = 'upload';
            sourceError.value = null;
            try {
                const formData = new FormData();
                formData.append('file', file);
                const res = await apiFetch('api/sources/upload', { method: 'POST', body: formData });
                const data = await res.json();
                if (!data.success) {
                    sourceError.value = sourceErrorText(data.error);
                    sourceFile.value = null;
                    return;
                }
                sourceUploadedPath.value = String(data.path || '');
                applySourceInspection(data.inspection);
            } catch (e) {
                sourceError.value = sourceErrorText(
                    t('pages.dashboard.sources.upload_failed', '压缩包没传上去'),
                );
                sourceFile.value = null;
                console.error('Source upload error:', e);
            } finally {
                sourceBusy.value = '';
            }
        };

        const inspectGitHubSource = async () => {
            const repository = String(sourceForm.github || '').trim();
            if (!repository) {
                sourceError.value = t('pages.dashboard.sources.need_repo', '填 owner/repo，或者直接贴仓库链接。');
                return;
            }
            touchModal('source');
            sourceFile.value = null;
            sourceUploadedPath.value = '';
            await runSourceInspect({ source_type: 'github', repository });
        };

        const inspectExternalApi = async () => {
            const endpoint = String(sourceForm.endpoint || '').trim();
            if (!endpoint) {
                sourceError.value = t('pages.dashboard.sources.need_endpoint', '填一个返回表情包清单的 HTTPS 地址。');
                return;
            }
            touchModal('source');
            sourceFile.value = null;
            sourceUploadedPath.value = '';
            await runSourceInspect({ source_type: 'http_json', endpoint });
        };

        // 已登记/自动发现的源：预检时只回传描述符，敏感字段不出后端
        const sourceDescriptorPayload = (source) => {
            if (sourceUploadedPath.value) {
                return { source_type: 'meme_pack', path: sourceUploadedPath.value };
            }
            if (source) {
                return source.discovered
                    ? { source_type: source.source_type || 'meme_pack', path: source.endpoint }
                    : { source_id: source.source_id };
            }
            const type = String(sourceInspection.value?.source_type || '').toLowerCase();
            if (type === 'github') return { source_type: 'github', repository: String(sourceForm.github || '').trim() };
            if (type === 'http_json' || type === 'http') {
                return { source_type: 'http_json', endpoint: String(sourceForm.endpoint || '').trim() };
            }
            return {};
        };

        const inspectRegisteredSource = async (source) => {
            touchModal('source');
            sourceFile.value = null;
            sourceUploadedPath.value = '';
            await runSourceInspect(sourceDescriptorPayload(source));
        };

        const sourceImportOptions = () => {
            const options = { review: Boolean(sourceForm.review) };
            const map = {};
            Object.entries(sourceCategoryMap).forEach(([key, value]) => {
                const target = String(value || '').trim();
                if (target) map[key] = target;
            });
            if (Object.keys(map).length > 0) options.category_map = map;
            options.scope_mode = sourceForm.scope_mode === 'local' ? 'local' : 'public';
            if (options.scope_mode !== 'public') {
                const origin = String(sourceForm.origin_target || '').trim();
                if (origin) options.origin_target = origin;
            }
            if (sourceForm.assign_character) {
                const character = String(sourceForm.character || '').trim();
                if (character) {
                    options.character = character;
                    options.create_character = true;
                }
            }
            return options;
        };

        const launchSourceJob = async (url, payload) => {
            sourceBusy.value = 'import';
            sourceError.value = null;
            try {
                const res = await apiFetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (!data.success || !data.job) {
                    sourceError.value = sourceErrorText(data.error);
                    return;
                }
                sourceJob.value = data.job;
                startSourcePoll();
            } catch (e) {
                sourceError.value = sourceErrorText(
                    t('pages.dashboard.sources.import_failed', '导入没能开始'),
                );
                console.error('Source import error:', e);
            } finally {
                sourceBusy.value = '';
            }
        };

        const startSourceImport = async () => {
            if (!sourceInspection.value) return;
            const descriptor = sourceDescriptorPayload(null);
            if (Object.keys(descriptor).length === 0) {
                sourceError.value = t('pages.dashboard.sources.need_inspect', '先跑一次预检，确认要导的是哪一份。');
                return;
            }
            await launchSourceJob('api/sources/import', { ...descriptor, ...sourceImportOptions() });
        };

        const syncSource = async (source) => {
            if (!source) return;
            if (source.discovered) {
                await launchSourceJob('api/sources/import', {
                    source_type: source.source_type || 'meme_pack',
                    path: source.endpoint,
                    ...sourceImportOptions(),
                });
                return;
            }
            // 已登记的源沿用登记时存下的映射与开关，只要 source_id 就够
            await launchSourceJob('api/sources/sync', { source_id: source.source_id });
        };

        const cancelSourceJob = async () => {
            const jobId = sourceJob.value?.job_id;
            if (!jobId || sourceBusy.value) return;
            const ok = await showConfirm(
                t('pages.dashboard.sources.cancel_confirm', '确定停止这次导入吗？已经入库的图片会保留。'),
            );
            if (!ok) return;
            sourceBusy.value = 'control';
            try {
                await apiFetch('api/sources/jobs/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: jobId }),
                });
                await pollSourceJob();
            } catch (e) {
                showAlert(t('pages.dashboard.sources.control_failed', '操作失败。'), 'error');
            } finally {
                sourceBusy.value = '';
            }
        };

        const controlSourceJob = async (action) => {
            const jobId = sourceJob.value?.job_id;
            if (!jobId || sourceBusy.value) return;
            sourceBusy.value = 'control';
            try {
                const res = await apiFetch('api/sources/jobs/control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: jobId, action }),
                });
                const data = await res.json();
                if (data.job) sourceJob.value = data.job;
                if (!data.success) {
                    showAlert(
                        sourceErrorText(data.error || t('pages.dashboard.sources.control_failed', '操作失败。')),
                        'error',
                    );
                } else if (action === 'resume') {
                    startSourcePoll();
                }
            } catch (e) {
                showAlert(t('pages.dashboard.sources.control_failed', '操作失败。'), 'error');
            } finally {
                sourceBusy.value = '';
            }
        };

        const forgetSource = async (source) => {
            if (!source?.source_id) return;
            const ok = await showConfirm(
                t('pages.dashboard.sources.forget_confirm', '只是不再记录这个源，已经导进来的表情包不会被删。继续吗？'),
            );
            if (!ok) return;
            try {
                const res = await apiFetch('api/sources/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ source_id: source.source_id }),
                });
                const data = await res.json();
                if (!data.success) {
                    showAlert(sourceErrorText(data.error), 'error');
                    return;
                }
                showAlert(t('pages.dashboard.sources.forgotten', '已从源列表移除，表情包保持原样。'), 'success');
                fetchSources();
            } catch (e) {
                showAlert(sourceErrorText(''), 'error');
                console.error('Source delete error:', e);
            }
        };

        // 受保护弹窗登记表：顺序＝模板里的叠放顺序，Esc 先处理最上面那个
        const guardedModals = [
            { key: 'preview', open: previewOpen, close: closePreview },
            { key: 'upload', open: uploadOpen, close: closeUploadModal },
            { key: 'batchUpload', open: batchUploadOpen, close: closeBatchUploadModal },
            { key: 'emotions', open: emotionsOpen, close: closeEmotionsModal },
            { key: 'characters', open: charactersOpen, close: closeCharactersModal },
            { key: 'batchMove', open: batchMoveOpen, close: closeBatchMoveModal },
            { key: 'batchWork', open: batchWorkOpen, close: closeBatchWorkModal },
            { key: 'batchCharacter', open: batchCharacterOpen, close: closeBatchCharacterModal },
            { key: 'batchScope', open: batchScopeOpen, close: closeBatchScopeModal },
            { key: 'pendingEdit', open: pendingEditOpen, close: closePendingEdit },
            { key: 'missingDesc', open: missingDescOpen, close: closeMissingDescModal },
            { key: 'source', open: sourceOpen, close: closeSourceModal },
        ];
        // 每次重新打开都按「干净」算，别让上一次的输入痕迹一直把窗口锁着
        guardedModals.forEach((modal) => {
            watch(modal.open, (isOpen) => { if (isOpen) releaseModal(modal.key); });
        });
        const closeTopGuardedModal = () => {
            for (let i = guardedModals.length - 1; i >= 0; i -= 1) {
                const modal = guardedModals[i];
                if (!modal.open.value) continue;
                if (isModalDirty(modal.key)) refuseModalClose(modal.key);
                else modal.close();
                return true;
            }
            return false;
        };

        onMounted(() => {
            updateDocumentMeta();
            syncThemeFromContext();
            applyTheme();
            bridge?.onContext?.((context) => {
                syncThemeFromContext(context);
            });
            updatePageSize();
            window.addEventListener('keydown', handleKeydown);
            window.addEventListener('resize', handleResize);
            window.addEventListener('click', closeThemePicker);
            imgObserver = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        const hash = entry.target.dataset.hash;
                        if (hash) loadImageData(hash);
                        imgObserver.unobserve(entry.target);
                        entry.target.dataset.observed = '';
                    }
                });
            }, { rootMargin: '200px' });
            checkHealth();
            loadDashboardPrefs();
            loadAll();
        });

        onUnmounted(() => {
            window.removeEventListener('keydown', handleKeydown);
            window.removeEventListener('resize', handleResize);
            window.removeEventListener('click', closeThemePicker);
            if (imgObserver) imgObserver.disconnect();
            clearTimeout(resizeTimer);
            clearTimeout(searchTimeout);
            stopSourcePoll();
        });

        return {
            activeSection,
            sidebarOpen,
            switchSection,
            images,
            categories,
            stats,
            loading,
            searchQuery,
            selectedCategory,
            selectedCharacter,
            characters,
            works,
            unassignedCharacterCount,
            selectLibraryCharacter,
            characterLabel,
            sortBy,
            currentPage,
            pageSize,
            total,
            pendingImages,
            pendingTotal,
            pendingCategoryTotal,
            pendingCategories,
            pendingStats,
            pendingLoading,
            pendingSearchQuery,
            pendingCategory,
            pendingCurrentPage,
            pendingPageSize,
            pendingBatchMode,
            pendingSelectedImages,
            fetchPendingImages,
            selectLibraryCategory,
            selectPendingCategory,
            getCategoryName,
            fetchPendingStats,
            pendingDebouncedSearch,
            approvePending,
            rejectPending,
            approvePendingBatch,
            rejectPendingBatch,
            togglePendingBatchMode,
            togglePendingSelection,
            allPendingSelected,
            toggleSelectAllPending,

            // issue #87：审核区编辑
            pendingEditOpen,
            pendingEditId,
            pendingEditForm,
            openPendingEdit,
            closePendingEdit,
            savePendingEdit,
            parseListField,

            previewOpen,
            previewItem,
            isEditing,
            editForm,
            openPreview,
            closePreview,
            prevImage,
            nextImage,
            startEdit,
            cancelEdit,
            saveEdit,

            isBatchMode,
            selectedImages,
            batchMoveOpen,
            batchTargetCategory,
            batchScopeOpen,
            batchScopeMode,
            toggleBatchMode,
            toggleSelection,
            selectAll,
            runStorageCleanup,
            handleBatchDelete,
            openBatchMoveModal,
            closeBatchMoveModal,
            confirmBatchMove,
            batchCharacterOpen,
            batchTargetCharacter,
            openBatchCharacterModal,
            closeBatchCharacterModal,
            confirmBatchCharacter,
            batchWorkOpen,
            batchTargetWork,
            openBatchWorkModal,
            closeBatchWorkModal,
            confirmBatchWork,
            charactersOpen,
            newCharacter,
            addingCharacter,
            deletingCharacterKey,
            openCharactersModal,
            closeCharactersModal,
            addCharacter,
            deleteCharacter,
            openBatchScopeModal,
            closeBatchScopeModal,
            confirmBatchScope,
            repairSelectedScope,

            uploadOpen,
            uploading,
            uploadFile,
            uploadPreviewUrl,
            uploadError,
            uploadForm,
            availableEmotions,
            analysisScenes,
            isSceneSelected,
            toggleScene,
            openUploadModal,
            closeUploadModal,
            handleFileSelect,
            submitUpload,

            analyzing,
            analyzeImage,
            singleReanalyze,
            onOverlayPointerDown,
            onOverlayClick,
            onOverlayInput,
            reanalyzePreviewItem,
            reanalyzePendingItem,

            batchUploadOpen,
            batchUploading,
            batchFolderMode,
            batchDragActive,
            batchFiles,
            batchPreviews,
            batchUploadError,
            batchUploadForm,
            batchDefaults,
            batchTaskId,
            batchTaskStatus,
            batchTaskTotal,
            batchTaskProcessed,
            batchTaskSuccess,
            batchTaskFailed,
            batchTaskAnalyzed,
            batchTaskCurrentFile,
            batchTaskPhase,
            batchTaskPaused,
            batchTaskCancelRequested,
            batchTaskAutoAnalyze,
            batchTaskEta,
            batchTaskRateLimited,
            batchTaskRetried,
            batchTaskConcurrency,
            batchTaskRpm,
            batchTaskResults,
            batchControlBusy,
            batchProgressPercent,
            batchEtaText,
            batchPhaseText,
            batchThrottleText,
            batchStatusLabel,
            batchFailures,
            batchEstimateMinutes,
            resetBatchThrottle,
            controlBatchTask,
            openBatchUploadModal,
            closeBatchUploadModal,
            batchFileInput,
            batchFolderInput,
            triggerBatchFileInput,
            clearBatchFiles,
            handleBatchFileSelect,
            onBatchDragEnter,
            onBatchDragOver,
            onBatchDragLeave,
            onBatchDrop,
            formatBatchSize,
            submitBatchUpload,
            resetBatchUpload,
            batchMode,
            reanalyzeForm,
            reanalyzeScan,
            reanalyzeScanning,
            reanalyzeSwitchNote,
            reanalyzeScanFailed,
            onReanalyzeTargetPick,
            reanalyzeIsPending,
            reanalyzeSelectedCount,
            reanalyzeMissingCount,
            reanalyzeAllCount,
            reanalyzeTargetCount,
            reanalyzePlannedCount,
            reanalyzeEstimateMinutes,
            resetReanalyzeThrottle,
            fetchReanalyzeScan,
            openBatchReanalyzeModal,
            submitBatchReanalyze,
            submitBatchModal,
            reanalyzeChangedCount,
            reanalyzeSuggestions,
            reanalyzeNoDescCount,
            reanalyzeTargetEmptyNote,

            missingDescOpen,
            missingDescScope,
            missingDescIsPending,
            missingDescLoading,
            missingDescError,
            missingDescItems,
            missingDescTotal,
            missingDescScanned,
            missingDescMaxItems,
            missingDescTruncated,
            missingDescPage,
            missingDescPageCount,
            missingDescCounts,
            missingDescDrafts,
            missingDescSaving,
            missingDescDone,
            missingDescRemaining,
            missingDescKey,
            openMissingDescModal,
            closeMissingDescModal,
            switchMissingDescScope,
            missingDescGoPage,
            saveMissingDesc,
            analyzeMissingDescRow,
            startMissingDescBatch,

            emotionsOpen,
            newEmotion,
            addingEmotion,
            deletingEmotionKey,
            openEmotionsModal,
            closeEmotionsModal,
            addEmotion,
            deleteEmotion,

            fetchImages,
            debouncedSearch,
            deleteImage,
            toggleScope,
            prevPage,
            nextPage,
            refreshView,
            formatDate,
            formatBytes,
            formatAddMethod,
            formatOriginTarget,
            getScopeLabel,
            PLACEHOLDER,
            imageDataUrls,
            originalDataUrls,
            loadOriginalImage,
            onItemSlotEnter,
            previewLoading,
            downloadImage,

            favoriteCount,
            toggleFavorite,
            batchSetFavorite,
            healthStatus,
            hashToColor,
            localeVersion,
            t,
            getHealthText,

            confirmOpen,
            confirmMessage,
            onConfirmYes,
            onConfirmNo,
            promptOpen,
            promptMessage,
            promptValue,
            onPromptOk,
            onPromptCancel,
            toastOpen,
            toastMessage,
            toastType,

            // 主题选择 / 视图模式 / 分类 accent
            themeMode,
            effectiveTheme,
            THEME_OPTIONS,
            originalThemeOptions,
            gameThemeOptions,
            setThemeMode,
            themePickerOpen,
            hudHpPct,
            hudApPct,
            hudXpPct,
            viewMode,
            setViewMode,
            catAccent,

            // 预览缩放
            previewZoom,
            isPanning,
            previewTransform,
            onPreviewWheel,
            startPan,
            toggleZoom,

            // 审核区键盘焦点
            focusedPendingId,

            // 外部表情包源
            sourceOpen,
            sourceLoading,
            sourceBusy,
            sourceList,
            sourceInspection,
            sourceFile,
            sourceError,
            sourceCategoryMap,
            sourceForm,
            sourceDefaults,
            sourceJob,
            sourceJobActive,
            sourceJobErrors,
            sourceProgressPercent,
            sourceEtaText,
            sourcePhaseText,
            sourceStatusLabel,
            sourceTypeLabel,
            openSourceModal,
            closeSourceModal,
            handleSourceFile,
            inspectGitHubSource,
            inspectExternalApi,
            inspectRegisteredSource,
            startSourceImport,
            syncSource,
            cancelSourceJob,
            controlSourceJob,
            forgetSource,
        };
    },
    template: TEMPLATE,
}).mount('#app');
