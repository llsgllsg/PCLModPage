# -*- coding: utf-8 -*-
"""
modrinth_api.py — Modrinth 模组数据层

流程: /v2/search 取最近 NEW_DAYS 天新发布的模组(好玩内容分类, 按人气排序)
      → 剔除基础/工具类 + 低关注垃圾 → 新度/关注/下载混合打分 →
      以当天为种子的加权抽样 → 排除近 14 天推过的 → 中文简介 → 返回卡片数据。

说明: 与 mc_daily_mods.py 的 CLI 脚本不同, 这里按 steam_api.py 的"每日推荐主页"思路,
      每天用固定种子加权抽一组不同的模组, 并保证近 14 天不重复。
      默认只推"新模组"(NEW_DAYS 天内发布), 想要老牌模组就把 NEW_DAYS 调大。
"""

from __future__ import annotations

import sys

# Windows 控制台默认可能不是 UTF-8, 强制 UTF-8 输出避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import concurrent.futures
import io
import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from PIL import Image

API_BASE = "https://api.modrinth.com/v2"
UA = {"User-Agent": "PCLModPage/1.0"}

# 复用连接的 Session: 对同一主机(api/cdn.modrinth.com)保持 keep-alive, 省去每请求的握手
_SESSION = requests.Session()
_SESSION.headers.update(UA)

CACHE_DIR = "cache"
CACHE_FILE = os.path.join(CACHE_DIR, "modrinth_mods.json")
CACHE_EXPIRE_MINUTES = 60

HISTORY_FILE = "history.json"
HISTORY_KEEP_DAYS = 14

IMAGE_DIR = "images"
# 图片引用前缀, 仅 --local-images 自托管模式使用。
# 空 = 自动用本机绝对路径(file:///…/images); 部署到服务器后填你的域名, 例如 "https://g-fish.dpdns.org/download/mods"
IMAGE_BASE_URL = ""

# 中文简介: 用 MCIM 镜像(mod.mcimirror.top)获取模组简介的 GPT 中文翻译, 失败自动回退英文
USE_TRANSLATE = True
MIRROR_BASE = "https://mod.mcimirror.top"
TRANSLATION_CACHE_FILE = os.path.join(CACHE_DIR, "translations.json")

# 下载按钮: 从 Modrinth 拿每个模组最新版本的 jar 直链(供 PCL2 "下载文件" 事件下载到当前版本 mods 文件夹)
DOWNLOAD_CACHE_FILE = os.path.join(CACHE_DIR, "downloads.json")

# ---- 筛选配置 ----
LIMIT = 12               # 每页模组数量
FUN_CATEGORIES = [       # 任一命中即算"好玩"(OR 关系)
    "adventure", "magic", "mobs", "food", "decoration", "worldgen",
    "game-mechanics", "minigame", "technology", "economy", "equipment",
    "transportation", "cursed", "social",
]
BORING_CATEGORIES = {"library", "optimization", "utility", "management"}
CATEGORY_CN = {          # 分类英文 → 中文
    "adventure": "冒险", "magic": "魔法", "mobs": "生物", "food": "食物",
    "decoration": "装饰", "worldgen": "世界生成", "game-mechanics": "机制",
    "minigame": "小游戏", "technology": "科技", "economy": "经济",
    "equipment": "装备", "transportation": "交通", "cursed": "恶搞", "social": "社交",
}
GAME_VERSION = None      # 例如 "1.20.1"; None = 不限
LOADER = None            # 例如 "fabric"; None = 不限
NEW_DAYS = 90            # 只看最近 N 天发布的新模组(想要更多老牌模组就调大, 如 9999)
MIN_FOLLOWS = 100        # 质量底线: 关注数低于此值的直接剔除(既要新又要评分好)

# ---- 混合打分权重 (满分为 100) ----
W_FOLLOWS = 0.40         # 关注数(近似评分)
W_DOWNLOADS = 0.20       # 下载量
W_RECENT = 0.40          # 新度(新发布 + 关注高 各占一半, 新老兼顾)

POOL_SIZE = 200          # 候选池大小(取排序最靠前的 N 个好玩模组)
TIMEOUT_SEC = 30


# ============================ 基础请求 ============================

def _http_get_json(url: str) -> dict:
    r = _SESSION.get(url, timeout=(5, TIMEOUT_SEC))
    r.raise_for_status()
    return r.json()


