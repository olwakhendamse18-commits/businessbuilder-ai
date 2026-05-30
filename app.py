from flask import Flask, request, jsonify, render_template, redirect, session
from openai import OpenAI
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pypdf import PdfReader
from docx import Document

import requests
import sqlite3
import os
import base64
import psycopg2


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


# -----------------------------
# DATABASE HELPERS
# -----------------------------

def using_postgres():
    return os.getenv("DATABASE_URL") is not None


def db():
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        return psycopg2.connect(database_url)

    return sqlite3.connect("business_ai.db")


def sql(query):
    """
    SQLite uses ? placeholders.
    PostgreSQL uses %s placeholders.
    This lets the same app work locally and on Render PostgreSQL.
    """
    if using_postgres():
        return query.replace("?", "%s")

    return query


def init_db():
    conn = db()
    cur = conn.cursor()

    id_type = "SERIAL PRIMARY KEY" if using_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS chats (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat'
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS messages (
            id {id_type},
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS projects (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            business_type TEXT NOT NULL,
            description TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS uploads (
            id {id_type},
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS payments (
            id {id_type},
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            reference TEXT
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS workflow_progress (
            id {id_type},
            user_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed'
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS workflow_answers (
            id {id_type},
            user_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            answer TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS business_plans (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# USER / AUTH / CHAT HELPERS
# -----------------------------

def save_message(user_id, chat_id, role, content):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO messages (user_id, chat_id, role, content)
            VALUES (?, ?, ?, ?)
        """),
        (user_id, chat_id, role, content)
    )

    conn.commit()
    conn.close()


def get_memory(user_id, chat_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT role, content
            FROM messages
            WHERE user_id = ?
            AND chat_id = ?
            ORDER BY id DESC
            LIMIT 10
        """),
        (user_id, chat_id)
    )

    rows = cur.fetchall()
    conn.close()

    rows.reverse()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in rows
    ]


def get_chat_messages(user_id, chat_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT role, content
            FROM messages
            WHERE user_id = ?
            AND chat_id = ?
            ORDER BY id ASC
        """),
        (user_id, chat_id)
    )

    rows = cur.fetchall()
    conn.close()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in rows
    ]


def create_chat(user_id, title="New Chat"):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            """
            INSERT INTO chats (user_id, title)
            VALUES (%s, %s)
            RETURNING id
            """,
            (user_id, title)
        )

        chat_id = cur.fetchone()[0]

    else:
        cur.execute(
            """
            INSERT INTO chats (user_id, title)
            VALUES (?, ?)
            """,
            (user_id, title)
        )

        chat_id = cur.lastrowid

    conn.commit()
    conn.close()

    return chat_id


def update_chat_title(chat_id, title):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            UPDATE chats
            SET title = ?
            WHERE id = ?
        """),
        (title, chat_id)
    )

    conn.commit()
    conn.close()


def get_chats(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title
            FROM chats
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    chats = cur.fetchall()
    conn.close()

    return chats


# -----------------------------
# PROJECT HELPERS
# -----------------------------

def save_project(user_id, title, business_type, description):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO projects (user_id, title, business_type, description)
            VALUES (?, ?, ?, ?)
        """),
        (user_id, title, business_type, description)
    )

    conn.commit()
    conn.close()


def get_projects(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT title, business_type, description
            FROM projects
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    projects = cur.fetchall()
    conn.close()

    return projects


# -----------------------------
# UPLOAD / FILE HELPERS
# -----------------------------

def get_latest_uploaded_file(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT filepath
            FROM uploads
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
        """),
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
        return base64.b64encode(
            image_file.read()
        ).decode("utf-8")


# -----------------------------
# PAYMENT HELPERS
# -----------------------------

def save_payment(user_id, provider, amount, status, reference):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO payments (user_id, provider, amount, status, reference)
            VALUES (?, ?, ?, ?, ?)
        """),
        (user_id, provider, amount, status, reference)
    )

    conn.commit()
    conn.close()


def user_has_paid(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM payments
            WHERE user_id = ?
            AND status = ?
            LIMIT 1
        """),
        (user_id, "success")
    )

    payment = cur.fetchone()
    conn.close()

    return payment is not None


def get_latest_payment(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT provider, amount, status, reference
            FROM payments
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
        """),
        (user_id,)
    )

    payment = cur.fetchone()
    conn.close()

    return payment


# -----------------------------
# WORKFLOW HELPERS
# -----------------------------

WORKFLOW_STEPS = {
    1: {
        "name": "Business Idea",
        "question": "Describe your business idea. What problem does it solve, who is it for, and what makes it valuable?"
    },
    2: {
        "name": "Brand Name",
        "question": "Describe the type of brand name, slogan, personality, and identity you want for your business."
    },
    3: {
        "name": "Target Market",
        "question": "Describe your ideal customer. Include their age, location, interests, problems, buying behavior, and where you can reach them."
    },
    4: {
        "name": "Products / Services",
        "question": "Describe the products or services your business will offer, including packages, features, benefits, and value."
    },
    5: {
        "name": "Pricing",
        "question": "Describe your pricing ideas, package options, target profit margins, discounts, and launch offers."
    },
    6: {
        "name": "Marketing Plan",
        "question": "Describe your marketing goals, social media ideas, launch strategy, customer acquisition channels, and content ideas."
    },
    7: {
        "name": "Shopify Setup Plan",
        "question": "Describe how you want your Shopify store to work, including pages, products, collections, policies, and checkout flow."
    },
    8: {
        "name": "Canva Branding Plan",
        "question": "Describe your visual branding needs, including logo direction, colors, fonts, social templates, banners, and product graphics."
    },
    9: {
        "name": "Launch Checklist",
        "question": "Describe what still needs to be completed before launch, including branding, products, website, payments, marketing, and customer support."
    }
}


def mark_workflow_step_complete(user_id, step_number, step_name):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM workflow_progress
            WHERE user_id = ?
            AND step_number = ?
        """),
        (user_id, step_number)
    )

    existing = cur.fetchone()

    if not existing:
        cur.execute(
            sql("""
                INSERT INTO workflow_progress
                (user_id, step_number, step_name, status)
                VALUES (?, ?, ?, ?)
            """),
            (user_id, step_number, step_name, "completed")
        )

    conn.commit()
    conn.close()


def get_completed_steps(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT step_number
            FROM workflow_progress
            WHERE user_id = ?
            AND status = ?
        """),
        (user_id, "completed")
    )

    rows = cur.fetchall()
    conn.close()

    return [row[0] for row in rows]


def save_workflow_answer(user_id, step_number, step_name, answer):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM workflow_answers
            WHERE user_id = ?
            AND step_number = ?
        """),
        (user_id, step_number)
    )

    existing = cur.fetchone()

    if existing:
        cur.execute(
            sql("""
                UPDATE workflow_answers
                SET answer = ?
                WHERE user_id = ?
                AND step_number = ?
            """),
            (answer, user_id, step_number)
        )
    else:
        cur.execute(
            sql("""
                INSERT INTO workflow_answers
                (user_id, step_number, step_name, answer)
                VALUES (?, ?, ?, ?)
            """),
            (user_id, step_number, step_name, answer)
        )

    conn.commit()
    conn.close()


