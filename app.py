import json, os, random, re
from flask import Flask, render_template, request, jsonify
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

# Load .env (untuk local dev, Railway otomatis inject ENV)
load_dotenv()

app = Flask(__name__)
app.static_folder = "static"

# ==============================
# Konstanta threshold
# ==============================
THRESH_INTENT  = 60.0   # FAQ/intent
THRESH_SUBJECT = 70.0   # subject buku
THRESH_TITLE   = 75.0   # judul buku

# ==============================
# Konfigurasi database (Railway + Local)
# ==============================
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "mysql.railway.internal"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "railway"),
    "autocommit": True,
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
    "connect_timeout": 60,
    "buffered": True,
}

def get_db_connection():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            return conn
    except Error as e:
        print(f"❌ DB Connection Error: {e}")
    return None

# ==============================
# Levenshtein
# ==============================
def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def levenshtein_similarity(s1, s2):
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 100.0
    distance = levenshtein_distance(s1, s2)
    return ((max_len - distance) / max_len) * 100.0

def clean_text(text):
    return re.sub(r"[^\w\s]", "", text.lower()).strip()

# ==============================
# Load intents dari MySQL
# ==============================
def load_intents_from_db():
    conn = get_db_connection()
    if conn is None:
        return {"intents": []}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM intents")
        rows = cur.fetchall()
        intents = {"intents": []}
        for row in rows:
            intents["intents"].append({
                "tag": row["tag"],
                "patterns": json.loads(row["patterns"]),
                "responses": json.loads(row["responses"])
            })
        return intents
    except Error as e:
        print("❌ Database error:", e)
        return {"intents": []}
    finally:
        cur.close()
        conn.close()

intents = load_intents_from_db()

# ==============================
# Data buku
# ==============================
def get_all_subject_keywords():
    conn = get_db_connection()
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT subject FROM books")
        results = cur.fetchall()
        return [row[0].lower() for row in results if row[0]]
    except Error as e:
        print("❌ DB Error (get_all_subject_keywords):", e)
        return []
    finally:
        cur.close()
        conn.close()

def search_books_by_title(user_input):
    conn = get_db_connection()
    if conn is None:
        return None, 0.0, None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT title, availability, location FROM books")
        books = cur.fetchall()

        best_score = 0.0
        matched_book = None
        ui = user_input.lower()

        for book in books:
            score = levenshtein_similarity(ui, book['title'].lower())
            if score > best_score and score >= THRESH_TITLE:
                best_score = score
                matched_book = book

        if matched_book:
            status = "tersedia" if matched_book['availability'] == 'tersedia' else "sedang dipinjam"
            response = f"Buku \"{matched_book['title']}\" saat ini {status} (rak {matched_book['location']})"
            return response, best_score, matched_book['title']

        return None, 0.0, None
    except Error as e:
        print("❌ DB Error (search_books_by_title):", e)
        return None, 0.0, None
    finally:
        cur.close()
        conn.close()

def search_books_by_subject(user_input):
    subject_keywords = get_all_subject_keywords()
    matched_subject = None
    best_similarity = 0.0
    ui = user_input.lower()

    for keyword in subject_keywords:
        similarity = levenshtein_similarity(ui, keyword)
        if similarity > best_similarity:
            best_similarity = similarity
            matched_subject = keyword

    if (not matched_subject) or (best_similarity < THRESH_SUBJECT):
        for keyword in subject_keywords:
            if keyword in ui:
                matched_subject = keyword
                best_similarity = max(best_similarity, 80.0)
                break

    if not matched_subject or best_similarity < THRESH_SUBJECT:
        return None, 0.0, None

    conn = get_db_connection()
    if conn is None:
        return None, 0.0, None
    try:
        cur = conn.cursor(dictionary=True)
        query = "SELECT title, location FROM books WHERE subject LIKE %s AND availability = 'tersedia'"
        cur.execute(query, ('%' + matched_subject + '%',))
        results = cur.fetchall()

        if results:
            lokasi_rak = results[0]['location']
            total = len(results)
            daftar_judul = "\n".join([f"{i+1}. {row['title']}" for i, row in enumerate(results)])
            response = f"Ada {total} buku tentang {matched_subject} di rak {lokasi_rak}:\n{daftar_judul}"
        else:
            response = f"Maaf, belum ada buku {matched_subject} yang tersedia saat ini."

        return response, best_similarity, f"subject:{matched_subject}"
    except Error as e:
        print("❌ DB Error (search_books_by_subject):", e)
        return None, 0.0, None
    finally:
        cur.close()
        conn.close()

# ==============================
# Orkestrasi pemilihan jawaban
# ==============================
def find_best_match(user_input):
    ui_clean = clean_text(user_input)

    # Intent
    best_score, best_response, best_pattern = 0.0, "", ""
    for intent in intents['intents']:
        for pattern in intent['patterns']:
            pattern_clean = clean_text(pattern)
            similarity = levenshtein_similarity(ui_clean, pattern_clean)
            if similarity > best_score:
                best_score = similarity
                best_response = random.choice(intent['responses'])
                best_pattern = pattern
    if best_score >= THRESH_INTENT:
        return best_response, best_score, best_pattern

    # Subject
    subj_resp, subj_score, subj_pattern = search_books_by_subject(user_input)
    if subj_resp:
        return subj_resp, subj_score, subj_pattern

    # Title
    title_resp, title_score, title_pattern = search_books_by_title(user_input)
    if title_resp:
        return title_resp, title_score, title_pattern

    return "Maaf, saya tidak mengerti maksud Anda, silakan pergi ke staf untuk pertanyaan lebih lanjut.", 0.0, ""

# ==============================
# Routes
# ==============================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/get")
def get_bot_response():
    user_txt = request.args.get("msg", "").strip()
    if not user_txt:
        return jsonify({"response": "Mohon masukkan pesan Anda.", "score": 0, "pattern": ""})
    response, score, pattern = find_best_match(user_txt)
    print(f"[BOT] pattern={pattern} | score={score:.2f} | user='{user_txt}'")
    return jsonify({"response": response, "score": score, "pattern": pattern})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
