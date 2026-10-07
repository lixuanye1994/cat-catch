/* 免构建共享层：全局响应式 store + 工具函数（Vue 3 global build） */

/* 跨组件共享的唯一状态源；各视图私有的临时状态放在组件内部 */
window.store = Vue.reactive({
    view: "sniff",

    // 通用嗅探 WS 连接状态（侧边栏指示灯）
    connected: false,

    // 全局下载通道
    globalConnected: false,
    globalDownloads: {},

    // 设置
    appSettings: null,
    settingsOpen: false,

    // 嗅探页 → M3U8 页的跨页交接载荷（每次跳转带一个新 seq）
    m3u8Handoff: null,

    // 下载中心 → M3U8 页：打开指定任务现场的事件，载荷 { seq, dl_id }
    openM3u8Job: null,

    // 全局轻量通知，元素 { id, message, type: 'ok'|'error'|'info' }
    toasts: [],
});

window.H = {
    async api(path, options = {}) {
        const res = await fetch(path, {
            headers: { "Content-Type": "application/json" },
            ...options,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `请求失败（HTTP ${res.status}）`);
        return data;
    },

    errText(e) {
        if (!e) return "未知错误";
        if (typeof e === "string") return e;
        if (e.message) return e.message;
        try { return JSON.stringify(e); } catch { return String(e); }
    },

    async copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const input = document.createElement("textarea");
            input.value = text;
            document.body.appendChild(input);
            input.select();
            document.execCommand("copy");
            input.remove();
        }
    },

    kindLabel(kind) {
        return { hls: "HLS", dash: "DASH", video: "视频", audio: "音频", key: "密钥", other: "其他" }[kind] || kind;
    },

    downloadStatusText(dl) {
        const map = {
            running: { starting: "启动中", download: "下载中", merge: "合并中", done: "处理中" },
        };
        let base;
        if (dl.status === "running") {
            base = (map.running[dl.phase] || "进行中");
            if (dl.percent != null) base += ` ${dl.percent}%`;
            return base;
        }
        base = { done: "已完成", error: "失败", canceled: "已停止", interrupted: "已中断" }[dl.status] || dl.status;
        if ((dl.status === "error" || dl.status === "interrupted") && dl.message)
            return `${base} · ${dl.message}`;
        return base;
    },

    fmtSize(bytes) {
        if (!bytes && bytes !== 0) return "-";
        if (bytes < 1024) return bytes + " B";
        const units = ["KB", "MB", "GB", "TB"];
        let v = bytes / 1024, i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return v.toFixed(v >= 100 ? 0 : 1) + " " + units[i];
    },

    fmtDuration(seconds) {
        seconds = Math.floor(seconds || 0);
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        const mm = String(m).padStart(2, "0");
        const ss = String(s).padStart(2, "0");
        return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },

    /* ---------- toast 轻通知 ---------- */
    _toastSeq: 0,
    _toastTimers: {},

    pushToast(message, type = "ok", timeout = 3000) {
        const id = ++this._toastSeq;
        store.toasts.push({ id, message, type });
        // 上限 5 条，超出移除最旧并清其计时器
        while (store.toasts.length > 5) {
            const old = store.toasts.shift();
            clearTimeout(this._toastTimers[old.id]);
            delete this._toastTimers[old.id];
        }
        if (timeout > 0) {
            this._toastTimers[id] = setTimeout(() => this.dismissToast(id), timeout);
        }
        return id;
    },

    dismissToast(id) {
        const i = store.toasts.findIndex(t => t.id === id);
        if (i !== -1) store.toasts.splice(i, 1);
        clearTimeout(this._toastTimers[id]);
        delete this._toastTimers[id];
    },

    // journal.updated 为 Unix 秒
    fmtTime(ts) {
        if (!ts) return "-";
        const d = new Date(ts * 1000);
        const p = n => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
             + `${p(d.getHours())}:${p(d.getMinutes())}`;
    },

    /* 注意：WS 的 download 消息与下载 POST 返回的 state 都不含 title，
     * 调用方需把已知 title 补进条目后再合并（标题规则与后端 base_title 耦合） */
    mergeDownloads(items, target = store.globalDownloads) {
        items.forEach(d => {
            target[d.dl_id] = { ...target[d.dl_id], ...d };
        });
    },

    /* 全局下载通道：断线自动重连；所有视图共用这一条 WS */
    connectGlobalWS() {
        let ws = this._globalWS;
        if (ws) {
            ws.onclose = null;
            try { ws.close(); } catch {}
        }
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws/downloads`);
        this._globalWS = ws;
        ws.onopen = () => { store.globalConnected = true; };
        ws.onclose = () => {
            store.globalConnected = false;
            setTimeout(() => this.connectGlobalWS(), 2000);
        };
        ws.onmessage = (ev) => {
            const msg = JSON.parse(ev.data);
            if (msg.type === "downloads") {
                store.globalDownloads = {};
                this.mergeDownloads(msg.items || []);
            } else if (msg.type === "download") {
                store.globalDownloads[msg.dl_id] =
                    { ...store.globalDownloads[msg.dl_id], ...msg };
            } else if (msg.type === "discard") {
                delete store.globalDownloads[msg.dl_id];
            }
        };
    },
};
