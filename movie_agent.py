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

# Minimum TMDB rating (approx "IMDb-style", but from TMDB)
MIN_RATING = 5.0

# How many days before a movie can be repeated
NO_REPEAT_DAYS = 180

# Region to use for certifications (we'll prefer IN, then US)
CERT_REGIONS_PRIORITY = ["IN", "US"]


def load_history():
    """Load movie history. Supports old format [id, id] and new [{'id':..,'date':..}]"""
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
                # Old format: just ID, treat as very old so it never blocks repeats
                normalized.append({"id": item, "date": "1970-01-01"})
    return normalized


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save history: {e}")


def get_today_pk():
    """Return today's date in Pakistan time."""
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
    weekday = today_pk.weekday()  # Monday = 0, Sunday = 6

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
    Return a list of TMDB movie dicts based on theme.
    We'll always filter by:
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
        # No extra filters: just good, reasonably popular movies
        return discover_movies(base_params)

    if theme == "horror_thriller":
        params = base_params.copy()
        # Horror (27), Thriller (53) – AND combination, but it's fine for our use
        params["with_genres"] = "27,53"
        return discover_movies(params)

    if theme == "mystery_war_bollywood":
        # Part 1: Mystery + War
        params_a = base_params.copy()
        params_a["with_genres"] = "9648,10752"  # Mystery + War
        results_a = discover_movies(params_a)

        # Part 2: Bollywood-ish (Hindi language)
        params_b = base_params.copy()
        params_b["with_original_language"] = "hi"
        results_b = discover_movies(params_b)

        # Merge and de-duplicate by id
        merged = {}
        for m in results_a + results_b:
            merged[m["id"]] = m
        return list(merged.values())

    if theme == "comedy_feelgood":
        params = base_params.copy()
        # Comedy (35), Family (10751), Romance (10749)
        params["with_genres"] = "35,10751,10749"
        return discover_movies(params)

    # Fallback to mix if unknown
    return discover_movies(base_params)


def get_movie_details(movie_id):
    """
    Fetch detailed movie info including credits and release_dates (for age rating).
    """
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "append_to_response": "credits,release_dates",
    }
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"TMDB movie details error for {movie_id}: {resp.status_code} {resp.text}")
        return None
    return resp.json()


def map_certification_to_age_bucket(cert):
    """
    Map TMDB certification to 13+ / 16+ / 18+ style buckets.
    """
    if not cert:
        return "Not rated"

    c = cert.upper().strip()

    # Very safe / general audiences
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
    Look into release_dates['results'] and try to get a certification
    for IN first, then US. Then map to our age bucket.
    """
    if not release_dates:
        return "Not rated"

    results = release_dates.get("results", [])
    chosen_cert = None

    # Try preferred regions in order
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


def build_whatsapp_message(movies, theme_label):
    """
    Build the WhatsApp message text for the selected movies.
    """
    today_pk = get_today_pk()
    date_str = today_pk.strftime("%A, %d %B %Y")

    lines = []
    lines.append(f"🎬 Daily Movie Picks – {date_str}")
    lines.append(f"Theme: {theme_label}")
    lines.append("You’ve got 3 movies today (rating never below 5.0):\n")

    for idx, m in enumerate(movies, start=1):
        title = m.get("title") or m.get("name") or "Unknown title"
        release_date = m.get("release_date") or ""
        year = release_date[:4] if release_date else "N/A"
        rating = m.get("vote_average", 0)
        runtime = m.get("runtime") or 0
        overview = (m.get("overview") or "").strip()
        genres = [g["name"] for g in m.get("genres", [])]
        genre_str = ", ".join(genres) if genres else "N/A"

        # Director
        director = "N/A"
        credits = m.get("credits", {})
        for person in credits.get("crew", []):
            if person.get("job") == "Director":
                director = person.get("name")
                break

        # Main cast (top 3)
        cast_list = credits.get("cast", [])[:3]
        cast_names = [c["name"] for c in cast_list]
        cast_str = ", ".join(cast_names) if cast_names else "N/A"

        # Spoken languages
        langs = m.get("spoken_languages", [])
        lang_names = [l["english_name"] for l in langs if l.get("english_name")]
        langs_str = ", ".join(lang_names) if lang_names else "N/A"

        # Age rating
        age_rating = get_age_rating_from_release_dates(m.get("release_dates"))

        # Trailer (if any, from videos? not requested now, so skipping for simplicity)

        lines.append(f"{idx}) 🎥 *{title}* ({year})")
        lines.append(f"   ⭐ Rating: {rating:.1f}")
        lines.append(f"   🔞 Age rating: {age_rating}")
        if runtime:
            hours = runtime // 60
            mins = runtime % 60
            if hours > 0:
                lines.append(f"   ⏱ Runtime: {hours}h {mins}m")
            else:
                lines.append(f"   ⏱ Runtime: {mins}m")
        else:
            lines.append("   ⏱ Runtime: N/A")
        lines.append(f"   🎭 Genres: {genre_str}")
        lines.append(f"   🎬 Director: {director}")
        lines.append(f"   ⭐ Cast: {cast_str}")
        lines.append(f"   🌐 Languages: {langs_str}")
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

    # Sort by rating & vote count to prioritize better-known movies
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

        # Skip if recently sent
        if was_recently_sent(movie_id, history, cutoff):
            continue

        # Rating guard (though discover already filters)
        rating = basic.get("vote_average", 0)
        if rating < MIN_RATING:
            continue

        # Fetch full details
        details = get_movie_details(movie_id)
        if not details:
            continue

        chosen.append(details)
        used_ids.add(movie_id)

    if not chosen:
        print("No suitable movies found today.")
        return

    # Update history
    for d in chosen:
        history.append({"id": d.get("id"), "date": today.isoformat()})
    save_history(history)

    # Build and send message
    msg = build_whatsapp_message(chosen, theme_label)
    print("Final WhatsApp message:\n", msg)
    send_whatsapp_message(msg)


if __name__ == "__main__":
    main()
