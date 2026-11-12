# Triper

多用戶、多旅程的行程規劃 Web App（單頁地圖 + 行程打勾 + 相片工作流），
純 Python 標準庫 + SQLite，零外部依賴。

- 帳戶註冊／登入（PBKDF2-SHA256，bearer token session）
- 每人多個旅程；每旅程有 `owner` / `editor` / `viewer` 三種角色
- 唯讀分享連結（`/?t=<token>`），開啟後自動加入為 viewer
- 行程逐項打勾（空白 → ✓完成 → ◐部分 → ✕跳過）
- 地圖 POI、相片 AI 分析（vision endpoint）、協作 3 秒輪詢同步

## 快速開始

```bash
git clone https://github.com/nkyang10/triper.git
cd triper
python3 server.py 8901          # 首次啟動會自動建立 data/trips.db（schema.sql）
```

開啟 <http://localhost:8901/> → 註冊帳號 → ＋ 新旅程。

環境變數（全部可選）：

| 變數 | 用途 | 預設 |
| --- | --- | --- |
| `TRIPER_DATA` | 資料目錄（DB + photos） | `./data` |
| `VISION_URL` | 相片分析 endpoint | `http://127.0.0.1:8103/v1/chat/completions` |
| `VISION_KEY` | vision API key | *(空)* |
| `VISION_MODEL` | vision model id | `vision-model-default` |

## 資料庫

**Repo 只存放 schema，不存放任何資料。** `data/` 已列入 `.gitignore`。

- Schema：`schema.sql`（`server.py` 啟動時自動套用，可安全重複執行）
- 手動套用：`mkdir -p data && sqlite3 data/trips.db < schema.sql`
- 備份：`sqlite3 data/trips.db ".backup 'backup-$(date +%F).db'"`（WAL 模式下不要直接 cp）
- 遷移：改 schema 時新增 `migrations/NNN_description.sql`，不要改舊檔

資料表一覽：`users` `sessions` `trips` `members` `invites` `items` `marks` `pois` `photos`

## API

認證：`Authorization: Bearer <token>`（GET 亦可用 `?token=`）。

```
POST /api/register            {email,password,name}
POST /api/login               {email,password}
POST /api/logout
GET  /api/me

GET  /api/trips
POST /api/trips               {title,start_date,end_date,days}
POST /api/trips/rename        {trip_id,title,start_date,end_date,days}
POST /api/trips/delete        {trip_id}                 owner only
POST /api/trips/invite        {trip_id,email,role}      owner only
POST /api/trips/share         {trip_id,role}            owner only
GET  /api/trips/members?trip=X
GET  /api/join?t=<share_token>

GET  /api/bootstrap?trip=X
GET  /api/state?trip=X
POST /api/set                 {trip,item,status}
POST /api/item                {trip,id,day,time,title,sub,kind,place_id}
POST /api/item/delete         {trip,id}
POST /api/item/move           {trip,id,dir}
POST /api/photo/analyze       {image}
POST /api/photo/commit        {trip,image,day,time,title,sub,kind,place_id}
POST /api/photo/attach        {trip,image,item_id}
POST /api/photo/delete        {trip,photo_id}
POST /api/poi                 {trip,name,addr,lat,lng,kind,hours}
GET  /api/photo/<file>
```

## 權限矩陣

| 動作 | owner | editor | viewer |
| --- | --- | --- | --- |
| 看行程 / 地圖 / 相片 | ✓ | ✓ | ✓ |
| 打勾、加改刪行程、加相、加 POI | ✓ | ✓ | ✗ (403) |
| 改旅程資料、刪旅程、邀請、出分享連結 | ✓ | ✗ | ✗ |

## 反向代理（HTTPS）

```nginx
location / {
  proxy_pass http://127.0.0.1:8901;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 安全須知

- 本服務未內建 TLS，公網部署請前置 Caddy／nginx／Cloudflare Tunnel
- Session 有效期 60 日，存於 `sessions` 表，登出即刪
- 密碼用 PBKDF2-HMAC-SHA256 200k iterations + 隨機 salt
- 沒有 email 驗證與忘記密碼流程：需要自行重設（DB 內刪除該 user 的 `pw_hash`）

## 目錄

```
server.py            HTTP server + JSON API（無第三方依賴）
schema.sql           資料表定義（唯一入版控的資料庫資產）
static/app.html      行程 + 地圖單頁應用
static/login.html    登入／註冊頁
data/                執行期資料（gitignored）
```

## License

MIT
