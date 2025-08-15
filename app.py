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
    
    # GUNAKAN LEVENSHTEIN DISTANCE - cari yang terbaik tanpa threshold dulu
    for keyword in subject_keywords:
        similarity = levenshtein_similarity(user_input.lower(), keyword.lower())
        print(f"   '{keyword}' = {similarity:.1f}%")
        
        if similarity > best_similarity:
            best_similarity = similarity
            matched_subject = keyword

    # Jika tidak ada yang bagus, coba substring match
    if best_similarity < 60:  # Threshold minimum untuk dianggap match
        for keyword in subject_keywords:
            if keyword in user_input.lower() or user_input.lower() in keyword:
                # Hitung ulang similarity untuk substring match
                substring_score = levenshtein_similarity(user_input.lower(), keyword.lower())
                if substring_score > best_similarity:
                    matched_subject = keyword
                    best_similarity = substring_score
                    print(f"📍 Substring match '{keyword}' = {best_similarity:.1f}%")

    # Minimal 30% similarity untuk dianggap valid
    if not matched_subject or best_similarity < 30:
        print("❌ No subject match found (score too low)")
        return None, 0, None

    print(f"✅ Best subject match: '{matched_subject}' with {best_similarity:.1f}%")

    # Query database untuk books dengan subject yang cocok
    conn = get_db_connection()
    if conn is None:
        return None, 0, None
    
    try:
        cur = conn.cursor(dictionary=True)
        query = """
        SELECT title, location FROM books 
        WHERE LOWER(subject) LIKE LOWER(%s) AND availability = 'tersedia'
        ORDER BY title
        """
        cur.execute(query, ('%' + matched_subject + '%',))
        results = cur.fetchall()

        if results:
            lokasi_rak = results[0]['location']
            total = len(results)
            daftar_judul = "\n".join([f"{i+1}. {row['title']}" for i, row in enumerate(results)])
            response = f"Ada {total} buku tentang {matched_subject} di rak {lokasi_rak}:\n{daftar_judul}"
            return response, round(best_similarity, 1), matched_subject
        else:
            response = f"Maaf, belum ada buku {matched_subject} yang tersedia saat ini."
            return response, round(best_similarity, 1), matched_subject

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
            if score > best_score:
                best_score = score
                matched_book = book
                if score > 80:  # Log high matches
                    print(f"   '{book['title']}' = {score:.1f}%")

        # Minimal 60% untuk title match
        if matched_book and best_score >= 60:
            status = "tersedia" if matched_book['availability'] == 'tersedia' else "sedang dipinjam"
            response = f"Buku \"{matched_book['title']}\" saat ini {status} (rak {matched_book['location']})"
            print(f"✅ Best title match: '{matched_book['title']}' with {best_score:.1f}%")
            return response, round(best_score, 1), matched_book['title']

        print("❌ No title match found (score too low)")
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

    # PRIORITAS 1: FAQ/Intent
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
                if similarity > 70:  # Log high matches
                    print(f"   High match: '{pattern[:30]}...' = {similarity:.1f}%")

    print(f"📚 Best FAQ match: {best_faq_score:.1f}%")

    # PRIORITAS 2: Subject Buku
    subject_result = search_books_by_subject(user_input_clean)
    if subject_result and len(subject_result) == 3:
        subject_response, subject_score, subject_keyword = subject_result
    else:
        subject_response, subject_score, subject_keyword = None, 0, None
    
    print(f"🔖 Subject search: {subject_score:.1f}% - '{subject_keyword}'")

    # PRIORITAS 3: Judul Buku
    title_result = search_books_by_title(user_input_clean)
    if title_result and len(title_result) == 3:
        book_title_response, book_score, book_title = title_result
    else:
        book_title_response, book_score, book_title = None, 0, None
    
    print(f"📖 Title search: {book_score:.1f}% - '{book_title}'")

    # LOGIKA PEMILIHAN BERDASARKAN SCORE TERTINGGI
    candidates = []
    
    # Tambahkan kandidat yang memenuhi threshold minimum
    if best_faq_score >= 50:  # FAQ threshold 50%
        candidates.append((best_faq_response, best_faq_score, best_faq_pattern, "FAQ"))
    
    if subject_score >= 60:  # Subject threshold 60%
        candidates.append((subject_response, subject_score, f"subject:{subject_keyword}", "SUBJECT"))
    
    if book_score >= 60:  # Title threshold 60%
        candidates.append((book_title_response, book_score, book_title, "TITLE"))

    # Jika ada kandidat, pilih yang score tertinggi
    if candidates:
        best_candidate = max(candidates, key=lambda x: x[1])
        response, score, pattern, type_match = best_candidate
        print(f"🏆 Winner: {type_match} with {score:.1f}%")
        return response, round(score, 1), pattern
    
    # Fallback: pilih yang terbaik meski di bawah threshold
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
        if score >= 25:  # threshold minimum untuk fallback
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

# Route untuk debug basic
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

# Route untuk debug detail scoring
@app.route("/debug-detail")
def debug_detail():
    query = request.args.get("q", "psikolog")
    
    # Test Levenshtein calculation dengan contoh
    test_subjects = ["psikologi", "technology", "sejarah", "matematika", "fisika", "kimia"]
    results = {}
    
    for subject in test_subjects:
        score = levenshtein_similarity(query.lower(), subject.lower())
        results[subject] = round(score, 1)
    
    # Test actual database subjects
    subjects = get_all_subject_keywords()
    db_results = {}
    for subject in subjects:
        score = levenshtein_similarity(query.lower(), subject.lower())
        if score > 0:  # Hanya tampilkan yang ada score
            db_results[subject] = round(score, 1)
    
    # Sort db_results by score descending
    db_results_sorted = dict(sorted(db_results.items(), key=lambda x: x[1], reverse=True))
    
    return jsonify({
        "query": query,
        "cleaned_query": clean_text(query),
        "test_scores": results,
        "database_subjects": db_results_sorted,
        "levenshtein_tests": {
            "psikolog_vs_psikologi": round(levenshtein_similarity("psikolog", "psikologi"), 1),
            "technology_vs_tech": round(levenshtein_similarity("technology", "tech"), 1),
            "exact_match": round(levenshtein_similarity("test", "test"), 1)
        },
        "total_subjects_in_db": len(subjects)
    })

# Route untuk melihat semua subjects di database
@app.route("/subjects")
def view_subjects():
    subjects = get_all_subject_keywords()
    return jsonify({
        "total": len(subjects),
        "subjects": sorted(subjects)
    })

# Health check untuk Railway
@app.route("/health")
def health_check():
    try:
        conn = get_db_connection()
        if conn:
            # Test query
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            conn.close()
            return jsonify({
                "status": "healthy", 
                "database": "connected",
                "intents_loaded": len(intents.get('intents', []))
            })
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