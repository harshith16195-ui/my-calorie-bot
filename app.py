#!/usr/bin/env python3
import os
import json
import sqlite3
import base64
import requests
from datetime import datetime, date
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

DB_PATH    = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "nutrition.db"))
START_DATE = date(2026, 4, 21)

USER = {
    "name":     "Harshith",
    "age":      31,
    "weight":   68,
    "height":   173,
    "goal":     "Visible abs and muscle definition",
    "calories": 2200,
    "protein":  170,
    "carbs":    220,
    "fat":      65,
}

app    = Flask(__name__)
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
ai     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS food_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date  TEXT NOT NULL,
            food_name TEXT NOT NULL,
            calories  REAL NOT NULL,
            protein   REAL NOT NULL,
            carbs     REAL NOT NULL,
            fat       REAL NOT NULL,
            logged_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def get_today():
    return date.today().isoformat()

def log_food(food_name, calories, protein, carbs, fat):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO food_log (log_date,food_name,calories,protein,carbs,fat,logged_at) VALUES (?,?,?,?,?,?,?)",
        (get_today(), food_name, calories, protein, carbs, fat, datetime.now().isoformat())
    )
    con.commit()
    con.close()

def get_today_totals():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT food_name,calories,protein,carbs,fat FROM food_log WHERE log_date=?",
        (get_today(),)
    ).fetchall()
    con.close()
    return rows

def get_daily_totals_last_n(n=7):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """SELECT log_date, SUM(calories), SUM(protein), SUM(carbs), SUM(fat)
           FROM food_log
           WHERE log_date >= date('now', ?)
           GROUP BY log_date ORDER BY log_date""",
        (f"-{n} days",)
    ).fetchall()
    con.close()
    return rows

def reset_today():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM food_log WHERE log_date=?", (get_today(),))
    con.commit()
    con.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def progress_bar(current, target, length=10):
    pct   = min(current / target, 1.0)
    filled = int(pct * length)
    bar   = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {int(pct*100)}%"

def totals_from_rows(rows):
    cal = sum(r[1] for r in rows)
    pro = sum(r[2] for r in rows)
    car = sum(r[3] for r in rows)
    fat = sum(r[4] for r in rows)
    return cal, pro, car, fat

def running_totals_msg(cal, pro, car, fat):
    t = USER
    return (
        f"\n📊 *Today so far:*\n"
        f"🔥 Calories: {cal:.0f}/{t['calories']} kcal {progress_bar(cal, t['calories'])}\n"
        f"💪 Protein:  {pro:.0f}/{t['protein']}g  {progress_bar(pro, t['protein'])}\n"
        f"🌾 Carbs:    {car:.0f}/{t['carbs']}g  {progress_bar(car, t['carbs'])}\n"
        f"🥑 Fat:      {fat:.0f}/{t['fat']}g  {progress_bar(fat, t['fat'])}"
    )

# ── Claude calls ──────────────────────────────────────────────────────────────
NUTRITION_SYSTEM = f"""You are a friendly personal nutrition coach for {USER['name']}, a {USER['age']}-year-old, {USER['weight']}kg, {USER['height']}cm male.
Goal: {USER['goal']}. Daily targets: {USER['calories']} kcal | {USER['protein']}g protein | {USER['carbs']}g carbs | {USER['fat']}g fat.
He trains with weights 4-6 days/week.

When asked to analyse food, ALWAYS respond with valid JSON only (no markdown fences), in this exact shape:
{{
  "food_name": "...",
  "portion": "...",
  "calories": 0,
  "protein": 0,
  "carbs": 0,
  "fat": 0,
  "verdict": "Great for your goal! / Decent choice / Watch the calories here",
  "tip": "One short practical tip"
}}
Use realistic average values. Be concise and encouraging."""

def analyse_text_food(text):
    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=NUTRITION_SYSTEM,
        messages=[{"role": "user", "content": f"Analyse this food and return JSON: {text}"}]
    )
    return json.loads(resp.content[0].text)

def analyse_image_food(image_url):
    img_resp = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
    img_b64  = base64.standard_b64encode(img_resp.content).decode("utf-8")
    media_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]

    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=NUTRITION_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text",  "text": "Analyse this food photo and return JSON."}
            ]
        }]
    )
    return json.loads(resp.content[0].text)

