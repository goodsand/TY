#import asyncio
import logging
import random
import re
import sys
import time
import argparse
import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Tuple
from playwright.async_api import async_playwright

# ============================================================================
# 配置区域（仅保留网页爬取相关）
# ============================================================================
TARGET_URL = "https://iptv.cqshushu.com/index.php"
DEFAULT_PROTOCOL = "http://"
IPS_PER_PAGE = 10
MAX_PAGES = 10
MAX_IPS = 0
MAX_DETAIL_PAGES = 30
DETAIL_PAGE_TIMEOUT = 30000
DETAIL_IDLE_TIMEOUT = 5000
DETAIL_MAX_SECONDS = 60
DETAIL_PAGE_DELAY_MIN = 1.0
DETAIL_PAGE_DELAY_MAX = 2.0
IP_MAX_SECONDS = 10
PAGE_DELAY_MIN = 5.0
PAGE_DELAY_MAX = 8.0
IP_DELAY_MIN = 2.0
IP_DELAY_MAX = 4.0
DETAIL_WAIT_MIN = 2.0
DETAIL_WAIT_MAX = 4.0
HEADLESS = True
CHROME_PATH = ""
PAGE_TIMEOUT = 60000
IDLE_TIMEOUT = 15000
SCRAPE_SOURCE_FILTER = "multicast"

# ============================================================================
# 输出文件
# ============================================================================
OUTPUT_DIR = Path(__file__).parent
OUTPUT_TXT = OUTPUT_DIR / "iptv_channels.txt"

# ============================================================================
# 频道名标准化（保留以便统一命名，但不再分类）
# ============================================================================
CCTV_MAP = {
    "1": "综合", "2": "财经", "3": "综艺", "4": "中文国际", "5": "体育",
    "5+": "体育赛事", "6": "电影", "7": "国防军事", "8": "电视剧",
    "9": "纪录", "10": "科教", "11": "戏曲", "12": "社会与法",
    "13": "新闻", "14": "少儿", "15": "音乐", "16": "奥林匹克", "17": "农业农村",
}
CCTV_RE = re.compile(r'(cctv)[-\s]?(5\+|\d{1,3})', re.IGNORECASE)
CLEAR_SUFFIX_RE = re.compile(r'[\s\-_]*(高清|超清|4K|超高清|标清|HD|FHD|UHD|2K|蓝光|原画|流畅|720P|1080P|2160P)', re.IGNORECASE)
INTERNAL_IP = re.compile(r'^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|127\.0\.0\.1)')

def norm_cctv(name: str) -> str:
    low = name.lower()
    if re.search(r'cctv[-\s]?4k', low):
        return "CCTV-4K"
    if re.search(r'cctv[-\s]?5\+', low):
        return "CCTV-5+体育赛事"
    m = CCTV_RE.search(low)
    if m:
        num = m.group(2)
        if num in CCTV_MAP:
            return f"CCTV-{num}{CCTV_MAP[num]}"
        return f"CCTV-{num}"
    return name

def unify_channel_name(raw_name: str) -> str:
    std_name = norm_cctv(raw_name)
    std_name = CLEAR_SUFFIX_RE.sub("", std_name)
    std_name = re.sub(r'[\s\-_]+$', "", std_name).strip()
    return std_name

def is_internal(url: str) -> bool:
    try:
        host = urlparse(url).hostname
        return bool(host and INTERNAL_IP.match(host))
    except:
        return False

def norm_type(t: str) -> str:
    m = {
        "all": "all", "全部": "all",
        "hotel": "hotel", "酒店": "hotel",
        "multicast": "multicast", "组播": "multicast",
        "migu": "migu", "咪咕": "migu",
        "other": "other", "其他": "other",
    }
    return m.get(t.strip().lower(), "all")

# ============================================================================
# 日志系统
# ============================================================================
class BJFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

class FlushStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logger = logging.getLogger('IPTV')
logger.setLevel(logging.INFO)
logger.handlers.clear()
_h = FlushStreamHandler(sys.stdout)
_h.setFormatter(BJFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_h)

# ============================================================================
# 工具函数（爬取辅助）
# ============================================================================
async def human_scroll(page):
    d = random.randint(150, 400)
    for _ in range(random.randint(3, 6)):
        await page.evaluate(f'window.scrollBy(0, {d // 3})')
        await asyncio.sleep(random.uniform(0.05, 0.15))
    await asyncio.sleep(random.uniform(0.3, 0.8))

async def random_mouse(page):
    await page.mouse.move(random.randint(100, 800), random.randint(100, 600))
    await asyncio.sleep(random.uniform(0.1, 0.3))

# ============================================================================
# 网页爬取核心逻辑（保持不变）
# ============================================================================
STEALTH_JS = """ // 抗检测脚本
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
window.chrome = {runtime: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
);
"""

