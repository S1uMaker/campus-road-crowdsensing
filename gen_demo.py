# -*- coding: utf-8 -*-
"""
演示数据生成器：不用真骑车，先生成两趟"骑行"数据把整条链路跑通。
运行：python gen_demo.py  然后  python analyze.py
特点：会话名固定（demo-r1 / demo-r2）+ 时间戳固定 → 重复运行幂等，不会产生重复数据。
重置：删除 data.db 后重跑本脚本。
"""
import math
import os
import random
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'data.db')

# ── 演示路线参数（正式采集前可不管；换个坐标就能在别处预览）──
CENTER = (39.9042, 116.4074)   # 路线中心（示例坐标，换成校园中心即可）
RADIUS = 170.0                 # 环线半径（米）
SPEED = 4.0                    # 骑行速度（米/秒）
ACC_FS = 60                    # 加速度采样率（Hz）
BASE_MS = 1730000000000        # 固定起始时间戳（保证幂等）
# 路段分区（按环线角度占比）：0–40% 好路，40–65% 中等，65–90% 破损，90–100% 好路
SIGMA_GOOD, SIGMA_MED, SIGMA_ROUGH = 0.10, 0.9, 3.0
# ────────────────────────────────────────────────────────

random.seed(42)


def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS samples(
            session TEXT NOT NULL, seq INTEGER NOT NULL, i INTEGER NOT NULL,
            t INTEGER NOT NULL, ax REAL, ay REAL, az REAL,
            lat REAL, lon REAL, sp REAL,
            PRIMARY KEY (session, seq, i))''')


def zone_sigma(frac):
    if frac < 0.40 or frac >= 0.90:
        return SIGMA_GOOD
    if frac < 0.65:
        return SIGMA_MED
    return SIGMA_ROUGH


def build_rows(t0_ms, factor):
    """按 1 Hz 生成 GPS、60 Hz 生成加速度，模拟一次完整骑行。"""
    rows = []
    length = 2 * math.pi * RADIUS
    dur = int(length / SPEED)
    lat0 = math.radians(CENTER[0])
    for s in range(dur):
        frac = s / dur
        ang = 2 * math.pi * frac
        lat = CENTER[0] + RADIUS * math.sin(ang) / 111320.0
        lon = CENTER[1] + RADIUS * math.cos(ang) / (111320.0 * math.cos(lat0))
        sp = max(1.5, random.gauss(SPEED, 0.15))
        rows.append((t0_ms + s * 1000, None, None, None, lat, lon, sp))

        sig = zone_sigma(frac) * factor
        for j in range(ACC_FS):
            t = t0_ms + s * 1000 + int(j * 1000 / ACC_FS)
            az = 9.81 + random.gauss(0, sig)
            if sig > 1.0 and random.random() < 0.02:      # 破损区的坑洼尖峰
                az += random.choice((-1, 1)) * random.uniform(3, 6)
            ax = random.gauss(0, 0.05 + sig * 0.2)
            ay = random.gauss(0, 0.05 + sig * 0.2)
            rows.append((t, ax, ay, az, None, None, None))
    return rows


def insert(session, rows, t0_ms):
    """按 10 秒一批分配 (seq, i)，与网页端的批次语义一致。"""
    batch = {}
    for row in rows:
        seq = (row[0] - t0_ms) // 10000
        i = batch.get(seq, 0)
        batch[seq] = i + 1
        yield (session, seq, i) + row


def main():
    init_db()
    total = 0
    jobs = [
        ('demo-r1', BASE_MS, 1.0),
        ('demo-r2', BASE_MS + 10 * 60 * 1000, 1.2),   # 第二位骑手：振动更大一点
    ]
    with sqlite3.connect(DB) as c:
        for session, t0, factor in jobs:
            rows = list(insert(session, build_rows(t0, factor), t0))
            c.executemany('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                          rows)
            total += len(rows)
            print(f'{session}: 生成 {len(rows)} 条样本（加速度 + 定位）')
    print(f'共 {total} 条（重复运行不会重复入库）。下一步：python analyze.py')


if __name__ == '__main__':
    main()
