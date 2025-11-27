import os
import json
import time
from typing import List, Dict, Any, Optional, Set

import requests
from bs4 import BeautifulSoup

# ========= CONFIG FROM ENV (GitHub Secrets) =========

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
ULTRA_INSTANCE_ID = os.getenv("ULTRA_INSTANCE_ID")
ULTRA_TOKEN = os.getenv("ULTRA_TOKEN")
WHATSAPP_TO = os.getenv("WHATSAPP_TO")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

HISTORY_FILE = "movie_history.json"

TMDB_BASE_URL = "https://api.themoviedb.org/3"
ULTRAMSG_CHAT_URL = "https://api.ultramsg.com/{instance}/messages/chat"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Platforms you accept
MAJOR_PLATFORMS = {
    "Netflix",
    "Amazon Prime Video",
    "Prime Video",
    "Disney Plus",
    "Disney+",
    "Disney+ Hotstar",
    "Hotstar",
    "Apple TV",
    "Apple iTunes",
    "YouTube",
    "YouTube Movies",
    "ZEE5",
    "Zee5",
    "SonyLIV",
    "Sony LIV"
}


class ConfigError(Exception):
    pass


def check_config() -> None:
    missing = []
    if not TMDB_API_KEY:
        missing.append("TMDB_API_KEY")
    if not ULTRA_INSTANCE_ID:
        missing.append("ULTRA_INSTANCE_ID")
    if not ULTRA_TOKEN:
        missing.append("ULTRA_TOKEN")
    if not WHATSAPP_TO:
        missing.append("WHATSAPP_TO")
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")


# ========= HISTORY MANAGEMENT =========

def load_history() -> Set[int]:
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("tmdb_ids", []))
    except Exception:
        return set()


def save_history(history_ids: Set[int]) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"tmdb_ids": sorted(list(history_ids))}, f, indent=2)


# ========= TMDB HELPERS =========

