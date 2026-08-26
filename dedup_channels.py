import glob
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_DIR = "output"

UA = "VLC/3.0.20 LibVLC/3.0.20"
lock = threading.Lock()


def probe(url, timeout=5):
    """探测组播源是否有效（返回 TS 流即有效），返回 (ok, 用时)。"""
    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout, stream=True,
                         headers={"User-Agent": UA}, allow_redirects=True)
        chunk = next(r.iter_content(64), None)
        r.close()
        ok = r.status_code == 200 and chunk and chunk[0] == 0x47
        return ok, time.time() - t0
    except Exception:
        return False, time.time() - t0


def load_sources():
    sources = {}
    for f in glob.glob(f"{OUT_DIR}/*.txt"):
        if "all_channels" in f:
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or "," not in line:
                    continue
                name, url = line.split(",", 1)
                sources.setdefault(name, []).append(url)
    # 每个频道内部去重（保留顺序）
    for k in sources:
        seen, uniq = set(), []
        for u in sources[k]:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        sources[k] = uniq
    return sources


def find_valid_sources(channel):
    """为一个频道探测全部源，返回按延迟排序的最多 5 个有效源。
    返回 [(name, url, latency), ...]"""
    name, urls = channel
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(probe, u): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            ok, cost = fut.result()
            if ok:
                results.append((u, cost))
    results.sort(key=lambda x: x[1])
    return [(name, u, round(c, 2)) for u, c in results[:5]]


def main():
    sources = load_sources()
    print(f"去重前源数量: {sum(len(v) for v in sources.values())}")
    print(f"唯一频道: {len(sources)}", flush=True)

    valid, invalid = [], []
    pool = ThreadPoolExecutor(max_workers=15)
    futures = {pool.submit(find_valid_sources, (n, u)): n
               for n, u in sources.items()}
    total = len(futures)
    done = 0
    for fut in as_completed(futures):
        name = futures[fut]
        done += 1
        try:
            res = fut.result()
        except Exception as e:
            res = []
        if res:
            valid.append(res)
        else:
            invalid.append(name)
        if done % 25 == 0 or done == total:
            print(f"  进度 {done}/{total}，有效频道 {len(valid)}，无效 {len(invalid)}", flush=True)
    pool.shutdown()

    valid.sort(key=lambda x: x[0])
    invalid.sort()

    with open(f"{OUT_DIR}/dedup_valid_channels.txt", "w", encoding="utf-8") as f:
        for ch in valid:
            for _, url, _ in ch:
                f.write(f"{ch[0][0]},{url}\n")
    with open(f"{OUT_DIR}/dedup_valid_channels.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in valid:
            name = ch[0][0]
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="北京组播",{name}\n')
            for _, url, _ in ch:
                f.write(f"{url}\n")
    if invalid:
        with open(f"{OUT_DIR}/invalid_channels.txt", "w", encoding="utf-8") as f:
            for name in invalid:
                f.write(f"{name}\n")

    total_entries = sum(len(ch) for ch in valid)
    print(f"\n有效频道: {len(valid)}")
    print(f"无效/超时频道: {len(invalid)}")
    print(f"保留源总数: {total_entries}")
    n5 = sum(1 for ch in valid if len(ch) == 5)
    print(f"达到 5 个源的频道: {n5}")
    print("\n前 10 个频道（含多源）:")
    for ch in valid[:10]:
        name = ch[0][0]
        for _, url, lat in ch:
            print(f"  {name} [{lat:.2f}s] {url}")
        print("  ---")


if __name__ == "__main__":
    main()
