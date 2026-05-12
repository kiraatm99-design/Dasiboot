import os
import re
import math
import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
BASE_URL = "https://v3.football.api-sports.io"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not API_KEY:
    raise RuntimeError("API_KEY is missing")

HEADERS = {
    "x-apisports-key": API_KEY
}

TIMEOUT = aiohttp.ClientTimeout(total=15)

# =========================
# TEAM TRANSLATIONS
# =========================

TEAM_TRANSLATIONS = {
    "باريس سان جيرمان": "PSG",
    "أتلتيكو مدريد": "Atletico Madrid",
    "اتلتيكو مدريد": "Atletico Madrid",
    "ريال مدريد": "Real Madrid",
    "برشلونة": "Barcelona",
    "ليفربول": "Liverpool",
    "مانشستر سيتي": "Manchester City",
    "مانشستر يونايتد": "Manchester United",
    "تشيلسي": "Chelsea",
    "ارسنال": "Arsenal",
    "آرسنال": "Arsenal",
    "باريس": "PSG",
    "ليفانتي": "Levante",
    "سيلتا فيغو": "Celta Vigo",
    "سيلتا": "Celta Vigo",
    "اوساسونا": "Osasuna",
    "أوساسونا": "Osasuna",
    "اتلتيك بلباو": "Athletic Club",
    "أتلتيك بلباو": "Athletic Club",
}

# =========================
# HELPERS
# =========================

