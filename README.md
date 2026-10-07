# cat-sniffer

猫抓（cat-catch）的本地独立版：输入视频网址，由本地 Playwright 浏览器完成嗅探，
本地 ffmpeg 完成 m3u8/mpd 的下载与合并，**无 2GB 大小限制**。

- 后端：Python + FastAPI + Playwright
- 前端：Vue 3（本地托管运行时，**不需要 Node / 构建步骤**）
- 转码/合并：本地 ffmpeg.exe（完全离线，不走在线 ffmpeg）

## 一、安装（仅首次）

需要 Python 3.10+。

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 安装 ffmpeg（m3u8/mpd 下载必需；仅需 ffmpeg.exe，不需要 ffprobe/ffplay）

**最省事**：下载我们准备好的 ffmpeg 压缩包（`.7z`，需用 [7-Zip](https://www.7-zip.org/) 解压）：

http://47.117.107.6:5244/d/data/download/ffmpeg.7z?sign=QdZs0fKHbY3zTH-rhSw8eZov8jJ-gLjzd_0G2tqvt2A=:0

解压后把 `bin\ffmpeg.exe` 复制到项目的 `ffmpeg\bin\` 目录即可，启动时会自动探测。

也可选择其他来源 / 方式（程序按以下顺序自动探测 ffmpeg）：

1. 放到项目 `ffmpeg/bin/ffmpeg.exe`（推荐，随项目目录走）；
2. 加入系统 PATH（例如 `winget install Gyan.FFmpeg` 或用 scoop 安装）；
3. 在页面左下角「设置」中手动指定 ffmpeg.exe 的路径。

官方 Windows 构建：https://www.gyan.dev/ffmpeg/builds/ （下载 release-essentials 即可）

未安装 ffmpeg 时，普通文件（mp4 等）下载不受影响，仅 m3u8/mpd 的合并会提示缺少 ffmpeg。

## 二、启动

```bash
python run.py
```

浏览器打开 http://127.0.0.1:9800 ，输入视频网页网址即可。

- 嗅探时会显示浏览器窗口，可在窗口中手动登录、点播放；
- 登录态通过独立浏览器配置保留（`data/profiles/`），登录一次长期有效。

## 三、目录说明

```
cat-sniffer/
├─ run.py                 # 启动入口
├─ requirements.txt
├─ ffmpeg/                # 本地 ffmpeg（不纳入 git，需自行放入 bin/ 下）
├─ downloads/             # 下载文件输出目录
├─ data/
│  ├─ config.json         # 设置（ffmpeg 路径等）
│  ├─ journal.json        # 下载任务台账（断点续传用）
│  ├─ tmp/                # 未完成任务的分片缓存
│  └─ profiles/           # 浏览器登录态（desktop）
├─ backend/
│  ├─ main.py             # FastAPI 路由 + WebSocket
│  ├─ browser.py          # Playwright 嗅探任务（注入/CDP）
│  ├─ matcher.py          # 资源过滤规则（移植自 background.js findMedia）
│  ├─ downloader.py       # 下载（Range/切片续传）+ ffmpeg 合并
│  ├─ hls.py              # M3U8 列表解析 + 并发分片下载
│  ├─ journal.py          # 任务台账持久化
│  ├─ manager.py          # 任务管理
│  └─ injected/search.js  # 猫抓深度嗅探脚本（仅改上报出口）
└─ frontend/              # Vue 3 页面（免构建）
```

## 四、B 站定向解析（无需打开浏览器页面）

首页左侧「B站解析」：粘贴 B 站链接（支持 `b23.tv` 短链、BV/av 链接）→ 获取视频后
自动解析清晰度，可切换分P；预览视频后在画质列表中点击对应行的「下载」。

- 免登录可得 480P / 360P（列表中默认隐藏这两个最低画质）；
- 在「设置」中填写 B 站 **SESSDATA** 后解锁 1080P / 4K / 会员画质（按账号权限）；
- 支持视频预览卡（HEVC/AV1 自动实时转码）与简介展示；
- DASH 视频流与音轨分别下载后由本地 ffmpeg 无损合并为 mp4。

注意：b23.tv 短链非永久有效，过期会提示无法识别，此时请用完整 BV 链接。

## 五、M3U8 专用下载页

首页左侧「M3U8 下载」，也可在通用嗅探结果中对 m3u8 资源点「下载」跳转进入（自动带入
地址和原页面的 Referer/Cookie/UA，并自动读取）。页面分四步：

1. **读取视频来源**：解析 master 列表；
2. **选择画质**：列出全部变体（分辨率 / 声明码率 / 编码 / 分片数），显示总时长；
3. **保存位置与参数**：可改保存目录和文件名，设置同时下载的分片并发数（1~8）、
   每个分片的重试次数（失败后递增间隔重试）；
4. **下载进度**：百分比、已下/总分片数、当前分片名、已下字节、实时速度、累计用时；
   可**暂停**（再次继续）或**取消**（清除缓存）。

分片先写临时文件、完成后自动改名，中断不会产生半截分片。

不支持：直播流、独立音轨/字幕、字节范围（BYTE-RANGE）分片。

## 六、下载方式

| 资源类型 | 处理方式 |
|---|---|
| m3u8 / HLS | 并发下载分片（可选择画质、AES-128 解密），再由 ffmpeg `-c copy` 无损封装为 mp4 |
| B站 DASH | 视频流与音轨分别下载，ffmpeg 双输入合并为 mp4 |
| mpd / DASH | ffmpeg 直接拉流，封装为 mkv |
| mp4 等普通文件 | httpx 携带原页面 Referer/Cookie 流式落盘 |
| 密钥 key | 展示与复制（标准 m3u8 加密由下载器自动处理，无需手动使用） |

下载时自动携带嗅探到的 Referer / Cookie / User-Agent。

## 七、防中断 / 断点续传

下载任务实时写入 `data/journal.json` 台账，分片缓存放在 `data/tmp/{任务id}/`：

- **进程被杀 / 关闭命令行 / 刷新页面**：下次启动时台账中 `running` 的任务自动标记为
  「已中断」，页面顶部出现横幅提示，可点「继续」或「全部继续」；M3U8 页面内的暂停任务
  同样可在刷新后继续（用时跨续传累计）；
- 普通文件与 B站 DASH：用 HTTP `Range` 从已下载字节处续传；
- m3u8：按分片续传，已下载的分片自动跳过，仅补缺失部分；
- 「丢弃 / 取消」会删除该任务的缓存与台账记录；已完成文件不受影响。

已验证：强杀服务留 15/100 分片，重启继续后合并出完整 1080P 成片。

注意：mpd（ffmpeg 直连）暂不支持续传，中断后需重新下载；带签名的播放地址可能过期，
若续传时距开始已过久，需要重新获取地址后再下载。

## 八、限制

- Widevine DRM 站点无法获取密钥（原扩展同样限制）；
- 反自动化严格的站点建议使用有头模式手动操作。
