import re
import sys
import time
import os
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://iptv.cqshushu.com"

s = requests.Session()
s.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
})


def fetch(url, retries=4):
    """绕过站点 JS challenge / 广告验证，返回最终 HTML。失败自动重试。"""
    for attempt in range(retries):
        try:
            r1 = s.get(url, timeout=30)
            if "安全验证" not in r1.text and "ad_verify" not in r1.text:
                return r1.text
            sep = "&" if "?" in url else "?"
            r2 = s.get(url + sep + "_js_challenge=1", timeout=30)
            if "ad_verify" in r2.text:
                s.cookies.set("ad_ok", "1", domain="iptv.cqshushu.com", path="/")
                r3 = s.get(url + sep + "_js_challenge=1", timeout=30)
                return r3.text
            if "安全验证" in r2.text:
                r2 = s.get(url, timeout=30)
                if "安全验证" in r2.text:
                    r2 = s.get(url + sep + "_js_challenge=1", timeout=30)
                if "ad_verify" in r2.text:
                    s.cookies.set("ad_ok", "1", domain="iptv.cqshushu.com", path="/")
                    r2 = s.get(url + sep + "_js_challenge=1", timeout=30)
                return r2.text
            return r2.text
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def parse_channels(html):
    """从频道列表页解析 (序号, 频道名, 播放URL)。"""
    rows = re.findall(r"<tr>\s*<td[^>]*>.*?</tr>", html, re.S)
    channels = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue
        seq = re.sub(r"<[^>]+>", "", cells[0]).strip()
        name = re.sub(r"<[^>]+>", "", cells[1]).strip()
        url = re.sub(r"<[^>]+>", "", cells[2]).strip()
        if name:
            channels.append((seq, name, url))
    return channels


def resolve_channel_sid(pid, stype):
    """从 IP 详情页(?p=...)解析出频道列表的 s 参数。"""
    for _ in range(3):
        html = fetch(f"{BASE}/index.php?p={pid}&t={stype}")
        m = re.search(r"href='\?s=([^'&]+)&t=([^'&]+)'", html)
        if m:
            return m.group(1), m.group(2)
        time.sleep(2)
    return None, None


def get_channel_list(sid, stype, page_size=100, max_pages=100):
    """抓取某个 IP 源(sid)的全部频道列表。sid 为频道列表页 s 参数。"""
    all_channels = []
    page = 1
    while page <= max_pages:
        url = f"{BASE}/index.php?s={sid}&t={stype}&page={page}&page_size={page_size}"
        html = fetch(url)
        chs = parse_channels(html)
        if not chs:
            # 可能被风控返回空/验证页，重试一次
            time.sleep(2)
            html = fetch(url)
            chs = parse_channels(html)
            if not chs:
                break
        all_channels.extend(chs)
        # 判断是否有下一页
        m = re.search(r'共\s*(\d+)\s*个频道', html)
        if not m:
            break
        total = int(m.group(1))
        page_span = re.search(r'第\s*(\d+)\s*页，共\s*(\d+)\s*页', html)
        if page_span:
            cur, last = int(page_span.group(1)), int(page_span.group(2))
        else:
            cur, last = page, 1
        if cur >= last or len(all_channels) >= total:
            break
        page += 1
        time.sleep(1)
    return all_channels


