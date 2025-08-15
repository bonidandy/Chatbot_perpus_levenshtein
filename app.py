import json, os, random, re, tempfile
from flask import Flask, render_template, request, jsonify
from gtts import gTTS
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

# Memuat variabel lingkungan dari .env (untuk pengembangan lokal)
load_dotenv()

app = Flask(__name__)
app.static_folder = "static"

# Konfigurasi database untuk Railway
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'mysql.railway.internal'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME', 'railway'),
    'autocommit': True,
    'charset': 'utf8mb4',
    'collation': 'utf8mb4_unicode_ci',
    'connect_timeout': 60,
    'buffered': True
}

# Fungsi untuk mendapatkan koneksi database
def get_db_connection():
    try:
        config = {k: v for k, v in DB_CONFIG.items() if v is not None}
        connection = mysql.connector.connect(**config)
        if connection.is_connected():
            return connection
        else:
            return None
    except Error as e:
        print(f"❌ Error connecting to MySQL: {e}")
        return None

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
    return max(0, similarity)  # Ensure non-negative

# Load intents dari MySQL dengan koneksi Railway
def load_intents_from_db():
    conn = get_db_connection()
    if conn is None:
        print("❌ Koneksi database gagal!")
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
        print(f"✅ Loaded {len(intents['intents'])} intents from database")
        return intents
    except Error as e:
        print("❌ Database error:", e)
        return {"intents": []}
    except json.JSONDecodeError as e:
        print("❌ JSON decode error:", e)
        return {"intents": []}
    finally:
        if conn.is_connected():
            cur.close()
            conn.close()

# Load intents saat aplikasi dimulai
intents = load_intents_from_db()

def clean_text(text):
    """Membersihkan text dari karakter khusus dan mengubah ke lowercase"""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()

# Cari subject dari buku dengan koneksi Railway
def get_all_subject_keywords():
    conn = get_db_connection()
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT subject FROM books WHERE subject IS NOT NULL AND subject != ''")
        results = cur.fetchall()
        keywords = [row[0].lower() for row in results if row[0]]
        print(f"📚 Found {len(keywords)} subject keywords: {keywords}")
        return keywords
    except Error as e:
        print("❌ DB Error (get_all_subject_keywords):", e)
        return []
    finally:
        cur.close()
        conn.close()

# Cari buku berdasarkan subject menggunakan Levenshtein Distance
def search_books_by_subject(user_input):
    subject_keywords = get_all_subject_keywords()
    if not subject_keywords:
        return None, 0, None
    
    matched_subject = None
    best_similarity = 0

    print(f"🔍 Searching subjects for: '{user_input}'")
    
    # GUNAKAN LEVENSHTEIN DISTANCE seperti di fungsi lain
    for keyword in subject_keywords:
        similarity = levenshtein_similarity(user_input.lower(), keyword)
        print(f"   '{keyword}' = {similarity:.1f}%")
        
        if similarity > best_similarity and similarity >= 70:  # threshold 70%
            best_similarity = similarity
            matched_subject = keyword

    # Fallback: cek exact substring match (tapi tetap hitung scorenya)
    if not matched_subject:
        for keyword in subject_keywords:
            if keyword in user_input.lower() or user_input.lower() in keyword:
                matched_subject = keyword
                # Hitung score yang lebih realistis untuk substring match
                best_similarity = min(85, levenshtein_similarity(user_input.lower(), keyword))
                print(f"📍 Substring match '{keyword}' = {best_similarity:.1f}%")
                break

    if not matched_subject:
        print("❌ No subject match found")
        return None, 0, None

    print(f"✅ Best subject match: '{matched_subject}' with {best_similarity:.1f}%")

    conn = get_db_connection()
    if conn is None:
        return None, 0, None
    
    try:
        cur = conn.cursor(dictionary=True)
        query = """
        SELECT title, location FROM books 
        WHERE subject LIKE %s AND availability = 'tersedia'
        ORDER BY title
        """
        cur.execute(query, ('%' + matched_subject + '%',))
        results = cur.fetchall()

        if results:
            lokasi_rak = results[0]['location']
            total = len(results)
            daftar_judul = "\n".join([f"{i+1}. {row['title']}" for i, row in enumerate(results)])
            response = f"Ada {total} buku tentang {matched_subject} di rak {lokasi_rak}:\n{daftar_judul}"
            return response, best_similarity, matched_subject
        else:
            response = f"Maaf, belum ada buku {matched_subject} yang tersedia saat ini."
            return response, best_similarity, matched_subject

    except Error as e:
        print("❌ DB Error (search_books_by_subject):", e)
        return None, 0, None
    finally:
        cur.close()
        conn.close()

# Cari judul buku menggunakan Levenshtein Distance
def search_books_by_title(user_input):
    conn = get_db_connection()
    if conn is None:
        return None, 0, None
    
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT title, availability, location FROM books")
        books = cur.fetchall()

        best_score = 0
        matched_book = None

        print(f"🔍 Searching book titles for: '{user_input}'")

        for book in books:
            # Menggunakan Levenshtein similarity
            score = levenshtein_similarity(user_input.lower(), book['title'].lower())
            if score > best_score and score >= 75:  # threshold 75%
                best_score = score
                matched_book = book
                print(f"   '{book['title']}' = {score:.1f}%")

        if matched_book:
            status = "tersedia" if matched_book['availability'] == 'tersedia' else "sedang dipinjam"
            response = f"Buku \"{matched_book['title']}\" saat ini {status} (rak {matched_book['location']})"
            print(f"✅ Best title match: '{matched_book['title']}' with {best_score:.1f}%")
            return response, best_score, matched_book['title']

        print("❌ No title match found")
        return None, 0, None
    except Error as e:
        print("❌ DB Error (search_books_by_title):", e)
        return None, 0, None
    finally:
        cur.close()
        conn.close()