async def scrape_ips_playwright(ctx, filter_type: str, max_pages: int) -> list:
    """使用 Playwright 爬取IP列表"""
    entries = []
    seen = set()
    target_url = f"{TARGET_URL}?t={filter_type}&province=all&limit={IPS_PER_PAGE}" if filter_type != "all" else f"{TARGET_URL}?province=all&limit={IPS_PER_PAGE}"
    page = None
    for attempt in range(5):
        try:
            if page is None or page.is_closed():
                page = await ctx.new_page()
                await page.add_init_script(STEALTH_JS)
            await page.goto(target_url, timeout=PAGE_TIMEOUT, wait_until="commit")
            await asyncio.sleep(random.uniform(5, 8))
            try:
                await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT)
            except:
                pass
            if filter_type != "all":
                current_filter = await page.evaluate("() => document.querySelector('#typeSelect')?.value")
                if current_filter == filter_type:
                    break
                else:
                    await asyncio.sleep(random.uniform(2, 4))
            else:
                break
        except Exception as e:
            logger.warning(f"[PW] 页面初始化失败，重试 {attempt+1}/5")
            page = None
            await asyncio.sleep(3)
    if page is None or page.is_closed():
        logger.error("[PW] 浏览器页面无法保持打开，放弃爬取")
        return entries

    current_page = 1
    while current_page <= max_pages:
        await human_scroll(page)
        await random_mouse(page)
        try:
            page_entries = await page.evaluate(r""" () => { const rows = document.querySelectorAll('table.iptv-table tbody tr'); return Array.from(rows).map(row => { const cells = row.querySelectorAll('td'); if (cells.length < 6) return null; const a = cells[0].querySelector('a'); if (!a) return null; const onclick = a.getAttribute('onclick') || ''; const m = onclick.match(/gotoIP\('([^']+)',\s*'([^']+)'\)/); return { ip: a.innerText.trim(), hash: m ? m[1] : '', type: m ? m[2] : '', channel_count: cells[1].innerText.trim(), type_info: cells[2].innerText.trim(), online_time: cells[3].innerText.trim(), update_time: cells[4].innerText.trim(), status: cells[5].innerText.trim() }; }).filter(x => x && x.ip && x.hash); } """)
        except Exception as e:
            logger.warning(f"[PW] 第{current_page}页数据提取失败: {e}")
            break

        new_count = 0
        for entry in page_entries:
            if filter_type != 'all' and entry['type'] != filter_type:
                continue
            if entry['ip'] in seen:
                continue
            if '失效' in entry['status']:
                continue
            seen.add(entry['ip'])
            entries.append(entry)
            new_count += 1

        if new_count == 0 and current_page > 1:
            break

        try:
            nxt = await page.query_selector('a:has-text("下一页")')
            if not nxt:
                break
            href = await nxt.get_attribute('href') or ''
            if 'page=' not in href:
                break
        except Exception as e:
            logger.warning(f"[PW] 查找下一页按钮失败: {e}")
            break

        delay = random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX)
        await asyncio.sleep(delay)
        try:
            await nxt.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT)
            except:
                pass
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))
        except Exception as e:
            logger.warning(f"[PW] 翻页点击失败: {e}")
            break
        current_page += 1

    logger.info(f"[PW] 共抓取 {len(entries)} 个IP")
    return entries

