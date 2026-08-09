from __future__ import annotations

import sys

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
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from PIL import Image

API_BASE = "https://api.modrinth.com/v2"
UA = {"User-Agent": "PCLModPage/1.0"}

_SESSION = requests.Session()
_SESSION.headers.update(UA)

CACHE_DIR = "cache"
CACHE_FILE = os.path.join(CACHE_DIR, "modrinth_mods.json")
CACHE_EXPIRE_MINUTES = 60

HISTORY_FILE = "history.json"
HISTORY_KEEP_DAYS = 14

IMAGE_DIR = "images"
IMAGE_BASE_URL = "https://g-fish.dpdns.org/download/images"

USE_TRANSLATE = True
MIRROR_BASE = "https://mod.mcimirror.top"
TRANSLATION_CACHE_FILE = os.path.join(CACHE_DIR, "translations.json")

DOWNLOAD_CACHE_FILE = os.path.join(CACHE_DIR, "downloads.json")

ICON_CACHE_DIR = os.path.join(CACHE_DIR, "icons")

LIMIT = 12
FUN_CATEGORIES = [
    "adventure", "magic", "mobs", "food", "decoration", "worldgen",
    "game-mechanics", "minigame", "technology", "economy", "equipment",
    "transportation", "cursed", "social",
]
BORING_CATEGORIES = {"library", "optimization", "utility", "management"}
CATEGORY_CN = {
    "adventure": "冒险", "magic": "魔法", "mobs": "生物", "food": "食物",
    "decoration": "装饰", "worldgen": "世界生成", "game-mechanics": "机制",
    "minigame": "小游戏", "technology": "科技", "economy": "经济",
    "equipment": "装备", "transportation": "交通", "cursed": "恶搞", "social": "社交",
}
GAME_VERSION = None
LOADER = None
NEW_DAYS = 365
MIN_FOLLOWS = 50

W_FOLLOWS = 0.40
W_DOWNLOADS = 0.20
W_RECENT = 0.40

POOL_SIZE = 200
TIMEOUT_SEC = 30


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
    cutoff = int((datetime.now().astimezone() - timedelta(days=NEW_DAYS)).timestamp())
    return {
        "index": "follows",
        "limit": 100,
        "offset": offset,
        "facets": json.dumps(facets),
        "new_filters": f"created_timestamp>={cutoff}",
    }


def _is_wanted(m: dict) -> bool:
    cats = set(m.get("categories") or [])
    if cats & BORING_CATEGORIES:
        return False
    if (m.get("follows") or 0) < MIN_FOLLOWS:
        return False
    return any(c in cats for c in FUN_CATEGORIES)


def _fetch_pool(size: int = POOL_SIZE) -> list[dict]:
    pool, offset = [], 0
    while len(pool) < size:
        url = API_BASE + "/search?" + urlencode(_build_search_params(offset))
        data = _http_get_json(url)
        hits = data.get("hits") or []
        raw = len(hits)
        if raw == 0:
            break
        pool.extend(m for m in hits if _is_wanted(m))
        offset += raw
        total = data.get("total_hits", offset)
        if offset >= total:
            break
    return pool[:size]


def _search_signature() -> dict:
    return {
        "new_days": NEW_DAYS,
        "index": "follows",
        "min_follows": MIN_FOLLOWS,
        "loader": LOADER,
        "game_version": GAME_VERSION,
    }


def _load_or_fetch_pool() -> list[dict]:
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


def _minmax_norm(values: dict) -> dict:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    rng = hi - lo
    if rng == 0:
        return {k: 50.0 for k in values}
    return {k: (v - lo) / rng * 100 for k, v in values.items()}


def _freshness_score(date_created) -> float:
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


def _load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("history") or {}
    except Exception:
        return {}


def _prune_history(history: dict) -> dict:
    cutoff = (date.today() - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    return dict(sorted((d, slugs) for d, slugs in history.items() if d >= cutoff))


def _recent_slugs(history: dict, window: int) -> set:
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


def _download_png(slug: str, url: str, path: str) -> None:
    r = _SESSION.get(url, timeout=(3, 10))
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    img = img.convert("RGB")
    img.save(path, "PNG")


def _download_images(mods: list[dict]) -> set:
    if not mods:
        return set()
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(ICON_CACHE_DIR, exist_ok=True)
    keep = {m["slug"] for m in mods}
    ok = set()

    todo = []
    for m in mods:
        slug = m["slug"]
        url = _pick_image(m)
        if not url:
            continue
        path = os.path.join(IMAGE_DIR, f"{slug}.png")
        cache_path = os.path.join(ICON_CACHE_DIR, f"{slug}.png")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ok.add(slug)
            continue
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            shutil.copyfile(cache_path, path)
            ok.add(slug)
            continue
        todo.append((slug, url, path, cache_path))

    def _download_to_cache(slug, url, path, cache_path):
        _download_png(slug, url, cache_path)
        shutil.copyfile(cache_path, path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_download_to_cache, *t): t[0] for t in todo}
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
    cats = [c for c in (m.get("categories") or []) if c in CATEGORY_CN]
    return [CATEGORY_CN[c] for c in cats[:3]]


def _latest_version(m: dict) -> str:
    best_key, best = None, ""
    for v in (m.get("versions") or []):
        parts = str(v).split(".")
        if not (2 <= len(parts) <= 3):
            continue
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            continue
        key = tuple(nums + [0] * (3 - len(nums)))
        if best_key is None or key > best_key:
            best_key, best = key, v
    return best


def _pick_image(m: dict) -> str:
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


def get_mods(limit: int = LIMIT, use_cdn: bool = False, pool: list[dict] | None = None,
             record_history: bool = True) -> list[dict]:
    if pool is None:
        pool = _load_or_fetch_pool()
    if not pool:
        return []

    n_follows = _minmax_norm({m["project_id"]: m.get("follows", 0) for m in pool})
    n_downloads = _minmax_norm({m["project_id"]: m.get("downloads", 0) for m in pool})
    for m in pool:
        m["score"] = _compute_score(m, n_follows, n_downloads)

    history = _prune_history(_load_history())
    eligible = []
    for window in range(HISTORY_KEEP_DAYS, -1, -1):
        recent = _recent_slugs(history, window)
        eligible = [m for m in pool if m["slug"] not in recent]
        if len(eligible) >= limit or window == 0:
            break

    rng = random.Random()
    selected = _weighted_sample(rng, eligible, limit)

    if record_history:
        history = _prune_history(_load_history())
        history[date.today().isoformat()] = [m["slug"] for m in selected]
        _save_history(history)

    _fetch_translations(selected)
    _fetch_download_urls(selected)

    if use_cdn:
        for m in selected:
            m["img"] = _pick_image(m)
    else:
        ok = _download_images(selected)
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
            "version": _latest_version(m),
            "description": (m.get("description") or "").strip(),
            "download_url": m.get("download_url") or "",
            "url": f"https://modrinth.com/mod/{m['slug']}",
        }
        for m in selected
    ]
