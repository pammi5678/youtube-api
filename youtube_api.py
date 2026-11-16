"""
YouTube Data API wrapper

Setup:
    pip install fastapi uvicorn httpx
    export YOUTUBE_API_KEY="your-key"      # Windows: set YOUTUBE_API_KEY=...
    uvicorn youtube_api:app --reload
Docs:
    http://127.0.0.1:8000/docs

Quota notes (default 10,000 units/day): search = 100 units per call,
videos/channels/comments = 1 unit per call. Responses are cached for
5 minutes to save quota.
"""
import os
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

API_KEY = os.getenv("YOUTUBE_API_KEY")
BASE = "https://www.googleapis.com/youtube/v3"
CACHE_TTL = 300  # seconds

app = FastAPI(title="YouTube Data API Wrapper", version="1.0.0")
_cache: dict[str, tuple[float, Any]] = {}


async def yt_get(endpoint: str, params: dict) -> dict:
    if not API_KEY:
        raise HTTPException(500, "Set the YOUTUBE_API_KEY environment variable.")
    params = {**params, "key": API_KEY}
    cache_key = endpoint + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "key")
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BASE}/{endpoint}", params=params)
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text)
        raise HTTPException(r.status_code, f"YouTube API error: {msg}")
    data = r.json()
    _cache[cache_key] = (time.time(), data)
    return data


def _int(v: Optional[str]) -> Optional[int]:
    return int(v) if v is not None else None


def shape_video(item: dict) -> dict:
    sn, st, cd = item.get("snippet", {}), item.get("statistics", {}), item.get("contentDetails", {})
    return {
        "id": item["id"],
        "title": sn.get("title"),
        "description": sn.get("description"),
        "channel_id": sn.get("channelId"),
        "channel_title": sn.get("channelTitle"),
        "published_at": sn.get("publishedAt"),
        "tags": sn.get("tags", []),
        "duration": cd.get("duration"),  # ISO 8601, e.g. PT4M13S
        "views": _int(st.get("viewCount")),
        "likes": _int(st.get("likeCount")),
        "comments": _int(st.get("commentCount")),
        "thumbnail": sn.get("thumbnails", {}).get("high", {}).get("url"),
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


@app.get("/")
def root():
    return {"service": "YouTube API wrapper", "docs": "/docs", "key_configured": bool(API_KEY)}


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    type: str = Query("video", pattern="^(video|channel|playlist)$"),
    max_results: int = Query(10, ge=1, le=50),
    order: str = Query("relevance", pattern="^(relevance|date|viewCount|rating)$"),
    page_token: Optional[str] = None,
):
    """Search YouTube. Costs 100 quota units per uncached call."""
    params = {"part": "snippet", "q": q, "type": type, "maxResults": max_results, "order": order}
    if page_token:
        params["pageToken"] = page_token
    data = await yt_get("search", params)
    results = []
    for it in data.get("items", []):
        sn = it["snippet"]
        id_obj = it["id"]
        results.append({
            "id": id_obj.get("videoId") or id_obj.get("channelId") or id_obj.get("playlistId"),
            "kind": id_obj["kind"].split("#")[-1],
            "title": sn.get("title"),
            "channel_title": sn.get("channelTitle"),
            "published_at": sn.get("publishedAt"),
            "description": sn.get("description"),
            "thumbnail": sn.get("thumbnails", {}).get("high", {}).get("url"),
        })
    return {"next_page_token": data.get("nextPageToken"), "results": results}


@app.get("/videos/{video_id}")
async def get_video(video_id: str):
    data = await yt_get("videos", {"part": "snippet,statistics,contentDetails", "id": video_id})
    if not data.get("items"):
        raise HTTPException(404, "Video not found.")
    return shape_video(data["items"][0])


@app.get("/videos")
async def get_videos(ids: str = Query(..., description="Comma-separated video IDs (max 50)")):
    id_list = [i for i in ids.split(",") if i]
    if not 1 <= len(id_list) <= 50:
        raise HTTPException(422, "Provide between 1 and 50 video IDs.")
    data = await yt_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(id_list)})
    return [shape_video(i) for i in data.get("items", [])]


@app.get("/channels/{channel_id}")
async def get_channel(channel_id: str):
    """Look up by channel ID (UC...). Use ?handle=@name on /channels instead for handles."""
    data = await yt_get("channels", {"part": "snippet,statistics", "id": channel_id})
    return _shape_channel(data)


@app.get("/channels")
async def get_channel_by_handle(handle: str = Query(..., examples=["@Google"])):
    data = await yt_get("channels", {"part": "snippet,statistics", "forHandle": handle})
    return _shape_channel(data)


def _shape_channel(data: dict) -> dict:
    if not data.get("items"):
        raise HTTPException(404, "Channel not found.")
    it = data["items"][0]
    sn, st = it["snippet"], it.get("statistics", {})
    return {
        "id": it["id"],
        "title": sn.get("title"),
        "description": sn.get("description"),
        "custom_url": sn.get("customUrl"),
        "country": sn.get("country"),
        "published_at": sn.get("publishedAt"),
        "subscribers": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
        "views": _int(st.get("viewCount")),
        "video_count": _int(st.get("videoCount")),
        "thumbnail": sn.get("thumbnails", {}).get("high", {}).get("url"),
    }


@app.get("/channels/{channel_id}/videos")
async def channel_videos(
    channel_id: str,
    max_results: int = Query(10, ge=1, le=50),
    page_token: Optional[str] = None,
):
    """Latest uploads via the channel's uploads playlist (1 unit, cheaper than search)."""
    ch = await yt_get("channels", {"part": "contentDetails", "id": channel_id})
    if not ch.get("items"):
        raise HTTPException(404, "Channel not found.")
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    params = {"part": "snippet", "playlistId": uploads, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token
    data = await yt_get("playlistItems", params)
    return {
        "next_page_token": data.get("nextPageToken"),
        "videos": [
            {
                "id": i["snippet"]["resourceId"]["videoId"],
                "title": i["snippet"]["title"],
                "published_at": i["snippet"]["publishedAt"],
                "thumbnail": i["snippet"].get("thumbnails", {}).get("high", {}).get("url"),
            }
            for i in data.get("items", [])
        ],
    }


@app.get("/videos/{video_id}/comments")
async def video_comments(
    video_id: str,
    max_results: int = Query(20, ge=1, le=100),
    order: str = Query("relevance", pattern="^(relevance|time)$"),
    page_token: Optional[str] = None,
):
    params = {"part": "snippet", "videoId": video_id, "maxResults": max_results,
              "order": order, "textFormat": "plainText"}
    if page_token:
        params["pageToken"] = page_token
    data = await yt_get("commentThreads", params)
    return {
        "next_page_token": data.get("nextPageToken"),
        "comments": [
            {
                "id": t["id"],
                "author": t["snippet"]["topLevelComment"]["snippet"].get("authorDisplayName"),
                "text": t["snippet"]["topLevelComment"]["snippet"].get("textDisplay"),
                "likes": t["snippet"]["topLevelComment"]["snippet"].get("likeCount"),
                "published_at": t["snippet"]["topLevelComment"]["snippet"].get("publishedAt"),
                "reply_count": t["snippet"].get("totalReplyCount"),
            }
            for t in data.get("items", [])
        ],
    }
