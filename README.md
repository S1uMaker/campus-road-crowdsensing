# 校园路面平整度众包系统

基于手机浏览器（加速度计 + GPS）的众包道路平整度采集与可视化：采集端网页 + Flask 服务端 + 分析脚本 + 地图页。手机固定在自行车把上骑行采集，数据经断网缓存与批量上传进入服务端，分析后在地图上按路段着色展示（优 / 良 / 差），并输出平整率与最差路段清单。

## 项目结构

```
campus-road-crowdsensing/
├── app.py                    # 服务端入口（Flask + SQLite）：采集页 + 上传/接口 + 地图页 + 激励页
├── web/                      # 前端：HTML 放结构与样式，JS 放逻辑（经 /static 直出）
│   ├── index.html            # 采集页（手机打开；实时定位小地图 + 完成提示弹窗）
│   ├── index.js              # 采集页逻辑：传感器采集、批量上传、断网缓存、完成结算
│   ├── map.html              # 结果地图页结构（仅管理员：需带 ?key=）
│   ├── map.js                # 地图页逻辑：网格着色渲染、重新分析
│   ├── points.html           # 激励中心结构
│   └── points.js             # 激励中心逻辑：积分排行榜渲染
├── scripts/
│   ├── gen_demo.py           # 生成两趟演示骑行数据（不骑车先把链路跑通；幂等，可重复运行）
│   └── analyze.py            # 分析：去重力 → 10 m 网格 RMS → 多趟中位数 → 分级 → 写 data/segments.json
├── data/                     # 运行数据（不进 git，脚本自动创建）：data.db / segments.json / backups/
├── certs/                    # 本地自签名证书（不进 git）：server.crt / server.key
├── requirements.txt
└── README.md
```

## 一、快速开始（先看效果，约 3 分钟）

```bash
pip install flask
python scripts/gen_demo.py   # 生成演示数据 → data/data.db
python scripts/analyze.py    # 分析 → data/segments.json
python app.py                # 启动服务
# 结果地图（仅自己，须带 key）：http://127.0.0.1:8000/map?key=k7m2x
# 激励中心：http://127.0.0.1:8000/points
# 之后采集完数据，在地图页点“🔄 重新分析数据”即可，无需回终端敲命令
```

改动阈值（校准）：编辑 `scripts/analyze.py` 顶部的 `TH_GOOD / TH_MED` 后重新运行 `scripts/analyze.py`（或在地图页点"重新分析"），刷新地图页即可。当前值 2.0 / 4.0，已按首趟试骑校准（手机放挎包）。

对齐方式：按**绝对经纬度 10 m 网格**融合——不同骑手、不同起点、不同方向经过同一块路面会自动合并，无需统一起点。

数据质量取证：采集过程中页面会自动上报"页面可见性 / WakeLock / 心跳"事件；`scripts/analyze.py` 的输出会显示每趟的 **"页面隐藏 Xs / WakeLock 丢失 N 次"**，用来核对降频是否由熄屏或切后台引起（两者都为 0 但采样率仍过低 → 属于系统/浏览器级节流，建议换官方 Chrome）。

激励机制（`/points` 激励中心）：骑手积分 = 数据分（每 1000 条样本 +5）+ 里程分（每 100 米 +1）+ 出勤分（每趟 +20），达标授予勋章（破风启程 / 数据搬运工 / 一公里俱乐部 / 三连骑 / 耐力王），由 `/api/points` 实时统计。积分口径与质检一致（剔除超速、瞬移、样本过少的会话），且入库幂等 → 重传/重放上传不会刷分（可与安全演示 Demo2 对照讲解）。

结果地图对骑手隐藏：`/map` 与 `/api/segments` 需带 `?key=`（`app.py` 顶部 `MAP_KEY`，默认 `k7m2x`，留空 = 关闭保护），骑手端页面没有任何入口；骑手在采集页能看到自己的**实时定位小地图**（当前位置 + 本次轨迹），每趟结束会弹出**完成提示并结算本次积分**。

## 二、真实采集流程

