import json, os, random, sqlite3, asyncio, glob, logging, hashlib, re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import Forbidden

# ------------------------- CONFIG -------------------------
BOT_TOKEN = "8636252501:AAEFLhyuYCEUnXitRcQrHxCmoSj3h3qgs_U"
ADMIN_IDS = [2067674349]               
QUESTIONS_FOLDER = "questions"        
DB_FILE = "bot.db"

# Logging setup to help you see errors
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation States
(CHOOSE_SUBJECT, CHOOSE_DIFF, CHOOSE_COUNT, QUIZ_RUNNING, BROADCAST_S) = range(5)

# ------------------------- DATABASE & REGISTRATION -------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, 
        total_quizzes INTEGER DEFAULT 0, correct_answers INTEGER DEFAULT 0, 
        best_score REAL DEFAULT 0, last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mode TEXT, 
        score INTEGER, total INTEGER, perc REAL DEFAULT 0.0, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS seen (
        user_id INTEGER, question_id TEXT, mode TEXT, PRIMARY KEY (user_id, question_id, mode))""")
    
    # Migration for 'mode' column
    cur.execute("PRAGMA table_info(seen)")
    if 'mode' not in [col[1] for col in cur.fetchall()]:
        cur.execute("ALTER TABLE seen ADD COLUMN mode TEXT DEFAULT 'PRACTICE'")
    conn.commit()
    conn.close()

def register_user(user):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)", (user.id, user.username, user.first_name))
    conn.execute("UPDATE users SET last_active=CURRENT_TIMESTAMP WHERE user_id=?", (user.id,))
    conn.commit()
    conn.close()

# ------------------------- QUESTION ENGINE -------------------------
def load_all_questions():
    all_q = []
    # Search in root and questions folder
    json_files = glob.glob("*.json") + glob.glob(os.path.join(QUESTIONS_FOLDER, "*.json"))
    
    for filepath in set(json_files):
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    root_sub = data.get("subject") or data.get("chapter_id")
                    questions = data.get("questions", [])
                else:
                    root_sub = None
                    questions = data
                
                if not root_sub:
                    root_sub = os.path.basename(filepath).replace(".json", "").strip()
                
                for q in questions:
                    # Generate unique ID based on question text to prevent collisions across files
                    q["id"] = hashlib.md5(str(q.get("question", "")).encode()).hexdigest()
                    q["chapter"] = str(q.get("chapter") or root_sub).replace("_", " ").title()
                    if not q.get("difficulty"): q["difficulty"] = "medium"
                    all_q.append(q)
            logger.info(f"Successfully loaded {len(questions)} questions from {filepath}")
        except Exception as e:
            # THIS WILL PRINT THE EXACT ERROR FOR YOUR 700 JSON FILE IN YOUR TERMINAL
            logger.error(f"❌ FAILED TO LOAD {filepath}: {e}")
    return all_q

def get_unseen_questions(user_id, count, chapter=None, difficulty=None, is_mock=False):
    conn = sqlite3.connect(DB_FILE)
    mode_label = "MOCK" if is_mock else "PRACTICE"
    seen = {row[0] for row in conn.execute("SELECT question_id FROM seen WHERE user_id=? AND mode=?", (user_id, mode_label)).fetchall()}
    conn.close()

    all_q = load_all_questions()
    available = [q for q in all_q if q["id"] not in seen]
    
    if chapter and chapter != "All":
        available = [q for q in available if q.get("chapter") == chapter]
        
    if not available: return []

    selected = []
    if is_mock:
        # 70% Hard, 20% Med, 10% Easy
        h_p = [q for q in available if str(q.get("difficulty","")).lower() == "hard"]
        m_p = [q for q in available if str(q.get("difficulty","")).lower() in ["medium", "med", "normal"]]
        e_p = [q for q in available if str(q.get("difficulty","")).lower() == "easy"]
        h_req, m_req = int(count * 0.70), int(count * 0.20)
        e_req = count - h_req - m_req
        selected = random.sample(h_p, min(h_req, len(h_p))) + random.sample(m_p, min(m_req, len(m_p))) + random.sample(e_p, min(e_req, len(e_p)))
        if len(selected) < count:
            remaining = [q for q in available if q not in selected]
            selected += random.sample(remaining, min(count - len(selected), len(remaining)))
        random.shuffle(selected)
    else:
        if difficulty and difficulty != "All":
            available = [q for q in available if str(q.get("difficulty", "")).lower() == difficulty.lower()]
        selected = random.sample(available, min(count, len(available)))

    for q in selected:
        o = q["options"][:]
        random.shuffle(o)
        q["_shuffled"] = o
    return selected

# ------------------------- ADMIN & BROADCAST -------------------------
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin feature to verify file loading"""
    if update.effective_user.id not in ADMIN_IDS: return
    all_q = load_all_questions()
    breakdown = {}
    for q in all_q:
        ch = q["chapter"]
        breakdown[ch] = breakdown.get(ch, 0) + 1
    
    items = "\n".join([f"• <b>{k}</b>: {v} Qs" for k,v in breakdown.items()])
    msg = f"<b>👑 ADMIN GLOBAL STATS</b>\n\nTotal Questions Loaded: {len(all_q)}\n\n<b>Breakdown:</b>\n{items}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("📣 Send the message to broadcast (text/image/file), or /cancel.")
    return BROADCAST_S

