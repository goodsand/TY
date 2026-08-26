import asyncio
import json
import logging
import random
import re
import sys
import argparse
import datetime
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, quote, urljoin
from typing import Dict, List, Tuple, Optional, Any
import aiohttp
from playwright.async_api import async_playwright

# ============================================================================
# 目标站抓取配置
# ============================================================================
TARGET_URL = "https://iptv.cqshushu.com/index.php" # 抓取源地址
DEFAULT_PROTOCOL = "http://" # 补全URL缺失的协议头
SCRAPE_SOURCE_FILTER = "multicast" # 默认抓取类型：all/hotel/multicast/migu/other
ENABLE_SCRAPE = True # 是否启用目标站抓取（可与 --skip-scrape 配合）
MAX_IPS = 0 # 最大处理IP数量，0表示无限制
MAX_PAGES =1 # IP列表最大翻页数
IPS_PER_PAGE = 100 # 每页IP数量（页面实际可能不同）
PAGE_DELAY_MIN = 5.0 # IP列表页翻页最小延迟（秒）
PAGE_DELAY_MAX = 8.0 # IP列表页翻页最大延迟（秒）
IP_DELAY_MIN = 2.0 # 不同IP之间的最小延迟（秒）
IP_DELAY_MAX = 4.0 # 不同IP之间的最大延迟（秒）
MAX_DETAIL_PAGES = 40 # 每个IP详情页最大翻页数
DETAIL_PAGE_TIMEOUT = 30000 # 详情页加载超时（毫秒）
DETAIL_IDLE_TIMEOUT = 5000 # 详情页空闲超时（毫秒）
DETAIL_MAX_SECONDS = 60 # 单个详情页采集最大时长（秒）
DETAIL_PAGE_DELAY_MIN = 1.0 # 详情页翻页最小延迟（秒）
DETAIL_PAGE_DELAY_MAX = 2.0 # 详情页翻页最大延迟（秒）
DETAIL_WAIT_MIN = 2.0 # 详情页加载后最小等待（秒）
DETAIL_WAIT_MAX = 4.0 # 详情页加载后最大等待（秒）
HEADLESS = True # 是否使用无头模式
CHROME_PATH = "" # Chrome/Chromium 可执行文件路径，留空自动查找
PAGE_TIMEOUT = 60000 # 页面加载超时（毫秒）
IDLE_TIMEOUT = 15000 # 页面空闲超时（毫秒）

# ============================================================================
# ============================================================================
ENABLE_FFMPEG = False # 是否启用测速（保持原开关名，与命令行 --skip-ffmpeg 配合）
FFMPEG_PATH = "ffmpeg" # FFmpeg可执行文件路径（rtsp/rtmp流测速）
FFPROBE_PATH = "ffprobe" # FFprobe可执行文件路径（分辨率探测）

# 测速基础参数
SPEED_TEST_TIMEOUT = 10 # 单请求测速超时（秒）
SPEED_TEST_CONCURRENCY = 10 # 测速网络并发数
PROBE_CONCURRENCY = 3 # ffprobe探测并发数

# 速率过滤（低于对应分辨率最低速率的源将被剔除）
OPEN_FILTER_SPEED = True # 是否开启速率过滤
MIN_SPEED = 0.15 # 默认最小速率（M/s）—— 降低门槛，避免误杀
RESOLUTION_SPEED_MAP = { # 分辨率与最低速率映射（M/s）
  "1280x720": 0.15,
  "1920x1080": 0.3,
  "3840x2160": 0.8,
}

# 分辨率过滤（需ffprobe探测，会增加测速耗时，但能更有效区分可播源性）
OPEN_FILTER_RESOLUTION = True # 是否开启分辨率过滤
MIN_RESOLUTION = "1280x720" # 最小分辨率
MAX_RESOLUTION = "3840x2160" # 最大分辨率

# 广告过滤（识别无信号/广告等循环占位源，复用测速已抓取的播放列表，不增加额外请求）
OPEN_FILTER_AD = True # 是否开启广告过滤

# 结果排序维度，按顺序依次比较: speed(速率高优先)/delay(延迟低优先)/resolution(分辨率高优先)
SORT_BY = ["speed"]

# 下载测速稳定性采样（速率稳定后提前结束，节省测速时间）
MIN_MEASURE_TIME = 1.0 # 最小测量时长（秒）
STABILITY_WINDOW = 4 # 稳定性采样窗口大小
STABILITY_THRESHOLD = 0.12 # 稳定性判定阈值（窗口内波动/均值）
SEGMENT_SAMPLE_LIMIT = 2 # m3u8分片采样测速数量
PLAYLIST_MAX_BYTES = 2 * 1024 * 1024 # 播放列表最大读取字节数

# 测速默认请求头（参考 iptv-api）
REQUEST_HEADERS = {
  "Accept": "*/*",
  "Connection": "keep-alive",
  "Accept-Language": "zh-CN,zh;q=0.8",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}

# ============================================================================
# 缓存配置
# ============================================================================
ENABLE_CACHE = True
CACHE_FILE = Path(__file__).parent / "iptv_speed_cache.json"
CACHE_EXPIRE_HOURS = 72
CACHE_EXPIRE_SEC = CACHE_EXPIRE_HOURS * 3600

# ============================================================================
# GitHub源配置
# ============================================================================
ENABLE_GITHUB = False
GITHUB_URLS = [
]
MAX_TEST_URLS_PER_CHANNEL = 8 # 每个频道最多测试的链接数
MAX_LINKS_PER_CHANNEL = 8 # 每个频道最终保留的最大有效链接数（与测试数一致，可独立调整）
GITHUB_TIMEOUT = 10
GITHUB_RETRIES = 1

# ============================================================================
# 输出配置
# ============================================================================
OUTPUT_DIR = Path(__file__).parent
OUTPUT_M3U = OUTPUT_DIR / "iptv_channels1.m3u"
OUTPUT_TXT = OUTPUT_DIR / "iptv_channels1.txt"

# ============================================================================
# 频道分类规则
# ============================================================================
CATEGORY_RULES = [
  {"name": "央视频道", "keywords": ["cctv", "cetv", "央视"]},
  {"name": "卫视频道", "keywords": ["卫视"]},
  {"name": "影视频道", "keywords": ["影视", "影院", "chc", "电影", "经典影"]},
  {"name": "体育频道", "keywords": ["体育", "赛事", "高尔夫", "劲爆"]},
  {"name": "纪实频道", "keywords": ["纪实", "探索", "记录", "人文", "自然"]},
]
GROUP_ORDER = ["央视频道", "卫视频道", "影视频道", "体育频道"]
Group_list=False
# ============================================================================
# 辅助正则与映射（无需修改）
# ============================================================================
CCTV_MAP = {
  "1": "综合", "2": "财经", "3": "综艺", "4": "国际", "5": "体育",
  "5+": "体育赛事", "6": "电影", "7": "军事农业", "8": "电视剧",
  "9": "纪录", "10": "科教", "11": "戏曲", "12": "社会与法",
  "13": "新闻", "14": "少儿", "15": "音乐", "16": "奥林匹克", "17": "农业农村",
}
CCTV_ORDER = [f"CCTV-{k}{v}" for k, v in CCTV_MAP.items() if k != "5+"]
CCTV_ORDER.insert(5, "CCTV-5+体育赛事")
CCTV_ORDER.append("CCTV-4K")
CCTV_RE = re.compile(r'(cctv)[-\s]?(5\+|\d{1,3})', re.IGNORECASE)
CHINESE_ONLY = re.compile(r'[^\u4e00-\u9fff]')
INTERNAL_IP = re.compile(r'^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|127\.0\.0\.1)')
CLEAR_SUFFIX_RE = re.compile(r'[\s\-_]*(高清|标清|4K|超清|蓝光|HD|FHD|UHD|2K|流畅|原画|精品|720P|1080P|2160P)', re.IGNORECASE)

