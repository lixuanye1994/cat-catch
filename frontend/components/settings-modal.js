/* 设置弹窗：打开时拉取最新配置，保存后关闭 */
window.Components = window.Components || {};

Components.SettingsModal = {
    inject: ["store"],
    data() {
        return {
            formSettings: {
                ffmpeg_path: "", ffprobe_path: "", output_dir: "",
                bilibili_sessdata: "",
            },
            settingsError: "",
        };
    },
    methods: {
        // 由根组件的「设置」按钮通过 ref 调用
        async loadSettings() {
            this.settingsError = "";
            this.formSettings = await this.api("/api/settings");
        },
        async saveSettings() {
            try {
                await this.api("/api/settings", {
                    method: "POST",
                    body: JSON.stringify(this.formSettings),
                });
                this.store.appSettings = { ...this.store.appSettings, ...this.formSettings };
                this.store.settingsOpen = false;
            } catch (e) {
                this.settingsError = e.message;
            }
        },
    },
};
