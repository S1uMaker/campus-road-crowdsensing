# -*- coding: utf-8 -*-
"""
校园路面平整度众包采集 · 服务端
----------------------------------------
运行:  python app.py
页面:  http://127.0.0.1:8000      采集页（手机浏览器打开）
       http://127.0.0.1:8000/map  结果地图页
接口:  POST /api/upload    批量上传（幂等：重传/重放同一批数据零效果）
       POST /api/event     页面状态事件（可见性/WakeLock，供数据质量追溯）
       GET  /api/segments  scripts/analyze.py 生成的分段结果
       GET  /api/stats     数据统计（演示重放攻击的幂等性用）

安全演示（可选，成本低、演示效果好）:
  把下面的 TOKEN 改成任意随机串，上传就必须带 X-Token 请求头；
  采集页 web/index.html 顶部的 TOKEN 填同一个值。
"""
import json
import os
import sqlite3
import sys

from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, 'web')        # 前端页面
DATA = os.path.join(BASE, 'data')      # 运行数据（数据库 / 分析结果）
CERTS = os.path.join(BASE, 'certs')    # 自签名证书（--https 用）
DB = os.path.join(DATA, 'data.db')

# 演示用最简认证：留空 = 关闭校验
TOKEN = ''

app = Flask(__name__)


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
    p = os.path.join(DATA, 'segments.json')
    if not os.path.exists(p):
        return jsonify(segments=[], stats={},
                       note='还没有结果：先运行 python scripts/analyze.py')
    with open(p, encoding='utf-8') as f:
        return jsonify(json.load(f))


@app.get('/api/stats')
def stats():
    with sqlite3.connect(DB) as c:
        n = c.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        sess = c.execute('SELECT session, COUNT(*) FROM samples '
                         'GROUP BY session ORDER BY session').fetchall()
    return jsonify(total_samples=n,
                   sessions=[{'session': s, 'samples': k} for s, k in sess])


@app.get('/')
def index():
    return send_from_directory(WEB, 'index.html')


@app.get('/map')
def map_page():
    return send_from_directory(WEB, 'map.html')


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
    print(f'  地图页              {scheme}://127.0.0.1:8000/map')
    app.run(host='0.0.0.0', port=8000, ssl_context=ssl_ctx)