def get_ip_list(t="all", province="all", limit=10, max_pages=100, status_filter=None):
    """抓 IP Sources 列表：ip, 节目数, 类型, 上线时间, 更新时间, 状态, sid。
    province 用省份编码（bj=北京）；status_filter 如 "新上线" 则只保留该状态的记录。"""
    ips = []
    page = 1
    while page <= max_pages:
        url = f"{BASE}/?t={t}&province={province}&limit={limit}&page={page}"
        html = fetch(url)
        rows = re.findall(r"<tr>\s*<td.*?</tr>", html, re.S)
        found = 0
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 6:
                continue
            m = re.search(r"gotoIP\('([^']+)'\s*,\s*'([^']+)'\)", row)
            sid = m.group(1) if m else ""
            stype = m.group(2) if m else ""
            vals = [re.sub(r"<[^>]+>", "", c).strip() for c in cells[:6]]
            ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", vals[0])
            ip = ip.group(1) if ip else vals[0]
            item = {"ip": ip, "programs": vals[1], "type": vals[2],
                    "online": vals[3], "update": vals[4], "status": vals[5],
                    "sid": sid, "stype": stype}
            if status_filter and status_filter not in item["status"]:
                continue
            ips.append(item)
            found += 1
        # 判断是否还有下一页
        page_btns = re.findall(r'class="pagination-btn[^"]*"[^>]*>([^<]*)</a>', html)
        if "下一页" not in page_btns or found == 0:
            break
        page += 1
        time.sleep(1)
    return ips


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "channels":
        sid = sys.argv[2]
        stype = sys.argv[3] if len(sys.argv) > 3 else "multicast"
        chs = get_channel_list(sid, stype)
        print(f"共 {len(chs)} 个频道：")
        for seq, name, url in chs:
            print(f"{seq}\t{name}\t{url}")
    elif cmd == "iplist":
        ips = get_ip_list()
        print(f"共 {len(ips)} 个 IP：")
        for x in ips:
            print(f"{x['ip']}\t节目数{x['programs']}\t{x['type']}\t{x['status']}\tsid={x['sid']}\ttype={x['stype']}")
    elif cmd == "batch":
        # 批量：区域=北京(bj) 类型=组播(multicast) 状态=新上线
        t = sys.argv[2] if len(sys.argv) > 2 else "multicast"
        province = sys.argv[3] if len(sys.argv) > 3 else "bj"
        status = sys.argv[4] if len(sys.argv) > 4 else "新上线"
        outdir = sys.argv[5] if len(sys.argv) > 5 else "output"
        ips = get_ip_list(t=t, province=province, limit=10, status_filter=status)
        print(f"筛选出 {len(ips)} 个 {status} 的 IP：")
        for x in ips:
            print(f"  {x['ip']} | {x['type']} | 节目数{x['programs']} | 状态:{x['status']}")
        if not ips:
            sys.exit(0)
        os.makedirs(outdir, exist_ok=True)
        all_entries = []
        for i, x in enumerate(ips, 1):
            print(f"[{i}/{len(ips)}] 抓取 {x['ip']} 频道列表...")
            ch_sid, ch_type = resolve_channel_sid(x["sid"], x["stype"])
            if not ch_sid:
                print(f"  解析频道列表链接失败，跳过")
                continue
            chs = get_channel_list(ch_sid, ch_type)
            print(f"  共 {len(chs)} 个频道")
            fname = re.sub(r"[^\w]", "_", x["ip"])
            with open(f"{outdir}/{fname}.txt", "w", encoding="utf-8") as f:
                for seq, name, url in chs:
                    f.write(f"{name},{url}\n")
            for seq, name, url in chs:
                all_entries.append((x["ip"], name, url))
            time.sleep(1)
        with open(f"{outdir}/all_channels.txt", "w", encoding="utf-8") as f:
            for ip, name, url in all_entries:
                f.write(f"{name},{url}\n")
        print(f"\n完成。共收集 {len(all_entries)} 条频道记录，输出目录: {outdir}")
    else:
        # demo：抓首页第一个 IP 的频道列表
        ips = get_ip_list(limit=6, max_pages=1)
        print(f"首页共 {len(ips)} 个 IP，取第一个抓取频道列表\n")
        first = ips[0]
        print(f"IP: {first['ip']} | 类型: {first['type']} | 节目数: {first['programs']}")
        ch_sid, ch_type = resolve_channel_sid(first["sid"], first["stype"])
        if not ch_sid:
            print("解析频道列表链接失败")
            sys.exit(1)
        print(f"频道列表页 s={ch_sid} t={ch_type}\n")
        chs = get_channel_list(ch_sid, ch_type)
        print(f"频道列表（共 {len(chs)} 个）：")
        for seq, name, url in chs:
            print(f"{seq}\t{name}\t{url}")