def _build_search_params(offset: int) -> dict:
    facets = [["project_type:mod"]]
    facets.append([f"categories:{c}" for c in FUN_CATEGORIES])
    if LOADER:
        facets.append([f"categories:{LOADER}"])
    if GAME_VERSION:
        facets.append([f"versions:{GAME_VERSION}"])
    # 只取最近 NEW_DAYS 天发布的新模组(new_filters 为 MeiliSearch 过滤语法)
    cutoff = int((datetime.now().astimezone() - timedelta(days=NEW_DAYS)).timestamp())
    return {
        "index": "follows",   # 在"新模组"里再按人气排序
        "limit": 100,
        "offset": offset,
        "facets": json.dumps(facets),
        "new_filters": f"created_timestamp>={cutoff}",
    }


def _is_wanted(m: dict) -> bool:
    """命中任一"好玩"分类, 不带基础/工具类标签, 且关注数不低于质量底线。"""
    cats = set(m.get("categories") or [])
    if cats & BORING_CATEGORIES:
        return False
    if (m.get("follows") or 0) < MIN_FOLLOWS:
        return False
    return any(c in cats for c in FUN_CATEGORIES)


def _fetch_pool(size: int = POOL_SIZE) -> list[dict]:
    """分页拉取候选池, 只保留好玩的模组。"""
    pool, offset = [], 0
    while len(pool) < size:
        url = API_BASE + "/search?" + urlencode(_build_search_params(offset))
        data = _http_get_json(url)
        hits = data.get("hits") or []
        raw = len(hits)
        if raw == 0:
            break
        pool.extend(m for m in hits if _is_wanted(m))
        offset += raw              # 按原始条数前进, 避免漏页/死循环
        total = data.get("total_hits", offset)
        if offset >= total:
            break
    return pool[:size]


def _search_signature() -> dict:
    """搜索参数签名: 参数变了缓存自动失效(比如改了 NEW_DAYS)。"""
    return {
        "new_days": NEW_DAYS,
        "index": "follows",
        "min_follows": MIN_FOLLOWS,
        "loader": LOADER,
        "game_version": GAME_VERSION,
    }


