#!/usr/bin/env python3
import os
import re
import json
import difflib
import sqlite3
import base64
import threading
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

# Pending confirmations: {from_number: nutrition_data_dict}
pending_confirmations: dict = {}
pending_lock = threading.Lock()

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            ended_at     TEXT
        )
    """)
    con.commit()
    con.close()

def get_active_session():
    """Returns (id, session_date) of the active session, or None."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, session_date FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    return row

def get_current_log_date():
    """Date label to use for food logging — active session date or today."""
    session = get_active_session()
    return session[1] if session else date.today().isoformat()

def get_today():
    return get_current_log_date()

def start_session():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO sessions (session_date, started_at) VALUES (?, ?)",
        (date.today().isoformat(), datetime.now().isoformat())
    )
    con.commit()
    con.close()

def end_session():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE sessions SET ended_at=? WHERE ended_at IS NULL",
        (datetime.now().isoformat(),)
    )
    con.commit()
    con.close()

def log_food(food_name, calories, protein, carbs, fat):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO food_log (log_date,food_name,calories,protein,carbs,fat,logged_at) VALUES (?,?,?,?,?,?,?)",
        (get_current_log_date(), food_name, calories, protein, carbs, fat, datetime.now().isoformat())
    )
    con.commit()
    con.close()

def get_totals_for_date(log_date):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT food_name,calories,protein,carbs,fat FROM food_log WHERE log_date=?",
        (log_date,)
    ).fetchall()
    con.close()
    return rows

def get_today_totals():
    return get_totals_for_date(get_current_log_date())

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
    con.execute("DELETE FROM food_log WHERE log_date=?", (get_current_log_date(),))
    con.commit()
    con.close()

def get_last_logged_entry():
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, food_name FROM food_log WHERE log_date=? ORDER BY logged_at DESC LIMIT 1",
        (get_current_log_date(),)
    ).fetchone()
    con.close()
    return row

def delete_entry_by_id(entry_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM food_log WHERE id=?", (entry_id,))
    con.commit()
    con.close()

def find_entry_fuzzy(food_name, log_date):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, food_name FROM food_log WHERE log_date=? ORDER BY logged_at DESC",
        (log_date,)
    ).fetchall()
    con.close()
    if not rows:
        return None
    query = food_name.lower()
    ids   = [r[0] for r in rows]
    names = [r[1].lower() for r in rows]
    # exact substring match
    for i, name in enumerate(names):
        if query in name or name in query:
            return (ids[i],)
    # word-level overlap
    query_words = set(query.split())
    best, best_id = 0, None
    for i, name in enumerate(names):
        overlap = len(query_words & set(name.split()))
        if overlap > best:
            best, best_id = overlap, ids[i]
    if best_id:
        return (best_id,)
    # difflib fuzzy fallback
    close = difflib.get_close_matches(query, names, n=1, cutoff=0.4)
    if close:
        return (ids[names.index(close[0])],)
    return None

def update_entry(entry_id, food_name, calories, protein, carbs, fat):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE food_log SET food_name=?, calories=?, protein=?, carbs=?, fat=? WHERE id=?",
        (food_name, calories, protein, carbs, fat, entry_id)
    )
    con.commit()
    con.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def progress_bar(current, target, length=10):
    pct    = min(current / target, 1.0)
    filled = int(pct * length)
    bar    = "█" * filled + "░" * (length - filled)
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
def parse_nutrition_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    return json.loads(raw)