def normalize_text(text: str) -> str:
    text = text.strip().lower()

    for ar, en in sorted(TEAM_TRANSLATIONS.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(ar.lower(), en.lower())

    text = re.sub(r"\s*(?:vs|v|ضد)\s*", " vs ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def api_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{endpoint}"
    async with session.get(url, headers=HEADERS, params=params, timeout=TIMEOUT) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def get_team(session: aiohttp.ClientSession, name: str):
    try:
        data = await api_get_json(session, "/teams", {"search": name})
        items = data.get("response", [])

        if not items:
            return None

        name_lower = name.strip().lower()

        for item in items:
            team = item.get("team", {})
            if team.get("name", "").strip().lower() == name_lower:
                return {"id": team["id"], "name": team["name"]}

        team = items[0].get("team", {})
        return {"id": team["id"], "name": team["name"]}

    except Exception as e:
        print("TEAM ERROR:", e)
        return None


async def get_last_matches(session: aiohttp.ClientSession, team_id: int, last: int = 8):
    try:
        data = await api_get_json(session, "/fixtures", {"team": team_id, "last": last})
        return data.get("response", [])
    except Exception as e:
        print("MATCH ERROR:", e)
        return []


def analyze_matches(matches: list, team_id: int) -> dict:
    wins = draws = losses = 0
    goals_scored = 0
    goals_conceded = 0
    clean_sheets = 0
    home_points = 0
    away_points = 0
    home_games = 0
    away_games = 0

    for match in matches:
        home = match.get("teams", {}).get("home", {})
        away = match.get("teams", {}).get("away", {})
        gh = match.get("goals", {}).get("home")
        ga = match.get("goals", {}).get("away")

        if gh is None or ga is None:
            continue

        if home.get("id") == team_id:
            home_games += 1
            goals_scored += gh
            goals_conceded += ga

            if ga == 0:
                clean_sheets += 1

            if gh > ga:
                wins += 1
                home_points += 3
            elif gh == ga:
                draws += 1
                home_points += 1
            else:
                losses += 1

        elif away.get("id") == team_id:
            away_games += 1
            goals_scored += ga
            goals_conceded += gh

            if gh == 0:
                clean_sheets += 1

            if ga > gh:
                wins += 1
                away_points += 3
            elif ga == gh:
                draws += 1
                away_points += 1
            else:
                losses += 1

    matches_count = max(len(matches), 1)

    avg_scored = goals_scored / matches_count
    avg_conceded = goals_conceded / matches_count
    form_points = (wins * 3) + draws

    home_form = home_points / home_games if home_games else 0
    away_form = away_points / away_games if away_games else 0

    power = (
        form_points
        + (avg_scored * 2.0)
        - (avg_conceded * 1.2)
        + (clean_sheets * 0.8)
        + (home_form * 0.5)
        + (away_form * 0.5)
    )

    return {
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "scored": goals_scored,
        "conceded": goals_conceded,
        "avg_scored": round(avg_scored, 2),
        "avg_conceded": round(avg_conceded, 2),
        "power": round(power, 2),
        "clean_sheets": clean_sheets,
        "home_form": round(home_form, 2),
        "away_form": round(away_form, 2),
    }


def predict_match(team1: dict, team2: dict, a1: dict, a2: dict) -> dict:
    diff = a1["power"] - a2["power"]

    win_raw = 1 / (1 + math.exp(-diff / 3.5))
    draw_raw = max(0.12, 0.32 - (abs(diff) / 30))
    remaining = max(0.01, 1 - draw_raw)

    p1 = win_raw * remaining
    p2 = remaining - p1

    total = p1 + p2 + draw_raw
    p1 /= total
    p2 /= total
    draw_p = draw_raw / total

    outcome_prob_map = {
        team1["name"]: p1,
        team2["name"]: p2,
        "DRAW": draw_p,
    }
    winner = max(outcome_prob_map, key=outcome_prob_map.get)
    winner_prob = outcome_prob_map[winner]

    odds = round(1 / max(winner_prob, 0.01), 2)

    expected_goals = round(
        (
            a1["avg_scored"]
            + a2["avg_scored"]
            + a1["avg_conceded"]
            + a2["avg_conceded"]
        ) / 2,
        1,
    )

    goals_market = "Over 2.5 Goals" if expected_goals >= 2.7 else "Under 2.5 Goals"

    if winner == "DRAW":
        exact_score = "1-1"
    elif winner == team1["name"]:
        exact_score = "2-1" if expected_goals >= 2.7 else "1-0"
    else:
        exact_score = "1-2" if expected_goals >= 2.7 else "0-1"

    return {
        "winner": winner,
        "odds": odds,
        "expected_goals": expected_goals,
        "goals_market": goals_market,
        "exact_score": exact_score,
        "team1_prob": round(p1 * 100, 1),
        "team2_prob": round(p2 * 100, 1),
        "draw_prob": round(draw_p * 100, 1),
    }


async def get_session(context: ContextTypes.DEFAULT_TYPE) -> aiohttp.ClientSession:
    session = context.application.bot_data.get("session")
    if session is None or session.closed:
        session = aiohttp.ClientSession()
        context.application.bot_data["session"] = session
    return session

# =========================
# COMMANDS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحبا في DASI-BOT ارسل المباراة التي تريد تحليلها\n\n"
        "مثال:\n"
        "Real Madrid vs Barcelona\n"
        "أو\n"
        "ريال مدريد ضد برشلونة"
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_text = update.message.text or ""
        text = normalize_text(raw_text)

        if "vs" not in text:
            await update.message.reply_text(
                "❌ اكتب المباراة بهذه الصيغة:\n"
                "Team vs Team"
            )
            return

        parts = [p.strip() for p in re.split(r"\s+vs\s+", text, maxsplit=1) if p.strip()]
        if len(parts) != 2:
            await update.message.reply_text("❌ صيغة المباراة غير صحيحة.")
            return

        t1, t2 = parts
        if not t1 or not t2:
            await update.message.reply_text("❌ أدخل اسمَي الفريقين بشكل واضح.")
            return

        if t1 == t2:
            await update.message.reply_text("❌ الفريقان متشابهان، تأكد من الاسم.")
            return

        session = await get_session(context)

        team1 = await get_team(session, t1)
        team2 = await get_team(session, t2)

        if not team1:
            await update.message.reply_text(f"❌ لم أجد الفريق الأول:\n{t1}")
            return

        if not team2:
            await update.message.reply_text(f"❌ لم أجد الفريق الثاني:\n{t2}")
            return

        matches1 = await get_last_matches(session, team1["id"], last=8)
        matches2 = await get_last_matches(session, team2["id"], last=8)

        a1 = analyze_matches(matches1, team1["id"])
        a2 = analyze_matches(matches2, team2["id"])

        prediction = predict_match(team1, team2, a1, a2)

        winner_text = (
            f"فوز {prediction['winner']}"
            if prediction["winner"] != "DRAW"
            else "تعادل"
        )

        result = (
            f"🏟 {team1['name']} vs {team2['name']}\n\n"
            f"🏆 توقع المباراة: {winner_text}\n"
            f"🎯 النتيجة المحتملة: {prediction['exact_score']}\n"
            f"⚽ عدد الأهداف المحتملة: {prediction['expected_goals']}\n"
            f"📈 السوق: {prediction['goals_market']}\n"
            f"💰 الأودد المتوقع: {prediction['odds']}\n\n"
            f"📊 احتمالات التوقع:\n"
            f"- {team1['name']}: {prediction['team1_prob']}%\n"
            f"- {team2['name']}: {prediction['team2_prob']}%\n"
            f"- التعادل: {prediction['draw_prob']}%\n\n"
            f"📌 قوة {team1['name']}: {a1['power']}\n"
            f"📌 قوة {team2['name']}: {a2['power']}"
        )

        await update.message.reply_text(result)

    except Exception as e:
        print("ERROR:", e)
        await update.message.reply_text("❌ حدث خطأ أثناء التحليل.")

# =========================
# RUN
# =========================

async def post_init(app: Application):
    app.bot_data["session"] = aiohttp.ClientSession(timeout=TIMEOUT)

async def post_shutdown(app: Application):
    session = app.bot_data.get("session")
    if session and not session.closed:
        await session.close()

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("✅ DASI-BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()