async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    count = 0
    for (uid,) in users:
        try:
            await update.message.copy(chat_id=uid)
            count += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    await update.message.reply_text(f"✅ Broadcast sent to {count} users.")
    return ConversationHandler.END

# ------------------------- MAIN HANDLERS -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    all_q = load_all_questions()
    kb = [
        [InlineKeyboardButton("📚 Practice Mode", callback_data="start_practice")],
        [InlineKeyboardButton("🧪 Mock (PRE)", callback_data="mock_pre"), InlineKeyboardButton("🔥 Mock (MAINS)", callback_data="mock_mains")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats"), InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("❓ Help", callback_data="help_info")]
    ]
    text = f"🏥 <b>NORCET AI PORTAL</b>\n\nTotal Questions: <code>{len(all_q)}</code>\nSelect your session:"
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    top = conn.execute("SELECT first_name, correct_answers FROM users ORDER BY correct_answers DESC LIMIT 10").fetchall()
    conn.close()
    text = "🏆 <b>TOP 10 LEADERBOARD</b>\n\n" + "\n".join([f"{i+1}. {u[0]} - {u[1]} Correct" for i, u in enumerate(top)])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="main_menu")]]), parse_mode=ParseMode.HTML)

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data, uid = query.data, update.effective_user.id
    await query.answer()

    if data == "main_menu": await start(update, context); return ConversationHandler.END
    if data == "leaderboard": await leaderboard(update, context); return
    if data == "help_info": 
        await query.edit_message_text("📢 Support: @StorylineArtBots", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="main_menu")]]))
        return

    if data == "start_practice":
        chapters = sorted({q["chapter"] for q in load_all_questions()})
        context.user_data["ch_list"] = chapters
        kb = [[InlineKeyboardButton(f"📂 {c}", callback_data=f"setch_{i}")] for i, c in enumerate(chapters)]
        kb.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
        await query.edit_message_text("🎯 <b>Select Subject:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        return CHOOSE_SUBJECT

    if data.startswith("setch_"):
        context.user_data["temp_ch"] = context.user_data["ch_list"][int(data.split("_")[1])]
        kb = [[InlineKeyboardButton(d, callback_data=f"sd_{d}")] for d in ["Easy", "Medium", "Hard", "All"]]
        await query.edit_message_text("⚖️ <b>Difficulty:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        return CHOOSE_DIFF

    if data.startswith("sd_"):
        context.user_data["temp_diff"] = data.split("_")[1]
        kb = [[InlineKeyboardButton(str(n), callback_data=f"sn_{n}")] for n in [10, 20, 50]]
        await query.edit_message_text("🔢 <b>Question Count:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        return CHOOSE_COUNT

    if data.startswith("sn_"):
        cnt = int(data.split("_")[1])
        qs = get_unseen_questions(uid, cnt, context.user_data["temp_ch"], context.user_data["temp_diff"])
        if not qs:
            await query.edit_message_text("❌ No new questions!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="main_menu")]]))
            return ConversationHandler.END
        context.user_data.update({"qs": qs, "idx": 0, "correct": 0, "mode": "PRACTICE"})
        await send_question(query, context); return QUIZ_RUNNING

    if data.startswith("mock_"):
        cnt = 100 if "pre" in data else 150
        qs = get_unseen_questions(uid, cnt, is_mock=True)
        context.user_data.update({"qs": qs, "idx": 0, "correct": 0, "mode": "MOCK"})
        await send_question(query, context); return QUIZ_RUNNING

async def send_question(query, context):
    ud = context.user_data
    q = ud["qs"][ud["idx"]]
    opts = "\n".join([f"<b>{chr(65+i)}.</b> {opt}" for i, opt in enumerate(q["_shuffled"])])
    text = f"✨ <b>Q {ud['idx']+1}/{len(ud['qs'])}</b>\n\n{q['question']}\n\n{opts}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("A", callback_data="ans_0"), InlineKeyboardButton("B", callback_data="ans_1")],
        [InlineKeyboardButton("C", callback_data="ans_2"), InlineKeyboardButton("D", callback_data="ans_3")],
        [InlineKeyboardButton("⏭️ Skip", callback_data="nav_skip"), InlineKeyboardButton("🛑 Stop", callback_data="nav_stop")]
    ]), parse_mode=ParseMode.HTML)

async def quiz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ud, data, uid = context.user_data, query.data, update.effective_user.id

    if data.startswith("ans_"):
        idx = int(data.split("_")[1])
        q = ud["qs"][ud["idx"]]
        is_cor = q["_shuffled"][idx] == q["answer"]
        if is_cor: ud["correct"] += 1
        
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT OR IGNORE INTO seen (user_id, question_id, mode) VALUES (?,?,?)", (uid, q["id"], ud["mode"]))
        conn.commit(); conn.close()
        
        if ud["mode"] == "PRACTICE":
            txt = f"{'✅' if is_cor else '❌'} <b>Ans:</b> <code>{q['answer']}</code>\n\n📖 {q.get('explanation','-')}"
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next ➡️", callback_data="nav_skip")]]), parse_mode=ParseMode.HTML)
        else:
            ud["idx"] += 1
            if ud["idx"] >= len(ud["qs"]): await finish_quiz(query, context)
            else: await send_question(query, context)

    elif data == "nav_skip":
        ud["idx"] += 1
        if ud["idx"] >= len(ud["qs"]): await finish_quiz(query, context)
        else: await send_question(query, context)
    elif data == "nav_stop": await finish_quiz(query, context, "🛑 Stopped")

async def finish_quiz(query, context, reason="🏁 Completed"):
    ud = context.user_data
    cor, tot = ud["correct"], max(ud["idx"], 1)
    await query.edit_message_text(f"{reason}\n✅ <b>Score:</b> <code>{cor}/{tot}</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="main_menu")]]), parse_mode=ParseMode.HTML)
    return ConversationHandler.END

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    bc_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={BROADCAST_S: [MessageHandler(filters.ALL & ~filters.COMMAND, do_broadcast)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    quiz_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callbacks, pattern="^(start_practice|mock_pre|mock_mains)$")],
        states={
            CHOOSE_SUBJECT: [CallbackQueryHandler(handle_callbacks, pattern="^setch_")],
            CHOOSE_DIFF: [CallbackQueryHandler(handle_callbacks, pattern="^sd_")],
            CHOOSE_COUNT: [CallbackQueryHandler(handle_callbacks, pattern="^sn_")],
            QUIZ_RUNNING: [CallbackQueryHandler(quiz_handler, pattern="^(ans_|nav_)")],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(bc_handler)
    app.add_handler(CallbackQueryHandler(handle_callbacks, pattern="^(help_info|main_menu|leaderboard)$"))
    app.add_handler(quiz_conv)
    
    print("Bot is LIVE...")
    app.run_polling()

if __name__ == "__main__": main()