NUTRITION_SYSTEM = f"""You are a friendly personal nutrition coach for {USER['name']}, a {USER['age']}-year-old, {USER['weight']}kg, {USER['height']}cm male.
Goal: {USER['goal']}. Daily targets: {USER['calories']} kcal | {USER['protein']}g protein | {USER['carbs']}g carbs | {USER['fat']}g fat.
He trains with weights 4-6 days/week. He is from Andhra Pradesh, India.

You have expert knowledge of Indian and Telugu cuisine. Key reference values:
- Idli (1 piece ~50g): 39 kcal, 2g protein, 8g carbs, 0.4g fat
- Dosa plain (1 ~90g): 133 kcal, 3g protein, 25g carbs, 2g fat
- Masala dosa (1): 215 kcal, 5g protein, 35g carbs, 6g fat
- Vada/medu vada (1): 97 kcal, 3g protein, 14g carbs, 4g fat
- Upma (1 cup): 180 kcal, 4g protein, 28g carbs, 6g fat
- Pesarattu (1 ~80g): 100 kcal, 6g protein, 15g carbs, 2g fat — green moong dal crepe
- Sambar (1 cup): 95 kcal, 5g protein, 14g carbs, 2.5g fat
- Rasam (1 cup): 40 kcal, 1g protein, 7g carbs, 1g fat
- Rice cooked (1 cup ~186g): 206 kcal, 4g protein, 45g carbs, 0.4g fat
- Chicken biryani (1 plate ~350g): 490 kcal, 28g protein, 58g carbs, 14g fat
- Natu kodi curry / country chicken curry (200g serving): 280 kcal, 32g protein, 8g carbs, 13g fat
- Gongura chicken (sorrel chicken, 200g): 290 kcal, 30g protein, 10g carbs, 14g fat
- Gongura mutton (200g): 320 kcal, 28g protein, 10g carbs, 18g fat
- Pulusu tamarind stew (1 cup): 120 kcal, 5g protein, 18g carbs, 3g fat
- Avakaya mango pickle (1 tbsp): 25 kcal, 0g protein, 3g carbs, 1.5g fat
- Kalakand (1 piece ~50g): 180 kcal, 5g protein, 28g carbs, 6g fat — milk-based sweet
- Pootharekulu (1 piece ~30g): 120 kcal, 2g protein, 22g carbs, 3g fat — rice paper sweet from Andhra
- Sooji/rava halwa (100g): 280 kcal, 4g protein, 42g carbs, 10g fat
- Gajar ka halwa carrot halwa (100g): 240 kcal, 5g protein, 35g carbs, 9g fat
- Besan ladoo (1 ~40g): 175 kcal, 4g protein, 22g carbs, 8g fat
- Gulab jamun (1 ~40g): 150 kcal, 2g protein, 27g carbs, 4g fat
- Rasgulla (1 ~40g): 100 kcal, 3g protein, 20g carbs, 1g fat
- Jalebi (2 pieces ~50g): 150 kcal, 1g protein, 34g carbs, 2g fat
- Dal toor/moong cooked (1 cup): 230 kcal, 18g protein, 40g carbs, 1g fat
- Chana masala (1 cup): 270 kcal, 14g protein, 45g carbs, 5g fat
- Palak paneer (1 cup): 290 kcal, 14g protein, 16g carbs, 18g fat
- Paneer (100g): 265 kcal, 18g protein, 4g carbs, 20g fat
- Roti/chapati (1): 70 kcal, 3g protein, 15g carbs, 0.5g fat
- Paratha plain (1): 260 kcal, 5g protein, 36g carbs, 10g fat
- Curd/yogurt full fat (1 cup): 100 kcal, 8g protein, 11g carbs, 2.5g fat
- Chai tea with milk (1 cup): 60 kcal, 2g protein, 9g carbs, 2g fat

When asked to analyse food, ALWAYS respond with valid JSON only (no markdown fences):
{{
  "food_name": "...",
  "portion": "...",
  "calories": 0,
  "protein": 0,
  "carbs": 0,
  "fat": 0,
  "verdict": "Great for your goal! / Decent choice / Watch the calories here",
  "tip": "One short practical tip",
  "ask_confirmation": false,
  "confirmation_prompt": ""
}}
Set ask_confirmation to true ONLY when genuinely uncertain what the food is or the portion size. Then set confirmation_prompt to: "Is this [specific dish description]? Reply yes to log [food_name] ([portion], ~[calories] kcal) or no to cancel."
Use realistic average values. Be concise and encouraging."""

def analyse_text_food(text):
    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=NUTRITION_SYSTEM,
        messages=[{"role": "user", "content": f"Analyse this food and return JSON: {text}"}]
    )
    return parse_nutrition_json(resp.content[0].text)

def analyse_image_food(image_url):
    img_resp   = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
    img_b64    = base64.standard_b64encode(img_resp.content).decode("utf-8")
    media_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]

    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=NUTRITION_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text",  "text": "Analyse this food photo and return JSON."}
            ]
        }]
    )
    return parse_nutrition_json(resp.content[0].text)

def claude_chat(prompt):
    resp = ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=(
            f"You are a friendly, concise nutrition coach for {USER['name']}. "
            f"Daily targets: {USER['calories']} kcal | {USER['protein']}g protein | "
            f"{USER['carbs']}g carbs | {USER['fat']}g fat. "
            f"Be practical, warm, and Indian-food friendly. Keep responses under 300 words."
        ),
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

