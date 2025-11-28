import os
import json
import requests
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
ULTRA_INSTANCE_ID = os.getenv("ULTRA_INSTANCE_ID")
ULTRA_TOKEN = os.getenv("ULTRA_TOKEN")
WHATSAPP_TO = os.getenv("WHATSAPP_TO")

HISTORY_FILE = "movie_history.json"

# Never go below this rating (TMDB rating)
MIN_RATING = 5.0

# How long before a movie can be repeated
NO_REPEAT_DAYS = 180

# For age-rating lookup (certifications)
CERT_REGIONS_PRIORITY = ["IN", "US", "PK"]

# For OTT platforms (watch/providers)
WATCH_REGION_PRIORITY = ["PK", "IN", "US"]
MAJOR_PROVIDERS = {
    "Netflix",
    "Amazon Prime Video",
    "Disney Plus",
    "Hotstar",
    "JioCinema",
    "ZEE5",
    "Zee5",
    "Sony Liv",
    "SonyLIV",
    "Hulu",
    "HBO Max",
    "Apple TV",
    "Apple TV+",
    "Google Play Movies",
    "YouTube",
    "MX Player",
}


def load_history():
    """Load movie history. Support old [id, id] and new [{'id':..,'date':..}] formats."""
    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    normalized = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item and "date" in item:
                normalized.append(item)
            elif isinstance(item, int):
                # Old format: only ID; treat as very old
                normalized.append({"id": item, "date": "1970-01-01"})
    return normalized


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save history: {e}")


def get_today_pk():
    tz = ZoneInfo("Asia/Karachi")
    return datetime.now(tz)


def was_recently_sent(movie_id, history, cutoff_date):
    for entry in history:
        if entry.get("id") == movie_id:
            try:
                d = date.fromisoformat(entry.get("date", "1970-01-01"))
            except ValueError:
                d = date(1970, 1, 1)
            if d >= cutoff_date:
                return True
    return False


def get_theme_for_today():
    """
    Monday: Mix
    Tuesday: Mix
    Wednesday: Mix
    Thursday: Mix
    Friday: Horror / Thriller
    Saturday: Mystery / War / Bollywood
    Sunday: Comedy / Feel Good
    """
    today_pk = get_today_pk()
    weekday = today_pk.weekday()  # Monday = 0

    if weekday in (0, 1, 2, 3):
        return "mix", "Mix Theme"
    elif weekday == 4:
        return "horror_thriller", "Horror / Thriller"
    elif weekday == 5:
        return "mystery_war_bollywood", "Mystery / War / Bollywood"
    else:
        return "comedy_feelgood", "Comedy / Feel Good"


def discover_movies(params, max_pages=3):
    """Fetch movies from TMDB discover with up to max_pages pages."""
    all_results = []
    base_url = "https://api.themoviedb.org/3/discover/movie"

    for page in range(1, max_pages + 1):
        query = params.copy()
        query["page"] = page
        query["api_key"] = TMDB_API_KEY

        resp = requests.get(base_url, params=query)
        if resp.status_code != 200:
            print(f"TMDB discover error: {resp.status_code} {resp.text}")
            break

        data = resp.json()
        results = data.get("results", [])
        if not results:
            break

        all_results.extend(results)
    return all_results


def get_movies_for_theme(theme):
    """
    Get candidate movies from TMDB based on the day's theme.
    Always enforce:
      - rating >= MIN_RATING
      - vote_count >= 500
      - release date >= 1990-01-01
    """
    base_params = {
        "sort_by": "vote_average.desc",
        "vote_average.gte": MIN_RATING,
        "vote_count.gte": 500,
        "include_adult": "false",
        "include_video": "false",
        "language": "en-US",
        "primary_release_date.gte": "1990-01-01",
    }

    if theme == "mix":
        return discover_movies(base_params)

    if theme == "horror_thriller":
        params = base_params.copy()
        params["with_genres"] = "27,53"  # Horror, Thriller
        return discover_movies(params)

    if theme == "mystery_war_bollywood":
        # Part A: Mystery + War
        params_a = base_params.copy()
        params_a["with_genres"] = "9648,10752"  # Mystery, War
        results_a = discover_movies(params_a)

        # Part B: Bollywood-ish (Hindi original language)
        params_b = base_params.copy()
        params_b["with_original_language"] = "hi"
        results_b = discover_movies(params_b)

        merged = {}
        for m in results_a + results_b:
            merged[m["id"]] = m
        return list(merged.values())

    if theme == "comedy_feelgood":
        params = base_params.copy()
        # Comedy, Family, Romance
        params["with_genres"] = "35,10751,10749"
        return discover_movies(params)

    # Fallback
    return discover_movies(base_params)


