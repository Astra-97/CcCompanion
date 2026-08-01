# 通话等待罐头音（voice-ambience）

通话里等 AI 回复的静默期，app 会播这段呼吸感环境音（"接通即闻声"）。这里的两样东西让**换音频、调音量都不用重新编译 APK**，改完立即生效、不用重启服务（每次请求现读文件）。

- **调音量**：改 `config.json` 的 `gain`（0.0–1.0，当前 0.6）。`delay_ms`/`on_ms`/`off_ms` 分别是"说完到开播的宽限、每个脉冲响多久、两个脉冲间的静默"。
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
  建议做成 3–4s 无缝循环、峰值别爆（-3dBFS 左右）。

app 每次通话开始拉 `GET /voice-call/ambience-config`，比对 `sha256` 与本地缓存不一致才重新下载 `GET /voice-call/ambience`；任何失败都静默退回 APK 内置音。所以扔新文件后，下一通电话就会拉到新的。