def find_best_match(user_input):
    user_input_clean = clean_text(user_input)
    print(f"\n🎯 Processing: '{user_input}' -> '{user_input_clean}'")

    # PRIORITAS 1: FAQ/Intent (60% threshold)
    best_faq_score = 0
    best_faq_response = ""
    best_faq_pattern = ""

    print(f"📚 Checking FAQ/Intents...")
    for intent in intents['intents']:
        for pattern in intent['patterns']:
            pattern_clean = clean_text(pattern)
            similarity = levenshtein_similarity(user_input_clean, pattern_clean)
            
            if similarity > best_faq_score:
                best_faq_score = similarity
                best_faq_response = random.choice(intent['responses'])
                best_faq_pattern = pattern
                if similarity > 80:  # Log high matches
                    print(f"   High match: '{pattern}' = {similarity:.1f}%")

    print(f"📚 Best FAQ match: {best_faq_score:.1f}% - '{best_faq_pattern[:50]}...'")

    # PRIORITAS 2: Subject Buku (70% threshold)
    subject_response, subject_score, subject_keyword = search_books_by_subject(user_input_clean)
    if not subject_response:
        subject_score = 0
        subject_keyword = None
    
    print(f"🔖 Subject search: {subject_score:.1f}% - '{subject_keyword}'")

    # PRIORITAS 3: Judul Buku (75% threshold)
    book_title_response, book_score, book_title = search_books_by_title(user_input_clean)
    if not book_title_response:
        book_score = 0
        book_title = None
    
    print(f"📖 Title search: {book_score:.1f}% - '{book_title}'")

    # LOGIKA PRIORITAS BERDASARKAN SCORE TERTINGGI
    candidates = []
    
    if best_faq_score >= 60:
        candidates.append((best_faq_response, best_faq_score, best_faq_pattern, "FAQ"))
    
    if subject_score >= 70:
        candidates.append((subject_response, subject_score, f"subject:{subject_keyword}", "SUBJECT"))
    
    if book_score >= 75:
        candidates.append((book_title_response, book_score, book_title, "TITLE"))

    if candidates:
        # Pilih yang score tertinggi
        best_candidate = max(candidates, key=lambda x: x[1])
        response, score, pattern, type_match = best_candidate
        print(f"🏆 Winner: {type_match} with {score:.1f}%")
        return response, round(score, 1), pattern
    
    # Jika tidak ada yang memenuhi threshold, pilih yang terbaik atau default
    all_scores = []
    
    if best_faq_score > 0:
        all_scores.append((best_faq_response, best_faq_score, best_faq_pattern, "FAQ"))
    if subject_score > 0:
        all_scores.append((subject_response, subject_score, f"subject:{subject_keyword}", "SUBJECT"))
    if book_score > 0:
        all_scores.append((book_title_response, book_score, book_title, "TITLE"))
    
    if all_scores:
        best_fallback = max(all_scores, key=lambda x: x[1])
        response, score, pattern, type_match = best_fallback
        if score >= 40:  # threshold minimum untuk fallback
            print(f"🔄 Fallback: {type_match} with {score:.1f}%")
            return response, round(score, 1), pattern

    print(f"❌ No match found")
    return "Maaf, saya tidak mengerti maksud Anda. Silakan coba pertanyaan lain atau hubungi petugas perpustakaan.", 0, ""

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/get")
def get_bot_response():
    user_txt = request.args.get("msg", "").strip()
    if not user_txt:
        return jsonify({
            "response": "Mohon masukkan pesan Anda.", 
            "score": 0, 
            "pattern": ""
        })

    try:
        response, score, pattern = find_best_match(user_txt)
        return jsonify({
            "response": response,
            "score": score,
            "pattern": pattern
        })
    except Exception as e:
        print(f"❌ Error in get_bot_response: {e}")
        return jsonify({
            "response": "Maaf, terjadi kesalahan sistem. Silakan coba lagi.", 
            "score": 0, 
            "pattern": "error"
        })

# Route untuk reload intents (berguna untuk development)
@app.route("/reload-intents")
def reload_intents():
    global intents
    try:
        intents = load_intents_from_db()
        return jsonify({
            "status": "success", 
            "message": f"Intents berhasil di-reload ({len(intents['intents'])} intents loaded)"
        })
    except Exception as e:
        return jsonify({
            "status": "error", 
            "message": f"Gagal reload intents: {e}"
        })

# Route untuk debug (hanya untuk development)
@app.route("/debug")
def debug_search():
    query = request.args.get("q", "technology")
    if query:
        print(f"\n{'='*60}")
        print(f"DEBUG SEARCH: '{query}'")
        print(f"{'='*60}")
        result = find_best_match(query)
        print(f"{'='*60}\n")
        return jsonify({
            "query": query,
            "result": {
                "response": result[0],
                "score": result[1],
                "pattern": result[2]
            }
        })
    return jsonify({"error": "Provide ?q=your_query parameter"})

# Health check untuk Railway
@app.route("/health")
def health_check():
    try:
        conn = get_db_connection()
        if conn:
            conn.close()
            return jsonify({"status": "healthy", "database": "connected"})
        else:
            return jsonify({"status": "unhealthy", "database": "disconnected"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    print("🚀 Starting Flask Chatbot Application...")
    print(f"📊 Loaded {len(intents.get('intents', []))} intents")
    
    # Untuk Railway deployment
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print(f"🌐 Running on port {port}, debug={debug_mode}")
    app.run(debug=debug_mode, host='0.0.0.0', port=port)