#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик статистики YouTube.
Раз в час (через GitHub Actions) опрашивает YouTube Data API v3,
сохраняет снимки счётчиков просмотров в data/history.json
и пересчитывает прирост за 24 часа / 7 дней / 30 дней в data/stats.json.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API_BASE = "https://www.googleapis.com/youtube/v3/"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
CHANNELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.txt")

VIDEOS_PER_CHANNEL = 50      # сколько последних видео канала отслеживать
MAX_AGE_DAYS = 32            # сколько дней хранить историю снимков
HOUR = 3600
DAY = 86400

WINDOWS = {"d24": 24 * HOUR, "d7": 7 * DAY, "d30": 30 * DAY}


# ---------------------------------------------------------------- API

def api(endpoint, **params):
    """Запрос к YouTube Data API v3."""
    params["key"] = os.environ["YT_API_KEY"]
    url = API_BASE + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (500, 503) and attempt < 2:
                time.sleep(5)
                continue
            raise RuntimeError(f"API error {e.code} on {endpoint}: {body[:500]}")
        except Exception:
            if attempt < 2:
                time.sleep(5)
                continue
            raise
    raise RuntimeError("unreachable")


def resolve_channel_id(line, cache):
    """Ссылка/хэндл канала -> channel ID (UC...). Результат кэшируется."""
    line = line.strip()
    if line in cache:
        return cache[line]

    cid = None
    m = re.search(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{10,})", line)
    if m:
        cid = m.group(1)
    elif line.startswith("UC") and re.fullmatch(r"UC[0-9A-Za-z_-]{10,}", line):
        cid = line
    else:
        handle = None
        m = re.search(r"youtube\.com/@([^/?&\s]+)", line)
        if m:
            handle = m.group(1)
        elif line.startswith("@"):
            handle = line[1:]
        if handle:
            resp = api("channels", part="id", forHandle=handle)
            items = resp.get("items", [])
            if items:
                cid = items[0]["id"]
        else:
            m = re.search(r"youtube\.com/(?:c|user)/([^/?&\s]+)", line)
            if m:
                name = m.group(1)
                resp = api("channels", part="id", forUsername=name)
                items = resp.get("items", [])
                if items:
                    cid = items[0]["id"]
                else:  # запасной путь — поиск (дороже по квоте, но один раз)
                    resp = api("search", part="snippet", type="channel",
                               q=name, maxResults=1)
                    items = resp.get("items", [])
                    if items:
                        cid = items[0]["snippet"]["channelId"]

    if cid:
        cache[line] = cid
    else:
        print(f"ВНИМАНИЕ: не удалось распознать канал: {line}", file=sys.stderr)
    return cid


def get_uploads_playlist(channel_id):
    resp = api("channels", part="contentDetails", id=channel_id)
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_recent_video_ids(playlist_id, limit=VIDEOS_PER_CHANNEL):
    ids, page_token = [], None
    while len(ids) < limit:
        params = dict(part="contentDetails", playlistId=playlist_id,
                      maxResults=min(50, limit - len(ids)))
        if page_token:
            params["pageToken"] = page_token
        resp = api("playlistItems", **params)
        ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_data(video_ids):
    """id -> {title, channel, published, views}. Пакетами по 50."""
    out = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = api("videos", part="snippet,statistics", id=",".join(batch))
        for item in resp.get("items", []):
            stats = item.get("statistics", {})
            if "viewCount" not in stats:  # премьеры/скрытые счётчики
                continue
            sn = item["snippet"]
            out[item["id"]] = {
                "title": sn["title"],
                "channel": sn["channelTitle"],
                "published": sn["publishedAt"],
                "views": int(stats["viewCount"]),
            }
    return out


# ------------------------------------------------------- история/расчёты

def parse_iso(ts):
    return int(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))) \
        - time.timezone


