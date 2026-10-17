# -*- coding: utf-8 -*-
"""
校园路面平整度众包采集 · 服务端
----------------------------------------
运行:  python app.py
页面:  http://127.0.0.1:8000         采集页（手机浏览器打开）
       http://127.0.0.1:8000/map     结果地图页（需带 ?key=MAP_KEY，骑手端看不到）
       http://127.0.0.1:8000/points  激励中心（积分规则 / 排行榜 / 勋章）
接口:  POST /api/upload    批量上传（幂等：重传/重放同一批数据零效果）
       POST /api/event     页面状态事件（可见性/WakeLock，供数据质量追溯）
       GET  /api/segments  scripts/analyze.py 生成的分段结果（需带 key）
       POST /api/analyze   触发 scripts/analyze.py（地图页按钮用，需带 key）
       GET  /api/points    骑手积分排行（实时从数据库统计，激励页用）
       GET  /api/session_points  单次采集积分（采集完成弹窗用）
       GET  /api/stats     数据统计（演示重放攻击的幂等性用）

安全演示（可选，成本低、演示效果好）:
  把下面的 TOKEN 改成任意随机串，上传就必须带 X-Token 请求头；
  采集页 web/index.html 顶部的 TOKEN 填同一个值。
"""
import json
import math
import os
import sqlite3
import subprocess
import sys
import time

from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, 'web')        # 前端页面
DATA = os.path.join(BASE, 'data')      # 运行数据（数据库 / 分析结果）
CERTS = os.path.join(BASE, 'certs')    # 自签名证书（--https 用）
DB = os.path.join(DATA, 'data.db')

# 演示用最简认证：留空 = 关闭校验
TOKEN = ''

# 结果地图保护：/map 与 /api/segments 必须带 ?key=MAP_KEY 才能访问（骑手端无入口）；
# 留空 = 不保护。建议改成一个别人猜不到的随机串。
MAP_KEY = 'k7m2x'

_analyzing = False   # /api/analyze 防并发标记

# static_folder=web：让 /static/<name>.js 直接送前端脚本（页面 JS 已从 HTML 抽出）
app = Flask(__name__, static_folder=WEB, static_url_path='/static')

# ── 激励积分规则（改这里时同步改 web/points.html 的规则文案）──
PTS_PER_1K_SAMPLES = 5    # 数据分：每 1000 条样本
PTS_PER_100M = 1          # 里程分：每 100 米有效轨迹
PTS_PER_RIDE = 20         # 出勤分：每趟有效采集
MIN_RIDE_SAMPLES = 100    # 一趟至少 100 条样本才计分（质检口径同 analyze.py）
JUMP_SPEED = 15.0         # GPS 瞬移阈值（同 analyze.py）
MAX_SPEED = 8.0           # 设备上报超速阈值（同 analyze.py）


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ride_distance(gps):
    """单趟里程：剔除设备标记超速点与瞬移点后累加（与 analyze.py 口径一致）。"""
    dist, prev = 0.0, None
    for t, lat, lon, sp in gps:
        if sp is not None and sp > MAX_SPEED:
            continue
        if prev is not None:
            dt = (t - prev[0]) / 1000.0
            d = haversine(prev[1], prev[2], lat, lon)
            if dt > 0 and d / dt > JUMP_SPEED:
                continue                     # 瞬移点：丢弃，prev 保持上一个有效点
            dist += d
        prev = (t, lat, lon)
    return dist


def rider_of(session):
    """会话名 → 骑手号：'r1-1730000000000' → 'r1'；无时间戳后缀（如 demo-r1）整体当骑手名。"""
    head, sep, tail = session.rpartition('-')
    if sep and tail.isdigit() and len(tail) >= 10:
        return head
    return session


def badges_of(samples, dist_m, rides):
    """勋章：全部返回，got 标记是否达成（页面上未达成显示灰色，作为激励目标）。"""
    return [
        {'icon': '🚴', 'name': '破风启程', 'desc': '完成第 1 趟采集', 'got': rides >= 1},
        {'icon': '📦', 'name': '数据搬运工', 'desc': '累计记录 ≥ 10000 条数据', 'got': samples >= 10000},
        {'icon': '🛣️', 'name': '一公里俱乐部', 'desc': '累计里程 ≥ 1 公里', 'got': dist_m >= 1000},
        {'icon': '🔥', 'name': '三连骑', 'desc': '完成 3 趟采集', 'got': rides >= 3},
        {'icon': '🏔️', 'name': '耐力王', 'desc': '累计里程 ≥ 5 公里', 'got': dist_m >= 5000},
    ]


