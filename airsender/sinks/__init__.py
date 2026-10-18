"""非 AirPlay 的輸出端：Google Cast、DLNA/UPnP、Snapcast。

每個模組提供：
- discover(...)：回傳 Device 清單
- run(...)：連線並持續串流直到被取消；回傳代表接收端自行結束，丟例外代表失敗（由上層重連）
"""

KIND_LABELS = {
    "airplay": "AirPlay",
    "cast": "Google Cast",
    "dlna": "DLNA / UPnP",
    "snapcast": "Snapcast",
}

# 由接收端自己拉 HTTP 串流的種類（延遲由接收端緩衝決定，約 2–5 秒）
HTTP_PULL_KINDS = {"cast", "dlna"}