# ── Session handlers ──────────────────────────────────────────────────────────
def generate_end_of_day_report(log_date, rows):
    cal, pro, car, fat = totals_from_rows(rows)
    t = USER

    food_list   = "\n".join(f"  • {r[0]} ({r[1]:.0f} kcal)" for r in rows)
    deficit     = t["calories"] - cal
    balance     = f"🔽 Deficit: {deficit:.0f} kcal" if deficit >= 0 else f"🔼 Surplus: {abs(deficit):.0f} kcal"
    protein_hit = "✅ Hit!" if pro >= t["protein"] * 0.9 else "❌ Missed"

    prompt = (
        f"End-of-day summary for {USER['name']} ({log_date}):\n"
        f"Calories: {cal:.0f}/{t['calories']} kcal | Protein: {pro:.0f}/{t['protein']}g | "
        f"Carbs: {car:.0f}/{t['carbs']}g | Fat: {fat:.0f}/{t['fat']}g\n"
        f"Foods eaten today: {', '.join(r[0] for r in rows)}\n\n"
        f"Give exactly 2-3 specific actionable suggestions for tomorrow to better hit targets. "
        f"Be Indian-food friendly. One sentence each, numbered list. No fluff."
    )
    suggestions = claude_chat(prompt)

    return (
        f"📋 *Day Summary — {log_date}*\n\n"
        f"*Meals logged:*\n{food_list}\n\n"
        f"🔥 Calories: {cal:.0f}/{t['calories']} kcal {progress_bar(cal, t['calories'])}\n"
        f"💪 Protein:  {pro:.0f}/{t['protein']}g  {progress_bar(pro, t['protein'])} {protein_hit}\n"
        f"🌾 Carbs:    {car:.0f}/{t['carbs']}g  {progress_bar(car, t['carbs'])}\n"
        f"🥑 Fat:      {fat:.0f}/{t['fat']}g  {progress_bar(fat, t['fat'])}\n\n"
        f"{balance}\n\n"
        f"🎯 *Tomorrow's focus:*\n{suggestions}"
    )

def handle_good_morning():
    parts    = []
    existing = get_active_session()

    if existing:
        _, prev_date = existing
        rows = get_totals_for_date(prev_date)
        if rows:
            report = generate_end_of_day_report(prev_date, rows)
            parts.append(
                f"⚠️ You forgot to say good night! Auto-closing {prev_date}...\n\n"
                f"{report}\n\n"
                f"─────────────────────\n\n"
            )
        end_session()

    start_session()
    parts.append(
        f"🌅 *Good morning, {USER['name']}!* 💪\n\n"
        f"New day session started — fresh start!\n"
        f"Log meals whenever you're ready. Say *good night* or *gn* to close your day and get your report."
    )
    return "".join(parts)

def handle_good_night():
    session = get_active_session()
    if not session:
        return "🌙 No active session. Send *good morning* or *gm* to start your day first!"

    _, session_date = session
    rows = get_totals_for_date(session_date)
    end_session()

    if not rows:
        return (
            f"🌙 *Good night, {USER['name']}!*\n\n"
            f"Nothing logged today — remember to track tomorrow! 😴\n"
            f"Start fresh with *good morning* tomorrow."
        )

    report = generate_end_of_day_report(session_date, rows)
    return f"🌙 *Good night, {USER['name']}!*\n\n{report}\n\n😴 Rest well — recovery is part of the journey!"

# ── Correction handlers ───────────────────────────────────────────────────────
def handle_undo():
    entry = get_last_logged_entry()
    if not entry:
        return "📭 Nothing to undo — no food logged in this session yet."

    entry_id, food_name = entry
    delete_entry_by_id(entry_id)

    rows = get_today_totals()
    if rows:
        cal, pro, car, fat = totals_from_rows(rows)
        return f"↩️ *Undone: {food_name}* removed." + running_totals_msg(cal, pro, car, fat)
    return f"↩️ *Undone: {food_name}* removed.\n\n📭 Log is now empty for today."

def handle_correction(text):
    m = re.match(r'correct\s+(.+?)\s+to\s+(.+)', text, re.IGNORECASE)
    if not m:
        return (
            "❓ Format: *correct [food] to [new description]*\n"
            "Example: correct chicken to 200g grilled chicken breast"
        )

    old_name   = m.group(1).strip()
    new_detail = m.group(2).strip()
    log_date   = get_current_log_date()
    entry      = find_entry_fuzzy(old_name, log_date)

    if not entry:
        rows = get_today_totals()
        food_list = "\n".join(f"  • {r[0]}" for r in rows) if rows else "  (nothing logged yet)"
        return (
            f"❓ Couldn't find '{old_name}' in today's log.\n\n"
            f"*Today's log:*\n{food_list}"
        )

    data = analyse_text_food(new_detail)
    update_entry(entry[0], data["food_name"], data["calories"], data["protein"], data["carbs"], data["fat"])

    rows = get_today_totals()
    cal, pro, car, fat = totals_from_rows(rows)
    return (
        f"✏️ *Corrected!*\n\n"
        f"Updated to: *{data['food_name']}* ({data['portion']})\n"
        f"🔥 {data['calories']:.0f} kcal | 💪 {data['protein']:.0f}g protein | "
        f"🌾 {data['carbs']:.0f}g carbs | 🥑 {data['fat']:.0f}g fat"
        + running_totals_msg(cal, pro, car, fat)
    )