def get_movie_details(movie_id):
    """
    Fetch detailed movie info including credits, release dates and videos (for trailer).
    """
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "append_to_response": "credits,release_dates,videos",
    }
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"TMDB movie details error for {movie_id}: {resp.status_code} {resp.text}")
        return None
    return resp.json()


def map_certification_to_age_bucket(cert):
    """
    Map a certification (PG-13, A, 18, etc.) to a simple 13+/16+/18+ bucket.
    """
    if not cert:
        return "Not rated"

    c = cert.upper().strip()

    # General / all ages
    if c in ["G", "PG", "U"] or "ALL" in c:
        return "All ages"

    # 13+ style
    if "PG-13" in c or "U/A" in c or "12" in c or c == "13":
        return "13+"

    # 16+ style
    if c.startswith("16") or c == "R" or c == "A":
        return "16+"

    # 18+ style
    if c.startswith("18") or c == "NC-17":
        return "18+"

    return "Not rated"


def get_age_rating_from_release_dates(release_dates):
    """
    Look at release_dates['results'] and try to pick a certification for IN, then US, then PK.
    """
    if not release_dates:
        return "Not rated"

    results = release_dates.get("results", [])
    chosen_cert = None

    for region in CERT_REGIONS_PRIORITY:
        for entry in results:
            if entry.get("iso_3166_1") == region:
                rels = entry.get("release_dates", [])
                for r in rels:
                    cert = r.get("certification")
                    if cert:
                        chosen_cert = cert
                        break
            if chosen_cert:
                break
        if chosen_cert:
            break

    return map_certification_to_age_bucket(chosen_cert)


def get_trailer_url(movie_details):
    """
    Get a YouTube trailer link from the movie's videos.
    Prefer official Trailer/Teaser, fall back to any YouTube video.
    """
    videos = (movie_details.get("videos") or {}).get("results", [])

    # Prefer official trailers/teasers
    for v in videos:
        if (
            v.get("site") == "YouTube"
            and v.get("type") in {"Trailer", "Teaser"}
            and not v.get("official") is False
        ):
            key = v.get("key")
            if key:
                return f"https://www.youtube.com/watch?v={key}"

    # Fallback: any YouTube video
    for v in videos:
        if v.get("site") == "YouTube":
            key = v.get("key")
            if key:
                return f"https://www.youtube.com/watch?v={key}"

    return None


def get_streaming_providers(movie_id):
    """
    Use TMDB watch/providers to get OTT platforms.
    Try PK, then IN, then US.
    Prefer flatrate, then rent, then buy.
    Return a de-duplicated list of provider names, preferring major ones.
    """
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/watch/providers"
    params = {"api_key": TMDB_API_KEY}

    try:
        resp = requests.get(url, params=params)
    except Exception as e:
        print(f"Error fetching watch providers for {movie_id}: {e}")
        return []

    if resp.status_code != 200:
        print(f"Watch/providers error for {movie_id}: {resp.status_code} {resp.text}")
        return []

    data = resp.json()
    results = data.get("results", {})

    region_data = None
    for region in WATCH_REGION_PRIORITY:
        if region in results:
            region_data = results[region]
            break

    if not region_data:
        return []

    providers = []
    # Prefer flatrate, then rent, then buy
    for key in ["flatrate", "rent", "buy"]:
        for p in region_data.get(key, []) or []:
            name = p.get("provider_name")
            if name:
                providers.append(name)

    if not providers:
        return []

    # Prefer major providers if we find any
    majors = [p for p in providers if p in MAJOR_PROVIDERS]
    ordered = majors or providers

    unique = []
    for p in ordered:
        if p not in unique:
            unique.append(p)

    return unique