def get_workflow_answer(user_id, step_number):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT answer
            FROM workflow_answers
            WHERE user_id = ?
            AND step_number = ?
            LIMIT 1
        """),
        (user_id, step_number)
    )

    row = cur.fetchone()
    conn.close()

    if row:
        return row[0]

    return ""


def get_all_workflow_answers(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT step_number, step_name, answer
            FROM workflow_answers
            WHERE user_id = ?
            ORDER BY step_number ASC
        """),
        (user_id,)
    )

    rows = cur.fetchall()
    conn.close()

    return rows

def save_business_plan(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            """
            INSERT INTO business_plans (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, title, content)
        )
        plan_id = cur.fetchone()[0]
    else:
        cur.execute(
            """
            INSERT INTO business_plans (user_id, title, content)
            VALUES (?, ?, ?)
            """,
            (user_id, title, content)
        )
        plan_id = cur.lastrowid

    conn.commit()
    conn.close()

    return plan_id


def get_business_plans(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM business_plans
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    plans = cur.fetchall()
    conn.close()

    return plans


def get_business_plan(user_id, plan_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM business_plans
            WHERE id = ?
            AND user_id = ?
            LIMIT 1
        """),
        (plan_id, user_id)
    )

    plan = cur.fetchone()
    conn.close()

    return plan


# -----------------------------
# PAGE ROUTES
# -----------------------------

@app.route("/")
def home():
    if "user_id" not in session:
        return redirect("/login")

    chats = get_chats(session["user_id"])

    return render_template(
        "index.html",
        chats=chats
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    email = request.form["email"]
    password = generate_password_hash(request.form["password"])

    try:
        conn = db()
        cur = conn.cursor()

        cur.execute(
            sql("""
                INSERT INTO users (email, password)
                VALUES (?, ?)
            """),
            (email, password)
        )

        conn.commit()
        conn.close()

        return redirect("/login")

    except Exception:
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form["email"]
    password = request.form["password"]

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, password
            FROM users
            WHERE email = ?
        """),
        (email,)
    )

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

    user_id = session["user_id"]

    projects = get_projects(user_id)
    paid = user_has_paid(user_id)
    latest_payment = get_latest_payment(user_id)
    business_plans = get_business_plans(user_id)

    return render_template(
        "dashboard.html",
        projects=projects,
        paid=paid,
        latest_payment=latest_payment,
        business_plans=business_plans
    )


@app.route("/business_plan/<int:plan_id>")
def business_plan(plan_id):
    if "user_id" not in session:
        return redirect("/login")

    plan = get_business_plan(
        session["user_id"],
        plan_id
    )

    if not plan:
        return redirect("/dashboard")

    return render_template(
        "business_plan.html",
        plan=plan
    )


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/refund")
def refund():
    return render_template("refund.html")


# -----------------------------
# PROJECT ROUTES
# -----------------------------

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

    if not user_has_paid(session["user_id"]):
        return redirect("/dashboard")

    file = request.files.get("file")

    if not file:
        return redirect("/dashboard")

    filename = secure_filename(file.filename)
    filepath = os.path.join(
        app.config["UPLOAD_FOLDER"],
        filename
    )

    file.save(filepath)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO uploads (user_id, filename, filepath)
            VALUES (?, ?, ?)
        """),
        (session["user_id"], filename, filepath)
    )

    conn.commit()
    conn.close()

    return redirect("/dashboard")


