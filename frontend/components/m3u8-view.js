/* M3U8 专用下载页：读取来源 → 选画质 → 参数 → 下载进度
 *
 * 进入现场：
 *   - store.m3u8Handoff：从通用嗅探结果跳转（mounted 首次 / watch 后续）
 *   - store.openM3u8Job：从下载中心「详情」打开任意 dl_id 的任务现场
 *   - 首次手动进入：填入默认保存目录
 */
window.Components = window.Components || {};

Components.M3u8View = {
    inject: ["store"],
    data() {
        return {
            m3u8: {
                url: "",
                loading: false,
                error: "",
                variants: [],
                selected: 0,
                taskId: "",
                itemId: 0,
                title: "",
                outputDir: "",
                concurrency: 4,
                retries: 3,
                jobId: null,
                restoreFailed: false,
                tpl: "",        // 命名模板："" 自由 | "movie" | "tv" | "anime"
                meta: { name: "", year: "", season: 1, episode: 1 },
            },
        };
    },
    computed: {
        selectedVariant() {
            return this.m3u8.variants.find(v => v.id === this.m3u8.selected)
                || this.m3u8.variants[0] || {};
        },
        m3u8Job() {
            return (this.m3u8.jobId && this.store.globalDownloads[this.m3u8.jobId]) || {};
        },
    },
    watch: {
        // 嗅探页跳转（组件已创建时走这里）
        "store.m3u8Handoff"(handoff) {
            if (handoff) this.applyHandoff(handoff);
        },
        // 下载中心「详情」：打开指定任务
        "store.openM3u8Job"(e) {
            if (e) this.restoreM3u8ById(e.dl_id);
        },
        // 任务在全局通道被 discard（如丢弃操作）时同步本地 jobId
        m3u8Job() {
            if (this.m3u8.jobId && !this.store.globalDownloads[this.m3u8.jobId]) {
                this.m3u8.jobId = null;
            }
        },
        // 模板字段 / 模板类型 / 所选画质变化时，重新拼装文件名
        "m3u8.tpl"() { this.syncTitle(); },
        "m3u8.meta": { handler() { this.syncTitle(); }, deep: true },
        selectedVariant() { this.syncTitle(); },
    },
    async mounted() {
        if (this.store.m3u8Handoff) {
            this.applyHandoff(this.store.m3u8Handoff);
            return;
        }
        // 组件首次创建即带著「详情」事件时（watcher 对已存在值不触发）
        if (this.store.openM3u8Job) {
            await this.restoreM3u8ById(this.store.openM3u8Job.dl_id);
            return;
        }
        this.fillDefaultDir();
    },
    methods: {
        fillDefaultDir() {
            if (this.store.appSettings) {
                this.m3u8.outputDir = this.store.appSettings.output_dir || "";
            }
        },

        applyHandoff(handoff) {
            this.resetM3u8();
            this.m3u8.url = handoff.url;
            this.m3u8.taskId = handoff.task_id;
            this.m3u8.itemId = handoff.item_id;
            this.fillDefaultDir();
            // 自动读取视频来源
            this.$nextTick(() => this.m3u8Probe());
        },

        /* ---------- 任务现场恢复（下载中心「详情」，支持任意 dl_id） ---------- */
        async restoreM3u8ById(dlId) {
            const rec = this.store.globalDownloads[dlId];
            if (!rec || rec.kind !== "hls" || !rec.params || !rec.params.url) {
                H.pushToast("找不到该 HLS 任务记录", "error");
                return;
            }
            await this.restoreFromRecord(rec);
        },

        async restoreFromRecord(rec) {
            this.resetM3u8();
            const opts = rec.options || {};
            this.m3u8.url = rec.params.url;
            this.applyParsedTitle(rec.title || "video");
            // 优先用记录内的保存目录，而非当前全局设置
            this.m3u8.outputDir = opts.output_dir
                || (this.store.appSettings && this.store.appSettings.output_dir) || "";
            this.m3u8.concurrency = opts.concurrency || 4;
            this.m3u8.retries = opts.retries ?? 3;
            // 带上原下载记录的请求头重新解析，并选中当时的变体
            await this.m3u8Probe(rec.dl_id);
            const match = this.m3u8.variants.find(v => v.url === rec.params.url);
            if (match) this.m3u8.selected = match.id;
            // 无论 probe 成败都绑定 jobId：probe 失败时由降级卡管理任务
            this.m3u8.jobId = rec.dl_id;
            this.m3u8.restoreFailed = this.m3u8.variants.length === 0;
        },

        newM3u8() {
            this.resetM3u8();
            this.fillDefaultDir();
        },

        resetM3u8() {
            this.m3u8.url = "";
            this.m3u8.loading = false;
            this.m3u8.error = "";
            this.m3u8.variants = [];
            this.m3u8.selected = 0;
            this.m3u8.taskId = "";
            this.m3u8.itemId = 0;
            this.m3u8.title = "";
            this.m3u8.concurrency = 4;
            this.m3u8.retries = 3;
            this.m3u8.jobId = null;
            this.m3u8.restoreFailed = false;
            this.m3u8.tpl = "";
            this.m3u8.meta = { name: "", year: "", season: 1, episode: 1 };
        },

        /* ---------- 探测 / 下载 ---------- */
        async m3u8Probe(restoreDlId = null) {
            if (!this.m3u8.url.trim()) return;
            this.m3u8.loading = true;
            this.m3u8.error = "";
            this.m3u8.variants = [];
            this.m3u8.jobId = null;
            try {
                const r = await this.api("/api/m3u8/probe", {
                    method: "POST",
                    body: JSON.stringify({
                        url: this.m3u8.url.trim(),
                        task_id: this.m3u8.taskId,
                        item_id: this.m3u8.itemId,
                        dl_id: restoreDlId || "",
                    }),
                });
                this.m3u8.variants = r.variants || [];
                this.m3u8.selected = 0;
                if (r.title_default) {
                    this.applyParsedTitle(r.title_default);
                } else if (!restoreDlId) {
                    // 手动输入 / 非恢复场景：从 URL 推导名称
                    this.applyParsedTitle(this.defaultNameFromUrl());
                }
                // 恢复场景（restoreDlId）沿用 rec.title，已在恢复前解析填入
            } catch (e) {
                this.m3u8.error = e.message;
            } finally {
                this.m3u8.loading = false;
            }
        },

        defaultNameFromUrl() {
            try {
                const path = new URL(this.m3u8.url).pathname;
                const base = path.split("/").filter(Boolean).pop() || "";
                const stem = base.replace(/\.m3u8?$/i, "");
                return stem || "video";
            } catch {
                return "video";
            }
        },

        /* ---------- 命名模板 ---------- */
        pad2(n) {
            return String(Number(n) || 0).padStart(2, "0");
        },

        // 中文数字（一 / 十一 / 二十三 / 一百零一）→ 阿拉伯数字；本身是数字串则直接转
        cnNum(s) {
            if (/^\d+$/.test(String(s))) return +s;
            const d = {零: 0, 一: 1, 两: 2, 二: 2, 三: 3, 四: 4,
                       五: 5, 六: 6, 七: 7, 八: 8, 九: 9};
            let section = 0, num = 0;
            for (const ch of String(s)) {
                if (ch in d) num = d[ch];
                else if (ch === "十") { section += (num || 1) * 10; num = 0; }
                else if (ch === "百") { section += (num || 1) * 100; num = 0; }
                else if (ch === "千") { section += (num || 1) * 1000; num = 0; }
            }
            return section + num;
        },

        // 变体分辨率（"1920x1080"）→ "1080p"，取不到返回 ""
        resTag(variant) {
            const m = /x(\d+)\s*$/i.exec((variant && variant.resolution) || "");
            return m ? `${m[1]}p` : "";
        },

        // 从网页标题尽力解析 名称/年份/季/集，并猜测模板类型
        parseMeta(raw) {
            const r = { name: "", year: "", season: 0, episode: 0, guess: "" };
            let s = (raw || "").replace(/\s+/g, " ").trim();
            if (!s) return r;

            const cut = (re) => {
                const m = re.exec(s);
                if (m) s = s.replace(m[0], " ");
                return m;
            };

            // 年份
            const ym = cut(/(?:19|20)\d{2}/);
            if (ym) r.year = ym[0];

            // 季+集：S01E01 / S1E1
            const CN = "零一二两三四五六七八九十百千";
            const numTok = `(?:\\d+|[${CN}]+)`;   // 阿拉伯数字 或 中文数字
            let m = /S(\d{1,2})\s*[.xX]?\s*E(\d{1,3})/i.exec(s);
            if (m) {
                r.season = +m[1]; r.episode = +m[2];
                s = s.replace(m[0], " ");
            } else {
                // 中文/数字：第x季 … 第x集/话/回
                const reBoth = new RegExp(
                    `第\\s*(${numTok})\\s*季[\\s\\S]{0,8}?第\\s*(${numTok})\\s*[集话回]`);
                m = reBoth.exec(s);
                if (m) {
                    r.season = this.cnNum(m[1]); r.episode = this.cnNum(m[2]);
                    s = s.replace(m[0], " ");
                }
            }

            // 仅集数：第x集 / [01] / EP01
            if (!r.episode) {
                m = cut(new RegExp(`第\\s*(${numTok})\\s*[集话回]`));
                if (m) r.episode = this.cnNum(m[1]);
            }
            if (!r.episode) {
                m = cut(/\[\s*0*(\d{1,3})\s*\]/);
                if (m) r.episode = +m[1];
            }
            if (!r.episode) {
                m = cut(/\bEP?\s*0*(\d{1,3})\b/i);
                if (m) r.episode = +m[1];
            }

            // 清理名称：分隔符后的站点尾巴
            let name = s.split(/[-_｜|·•~～]| : |：/)[0];
            name = name.replace(/[()（）\[\]【】]/g, " ");
            // 结尾常见站点噪声词，反复剥离
            const noise = /(?:在线(?:免费)?(?:观看|播放)|免费(?:观看|播放)|高清(?:完整版)?|完整版|全集|正片|中(?:文)?字幕?|国语|电影|电视剧|剧集|动漫|动画|综艺|视频|详情|下载|HD|BD)/;
            let prev;
            do {
                prev = name;
                name = name.replace(new RegExp("\\s*" + noise.source + "\\s*$", "i"), " ");
            } while (name !== prev);
            name = name.replace(/[\s_·•\-—~～,，、]+/g, " ").trim();
            r.name = name;

            if (r.season && r.episode) r.guess = "tv";
            else if (r.episode) r.guess = "anime";
            else if (r.year) r.guess = "movie";
            return r;
        },

        applyParsedTitle(raw) {
            const info = this.parseMeta(raw);
            this.m3u8.meta = {
                name: info.name || (raw || "").trim(),
                year: info.year,
                season: info.season || 1,
                episode: info.episode || 1,
            };
            this.m3u8.tpl = info.guess;
            this.m3u8.title = info.guess ? this.composeName() : (raw || "").trim();
        },

        composeName() {
            const m = this.m3u8.meta;
            const res = this.resTag(this.selectedVariant);
            const rs = res ? ` [${res}]` : "";
            if (this.m3u8.tpl === "movie")
                return `${m.name}${m.year ? ` (${m.year})` : ""}${rs}`.trim();
            if (this.m3u8.tpl === "tv")
                return `${m.name} S${this.pad2(m.season)}E${this.pad2(m.episode)}${rs}`.trim();
            if (this.m3u8.tpl === "anime")
                return `${m.name} [${this.pad2(m.episode)}]${rs}`.trim();
            return this.m3u8.title;
        },

        syncTitle() {
            if (this.m3u8.tpl) this.m3u8.title = this.composeName();
        },

        // 点击模板；再点当前模板则退回自由命名
        setTpl(type) {
            if (this.m3u8.tpl === type) {
                this.m3u8.tpl = "";
                return;
            }
            if (!this.m3u8.meta.name.trim()) {
                this.m3u8.meta.name = this.m3u8.title.trim();
            }
            this.m3u8.tpl = type;
        },

        async m3u8Start() {
            if (this.m3u8.tpl && !this.m3u8.meta.name.trim()) {
                alert("请填写名称");
                return;
            }
            if (!this.m3u8.title.trim()) {
                alert("请填写文件名称");
                return;
            }
            try {
                const state = await this.api("/api/m3u8/download", {
                    method: "POST",
                    body: JSON.stringify({
                        url: this.selectedVariant.url,
                        title: this.m3u8.title.trim(),
                        output_dir: this.m3u8.outputDir.trim(),
                        concurrency: this.m3u8.concurrency,
                        retries: this.m3u8.retries,
                        task_id: this.m3u8.taskId,
                        item_id: this.m3u8.itemId,
                    }),
                });
                this.m3u8.jobId = state.dl_id;
            } catch (e) {
                alert(this.errText(e));
            }
        },

        m3u8Pause() {
            this.api(`/api/download/${this.m3u8.jobId}/cancel`, { method: "POST" });
        },

        m3u8ResumeJob() {
            this.api(`/api/download/${this.m3u8.jobId}/resume`, { method: "POST" });
        },

        async m3u8CancelJob() {
            if (!confirm("确定取消该下载吗？已下载的分片缓存将被删除。")) return;
            await this.api(`/api/download/${this.m3u8.jobId}/discard`, { method: "POST" });
            this.m3u8.jobId = null;
        },

        async m3u8Clear() {
            await this.api(`/api/download/${this.m3u8.jobId}/discard`, { method: "POST" });
            this.m3u8.jobId = null;
        },
    },
};