async def extract_detail_channels_playwright(ctx, detail_url: str) -> list:
    """从详情页获取频道列表（返回原始名称和URL的二元组列表）"""
    channels = []
    page = None
    start_time = time.perf_counter()
    def is_overtime():
        return time.perf_counter() - start_time > DETAIL_MAX_SECONDS

    try:
        page = await ctx.new_page()
        await page.add_init_script(STEALTH_JS)
        await page.goto(detail_url, timeout=DETAIL_PAGE_TIMEOUT, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(DETAIL_WAIT_MIN, DETAIL_WAIT_MAX))
        try:
            await page.wait_for_load_state("networkidle", timeout=DETAIL_IDLE_TIMEOUT)
        except:
            pass
        page_title = await page.title()
        page_text = ""
        try:
            page_text = (await page.inner_text("body"))[:500]
        except:
            pass
        if "安全验证" in page_title or "暂时被拒绝" in page_text or "安全验证" in page_text:
            logger.debug(f"[PW] 详情页触发安全验证: {detail_url[:60]}")
            return channels

        s_hash = None
        channel_list_url = None
        s_link = await page.evaluate(r"""
            () => {
                const links = document.querySelectorAll('a[href*="?s="]');
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    if (href.includes('?s=')) {
                        return href;
                    }
                }
                return null;
            }
        """)
        if s_link:
            m = re.search(r'[?&]s=([^&]+)', s_link)
            if m:
                s_hash = m.group(1)
                t_match = re.search(r'[?&]t=([^&]+)', detail_url)
                t_type = t_match.group(1) if t_match else 'hotel'
                channel_list_url = f"{TARGET_URL}?s={s_hash}&t={t_type}&page_size=100"
                logger.debug(f"[PW] 获取频道列表URL: {channel_list_url[:80]}")

        if not channel_list_url:
            for sel in ['a:has-text("查看频道列表")', 'a.btn-play', '.btn-play']:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        href = await btn.get_attribute("href") or ""
                        if '?s=' in href:
                            m = re.search(r'[?&]s=([^&]+)', href)
                            if m:
                                s_hash = m.group(1)
                                t_match = re.search(r'[?&]t=([^&]+)', detail_url)
                                t_type = t_match.group(1) if t_match else 'hotel'
                                channel_list_url = f"{TARGET_URL}?s={s_hash}&t={t_type}&page_size=100"
                                break
                except:
                    continue

        if not channel_list_url:
            logger.debug(f"[PW] 未找到频道列表链接: {detail_url[:60]}")
            return channels

        await page.goto(channel_list_url, timeout=DETAIL_PAGE_TIMEOUT, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3, 5))
        try:
            await page.wait_for_load_state("networkidle", timeout=DETAIL_IDLE_TIMEOUT)
        except:
            pass
        await asyncio.sleep(random.uniform(1, 2))

        seen_page_urls = set()
        for page_num in range(1, MAX_DETAIL_PAGES + 1):
            if is_overtime():
                logger.debug(f"详情页超时(>{DETAIL_MAX_SECONDS}s)，强制结束: {detail_url[:60]}")
                break
            table_loaded = False
            try:
                await page.wait_for_selector('table.iptv-table tbody tr', timeout=10000)
                table_loaded = True
            except:
                try:
                    await page.wait_for_selector('table tbody tr', timeout=5000)
                    table_loaded = True
                except:
                    pass
            if not table_loaded:
                if page_num == 1:
                    logger.debug(f"[PW] 未找到频道表格: {detail_url[:60]}")
                break

            page_channels = await page.evaluate(r"""
                () => {
                    const results = [];
                    const rows = document.querySelectorAll('table.iptv-table tbody tr, table tbody tr');
                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 3) continue;
                        const name = cells[1] ? cells[1].innerText.trim() : '';
                        let url = '';
                        const a = cells[2] ? cells[2].querySelector('a') : null;
                        if (a) {
                            url = a.getAttribute('href') || a.innerText.trim();
                        } else if (cells[2]) {
                            url = cells[2].innerText.trim();
                        }
                        if (name && url) {
                            results.push({name: name, url: url});
                        }
                    }
                    return results;
                }
            """)
            if not page_channels:
                break
            for ch in page_channels:
                name = ch.get('name', '').strip()
                url = ch.get('url', '').strip()
                if name and url:
                    url = url.replace('&amp;', '&')
                    if not url.startswith(('http://', 'https://')):
                        url = DEFAULT_PROTOCOL + url
                    channels.append((name, url))

            current_page_url = page.url
            if current_page_url in seen_page_urls:
                logger.debug(f"[PW] URL重复，停止翻页")
                break
            seen_page_urls.add(current_page_url)
            if page_num >= MAX_DETAIL_PAGES:
                break

            nxt = None
            try:
                pagination_btns = await page.query_selector_all('.pagination-btn')
                for btn in pagination_btns:
                    btn_text = (await btn.inner_text()).strip()
                    btn_href = await btn.get_attribute('href') or ''
                    if btn_text == '下一页' and btn_href:
                        nxt = btn
                        break
            except:
                pass
            if not nxt:
                try:
                    current_url = page.url
                    if 'page=' in current_url:
                        m = re.search(r'page=(\d+)', current_url)
                        if m:
                            next_page = int(m.group(1)) + 1
                            next_url = re.sub(r'page=\d+', f'page={next_page}', current_url)
                            await page.goto(next_url, timeout=DETAIL_PAGE_TIMEOUT, wait_until="domcontentloaded")
                            await asyncio.sleep(random.uniform(DETAIL_WAIT_MIN, DETAIL_WAIT_MAX))
                            try:
                                await page.wait_for_load_state("networkidle", timeout=DETAIL_IDLE_TIMEOUT)
                            except:
                                pass
                            continue
                except:
                    pass
                break
            try:
                disabled = await nxt.get_attribute("disabled") or ""
                cls = await nxt.get_attribute("class") or ""
                if disabled or "disabled" in cls:
                    break
            except:
                pass
            await asyncio.sleep(random.uniform(DETAIL_PAGE_DELAY_MIN, DETAIL_PAGE_DELAY_MAX))
            try:
                await nxt.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=DETAIL_IDLE_TIMEOUT)
                except:
                    pass
                await asyncio.sleep(random.uniform(1, 2))
            except Exception as e:
                logger.debug(f"翻页点击失败: {e}")
                break
    except Exception as e:
        logger.debug(f"[PW] 提取频道异常: {e}")
    finally:
        if page and not page.is_closed():
            try:
                await page.close()
            except:
                pass

    # 不去重，直接返回所有
    return channels

