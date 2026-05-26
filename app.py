from flask import Flask, request, jsonify, render_template, redirect, session
from openai import OpenAI
from dotenv import load_dotenv
import sqlite3
import os
import base64
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from pypdf import PdfReader
from docx import Document

load_dotenv()

app = Flask(__name__)
app.secret_key = "businessbuilder-secret-2026"

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
You are BusinessBuilder AI, an AI assistant that helps users create businesses.
Help with business ideas, branding, Shopify planning, Canva ideas, marketing, pricing, and invoices.
Ask useful questions and give step-by-step help.
Do not pretend real Shopify, Canva, or payment tools are connected yet.
"""

def db():
    return sqlite3.connect("business_ai.db")

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            business_type TEXT NOT NULL,
            description TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL
        )
    """)

    try:
        cur.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")
    except:
        pass

    conn.commit()
    conn.close()

def save_message(user_id, chat_id, role, content):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, chat_id, role, content) VALUES (?, ?, ?, ?)",
        (user_id, chat_id, role, content)
    )
    conn.commit()
    conn.close()

def save_project(user_id, title, business_type, description):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (user_id, title, business_type, description) VALUES (?, ?, ?, ?)",
        (user_id, title, business_type, description)
    )
    conn.commit()
    conn.close()

def get_projects(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT title, business_type, description FROM projects WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    )
    projects = cur.fetchall()
    conn.close()
    return projects

def get_memory(user_id, chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content FROM messages
        WHERE user_id = ? AND chat_id = ?
        ORDER BY id DESC LIMIT 10
        """,
        (user_id, chat_id)
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]

def get_chat_messages(user_id, chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content FROM messages
        WHERE user_id = ? AND chat_id = ?
        ORDER BY id ASC
        """,
        (user_id, chat_id)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in rows]

def create_chat(user_id, title="New Chat"):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chats (user_id, title) VALUES (?, ?)",
        (user_id, title)
    )
    conn.commit()
    chat_id = cur.lastrowid
    conn.close()
    return chat_id

def update_chat_title(chat_id, title):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chats SET title = ? WHERE id = ?",
        (title, chat_id)
    )
    conn.commit()
    conn.close()

def get_chats(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title FROM chats WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    )
    chats = cur.fetchall()
    conn.close()
    return chats

def get_latest_uploaded_file(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT filepath FROM uploads WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    file = cur.fetchone()
    conn.close()
    return file[0] if file else None

def extract_file_text(filepath):
    if filepath.endswith(".txt"):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    if filepath.endswith(".pdf"):
        reader = PdfReader(filepath)
        text = ""

        for page in reader.pages:
            text += page.extract_text() or ""

        return text

    if filepath.endswith(".docx"):
        document = Document(filepath)
        text = ""

        for paragraph in document.paragraphs:
            text += paragraph.text + "\n"

        return text

    return ""

def image_to_base64(filepath):
    with open(filepath, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")
    
@app.route("/")
def home():
    if "user_id" not in session:
        return redirect("/login")

    chats = get_chats(session["user_id"])
    return render_template("index.html", chats=chats)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    email = request.form["email"]
    password = generate_password_hash(request.form["password"])

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, password))
        conn.commit()
        conn.close()
        return redirect("/login")
    except:
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form["email"]
    password = request.form["password"]

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, password FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    conn.close()

    if user and check_password_hash(user[1], password):
        session["user_id"] = user[0]
        return redirect("/")

    return "Invalid email or password."

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    projects = get_projects(session["user_id"])
    return render_template("dashboard.html", projects=projects)

@app.route("/create_project", methods=["POST"])
def create_project():
    if "user_id" not in session:
        return redirect("/login")

    save_project(
        session["user_id"],
        request.form["title"],
        request.form["business_type"],
        request.form["description"]
    )

    return redirect("/dashboard")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "user_id" not in session:
        return redirect("/login")

    file = request.files.get("file")

    if not file:
        return redirect("/dashboard")

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO uploads (user_id, filename, filepath) VALUES (?, ?, ?)",
        (session["user_id"], filename, filepath)
    )
    conn.commit()
    conn.close()

    return redirect("/dashboard")

@app.route("/new_chat")
def new_chat():
    if "user_id" not in session:
        return redirect("/login")

    session.pop("chat_id", None)
    return redirect("/")

@app.route("/switch_chat/<int:chat_id>")
def switch_chat(chat_id):
    if "user_id" not in session:
        return redirect("/login")

    session["chat_id"] = chat_id
    return redirect("/")

@app.route("/messages")
def messages():
    if "user_id" not in session:
        return jsonify([])

    chat_id = session.get("chat_id")

    if not chat_id:
        return jsonify([])

    return jsonify(get_chat_messages(session["user_id"], chat_id))

@app.route("/chat", methods=["POST"])
def chat():

    if "user_id" not in session:
        return jsonify({
            "reply": "Please log in first."
        })

    user_id = session["user_id"]

    data = request.get_json()

    user_message = data.get("message", "")

    chat_id = session.get("chat_id")

    new_chat_created = False

    if not chat_id:

        chat_id = create_chat(
            user_id,
            "New Chat"
        )

        session["chat_id"] = chat_id

        new_chat_created = True

    save_message(
        user_id,
        chat_id,
        "user",
        user_message
    )

    if new_chat_created:

        title_response = client.chat.completions.create(

            model="gpt-4.1-mini",

            messages=[
                {
                    "role": "system",
                    "content": "Create a short chat title. Maximum 5 words. No quotation marks."
                },
                {
                    "role": "user",
                    "content": user_message
                }
            ]

        )

        title = (
            title_response
            .choices[0]
            .message
            .content
            .strip()
        )

        update_chat_title(
            chat_id,
            title
        )

    uploaded_file = get_latest_uploaded_file(user_id)

    file_context = ""

    if uploaded_file:

        if (
            uploaded_file.endswith(".txt")
            or uploaded_file.endswith(".pdf")
            or uploaded_file.endswith(".docx")
        ):

            file_context = (
                extract_file_text(uploaded_file)
            )[:4000]

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    if file_context:

        messages.append({
            "role": "system",
            "content":
                "The user uploaded this file content:\n\n"
                + file_context
        })

    messages.extend(
        get_memory(
            user_id,
            chat_id
        )
    )

    image_extensions = [
        ".png",
        ".jpg",
        ".jpeg",
        ".webp"
    ]

    if (
        uploaded_file
        and any(
            uploaded_file.lower().endswith(ext)
            for ext in image_extensions
        )
    ):

        image_base64 = image_to_base64(
            uploaded_file
        )

        response = client.chat.completions.create(

            model="gpt-4.1-mini",

            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_message
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url":
                                "data:image/jpeg;base64,"
                                + image_base64
                            }
                        }
                    ]
                }
            ]

        )

    else:

        response = client.chat.completions.create(

            model="gpt-4.1-mini",

            messages=messages

        )

    reply = response.choices[0].message.content

    save_message(
        user_id,
        chat_id,
        "assistant",
        reply
    )

    return jsonify({
        "reply": reply
    })

if __name__ == "__main__":
    init_db()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )