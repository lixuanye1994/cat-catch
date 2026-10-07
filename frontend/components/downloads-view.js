/* 下载任务中心：正在下载（running + 未完成）/ 历史记录（done）两个 tab；
 * 展示全部 kind（含 HLS），HLS 行通过 openM3u8Job 事件跳 M3U8 页恢复现场 */
window.Components = window.Components || {};

Components.DownloadsView = {
    inject: ["store"],
    data() {
        return {
            activeTab: "active",   // 'active' | 'history'
            busy: false,
        };
    },
    computed: {
        allJobs() {
            return Object.values(this.store.globalDownloads);
        },
        runningJobs() {
            return this.allJobs
                .filter(d => d.status === "running")
                .sort((a, b) => (b.updated || 0) - (a.updated || 0));
        },
        unfinishedJobs() {
            // interrupted: 服务重启/崩溃；canceled: 手动停止（也可 resume）；error: 失败重试
            return this.allJobs
                .filter(d => ["interrupted", "canceled", "error"].includes(d.status))
                .sort((a, b) => (b.updated || 0) - (a.updated || 0));
        },
        activeJobs() {
            // running 在前、未完成在后
            return [].concat(this.runningJobs, this.unfinishedJobs);
        },
        historyJobs() {
            return this.allJobs
                .filter(d => d.status === "done")
                .sort((a, b) => (b.updated || 0) - (a.updated || 0));
        },
    },
    methods: {
        async resume(dl) {
            try {
                await H.api(`/api/download/${dl.dl_id}/resume`, { method: "POST" });
                H.pushToast(`已继续：${dl.title || "下载任务"}`);
            } catch (e) {
                H.pushToast(H.errText(e), "error");
            }
        },

        async resumeAll() {
            this.busy = true;
            for (const dl of [...this.unfinishedJobs]) await this.resume(dl);
            this.busy = false;
        },

        cancelDownload(dl) {
            H.api(`/api/download/${dl.dl_id}/cancel`, { method: "POST" })
             .catch(e => H.pushToast(H.errText(e), "error"));
        },

        async discard(dl) {
            const tip = dl.status === "done"
                ? `确定清除「${dl.title}」的下载记录吗？`
                : `确定丢弃「${dl.title}」的下载缓存吗？已下载的临时数据将被删除。`;
            if (!confirm(tip)) return;
            try {
                await H.api(`/api/download/${dl.dl_id}/discard`, { method: "POST" });
            } catch (e) {
                H.pushToast(H.errText(e), "error");
            }
        },

        async discardAllUnfinished() {
            if (!confirm(`确定丢弃全部 ${this.unfinishedJobs.length} 个未完成下载吗？`)) return;
            this.busy = true;
            for (const dl of [...this.unfinishedJobs]) {
                try {
                    await H.api(`/api/download/${dl.dl_id}/discard`, { method: "POST" });
                } catch (e) {
                    H.pushToast(H.errText(e), "error");
                }
            }
            this.busy = false;
        },

        async clearHistory() {
            const n = this.historyJobs.length;
            if (!n) return;
            if (!confirm(`确定清空全部 ${n} 条历史记录吗？此操作不可撤销。`)) return;
            this.busy = true;
            for (const dl of [...this.historyJobs]) {
                try {
                    await H.api(`/api/download/${dl.dl_id}/discard`, { method: "POST" });
                } catch (e) {
                    H.pushToast(H.errText(e), "error");
                }
            }
            this.busy = false;
        },

        // HLS「详情」：发跨页事件并切视图，可打开任意 dl_id
        openHlsDetail(dl) {
            this.store.openM3u8Job = { seq: Date.now(), dl_id: dl.dl_id };
            this.store.view = "m3u8";
        },
    },
};