# ============================================================================
# 导出函数（直接输出所有条目，不分类去重）
# ============================================================================
def export_all_txt(channels: List[Tuple[str, str]]):
    """将 (name, url) 列表全部写入 TXT，每行 name,url"""
    with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
        for name, url in channels:
            if name.strip():  # 确保名称非空
                f.write(f"{name},{url}\n")
    logger.info(f"导出完成: {len(channels)} 条链接, 文件: {OUTPUT_TXT}")

# ============================================================================
# 主流程
# ============================================================================
async def main():
    parser = argparse.ArgumentParser(description="IPTV网站爬取器（只爬取网站，输出原始列表）")
    parser.add_argument("--type", default="all", help="抓取类型: all/hotel/multicast/migu/other")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="最大翻页数")
    parser.add_argument("--max-ips", type=int, default=MAX_IPS, help="最大IP数, 0=不限")
    parser.add_argument("--headless", default="true", help="无头模式: true/false")
    parser.add_argument("--chrome-path", default="", help="Chrome路径")
    args = parser.parse_args()

    ft = norm_type(args.type)
    max_pages = args.max_pages
    max_ips = args.max_ips
    headless = args.headless.lower() != "false"

    logger.info("=" * 60)
    logger.info("IPTV 网站爬取器启动（原始数据输出）")
    logger.info(f"  类型: {ft} | 最大页数: {max_pages} | 最大IP: {max_ips if max_ips>0 else '不限'}")
    logger.info("=" * 60)

    all_channels = []  # 存储 (标准化名称, url)

    start_time = time.time()

    # 网页爬取
    logger.info("--- 开始网页爬取 ---")
    entries = []
    try:
        chrome_path = args.chrome_path or CHROME_PATH
        if not chrome_path:
            candidates = [
                str(Path(__file__).parent / ".openclaw/tmp/browser/chrome-linux64/chrome"),
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
            for c in candidates:
                if Path(c).exists() and Path(c).is_file():
                    chrome_path = c
                    break
        if chrome_path:
            logger.info(f"Chrome路径: {chrome_path}")
        else:
            logger.info("Chrome路径: Playwright默认")

        async with async_playwright() as p:
            launch_opts = {
                "headless": headless,
                "args": [
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=BlockInsecurePrivateNetworkRequests",
                ]
            }
            if chrome_path:
                launch_opts["executable_path"] = chrome_path

            browser = await p.chromium.launch(**launch_opts)
            ctx = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            await ctx.add_init_script(STEALTH_JS)

            try:
                entries = await scrape_ips_playwright(ctx, ft, max_pages)
                logger.info(f"[PW] 共抓取 {len(entries)} 个IP")
            except Exception as e:
                logger.warning(f"Playwright IP列表抓取失败: {e}")

            if max_ips > 0:
                entries = entries[:max_ips]

            if entries:
                logger.info(f"开始获取 {len(entries)} 个IP的详情页频道...")
                for i, entry in enumerate(entries):
                    try:
                        detail_url = f"{TARGET_URL}?p={entry['hash']}&t={entry['type']}"
                        raw_channels = await extract_detail_channels_playwright(ctx, detail_url)
                        if raw_channels:
                            logger.info(f"[{i+1}/{len(entries)}] {entry['ip']}: {len(raw_channels)} 个频道")
                        # 标准化频道名，并添加到总列表（不分类，不去重）
                        for raw_name, url in raw_channels:
                            std_name = unify_channel_name(raw_name)
                            # 过滤内网IP（可选，保留）
                            if not is_internal(url):
                                all_channels.append((std_name, url))
                        await asyncio.sleep(random.uniform(IP_DELAY_MIN, IP_DELAY_MAX))
                    except Exception as e:
                        logger.warning(f"IP {entry['ip']} 处理失败")

            try:
                await ctx.close()
            except:
                pass
            try:
                await browser.close()
            except:
                pass
    except Exception as e:
        logger.warning(f"Playwright启动失败: {e}")

    logger.info("=" * 60)
    logger.info(f"爬取汇总: 共获取 {len(all_channels)} 条链接（已过滤内网IP）")
    logger.info("=" * 60)

    # 直接导出所有链接，不分类、不去重、不限制数量
    export_all_txt(all_channels)

    total_time = time.time() - start_time
    logger.info("=" * 60)
    logger.info("全部完成:")
    logger.info(f"  总链接数: {len(all_channels)}")
    logger.info(f"  总耗时: {total_time:.1f}s")
    logger.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
