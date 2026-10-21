# 通话等待罐头音（voice-ambience）

通话里等 AI 回复的静默期，app 会播这段环境音（当前是 4s 钢琴，2026-10-21 起；原呼吸音已退役）（"接通即闻声"）。这里的两样东西让**换音频、调音量都不用重新编译 APK**，改完立即生效、不用重启服务（每次请求现读文件）。

- **调音量**：改 `config.json` 的 `gain`（0.0–1.0，当前 0.6）。`delay_ms`/`on_ms`/`off_ms` 分别是"说完到开播的宽限、每个脉冲响多久、两个脉冲间的静默"。
- **时序安全区间（服务端强制夹紧，越界会被改写并在日志打 `voice-ambience config clamped`）**：

  | 字段 | 安全区间 | 默认 |
  |---|---|---|
  | `delay_ms` | 2000–10000 | 2000 |
  | `on_ms` | 500–3000 | 3000 |
  | `off_ms` | 2000–10000 | 2000 |
  | `gain` | 0.0–1.0 | 0.6 |

  为什么：安卓端（`ChatScreen.armCannedAudio` + `PcmRecorder`）每个脉冲播放期间把通话麦克风**硬静音**——帧直接丢弃、本地 VAD 也不跑，只在 `off_ms` 间隙里重新开麦。第一个脉冲在本地判定"说完了"（RMS<500 持续 600ms）之后 `delay_ms` 开播；而服务端 VAD 要连续 1200ms RMS<300 才收句。所以 `delay_ms` 太短（600/1500）= 服务端还没收句麦就被掐，句中逗号停顿也会误触发；`on_ms` 太长（6000/10000/20000）= 误触发后长时间聋掉。2026-10-21 钢琴版就是这样把每通电话的第一句吞掉的。
- **素材长度**：`on_ms` 决定每个脉冲实际响多久（每次从头播），素材比 `on_ms` 长只会被截断。建议做成 ≤3s 的片段，首尾各 100–200ms 淡入淡出，免得被截断时"咔"一声。
- **换音频**：替换 `ambience.wav`。**格式硬要求**：16kHz、单声道、有符号 16-bit PCM、canonical 44 字节 WAV 头（app 端按跳 44 字节读裸样本）。转换姿势：
  ```sh
  ffmpeg -i 新素材.任意 -ac 1 -ar 16000 -sample_fmt s16 /tmp/amb.wav
  # ffmpeg 可能写非 44 字节头，用 python wave 重写成 canonical 44 字节头：
  python3 - <<'PY'
  import wave
  r = wave.open('/tmp/amb.wav'); frames = r.readframes(r.getnframes()); r.close()
  w = wave.open('ambience.wav','wb')
  w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
  w.writeframes(frames); w.close()
  PY
  ```
  建议做成 3–4s 无缝循环、峰值别爆（-3dBFS 左右）；音量别比原呼吸音大（RMS 同级或更低）。

app 每次通话开始拉 `GET /voice-call/ambience-config`，比对 `sha256` 与本地缓存不一致才重新下载 `GET /voice-call/ambience`；任何失败都静默退回 APK 内置音。所以扔新文件后，下一通电话就会拉到新的。