def tmdb_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if params is None:
        params = {}
    params["api_key"] = TMDB_API_KEY
    resp = requests.get(f"{TMDB_BASE_URL}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def discover_movies(page: int = 1) -> Dict[str, Any]:
    params = {
        "sort_by": "vote_average.desc",
        "vote_count.gte": 500,
        "primary_release_date.gte": "1990-01-01",
        "include_adult": "false",
        "page": page,
    }
    return tmdb_get("/discover/movie", params=params)


def get_movie_details(tmdb_id: int) -> Dict[str, Any]:
    return tmdb_get(f"/movie/{tmdb_id}", {
        "append_to_response": "credits,external_ids,videos"
    })


def get_watch_providers(tmdb_id: int) -> Dict[str, Any]:
    return tmdb_get(f"/movie/{tmdb_id}/watch/providers")


def has_required_language(details: Dict[str, Any]) -> bool:
    langs = {l.get("iso_639_1") for l in details.get("spoken_languages", [])}
    return "en" in langs or "hi" in langs


def extract_major_platforms(providers: Dict[str, Any]) -> List[str]:
    results = providers.get("results", {})
    platforms = set()

    for region in ("IN", "US", "GB"):
        region_data = results.get(region, {})
        for key in ("flatrate", "rent", "buy", "ads"):
            for item in region_data.get(key, []) or []:
                name = item.get("provider_name")
                if name and name in MAJOR_PLATFORMS:
                    platforms.add(name)

    return sorted(platforms)


def extract_director(details):
    for member in details.get("credits", {}).get("crew", []):
        if member.get("job") == "Director":
            return member.get("name")
    return None


def extract_main_cast(details, limit=4):
    return [c.get("name") for c in details.get("credits", {}).get("cast", [])[:limit]]


def extract_trailer(details):
    videos = details.get("videos", {}).get("results", [])
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            return f"https://www.youtube.com/watch?v={v.get('key')}"
    return None


# ========= NUDITY ANALYSIS (HYBRID) =========

def fetch_parents_guide(imdb_id: str) -> Optional[str]:
    if not imdb_id:
        return None

    url = f"https://www.imdb.com/title/{imdb_id}/parentalguide"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.get_text(separator="\n")[:8000]
    except:
        return None


def analyze_nudity(text: str) -> Optional[str]:
    if not OPENAI_API_KEY or not text:
        return None

    prompt = (
        "Your job is to read the IMDb Parents Guide text and extract only nudity/sexual content. "
        "Estimate the number of nude or sexual scenes. Provide a JSON response like: "
        '{ "approx_nude_scenes": <number>, "summary": "<short summary>" }.'
    )

    try:
        r = requests.post(
            OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4.1-mini",
                "messages": [
                    {"role": "system", "content": "You classify nudity/sexual content safely."},
                    {"role": "user", "content": prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1
            },
            timeout=30
        )
        data = r.json()
        content = data["choices"][0]["message"]["content"]

        import json as _json
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:].strip()

        parsed = _json.loads(content)
        n = parsed.get("approx_nude_scenes")
        summary = parsed.get("summary")
        if isinstance(n, (int, float)) and summary:
            return f"Approx. {int(n)} nude/sexual scenes. {summary}"
        return None
    except:
        return None


def get_nudity_info(imdb_id: str) -> str:
    text = fetch_parents_guide(imdb_id)
    if not text:
        return "Nudity info unavailable."

    ai = analyze_nudity(text)
    return ai or "Nudity info unavailable."


# ========= MOVIE SELECTION =========

def pick_movies(history: Set[int], count=3) -> List[Dict[str, Any]]:
    movies = []
    max_pages = 5

    for page in range(1, max_pages + 1):
        print("Scanning page:", page)
        try:
            data = discover_movies(page)
        except:
            continue

        for item in data.get("results", []):
            tmdb_id = item.get("id")
            if not tmdb_id or tmdb_id in history:
                continue

            rating = float(item.get("vote_average") or 0)
            if rating < 7:
                continue

            try:
                details = get_movie_details(tmdb_id)
                providers = get_watch_providers(tmdb_id)
            except:
                continue

            if not has_required_language(details):
                continue

            platforms = extract_major_platforms(providers)
            if not platforms:
                continue

            imdb_id = details.get("external_ids", {}).get("imdb_id")

            movies.append({
                "tmdb_id": tmdb_id,
                "title": details.get("title"),
                "year": (details.get("release_date", "") or "")[:4],
                "overview": details.get("overview"),
                "runtime": details.get("runtime"),
                "genres": [g.get("name") for g in details.get("genres", [])],
                "director": extract_director(details),
                "cast": extract_main_cast(details),
                "languages": [l.get("english_name") for l in details.get("spoken_languages", [])],
                "rating": rating,
                "platforms": platforms,
                "imdb_id": imdb_id,
                "trailer": extract_trailer(details),
            })

        time.sleep(0.4)

        if len(movies) >= 25:
            break

    movies.sort(key=lambda m: m["rating"], reverse=True)

    final = []
    def pick_range(low, high):
        nonlocal final
        if len(final) >= count:
            return
        for m in movies:
            if m in final:
                continue
            if low <= m["rating"] <= high:
                final.append(m)
                if len(final) == count:
                    break

    pick_range(9, 10)
    pick_range(8, 8.9)
    pick_range(7, 7.9)

    if len(final) < count:
        for m in movies:
            if m not in final:
                final.append(m)
                if len(final) == count:
                    break

    return final[:count]


# ========= WHATSAPP MESSAGE =========

def format_movie(m):
    lines = [
        f"🎬 {m['title']} ({m['year']})",
        f"⭐ Rating: {m['rating']}",
        f"⏱ Runtime: {m['runtime']} min",
        f"🎭 Genres: {', '.join(m['genres'])}",
        f"🎬 Director: {m['director']}",
        f"👥 Cast: {', '.join(m['cast'])}",
        f"🌐 Languages: {', '.join(m['languages'])}",
        f"📺 Platforms: {', '.join(m['platforms'])}",
        f"📹 Trailer: {m['trailer'] or 'N/A'}",
        "",
        m["overview"] or "",
        "",
        f"🔞 {m['nudity_info']}",
    ]
    return "\n".join(lines)


def send_whatsapp(text):
    url = ULTRAMSG_CHAT_URL.format(instance=ULTRA_INSTANCE_ID)
    r = requests.post(url, data={
        "token": ULTRA_TOKEN,
        "to": WHATSAPP_TO,
        "body": text,
    }, timeout=15)
    print("WhatsApp Response:", r.text)


# ========= MAIN =========

def main():
    try:
        check_config()
    except Exception as e:
        print("CONFIG ERROR:", e)
        return

    history = load_history()
    movies = pick_movies(history)

    if not movies:
        send_whatsapp("No movies found today.")
        return

    for m in movies:
        if m.get("imdb_id"):
            m["nudity_info"] = get_nudity_info(m["imdb_id"])
        else:
            m["nudity_info"] = "Nudity info unavailable."

    text = "Here are your 3 movies for today:\n\n" + "\n\n" + ("-"*35) + "\n\n"
    text += ("\n" + ("-"*35) + "\n\n").join(format_movie(m) for m in movies)

    send_whatsapp(text)

    for m in movies:
        history.add(m["tmdb_id"])

    save_history(history)


if __name__ == "__main__":
    main()