def map_key_ok():
    """结果地图访问校验：MAP_KEY 留空 = 不校验。"""
    return not MAP_KEY or request.args.get('key') == MAP_KEY


def init_db():
    os.makedirs(DATA, exist_ok=True)
    with sqlite3.connect(DB) as c:
        # 主键 (session, seq, i)：断网重传与恶意重放都零效果（幂等）
        c.execute('''CREATE TABLE IF NOT EXISTS samples(
            session TEXT NOT NULL,
            seq     INTEGER NOT NULL,
            i       INTEGER NOT NULL,
            t       INTEGER NOT NULL,
            ax REAL, ay REAL, az REAL,
            lat REAL, lon REAL, sp REAL,
            PRIMARY KEY (session, seq, i)
        )''')
        # 页面状态事件：可见性变化 / WakeLock 申请与丢失 / 心跳，用于事后核对采集环境
        c.execute('''CREATE TABLE IF NOT EXISTS events(
            session TEXT NOT NULL,
            t       INTEGER NOT NULL,
            ev      TEXT NOT NULL,
            PRIMARY KEY (session, t, ev)
        )''')


@app.post('/api/upload')
def upload():
    if TOKEN and request.headers.get('X-Token') != TOKEN:
        return jsonify(ok=False, error='bad token'), 401
    d = request.get_json(force=True, silent=True)
    if not d or not d.get('session') or not isinstance(d.get('samples'), list):
        return jsonify(ok=False, error='bad request'), 400
    rows = []
    for i, s in enumerate(d['samples']):
        rows.append((str(d['session']), int(d.get('seq') or 0), i,
                     int(s.get('t') or 0),
                     s.get('ax'), s.get('ay'), s.get('az'),
                     s.get('lat'), s.get('lon'), s.get('sp')))
    with sqlite3.connect(DB) as c:
        c.executemany(
            'INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
    return jsonify(ok=True, received=len(rows))


@app.post('/api/event')
def event():
    if TOKEN and request.headers.get('X-Token') != TOKEN:
        return jsonify(ok=False, error='bad token'), 401
    d = request.get_json(force=True, silent=True) or {}
    if not d.get('session') or not d.get('ev'):
        return jsonify(ok=False, error='bad request'), 400
    with sqlite3.connect(DB) as c:
        c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                  (str(d['session']), int(d.get('t') or 0), str(d['ev'])[:80]))
    return jsonify(ok=True)


@app.get('/api/segments')
def segments():
    if not map_key_ok():
        return jsonify(ok=False, error='not found'), 404
    p = os.path.join(DATA, 'segments.json')
    if not os.path.exists(p):
        return jsonify(segments=[], stats={},
                       note='还没有结果：先运行 python scripts/analyze.py')
    with open(p, encoding='utf-8') as f:
        return jsonify(json.load(f))


@app.post('/api/analyze')
def api_analyze():
    """触发一次 scripts/analyze.py（地图页按钮用），返回结果摘要。"""
    if not map_key_ok():
        return jsonify(ok=False, error='not found'), 404
    global _analyzing
    if _analyzing:
        return jsonify(ok=False, error='正在分析中，请稍候'), 409
    _analyzing = True
    try:
        script = os.path.join(BASE, 'scripts', 'analyze.py')
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        r = subprocess.run([sys.executable, script], cwd=BASE, env=env,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error='分析超时（超过 120 秒）'), 500
    finally:
        _analyzing = False
    out = (r.stdout or '').strip().splitlines()
    if r.returncode != 0:
        err = (r.stderr or '').strip().splitlines()
        return jsonify(ok=False, error='分析失败（脚本返回错误）',
                       output=(out + err)[-12:]), 500
    summary = {}
    try:
        with open(os.path.join(DATA, 'segments.json'), encoding='utf-8') as f:
            d = json.load(f)
        st = d.get('stats') or {}
        summary = {'cells': st.get('cells', 0), 'rides': st.get('rides', 0),
                   'paved_ratio': st.get('paved_ratio', 0),
                   'excluded': len(st.get('excluded') or []),
                   'generated': d.get('generated')}
    except Exception:
        pass
    return jsonify(ok=True, summary=summary, output=out[-12:])


@app.get('/api/stats')
def stats():
    with sqlite3.connect(DB) as c:
        n = c.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        sess = c.execute('SELECT session, COUNT(*) FROM samples '
                         'GROUP BY session ORDER BY session').fetchall()
    return jsonify(total_samples=n,
                   sessions=[{'session': s, 'samples': k} for s, k in sess])


