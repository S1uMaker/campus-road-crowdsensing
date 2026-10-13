# -*- coding: utf-8 -*-
"""
分析脚本：data.db → segments.json（供 /map 地图页读取）
流程：读样本 → 低通滤波估重力 → 垂向加速度 a⊥ → 按 GPS 坐标网格（10 m × 10 m）
      聚合 → 每格 RMS → 多趟取中位数 → 分级 → 输出
对齐方式：网格按绝对经纬度切分，不依赖"从起点数第几段"。不同骑手、不同起点、
          不同方向只要经过同一块路面，就会落入同一个网格并自动融合。
运行：python analyze.py
注意：TH_GOOD / TH_MED 为初始值，试骑后用已知路况的锚点路段校准。
"""
import bisect
import json
import math
import os
import sqlite3
import statistics
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'data.db')
OUT = os.path.join(BASE, 'segments.json')

# ── 可调参数（校准改这里）────────────────────────────
SEG_LEN = 10.0        # 网格边长（米）：越小越精细，但每格样本数越少
LP_WIN = 0.5          # 重力低通估计窗长（秒）
MIN_SPEED = 1.0       # < 1 m/s 视为静止，样本无效
MAX_SPEED = 8.0       # > 8 m/s 视为异常（质检拦截）
JUMP_SPEED = 15.0     # GPS 相邻点隐含速度 > 15 m/s → 瞬移，剔除
MIN_SEG_SAMPLES = 20  # 每个网格最少有效样本数
MIN_SESSION_VALID = 0.2   # 会话有效样本占比下限，低于则整体剔除
TH_GOOD = 0.8         # RMS < 0.8 → 优
TH_MED = 2.5          # RMS < 2.5 → 良；否则 差
# ────────────────────────────────────────────────────


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS samples(
            session TEXT NOT NULL, seq INTEGER NOT NULL, i INTEGER NOT NULL,
            t INTEGER NOT NULL, ax REAL, ay REAL, az REAL,
            lat REAL, lon REAL, sp REAL,
            PRIMARY KEY (session, seq, i))''')


def load_rows():
    with sqlite3.connect(DB) as c:
        return c.execute('SELECT session, t, ax, ay, az, lat, lon, sp '
                         'FROM samples ORDER BY session, t').fetchall()


def load_events():
    """读取页面状态事件（老数据库没有 events 表时返回空）。"""
    try:
        with sqlite3.connect(DB) as c:
            return c.execute('SELECT session, t, ev FROM events '
                             'ORDER BY session, t').fetchall()
    except sqlite3.OperationalError:
        return []


def session_event_stats(evs):
    """由事件流估算：页面隐藏总时长（秒）、WakeLock 被系统收走次数。"""
    hidden_s, wl_release, hidden_at = 0.0, 0, None
    for t, ev in evs:
        if ev == 'hidden':
            hidden_at = t
        elif ev == 'visible' and hidden_at is not None:
            hidden_s += (t - hidden_at) / 1000.0
            hidden_at = None
        elif ev == 'wl_release':
            wl_release += 1
    return round(hidden_s, 1), wl_release


def split_accel_gps(rows):
    accel, gps = [], []
    for _, t, ax, ay, az, lat, lon, sp in rows:
        if ax is not None and ay is not None and az is not None:
            accel.append((t, ax, ay, az))
        if lat is not None and lon is not None:
            gps.append((t, lat, lon, sp))
    return accel, gps


def clean_gps(gps):
    """GPS 质检：剔除设备已标记超速的点与瞬移点，返回清洗轨迹与累计距离。"""
    out, dropped = [], 0
    for t, lat, lon, sp in gps:
        if sp is not None and sp > MAX_SPEED:
            dropped += 1
            continue
        if out:
            pt, plat, plon, _ = out[-1]
            dt = (t - pt) / 1000.0
            if dt > 0 and haversine(plat, plon, lat, lon) / dt > JUMP_SPEED:
                dropped += 1
                continue
        out.append((t, lat, lon, sp))
    cum = [0.0]
    for k in range(1, len(out)):
        cum.append(cum[-1] + haversine(out[k - 1][1], out[k - 1][2],
                                       out[k][1], out[k][2]))
    return out, cum, dropped


def speed_of(gps, cum, i):
    """第 i 个 GPS 点的有效速度：优先用设备上报，缺失时用相邻点推算。"""
    sp = gps[i][3]
    if sp is not None and sp >= 0:
        return sp
    if i + 1 < len(gps):
        dt = (gps[i + 1][0] - gps[i][0]) / 1000.0
        if 0 < dt <= 5:
            return (cum[i + 1] - cum[i]) / dt
    if i > 0:
        dt = (gps[i][0] - gps[i - 1][0]) / 1000.0
        if 0 < dt <= 5:
            return (cum[i] - cum[i - 1]) / dt
    return None


def vertical_series(accel):
    """时间窗滑动平均估重力方向，返回 [(t, a⊥)]；a⊥ 为沿重力方向的动态分量。"""
    if not accel:
        return []
    ts = [a[0] for a in accel]
    px, py, pz = [0.0], [0.0], [0.0]
    for _, x, y, z in accel:
        px.append(px[-1] + x)
        py.append(py[-1] + y)
        pz.append(pz[-1] + z)
    half = LP_WIN * 1000 / 2
    out = []
    for k, (t, x, y, z) in enumerate(accel):
        j0 = bisect.bisect_left(ts, t - half)
        j1 = bisect.bisect_right(ts, t + half) - 1
        n = j1 - j0 + 1
        gx = (px[j1 + 1] - px[j0]) / n
        gy = (py[j1 + 1] - py[j0]) / n
        gz = (pz[j1 + 1] - pz[j0]) / n
        g = math.sqrt(gx * gx + gy * gy + gz * gz)
        if g < 1e-6:
            continue
        out.append((t, (x * gx + y * gy + z * gz) / g - g))
    return out


def grade_of(score):
    if score < TH_GOOD:
        return '优'
    if score < TH_MED:
        return '良'
    return '差'


def process_session(rows, dlat, dlon):
    """单趟处理：返回 (info, 剔除原因或 None)。info['cells'] = {(row,col): [a⊥...]}"""
    accel, gps = split_accel_gps(rows)
    gps, cum, gps_dropped = clean_gps(gps)
    vert = vertical_series(accel)

    duration = (accel[-1][0] - accel[0][0]) / 1000.0 if len(accel) > 1 else 0
    info = {
        'accel': len(accel), 'gps': len(gps), 'gps_dropped': gps_dropped,
        'fs': round((len(accel) - 1) / duration, 1) if duration > 0 else 0,
        'dist_m': round(cum[-1], 1) if cum else 0,
        'valid': 0, 'valid_ratio': 0.0, 'cells': {},
    }
    if not gps or not vert:
        return info, '无定位或无加速度数据'

    gt = [g[0] for g in gps]
    speeds = [speed_of(gps, cum, i) for i in range(len(gps))]
    cell_acc = defaultdict(list)
    for t, ap in vert:
        k = bisect.bisect_left(gt, t)
        if k == 0 or k == len(gps):
            continue                       # 轨迹时间范围之外
        t0, t1 = gt[k - 1], gt[k]
        if t1 - t0 > 5000:
            continue                       # GPS 断续，位置不可靠
        v = speeds[k - 1] if speeds[k - 1] is not None else speeds[k]
        if v is None or v < MIN_SPEED or v > MAX_SPEED:
            continue                       # 静止 / 超速 / 无速度参照（质检）
        if abs(ap) > 60:
            continue                       # 异常尖峰（质检）
        frac = (t - t0) / (t1 - t0)
        lat = gps[k - 1][1] + frac * (gps[k][1] - gps[k - 1][1])
        lon = gps[k - 1][2] + frac * (gps[k][2] - gps[k - 1][2])
        cell = (math.floor(lat / dlat), math.floor(lon / dlon))
        cell_acc[cell].append(ap)

    info['valid'] = sum(len(v) for v in cell_acc.values())
    info['valid_ratio'] = round(info['valid'] / max(1, len(vert)), 3)
    info['cells'] = {c: v for c, v in cell_acc.items() if len(v) >= MIN_SEG_SAMPLES}
    return info, None


def main():
    init_db()
    rows = load_rows()
    if not rows:
        print('数据库为空：先运行 python gen_demo.py 生成演示数据，或去采集。')
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump({'segments': [], 'stats': {}}, f, ensure_ascii=False)
        return

    by_session = defaultdict(list)
    for r in rows:
        by_session[r[0]].append(r)

    ev_by = defaultdict(list)
    for s, t, ev in load_events():
        ev_by[s].append((t, ev))

    # 网格：按数据平均纬度把 SEG_LEN 米换算成经纬度边长
    lats = [r[5] for r in rows if r[5] is not None]
    mean_lat = sum(lats) / len(lats) if lats else 39.9
    dlat = SEG_LEN / 111320.0
    dlon = SEG_LEN / (111320.0 * math.cos(math.radians(mean_lat)))

    included, excluded = {}, []
    for name in sorted(by_session):
        info, reason = process_session(by_session[name], dlat, dlon)
        info['hidden_s'], info['wl_release'] = session_event_stats(ev_by.get(name, []))
        if not reason and (info['valid_ratio'] < MIN_SESSION_VALID
                           or info['valid'] < 100 or info['dist_m'] < 50):
            reason = (f"有效样本占比 {info['valid_ratio']:.0%}、有效样本 "
                      f"{info['valid']}、里程 {info['dist_m']} m → 判定为异常/不足")
        if reason:
            excluded.append(dict(session=name, reason=reason,
                                 **{k: info[k] for k in
                                    ('accel', 'gps', 'valid', 'valid_ratio', 'dist_m',
                                     'hidden_s', 'wl_release')}))
        else:
            included[name] = info
        print(f"[会话] {name}: 加速度 {info['accel']} 条(fs≈{info['fs']}Hz) "
              f"定位 {info['gps']} 条 里程 {info['dist_m']} m "
              f"有效 {info['valid']} ({info['valid_ratio']:.0%}) "
              f"| 页面隐藏 {info['hidden_s']:.0f}s WakeLock丢失 {info['wl_release']} 次 "
              + (f"→ 剔除：{reason}" if reason else "→ 计入"))

    if not included:
        print('没有有效会话，无法生成结果。')
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump({'segments': [], 'stats': {'excluded': excluded}},
                      f, ensure_ascii=False, indent=1)
        return

    # 参考轨迹：取里程最长的会话（用于地图上的路线折线）
    ref_name = max(included, key=lambda n: included[n]['dist_m'])
    _, ref_gps = split_accel_gps(by_session[ref_name])
    ref_gps, ref_cum, _ = clean_gps(ref_gps)

    def point_at(s):
        k = bisect.bisect_left(ref_cum, s)
        if k <= 0:
            return ref_gps[0][1], ref_gps[0][2]
        if k >= len(ref_cum):
            return ref_gps[-1][1], ref_gps[-1][2]
        c0, c1 = ref_cum[k - 1], ref_cum[k]
        f = 0 if c1 == c0 else (s - c0) / (c1 - c0)
        lat = ref_gps[k - 1][1] + f * (ref_gps[k][1] - ref_gps[k - 1][1])
        lon = ref_gps[k - 1][2] + f * (ref_gps[k][2] - ref_gps[k - 1][2])
        return lat, lon

    # 逐网格跨会话融合：每趟先算格内 RMS，再对多趟取中位数
    cell_scores = defaultdict(list)
    cell_samples = defaultdict(int)
    for info in included.values():
        for c, vals in info['cells'].items():
            cell_scores[c].append(math.sqrt(sum(v * v for v in vals) / len(vals)))
            cell_samples[c] += len(vals)

    cells = []
    for (row, col) in sorted(cell_scores):
        vals = cell_scores[(row, col)]
        score = round(statistics.median(vals), 3)
        lat = (row + 0.5) * dlat
        lon = (col + 0.5) * dlon
        cells.append({
            'row': row, 'col': col,
            'lat': round(lat, 6), 'lon': round(lon, 6),
            'bounds': [[round(row * dlat, 6), round(col * dlon, 6)],
                       [round((row + 1) * dlat, 6), round((col + 1) * dlon, 6)]],
            'score': score, 'grade': grade_of(score),
            'rides': len(vals), 'n': cell_samples[(row, col)],
        })
    for idx, c in enumerate(cells):
        c['idx'] = idx

    route_len = ref_cum[-1] if ref_cum else 0
    good = sum(1 for c in cells if c['grade'] in ('优', '良'))
    paved_ratio = good / len(cells) if cells else 0
    worst = sorted(cells, key=lambda c: -c['score'])[:5]

    # 地图折线：每约 5 m 取一个点
    poly, s = [], 0.0
    while ref_gps and s <= ref_cum[-1]:
        lat, lon = point_at(s)
        poly.append([round(lat, 6), round(lon, 6)])
        s += SEG_LEN / 2

    out = {
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'grid_m': SEG_LEN,
        'segments': cells,
        'route': poly,
        'stats': {
            'route_len_m': round(route_len, 1),
            'rides': len(included),
            'cells': len(cells),
            'paved_ratio': round(paved_ratio, 3),
            'worst': [{'idx': c['idx'], 'score': c['score'], 'grade': c['grade'],
                       'lat': c['lat'], 'lon': c['lon']} for c in worst],
            'excluded': excluded,
            'sessions': [{'session': n, 'dist_m': i['dist_m'], 'valid': i['valid'],
                          'hidden_s': i['hidden_s'], 'wl_release': i['wl_release']}
                         for n, i in included.items()],
        },
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成：{len(cells)} 个网格，平整率 {paved_ratio:.0%}，"
          f"有效会话 {len(included)} 个，剔除 {len(excluded)} 个 → {OUT}")
    print('最差网格 TOP5：', [(c['idx'], c['score'], c['grade']) for c in worst])


if __name__ == '__main__':
    main()