def handle_pending_confirmation(lower, from_number):
    with pending_lock:
        data = pending_confirmations.pop(from_number, None)
    if data is None:
        return None  # raced away; fall through to normal processing
    if lower in ("yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure"):
        log_food(data["food_name"], data["calories"], data["protein"], data["carbs"], data["fat"])
        rows = get_today_totals()
        cal, pro, car, fat = totals_from_rows(rows)
        return (
            f"✅ *Logged: {data['food_name']}* ({data['portion']})\n\n"
            f"🔥 {data['calories']:.0f} kcal | 💪 {data['protein']:.0f}g protein | "
            f"🌾 {data['carbs']:.0f}g carbs | 🥑 {data['fat']:.0f}g fat"
            + running_totals_msg(cal, pro, car, fat)
        )
    return "❌ Cancelled. Send the food description again if you want to try differently."

# ── Command handlers ──────────────────────────────────────────────────────────
def handle_summary():
    rows = get_today_totals()
    if not rows:
        return "📭 No food logged today yet! Send me a photo or type what you've eaten."

    cal, pro, car, fat = totals_from_rows(rows)
    t = USER
    deficit     = t["calories"] - cal
    protein_hit = "✅ Hit!" if pro >= t["protein"] * 0.9 else "❌ Missed"
    food_list   = "\n".join(f"  • {r[0]} ({r[1]:.0f} kcal)" for r in rows)

    balance = (
        f"🔽 Deficit: {deficit:.0f} kcal — fat loss mode!"
        if deficit >= 0
        else f"🔼 Surplus: {abs(deficit):.0f} kcal — adjust dinner"
    )

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
    rows = get_today_totals()
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
    history      = get_daily_totals_last_n(days_elapsed)

    total_deficit = sum(max(USER["calories"] - r[1], 0) for r in history)
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
    session        = get_active_session()
    session_status = "🟢 Session active" if session else "🔴 No active session — say *good morning* to start"
    return (
        f"👋 Hey {USER['name']}! Here's what I can do:\n\n"
        f"{session_status}\n\n"
        f"📸 *Photo* — send a food photo to analyse it\n"
        f"✏️ *Text* — type what you ate (e.g. '2 boiled eggs')\n\n"
        f"*Session:*\n"
        f"  good morning / gm — start your day\n"
        f"  good night / gn — end day + get full report\n\n"
        f"*Corrections:*\n"
        f"  undo — remove last logged entry\n"
        f"  correct [food] to [new] — fix an entry\n\n"
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

    # Delivery status callbacks have no Body and no media — ack and skip
    if not incoming_msg and not media_url:
        return str(MessagingResponse()), 200, {"Content-Type": "text/xml"}

    app.logger.info("MSG from=%s body=%r media=%r", from_number, incoming_msg[:80], bool(media_url))

    reply = process_message(incoming_msg, media_url, from_number)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}

def process_message(text, media_url="", from_number=""):
    lower = text.lower().strip()

    try:
        # ── Pending confirmation ──
        if from_number and from_number in pending_confirmations:
            result = handle_pending_confirmation(lower, from_number)
            if result is not None:
                return result
            # entry was raced away; fall through and process as normal message

        # ── Image ──
        if media_url:
            data = analyse_image_food(media_url)
            if data.get("ask_confirmation"):
                if from_number:
                    with pending_lock:
                        pending_confirmations[from_number] = data
                return (
                    f"🤔 *Just to confirm:*\n{data['confirmation_prompt']}\n\n"
                    f"Reply *yes* to log or *no* to cancel."
                )
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

        # ── Session ──
        if lower in ("good morning", "gm", "morning"):
            return handle_good_morning()
        if lower in ("good night", "gn", "goodnight", "night", "gnight"):
            return handle_good_night()

        # ── Corrections ──
        if lower == "undo":
            return handle_undo()
        if re.match(r'correct\s+to\s+', lower):
            rows = get_today_totals()
            food_list = "\n".join(f"  • {r[0]}" for r in rows) if rows else "  (nothing logged yet)"
            return (
                f"❓ Which food are you correcting?\n\n"
                f"*Today's log:*\n{food_list}\n\n"
                f"Format: *correct [food] to [new description]*\n"
                f"Example: correct chicken to 200g grilled chicken breast"
            )
        if re.match(r'correct\s+.+\s+to\s+.+', lower):
            return handle_correction(text)

        # ── Slash commands ──
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
        if data.get("ask_confirmation"):
            if from_number:
                with pending_lock:
                    pending_confirmations[from_number] = data
            return (
                f"🤔 *Just to confirm:*\n{data['confirmation_prompt']}\n\n"
                f"Reply *yes* to log or *no* to cancel."
            )
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
