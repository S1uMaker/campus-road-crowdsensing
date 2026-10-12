# -*- coding: utf-8 -*-
"""
回退方案 A：把 phyphox 导出的数据转成与网页端相同的 JSON 并上传（接口完全一致）。
用法：
  python phyphox_to_json.py 导出文件.zip --rider r3
  python phyphox_to_json.py ./导出目录   --rider r3 --url http://127.0.0.1:8000
  python phyphox_to_json.py 导出文件.zip --rider r3 --dry out.json   # 只转换不上传

支持：phyphox"导出数据"得到的 zip（内含 Accelerometer 与 Location 两张 CSV 表），
或一个放有这些 CSV 的目录。时间基准优先取 CSV 第一行的绝对时间，其次 --start，最后文件修改时间。
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys
import time
import urllib.request
import zipfile


def parse_time_cell(v):
    """CSV 时间列：正常是相对秒；第一行可能是绝对时间字符串。"""
    v = (v or '').strip()
    try:
        return float(v), None
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return None, datetime.datetime.strptime(v, fmt)
        except ValueError:
            pass
    return None, None


def read_table(text):
    """解析一张 phyphox CSV，返回 (行列表, 检测到的绝对起始时间或 None)。"""
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if r and r[0].strip() != '']
    if not rows:
        return [], None
    header, data = rows[0], rows[1:]
    idx = {h.strip().lower(): i for i, h in enumerate(header)}

    def col(*keys):
        for h, i in idx.items():
            if any(k.lower() in h for k in keys):
                return i
        return None

    ti = col('Time (s)') if col('Time (s)') is not None else 0
    xi, yi, zi = col('X (m/s'), col('Y (m/s'), col('Z (m/s')
    lati, loni, spi = col('Latitude'), col('Longitude'), col('Speed')

    dt_start, out = None, []
    for r in data:
        if len(r) <= ti:
            continue
        rel, dt = parse_time_cell(r[ti])
        if dt is not None:
            dt_start = dt
            rel = 0.0
        if rel is None:
            continue
        try:
            if xi is not None:
                out.append((rel, float(r[xi]), float(r[yi]), float(r[zi])))
            elif lati is not None:
                lat, lon = float(r[lati]), float(r[loni])
                sp = None
                if spi is not None and r[spi].strip():
                    try:
                        sp = float(r[spi])
                    except ValueError:
                        sp = None
                out.append((rel, lat, lon, sp))
        except (ValueError, IndexError):
            continue
    return out, dt_start


def collect_csvs(path):
    """从 zip 或目录收集 CSV 文本，按文件名分成加速度 / 定位两组。"""
    acc_texts, loc_texts = [], []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.lower().endswith('.csv'):
                    continue
                text = z.read(name).decode('utf-8', 'replace')
                low = name.lower()
                if 'accelerometer' in low or '加速度' in name:
                    acc_texts.append(text)
                elif 'location' in low or '定位' in name:
                    loc_texts.append(text)
    else:
        for fn in sorted(os.listdir(path)):
            if not fn.lower().endswith('.csv'):
                continue
            text = open(os.path.join(path, fn), encoding='utf-8', errors='replace').read()
            low = fn.lower()
            if 'accelerometer' in low or '加速度' in fn:
                acc_texts.append(text)
            elif 'location' in low or '定位' in fn:
                loc_texts.append(text)
    return acc_texts, loc_texts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', help='phyphox 导出的 .zip 或 CSV 所在目录')
    ap.add_argument('--rider', default='r1')
    ap.add_argument('--url', default='http://127.0.0.1:8000')
    ap.add_argument('--start', default=None, help='起始时间（"YYYY-mm-dd HH:MM:SS" 或 Unix 秒）')
    ap.add_argument('--dry', default=None, help='只把转换结果写到该 JSON 文件，不上传')
    args = ap.parse_args()

    acc_texts, loc_texts = collect_csvs(args.path)
    if not acc_texts:
        sys.exit('没找到加速度 CSV（文件名需含 Accelerometer/加速度）')
    acc_rows, acc_dt = read_table(acc_texts[0])
    loc_rows, loc_dt = read_table(loc_texts[0]) if loc_texts else ([], None)

    # 时间基准
    t0 = None
    if args.start:
        try:
            t0 = float(args.start) * 1000
        except ValueError:
            t0 = datetime.datetime.strptime(args.start, '%Y-%m-%d %H:%M:%S').timestamp() * 1000
    elif acc_dt or loc_dt:
        t0 = (acc_dt or loc_dt).timestamp() * 1000
    else:
        t0 = os.path.getmtime(args.path) * 1000
    print(f'时间基准 t0 = {datetime.datetime.fromtimestamp(t0 / 1000)}，'
          f'加速度 {len(acc_rows)} 条，定位 {len(loc_rows)} 条')

    samples = [{'t': int(t0 + rel * 1000), 'ax': x, 'ay': y, 'az': z}
               for rel, x, y, z in acc_rows]
    samples += [{'t': int(t0 + rel * 1000), 'lat': lat, 'lon': lon, 'sp': sp}
                for rel, lat, lon, sp in loc_rows]
    samples.sort(key=lambda s: s['t'])

    session = f'{args.rider}-{int(t0)}'

    if args.dry:
        with open(args.dry, 'w', encoding='utf-8') as f:
            json.dump({'session': session, 'samples': samples}, f, ensure_ascii=False)
        print(f'已写出 {args.dry}（未上传）')
        return

    # 按 10 秒一批上传，seq 与批次绑定（重试保持同 seq，服务端幂等）
    sent, seq = 0, 0
    while sent < len(samples):
        t_start = samples[sent]['t']
        batch = [s for s in samples[sent:] if s['t'] - t_start < 10000]
        body = json.dumps({'session': session, 'seq': seq,
                           'samples': batch}).encode('utf-8')
        req = urllib.request.Request(
            args.url.rstrip('/') + '/api/upload', data=body,
            headers={'Content-Type': 'application/json'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                break
            except Exception as e:
                if attempt == 2:
                    sys.exit(f'上传失败（第 {seq} 批）：{e}\n检查服务端是否在运行：{args.url}')
                time.sleep(1)
        sent += len(batch)
        seq += 1
        print(f'  已上传 {sent}/{len(samples)} 条')
    print(f'完成：会话 {session} 已全部上传。下一步：python analyze.py')


if __name__ == '__main__':
    main()
