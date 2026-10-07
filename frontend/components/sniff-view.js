/* 通用嗅探视图：任务控制 + 独立 WS + 结果列表（KeepAlive 缓存，切走后嗅探不中断） */
window.Components = window.Components || {};

Components.SniffView = {
    inject: ["store"],
    data() {
        return {
            url: "",
            task: null,
            ws: null,
            items: [],
            filter: "all",
            filters: [
                { key: "all", label: "全部" },
                { key: "hls", label: "HLS" },
                { key: "dash", label: "DASH" },
                { key: "video", label: "视频" },
                { key: "audio", label: "音频" },
                { key: "key", label: "密钥" },
            ],
        };
    },
    computed: {
        taskRunning() {
            return this.task && (this.task.status === "running" || this.task.status === "starting");
        },
        statusText() {
            return { starting: "启动中…", running: "嗅探中", stopped: "已停止", error: "出错" }[this.task.status] || "";
        },
        filteredItems() {
            if (this.filter === "all") return this.items;
            return this.items.filter(i => i.kind === this.filter);
        },
    },
    async mounted() {
        // 页面刷新后：找回仍在运行的嗅探任务并恢复现场
        await this.restoreActiveTask();
    },
    unmounted() {
        // KeepAlive 下组件切走只是停用、不会销毁；仅应用关闭时真正断开
        this.closeWS();
    },
    methods: {
        // 查询后端是否还有活动任务，有则重连其 WS（连上即收 snapshot 恢复停止按钮等）
        async restoreActiveTask() {
            try {
                const r = await this.api("/api/tasks");
                const active = (r.tasks || []).find(
                    t => t.status === "running" || t.status === "starting");
                if (active) {
                    this.url = active.page_url || active.url || "";
                    this.connectWS(active.task_id);
                }
            } catch (e) {
                // 恢复失败不应阻塞页面正常使用
            }
        },

        async startSniff() {
            if (!this.url.trim()) return;
            try {
                const { task_id } = await this.api("/api/sniff", {
                    method: "POST",
                    body: JSON.stringify({ url: this.url.trim() }),
                });
                this.connectWS(task_id);
            } catch (e) {
                alert(this.errText(e));
            }
        },

        async stopSniff() {
            if (!this.task) return;
            await this.api(`/api/task/${this.task.task_id}/stop`, { method: "POST" });
        },

        connectWS(taskId) {
            this.closeWS();
            this.items = [];
            const proto = location.protocol === "https:" ? "wss" : "ws";
            const ws = new WebSocket(`${proto}://${location.host}/ws/task/${taskId}`);
            this.ws = ws;
            ws.onopen = () => { this.store.connected = true; };
            ws.onclose = () => { this.store.connected = false; };
            ws.onmessage = (ev) => this.handleMessage(JSON.parse(ev.data));
        },

        closeWS() {
            if (this.ws) {
                this.ws.onclose = null;
                this.ws.close();
                this.ws = null;
            }
            this.store.connected = false;
        },

        handleMessage(msg) {
            switch (msg.type) {
                case "snapshot":
                    this.task = {
                        task_id: msg.task_id,
                        status: msg.status,
                        page_title: msg.page_title,
                        page_url: msg.page_url,
                        error: msg.error,
                    };
                    this.items = msg.items || [];
                    if (!this.url) this.url = msg.url || "";
                    break;
                case "status":
                    if (this.task) {
                        this.task.status = msg.status;
                        if (msg.page_title) this.task.page_title = msg.page_title;
                        if (msg.page_url) this.task.page_url = msg.page_url;
                        if (msg.error) this.task.error = msg.error;
                    }
                    break;
                case "media":
                    this.items.push(msg.item);
                    break;
                case "update": {
                    const idx = this.items.findIndex(i => i.id === msg.item.id);
                    if (idx !== -1) this.items[idx] = msg.item;
                    break;
                }
            }
        },

        async downloadItem(item) {
            if (!this.task) return;
            // m3u8 带着嗅探上下文跳转专用下载页；其余类型直接后台下载
            if (item.kind === "hls") {
                this.store.m3u8Handoff = {
                    seq: Date.now(),
                    url: item.url,
                    task_id: this.task.task_id,
                    item_id: item.id,
                };
                this.store.view = "m3u8";
                return;
            }
            try {
                const state = await this.api(`/api/task/${this.task.task_id}/download`, {
                    method: "POST",
                    body: JSON.stringify({ item_id: item.id }),
                });
                // POST 返回的 state 不含 title，主动补标题合并（规则同后端 base_title）
                const title = this.task.page_title || item.host || "video";
                H.mergeDownloads([{ ...state, title }]);
                H.pushToast("已加入下载任务");
            } catch (e) {
                H.pushToast(this.errText(e), "error");
            }
        },

        countOf(key) {
            if (key === "all") return this.items.length;
            return this.items.filter(i => i.kind === key).length;
        },

        shortKey(key) {
            return key.length > 24 ? key.slice(0, 12) + "…" + key.slice(-8) : key;
        },
    },
};
