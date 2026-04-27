import json, os, random, datetime, sqlite3, asyncio, glob, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.error import NetworkError

# Logging for debugging proxy issues
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------- CONFIG -------------------------
BOT_TOKEN = "8659698523:AAHZ2AZAOK62Aqau9UGIhsSUW4XmEeHai3c"
ADMIN_IDS = [2067674349]               
QUESTIONS_FOLDER = "questions"        
DB_FILE = "bot.db"

(PRACTICE_CHOOSE_CHAPTER, PRACTICE_CHOOSE_DIFF, PRACTICE_CHOOSE_NUM) = range(3)
QUIZ_ACTIVE = 100 

# ------------------------- DATABASE -------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, total_quizzes INTEGER DEFAULT 0, total_questions INTEGER DEFAULT 0, correct_answers INTEGER DEFAULT 0, best_score REAL DEFAULT 0, last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS seen (user_id INTEGER, question_id TEXT, PRIMARY KEY (user_id, question_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mode TEXT, score INTEGER, total INTEGER, percentage REAL, started TIMESTAMP DEFAULT CURRENT_TIMESTAMP, finished TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

def register_user(user):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)", (user.id, user.username, user.first_name))
    cur.execute("UPDATE users SET username=?, first_name=?, last_active=CURRENT_TIMESTAMP WHERE user_id=?", (user.username, user.first_name, user.id))
    conn.commit()
    conn.close()

# ------------------------- QUESTION LOADER -------------------------
def load_all_questions():
    all_q = []
    if not os.path.exists(QUESTIONS_FOLDER): os.makedirs(QUESTIONS_FOLDER)
    for filepath in glob.glob(os.path.join(QUESTIONS_FOLDER, "*.json")):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                questions = data if isinstance(data, list) else data.get("questions", [])
                chapter = os.path.splitext(os.path.basename(filepath))[0]
                for q in questions:
                    if "chapter" not in q: q["chapter"] = chapter
                    all_q.append(q)
        except Exception as e: logger.error(f"Error loading {filepath}: {e}")
    return all_q

def get_unseen_questions(user_id, filters, count):
    conn = sqlite3.connect(DB_FILE)
    seen = {row[0] for row in conn.execute("SELECT question_id FROM seen WHERE user_id=?", (user_id,))}
    conn.close()
    all_q = load_all_questions()
    available = [q for q in all_q if str(q.get("id", "")) not in seen]
    if filters.get("chapter") and filters["chapter"] != "All":
        available = [q for q in available if q.get("chapter") == filters["chapter"]]
    if not available: available = all_q
    return random.sample(available, min(count, len(available)))

def save_session(user_id, mode, score, total):
    perc = round(score/total*100, 2) if total else 0
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO sessions (user_id, mode, score, total, percentage) VALUES (?,?,?,?,?)", (user_id, mode, score, total, perc))
    conn.execute("UPDATE users SET total_quizzes=total_quizzes+1, total_questions=total_questions+?, correct_answers=correct_answers+?, best_score=MAX(best_score, ?) WHERE user_id=?", (total, score, perc, user_id))
    conn.commit()
    conn.close()
    return perc

# ------------------------- UI DESIGN -------------------------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Start Practice", callback_data="practice")],
        [InlineKeyboardButton("🧪 Full Mock Test", callback_data="mock_start")],
        [InlineKeyboardButton("📊 My Stats", callback_data="mystats")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")]
    ])

def get_quiz_kb(q, mode):
    opts = q["_shuffled_opts"]
    kb = []
    # Grid layout for options
    for i in range(0, len(opts), 2):
        row = [InlineKeyboardButton(f"{chr(65+j)}", callback_data=f"ans_{mode}_{j}") for j in range(i, min(i+2, len(opts)))]
        kb.append(row)
    # Control Bar
    kb.append([
        InlineKeyboardButton("⬅️ Prev", callback_data=f"prev_{mode}"),
        InlineKeyboardButton("⏭️ Skip", callback_data=f"skip_{mode}"),
        InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{mode}")
    ])
    return InlineKeyboardMarkup(kb)

# ------------------------- RESILIENCE WRAPPER -------------------------
async def safe_edit(query, text, reply_markup=None):
    """Retries editing a message multiple times to bypass 503 proxy errors."""
    for i in range(5):
        try:
            return await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except NetworkError:
            await asyncio.sleep(1) # Wait for proxy to clear
    logger.warning("Safe edit failed after 5 retries.")