def claude_chat(prompt):
    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=f"You are a friendly, concise nutrition coach for {USER['name']}. Daily targets: {USER['calories']} kcal | {USER['protein']}g protein | {USER['carbs']}g carbs | {USER['fat']}g fat. Be practical, warm, and Indian-food friendly. Keep responses under 300 words.",
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

# ── Command handlers ──────────────────────────────────────────────────────────
def handle_summary():
    rows = get_today_totals()
    if not rows:
        return "📭 No food logged today yet! Send me a photo or type what you've eaten."

    cal, pro, car, fat = totals_from_rows(rows)
    t = USER
    deficit = t["calories"] - cal
    protein_hit = "✅ Hit!" if pro >= t["protein"] * 0.9 else "❌ Missed"

    food_list = "\n".join(f"  • {r[0]} ({r[1]:.0f} kcal)" for r in rows)

    if deficit >= 0:
        balance = f"🔽 Deficit: {deficit:.0f} kcal — fat loss mode!"
    else:
        balance = f"🔼 Surplus: {abs(deficit):.0f} kcal — adjust dinner"

    if cal <= t["calories"] * 1.05 and pro >= t["protein"] * 0.9:
        verdict = "✅ *On track* — great work today!"
    elif cal <= t["calories"] * 1.1 and pro >= t["protein"] * 0.75:
        verdict = "🟡 *Slightly off* — close enough, keep going"
    else:
        verdict = "🔴 *Needs attention* — adjust your next meal"

    return (
        f"📋 *Daily Summary — {get_today()}*\n\n"
        f"*Meals logged:*\n{food_list}\n\n"
        f"🔥 Calories: {cal:.0f}/{t['calories']} kcal\n"
        f"💪 Protein:  {pro:.0f}/{t['protein']}g — {protein_hit}\n"
        f"🌾 Carbs:    {car:.0f}/{t['carbs']}g\n"
        f"🥑 Fat:      {fat:.0f}/{t['fat']}g\n\n"
        f"{balance}\n\n{verdict}"
    )

def handle_craving(food):
    rows  = get_today_totals()
    cal, pro, car, fat = totals_from_rows(rows) if rows else (0, 0, 0, 0)
    remaining_cal = USER["calories"] - cal

    prompt = (
        f"{USER['name']} is craving {food}. He has {remaining_cal:.0f} kcal remaining today "
        f"and {USER['protein'] - pro:.0f}g protein left to hit. "
        f"Give: 1) A healthier alternative that satisfies the same craving with portion size. "
        f"2) If he has it anyway, what portion fits and how to adjust the rest of the day. "
        f"Be warm and non-judgmental. Keep it practical and Indian-food friendly."
    )
    return f"😋 *Craving: {food}*\n\n" + claude_chat(prompt)

def handle_recommend():
    history = get_daily_totals_last_n(7)
    if not history:
        return "📭 Not enough data yet — log a few days of meals first and I'll have personalised tips for you!"

    summary = "\n".join(
        f"  {r[0]}: {r[1]:.0f} kcal | {r[2]:.0f}g protein | {r[3]:.0f}g carbs | {r[4]:.0f}g fat"
        for r in history
    )
    prompt = (
        f"Here are {USER['name']}'s last 7 days of nutrition:\n{summary}\n\n"
        f"His targets: {USER['calories']} kcal | {USER['protein']}g protein | {USER['carbs']}g carbs | {USER['fat']}g fat.\n"
        f"Give: 1) 3 specific meal swap suggestions to better hit protein (Indian-food friendly). "
        f"2) Optimal meal timing around his evening weight training. "
        f"Be specific, practical, and encouraging."
    )
    return "🥗 *7-Day Recommendations*\n\n" + claude_chat(prompt)