def _load_or_fetch_pool() -> list[dict]:
    """读 60 分钟缓存(参数一致才复用), 否则抓取。"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
                ts = datetime.fromisoformat(cache["timestamp"])
                if (datetime.now() - ts < timedelta(minutes=CACHE_EXPIRE_MINUTES)
                        and cache.get("config") == _search_signature()):
                    return cache["pool"]
        except Exception as e:
            print(f"[警告] 读取缓存失败: {e}")
    print("[信息] 正在从 Modrinth 获取模组数据...")
    pool = _fetch_pool()
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"timestamp": datetime.now().isoformat(),
                   "config": _search_signature(),
                   "pool": pool},
                  f, ensure_ascii=False, indent=2)
    return pool


# ============================ 打分与抽样 ============================

def _minmax_norm(values: dict) -> dict:
    """把数值归一化到 0-100: {key: 0-100}。"""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    rng = hi - lo
    if rng == 0:
        return {k: 50.0 for k in values}
    return {k: (v - lo) / rng * 100 for k, v in values.items()}


def _freshness_score(date_created) -> float:
    """按发布时间打分: 当天发布=100, 每过 1 天减 1 分, 满 100 天归零。"""
    if not date_created:
        return 0
    try:
        created = datetime.fromisoformat(str(date_created).replace("Z", "+00:00"))
        days = max(0, (datetime.now().astimezone() - created).days)
    except Exception:
        return 0
    return max(0.0, 100 - days)


def _compute_score(m: dict, n_follows: dict, n_downloads: dict) -> float:
    return (
        W_FOLLOWS * n_follows.get(m["project_id"], 0)
        + W_DOWNLOADS * n_downloads.get(m["project_id"], 0)
        + W_RECENT * _freshness_score(m.get("date_created"))
    )


def _weighted_sample(rng: random.Random, pool: list[dict], k: int) -> list[dict]:
    """按 score 加权随机抽 k 个(不重复), 分数越高越容易被选中。
    保底权重 1, 保证低分模组偶尔也能出现; 同一天重复跑结果一致。"""
    if len(pool) <= k:
        return pool[:]
    pool = list(pool)
    weights = [max(m.get("score", 0), 1) for m in pool]
    chosen = []
    for _ in range(k):
        total = sum(weights)
        r = rng.random() * total
        acc = 0
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                chosen.append(pool.pop(i))
                weights.pop(i)
                break
    return chosen


def _day_number() -> int:
    """1970-01-01 至今的天数, 用作每日随机种子(同一天重复跑结果一致)。"""
    return (date.today() - date(1970, 1, 1)).days


# ============================ 历史去重 ============================

def _load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("history") or {}
    except Exception:
        return {}


def _prune_history(history: dict) -> dict:
    """只保留最近 HISTORY_KEEP_DAYS 天的记录, 按日期升序返回。"""
    cutoff = (date.today() - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    return dict(sorted((d, slugs) for d, slugs in history.items() if d >= cutoff))


def _recent_slugs(history: dict, window: int) -> set:
    """最近 window 天内推过的 slug 集合。"""
    slugs = set()
    for d in sorted(history)[-window:] if window else []:
        slugs.update(history[d])
    return slugs


def _save_history(history: dict) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"history": history}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 写入去重记录失败: {e}")


# ============================ 图片处理 ============================

def _download_png(slug: str, url: str, path: str) -> None:
    """下载图标并转成 png(PCL2/WPF 不认 webp)。
    注意: Modrinth 图标只有 _96 一种尺寸(实测 _128/_256/_512 均不存在),
    直接用搜索返回的 icon_url 即可, 不要再探测其他尺寸。"""
    r = _SESSION.get(url, timeout=(3, 10))
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    img = img.convert("RGB")
    img.save(path, "PNG")


def _download_images(mods: list[dict]) -> set:
    """把选中模组的图标转 png 存到 images/{slug}.png, 清理不再引用的旧图。
    返回成功下载的 slug 集合; 已存在的直接复用。"""
    if not mods:
        return set()
    os.makedirs(IMAGE_DIR, exist_ok=True)
    keep = {m["slug"] for m in mods}
    ok = set()

    # 只对还没缓存过的模组发起下载
    todo = []
    for m in mods:
        url = _pick_image(m)
        if not url:
            continue
        path = os.path.join(IMAGE_DIR, f"{m['slug']}.png")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ok.add(m["slug"])          # 已缓存, 复用
            continue
        todo.append((m["slug"], url, path))

    # 并发下载, 大幅缩短等待(串行 12 张 → 并发约 2 批)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_download_png, slug, url, path): slug for slug, url, path in todo}
        for fut in concurrent.futures.as_completed(futures):
            slug = futures[fut]
            try:
                fut.result()
                ok.add(slug)
                print(f"[信息] 已缓存图标 {slug}.png")
            except Exception as e:
                print(f"[警告] 下载图标失败 {slug}: {e}")

    for name in os.listdir(IMAGE_DIR):
        if not name.endswith(".png"):
            continue
        slug = name[:-4]
        if slug not in keep:
            try:
                os.remove(os.path.join(IMAGE_DIR, name))
            except OSError:
                pass
    return ok


def _cn_categories(m: dict) -> list[str]:
    """取模组的好玩分类中文标签, 最多 3 个。"""
    cats = [c for c in (m.get("categories") or []) if c in CATEGORY_CN]
    return [CATEGORY_CN[c] for c in cats[:3]]


def _pick_image(m: dict) -> str:
    """选图片: 优先横版 featured_gallery(更清晰, 类似 Steam 横幅), 没有则用方形图标。
    两者都是 Modrinth CDN 的 webp 地址, PCL2 的 MyImage 原生支持。"""
    return ((m.get("featured_gallery") or "").strip() or (m.get("icon_url") or "").strip())


def _load_translation_cache() -> dict:
    if os.path.exists(TRANSLATION_CACHE_FILE):
        try:
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_translation_cache(cache: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 写入翻译缓存失败: {e}")


def _fetch_translations(mods: list[dict]) -> None:
    """从 MCIM 镜像批量获取中文简介(带本地缓存), 失败则保持英文, 绝不中断主流程。"""
    if not mods or not USE_TRANSLATE:
        return
    cache = _load_translation_cache()
    missing = [m for m in mods if str(m["project_id"]) not in cache]
    if missing:
        ids = [m["project_id"] for m in missing]
        try:
            r = _SESSION.post(
                f"{MIRROR_BASE}/translate/modrinth",
                params={"project_id": ids[0]},
                json={"project_ids": ids},
                timeout=(5, 20),
            )
            r.raise_for_status()
            for item in r.json():
                cache[str(item.get("project_id"))] = item.get("translated")
            _save_translation_cache(cache)
        except Exception as e:
            print(f"[警告] 获取中文简介失败(将用英文): {e}")
    for m in mods:
        zh = cache.get(str(m["project_id"]))
        if zh:
            m["description"] = zh


def _load_download_cache() -> dict:
    if os.path.exists(DOWNLOAD_CACHE_FILE):
        try:
            with open(DOWNLOAD_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_download_cache(cache: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(DOWNLOAD_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 写入下载链接缓存失败: {e}")


def _latest_jar_url(project_id: str) -> str | None:
    """查 Modrinth /project/{id}/version 拿最新版本的 jar 直链(primary 文件优先)。"""
    r = _SESSION.get(f"{API_BASE}/project/{project_id}/version", timeout=(5, 15))
    r.raise_for_status()
    for v in r.json():
        for f in v.get("files") or []:
            if f.get("primary"):
                return f["url"]
        if v.get("files"):
            return v["files"][0]["url"]
    return None


def _fetch_download_urls(mods: list[dict]) -> None:
    """并行获取每个模组的下载直链(带缓存), 失败则无下载按钮。"""
    if not mods:
        return
    cache = _load_download_cache()
    missing = [m for m in mods if str(m["project_id"]) not in cache]
    if missing:
        def fetch(m):
            try:
                return m["project_id"], _latest_jar_url(m["project_id"])
            except Exception:
                return m["project_id"], None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for pid, url in ex.map(fetch, missing):
                cache[str(pid)] = url
        _save_download_cache(cache)
    for m in mods:
        m["download_url"] = cache.get(str(m["project_id"]))


# ============================ 主入口 ============================

def get_mods(limit: int = LIMIT, use_cdn: bool = True, pool: list[dict] | None = None,
             record_history: bool = True) -> list[dict]:
    """返回当天推荐模组卡片数据(已去重)。

    use_cdn=True(默认): 图片直接引用 Modrinth CDN 的 webp 地址,
        PCL2 的 MyImage 原生支持 webp + 网络图片自动缓存, 本地订阅即可显示。
    use_cdn=False: 下载图片转 png 到 images/ 并引用 IMAGE_BASE_URL(自托管用)。
    简介: 默认通过 MCIM 镜像把英文简介替换成中文, 失败自动回退英文。
    pool 可传入预取的候选池(测试用), 为 None 时读缓存/抓取。
    record_history=False 时不写历史(预览用)。
    """
    if pool is None:
        pool = _load_or_fetch_pool()
    if not pool:
        return []

    # 打分
    n_follows = _minmax_norm({m["project_id"]: m.get("follows", 0) for m in pool})
    n_downloads = _minmax_norm({m["project_id"]: m.get("downloads", 0) for m in pool})
    for m in pool:
        m["score"] = _compute_score(m, n_follows, n_downloads)

    # 去重: 排除近 14 天推过的; 候选不足时逐步放宽窗口
    history = _prune_history(_load_history())
    eligible = []
    for window in range(HISTORY_KEEP_DAYS, -1, -1):
        recent = _recent_slugs(history, window)
        eligible = [m for m in pool if m["slug"] not in recent]
        if len(eligible) >= limit or window == 0:
            break

    # 用当天日期做种子, 按分数加权抽样
    rng = random.Random(_day_number())
    selected = _weighted_sample(rng, eligible, limit)

    # 写回当天历史
    if record_history:
        history = _prune_history(_load_history())
        history[date.today().isoformat()] = [m["slug"] for m in selected]
        _save_history(history)

    # 中文简介 + 下载直链 (只对选中的模组, 失败不影响主流程)
    _fetch_translations(selected)
    _fetch_download_urls(selected)

    # 图片 (只对选中的模组做网络请求)
    if use_cdn:
        for m in selected:
            m["img"] = _pick_image(m)
    else:
        ok = _download_images(selected)
        # PCL2 加载 XAML 时相对路径按它自己的工作目录解析, 必须用绝对 URL:
        # 本地用 file:/// 绝对路径, 部署后由 IMAGE_BASE_URL 指定 https 地址
        base = IMAGE_BASE_URL.rstrip("/") if IMAGE_BASE_URL else Path(IMAGE_DIR).resolve().as_uri()
        for m in selected:
            m["img"] = f"{base}/{m['slug']}.png" if m["slug"] in ok else _pick_image(m)

    return [
        {
            "slug": m["slug"],
            "title": m["title"],
            "author": m["author"],
            "img": m["img"],
            "downloads": m.get("downloads", 0),
            "follows": m.get("follows", 0),
            "categories": _cn_categories(m),
            "description": (m.get("description") or "").strip(),
            "download_url": m.get("download_url") or "",
            "url": f"https://modrinth.com/mod/{m['slug']}",
        }
        for m in selected
    ]