def truncate(text, max_len=380):
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def build_whatsapp_message(movies, theme_label):
    """
    Build a clean, consistent WhatsApp message with:
    - Title, year
    - Rating, age, runtime
    - Genres, languages, director, cast
    - Streaming providers
    - Trailer link
    - Short summary
    """
    today_pk = get_today_pk()
    date_str = today_pk.strftime("%A, %d %B %Y")

    lines = []
    lines.append("🎬 *Daily Movie Picks*")
    lines.append(f"📅 {date_str}")
    lines.append(f"🎭 Theme: {theme_label}")
    lines.append("────────────────────")
    lines.append("Here are 3 picks for tonight (rating ≥ 5.0):")
    lines.append("")

    for idx, m in enumerate(movies, start=1):
        title = m.get("title") or m.get("name") or "Unknown title"
        release_date = m.get("release_date") or ""
        year = release_date[:4] if release_date else "N/A"
        rating = m.get("vote_average", 0)
        runtime = m.get("runtime") or 0
        overview = truncate(m.get("overview") or "")
        genres = [g["name"] for g in m.get("genres", [])]
        genre_str = ", ".join(genres) if genres else "N/A"

        director = "N/A"
        credits = m.get("credits", {})
        for person in credits.get("crew", []):
            if person.get("job") == "Director":
                director = person.get("name")
                break

        cast_list = credits.get("cast", [])[:3]
        cast_names = [c["name"] for c in cast_list]
        cast_str = ", ".join(cast_names) if cast_names else "N/A"

        langs = m.get("spoken_languages", [])
        lang_names = [l["english_name"] for l in langs if l.get("english_name")]
        langs_str = ", ".join(lang_names) if lang_names else "N/A"

        age_rating = get_age_rating_from_release_dates(m.get("release_dates"))
        trailer_url = get_trailer_url(m)
        providers = get_streaming_providers(m.get("id"))
        if providers:
            streaming_str = ", ".join(providers)
        else:
            streaming_str = "Not available on major platforms (for your region)"

        # Title line
        lines.append(f"{idx}) 🎥 *{title}* ({year})")

        # Rating / age / runtime line
        info_line = f"   ⭐ {rating:.1f} | 🔞 {age_rating}"
        if runtime:
            hours = runtime // 60
            mins = runtime % 60
            if hours > 0:
                info_line += f" | ⏱ {hours}h {mins}m"
            else:
                info_line += f" | ⏱ {mins}m"
        lines.append(info_line)

        # Meta lines
        lines.append(f"   🎭 Genres: {genre_str}")
        lines.append(f"   🌐 Languages: {langs_str}")
        lines.append(f"   🎬 Director: {director}")
        lines.append(f"   ⭐ Cast: {cast_str}")
        lines.append(f"   📺 Streaming: {streaming_str}")
        if trailer_url:
            lines.append(f"   ▶️ Trailer: {trailer_url}")
        else:
            lines.append("   ▶️ Trailer: Not available")
        if overview:
            lines.append(f"   📝 Summary: {overview}")
        lines.append("")  # blank line between movies

    lines.append("Enjoy your movies! 🍿")
    return "\n".join(lines)


def send_whatsapp_message(text):
    if not (ULTRA_INSTANCE_ID and ULTRA_TOKEN and WHATSAPP_TO):
        print("Missing UltraMsg configuration, skipping WhatsApp send.")
        print(text)
        return

    url = f"https://api.ultramsg.com/{ULTRA_INSTANCE_ID}/messages/chat"
    payload = {
        "token": ULTRA_TOKEN,
        "to": WHATSAPP_TO,
        "body": text,
    }

    try:
        resp = requests.post(url, data=payload)
        print(f"UltraMsg response: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Failed to send WhatsApp message: {e}")


def main():
    if not TMDB_API_KEY:
        print("TMDB_API_KEY is not set. Exiting.")
        return

    history = load_history()
    today = date.today()
    cutoff = today - timedelta(days=NO_REPEAT_DAYS)

    theme, theme_label = get_theme_for_today()
    print(f"Today's theme: {theme} ({theme_label})")

    candidates = get_movies_for_theme(theme)
    print(f"Fetched {len(candidates)} candidate movies from TMDB for theme {theme}")

    # Sort by rating then vote count
    candidates.sort(
        key=lambda m: (m.get("vote_average", 0), m.get("vote_count", 0)),
        reverse=True,
    )

    chosen = []
    used_ids = set()

    for basic in candidates:
        if len(chosen) >= 3:
            break

        movie_id = basic.get("id")
        if not movie_id or movie_id in used_ids:
            continue

        # Skip if recently recommended
        if was_recently_sent(movie_id, history, cutoff):
            continue

        # Guard rating (though discover already filtered)
        rating = basic.get("vote_average", 0)
        if rating < MIN_RATING:
            continue

        details = get_movie_details(movie_id)
        if not details:
            continue

        chosen.append(details)
        used_ids.add(movie_id)

    if not chosen:
        print("No suitable movies found today.")
        return

    # Update history with today's picks
    for d in chosen:
        history.append({"id": d.get("id"), "date": today.isoformat()})
    save_history(history)

    # Build and send WhatsApp message
    msg = build_whatsapp_message(chosen, theme_label)
    print("Final WhatsApp message:\n", msg)
    send_whatsapp_message(msg)


if __name__ == "__main__":
    main()
