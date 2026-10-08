/* B 站解析视图：解析视频信息 → 获取清晰度 → 下载（无需打开浏览器页面） */
window.Components = window.Components || {};

Components.BiliView = {
    inject: ["store"],
    data() {
        return {
            bili: {
                url: "",
                loading: false,
                video: null,
                page: 1,
                qualities: [],
                loadingQualities: false,
                jobIds: [],     // 与 qualities 对齐：每条画质当前下载任务 dl_id
                // 全部分P批量下载
                batch: {
                    running: false,
                    qIndex: 0,            // 选中的画质档位（按当前 P 的 qualities）
                    total: 0,
                    doneCount: 0,
                    curPage: 0,
                    curJobId: "",
                    stop: false,
                },
            },
        };
    },
    computed: {
        // 当前分 P 是否有任意一个进行中的下载
        hasActiveDownload() {
            return this.bili.jobIds.some(id => {
                const j = id && this.store.globalDownloads[id];
                return j && j.status === "running";
            });
        },
        batchJob() {
            const id = this.bili.batch.curJobId;
            return (id && this.store.globalDownloads[id]) || {};
        },
        // 整体百分比：已完成P数 + 当前P内部进度
        batchPercent() {
            const b = this.bili.batch;
            if (!b.total) return 0;
            const cur = (b.curJobId && this.batchJob.status === "running")
                ? (this.batchJob.percent || 0) / 100 : 0;
            return Math.min(100, Math.round((b.doneCount + cur) / b.total * 100));
        },
        batchQualityLabel() {
            const q = this.bili.qualities[this.bili.batch.qIndex];
            return q ? q.label : "";
        },
        biliPreviewUrl() {
            const q = this.bili.qualities[0];
            if (!q) return "";
            const c = (q.codecs || "").toLowerCase();
            const needsTranscode = c.startsWith("hev") || c.startsWith("hvc")
                || c.startsWith("av01");
            const p = new URLSearchParams({
                video_url: q.video_url,
                audio_url: q.audio_url || "",
            });
            if (needsTranscode) p.set("transcode", "1");
            return `/api/bilibili/preview?${p.toString()}`;
        },
    },
    async mounted() {
        // 刷新恢复：有上次链接则自动重新解析恢复现场
        const last = this.readLastUrl();
        if (last) {
            this.bili.url = last;
            await this.biliResolve();
            // 批量队列未跑完 → 询问是否继续（不自动占用界面）
            if (this.batchUnfinished()) {
                if (confirm("检测到上次的全部分P下载未完成，是否继续？")) {
                    this.resumeBatchFromSaved();
                }
            }
        }
    },
    methods: {
        /* ---------- 现场持久化（localStorage） ---------- */
        readLastUrl() {
            try { return localStorage.getItem("cat-bili-url") || ""; }
            catch { return ""; }
        },
        saveLastUrl(url) {
            try { localStorage.setItem("cat-bili-url", url); } catch {}
        },
        clearLastUrl() {
            try { localStorage.removeItem("cat-bili-url"); } catch {}
        },
        saveBatchState() {
            const b = this.bili.batch;
            try {
                localStorage.setItem("cat-bili-batch", JSON.stringify({
                    qIndex: b.qIndex,
                    total: b.total,
                    doneCount: b.doneCount,
                    curPage: b.curPage,
                }));
            } catch {}
        },
        readBatchState() {
            try {
                return JSON.parse(localStorage.getItem("cat-bili-batch") || "null");
            } catch { return null; }
        },
        clearBatchState() {
            try { localStorage.removeItem("cat-bili-batch"); } catch {}
        },
        // 批量是否未完成（已存记录且完成数 < 总数）
        batchUnfinished() {
            const s = this.readBatchState();
            return s && s.total > 0 && s.doneCount < s.total;
        },

        async biliResolve() {
            if (!this.bili.url.trim()) return;
            this.bili.loading = true;
            this.bili.video = null;
            this.bili.qualities = [];
            try {
                const video = await this.api("/api/bilibili/resolve", {
                    method: "POST",
                    body: JSON.stringify({ url: this.bili.url.trim() }),
                });
                this.bili.video = video;
                this.bili.page = 1;
                this.saveLastUrl(this.bili.url.trim());
                // 自动获取最高画质下载地址，无需再点按钮
                await this.biliGetQualities();
            } catch (e) {
                alert(this.errText(e));
            } finally {
                this.bili.loading = false;
            }
        },

        async biliGetQualities() {
            const part = this.bili.video.pages.find(p => p.page === this.bili.page)
                || this.bili.video.pages[0];
            this.bili.loadingQualities = true;
            this.bili.qualities = [];
            try {
                const { qualities } = await this.api("/api/bilibili/playurl", {
                    method: "POST",
                    body: JSON.stringify({
                        bvid: this.bili.video.bvid,
                        cid: part.cid,
                    }),
                });
                // 列表已按从高到低排列；去掉两个最低画质（至少保留 1 个）
                this.bili.qualities = qualities.length > 2
                    ? qualities.slice(0, qualities.length - 2)
                    : qualities;
                this.bili.jobIds = this.bili.qualities.map(() => "");
            } catch (e) {
                alert(this.errText(e));
            } finally {
                this.bili.loadingQualities = false;
            }
        },

        // 按视频流码率 × 时长估算体积（字节，仅视频流，供 ≈ 参考）
        biliEstSize(q) {
            const dur = this.bili.video && this.bili.video.duration;
            if (!q || !q.bandwidth || !dur) return 0;
            return Math.round(q.bandwidth * dur / 8);
        },

        async biliDownloadAt(i) {
            const q = this.bili.qualities[i];
            if (!q) return;
            try {
                const state = await this.api("/api/bilibili/download", {
                    method: "POST",
                    body: JSON.stringify({
                        title: this.bili.video.title,
                        video_url: q.video_url,
                        audio_url: q.audio_url,
                    }),
                });
                H.mergeDownloads([{ ...state, title: this.bili.video.title }]);
                this.bili.jobIds[i] = state.dl_id;
                H.pushToast("已加入下载任务");
            } catch (e) {
                H.pushToast(this.errText(e), "error");
            }
        },

        // 取某条画质对应的下载任务（含全局通道实时状态）
        jobAt(i) {
            const id = this.bili.jobIds[i];
            return (id && this.store.globalDownloads[id]) || {};
        },

        biliPause(i) {
            const id = this.bili.jobIds[i];
            if (id) this.api(`/api/download/${id}/cancel`, { method: "POST" });
        },

        biliResume(i) {
            const id = this.bili.jobIds[i];
            if (id) this.api(`/api/download/${id}/resume`, { method: "POST" });
        },

        async biliDiscard(i) {
            const id = this.bili.jobIds[i];
            if (!id) return;
            if (!confirm("确定取消该下载吗？已下载的缓存将被删除。")) return;
            await this.api(`/api/download/${id}/discard`, { method: "POST" });
            this.bili.jobIds[i] = "";
        },

        /* ---------- 全部分P批量下载（前端编排，逐P串行） ---------- */
        async startBatch() {
            if (!this.bili.qualities.length) return;
            const b = this.bili.batch;
            b.running = true;
            b.stop = false;
            b.total = this.bili.video.pages.length;
            b.doneCount = 0;
            b.qIndex = Math.min(b.qIndex || 0, this.bili.qualities.length - 1);
            this.saveBatchState();

            for (const p of this.bili.video.pages) {
                if (b.stop) break;
                b.curPage = p.page;
                try {
                    // 取该分P的下载地址
                    const { qualities } = await this.api("/api/bilibili/playurl", {
                        method: "POST",
                        body: JSON.stringify({ bvid: this.bili.video.bvid, cid: p.cid }),
                    });
                    const list = (qualities || []).filter(q =>
                        ![16, 32].includes(q.quality_id));   // 与单P一致，去最低两档
                    const q = list[Math.min(b.qIndex, list.length - 1)] || list[0];
                    if (!q) throw new Error("P" + p.page + " 未获取到下载地址");

                    // 文件名带 P 编号，避免多P同名覆盖
                    const ptag = "P" + String(p.page).padStart(2, "0");
                    const title = this.bili.video.title + " " + ptag;
                    const state = await this.api("/api/bilibili/download", {
                        method: "POST",
                        body: JSON.stringify({
                            title,
                            video_url: q.video_url,
                            audio_url: q.audio_url,
                        }),
                    });
                    b.curJobId = state.dl_id;
                    await this.waitJob(state.dl_id);
                    if (b.stop) break;
                    b.doneCount++;
                    this.saveBatchState();
                    // 分P之间随机停顿 0.8~1.6s，错峰避免连续请求触发限流
                    await new Promise(r => setTimeout(r, 800 + Math.random() * 800));
                } catch (e) {
                    H.pushToast("P" + p.page + " 下载失败：" + this.errText(e), "error");
                    // 失败不中断整批，继续下一P
                }
            }
            b.curJobId = "";
            b.running = false;
            if (b.doneCount === b.total) {
                // 全部完成：清批量记录，下次开启不弹恢复提示
                this.clearBatchState();
                if (!b.stop) H.pushToast("全部分P下载完成");
            }
        },

        // 从已保存进度恢复批量队列：跳过已完成P，从未完成处继续
        async resumeBatchFromSaved() {
            const s = this.readBatchState();
            if (!s || !this.bili.video) return;
            const b = this.bili.batch;
            b.running = true;
            b.stop = false;
            b.qIndex = Math.min(s.qIndex || 0, this.bili.qualities.length - 1);
            b.total = this.bili.video.pages.length;
            b.doneCount = Math.min(s.doneCount || 0, b.total);
            // 从下一个未完成P开始
            const startPage = (s.curPage && s.doneCount >= s.curPage)
                ? s.curPage + 1 : s.curPage;
            const pages = this.bili.video.pages.filter(p => p.page >= startPage);

            for (const p of pages) {
                if (b.stop) break;
                b.curPage = p.page;
                try {
                    const { qualities } = await this.api("/api/bilibili/playurl", {
                        method: "POST",
                        body: JSON.stringify({ bvid: this.bili.video.bvid, cid: p.cid }),
                    });
                    const list = (qualities || []).filter(q =>
                        ![16, 32].includes(q.quality_id));
                    const q = list[Math.min(b.qIndex, list.length - 1)] || list[0];
                    if (!q) throw new Error("P" + p.page + " 未获取到下载地址");
                    const ptag = "P" + String(p.page).padStart(2, "0");
                    const title = this.bili.video.title + " " + ptag;
                    const state = await this.api("/api/bilibili/download", {
                        method: "POST",
                        body: JSON.stringify({
                            title, video_url: q.video_url, audio_url: q.audio_url,
                        }),
                    });
                    b.curJobId = state.dl_id;
                    await this.waitJob(state.dl_id);
                    if (b.stop) break;
                    b.doneCount++;
                    this.saveBatchState();
                    await new Promise(r => setTimeout(r, 800 + Math.random() * 800));
                } catch (e) {
                    H.pushToast("P" + p.page + " 下载失败：" + this.errText(e), "error");
                }
            }
            b.curJobId = "";
            b.running = false;
            if (b.doneCount === b.total) {
                this.clearBatchState();
                H.pushToast("全部分P下载完成");
            }
        },

        // 等待某任务到达终态（done/canceled/error），轮询全局状态
        async waitJob(dlId) {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            for (let k = 0; k < 60 * 60 * 4; k++) {   // 上限4小时
                if (this.bili.batch.stop) {
                    await this.api(`/api/download/${dlId}/cancel`, { method: "POST" });
                    return;
                }
                const j = this.store.globalDownloads[dlId];
                if (j && ["done", "canceled", "error"].includes(j.status)) {
                    if (j.status !== "done") {
                        H.pushToast("P" + this.bili.batch.curPage + " 未成功完成", "error");
                    }
                    return;
                }
                await sleep(1000);
            }
        },

        stopBatch() {
            if (!confirm("确定停止全部剩余分P下载吗？")) return;
            this.bili.batch.stop = true;
            // 保留进度记录，下次开启可询问是否继续
            this.saveBatchState();
        },
    },
};
