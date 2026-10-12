# -*- coding: utf-8 -*-
"""
分析脚本：data.db → segments.json（供 /map 地图页读取）
流程：读样本 → 低通滤波估重力 → 垂向加速度 a⊥ → 按 GPS 距离切 10 m 段
      → 每段 RMS → 多趟取中位数 → 分级 → 输出
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
SEG_LEN = 10.0        # 分段长度（米）
LP_WIN = 0.5          # 重力低通估计窗长（秒）
MIN_SPEED = 1.0       # < 1 m/s 视为静止，样本无效
MAX_SPEED = 8.0       # > 8 m/s 视为异常（质检拦截）
JUMP_SPEED = 15.0     # GPS 相邻点隐含速度 > 15 m/s → 瞬移，剔除
MIN_SEG_SAMPLES = 20  # 每段最少有效样本数
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


def process_session(rows):
    """单趟处理：返回 (info, 剔除原因或 None)。info['segs'] = {段号: [a⊥...]}"""
    accel, gps = split_accel_gps(rows)
    gps, cum, gps_dropped = clean_gps(gps)
    vert = vertical_series(accel)

    duration = (accel[-1][0] - accel[0][0]) / 1000.0 if len(accel) > 1 else 0
    info = {
        'accel': len(accel), 'gps': len(gps), 'gps_dropped': gps_dropped,
        'fs': round((len(accel) - 1) / duration, 1) if duration > 0 else 0,
        'dist_m': round(cum[-1], 1) if cum else 0,
        'valid': 0, 'valid_ratio': 0.0, 'segs': {},
    }
    if not gps or not vert:
        return info, '无定位或无加速度数据'

    gt = [g[0] for g in gps]
    speeds = [speed_of(gps, cum, i) for i in range(len(gps))]
    seg_acc = defaultdict(list)
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
        s = cum[k - 1] + frac * (cum[k] - cum[k - 1])
        seg_acc[int(s // SEG_LEN)].append(ap)

    info['valid'] = sum(len(v) for v in seg_acc.values())
    info['valid_ratio'] = round(info['valid'] / max(1, len(vert)), 3)
    info['segs'] = {k: v for k, v in seg_acc.items() if len(v) >= MIN_SEG_SAMPLES}
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

    included, excluded = {}, []
    for name in sorted(by_session):
        info, reason = process_session(by_session[name])
        if not reason and (info['valid_ratio'] < MIN_SESSION_VALID
                           or info['valid'] < 100 or info['dist_m'] < 50):
            reason = (f"有效样本占比 {info['valid_ratio']:.0%}、有效样本 "
                      f"{info['valid']}、里程 {info['dist_m']} m → 判定为异常/不足")
        if reason:
            excluded.append(dict(session=name, reason=reason,
                                 **{k: info[k] for k in
                                    ('accel', 'gps', 'valid', 'valid_ratio', 'dist_m')}))
        else:
            included[name] = info
        print(f"[会话] {name}: 加速度 {info['accel']} 条(fs≈{info['fs']}Hz) "
              f"定位 {info['gps']} 条 里程 {info['dist_m']} m "
              f"有效 {info['valid']} ({info['valid_ratio']:.0%}) "
              + (f"→ 剔除：{reason}" if reason else "→ 计入"))

    if not included:
        print('没有有效会话，无法生成结果。')
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump({'segments': [], 'stats': {'excluded': excluded}},
                      f, ensure_ascii=False, indent=1)
        return

    # 参考轨迹：取里程最长的会话（用于地图折线与段中点坐标）
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

    # 逐段跨会话融合：每趟先算段 RMS，再对多趟取中位数
    seg_scores = defaultdict(list)
    for info in included.values():
        for seg, vals in info['segs'].items():
            seg_scores[seg].append(math.sqrt(sum(v * v for v in vals) / len(vals)))

    segments = []
    for seg in sorted(seg_scores):
        vals = seg_scores[seg]
        score = round(statistics.median(vals), 3)
        lat, lon = point_at(seg * SEG_LEN + SEG_LEN / 2)
        segments.append({'seg': seg, 'lat': round(lat, 6), 'lon': round(lon, 6),
                         'score': score, 'grade': grade_of(score),
                         'rides': len(vals)})

    route_len = ref_cum[-1] if ref_cum else 0
    graded = [s for s in segments if s['rides'] > 0]
    good_len = sum(SEG_LEN for s in graded if s['grade'] in ('优', '良'))
    paved_ratio = good_len / (len(graded) * SEG_LEN) if graded else 0
    worst = sorted(graded, key=lambda s: -s['score'])[:5]

    # 地图折线：每约 5 m 取一个点
    poly, s = [], 0.0
    while ref_gps and s <= ref_cum[-1]:
        lat, lon = point_at(s)
        poly.append([round(lat, 6), round(lon, 6)])
        s += SEG_LEN / 2

    out = {
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'segments': segments,
        'route': poly,
        'stats': {
            'route_len_m': round(route_len, 1),
            'rides': len(included),
            'paved_ratio': round(paved_ratio, 3),
            'worst': [{'seg': s['seg'], 'score': s['score'], 'grade': s['grade']}
                      for s in worst],
            'excluded': excluded,
            'sessions': [{'session': n, 'dist_m': i['dist_m'], 'valid': i['valid']}
                         for n, i in included.items()],
        },
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成：{len(segments)} 个路段，平整率 {paved_ratio:.0%}，"
          f"有效会话 {len(included)} 个，剔除 {len(excluded)} 个 → {OUT}")
    print('最差路段 TOP5：', [(s['seg'], s['score'], s['grade']) for s in worst])


if __name__ == '__main__':
    main()
