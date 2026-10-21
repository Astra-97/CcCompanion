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
- **新版 App 的长等待音乐（`ambience_no_mute_v1`，2026-10-21 起）**：新 APK 拉配置时带 `?caps=ambience_no_mute_v1`，服务端回包里多一个 `"mic_policy": "ambience_no_mute_v1"` 作确认。确认后的 App：
  - 罐头音只在服务端发出 `turn_accepted`（服务端 VAD 已收句）之后才起播，不再按本地 600ms 静音判定起播；
  - 罐头音**从不**静音/丢弃麦克风帧，回声交给平台 AEC（`VOICE_COMMUNICATION` 音源 + `AcousticEchoCanceler`）；本地检测到她开口仍会立刻停掉罐头音。（思考期间麦克风是否收音仍由原有的严格半双工轮次闸门决定，和罐头音无关。）
  - 因此只对它放宽区间：`delay_ms` 300–10000、`on_ms` 500–60000（其余同上表）。取值写在 `config.json` 的 `no_mute_v1` 覆盖块里（当前 `delay_ms 800 / on_ms 60000 / off_ms 2000`），只有声明了能力的客户端会用到；顶层字段仍是旧客户端的值，旧客户端继续走上表的夹紧区间。
  - 素材短于 `on_ms` 时 App 会无缝循环播放（4s 钢琴 × 循环 ≈ 一分钟），所以长等待不需要换 `ambience.wav`。
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