# -----------------------------
# CHAT ROUTES
# -----------------------------

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

    return jsonify(
        get_chat_messages(
            session["user_id"],
            chat_id
        )
    )


# -----------------------------
# PAYMENT ROUTES
# -----------------------------

@app.route("/paystack_checkout")
def paystack_checkout():
    if "user_id" not in session:
        return redirect("/login")

    paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")

    if not paystack_secret:
        return "Paystack secret key is missing."

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT email
            FROM users
            WHERE id = ?
        """),
        (session["user_id"],)
    )

    user = cur.fetchone()
    conn.close()

    if not user:
        return redirect("/login")

    email = user[0]
    amount = 49900

    headers = {
        "Authorization": f"Bearer {paystack_secret}",
        "Content-Type": "application/json"
    }

    data = {
        "email": email,
        "amount": amount,
        "currency": "ZAR",
        "callback_url": request.host_url + "payment_success"
    }

    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers=headers,
        json=data
    )

    result = response.json()

    if result.get("status"):
        return redirect(result["data"]["authorization_url"])

    return "Could not create Paystack checkout: " + str(result)


@app.route("/payment_success")
def payment_success():
    if "user_id" not in session:
        return redirect("/login")

    reference = request.args.get("reference", "manual-success")

    save_payment(
        session["user_id"],
        "paystack",
        49900,
        "success",
        reference
    )

    return redirect("/dashboard")


# -----------------------------
# WORKFLOW ROUTES
# -----------------------------

@app.route("/business_workflow")
def business_workflow():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    completed_steps = get_completed_steps(user_id)
    completed_count = len(completed_steps)
    answer_count = len(get_all_workflow_answers(user_id))
    total_steps = 9
    progress_percent = int((completed_count / total_steps) * 100)

    return render_template(
        "business_workflow.html",
        completed_steps=completed_steps,
        completed_count=completed_count,
        answer_count=answer_count,
        total_steps=total_steps,
        progress_percent=progress_percent
    )


@app.route("/complete_step/<int:step_number>/<step_name>")
def complete_step(step_number, step_name):
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    mark_workflow_step_complete(
        user_id,
        step_number,
        step_name
    )

    return redirect("/business_workflow")


@app.route("/workflow_step/<int:step_number>", methods=["GET", "POST"])
def workflow_step(step_number):
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    step = WORKFLOW_STEPS.get(step_number)

    if not step:
        return redirect("/business_workflow")

    if request.method == "POST":
        answer = request.form["answer"]

        save_workflow_answer(
            user_id,
            step_number,
            step["name"],
            answer
        )

        mark_workflow_step_complete(
            user_id,
            step_number,
            step["name"]
        )

        return redirect("/business_workflow")

    existing_answer = get_workflow_answer(
        user_id,
        step_number
    )

    return render_template(
        "workflow_step.html",
        step_number=step_number,
        step_name=step["name"],
        question=step["question"],
        existing_answer=existing_answer
    )


@app.route("/generate_business_plan")
def generate_business_plan():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_all_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?plan_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create a complete professional business plan using the user's saved workflow answers.

The business plan must include:

1. Executive Summary
2. Business Idea
3. Brand Name and Brand Identity
4. Target Market
5. Products or Services
6. Pricing Strategy
7. Marketing Plan
8. Shopify Setup Plan
9. Canva Branding Plan
10. Launch Checklist
11. Next 30-Day Action Plan

User workflow answers:
{workflow_text}
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    business_plan = response.choices[0].message.content

    plan_id = save_business_plan(
        user_id,
        "Generated Business Plan",
        business_plan
    )

    return redirect(f"/business_plan/{plan_id}")


# -----------------------------
# MAIN AI CHAT ROUTE
# -----------------------------

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

        title = title_response.choices[0].message.content.strip()

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
            file_context = extract_file_text(uploaded_file)[:4000]

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

    if uploaded_file and any(
        uploaded_file.lower().endswith(ext)
        for ext in image_extensions
    ):
        image_base64 = image_to_base64(uploaded_file)

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


# -----------------------------
# START APP
# -----------------------------

init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