def thin_snaps(snaps, now):
    """Прореживание: <48ч — все точки, до 8 дней — раз в 6ч, дальше — раз в сутки."""
    keep, seen = [], set()
    for ts, views in snaps:
        age = now - ts
        if age > MAX_AGE_DAYS * DAY:
            continue
        if age <= 48 * HOUR:
            bucket = ("h", ts)
        elif age <= 8 * DAY:
            bucket = ("q", ts // (6 * HOUR))
        else:
            bucket = ("d", ts // DAY)
        if bucket in seen:
            continue
        seen.add(bucket)
        keep.append([ts, views])
    return keep


def growth(snaps, published_ts, current_views, window, now):
    """Прирост просмотров за окно window (сек). Возвращает (прирост, полное ли окно)."""
    cutoff = now - window
    if published_ts >= cutoff:
        return current_views, True  # видео моложе окна — все его просмотры «в окне»
    baseline, baseline_ts = None, None
    for ts, views in snaps:  # снимки отсортированы по времени
        if ts <= cutoff:
            baseline, baseline_ts = views, ts
        else:
            break
    if baseline is None:
        if not snaps:
            return 0, False
        return max(0, current_views - snaps[0][1]), False  # история короче окна
    return max(0, current_views - baseline), True


def compute_stats(history, now):
    videos_out = []
    for vid, v in history["videos"].items():
        snaps = v.get("snaps", [])
        if not snaps:
            continue
        current_views = snaps[-1][1]
        pub = parse_iso(v["published"])
        entry = {
            "id": vid,
            "title": v["title"],
            "channel": v["channel"],
            "published": v["published"],
            "views": current_views,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        }
        for key, window in WINDOWS.items():
            delta, full = growth(snaps, pub, current_views, window, now)
            entry[key] = delta
            entry[key + "_full"] = full
        videos_out.append(entry)
    return {
        "generated": int(now),
        "videos": videos_out,
    }


# --------------------------------------------------------------- main

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"channels_cache": {}, "videos": {}}


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    history = load_history()
    now = int(time.time())

    with open(CHANNELS_FILE, encoding="utf-8") as f:
        channel_lines = [l.strip() for l in f
                         if l.strip() and not l.strip().startswith("#")]

    # 1. Каналы -> ID -> последние видео
    tracked_ids = set()
    for line in channel_lines:
        cid = resolve_channel_id(line, history["channels_cache"])
        if not cid:
            continue
        playlist = get_uploads_playlist(cid)
        if not playlist:
            print(f"ВНИМАНИЕ: нет плейлиста загрузок у {line}", file=sys.stderr)
            continue
        tracked_ids.update(get_recent_video_ids(playlist))

    # 2. + видео, которые выпали из последних 50, но ещё имеют свежую историю
    for vid, v in history["videos"].items():
        snaps = v.get("snaps", [])
        if snaps and now - snaps[-1][0] <= MAX_AGE_DAYS * DAY:
            tracked_ids.add(vid)

    if not tracked_ids:
        print("Нет видео для отслеживания — проверь channels.txt", file=sys.stderr)
        sys.exit(1)

    # 3. Текущие счётчики
    data = fetch_video_data(sorted(tracked_ids))
    print(f"Получена статистика по {len(data)} видео")

    # 4. Обновление истории
    for vid, info in data.items():
        v = history["videos"].setdefault(vid, {"snaps": []})
        v["title"] = info["title"]
        v["channel"] = info["channel"]
        v["published"] = info["published"]
        v["snaps"].append([now, info["views"]])
        v["snaps"] = thin_snaps(v["snaps"], now)

    # 5. Удаление устаревших видео
    history["videos"] = {
        vid: v for vid, v in history["videos"].items()
        if v.get("snaps") and now - v["snaps"][-1][0] <= MAX_AGE_DAYS * DAY
    }

    # 6. Запись
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    stats = compute_stats(history, now)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Готово: {len(stats['videos'])} видео в stats.json")


if __name__ == "__main__":
    main()