def handle_progress():
    days_elapsed = (date.today() - START_DATE).days + 1
    history = get_daily_totals_last_n(days_elapsed)

    total_deficit = 0
    for r in history:
        daily_cal = r[1]
        total_deficit += max(USER["calories"] - daily_cal, 0)

    est_fat_lost_kg = total_deficit / 7700
    weeks_remaining = max(0, (10 - est_fat_lost_kg / 0.35))

    prompt = (
        f"{USER['name']} is {days_elapsed} days into his fat loss journey. "
        f"Estimated fat lost so far: {est_fat_lost_kg:.2f}kg from calorie tracking. "
        f"Goal: visible abs and muscle definition. Estimated {weeks_remaining:.1f} weeks remaining. "
        f"Write a short, genuine motivational progress update. Be specific, not generic."
    )
    motivation = claude_chat(prompt)

    return (
        f"📈 *Progress Report*\n\n"
        f"📅 Day {days_elapsed} of your journey\n"
        f"🏋️ Start date: {START_DATE.strftime('%d %b %Y')}\n"
        f"⚖️ Est. fat lost: ~{est_fat_lost_kg:.2f}kg\n"
        f"🎯 Est. weeks to goal: ~{weeks_remaining:.1f} weeks\n\n"
        f"{motivation}"
    )

def handle_reset():
    reset_today()
    return (
        f"🔄 *Reset complete!*\n\n"
        f"Today's log is cleared. Fresh start — let's make it count! 💪\n"
        f"Send me a photo or type what you eat to start logging."
    )

def handle_help():
    return (
        f"👋 Hey {USER['name']}! Here's what I can do:\n\n"
        f"📸 *Photo* — send a food photo to analyse it\n"
        f"✏️ *Text* — type what you ate (e.g. '2 boiled eggs')\n\n"
        f"*Commands:*\n"
        f"  /summary — full day recap\n"
        f"  /craving [food] — healthier alternatives\n"
        f"  /recommend — 7-day meal suggestions\n"
        f"  /progress — fat loss estimate\n"
        f"  /reset — clear today's log\n"
        f"  /help — show this menu\n\n"
        f"Daily targets: {USER['calories']} kcal | {USER['protein']}g protein | {USER['carbs']}g carbs | {USER['fat']}g fat"
    )

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    media_url    = request.form.get("MediaUrl0", "")
    from_number  = request.form.get("From", "")

    reply = process_message(incoming_msg, media_url)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}

def process_message(text, media_url=""):
    lower = text.lower().strip()

    try:
        # ── Image ──
        if media_url:
            data = analyse_image_food(media_url)
            log_food(data["food_name"], data["calories"], data["protein"], data["carbs"], data["fat"])
            rows = get_today_totals()
            cal, pro, car, fat = totals_from_rows(rows)
            return (
                f"📸 *{data['food_name']}* ({data['portion']})\n\n"
                f"🔥 {data['calories']:.0f} kcal\n"
                f"💪 Protein: {data['protein']:.0f}g\n"
                f"🌾 Carbs:   {data['carbs']:.0f}g\n"
                f"🥑 Fat:     {data['fat']:.0f}g\n\n"
                f"*Verdict:* {data['verdict']}\n"
                f"💡 {data['tip']}"
                + running_totals_msg(cal, pro, car, fat)
            )

        # ── Commands ──
        if lower == "/summary":
            return handle_summary()
        if lower.startswith("/craving "):
            return handle_craving(text[9:].strip())
        if lower == "/recommend":
            return handle_recommend()
        if lower == "/progress":
            return handle_progress()
        if lower == "/reset":
            return handle_reset()
        if lower in ("/help", "help", "hi", "hello", "hey"):
            return handle_help()

        # ── Text food log ──
        data = analyse_text_food(text)
        log_food(data["food_name"], data["calories"], data["protein"], data["carbs"], data["fat"])
        rows = get_today_totals()
        cal, pro, car, fat = totals_from_rows(rows)
        return (
            f"✅ *Logged: {data['food_name']}* ({data['portion']})\n\n"
            f"🔥 {data['calories']:.0f} kcal\n"
            f"💪 Protein: {data['protein']:.0f}g\n"
            f"🌾 Carbs:   {data['carbs']:.0f}g\n"
            f"🥑 Fat:     {data['fat']:.0f}g\n\n"
            f"*Verdict:* {data['verdict']}\n"
            f"💡 {data['tip']}"
            + running_totals_msg(cal, pro, car, fat)
        )

    except Exception as e:
        return f"⚠️ Something went wrong: {str(e)}\n\nTry again or type /help"

def send_whatsapp(to, body):
    client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to, body=body)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"✅ My Calorie bot is running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