@app.get('/api/points')
def api_points():
    """骑手积分榜：实时从 samples 表统计（幂等入库 → 重传/重放不会刷分）。"""
    with sqlite3.connect(DB) as c:
        rows = c.execute('SELECT session, t, ax, lat, lon, sp FROM samples '
                         'ORDER BY session, t').fetchall()

    # 先按会话汇总（一趟 = 一个 session），再按骑手合并（同一骑手可有多趟）
    sessions = {}
    for s, t, ax, lat, lon, sp in rows:
        d = sessions.setdefault(s, {'accel': 0, 'gps': []})
        if ax is not None:
            d['accel'] += 1
        if lat is not None and lon is not None:
            d['gps'].append((t, lat, lon, sp))

    riders = {}
    for s, d in sessions.items():
        if d['accel'] < MIN_RIDE_SAMPLES:
            continue    # 样本过少（投毒/误触开始）：不计分，QC 口径与 analyze.py 一致
        r = riders.setdefault(rider_of(s), {'rides': 0, 'samples': 0, 'dist': 0.0})
        r['rides'] += 1
        r['samples'] += d['accel']
        r['dist'] += ride_distance(d['gps'])

    out = []
    for name, r in riders.items():
        data_pts = round(r['samples'] / 1000.0 * PTS_PER_1K_SAMPLES)
        dist_pts = round(r['dist'] / 100.0 * PTS_PER_100M)
        attend_pts = PTS_PER_RIDE * r['rides']
        out.append({
            'rider': name, 'points': data_pts + dist_pts + attend_pts,
            'rides': r['rides'], 'samples': r['samples'],
            'dist_m': round(r['dist'], 1),
            'breakdown': {'data': data_pts, 'dist': dist_pts, 'attend': attend_pts},
            'badges': badges_of(r['samples'], r['dist'], r['rides']),
        })
    out.sort(key=lambda x: (-x['points'], x['rider']))
    return jsonify(
        generated=time.strftime('%Y-%m-%d %H:%M:%S'),
        rules={'pts_per_1k_samples': PTS_PER_1K_SAMPLES,
               'pts_per_100m': PTS_PER_100M,
               'pts_per_ride': PTS_PER_RIDE,
               'min_ride_samples': MIN_RIDE_SAMPLES},
        riders=out)


@app.get('/api/session_points')
def session_points():
    """单次采集的积分（采集完成弹窗用）：口径与排行榜一致。"""
    s = request.args.get('session', '')
    if not s:
        return jsonify(ok=False, error='bad request'), 400
    with sqlite3.connect(DB) as c:
        rows = c.execute('SELECT t, ax, lat, lon, sp FROM samples '
                         'WHERE session=? ORDER BY t', (s,)).fetchall()
    accel = sum(1 for r in rows if r[1] is not None)
    gps = [(r[0], r[2], r[3], r[4]) for r in rows if r[2] is not None and r[3] is not None]
    if accel < MIN_RIDE_SAMPLES:
        return jsonify(ok=True, counted=False, samples=accel, dist_m=0.0, points=0)
    dist = ride_distance(gps)
    pts = (round(accel / 1000.0 * PTS_PER_1K_SAMPLES)
           + round(dist / 100.0 * PTS_PER_100M) + PTS_PER_RIDE)
    return jsonify(ok=True, counted=True, samples=accel,
                   dist_m=round(dist, 1), points=pts)


@app.get('/')
def index():
    return send_from_directory(WEB, 'index.html')


@app.get('/map')
def map_page():
    if not map_key_ok():
        return jsonify(ok=False, error='not found'), 404
    return send_from_directory(WEB, 'map.html')


@app.get('/points')
def points_page():
    return send_from_directory(WEB, 'points.html')


if __name__ == '__main__':
    init_db()
    # --https：用同目录自签名证书启 HTTPS（局域网手机直连用；隧道方案无需加）
    ssl_ctx = None
    if '--https' in sys.argv:
        cert = os.path.join(CERTS, 'server.crt')
        key = os.path.join(CERTS, 'server.key')
        if os.path.exists(cert) and os.path.exists(key):
            ssl_ctx = (cert, key)
        else:
            print('未找到 certs/server.crt / certs/server.key，仍按 http 启动')
    scheme = 'https' if ssl_ctx else 'http'
    print('服务已启动：')
    print(f'  采集页（手机打开）  {scheme}://<本机IP>:8000/'
          + ('（自签名证书，手机首次提示时点"继续访问"）' if ssl_ctx else ''))
    map_url = f'{scheme}://127.0.0.1:8000/map' + (f'?key={MAP_KEY}' if MAP_KEY else '')
    print(f'  地图页（仅自己）    {map_url}    ← 带 key，勿外发')
    print(f'  激励中心            {scheme}://127.0.0.1:8000/points')
    app.run(host='0.0.0.0', port=8000, ssl_context=ssl_ctx)