# 反检测脚本
STEALTH_JS = """ // Step 1: Delete webdriver getter from prototype, redefine as data property // paer.js checks Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver') // If there's a getter -> flags webdriver_spoof. Data property (no getter) passes. delete Navigator.prototype.webdriver; Object.defineProperty(Navigator.prototype, 'webdriver', { value: undefined, writable: false, configurable: true }); // Step 2: Real chrome.runtime (paer.js checks chrome_runtime_missing) if (!window.chrome) window.chrome = {}; window.chrome.runtime = { connect: function() { return { onMessage: {addListener:function(){}}, postMessage:function(){}, onDisconnect: {addListener:function(){}} }; }, sendMessage: function() {}, onConnect: {addListener:function(){}, removeListener:function(){}, hasListener:function(){return false;}}, onMessage: {addListener:function(){}, removeListener:function(){}, hasListener:function(){return false;}}, getURL: function(p) { return 'chrome-extension://invalid/'+p; }, id: undefined }; // Step 3: Clean automation traces for (let k in window) { if (k.startsWith('cdc_') || k.startsWith('__webdriver') || k.startsWith('__driver') || k.startsWith('__selenium')) delete window[k]; } // Step 4: Permissions const origQuery = window.navigator.permissions.query; window.navigator.permissions.query = (p) => p.name==='notifications' ? Promise.resolve({state:Notification.permission}) : origQuery(p); """

# 测速解析正则与常量（参考 iptv-api）
RT_URL_PATTERN = re.compile(r'^(rtmp|rtsp)://.*$', re.IGNORECASE) # rtsp/rtmp流走FFmpeg测速
M3U8_CONTENT_TYPES = [ # m3u8内容类型
  "application/x-mpegurl", "application/vnd.apple.mpegurl",
  "audio/mpegurl", "audio/x-mpegurl",
]
AD_FILTER_KEYWORDS = [ # 广告/无信号占位源关键字
  "no_signal", "nosignal", "no-signal", "signal_offline",
  "no_video", "novideo", "advertisement", "advert",
  "placeholder", "default_video", "cctv_off", "/ad/", "/ads/",
]
AD_MAX_LOOP_DURATION = 90 # 短循环列表判定最大总时长（秒）

# ============================================================================
# 日志配置（实时刷新）
# ============================================================================
class BJFormatter(logging.Formatter):
  def formatTime(self, record, datefmt=None):
    dt = datetime.datetime.fromtimestamp(
      record.created,
      datetime.timezone(datetime.timedelta(hours=8))
    )
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
# 辅助函数（分类、去重、规范化等）
# ============================================================================
def build_classifier():
  compiled = []
  for rule in CATEGORY_RULES:
    pat = re.compile("|".join(re.escape(k.lower()) for k in rule["keywords"]))
    compiled.append((rule["name"], pat))
  return lambda name: next((g for g, p in compiled if p.search(name.lower())), None)

classify = build_classifier()

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

def clean_cn(name: str) -> str:
  return CHINESE_ONLY.sub('', name)

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

