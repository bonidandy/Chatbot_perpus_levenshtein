import json, os, random, re, tempfile
from flask import Flask, render_template, request, jsonify
from gtts import gTTS
import mysql.connector
from mysql.connector import Error

app = Flask(__name__)
app.static_folder = "static"

# Implementasi Levenshtein Distance murni
def levenshtein_distance(s1, s2):
    """
    Menghitung Levenshtein Distance antara dua string
    """
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
    """
    Mengkonversi Levenshtein Distance ke similarity score (0-100)
    """
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 100.0
    
    distance = levenshtein_distance(s1, s2)
    similarity = ((max_len - distance) / max_len) * 100
    return similarity

# Load intents dari MySQL
def load_intents_from_db():
    try:
        conn = mysql.connector.connect(
            host="localhost", user="root", password="", database="crud"
        )
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
        print("Database error:", e)
        return {"intents": []}
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()

intents = load_intents_from_db()

def clean_text(text):
    return re.sub(r"[^\w\s]", "", text.lower()).strip()

# Cari subject dari buku
def get_all_subject_keywords():
    try:
        conn = mysql.connector.connect(
            host="localhost", user="root", password="", database="crud"
        )
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT subject FROM books")
        results = cur.fetchall()
        cur.close()
        conn.close()

        return [row[0].lower() for row in results if row[0]]
    except Error as e:
        print("DB Error (get_all_subject_keywords):", e)
        return []

# Cari judul buku menggunakan Levenshtein Distance
def search_books_by_title(user_input):
    try:
        conn = mysql.connector.connect(
            host="localhost", user="root", password="", database="crud"
        )
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT title, availability, location FROM books")
        books = cur.fetchall()
        cur.close()
        conn.close()

        best_score = 0
        matched_book = None

        for book in books:
            # Menggunakan Levenshtein similarity
            score = levenshtein_similarity(user_input.lower(), book['title'].lower())
            if score > best_score and score >= 75:
                best_score = score
                matched_book = book

        if matched_book:
            status = "tersedia" if matched_book['availability'] == 'tersedia' else "sedang dipinjam"
            return f"Buku \"{matched_book['title']}\" saat ini {status} (rak {matched_book['location']})", best_score, matched_book['title']

        return None, 0, None
    except Error as e:
        print("DB Error (search_books_by_title):", e)
        return None, 0, None

def search_books_by_subject(user_input):
    subject_keywords = get_all_subject_keywords()
    matched_subject = None
    best_similarity = 0

    # Cari subject dengan similarity terbaik menggunakan Levenshtein
    for keyword in subject_keywords:
        similarity = levenshtein_similarity(user_input.lower(), keyword)
        if similarity > best_similarity and similarity >= 70:
            best_similarity = similarity
            matched_subject = keyword

    # Fallback: cek apakah keyword ada dalam input
    if not matched_subject:
        for keyword in subject_keywords:
            if keyword in user_input.lower():
                matched_subject = keyword
                break

    if not matched_subject:
        return None

    try:
        conn = mysql.connector.connect(
            host="localhost", user="root", password="", database="crud"
        )
        cur = conn.cursor(dictionary=True)
        query = """
        SELECT title, location FROM books 
        WHERE subject LIKE %s AND availability = 'tersedia'
        """
        cur.execute(query, ('%' + matched_subject + '%',))
        results = cur.fetchall()
        cur.close()
        conn.close()

        if results:
            lokasi_rak = results[0]['location']
            total = len(results)
            daftar_judul = "\n".join([f"{i+1}. {row['title']}" for i, row in enumerate(results)])
            return (
                f"Ada {total} buku tentang {matched_subject} di rak {lokasi_rak}:\n{daftar_judul}"
            )
        else:
            return f"Maaf, belum ada buku {matched_subject} yang tersedia saat ini."

    except Error as e:
        print("DB Error (books):", e)
        return None

def find_best_match(user_input):
    user_input = clean_text(user_input)

    # PRIORITAS 1: FAQ/Intent (80%) - UTAMA
    best_score = 0
    best_response = ""
    best_pattern = ""

    for intent in intents['intents']:
        for pattern in intent['patterns']:
            pattern_clean = clean_text(pattern)
            
            # Menggunakan Levenshtein similarity murni
            similarity = levenshtein_similarity(user_input, pattern_clean)
            
            if similarity > best_score:
                best_score = similarity
                best_response = random.choice(intent['responses'])
                best_pattern = pattern

    # Jika FAQ cocok dengan threshold 80%, langsung return
    if best_score >= 60:
        return best_response, best_score, best_pattern

    # PRIORITAS 2: Subject Buku (70%)
    dynamic_book_response = search_books_by_subject(user_input)
    if dynamic_book_response:
        return dynamic_book_response, 100, "pencarian_subject"

    # PRIORITAS 3: Judul Buku (75%)
    book_title_response, book_score, book_pattern = search_books_by_title(user_input)
    if book_title_response:
        return book_title_response, book_score, book_pattern

    # Default response jika semua gagal
    return "Maaf, saya tidak mengerti maksud Anda.", 0, ""

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/get")
def get_bot_response():
    user_txt = request.args.get("msg", "").strip()
    if not user_txt:
        return jsonify({"response": "Mohon masukkan pesan Anda.", "score": 0, "pattern": ""})

    response, score, pattern = find_best_match(user_txt)
    return jsonify({
        "response": response,
        "score": score,
        "pattern": pattern
    })

if __name__ == "__main__":
    app.run(debug=True)