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
            },
        };
    },
    computed: {
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
    methods: {
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
                H.pushToast("已加入下载任务");
            } catch (e) {
                H.pushToast(this.errText(e), "error");
            }
        },
    },
};
