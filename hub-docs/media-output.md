# 媒体输出协议

支持屏幕的客户端在 WebSocket `hello.features` 中声明

```json
{"media": {"version": 1, "kinds": ["image", "video", "audio"]}}
```

Hub 为支持此协议的设备开放 `show_media` 模型工具。工具接收 `items` 数组，每次 1 至 8 项，每项包含 `kind`、`url` 和 `title`。资源地址为设备可访问的 HTTP(S) 直链，标题最多 200 字。视频网页地址需要先通过相应工具获取可播放的资源地址。工具发送成功后，模型可以继续提供语音说明。

服务端发送 JSON 文本帧

```json
{
  "type": "media",
  "version": 1,
  "session_id": "session-id",
  "sentence_id": "turn-id",
  "id": "tool-call-id",
  "items": [
    {"kind": "image", "url": "https://example.com/photo.jpg", "title": "图片说明"},
    {"kind": "video", "url": "https://example.com/clip.mp4", "title": "视频标题"},
    {"kind": "audio", "url": "https://example.com/music.mp3", "title": "音频标题"}
  ]
}
```

客户端以会话和消息编号去重，丢弃打断期间的旧消息。图片在对话中显示，音视频使用浏览器原生播放、进度和音量控件，由用户点按播放。播放期间暂停语音上传并保持屏幕亮起，唤醒或收到新语音回复时暂停媒体。页面关闭或播放心跳超时后释放屏幕。实际编码支持取决于设备浏览器，加载或解码失败会显示错误。

媒体工具结果随对话存储，`/kiosk/chat/history` 的助手记录可包含 `media` 数组，用于断线和重启后恢复。资源由客户端直接获取，源地址的有效期及访问权限决定后续能否重播。