1. 启动服务：`python app.py`（Windows 首次会弹防火墙允许提示，选允许）
2. 另开一个终端做 HTTPS 隧道（浏览器读传感器/定位要求 HTTPS）：
   `cloudflared tunnel --url http://localhost:8000`
   （没装的话：`winget install Cloudflare.cloudflared`，或官网下载单文件 exe）
3. 手机用隧道给出的 `https://xxx.trycloudflare.com` 打开采集页
4. **用 Chrome（安卓）/ Safari（iOS）打开，不要用微信内置浏览器**；手机固定车把支架 → 填骑手号 → 开始采集 → **先原地看 5 秒："加速度"条数持续增长再出发** → 骑行（保持页面在前台、屏幕常亮）→ 停止
5. 在地图页点“🔄 重新分析数据”按钮（等价于手动 `python scripts/analyze.py`）→ 打开启动时提示的带 key 地图地址（如 `/map?key=k7m2x`）
6. 数据在 `data/data.db`；**正式采集前先重置**（删除 `data/data.db`，`app.py`/`scripts/analyze.py` 会自动重建空库），避免把演示数据混进真实结果；想看演示效果再单独跑 `scripts/gen_demo.py`

## 三、安全演示（可选，成本低、演示效果好）

```bash
# Demo1 投毒被质检拦截：伪造"瞬移 + 超速"的假数据
curl -X POST http://127.0.0.1:8000/api/upload -H 'Content-Type: application/json' \
  -d '{"session":"attacker","seq":1,"samples":[
      {"t":1730000000000,"ax":0,"ay":0,"az":30,"lat":39.9042,"lon":116.4074,"sp":5},
      {"t":1730000001000,"ax":0,"ay":0,"az":30,"lat":39.99,"lon":116.85,"sp":200}]}'
python scripts/analyze.py  # → attacker 会话被判定异常并剔除（见输出）

# Demo2 重放攻击被幂等挡下：同一批数据重复发 10 次，总条数不变
curl http://127.0.0.1:8000/api/stats

# Demo3 明文 vs 密文（呼应课程实验 1 的 tcpdump）：
sudo tcpdump -i any -A port 8000     # HTTP：JSON 明文可见；走 HTTPS 隧道后抓包不可读
```

开启最简认证：把 `app.py` 和 `web/index.html` 顶部的 `TOKEN` 改成同一个随机串，上传就必须带请求头。

## 四、常见问题

- **采集页顶部出现 HTTPS 警告**：换 https 域名打开（cloudflared / 学生服务器证书）。
- **开始采集后"加速度"一直是 0（定位正常）**：运动传感器没被放行，按顺序排查：① 不要用微信内置浏览器，换 Chrome（安卓）/ Safari（iOS）；② iOS：设置 → Safari → 运动与方向访问 → 打开；③ 安卓：点地址栏锁形图标 → 权限 → 允许"运动与方向"。页面已内置自检：开始 5 秒没有加速度数据会弹红色提示，看到提示先别出发。
- **保持屏幕常亮**：采集页会自动尝试 Wake Lock；不支持时请手动设置系统常亮。
- **Windows 控制台中文乱码**：先执行 `chcp 65001`，或直接用 Git Bash。
- **地图瓦片加载慢**：`web/map.html` 顶部把 `TILE_PROVIDER` 从 `'amap'` 改成 `'osm'`，或改用高德官方 JS API（需申请 key）。
- **地图整页空白**：Leaflet 走 unpkg CDN；网络不通时把 `leaflet.js` / `leaflet.css` 下载到本地，把 `web/map.html` 里两个链接改成相对路径。
- **地图上路线整体偏移几百米**：GPS 原始坐标是 WGS-84，高德等国内瓦片是 GCJ-02（火星坐标），两者有 300~600 m 系统性差异；`web/map.html` 已内置转换（切到 OSM 瓦片时自动跳过）。数据库与分析结果始终保持 WGS-84 原始坐标。
- **端口被占用**：改 `app.py` 最后一行的 `port=8000`，同时记得改隧道命令。
- **演示数据的坐标**是占位位置，正式采集后地图会自动定位到你的校园（fitBounds）。