# ------------------------- HANDLERS -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await update.message.reply_text("🏥 *NORCET AI Preparation*\nChoose your mode:", parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer() 
    except: pass

    data = query.data
    if data == "practice":
        chapters = sorted({q.get("chapter", "Gen") for q in load_all_questions()})
        kb = [[InlineKeyboardButton(f"📂 {ch}", callback_data=f"pchap_{ch}")] for ch in chapters]
        kb.append([InlineKeyboardButton("« Back", callback_data="main")])
        await safe_edit(query, "🎯 *Select Subject:*", InlineKeyboardMarkup(kb))
        return PRACTICE_CHOOSE_CHAPTER
    
    elif data.startswith("pchap_"):
        context.user_data["p_chapter"] = data[6:]
        kb = [[InlineKeyboardButton(d, callback_data=f"pdiff_{d}")] for d in ["Easy", "Medium", "Hard", "All"]]
        await safe_edit(query, "⚖️ *Select Difficulty:*", InlineKeyboardMarkup(kb))
        return PRACTICE_CHOOSE_DIFF

    elif data.startswith("pdiff_"):
        context.user_data["p_diff"] = data[6:]
        kb = [[InlineKeyboardButton(str(n), callback_data=f"pnum_{n}")] for n in [10, 20, 50]]
        await safe_edit(query, "🔢 *Questions:*", InlineKeyboardMarkup(kb))
        return PRACTICE_CHOOSE_NUM

    elif data.startswith("pnum_"):
        num = int(data[5:])
        qs = get_unseen_questions(update.effective_user.id, {"chapter": context.user_data["p_chapter"]}, num)
        for q in qs:
            o = q["options"][:]
            random.shuffle(o)
            q["_shuffled_opts"] = o
        context.user_data.update({"qs": qs, "idx": 0, "correct": 0, "mode": "practice"})
        await send_q(query, context)
        return QUIZ_ACTIVE

    elif data == "mock_start":
        qs = get_unseen_questions(update.effective_user.id, {}, 100)
        for q in qs:
            o = q["options"][:]
            random.shuffle(o)
            q["_shuffled_opts"] = o
        context.user_data.update({"qs": qs, "idx": 0, "correct": 0, "mode": "mock"})
        await send_q(query, context)
        return QUIZ_ACTIVE
    
    elif data == "main":
        await safe_edit(query, "🏥 *Main Menu:*", main_menu_kb())
        return ConversationHandler.END

async def send_q(query, context):
    idx, qs, mode = context.user_data["idx"], context.user_data["qs"], context.user_data["mode"]
    if idx >= len(qs): return await finish(query, context)
    q = qs[idx]
    
    # Format options list for the message body
    opts_text = "\n".join([f"*{chr(65+i)}.* {opt}" for i, opt in enumerate(q["_shuffled_opts"])])
    txt = f"📝 *Question {idx+1}/{len(qs)}*\n\n{q['question']}\n\n{opts_text}"
    
    await safe_edit(query, txt, get_quiz_kb(q, mode))

async def quiz_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data_parts = query.data.split("_")
    action, mode = data_parts[0], data_parts[1]
    
    # Safely get current index and questions list
    idx = context.user_data.get("idx", 0)
    qs = context.user_data.get("qs", [])
    
    # 🛡️ SAFETY CHECK: If index somehow exceeds the list, end the quiz immediately to prevent IndexError
    if idx >= len(qs):
        await finish(query, context)
        return ConversationHandler.END
        
    if action == "ans":
        ans_idx = int(data_parts[2])
        q = qs[idx]
        is_cor = q["_shuffled_opts"][ans_idx] == q["answer"]
        
        # Immediate feedback via pop-up alert
        feedback = "✅ Correct!" if is_cor else f"❌ Wrong! Ans: {q['answer']}"
        try: await query.answer(feedback, show_alert=False) 
        except: pass

        if is_cor: context.user_data["correct"] += 1
        
        if mode == "practice":
            # Show detailed result + explanation in Practice mode
            res_txt = f"{feedback}\n\n📖 *Explanation:*\n{q.get('explanation', 'Not provided.')}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Next Question ➡️", callback_data=f"skip_{mode}")]])
            await safe_edit(query, res_txt, kb)
        else:
            # Auto-advance in Mock mode for speed
            context.user_data["idx"] += 1
            await send_q(query, context)
            
    elif action == "skip":
        context.user_data["idx"] += 1
        await send_q(query, context)
    elif action == "prev" and idx > 0:
        context.user_data["idx"] -= 1
        await send_q(query, context)
    elif action == "stop":
        await finish(query, context)
        return ConversationHandler.END

async def finish(query, context):
    mode = context.user_data.get("mode", "practice")
    total_attempted = context.user_data.get("idx", 0)
    if total_attempted == 0: total_attempted = 1 # Avoid div by zero
    perc = save_session(query.from_user.id, mode, context.user_data.get("correct", 0), total_attempted)
    
    txt = f"🏁 *Test Finished!*\n\n✅ Correct: `{context.user_data.get('correct', 0)}`\n📊 Accuracy: `{perc}%`"
    await safe_edit(query, txt, main_menu_kb())

# ------------------------- MAIN -------------------------
def main():
    init_db()
    # Corrected proxy config and high-latency settings
    t_req = HTTPXRequest(proxy="http://proxy.server:3128", connect_timeout=60.0, read_timeout=60.0)
    app = Application.builder().token(BOT_TOKEN).request(t_req).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^(practice|mock_start|main)$")],
        states={
            PRACTICE_CHOOSE_CHAPTER: [CallbackQueryHandler(button_handler, pattern="^pchap_")],
            PRACTICE_CHOOSE_DIFF: [CallbackQueryHandler(button_handler, pattern="^pdiff_")],
            PRACTICE_CHOOSE_NUM: [CallbackQueryHandler(button_handler, pattern="^pnum_")],
            QUIZ_ACTIVE: [CallbackQueryHandler(quiz_logic, pattern="^(ans|skip|prev|stop)_")],
        },
        fallbacks=[CommandHandler("start", start_cmd)],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(conv)
    
    print("Bot starting with Safe-Edit technology and Index Armor...")
    app.run_polling(poll_interval=3.0)

if __name__ == "__main__":
    main()