def progress_bar(cur: int, total: int, ok: int, fail: int, last_pct: int) -> int:
  if total == 0:
    return 0
  pct = int(cur / total * 100)
  if pct == last_pct and cur != total:
    return last_pct
  bar = '█' * (pct // 5) + '-' * (20 - pct // 5)
  logger.info(f"({pct}%) {bar} ({cur}/{total}) 成功{ok} 失败{fail}")
  sys.stdout.flush()
  return pct

# ============================================================================
# 模拟人类行为（反检测）
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
# 缓存读写
# ============================================================================
def load_cache() -> dict:
  if not ENABLE_CACHE or not CACHE_FILE.exists():
    return {}
  try:
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
      cache = json.load(f)
    now = time.time()
    valid = {}
    for url, data in cache.items():
      if isinstance(data, dict) and "ok" in data and "ts" in data:
        if now - data["ts"] < CACHE_EXPIRE_SEC:
          valid[url] = data
    logger.info(f"缓存加载: {len(valid)} 条有效")
    return valid
  except Exception as e:
    logger.debug(f"缓存加载异常: {e}")
    return {}

def save_cache(cache: dict):
  if not ENABLE_CACHE:
    return
  try:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
      json.dump(cache, f, ensure_ascii=False, indent=2)
  except Exception as e:
    logger.warning(f"保存缓存失败: {e}")

# ============================================================================
# 测速相关函数（参考 iptv-api：下载测速 + m3u8分片测速 + FFmpeg/ffprobe探测）
# ============================================================================
def get_resolution_value(resolution_str):
  """获取分辨率像素值（如 1920x1080 -> 2073600）"""
  try:
    if resolution_str:
      m = re.search(r"(\d+)[xX*](\d+)", resolution_str)
      if m:
        width, height = map(int, m.groups())
        return width * height
  except Exception:
    pass
  return 0

def _parse_time_to_seconds(t: str) -> float:
  """时间字符串转秒（支持 hh:mm:ss.ms / mm:ss.ms / ss.ms）"""
  if not t:
    return 0.0
  parts = [p.strip() for p in t.split(':') if p.strip() != ""]
  if not parts:
    return 0.0
  try:
    total = 0.0
    for i, part in enumerate(reversed(parts)):
      total += float(part) * (60 ** i)
    return total
  except Exception:
    return 0.0

@asynccontextmanager
async def _limit(semaphore):
  """信号量限流（无信号量时直通）"""
  if semaphore is None:
    yield
    return
  async with semaphore:
    yield

@asynccontextmanager
async def _speed_session(session):
  """复用传入会话，否则创建测速专用会话"""
  if session is not None:
    yield session
    return
  async with create_speed_test_session() as created_session:
    yield created_session

def create_speed_test_session(concurrency: int = SPEED_TEST_CONCURRENCY):
  """创建测速专用会话（参考 iptv-api：禁用SSL校验、限制单Host并发、DNS缓存）"""
  limit = max(1, int(concurrency or 1))
  return aiohttp.ClientSession(
    connector=aiohttp.TCPConnector(
      ssl=False, limit=limit, limit_per_host=min(2, limit), ttl_dns_cache=300
    ),
    timeout=aiohttp.ClientTimeout(total=None),
    trust_env=True,
  )

async def get_speed_with_download(url, headers=None, session=None,
                                  timeout=SPEED_TEST_TIMEOUT, semaphore=None):
  """
  下载测速（参考 iptv-api）：流式下载数据实时采样速率，
  速率稳定后提前结束，返回 speed(M/s)/delay(ms)/size(字节)/time(秒)
  """
  start_time = time.time()
  delay = -1
  total_size = 0
  min_bytes = 64 * 1024
  last_sample_time = start_time
  last_sample_size = 0
  speed_samples = deque(maxlen=STABILITY_WINDOW)

  if session is None:
    session = aiohttp.ClientSession(
      connector=aiohttp.TCPConnector(ssl=False), trust_env=True
    )
    created_session = True
  else:
    created_session = False

  try:
    async with _limit(semaphore):
      async with session.get(url, headers=headers, timeout=timeout) as response:
        if response.status != 200:
          raise Exception(f"Invalid response status: {response.status}")
        delay = int(round((time.time() - start_time) * 1000))
        async for chunk in response.content.iter_any():
          if chunk:
            total_size += len(chunk)
            now = time.time()
            elapsed = now - start_time
            delta_t = now - last_sample_time
            delta_b = total_size - last_sample_size
            if delta_t > 0 and delta_b > 0:
              inst_speed = delta_b / delta_t / 1024.0 / 1024.0
              speed_samples.append(inst_speed)
              last_sample_time = now
              last_sample_size = total_size
            if (elapsed >= MIN_MEASURE_TIME and total_size >= min_bytes
                    and len(speed_samples) >= STABILITY_WINDOW):
              mean = sum(speed_samples) / len(speed_samples)
              if mean > 0 and (max(speed_samples) - min(speed_samples)) / mean < STABILITY_THRESHOLD:
                total_time = elapsed
                return {
                  'speed': total_size / total_time / 1024 / 1024,
                  'delay': delay,
                  'size': total_size,
                  'time': total_time,
                }
  except Exception as e:
    logger.debug(f"下载测速失败 {url[:60]}: {type(e).__name__}: {e}")
  finally:
    if created_session:
      await session.close()
  total_time = time.time() - start_time
  speed_value = total_size / total_time / 1024 / 1024 if total_time > 0 else 0.0
  return {
    'speed': speed_value,
    'delay': delay,
    'size': total_size,
    'time': total_time,
  }

async def get_headers(url, headers=None, session=None, timeout=8, semaphore=None):
  """HEAD请求获取响应头（用于识别重定向与m3u8内容类型），HEAD失败时fallback到GET"""
  if session is None:
    session = aiohttp.ClientSession(
      connector=aiohttp.TCPConnector(ssl=False), trust_env=True
    )
    created_session = True
  else:
    created_session = False
  res_headers = {}
  try:
    async with _limit(semaphore):
      # 先尝试 HEAD
      try:
        async with session.head(url, headers=headers, timeout=timeout,
                               allow_redirects=False) as response:
          res_headers = dict(response.headers)
      except Exception as head_err:
        # 部分服务器不支持 HEAD，fallback 到 GET（不读取 body，只取 headers）
        try:
          async with session.get(url, headers=headers, timeout=timeout,
                                allow_redirects=False) as response:
            res_headers = dict(response.headers)
        except Exception as get_err:
          logger.debug(f"获取响应头失败 HEAD({type(head_err).__name__}) GET({type(get_err).__name__}): {url[:60]}")
  except Exception as e:
    logger.debug(f"get_headers 异常: {url[:60]} {type(e).__name__}: {e}")
  finally:
    if created_session:
      await session.close()
  return res_headers

async def get_url_content(url, headers=None, session=None,
                          timeout=SPEED_TEST_TIMEOUT, semaphore=None):
  """获取播放列表内容（限制最大字节数，防止误下大文件）"""
  if session is None:
    session = aiohttp.ClientSession(
      connector=aiohttp.TCPConnector(ssl=False), trust_env=True
    )
    created_session = True
  else:
    created_session = False
  content = ""
  try:
    async with _limit(semaphore):
      async with session.get(url, headers=headers, timeout=timeout) as response:
        if response.status == 200:
          payload = await response.content.read(PLAYLIST_MAX_BYTES + 1)
          if len(payload) > PLAYLIST_MAX_BYTES:
            raise Exception("Response too large")
          content = payload.decode(response.charset or "utf-8", errors="replace")
        else:
          raise Exception(f"Invalid response status: {response.status}")
  except Exception as e:
    logger.debug(f"获取URL内容失败 {url[:60]}: {type(e).__name__}: {e}")
  finally:
    if created_session:
      await session.close()
  return content

def check_m3u8_valid(headers: dict) -> bool:
  """根据Content-Type判断是否为m3u8（参考 iptv-api）"""
  content_type = headers.get('Content-Type', '').lower()
  if not content_type:
    return False
  return any(item in content_type for item in M3U8_CONTENT_TYPES)

def _parse_m3u8_attrs(attr_str: str) -> dict:
  """解析 #EXT-X-STREAM-INF 属性串"""
  attrs = {}
  for m in re.finditer(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', attr_str):
    key = m.group(1)
    val = m.group(2)
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
      val = val[1:-1]
    attrs[key] = val
  return attrs

def parse_m3u8(content: str):
  """
  轻量m3u8解析（参考 iptv-api 使用m3u8库的场景）：
  返回 (playlists, segments, segment_durations, is_endlist)
  - playlists: [{uri, bandwidth, resolution, frame_rate}] 多码率主列表
  - segments: 分片URI列表（媒体列表）
  """
  playlists = []
  segments = []
  segment_durations = []
  is_endlist = False
  pending_stream = None
  pending_duration = None
  for raw_line in content.splitlines():
    line = raw_line.strip()
    if not line:
      continue
    if line.startswith('#EXT-X-STREAM-INF:'):
      attrs = _parse_m3u8_attrs(line.split(':', 1)[1])
      resolution = None
      if attrs.get('RESOLUTION'):
        m = re.match(r'(\d+)[xX](\d+)', attrs['RESOLUTION'])
        if m:
          resolution = (int(m.group(1)), int(m.group(2)))
      frame_rate = None
      if attrs.get('FRAME-RATE'):
        try:
          frame_rate = float(attrs['FRAME-RATE'])
        except ValueError:
          frame_rate = None
      try:
        bandwidth = int(attrs.get('BANDWIDTH') or 0)
      except ValueError:
        bandwidth = 0
      pending_stream = {
        'bandwidth': bandwidth,
        'resolution': resolution,
        'frame_rate': frame_rate,
      }
    elif line.startswith('#EXTINF:'):
      try:
        pending_duration = float(line.split(':', 1)[1].split(',')[0])
      except (ValueError, IndexError):
        pending_duration = None
    elif line.startswith('#EXT-X-ENDLIST'):
      is_endlist = True
    elif line.startswith('#'):
      continue
    else:
      if pending_stream is not None:
        pending_stream['uri'] = line
        playlists.append(pending_stream)
        pending_stream = None
      else:
        segments.append(line)
        segment_durations.append(pending_duration or 0.0)
        pending_duration = None
  return playlists, segments, segment_durations, is_endlist

def is_ad_playlist(segments, segment_durations, is_endlist, base_url: str = "") -> bool:
  """识别无信号/广告等循环占位源（参考 iptv-api）"""
  if not segments:
    return False
  haystack = (base_url + " " + " ".join(segments)).lower()
  if any(keyword in haystack for keyword in AD_FILTER_KEYWORDS):
    return True
  if is_endlist:
    total_duration = sum(d or 0 for d in segment_durations)
    if 0 < total_duration <= AD_MAX_LOOP_DURATION:
      return True
  return False

async def ffmpeg_url(url, headers=None, timeout=SPEED_TEST_TIMEOUT):
  """
  FFmpeg测速（参考 iptv-api）：适用于rtsp/rtmp等协议流，
  监听输出统计，速率稳定后提前结束，返回stderr文本
  """
  headers_str = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
  args = [FFMPEG_PATH, "-nostdin", "-threads", "1", "-t", str(timeout)]
  if headers_str:
    args += ["-headers", headers_str]
  args += ["-http_persistent", "0", "-stats", "-i", url, "-f", "null", "-"]

  proc = None
  stderr_parts = []
  speed_samples = []
  bitrate_re = re.compile(r"bitrate=\s*([0-9\.]+)\s*k?bits/s", re.IGNORECASE)
  start = time.time()
  try:
    proc = await asyncio.create_subprocess_exec(
      *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    while True:
      try:
        line = await asyncio.wait_for(proc.stderr.readline(), timeout=0.5)
      except asyncio.TimeoutError:
        line = b''
      elapsed = time.time() - start

      if line == b'':
        if proc.returncode is None:
          if elapsed >= timeout:
            proc.kill()
            await proc.wait()
            break
          await asyncio.sleep(0)
          if proc.returncode is not None:
            break
          continue
        else:
          break

      stderr_parts.append(line)
      try:
        text = line.decode(errors="ignore")
      except Exception:
        text = ""

      m = bitrate_re.search(text)
      if m:
        try:
          kbps = float(m.group(1))
          speed_samples.append(kbps / 8.0 / 1024.0)
        except Exception:
          pass

      if elapsed >= MIN_MEASURE_TIME and len(speed_samples) >= STABILITY_WINDOW:
        window = speed_samples[-STABILITY_WINDOW:]
        mean = sum(window) / len(window)
        if mean > 0 and (max(window) - min(window)) / mean < STABILITY_THRESHOLD:
          try:
            proc.kill()
          except Exception:
            pass
          await proc.wait()
          break

    try:
      _out, err = await asyncio.wait_for(proc.communicate(), timeout=1)
      if err:
        stderr_parts.append(err)
    except asyncio.TimeoutError:
      try:
        proc.kill()
      except Exception:
        pass
      try:
        await proc.wait()
      except Exception:
        pass
    except Exception:
      try:
        proc.kill()
      except Exception:
        pass
      try:
        await proc.wait()
      except Exception:
        pass
  except Exception as e:
    logger.debug(f"ffmpeg 测速异常 {url[:60]}: {type(e).__name__}: {e}")
    if proc:
      try:
        proc.kill()
      except Exception:
        pass
      try:
        await proc.wait()
      except Exception:
        pass
  finally:
    if proc:
      try:
        proc.kill()
      except Exception:
        pass
      try:
        await proc.wait()
      except Exception:
        pass
  stderr_bytes = b"".join(stderr_parts)
  try:
    return stderr_bytes.decode(errors="ignore")
  except Exception:
    return None

def get_video_info(video_info):
  """
  从FFmpeg输出解析视频信息（参考 iptv-api）：
  resolution / fps / speed(M/s，按已解码字节、输出大小、码率顺序解析)
  """
  if not video_info:
    return {'resolution': None, 'fps': None, 'speed': None}

  resolution = None
  fps = None
  match = re.search(r"(\d{3,4}x\d{3,4})", video_info)
  if match:
    resolution = match.group(0)
  m_fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_info, re.IGNORECASE)
  if not m_fps:
    m_fps = re.search(r"(\d+(?:\.\d+)?)\s*tbr", video_info, re.IGNORECASE)
  if not m_fps:
    m_fps = re.search(r"(\d+(?:\.\d+)?)\s*tbn", video_info, re.IGNORECASE)
  if m_fps:
    try:
      fps = float(m_fps.group(1))
    except Exception:
      fps = None

  def parse_size_value(value_str, unit):
    try:
      val = float(value_str)
    except Exception:
      return 0.0
    if not unit:
      return val
    unit_lower = unit.lower()
    if unit_lower in ("b", "bytes"):
      return val
    if unit_lower in ("kib", "k"):
      return val * 1024.0
    if unit_lower == "kb":
      return val * 1000.0
    if unit_lower in ("mib", "mb"):
      return val * 1024.0 * 1024.0
    return val

  speed_val = None
  try:
    # 优先: video/audio已解码字节数 / 播放时长
    total_bytes = 0.0
    m_video_size = re.search(r"video:\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|kB|B|kb|KB)?", video_info, re.IGNORECASE)
    m_audio_size = re.search(r"audio:\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|kB|B|kb|KB)?", video_info, re.IGNORECASE)
    if m_video_size:
      total_bytes += parse_size_value(m_video_size.group(1), m_video_size.group(2))
    if m_audio_size:
      total_bytes += parse_size_value(m_audio_size.group(1), m_audio_size.group(2))
    m_time = re.search(r"time=\s*([0-9:.]+)", video_info)
    if total_bytes > 0 and m_time:
      secs = _parse_time_to_seconds(m_time.group(1))
      if secs > 0:
        speed_val = total_bytes / secs / 1024.0 / 1024.0
  except Exception:
    pass

  if speed_val is None:
    try:
      # 其次: 输出总大小 / 播放时长
      m_lsize = re.search(r"Lsize=\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|kB|MiB|B|kb|KB)?", video_info, re.IGNORECASE)
      m_size = re.search(r"size=\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|kB|MiB|B|kb|KB)?", video_info, re.IGNORECASE)
      m_time = re.search(r"time=\s*([0-9:.]+)", video_info)
      size_bytes = 0.0
      if m_lsize and m_lsize.group(1).upper() != "N/A":
        size_bytes = parse_size_value(m_lsize.group(1), m_lsize.group(2))
      elif m_size:
        size_bytes = parse_size_value(m_size.group(1), m_size.group(2))
      if size_bytes > 0 and m_time:
        secs = _parse_time_to_seconds(m_time.group(1))
        if secs > 0:
          speed_val = size_bytes / secs / 1024.0 / 1024.0
    except Exception:
      pass

  if speed_val is None:
    try:
      # 最后: 平均码率换算
      m_bitrate = re.search(r"bitrate=\s*([0-9.]+)\s*k?bits/s", video_info)
      if m_bitrate:
        kbps = float(m_bitrate.group(1))
        speed_val = kbps / 8.0 / 1024.0
    except Exception:
      pass

  return {'resolution': resolution, 'fps': fps, 'speed': speed_val}

async def probe_url(url, headers=None, timeout=SPEED_TEST_TIMEOUT):
  """使用ffprobe探测流元数据（分辨率/帧率），参考 iptv-api"""
  proc = None
  try:
    header_str = ''.join(f'{k}: {v}\r\n' for k, v in (headers or {}).items()) if headers else ''
    args = [
      FFPROBE_PATH, '-v', 'error',
      '-probesize', '512000',
      '-analyzeduration', '1000000',
      '-show_entries', 'stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate',
      '-print_format', 'json',
    ]
    if header_str:
      args += ['-headers', header_str]
    args += [url]
    proc = await asyncio.create_subprocess_exec(
      *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
      out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
      return None
    except Exception:
      return None
    if out:
      try:
        data = json.loads(out.decode('utf-8'))
        streams = data.get('streams', [])
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video:
          res = None
          w, h = video.get('width'), video.get('height')
          if w and h:
            res = f"{w}x{h}"
          fps = None
          for rate_key in ('avg_frame_rate', 'r_frame_rate'):
            rate_str = video.get(rate_key)
            if rate_str:
              try:
                if '/' in str(rate_str):
                  num, den = str(rate_str).split('/')
                  fps = float(num) / float(den) if float(den) != 0 else None
                else:
                  fps = float(rate_str)
                if fps:
                  break
              except Exception:
                fps = None
          return {'resolution': res, 'fps': fps}
      except Exception as e:
                logger.debug(f"ffprobe 解析失败 {url[:60]}: {type(e).__name__}: {e}")
  except Exception as e:
    logger.debug(f"ffprobe 异常 {url[:60]}: {type(e).__name__}: {e}")
  finally:
    if proc:
      try:
        proc.kill()
      except Exception:
        pass
      try:
        await proc.wait()
      except Exception:
        pass
  return None

def _build_headers_for_url(url: str) -> dict:
  """根据URL动态构建请求头：只有咪咕源才加咪咕Referer"""
  headers = {**REQUEST_HEADERS}
  host = ""
  try:
    host = urlparse(url).hostname or ""
  except:
    pass
  if host and ("migu" in host.lower() or "miguvideo" in host.lower()):
    headers["Referer"] = "https://www.miguvideo.com/"
  return headers

async def get_result(url, headers=None, resolution=None,
                     filter_resolution=OPEN_FILTER_RESOLUTION,
                     timeout=SPEED_TEST_TIMEOUT, session=None,
                     http_semaphore=None, probe_semaphore=None,
                     redirects_remaining=5):
  """
  获取单链接测速结果（参考 iptv-api get_result）：
  - rtsp/rtmp流: FFmpeg测速
  - m3u8: 解析播放列表（多码率取最高带宽）后采样分片测速
  - 其余: 直接流式下载测速
  - 分辨率未知时按需ffprobe探测
  返回: {speed(M/s), delay(ms), resolution, fps?}
  """
  info = {'speed': 0.0, 'delay': -1, 'resolution': resolution}
  location = None
  segment_urls = []
  is_rt = RT_URL_PATTERN.match(url) is not None
  
  # 如果外部没传 headers，按 URL 动态构建
  if headers is None:
    headers = _build_headers_for_url(url)
    
  try:
    url = quote(url, safe=':/?$&=@[]%').partition('$')[0]
    async with _speed_session(session) as active_session:
      if is_rt:
        async with _limit(probe_semaphore):
          start_time = time.time()
          ff_out = await ffmpeg_url(url, headers, timeout)
        if ff_out:
          parsed = get_video_info(ff_out)
          if parsed:
            info['delay'] = int(round((time.time() - start_time) * 1000))
            info['speed'] = parsed['speed'] or 0.0
            if parsed['resolution']:
              info['resolution'] = parsed['resolution']
            if parsed['fps']:
              info['fps'] = parsed['fps']
      else:
        res_headers = await get_headers(url, headers, active_session, semaphore=http_semaphore)
        location = res_headers.get('Location') if res_headers else None
        if location:
          if redirects_remaining <= 0:
            raise Exception("Too many redirects")
          info.update(await get_result(
            urljoin(url, location),
            headers,
            resolution,
            filter_resolution,
            timeout,
            session=active_session,
            http_semaphore=http_semaphore,
            probe_semaphore=probe_semaphore,
            redirects_remaining=redirects_remaining - 1,
          ))
        else:
          should_parse_m3u8 = ".m3u8" in url.lower() or check_m3u8_valid(res_headers)
          url_content = ""
          if should_parse_m3u8:
            url_content = await get_url_content(
              url, headers, active_session, timeout, semaphore=http_semaphore
            )
          
          # === 修复：m3u8 内容获取失败时 fallback 到直接下载测速 ===
          if should_parse_m3u8 and url_content:
            playlists, segments, seg_durations, is_endlist = parse_m3u8(url_content)
            valid_playlists = [p for p in playlists if p.get('uri')]
            if valid_playlists:
              # 多码率主列表: 取带宽最高的子列表
              best = max(valid_playlists, key=lambda p: p.get('bandwidth') or 0)
              if best.get('resolution'):
                w, h = best['resolution']
                info['resolution'] = f"{w}x{h}"
              if best.get('frame_rate'):
                info['fps'] = best['frame_rate']
              playlist_url = urljoin(url, best['uri'])
              playlist_content = await get_url_content(
                playlist_url, headers, active_session, timeout, semaphore=http_semaphore
              )
              if playlist_content:
                _pls, segs, durs, endlist = parse_m3u8(playlist_content)
                if OPEN_FILTER_AD and is_ad_playlist(segs, durs, endlist, playlist_url):
                  raise Exception("Ad source filtered")
                segment_urls = [urljoin(playlist_url, s) for s in segs]
            else:
              if OPEN_FILTER_AD and is_ad_playlist(segments, seg_durations, is_endlist, url):
                raise Exception("Ad source filtered")
              segment_urls = [urljoin(url, s) for s in segments]
            if not segment_urls:
              raise Exception("Segment urls not found")
          else:
            # 非 m3u8，或 m3u8 内容获取失败，都走直接下载测速
            res_info = await get_speed_with_download(
              url, headers, active_session, timeout, semaphore=http_semaphore
            )
            info.update({'speed': res_info['speed'], 'delay': res_info['delay']})
            
          if segment_urls:
            # 采样最后几个分片测速（直播流最新分片）
            sampled = segment_urls[-(SEGMENT_SAMPLE_LIMIT + 1):-1]
            if not sampled:
              sampled = segment_urls[-SEGMENT_SAMPLE_LIMIT:]
            tasks = [
              get_speed_with_download(
                ts_url, headers, active_session, timeout, semaphore=http_semaphore
              )
              for ts_url in sampled
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = [
              r for r in results
              if isinstance(r, dict) and r.get('size') and r.get('time')
            ]
            total_size = sum(r['size'] for r in valid_results)
            total_time = sum(r['time'] for r in valid_results)
            info['speed'] = total_size / total_time / 1024 / 1024 if total_time > 0 else 0
            delays = [r['delay'] for r in valid_results if r.get('delay', -1) >= 0]
            info['delay'] = int(sum(delays) / len(delays)) if delays else -1
  except Exception as e:
    # === 修复：不再静默吞掉异常，输出 debug 日志以便排查 ===
    logger.debug(f"get_result 失败 {url[:60]}: {type(e).__name__}: {e}")
  finally:
    if (filter_resolution and not is_rt and not location
            and not info.get('resolution') and info.get('delay') != -1):
      try:
        async with _limit(probe_semaphore):
          probed = await probe_url(url, headers, timeout=timeout)
        if probed:
          if probed.get('resolution'):
            info['resolution'] = probed['resolution']
          if probed.get('fps'):
            info['fps'] = probed['fps']
      except Exception as e:
        logger.debug(f"ffprobe 补充探测失败 {url[:60]}: {type(e).__name__}: {e}")
  return info

def get_sort_result(results):
  """
  过滤并排序测速结果（参考 iptv-api get_sort_result）：
  - 延迟无效(delay=-1)剔除
  - 速率低于分辨率对应最低速率剔除
  - 分辨率超出[min, max]范围剔除
  - 按 SORT_BY 维度排序
  """
  total_result = []
  for result in results:
    result_speed = result.get('speed') or 0
    result_delay = result.get('delay')
    resolution = result.get('resolution')
    if result_delay is None or result_delay == -1:
      continue
    # === 修复：无分辨率时使用更宽松的默认阈值，避免误杀 ===
    speed_threshold = RESOLUTION_SPEED_MAP.get(resolution, MIN_SPEED) if resolution else MIN_SPEED
    if OPEN_FILTER_SPEED and result_speed < speed_threshold:
      continue
    if OPEN_FILTER_RESOLUTION and resolution:
      resolution_value = get_resolution_value(resolution)
      if (resolution_value < get_resolution_value(MIN_RESOLUTION)
              or resolution_value > get_resolution_value(MAX_RESOLUTION)):
        continue
    total_result.append(result)

  def sort_key(item):
    keys = []
    for dim in SORT_BY:
      if dim == "speed":
        keys.append(-(item.get("speed") or 0))
      elif dim == "delay":
        delay = item.get("delay")
        keys.append(delay if isinstance(delay, (int, float)) and delay >= 0 else float("inf"))
      elif dim == "resolution":
        keys.append(-(get_resolution_value(item.get("resolution") or "") or 0))
    return tuple(keys)

  total_result.sort(key=sort_key)
  return total_result

def _finalize_result(result_map):
  """按iptv-api排序规则整理结果，每频道保留前N条有效链接"""
  final = {}
  for k, vs in result_map.items():
    sorted_vs = get_sort_result(vs)
    final[k] = [item['url'] for item in sorted_vs[:MAX_LINKS_PER_CHANNEL]]
  return final

# ============================================================================
# 批量测试流水线
# ============================================================================
async def batch_test_pipeline(channel_map: Dict[Tuple[str, str], List[str]]
                              ) -> Dict[Tuple[str, str], List[str]]:
  """
  批量测速流水线（参考 iptv-api）：
  1. 按频道截取待测链接
  2. 命中缓存的直接复用结果
  3. 并发测速（下载测速/m3u8分片测速/FFmpeg测速）
  4. 按速率/延迟/分辨率过滤并排序
  """
  if not channel_map:
    return {}

  if MAX_TEST_URLS_PER_CHANNEL > 0:
    channel_map = {k: v[:MAX_TEST_URLS_PER_CHANNEL] for k, v in channel_map.items()}
    capped_total = sum(len(v) for v in channel_map.values())
    logger.info(f"按配置截取: 每频道最多{MAX_TEST_URLS_PER_CHANNEL}个, 共{capped_total}个链接待测")

  cache = load_cache() if ENABLE_CACHE else {}
  new_cache = {}
  result_map = defaultdict(list)

  all_urls = []
  url_to_channel = {}
  cached_ok = 0

  for (g, n), urls in channel_map.items():
    for u in urls:
      if is_internal(u):
        continue
      ci = cache.get(u)
      if ci and isinstance(ci, dict) and "speed" in ci and "delay" in ci:
        if time.time() - ci.get("ts", 0) < CACHE_EXPIRE_SEC:
          result_map[(g, n)].append({
            "url": u,
            "speed": ci.get("speed", 0),
            "delay": ci.get("delay", -1),
            "resolution": ci.get("resolution"),
          })
          cached_ok += 1
          continue
      all_urls.append(u)
      url_to_channel[u] = (g, n)

  if not all_urls:
    logger.info(f"全部命中缓存: {cached_ok} 条")
    return _finalize_result(result_map)

  logger.info(
    f"=== 开始测速 ({len(all_urls)} 条, 并发:{SPEED_TEST_CONCURRENCY}, "
    f"超时:{SPEED_TEST_TIMEOUT}s, ffprobe并发:{PROBE_CONCURRENCY}) ==="
  )
  http_sem = asyncio.Semaphore(SPEED_TEST_CONCURRENCY)
  probe_sem = asyncio.Semaphore(PROBE_CONCURRENCY)

  ok = fail = 0
  lp = -1

  async with create_speed_test_session(SPEED_TEST_CONCURRENCY) as session:
    async def _test_one(url: str):
      # === 修复：不再硬编码咪咕 Referer，由 get_result 内部动态判断 ===
      return url, await get_result(
        url,
        headers=None,  # 传 None，让 get_result 内部按 URL 动态构建
        session=session,
        http_semaphore=http_sem,
        probe_semaphore=probe_sem,
      )

    tasks = [asyncio.ensure_future(_test_one(u)) for u in all_urls]
    done = 0
    for coro in asyncio.as_completed(tasks):
      url, res = await coro
      done += 1
      g, n = url_to_channel[url]

      if res.get("delay", -1) != -1:
        ok += 1
        result_map[(g, n)].append({
          "url": url,
          "speed": res.get("speed") or 0,
          "delay": res.get("delay"),
          "resolution": res.get("resolution"),
        })
      else:
        fail += 1

      new_cache[url] = {
        "ok": res.get("delay", -1) != -1,
        "speed": res.get("speed") or 0,
        "delay": res.get("delay", -1),
        "resolution": res.get("resolution"),
        "ts": time.time(),
      }
      lp = progress_bar(done, len(tasks), ok, fail, lp)

  if ENABLE_CACHE and new_cache:
    cache.update(new_cache)
    save_cache(cache)

  logger.info(f"测速完成: 可用 {ok}, 无效 {fail}")
  return _finalize_result(result_map)

# ============================================================================
# GitHub源下载与解析
# ============================================================================
async def download_github(url: str, session: aiohttp.ClientSession) -> str:
  req_url = quote(url, safe=":/?&=%#[]@!$'()*+,;-._~") if any(ord(c) > 127 for c in url) else url
  for attempt in range(1, GITHUB_RETRIES + 1):
    try:
      async with session.get(req_url, headers={'User-Agent': 'Mozilla/5.0'}) as r:
        if r.status == 200:
          text = await r.text()
          if not text or len(text.strip()) < 50:
            logger.warning(f"GitHub内容过短({len(text)}字符): {url[:80]}")
            continue
          head = text[:500].lower()
          if '<html' in head:
            logger.warning(f"GitHub返回的是HTML页面: {url[:80]}")
            continue
          return text
        else:
          logger.warning(f"GitHub状态码{r.status}: {url[:80]}")
    except Exception as e:
      logger.warning(f"GitHub下载失败({attempt}/{GITHUB_RETRIES}): {url[:80]} {type(e).__name__}")
      await asyncio.sleep(1)
  return ""

def parse_m3u_content(content: str) -> List[Tuple[str, str, str]]:
  channels = []
  name = ""
  for line in content.splitlines():
    line = line.strip()
    if line.startswith("#EXTINF"):
      m = re.search(r'group-title="([^"]*)",(.+)', line)
      if m:
        name = m.group(2).strip()
      else:
        m2 = re.search(r'#EXTINF:-1.*?,(.+)', line)
        name = m2.group(1).strip() if m2 else ""
    elif line.startswith("http") and name:
      url = line.strip()
      std_ch = unify_channel_name(name)
      g = classify(std_ch)
      if g:
        fn = std_ch if g == "央视频道" else clean_cn(std_ch)
        channels.append((g, fn, url))
        name = ""
  return channels

def parse_txt_content(content: str) -> List[Tuple[str, str, str]]:
  channels = []
  for line in content.splitlines():
    line = line.strip()
    if not line or line.startswith('#') or line.endswith('#genre#'):
      continue
    if ',' in line:
      parts = line.split(',', 1)
      if len(parts) == 2:
        name = parts[0].strip()
        url = parts[1].strip()
        if '$' in url:
          url = url.split('$')[0].strip()
        if name and url:
          std_ch = unify_channel_name(name)
          g = classify(std_ch)
          if g:
            fn = std_ch if g == "央视频道" else clean_cn(std_ch)
            channels.append((g, fn, url))
  return channels

async def fetch_github_sources() -> Tuple[List[Tuple[str, str, str]], List[set]]:
  if not ENABLE_GITHUB or not GITHUB_URLS:
    return [], []
  all_channels = []
  source_urls_list = []
  timeout = aiohttp.ClientTimeout(total=GITHUB_TIMEOUT)
  async with aiohttp.ClientSession(timeout=timeout) as session:
    tasks = [download_github(url, session) for url in GITHUB_URLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
      source_name = f"GitHub-{i+1}"
      if isinstance(result, Exception) or not result:
        logger.warning(f"{source_name}: 下载失败")
        source_urls_list.append(set())
        continue
      content = result.strip()
      if content.startswith('#EXTM3U') or '#EXTINF' in content:
        channels = parse_m3u_content(content)
      else:
        channels = parse_txt_content(content)
      logger.debug(f"{source_name}: 解析到 {len(channels)} 个频道")
      url_set = {url for _, _, url in channels}
      source_urls_list.append(url_set)
      all_channels.extend(channels)
    logger.info(f"GitHub 源合计: {len(all_channels)} 条原始链接")
  return all_channels, source_urls_list

# ============================================================================
# 目标站抓取（Playwright）
# ============================================================================
async def scrape_ips_playwright(ctx, filter_type: str, max_pages: int) -> list:
  entries = []
  seen = set()
  target_url = f"{TARGET_URL}?t={filter_type}&province=bj&q=北京联通&page=1&search_page_size={IPS_PER_PAGE}" if filter_type != "all" else f"{TARGET_URL}?province=bj&q=北京联通&page=1&search_page_size={IPS_PER_PAGE}"
  page = None
  filter_applied = False
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
          filter_applied = True
          break
        else:
          await asyncio.sleep(random.uniform(2, 4))
      else:
        filter_applied = True
        break
    except Exception as e:
      logger.warning(f"[PW] 目标页加载失败 {attempt+1}/5")
      page = None
      await asyncio.sleep(3)
  if page is None or page.is_closed():
    logger.error("[PW] 无法加载目标页，放弃抓取")
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
      logger.warning(f"[PW] 下一页按钮获取失败: {e}")
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
      logger.warning(f"[PW] 翻页失败: {e}")
      break
    current_page += 1

  logger.info(f"[PW] 共抓取 {len(entries)} 个IP")
  return entries

async def extract_detail_channels_playwright(ctx, detail_url: str) -> list:
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
    if "站点禁止" in page_title or "访问被拒绝" in page_text or "站点禁止" in page_text:
      logger.debug(f"[PW] 详情页被拒绝: {detail_url[:60]}")
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
        logger.debug(f"[PW] 构造频道列表URL: {channel_list_url[:80]}")

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
        logger.debug(f"详情页超时(>{DETAIL_MAX_SECONDS}s)强制停止: {detail_url[:60]}")
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
        logger.debug(f"翻页失败: {e}")
        break
  except Exception as e:
    logger.debug(f"[PW] 提取频道异常: {e}")
  finally:
    if page and not page.is_closed():
      try:
        await page.close()
      except:
        pass

  seen = set()
  unique = []
  for name, url in channels:
    if url not in seen:
      seen.add(url)
      unique.append((name, url))
  return unique

async def extract1_detail_channels_playwright(ctx, detail_url: str) -> list:
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
    if "站点禁止" in page_title or "访问被拒绝" in page_text or "站点禁止" in page_text:
      logger.debug(f"[PW] 详情页被拒绝: {detail_url[:60]}")
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
    logger.info(f" 详情页1: {detail_url}")
    if s_link:
      m = re.search(r'[?&]s=([^&]+)', s_link)
      if m:
        s_hash = m.group(1)
        t_match = re.search(r'[?&]t=([^&]+)', detail_url)
        t_type = t_match.group(1) if t_match else 'hotel'
        channel_list_url = f"{TARGET_URL}?s={s_hash}&t={t_type}&channels=1&format=txt"
        logger.debug(f"[PW] 构造频道列表URL: {channel_list_url[:80]}")

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
                channel_list_url = f"{TARGET_URL}?s={s_hash}&t={t_type}&channels=1&format=txt"
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

    
    for page_num in range(1, 2):
      if is_overtime():
        logger.debug(f"详情页超时(>{DETAIL_MAX_SECONDS}s)强制停止: {detail_url[:60]}")
        break


      page_channels = await page.evaluate(r"""
        () => {
          const results = [];
          const mat=[...document.body.innerText.matchAll(/\n(.+?),(.+?)\n/g)];
          mat.forEach(function(m){results.push({name:m[1],url:m[2]})});
          return results;
        }
      """)
      if not page_channels:
        break
      logger.info(f" 详情页w: {channel_list_url}")
      for ch in page_channels:
        name = ch.get('name', '').strip()
        url = ch.get('url', '').strip()
        if name and url:
          url = url.replace('&amp;', '&')
          if not url.startswith(('http://', 'https://')):
            url = DEFAULT_PROTOCOL + url
          channels.append((name, url))

   
      
  except Exception as e:
    logger.debug(f"[PW] 提取频道异常: {e}")
  finally:
    if page and not page.is_closed():
      try:
        await page.close()
      except:
        pass

  seen = set()
  unique = []
  for name, url in channels:
    if url not in seen:
      seen.add(url)
      unique.append((name, url))
  return unique
    
# ============================================================================
# URL去重
# ============================================================================
def deduplicate_urls(ch_map: Dict[Tuple[str, str], List[str]]) -> Dict[Tuple[str, str], List[str]]:
  url_to_ch = defaultdict(list)
  for (g, n), urls in ch_map.items():
    for u in urls:
      url_to_ch[u].append((g, n))
  url_chosen = {}
  for url, chs in url_to_ch.items():
    if len(chs) == 1:
      url_chosen[url] = chs[0]
    else:
      plus = [c for c in chs if '+' in c[1].lower()]
      url_chosen[url] = plus[0] if plus else max(chs, key=lambda c: len(c[1]))
  new_map = defaultdict(list)
  for (g, n), urls in ch_map.items():
    for u in urls:
      if url_chosen[u] == (g, n):
        new_map[(g, n)].append(u)
  return dict(new_map)

# ============================================================================
# 导出M3U/TXT
# ============================================================================
def export(ch_map: Dict[Tuple[str, str], List[str]]):
  now = datetime.datetime.now(
    datetime.timezone(datetime.timedelta(hours=8))
  ).strftime("%Y-%m-%d %H:%M:%S")
  groups = defaultdict(list)
  for (g, n), urls in ch_map.items():
    for u in urls:
      groups[g].append((n, u))

  cctv_weight = {name: idx for idx, name in enumerate(CCTV_ORDER)}

  with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
    f.write("#EXTM3U\n")
    for grp in GROUP_ORDER:
      if grp not in groups:
        continue
      chs = groups[grp]
      if grp == "央视频道":
        chs_sorted = sorted(chs, key=lambda x: cctv_weight.get(x[0], 9999))
      else:
        chs_sorted = sorted(chs, key=lambda x: x[0])
      for n, u in chs_sorted:
        if n.strip():
          f.write(f'#EXTINF:-1 group-title="{grp}",{n}\n{u}\n')
      f.write("\n")
    f.write(f'#EXTINF:-1 group-title="更新时间",{now}\nhttps://example.com\n')

  with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
    for grp in GROUP_ORDER:
      if grp not in groups:
        continue
      f.write(f"{grp},#genre#\n")
      chs = groups[grp]
      if grp == "央视频道":
        chs_sorted = sorted(chs, key=lambda x: cctv_weight.get(x[0], 9999))
      else:
        chs_sorted = sorted(chs, key=lambda x: x[0])
      for n, u in chs_sorted:
        if n.strip():
          f.write(f"{n},{u}\n")
      f.write("\n")
    f.write(f"更新时间,#genre#\n{now},https://example.com\n")

  logger.info(f"导出完成: {len(ch_map)} 个频道")

# ============================================================================
# 主函数
# ============================================================================
async def main():
  parser = argparse.ArgumentParser(description="IPTV源抓取工具（iptv-api测速版）")
  parser.add_argument("--type", default="multicast", help="抓取源类型: all/hotel/multicast/migu/other")
  parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="最大翻页数")
  parser.add_argument("--max-ips", type=int, default=MAX_IPS, help="最大IP数量, 0=无限制")
  parser.add_argument("--headless", default="true", help="无头模式: true/false")
  parser.add_argument("--skip-ffmpeg", action="store_true", help="跳过FFmpeg测速")
  parser.add_argument("--chrome-path", default="", help="Chrome路径")
  parser.add_argument("--skip-scrape", action="store_true", help="跳过目标站抓取")
  parser.add_argument("--skip-github", action="store_true", help="跳过GitHub源")
  args = parser.parse_args()

  config_raw_type = SCRAPE_SOURCE_FILTER
  cmd_raw_type = args.type
  if cmd_raw_type and cmd_raw_type.strip().lower() != "all":
    ft = norm_type(cmd_raw_type)
    logger.info(f"使用命令行指定类型: {ft}")
  else:
    ft = norm_type(config_raw_type)
    logger.info(f"使用配置默认类型: {ft}")

  max_pages = args.max_pages
  max_ips = args.max_ips
  headless = args.headless.lower() != "false" if args.headless else HEADLESS
  do_ffmpeg = ENABLE_FFMPEG and not args.skip_ffmpeg
  do_scrape = ENABLE_SCRAPE and not args.skip_scrape

  start_time = time.time()
  logger.info("=" * 60)
  logger.info("IPTV 源抓取工具启动")
  logger.info(f" 类型: {ft} | 抓取目标站: {'开启' if do_scrape else '关闭'} | GitHub: {'开启' if ENABLE_GITHUB and not args.skip_github else '关闭'} | FFmpeg: {'开启' if do_ffmpeg else '关闭'}")
  logger.info("=" * 60)

  all_channels = []
  github_sources_urls = []
  scrape_urls_set = set()

  # GitHub源
  if ENABLE_GITHUB and not args.skip_github:
    github_chs, github_sources_urls = await fetch_github_sources()
    for g, n, u in github_chs:
      all_channels.append((g, n, u))

  # 目标站抓取（Playwright）
  if do_scrape:
    logger.info("--- 开始目标站抓取 ---")
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
        logger.info("Chrome路径: 使用Playwright默认")

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
        except Exception as e:
          logger.warning(f"Playwright IP列表抓取失败: {e}")

        if max_ips > 0:
          entries = entries[:max_ips]
        
        if entries:
          for i, entry in enumerate(entries):
            try:
              detail_url = f"{TARGET_URL}?p={entry['hash']}&t={entry['type']}"
                

              #chs = await extract_detail_channels_playwright(ctx, detail_url)
              chs = await extract1_detail_channels_playwright(ctx, detail_url)
              for name, url in chs:
                #std_ch = unify_channel_name(name)
                g = classify(std_ch)
                
                if g :
                  #fn = std_ch if g == "央视频道" else clean_cn(std_ch)
                  all_channels.append((g, name, url))
                  scrape_urls_set.add(url)
                else:
                  all_channels.append(("其他", name, url))
                  scrape_urls_set.add(url)
              await asyncio.sleep(random.uniform(IP_DELAY_MIN, IP_DELAY_MAX))
            except Exception as e:
              logger.warning(f"IP {entry['ip']} 详情提取失败")

        try: await ctx.close()
        except: pass
        try: await browser.close()
        except: pass
    except Exception as e:
      logger.warning(f"Playwright整体失败: {e}")

  # 过滤内网IP
  before = len(all_channels)
  all_channels = [(g, n, u) for g, n, u in all_channels if not is_internal(u)]
  if before != len(all_channels):
    logger.info(f"过滤内网IP: {before} -> {len(all_channels)}")

  #

  ch_map = defaultdict(list)
  for g, n, u in all_channels:
    ch_map[(g, n)].append(u)
  ch_map = deduplicate_urls(ch_map)

  allowed = set(GROUP_ORDER)
  ch_map = {k: v for k, v in ch_map.items() if k[0] in allowed}

  total_links_before_test = sum(len(v) for v in ch_map.values())
  logger.info(f"去重后: {len(ch_map)} 个频道, {total_links_before_test} 条链接")

  # === 测速筛选 ===
  if do_ffmpeg and ch_map:
    logger.info("--- 开始测速筛选 ---")
    ff_start = time.time()
    ch_map = await batch_test_pipeline(ch_map)
    logger.info(f"测速耗时: {time.time() - ff_start:.1f}s")

  # 导出
  export(ch_map)

  # === 统计信息 ===
  final_urls = set()
  for urls in ch_map.values():
    final_urls.update(urls)

  logger.info("=" * 60)
  logger.info("来源有效性统计:")
  for i, url_set in enumerate(github_sources_urls, start=1):
    raw = len(url_set)
    effective = len(url_set & final_urls)
    pct = (effective / raw * 100) if raw else 0
    logger.info(f" GitHub 源{i}原始链接: 共{raw}条, 有效{effective}条, 有效率{pct:.1f}%")

  raw_scrape = len(scrape_urls_set)
  effective_scrape = len(scrape_urls_set & final_urls)
  pct_scrape = (effective_scrape / raw_scrape * 100) if raw_scrape else 0
  logger.info(f" 目标站抓取原始链接: 共{raw_scrape}条, 有效{effective_scrape}条, 有效率{pct_scrape:.1f}%")
  logger.info("=" * 60)

  total_time = time.time() - start_time
  logger.info(f"总耗时: {total_time:.1f}s")
  logger.info("=" * 60)

if __name__ == "__main__":
  asyncio.run(main())
