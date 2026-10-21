# 通话自定义壁纸（voice-wallpaper）

通话全屏页默认画「夜灯」场景（App 内置，不占服务端）。这里放一张图，App 就改用它全屏铺底（`ContentScale.Crop`）+ 一层暗色遮罩保证字幕可读。和 `voice-ambience/` 一样：**每次请求现读目录，换图/删图不用重启服务、不用重编 APK。**

- `wallpaper.jpg|png|webp`：当前壁纸（只会有一张；格式按文件头嗅探，不信扩展名/Content-Type）。**运行数据，不进 git**（`.gitignore` 已忽略）。
- `config.json`（可选）：`{"dim": 0.45}`，遮罩不透明度，0.0–0.9，越大越暗。

接口（全部要 native `X-Auth-Token`，fail-closed）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/voice-call/wallpaper-config` | `{ok, sha256, url, dim}`；没有壁纸时 `sha256`/`url` 为空串 |
| GET | `/voice-call/wallpaper` | 图片字节，`ETag` = sha256 前 16 位，支持 `If-None-Match` |
| POST | `/voice-call/wallpaper` | 原始图片 body（jpg/png/webp，≤8MB），临时文件 + `os.replace` 原子替换 |
| DELETE | `/voice-call/wallpaper` | 删除壁纸，App 回到夜灯 |

App 端：通话页点顶部「NIGHT LIGHT」字样（长按也行）→ 从相册选壁纸 / 切回夜灯 / 删除壁纸。App 上传前会把图缩到长边 ≤2160 再压 JPEG。每通电话开始拉一次 config，sha 不变就用本地缓存。
