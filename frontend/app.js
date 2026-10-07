/* 启动入口：拉取初始数据 → 连接全局下载通道 → 加载组件模板 → 挂载 */
(async () => {
    try {
        const data = await H.api("/api/downloads");
        H.mergeDownloads(data.items || []);
    } catch {}
    try {
        store.appSettings = await H.api("/api/settings");
    } catch {}

    // 下载统一在下载中心管理，启动恒落嗅探首页；未完成任务可从下载中心恢复
    H.connectGlobalWS();

    const app = Vue.createApp({
        data: () => ({ store }),
        computed: {
            runningCount() {
                return Object.values(store.globalDownloads)
                    .filter(d => d.status === "running").length;
            },
        },
        methods: {
            openSettings() {
                store.settingsOpen = true;
                this.$refs.settingsModal.loadSettings();
            },
            dismissToast(id) {
                H.dismissToast(id);
            },
        },
    });

    // 所有子组件通过 inject: ["store"] 获取共享状态
    app.provide("store", store);

    // 共享工具挂为全局属性，所有组件模板可直接 fmtSize(...) 这样调用
    ["api", "errText", "fmtSize", "fmtDuration", "fmtTime", "copyText",
     "kindLabel", "downloadStatusText"].forEach(k => {
        app.config.globalProperties[k] = H[k];
    });

    // 组件逻辑由各 <script> 挂到 window.Components；模板运行时 fetch
    const defs = [
        ["settings-modal", Components.SettingsModal, "/components/settings-modal.html"],
        ["sniff-view",     Components.SniffView,     "/components/sniff-view.html"],
        ["bilibili-view",  Components.BiliView,      "/components/bili-view.html"],
        ["downloads-view", Components.DownloadsView, "/components/downloads-view.html"],
        ["m3u8-view",      Components.M3u8View,      "/components/m3u8-view.html"],
    ];
    await Promise.all(defs.map(async ([name, def, url]) => {
        def.template = await (await fetch(url)).text();
        app.component(name, def);
    }));

    app.mount("#app");
})();
