import requests
import json
import os
import time

# =========================
# ENVIRONMENT VARIABLES
# (Loaded from GitHub Secrets)
# =========================

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
ULTRA_INSTANCE_ID = os.getenv("ULTRA_INSTANCE_ID")
ULTRA_TOKEN = os.getenv("ULTRA_TOKEN")
WHATSAPP_TO = os.getenv("WHATSAPP_TO")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

HISTORY_FILE = "movie_history.json"
TMDB_BASE_URL = "https://api.themoviedb.org/3"


# =========================
# HISTORY (NO REPEAT MOVIES)
# =========================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"tmdb_ids": []}
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except:
        return {"tmdb_ids": []}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)


# =========================
# TMDB HELPERS
# =========================

def tmdb_get(path, params=None):
    if params is None:
        params = {}
    params["api_key"] = TMDB_API_KEY

    r = requests.get(f"{TMDB_BASE_URL}{path}", params=params)
    r.raise_for_status()
    return r.json()


def discover_movies(page=1):
    """Top-rated movies, sorted by rating."""
    params = {
        "sort_by": "vote_average.desc",
        "vote_count.gte": 1000,
        "primary_release_date.gte": "1990-01-01",
        "include_adult": "false",
        "page": page
    }
    return tmdb_get("/discover/movie", params)


def get_movie_details(movie_id):
    params = {
        "append_to_response": "credits,videos,external_ids"
    }
    return tmdb_get(f"/movie/{movie_id}", params)


# =========================
# AI NUDITY SUMMARY (Qwen 2.5)
# =========================

def generate_nudity_info(title, genres, overview):
    prompt = f"""
    Your task is to summarize any nudity, sensuality, or intimate content in a movie.

    Movie Title: {title}
    Genres: {genres}
    Plot Summary: {overview}

    Rules:
    - If the movie contains nudity, estimate the number of scenes (approximate).
    - If there is no nudity, clearly say: "This movie contains no nudity."
    - Keep it CLEAN, SHORT, and NON-EXPLICIT.
    """

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com",
        "X-Title": "Daily Movie Agent"
    }

    data = {
        "model": "qwen/qwen-2.5",
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=data, headers=headers)
        return r.json()["choices"][0]["message"]["content"]
    except:
        return "Nudity info unavailable."


# =========================
# WHATSAPP SENDER
# =========================

def send_whatsapp_message(text):
    url = f"https://api.ultramsg.com/{ULTRA_INSTANCE_ID}/messages/chat"

    payload = {
        "token": ULTRA_TOKEN,
        "to": WHATSAPP_TO,
        "body": text
    }

    r = requests.post(url, data=payload)
    print("WhatsApp Response:", r.text)


# =========================
# MOVIE SELECTION
# =========================

def pick_movies():
    history = load_history()
    used_ids = set(history["tmdb_ids"])

    movies_collected = []

    for page in range(1, 4):  # scan first 3 pages of top movies
        print(f"Scanning TMDB page {page}...")
        data = discover_movies(page)
        results = data.get("results", [])

        for m in results:
            mid = m["id"]
            if mid not in used_ids:
                movies_collected.append(m)
            if len(movies_collected) == 3:
                break

        if len(movies_collected) == 3:
            break

        time.sleep(0.3)

    # Update history
    for m in movies_collected:
        history["tmdb_ids"].append(m["id"])
    save_history(history)

    return movies_collected


# =========================
# MESSAGE BUILDER
# =========================

def build_movie_message(movie):
    details = get_movie_details(movie["id"])

    title = details.get("title", "Unknown")
    year = details.get("release_date", "")[:4]
    rating = details.get("vote_average")
    runtime = details.get("runtime", "N/A")
    genres = ", ".join([g["name"] for g in details.get("genres", [])])
    overview = details.get("overview", "No overview available.")

    # Director
    director = "Unknown"
    for c in details.get("credits", {}).get("crew", []):
        if c.get("job") == "Director":
            director = c.get("name")
            break

    # Cast
    cast = ", ".join([c["name"] for c in details.get("credits", {}).get("cast", [])[:4]])

    # Languages
    spoken = details.get("spoken_languages", [])
    languages = ", ".join([l.get("english_name", "N/A") for l in spoken])

    # Trailer
    trailer_url = "Unavailable"
    videos = details.get("videos", {}).get("results", [])
    for v in videos:
        if v.get("type") == "Trailer" and v.get("site") == "YouTube":
            trailer_url = f"https://www.youtube.com/watch?v={v.get('key')}"
            break

    # Nudity summary (AI)
    nudity_info = generate_nudity_info(title, genres, overview)

    return f"""
🎬 *{title}* ({year})
⭐ Rating: {rating}
⏱ Runtime: {runtime} min
🎭 Genres: {genres}
🎬 Director: {director}
🎤 Cast: {cast}
🌐 Languages: {languages}
📺 Trailer: {trailer_url}

🧩 *Nudity & Intimacy Info:*  
{nudity_info}

----------------------------------------
"""


# =========================
# MAIN SCRIPT
# =========================

def main():
    movies = pick_movies()

    message = "🎥 *Your 3 Movies for Today*\n\n----------------------------------------\n"
    for m in movies:
        message += build_movie_message(m)

    send_whatsapp_message(message)


if __name__ == "__main__":
    main()

