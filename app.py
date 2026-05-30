from flask import Flask, request, jsonify, render_template, redirect, session, send_file
from openai import OpenAI
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
from pypdf import PdfReader
from docx import Document
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

import requests
import sqlite3
import os
import base64
import hashlib
import hmac
import logging
import secrets
import psycopg2
import re
import json
import urllib.parse
from io import BytesIO
from xml.sax.saxutils import escape


load_dotenv()

app = Flask(__name__)
logger = logging.getLogger(__name__)
secret_key = os.getenv("SECRET_KEY")

if not secret_key:
    if os.getenv("DATABASE_URL"):
        raise RuntimeError("SECRET_KEY must be configured in production.")

    secret_key = "businessbuilder-local-development-secret"

app.secret_key = secret_key

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

openai_api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_api_key) if openai_api_key else None

SYSTEM_PROMPT = """
You are BusinessBuilder AI, an AI assistant that helps users create businesses.
Help with business ideas, branding, Shopify planning, Canva ideas, marketing, pricing, and invoices.
Ask useful questions and give step-by-step help.
Do not pretend real Shopify, Canva, or payment tools are connected yet.
"""

SHOPIFY_ADMIN_API_VERSION = "2026-04"
CANVA_AUTHORIZATION_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
CANVA_PROFILE_URL = "https://api.canva.com/rest/v1/users/me/profile"
CANVA_DESIGNS_URL = "https://api.canva.com/rest/v1/designs"
CANVA_SCOPES = os.getenv(
    "CANVA_SCOPES",
    "design:meta:read design:content:write profile:read"
)


# -----------------------------
# VALIDATION / ERROR HELPERS
# -----------------------------

class ExternalServiceError(Exception):
    pass


def render_error(message, status_code=500, back_url="/dashboard"):
    return render_template(
        "error.html",
        message=message,
        back_url=back_url
    ), status_code


def is_valid_email(email):
    return bool(
        email
        and len(email) <= 254
        and re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            email
        )
    )


def safe_openai_chat_completion(**kwargs):
    if not client:
        raise ExternalServiceError(
            "AI generation is temporarily unavailable. Please try again later."
        )

    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        raise ExternalServiceError(
            "AI generation is temporarily unavailable. Please try again later."
        )


def send_email(to, subject, body):
    provider = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    api_key = os.getenv("EMAIL_API_KEY", "").strip()
    from_email = os.getenv("FROM_EMAIL", "").strip()

    if not provider or not api_key or not from_email:
        logger.info("Email notification skipped because email is not configured.")
        return False

    if provider != "resend":
        logger.warning("Email notification skipped because the provider is unsupported.")
        return False

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": from_email,
                "to": [to],
                "subject": subject,
                "text": body
            },
            timeout=10
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("Email notification could not be delivered.")
        return False

    return True


@app.errorhandler(ExternalServiceError)
def handle_external_service_error(error):
    if request.path == "/chat":
        return jsonify({"reply": str(error)}), 503

    return render_error(str(error), 503)


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return error

    if request.path == "/chat":
        return jsonify({
            "reply": "Something went wrong. Please try again."
        }), 500

    return render_error(
        "Something went wrong while processing your request. Please try again.",
        500
    )


# -----------------------------
# DATABASE HELPERS
# -----------------------------

def using_postgres():
    return bool(os.getenv("DATABASE_URL"))


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

    def execute_schema(query):
        cur.execute(sql(query))

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS chats (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT 'New Chat'
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS messages (
            id {id_type},
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS projects (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            business_type TEXT NOT NULL,
            description TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS uploads (
            id {id_type},
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS payments (
            id {id_type},
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            reference TEXT
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS workflow_progress (
            id {id_type},
            user_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed'
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS workflow_answers (
            id {id_type},
            user_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            answer TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS business_plans (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute(sql("""
        DELETE FROM payments
        WHERE reference IS NOT NULL
        AND id NOT IN (
            SELECT MIN(id)
            FROM payments
            WHERE reference IS NOT NULL
            GROUP BY reference
        )
    """))

    cur.execute(sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_reference_unique
        ON payments (reference)
    """))

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS shopify_plans (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS canva_branding_packages (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS canva_design_briefs (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS canva_designs (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            canva_design_id TEXT NOT NULL,
            edit_url TEXT,
            view_url TEXT,
            status TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS build_quotes (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            shopify_plan TEXT NOT NULL,
            canva_plan TEXT NOT NULL,
            estimated_shopify_cost TEXT NOT NULL,
            estimated_canva_cost TEXT NOT NULL,
            service_fee TEXT NOT NULL,
            total_estimate TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS shopify_connections (
            id {id_type},
            user_id INTEGER NOT NULL,
            shop_domain TEXT NOT NULL,
            access_token TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS canva_connections (
            id {id_type},
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            connected_email TEXT,
            access_token TEXT,
            refresh_token TEXT
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS canva_oauth_sessions (
            id {id_type},
            user_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            code_verifier TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS shopify_products (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            shopify_product_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS shopify_collections (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            shopify_collection_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS shopify_pages (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            page_type TEXT NOT NULL,
            content TEXT NOT NULL,
            shopify_page_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    execute_schema(f"""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id {id_type},
            user_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute(sql("""
        CREATE INDEX IF NOT EXISTS idx_usage_logs_user_action_created
        ON usage_logs (user_id, action_type, created_at)
    """))

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
            sql("""
            INSERT INTO chats (user_id, title)
            VALUES (%s, %s)
            RETURNING id
            """),
            (user_id, title)
        )

        chat_id = cur.fetchone()[0]

    else:
        cur.execute(
            sql("""
            INSERT INTO chats (user_id, title)
            VALUES (?, ?)
            """),
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
    if reference and payment_reference_exists(reference):
        return False

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute(
            sql("""
                INSERT INTO payments (user_id, provider, amount, status, reference)
                VALUES (?, ?, ?, ?, ?)
            """),
            (user_id, provider, amount, status, reference)
        )
    except (sqlite3.IntegrityError, psycopg2.IntegrityError):
        conn.rollback()
        conn.close()
        return False

    conn.commit()
    conn.close()

    return True


def payment_reference_exists(reference):
    if not reference:
        return False

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM payments
            WHERE reference = ?
            LIMIT 1
        """),
        (reference,)
    )

    payment = cur.fetchone()
    conn.close()

    return payment is not None


def verify_paystack_transaction(reference):
    paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")

    if not paystack_secret:
        return None, "Paystack secret key is missing."

    headers = {
        "Authorization": f"Bearer {paystack_secret}"
    }

    try:
        response = requests.get(
            "https://api.paystack.co/transaction/verify/"
            + urllib.parse.quote(reference, safe=""),
            headers=headers,
            timeout=20
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        return None, "Could not verify the Paystack transaction."

    transaction = result.get("data") or {}

    if not result.get("status") or transaction.get("status") != "success":
        return None, "Paystack could not confirm a successful payment."

    return transaction, None


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


def get_user_email(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT email
            FROM users
            WHERE id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    user = cur.fetchone()
    conn.close()

    return user[0] if user else None


def get_user_id_by_email(email):
    if not email:
        return None

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM users
            WHERE LOWER(email) = ?
            LIMIT 1
        """),
        (email.strip().lower(),)
    )

    user = cur.fetchone()
    conn.close()

    return user[0] if user else None


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


USAGE_LIMITS = {
    "chat_message": {"limit": 50, "period": "daily"},
    "business_plan": {"limit": 10, "period": "total"},
    "shopify_plan": {"limit": 10, "period": "total"},
    "shopify_product": {"limit": 20, "period": "total"},
    "canva_branding": {"limit": 10, "period": "total"},
    "canva_design_brief": {"limit": 10, "period": "total"},
    "canva_design": {"limit": 20, "period": "total"},
    "launch_package": {"limit": 10, "period": "total"},
    "pdf_export": {"limit": 20, "period": "total"}
}

USAGE_LABELS = {
    "chat_message": "chat messages",
    "business_plan": "business plan generation",
    "shopify_plan": "Shopify setup plan generation",
    "shopify_product": "Shopify product creation",
    "canva_branding": "Canva branding package generation",
    "canva_design_brief": "Canva design brief generation",
    "canva_design": "Canva design draft creation",
    "launch_package": "launch package views",
    "pdf_export": "PDF exports"
}


def log_usage(user_id, action_type):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO usage_logs (user_id, action_type)
            VALUES (?, ?)
        """),
        (user_id, action_type)
    )

    conn.commit()
    conn.close()


def get_usage_count(user_id, action_type, period="daily"):
    conn = db()
    cur = conn.cursor()

    if period == "daily":
        cur.execute(
            sql("""
                SELECT COUNT(*)
                FROM usage_logs
                WHERE user_id = ?
                AND action_type = ?
                AND created_at >= CURRENT_DATE
            """),
            (user_id, action_type)
        )
    elif period == "total":
        cur.execute(
            sql("""
                SELECT COUNT(*)
                FROM usage_logs
                WHERE user_id = ?
                AND action_type = ?
            """),
            (user_id, action_type)
        )
    else:
        conn.close()
        raise ValueError("Usage period must be daily or total.")

    usage_count = cur.fetchone()[0]
    conn.close()

    return usage_count


def usage_limit_reached(user_id, action_type):
    limit_config = USAGE_LIMITS.get(action_type)

    if not limit_config:
        return False

    return get_usage_count(
        user_id,
        action_type,
        limit_config["period"]
    ) >= limit_config["limit"]


def usage_limit_redirect(action_type, destination="/dashboard"):
    return redirect(
        f"{destination}?usage_limit={urllib.parse.quote(action_type)}"
    )


def get_usage_limit_message(action_type):
    limit_config = USAGE_LIMITS.get(action_type)

    if not limit_config:
        return ""

    period_label = " today" if limit_config["period"] == "daily" else ""

    return (
        f"You have reached the {limit_config['limit']}"
        f"{period_label} limit for {USAGE_LABELS[action_type]}. "
        "Your saved work is still available from the dashboard."
    )


def get_usage_summary(user_id):
    counts = {
        action_type: get_usage_count(
            user_id,
            action_type,
            limit_config["period"]
        )
        for action_type, limit_config in USAGE_LIMITS.items()
    }

    return {
        "chat_messages": counts["chat_message"],
        "business_plans": counts["business_plan"],
        "shopify_actions": (
            counts["shopify_plan"]
            + counts["shopify_product"]
        ),
        "shopify_plans": counts["shopify_plan"],
        "shopify_products": counts["shopify_product"],
        "canva_actions": (
            counts["canva_branding"]
            + counts["canva_design_brief"]
            + counts["canva_design"]
        ),
        "canva_branding": counts["canva_branding"],
        "canva_design_briefs": counts["canva_design_brief"],
        "canva_designs": counts["canva_design"],
        "launch_packages": counts["launch_package"],
        "pdf_exports": counts["pdf_export"]
    }


def is_admin_user():
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    user_id = session.get("user_id")

    if not admin_email or not user_id:
        return False

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT email
            FROM users
            WHERE id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    user = cur.fetchone()
    conn.close()

    return bool(
        user
        and user[0]
        and user[0].strip().lower() == admin_email
    )


def get_admin_dashboard_data():
    conn = db()
    cur = conn.cursor()

    def count(query, params=()):
        cur.execute(sql(query), params)
        return cur.fetchone()[0]

    metrics = {
        "total_users": count("SELECT COUNT(*) FROM users"),
        "paid_users": count("""
            SELECT COUNT(DISTINCT user_id)
            FROM payments
            WHERE status = ?
        """, ("success",)),
        "total_payments": count("SELECT COUNT(*) FROM payments"),
        "business_plans": count("SELECT COUNT(*) FROM business_plans"),
        "shopify_connections": count("SELECT COUNT(*) FROM shopify_connections"),
        "canva_connections": count("SELECT COUNT(*) FROM canva_connections"),
        "shopify_products": count("SELECT COUNT(*) FROM shopify_products"),
        "canva_designs": count("SELECT COUNT(*) FROM canva_designs"),
        "total_usage_logs": count("SELECT COUNT(*) FROM usage_logs")
    }

    cur.execute(sql("""
        SELECT id, email
        FROM users
        ORDER BY id DESC
        LIMIT 10
    """))
    recent_users = cur.fetchall()

    cur.execute(sql("""
        SELECT
            payments.id,
            users.email,
            payments.provider,
            payments.amount,
            payments.status,
            payments.reference
        FROM payments
        LEFT JOIN users ON users.id = payments.user_id
        ORDER BY payments.id DESC
        LIMIT 10
    """))
    recent_payments = cur.fetchall()

    cur.execute(sql("""
        SELECT action_type, COUNT(*) AS usage_count
        FROM usage_logs
        GROUP BY action_type
        ORDER BY usage_count DESC, action_type ASC
        LIMIT 10
    """))
    most_used_actions = cur.fetchall()

    conn.close()

    return {
        "metrics": metrics,
        "recent_users": recent_users,
        "recent_payments": recent_payments,
        "most_used_actions": most_used_actions
    }


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


def get_nonempty_workflow_answers(user_id):
    return [
        answer
        for answer in get_all_workflow_answers(user_id)
        if answer[2] and answer[2].strip()
    ]


def save_business_plan(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            sql("""
            INSERT INTO business_plans (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """),
            (user_id, title, content)
        )
        plan_id = cur.fetchone()[0]
    else:
        cur.execute(
            sql("""
            INSERT INTO business_plans (user_id, title, content)
            VALUES (?, ?, ?)
            """),
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


def save_shopify_plan(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            sql("""
            INSERT INTO shopify_plans (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """),
            (user_id, title, content)
        )
        plan_id = cur.fetchone()[0]
    else:
        cur.execute(
            sql("""
            INSERT INTO shopify_plans (user_id, title, content)
            VALUES (?, ?, ?)
            """),
            (user_id, title, content)
        )
        plan_id = cur.lastrowid

    conn.commit()
    conn.close()

    return plan_id


def get_shopify_plans(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM shopify_plans
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    plans = cur.fetchall()
    conn.close()

    return plans


def get_shopify_plan(user_id, plan_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM shopify_plans
            WHERE id = ?
            AND user_id = ?
            LIMIT 1
        """),
        (plan_id, user_id)
    )

    plan = cur.fetchone()
    conn.close()

    return plan


def save_canva_branding_package(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            sql("""
            INSERT INTO canva_branding_packages (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """),
            (user_id, title, content)
        )
        package_id = cur.fetchone()[0]
    else:
        cur.execute(
            sql("""
            INSERT INTO canva_branding_packages (user_id, title, content)
            VALUES (?, ?, ?)
            """),
            (user_id, title, content)
        )
        package_id = cur.lastrowid

    conn.commit()
    conn.close()

    return package_id


def get_canva_branding_packages(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM canva_branding_packages
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    packages = cur.fetchall()
    conn.close()

    return packages


def get_canva_branding_package(user_id, package_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM canva_branding_packages
            WHERE id = ?
            AND user_id = ?
            LIMIT 1
        """),
        (package_id, user_id)
    )

    package = cur.fetchone()
    conn.close()

    return package


def save_canva_design_brief(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            sql("""
            INSERT INTO canva_design_briefs (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """),
            (user_id, title, content)
        )
        brief_id = cur.fetchone()[0]
    else:
        cur.execute(
            sql("""
            INSERT INTO canva_design_briefs (user_id, title, content)
            VALUES (?, ?, ?)
            """),
            (user_id, title, content)
        )
        brief_id = cur.lastrowid

    conn.commit()
    conn.close()

    return brief_id


def get_canva_design_briefs(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM canva_design_briefs
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    briefs = cur.fetchall()
    conn.close()

    return briefs


def get_canva_design_brief(user_id, brief_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM canva_design_briefs
            WHERE id = ?
            AND user_id = ?
            LIMIT 1
        """),
        (brief_id, user_id)
    )

    brief = cur.fetchone()
    conn.close()

    return brief


def get_latest_canva_design_brief(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM canva_design_briefs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
        """),
        (user_id,)
    )

    brief = cur.fetchone()
    conn.close()

    return brief


def save_canva_design(
    user_id,
    title,
    canva_design_id,
    edit_url,
    view_url,
    status
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO canva_designs (
                user_id,
                title,
                canva_design_id,
                edit_url,
                view_url,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """),
        (
            user_id,
            title,
            canva_design_id,
            edit_url,
            view_url,
            status
        )
    )

    conn.commit()
    conn.close()


def get_canva_designs(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT title, canva_design_id, edit_url, view_url, status
            FROM canva_designs
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    designs = cur.fetchall()
    conn.close()

    return designs


def save_build_quote(
    user_id,
    title,
    shopify_plan,
    canva_plan,
    estimated_shopify_cost,
    estimated_canva_cost,
    service_fee,
    total_estimate,
    content
):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            sql("""
            INSERT INTO build_quotes (
                user_id,
                title,
                shopify_plan,
                canva_plan,
                estimated_shopify_cost,
                estimated_canva_cost,
                service_fee,
                total_estimate,
                content
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """),
            (
                user_id,
                title,
                shopify_plan,
                canva_plan,
                estimated_shopify_cost,
                estimated_canva_cost,
                service_fee,
                total_estimate,
                content
            )
        )
        quote_id = cur.fetchone()[0]
    else:
        cur.execute(
            sql("""
            INSERT INTO build_quotes (
                user_id,
                title,
                shopify_plan,
                canva_plan,
                estimated_shopify_cost,
                estimated_canva_cost,
                service_fee,
                total_estimate,
                content
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """),
            (
                user_id,
                title,
                shopify_plan,
                canva_plan,
                estimated_shopify_cost,
                estimated_canva_cost,
                service_fee,
                total_estimate,
                content
            )
        )
        quote_id = cur.lastrowid

    conn.commit()
    conn.close()

    return quote_id


def get_build_quotes(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, title, content
            FROM build_quotes
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    quotes = cur.fetchall()
    conn.close()

    return quotes


def get_build_quote(user_id, quote_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT
                id,
                title,
                shopify_plan,
                canva_plan,
                estimated_shopify_cost,
                estimated_canva_cost,
                service_fee,
                total_estimate,
                content
            FROM build_quotes
            WHERE id = ?
            AND user_id = ?
            LIMIT 1
        """),
        (quote_id, user_id)
    )

    quote = cur.fetchone()
    conn.close()

    return quote


def save_shopify_connection(user_id, shop_domain, access_token, status):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM shopify_connections
            WHERE user_id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    connection = cur.fetchone()

    if connection:
        cur.execute(
            sql("""
                UPDATE shopify_connections
                SET shop_domain = ?,
                    access_token = ?,
                    status = ?
                WHERE user_id = ?
            """),
            (shop_domain, access_token, status, user_id)
        )
    else:
        cur.execute(
            sql("""
                INSERT INTO shopify_connections (
                    user_id,
                    shop_domain,
                    access_token,
                    status
                )
                VALUES (?, ?, ?, ?)
            """),
            (user_id, shop_domain, access_token, status)
        )

    conn.commit()
    conn.close()


def get_shopify_connection(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, shop_domain, access_token, status
            FROM shopify_connections
            WHERE user_id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    connection = cur.fetchone()
    conn.close()

    return connection


def get_canva_connection(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT
                id,
                user_id,
                status,
                connected_email,
                access_token,
                refresh_token
            FROM canva_connections
            WHERE user_id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    connection = cur.fetchone()
    conn.close()

    return connection


def save_canva_connection(
    user_id,
    status,
    connected_email,
    access_token,
    refresh_token
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id
            FROM canva_connections
            WHERE user_id = ?
            LIMIT 1
        """),
        (user_id,)
    )

    connection = cur.fetchone()

    if connection:
        cur.execute(
            sql("""
                UPDATE canva_connections
                SET status = ?,
                    connected_email = ?,
                    access_token = ?,
                    refresh_token = ?
                WHERE user_id = ?
            """),
            (
                status,
                connected_email,
                access_token,
                refresh_token,
                user_id
            )
        )
    else:
        cur.execute(
            sql("""
                INSERT INTO canva_connections (
                    user_id,
                    status,
                    connected_email,
                    access_token,
                    refresh_token
                )
                VALUES (?, ?, ?, ?, ?)
            """),
            (
                user_id,
                status,
                connected_email,
                access_token,
                refresh_token
            )
        )

    conn.commit()
    conn.close()


def create_canva_design(user_id, title, width=1080, height=1080):
    connection = get_canva_connection(user_id)

    if not connection or connection[2] != "connected":
        return None, None, None, None, "Connect Canva before creating a design draft."

    access_token = connection[4]

    if not access_token:
        return None, None, None, None, "Your Canva connection is missing an access token. Reconnect Canva and try again."

    try:
        response = requests.post(
            CANVA_DESIGNS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json={
                "type": "type_and_asset",
                "design_type": {
                    "type": "custom",
                    "width": width,
                    "height": height
                },
                "title": title
            },
            timeout=10
        )
        result = response.json()
    except requests.RequestException:
        return None, None, None, None, "Could not reach Canva. Please try again."
    except ValueError:
        return None, None, None, None, "Canva returned an unexpected response. Please try again."

    design = result.get("design") or result.get("data", {}).get("design") or {}
    canva_design_id = design.get("id") or design.get("design_id")

    if response.status_code != 200 or not canva_design_id:
        return None, None, None, None, "Canva could not create the design draft. Reconnect Canva or try again."

    urls = design.get("urls", {})

    return (
        canva_design_id,
        urls.get("edit_url") or design.get("edit_url", ""),
        urls.get("view_url") or design.get("view_url", ""),
        "draft",
        None
    )


def save_canva_oauth_session(user_id, state, code_verifier):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            DELETE FROM canva_oauth_sessions
            WHERE user_id = ?
        """),
        (user_id,)
    )

    cur.execute(
        sql("""
            INSERT INTO canva_oauth_sessions (
                user_id,
                state,
                code_verifier
            )
            VALUES (?, ?, ?)
        """),
        (user_id, state, code_verifier)
    )

    conn.commit()
    conn.close()


def pop_canva_oauth_verifier(user_id, state):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT code_verifier
            FROM canva_oauth_sessions
            WHERE user_id = ?
            AND state = ?
            LIMIT 1
        """),
        (user_id, state)
    )

    row = cur.fetchone()

    cur.execute(
        sql("""
            DELETE FROM canva_oauth_sessions
            WHERE user_id = ?
        """),
        (user_id,)
    )

    conn.commit()
    conn.close()

    return row[0] if row else None


def normalize_shopify_domain(shop_domain):
    domain = shop_domain.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0]

    if re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        return domain

    return None


def test_shopify_connection(shop_domain, access_token):
    endpoint = (
        f"https://{shop_domain}/admin/api/"
        f"{SHOPIFY_ADMIN_API_VERSION}/graphql.json"
    )
    query = """
    {
        shop {
            name
            myshopifyDomain
        }
    }
    """

    try:
        response = requests.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token
            },
            json={"query": query},
            timeout=10
        )
        result = response.json()
    except requests.RequestException:
        return False, "Could not reach Shopify. Check the store domain and try again."
    except ValueError:
        return False, "Shopify returned an unexpected response. Check your connection details."

    if response.status_code != 200 or result.get("errors"):
        return False, "Shopify connection failed. Check the store domain and Admin API access token."

    shop = result.get("data", {}).get("shop")

    if not shop:
        return False, "Shopify connection failed. The shop details could not be retrieved."

    return True, f"Connected to {shop['name']} ({shop['myshopifyDomain']})."


def save_shopify_product(
    user_id,
    title,
    description,
    shopify_product_id,
    status
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO shopify_products (
                user_id,
                title,
                description,
                shopify_product_id,
                status
            )
            VALUES (?, ?, ?, ?, ?)
        """),
        (user_id, title, description, shopify_product_id, status)
    )

    conn.commit()
    conn.close()


def get_shopify_products(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT title, description, shopify_product_id, status
            FROM shopify_products
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    products = cur.fetchall()
    conn.close()

    return products


def save_shopify_collection(
    user_id,
    title,
    description,
    shopify_collection_id,
    status
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO shopify_collections (
                user_id,
                title,
                description,
                shopify_collection_id,
                status
            )
            VALUES (?, ?, ?, ?, ?)
        """),
        (user_id, title, description, shopify_collection_id, status)
    )

    conn.commit()
    conn.close()


def get_shopify_collections(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT title, description, shopify_collection_id, status
            FROM shopify_collections
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    collections = cur.fetchall()
    conn.close()

    return collections


def save_shopify_page(
    user_id,
    title,
    page_type,
    content,
    shopify_page_id,
    status
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            INSERT INTO shopify_pages (
                user_id,
                title,
                page_type,
                content,
                shopify_page_id,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """),
        (user_id, title, page_type, content, shopify_page_id, status)
    )

    conn.commit()
    conn.close()


def get_shopify_pages(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT title, page_type, content, shopify_page_id, status
            FROM shopify_pages
            WHERE user_id = ?
            ORDER BY id DESC
        """),
        (user_id,)
    )

    pages = cur.fetchall()
    conn.close()

    return pages


def send_shopify_graphql(user_id, query, variables):
    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return None, "Connect and test your Shopify store before building store drafts."

    shop_domain = connection[1]
    access_token = connection[2]
    endpoint = (
        f"https://{shop_domain}/admin/api/"
        f"{SHOPIFY_ADMIN_API_VERSION}/graphql.json"
    )

    try:
        response = requests.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token
            },
            json={
                "query": query,
                "variables": variables
            },
            timeout=10
        )
        result = response.json()
    except requests.RequestException:
        return None, "Could not reach Shopify. Check your connection and try again."
    except ValueError:
        return None, "Shopify returned an unexpected response."

    if response.status_code != 200 or result.get("errors"):
        return None, "Shopify could not create the requested draft item."

    return result, None


def create_shopify_collection(user_id, title, description):
    mutation = """
    mutation CreateCollection($input: CollectionInput!) {
        collectionCreate(input: $input) {
            collection {
                id
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    result, error = send_shopify_graphql(
        user_id,
        mutation,
        {
            "input": {
                "title": title,
                "descriptionHtml": (
                    f"<p>{escape(description).replace(chr(10), '<br>')}</p>"
                )
            }
        }
    )

    if error:
        return None, None, error

    collection_create = result.get("data", {}).get("collectionCreate") or {}

    if collection_create.get("userErrors"):
        return None, None, "Shopify could not create the unpublished collection."

    collection = collection_create.get("collection")

    if not collection:
        return None, None, "Shopify did not return the created collection."

    return collection["id"], "unpublished", None


def create_shopify_page(user_id, title, content):
    mutation = """
    mutation CreatePage($page: PageCreateInput!) {
        pageCreate(page: $page) {
            page {
                id
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    result, error = send_shopify_graphql(
        user_id,
        mutation,
        {
            "page": {
                "title": title,
                "body": f"<p>{escape(content).replace(chr(10), '<br>')}</p>",
                "isPublished": False
            }
        }
    )

    if error:
        return None, None, error

    page_create = result.get("data", {}).get("pageCreate") or {}

    if page_create.get("userErrors"):
        return None, None, "Shopify could not create the unpublished page."

    page = page_create.get("page")

    if not page:
        return None, None, "Shopify did not return the created page."

    return page["id"], "draft", None


def create_shopify_product(
    user_id,
    title,
    description,
    suggested_price=None,
    category=None
):
    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return None, None, "Connect and test your Shopify store before creating products."

    shop_domain = connection[1]
    access_token = connection[2]
    endpoint = (
        f"https://{shop_domain}/admin/api/"
        f"{SHOPIFY_ADMIN_API_VERSION}/graphql.json"
    )
    mutation = """
    mutation CreateProduct($product: ProductCreateInput!) {
        productCreate(product: $product) {
            product {
                id
                status
            }
            userErrors {
                field
                message
            }
        }
    }
    """
    description_lines = [description]

    if suggested_price:
        description_lines.append(f"Suggested price: {suggested_price}")

    if category:
        description_lines.append(f"Collection / category: {category}")

    full_description = "\n\n".join(description_lines)
    product_input = {
        "title": title,
        "descriptionHtml": f"<p>{escape(full_description).replace(chr(10), '<br>')}</p>",
        "vendor": "BusinessBuilder AI",
        "status": "DRAFT"
    }

    if category:
        product_input["productType"] = category

    def send_product_create(input_data):
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": access_token
                },
                json={
                    "query": mutation,
                    "variables": {"product": input_data}
                },
                timeout=10
            )
            return response.status_code, response.json()
        except requests.RequestException:
            return None, None
        except ValueError:
            return None, None

    status_code, result = send_product_create(product_input)

    if not result:
        return None, None, "Could not reach Shopify. Try again after checking your connection."

    graphql_errors = result.get("errors", [])
    product_create = result.get("data", {}).get("productCreate") or {}
    user_errors = product_create.get("userErrors", [])
    all_errors = graphql_errors + user_errors

    if all_errors and any("status" in str(error).lower() for error in all_errors):
        product_input.pop("status", None)
        status_code, result = send_product_create(product_input)

        if not result:
            return None, None, "Could not reach Shopify. Try again after checking your connection."

        graphql_errors = result.get("errors", [])
        product_create = result.get("data", {}).get("productCreate") or {}
        user_errors = product_create.get("userErrors", [])

    if status_code != 200 or graphql_errors or user_errors:
        return None, None, "Shopify could not create the draft product. Check your Admin API permissions."

    product = product_create.get("product")

    if not product:
        return None, None, "Shopify did not return the created draft product."

    return product["id"], product.get("status", "DRAFT"), None


def create_business_plan_pdf(title, content):
    pdf_buffer = BytesIO()
    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BusinessPlanTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#172554"),
        fontSize=22,
        leading=28,
        spaceAfter=8
    )
    brand_style = ParagraphStyle(
        "BusinessPlanBrand",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#2563eb"),
        fontSize=10,
        leading=14,
        spaceAfter=14
    )
    heading_style = ParagraphStyle(
        "BusinessPlanHeading",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#1e3a8a"),
        fontSize=14,
        leading=18,
        spaceBefore=10,
        spaceAfter=5
    )
    body_style = ParagraphStyle(
        "BusinessPlanBody",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#334155"),
        fontSize=10.5,
        leading=15,
        spaceAfter=6
    )

    story = [
        Paragraph("BusinessBuilder AI", brand_style),
        Paragraph(escape(title), title_style),
        Spacer(1, 5 * mm)
    ]

    for line in content.splitlines():
        text = line.strip()

        if not text:
            story.append(Spacer(1, 2.5 * mm))
            continue

        clean_text = escape(text)
        is_heading = (
            text.startswith("#")
            or (
                len(text) <= 90
                and (
                    text.endswith(":")
                    or text[0].isdigit() and "." in text[:4]
                )
            )
        )

        if text.startswith("#"):
            clean_text = escape(text.lstrip("#").strip())

        story.append(
            Paragraph(
                clean_text,
                heading_style if is_heading else body_style
            )
        )

    document.build(story)
    pdf_buffer.seek(0)

    return pdf_buffer


def create_launch_package_pdf(launch_data):
    pdf_buffer = BytesIO()
    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "LaunchPackageTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#172554"),
        fontSize=22,
        leading=28,
        spaceAfter=8
    )
    brand_style = ParagraphStyle(
        "LaunchPackageBrand",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#2563eb"),
        fontSize=10,
        leading=14,
        spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        "LaunchPackageSubtitle",
        parent=styles["BodyText"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#64748b"),
        fontSize=10,
        leading=14,
        spaceAfter=12
    )
    heading_style = ParagraphStyle(
        "LaunchPackageHeading",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#1e3a8a"),
        fontSize=14,
        leading=18,
        spaceBefore=12,
        spaceAfter=5
    )
    subheading_style = ParagraphStyle(
        "LaunchPackageSubheading",
        parent=styles["Heading3"],
        textColor=colors.HexColor("#334155"),
        fontSize=11.5,
        leading=15,
        spaceBefore=7,
        spaceAfter=4
    )
    body_style = ParagraphStyle(
        "LaunchPackageBody",
        parent=styles["BodyText"],
        textColor=colors.HexColor("#334155"),
        fontSize=10,
        leading=14,
        spaceAfter=5
    )
    bullet_style = ParagraphStyle(
        "LaunchPackageBullet",
        parent=body_style,
        leftIndent=10,
        firstLineIndent=-8
    )

    story = [
        Paragraph("BusinessBuilder AI", brand_style),
        Paragraph("Final Launch Package", title_style),
        Paragraph(
            "Your business strategy, draft store assets, branding outputs, and launch priorities.",
            subtitle_style
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            (
                f"Shopify products: {len(launch_data['shopify_products'])} &nbsp; | &nbsp; "
                f"Shopify collections: {len(launch_data['shopify_collections'])} &nbsp; | &nbsp; "
                f"Shopify pages: {len(launch_data['shopify_pages'])} &nbsp; | &nbsp; "
                f"Canva drafts: {len(launch_data['canva_designs'])}"
            ),
            subtitle_style
        )
    ]

    def add_heading(title, style=heading_style):
        story.append(Paragraph(escape(title), style))

    def add_text(content):
        for line in str(content or "").splitlines():
            text = line.strip()

            if not text:
                story.append(Spacer(1, 2 * mm))
                continue

            clean_text = escape(text)
            is_heading = (
                text.startswith("#")
                or (
                    len(text) <= 90
                    and (
                        text.endswith(":")
                        or text[0].isdigit() and "." in text[:4]
                    )
                )
            )

            if text.startswith("#"):
                clean_text = escape(text.lstrip("#").strip())

            story.append(
                Paragraph(
                    clean_text,
                    subheading_style if is_heading else body_style
                )
            )

    def add_bullet(text):
        story.append(Paragraph(f"&bull; {escape(str(text))}", bullet_style))

    def workflow_answer(step_number, fallback):
        return launch_data["answers_by_step"].get(step_number, {}).get(
            "answer",
            fallback
        )

    add_heading("Business Summary")
    add_text(workflow_answer(1, "Complete workflow step 1 to add your business summary."))

    add_heading("Brand Summary")
    add_text(workflow_answer(2, "Complete workflow step 2 to add your brand summary."))

    add_heading("Target Market")
    add_text(workflow_answer(3, "Complete workflow step 3 to define your target market."))

    add_heading("Products / Services")
    add_text(workflow_answer(4, "Complete workflow step 4 to define your products or services."))

    add_heading("Pricing")
    add_text(workflow_answer(5, "Complete workflow step 5 to add your pricing strategy."))

    add_heading("Marketing Plan")
    add_text(workflow_answer(6, "Complete workflow step 6 to add your marketing plan."))

    add_heading("Shopify Assets Created")
    add_text("Review and publish these draft or unpublished Shopify assets manually.")

    add_heading("Products", subheading_style)
    if launch_data["shopify_products"]:
        for product in launch_data["shopify_products"]:
            add_bullet(f"{product[0]} - {product[3]}")
    else:
        add_text("No draft Shopify products created yet.")

    add_heading("Collections", subheading_style)
    if launch_data["shopify_collections"]:
        for collection in launch_data["shopify_collections"]:
            add_bullet(f"{collection[0]} - {collection[3]}")
    else:
        add_text("No unpublished Shopify collections created yet.")

    add_heading("Pages", subheading_style)
    if launch_data["shopify_pages"]:
        for page in launch_data["shopify_pages"]:
            add_bullet(f"{page[0]} - {page[4]}")
    else:
        add_text("No unpublished Shopify pages created yet.")

    if launch_data["latest_shopify_plan"]:
        add_heading("Latest Shopify Setup Plan", subheading_style)
        add_text(launch_data["latest_shopify_plan"][2])

    add_heading("Canva Assets Created")
    if launch_data["canva_designs"]:
        for design in launch_data["canva_designs"]:
            add_bullet(f"{design[0]} - {design[4]}")
    else:
        add_text("No Canva design drafts created yet.")

    if launch_data["latest_canva_branding_package"]:
        add_heading("Latest Canva Branding Package", subheading_style)
        add_text(launch_data["latest_canva_branding_package"][2])

    if launch_data["latest_canva_design_brief"]:
        add_heading("Latest Canva Design Brief", subheading_style)
        add_text(launch_data["latest_canva_design_brief"][2])

    if launch_data["latest_business_plan"]:
        add_heading("Latest Generated Business Plan")
        add_text(launch_data["latest_business_plan"][2])

    if launch_data["latest_build_quote"]:
        add_heading("Latest Store + Branding Quote")
        add_text(launch_data["latest_build_quote"][2])

    add_heading("Launch Checklist")
    add_text(workflow_answer(9, "Complete workflow step 9 to add your launch checklist."))

    add_heading("Next 30-Day Action Plan")
    add_heading("Days 1-7", subheading_style)
    add_text("Review your business plan, validate pricing, and refine your product or service offer.")
    add_heading("Days 8-14", subheading_style)
    add_text("Review Shopify draft products, collections, and pages. Add final images and publish only approved assets.")
    add_heading("Days 15-21", subheading_style)
    add_text("Edit your Canva drafts, prepare social content, and schedule your launch marketing.")
    add_heading("Days 22-30", subheading_style)
    add_text("Run checkout tests, complete the launch checklist, publish approved assets, and monitor your first customer feedback.")

    document.build(story)
    pdf_buffer.seek(0)

    return pdf_buffer


# -----------------------------
# PAGE ROUTES
# -----------------------------

@app.route("/")
def home():
    if "user_id" not in session:
        return redirect("/landing")

    chats = get_chats(session["user_id"])

    return render_template(
        "index.html",
        chats=chats
    )


@app.route("/landing")
def landing():
    if "user_id" in session:
        return redirect("/dashboard")

    return render_template("landing.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    email = request.form.get("email", "").strip().lower()
    raw_password = request.form.get("password", "")

    if not is_valid_email(email):
        return render_template(
            "signup.html",
            message="Enter a valid email address."
        ), 400

    if not raw_password:
        return render_template(
            "signup.html",
            message="Enter a password."
        ), 400

    password = generate_password_hash(raw_password)
    conn = None

    try:
        conn = db()
        cur = conn.cursor()

        cur.execute(
            sql("""
                SELECT id
                FROM users
                WHERE LOWER(email) = ?
                LIMIT 1
            """),
            (email,)
        )

        if cur.fetchone():
            conn.close()

            return render_template(
                "signup.html",
                message="An account with that email already exists."
            ), 400

        cur.execute(
            sql("""
                INSERT INTO users (email, password)
                VALUES (?, ?)
            """),
            (email, password)
        )

        conn.commit()
        cur.execute(
            sql("""
                SELECT id
                FROM users
                WHERE LOWER(email) = ?
                LIMIT 1
            """),
            (email,)
        )
        user = cur.fetchone()
        conn.close()

        if not user:
            raise RuntimeError("Created user could not be loaded.")

        session["user_id"] = user[0]

        send_email(
            email,
            "Welcome to BusinessBuilder AI",
            (
                "Your BusinessBuilder AI workspace is ready. "
                "Start by activating your Starter Package, then complete the "
                "guided workflow to build your plans, store drafts, and brand assets."
            )
        )

        return redirect("/dashboard")

    except (sqlite3.IntegrityError, psycopg2.IntegrityError):
        if conn:
            conn.rollback()
            conn.close()

        return render_template(
            "signup.html",
            message="An account with that email already exists."
        ), 400
    except Exception:
        if conn:
            conn.close()

        raise


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not is_valid_email(email) or not password:
        return render_template(
            "login.html",
            message="Enter a valid email address and password."
        ), 400

    conn = db()
    cur = conn.cursor()

    cur.execute(
        sql("""
            SELECT id, password
            FROM users
            WHERE LOWER(email) = ?
        """),
        (email,)
    )

    user = cur.fetchone()
    conn.close()

    if user and check_password_hash(user[1], password):
        session["user_id"] = user[0]
        return redirect("/")

    return render_template(
        "login.html",
        message="Invalid email or password."
    ), 401


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
    shopify_plans = get_shopify_plans(user_id)
    canva_branding_packages = get_canva_branding_packages(user_id)
    canva_design_briefs = get_canva_design_briefs(user_id)
    canva_designs = get_canva_designs(user_id)
    build_quotes = get_build_quotes(user_id)
    shopify_connection = get_shopify_connection(user_id)
    canva_connection = get_canva_connection(user_id)
    shopify_products = get_shopify_products(user_id)
    shopify_collections = get_shopify_collections(user_id)
    shopify_pages = get_shopify_pages(user_id)

    shopify_connection_summary = None

    if shopify_connection:
        shopify_connection_summary = {
            "shop_domain": shopify_connection[1],
            "status": shopify_connection[3]
        }

    canva_connection_summary = None

    if canva_connection:
        canva_connection_summary = {
            "status": canva_connection[2]
        }

    return render_template(
        "dashboard.html",
        projects=projects,
        paid=paid,
        latest_payment=latest_payment,
        business_plans=business_plans,
        shopify_plans=shopify_plans,
        canva_branding_packages=canva_branding_packages,
        canva_design_briefs=canva_design_briefs,
        canva_designs=canva_designs,
        build_quotes=build_quotes,
        shopify_connection=shopify_connection_summary,
        canva_connection=canva_connection_summary,
        shopify_products=shopify_products,
        shopify_collections=shopify_collections,
        shopify_pages=shopify_pages,
        usage_summary=get_usage_summary(user_id),
        usage_limit_message=get_usage_limit_message(
            request.args.get("usage_limit")
        ),
        is_admin=is_admin_user()
    )


@app.route("/admin")
def admin():
    if "user_id" not in session:
        return redirect("/login")

    if not is_admin_user():
        return redirect("/dashboard")

    return render_template(
        "admin.html",
        **get_admin_dashboard_data()
    )


@app.route("/build_center")
def build_center():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    paid = user_has_paid(user_id)

    if not paid:
        return redirect("/dashboard")

    completed_steps = get_completed_steps(user_id)
    completed_count = len(completed_steps)
    total_steps = len(WORKFLOW_STEPS)
    workflow_answers = get_all_workflow_answers(user_id)
    business_plans = get_business_plans(user_id)
    shopify_plans = get_shopify_plans(user_id)
    shopify_products = get_shopify_products(user_id)
    shopify_collections = get_shopify_collections(user_id)
    shopify_pages = get_shopify_pages(user_id)
    canva_branding_packages = get_canva_branding_packages(user_id)
    canva_design_briefs = get_canva_design_briefs(user_id)
    canva_designs = get_canva_designs(user_id)
    build_quotes = get_build_quotes(user_id)
    latest_payment = get_latest_payment(user_id)
    shopify_connection = get_shopify_connection(user_id)
    canva_connection = get_canva_connection(user_id)

    def status_for(completed, started=False):
        if completed:
            return "Completed"

        if started:
            return "In Progress"

        return "Not Started"

    workflow_started = bool(completed_steps or workflow_answers)
    shopify_connected = bool(
        shopify_connection
        and shopify_connection[3] == "connected"
    )
    canva_connected = bool(
        canva_connection
        and canva_connection[2] == "connected"
    )
    latest_canva_design_url = ""

    if canva_designs:
        latest_canva_design_url = canva_designs[0][2] or canva_designs[0][3]

    build_items = [
        {
            "title": "Starter Package Payment",
            "status": status_for(paid),
            "description": "Your verified payment unlocks the complete business-building workspace.",
            "url": "/dashboard",
            "action": "View Payment Status",
            "count": "Verified" if latest_payment else "Active"
        },
        {
            "title": "Business Workflow",
            "status": status_for(completed_count == total_steps, workflow_started),
            "description": f"{completed_count} of {total_steps} guided setup steps completed.",
            "url": "/business_workflow",
            "action": "Open Workflow",
            "count": f"{completed_count} / {total_steps} steps"
        },
        {
            "title": "Business Plan",
            "status": status_for(business_plans, workflow_started),
            "description": "Generate and review your complete saved business strategy.",
            "url": (
                f"/business_plan/{business_plans[0][0]}"
                if business_plans
                else "/generate_business_plan"
            ),
            "action": "View Plan" if business_plans else "Generate Plan",
            "count": f"{len(business_plans)} saved"
        },
        {
            "title": "Shopify Setup Plan",
            "status": status_for(shopify_plans, 7 in completed_steps),
            "description": "Plan your store pages, products, collections, policies, and launch.",
            "url": (
                f"/shopify_plan/{shopify_plans[0][0]}"
                if shopify_plans
                else "/generate_shopify_plan"
            ),
            "action": "View Shopify Plan" if shopify_plans else "Generate Shopify Plan",
            "count": f"{len(shopify_plans)} saved"
        },
        {
            "title": "Shopify Connection",
            "status": status_for(shopify_connected),
            "description": (
                "Your Shopify Admin API connection is ready for draft product creation."
                if shopify_connected
                else "Connect and test Shopify before creating draft store products."
            ),
            "url": "/shopify_settings",
            "action": "Manage Shopify" if shopify_connected else "Connect Shopify"
        },
        {
            "title": "Shopify Products",
            "status": status_for(shopify_products, bool(shopify_plans)),
            "description": f"{len(shopify_products)} draft Shopify products created.",
            "url": "/business_workflow",
            "action": "Open Product Tools",
            "count": f"{len(shopify_products)} created"
        },
        {
            "title": "Shopify Store Draft",
            "status": status_for(
                shopify_collections and shopify_pages,
                bool(shopify_products or shopify_collections or shopify_pages)
            ),
            "description": "Create draft products, unpublished collections, and unpublished store pages in one guided build.",
            "url": "/build_shopify_store_draft",
            "action": "Build Shopify Store Draft",
            "secondary_url": (
                "/shopify_build_summary"
                if shopify_collections or shopify_pages
                else ""
            ),
            "secondary_action": "View Summary",
            "count": (
                f"{len(shopify_collections)} collections / "
                f"{len(shopify_pages)} pages"
            )
        },
        {
            "title": "Canva Branding Package",
            "status": status_for(canva_branding_packages, 8 in completed_steps),
            "description": "Create your logo, color, typography, and visual asset direction.",
            "url": (
                f"/canva_branding/{canva_branding_packages[0][0]}"
                if canva_branding_packages
                else "/generate_canva_branding"
            ),
            "action": "View Branding Package" if canva_branding_packages else "Generate Branding Package",
            "count": f"{len(canva_branding_packages)} saved"
        },
        {
            "title": "Canva Design Briefs",
            "status": status_for(canva_design_briefs, bool(canva_branding_packages)),
            "description": "Turn your brand direction into practical Canva creation instructions.",
            "url": (
                f"/canva_design_brief/{canva_design_briefs[0][0]}"
                if canva_design_briefs
                else "/generate_canva_design_brief"
            ),
            "action": "View Design Brief" if canva_design_briefs else "Generate Design Brief",
            "count": f"{len(canva_design_briefs)} saved"
        },
        {
            "title": "Canva Connection",
            "status": status_for(canva_connected),
            "description": (
                "Your Canva OAuth connection is ready for editable design drafts."
                if canva_connected
                else "Connect Canva before creating editable design drafts."
            ),
            "url": "/canva_settings",
            "action": "Manage Canva" if canva_connected else "Connect Canva"
        },
        {
            "title": "Canva Design Drafts",
            "status": status_for(canva_designs, bool(canva_design_briefs)),
            "description": f"{len(canva_designs)} editable Canva design drafts created.",
            "url": "/create_canva_designs",
            "action": "Create Canva Design Drafts",
            "secondary_url": latest_canva_design_url,
            "secondary_action": "Open Latest Canva Draft",
            "secondary_external": bool(latest_canva_design_url),
            "count": f"{len(canva_designs)} created"
        },
        {
            "title": "Store + Branding Quote",
            "status": status_for(build_quotes, workflow_started),
            "description": "Review your saved informational store and branding estimate.",
            "url": (
                f"/build_quote/{build_quotes[0][0]}"
                if build_quotes
                else "/generate_build_quote"
            ),
            "action": "View Quote" if build_quotes else "Generate Quote",
            "count": f"{len(build_quotes)} saved"
        },
        {
            "title": "Launch Checklist",
            "status": status_for(9 in completed_steps, workflow_started),
            "description": "Complete your final pre-launch checks for branding, store, marketing, and support.",
            "url": "/workflow_step/9",
            "action": "Open Launch Checklist"
        }
    ]

    next_recommended_action = next(
        item
        for item in build_items
        if item["status"] != "Completed"
    ) if any(
        item["status"] != "Completed"
        for item in build_items
    ) else {
        "title": "Review Your Build Center",
        "description": "Your core business build is complete. Review your saved outputs and prepare for launch.",
        "url": "/build_center",
        "action": "Review Build Center"
    }

    return render_template(
        "build_center.html",
        build_items=build_items,
        completed_count=completed_count,
        total_steps=total_steps,
        next_recommended_action=next_recommended_action
    )


def get_launch_package_data(user_id):
    workflow_answers = get_all_workflow_answers(user_id)
    answers_by_step = {
        step_number: {
            "name": step_name,
            "answer": answer
        }
        for step_number, step_name, answer in workflow_answers
    }
    business_plans = get_business_plans(user_id)
    shopify_plans = get_shopify_plans(user_id)
    shopify_products = get_shopify_products(user_id)
    shopify_collections = get_shopify_collections(user_id)
    shopify_pages = get_shopify_pages(user_id)
    canva_branding_packages = get_canva_branding_packages(user_id)
    canva_design_briefs = get_canva_design_briefs(user_id)
    canva_designs = get_canva_designs(user_id)
    build_quotes = get_build_quotes(user_id)

    return {
        "answers_by_step": answers_by_step,
        "latest_business_plan": business_plans[0] if business_plans else None,
        "latest_shopify_plan": shopify_plans[0] if shopify_plans else None,
        "shopify_products": shopify_products,
        "shopify_collections": shopify_collections,
        "shopify_pages": shopify_pages,
        "latest_canva_branding_package": (
            canva_branding_packages[0]
            if canva_branding_packages
            else None
        ),
        "latest_canva_design_brief": (
            canva_design_briefs[0]
            if canva_design_briefs
            else None
        ),
        "canva_designs": canva_designs,
        "latest_build_quote": build_quotes[0] if build_quotes else None
    }


def send_launch_package_email_once(user_id):
    if session.get("launch_package_email_sent"):
        return

    email = get_user_email(user_id)

    if email:
        send_email(
            email,
            "Your BusinessBuilder AI launch package is ready",
            (
                "Your final launch package is ready to review. "
                "Open BusinessBuilder AI to check your strategy, draft assets, "
                "launch checklist, and PDF export."
            )
        )

    session["launch_package_email_sent"] = True


@app.route("/launch_package")
def launch_package():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "launch_package"):
        return usage_limit_redirect("launch_package")

    send_launch_package_email_once(user_id)

    response = render_template(
        "launch_package.html",
        **get_launch_package_data(user_id)
    )

    log_usage(user_id, "launch_package")

    return response


@app.route("/download_launch_package")
def download_launch_package():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "pdf_export"):
        return usage_limit_redirect("pdf_export")

    send_launch_package_email_once(user_id)

    response = send_file(
        create_launch_package_pdf(get_launch_package_data(user_id)),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="businessbuilder-ai-launch-package.pdf"
    )

    log_usage(user_id, "pdf_export")

    return response


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


@app.route("/download_business_plan/<int:plan_id>")
def download_business_plan(plan_id):
    if "user_id" not in session:
        return redirect("/login")

    plan = get_business_plan(
        session["user_id"],
        plan_id
    )

    if not plan:
        return redirect("/dashboard")

    if usage_limit_reached(session["user_id"], "pdf_export"):
        return usage_limit_redirect("pdf_export")

    filename = re.sub(r"[_-]+", "-", secure_filename(plan[1])).strip("-")
    filename = filename or "business-plan"

    response = send_file(
        create_business_plan_pdf(plan[1], plan[2]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{filename}.pdf"
    )

    log_usage(session["user_id"], "pdf_export")

    return response


@app.route("/shopify_plan/<int:plan_id>")
def shopify_plan(plan_id):
    if "user_id" not in session:
        return redirect("/login")

    plan = get_shopify_plan(
        session["user_id"],
        plan_id
    )

    if not plan:
        return redirect("/dashboard")

    return render_template(
        "shopify_plan.html",
        plan=plan
    )


@app.route("/canva_branding/<int:package_id>")
def canva_branding(package_id):
    if "user_id" not in session:
        return redirect("/login")

    package = get_canva_branding_package(
        session["user_id"],
        package_id
    )

    if not package:
        return redirect("/dashboard")

    return render_template(
        "canva_branding.html",
        package=package
    )


@app.route("/canva_design_brief/<int:brief_id>")
def canva_design_brief(brief_id):
    if "user_id" not in session:
        return redirect("/login")

    brief = get_canva_design_brief(
        session["user_id"],
        brief_id
    )

    if not brief:
        return redirect("/dashboard")

    return render_template(
        "canva_design_brief.html",
        brief=brief
    )


@app.route("/build_quote/<int:quote_id>")
def build_quote(quote_id):
    if "user_id" not in session:
        return redirect("/login")

    quote = get_build_quote(
        session["user_id"],
        quote_id
    )

    if not quote:
        return redirect("/dashboard")

    return render_template(
        "build_quote.html",
        quote=quote
    )


@app.route("/shopify_settings", methods=["GET", "POST"])
def shopify_settings():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    connection = get_shopify_connection(user_id)
    connection_summary = None
    message = None

    if connection:
        connection_summary = {
            "shop_domain": connection[1],
            "status": connection[3]
        }

    if request.method == "POST":
        shop_domain = normalize_shopify_domain(
            request.form.get("shop_domain", "")
        )
        access_token = request.form.get("access_token", "").strip()

        if not shop_domain:
            message = "Enter a valid Shopify domain such as your-store.myshopify.com."
        elif not access_token:
            message = "Enter an Admin API access token to save and test the connection."
        else:
            connected, message = test_shopify_connection(
                shop_domain,
                access_token
            )
            status = "connected" if connected else "not_connected"

            save_shopify_connection(
                user_id,
                shop_domain,
                access_token,
                status
            )

            connection_summary = {
                "shop_domain": shop_domain,
                "status": status
            }

    return render_template(
        "shopify_settings.html",
        connection=connection_summary,
        message=message
    )


@app.route("/shopify_build_summary")
def shopify_build_summary():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    return render_template(
        "shopify_build_summary.html",
        shopify_products=get_shopify_products(user_id),
        shopify_collections=get_shopify_collections(user_id),
        shopify_pages=get_shopify_pages(user_id)
    )


@app.route("/canva_settings")
def canva_settings():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    connection = get_canva_connection(user_id)
    message = {
        "connected": "Canva connected successfully.",
        "configuration": "Canva OAuth is not configured yet. Add the required environment variables.",
        "state": "Canva connection could not be verified. Please start the connection again.",
        "denied": "Canva authorization was not completed.",
        "token": "Canva could not complete the token exchange. Please try again."
    }.get(request.args.get("canva_oauth"))

    connection_summary = None

    if connection:
        connection_summary = {
            "status": connection[2]
        }

    return render_template(
        "canva_settings.html",
        connection=connection_summary,
        message=message
    )


@app.route("/connect_canva")
def connect_canva():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    client_id = os.getenv("CANVA_CLIENT_ID")
    redirect_uri = os.getenv("CANVA_REDIRECT_URI")

    if not client_id or not redirect_uri:
        return redirect("/canva_settings?canva_oauth=configuration")

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(32)

    session["canva_oauth_state"] = state
    save_canva_oauth_session(
        user_id,
        state,
        code_verifier
    )

    authorization_url = CANVA_AUTHORIZATION_URL + "?" + urllib.parse.urlencode({
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": CANVA_SCOPES,
        "response_type": "code",
        "client_id": client_id,
        "state": state,
        "redirect_uri": redirect_uri
    })

    return redirect(authorization_url)


@app.route("/canva_callback")
def canva_callback():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    expected_state = session.pop("canva_oauth_state", None)
    received_state = request.args.get("state", "")

    if (
        not expected_state
        or not received_state
        or not secrets.compare_digest(expected_state, received_state)
    ):
        return redirect("/canva_settings?canva_oauth=state")

    code_verifier = pop_canva_oauth_verifier(
        user_id,
        received_state
    )

    if not code_verifier:
        return redirect("/canva_settings?canva_oauth=state")

    if request.args.get("error"):
        return redirect("/canva_settings?canva_oauth=denied")

    code = request.args.get("code")
    client_id = os.getenv("CANVA_CLIENT_ID")
    client_secret = os.getenv("CANVA_CLIENT_SECRET")
    redirect_uri = os.getenv("CANVA_REDIRECT_URI")

    if not code or not client_id or not client_secret or not redirect_uri:
        return redirect("/canva_settings?canva_oauth=configuration")

    try:
        response = requests.post(
            CANVA_TOKEN_URL,
            auth=(client_id, client_secret),
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "code": code,
                "redirect_uri": redirect_uri
            },
            timeout=10
        )
        token_data = response.json()
    except requests.RequestException:
        return redirect("/canva_settings?canva_oauth=token")
    except ValueError:
        return redirect("/canva_settings?canva_oauth=token")

    access_token = token_data.get("access_token")

    if response.status_code != 200 or not access_token:
        return redirect("/canva_settings?canva_oauth=token")

    refresh_token = token_data.get("refresh_token", "")
    connected_email = "Connected Canva User"

    try:
        profile_response = requests.get(
            CANVA_PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=10
        )
        profile_data = profile_response.json()

        if profile_response.status_code == 200:
            connected_email = (
                profile_data.get("profile", {}).get("display_name")
                or connected_email
            )
    except (requests.RequestException, ValueError):
        pass

    save_canva_connection(
        user_id,
        "connected",
        connected_email,
        access_token,
        refresh_token
    )

    return redirect("/canva_settings?canva_oauth=connected")


@app.route("/health_check")
def health_check():
    if "user_id" not in session:
        return jsonify({"error": "Authentication required."}), 401

    database_connected = False
    conn = None

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(sql("SELECT 1"))
        database_connected = cur.fetchone() is not None
    except (sqlite3.Error, psycopg2.Error):
        database_connected = False
    finally:
        if conn:
            conn.close()

    return jsonify({
        "database_connected": database_connected,
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "paystack_configured": bool(os.getenv("PAYSTACK_SECRET_KEY")),
        "database_type": "postgres" if using_postgres() else "sqlite"
    })


@app.route("/launch_readiness")
def launch_readiness():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    database_connected = False
    conn = None

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(sql("SELECT 1"))
        database_connected = cur.fetchone() is not None
    except (sqlite3.Error, psycopg2.Error):
        database_connected = False
    finally:
        if conn:
            conn.close()

    shopify_connection = get_shopify_connection(user_id)
    canva_connection = get_canva_connection(user_id)
    shopify_connected = bool(
        shopify_connection
        and shopify_connection[3] == "connected"
    )
    canva_connected = bool(
        canva_connection
        and canva_connection[2] == "connected"
    )
    paystack_configured = bool(os.getenv("PAYSTACK_SECRET_KEY"))
    openai_configured = bool(os.getenv("OPENAI_API_KEY"))
    email_configured = bool(
        os.getenv("EMAIL_PROVIDER", "").strip().lower() == "resend"
        and os.getenv("EMAIL_API_KEY")
        and os.getenv("FROM_EMAIL")
    )
    checks = [
        {
            "title": "Database",
            "ready": database_connected,
            "description": (
                "Database connection is available."
                if database_connected
                else "Database connection needs attention."
            )
        },
        {
            "title": "Paystack",
            "ready": paystack_configured,
            "description": (
                "Payment verification environment variable is configured."
                if paystack_configured
                else "Configure Paystack before accepting live payments."
            )
        },
        {
            "title": "OpenAI",
            "ready": openai_configured,
            "description": (
                "AI generation environment variable is configured."
                if openai_configured
                else "Configure OpenAI before using AI generation tools."
            )
        },
        {
            "title": "Shopify Store",
            "ready": shopify_connected,
            "description": (
                "Your Shopify store connection is ready."
                if shopify_connected
                else "Connect and test Shopify before building store drafts."
            )
        },
        {
            "title": "Canva Account",
            "ready": canva_connected,
            "description": (
                "Your Canva connection is ready."
                if canva_connected
                else "Connect Canva before creating editable design drafts."
            )
        },
        {
            "title": "Policy Pages",
            "ready": True,
            "description": "Terms, Privacy, and Refund Policy pages are available."
        },
        {
            "title": "Email Notifications",
            "ready": email_configured,
            "description": (
                "Optional email notifications are configured."
                if email_configured
                else "Optional email notifications are not configured yet."
            )
        }
    ]

    return render_template(
        "launch_readiness.html",
        checks=checks,
        database_type="postgres" if using_postgres() else "sqlite"
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
        return render_error(
            "Payment checkout is temporarily unavailable. Please try again later.",
            503
        )

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

    try:
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers=headers,
            json=data,
            timeout=20
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        return render_error(
            "Payment checkout could not be started. Please try again.",
            503
        )

    authorization_url = (result.get("data") or {}).get("authorization_url")

    if not result.get("status") or not authorization_url:
        return render_error(
            "Payment checkout could not be started. Please try again.",
            503
        )

    return redirect(authorization_url)


@app.route("/payment_success")
def payment_success():
    if "user_id" not in session:
        return redirect("/login")

    reference = request.args.get("reference")

    if not reference:
        return render_error(
            "The payment reference is missing. Please start checkout again.",
            400
        )

    if payment_reference_exists(reference):
        return redirect("/dashboard")

    transaction, error = verify_paystack_transaction(reference)

    if error:
        return render_error(
            "Payment verification could not be completed. Please try again.",
            400
        )

    save_payment(
        session["user_id"],
        "paystack",
        transaction.get("amount", 49900),
        "success",
        reference
    )

    email = get_user_email(session["user_id"])

    if email:
        send_email(
            email,
            "Your BusinessBuilder AI payment is confirmed",
            (
                "Your Paystack payment has been verified and your BusinessBuilder AI "
                "Starter Package is active. Open your dashboard to complete the guided workflow."
            )
        )

    return redirect("/dashboard")


@app.route("/paystack_webhook", methods=["POST"])
def paystack_webhook():
    paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")

    if not paystack_secret:
        return "", 503

    raw_body = request.get_data(cache=False)
    received_signature = request.headers.get("X-Paystack-Signature", "")
    expected_signature = hmac.new(
        paystack_secret.encode("utf-8"),
        raw_body,
        hashlib.sha512
    ).hexdigest()

    # Verify the signature before parsing JSON so forged requests cannot
    # mark an account as paid or create fake payment records.
    if (
        not received_signature
        or not hmac.compare_digest(expected_signature, received_signature)
    ):
        return "", 400

    try:
        event = json.loads(raw_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", 400

    if not isinstance(event, dict):
        return "", 400

    if event.get("event") != "charge.success":
        return "", 200

    transaction = event.get("data")

    if not isinstance(transaction, dict):
        return "", 200

    customer = transaction.get("customer")

    if not isinstance(customer, dict):
        return "", 200

    reference = transaction.get("reference")
    amount = transaction.get("amount")
    status = transaction.get("status")
    customer_email = customer.get("email")

    if (
        not isinstance(reference, str)
        or not reference.strip()
        or not isinstance(amount, (int, float))
        or isinstance(amount, bool)
        or status != "success"
        or not isinstance(customer_email, str)
        or not customer_email.strip()
    ):
        return "", 200

    reference = reference.strip()
    user_id = get_user_id_by_email(customer_email)

    if not user_id or payment_reference_exists(reference):
        return "", 200

    save_payment(
        user_id,
        "paystack",
        amount,
        "success",
        reference
    )

    return "", 200


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
        answer = request.form.get("answer", "").strip()

        if not answer:
            return render_template(
                "workflow_step.html",
                step_number=step_number,
                step_name=step["name"],
                question=step["question"],
                existing_answer="",
                message="Enter an answer before saving this workflow step."
            ), 400

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

    if usage_limit_reached(user_id, "business_plan"):
        return usage_limit_redirect("business_plan", "/business_workflow")

    answers = get_nonempty_workflow_answers(user_id)

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

    response = safe_openai_chat_completion(
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

    log_usage(user_id, "business_plan")

    email = get_user_email(user_id)

    if email:
        send_email(
            email,
            "Your BusinessBuilder AI business plan is ready",
            (
                "Your generated business plan has been saved. "
                "Open your BusinessBuilder AI dashboard to review it or download the PDF."
            )
        )

    return redirect(f"/business_plan/{plan_id}")


@app.route("/generate_shopify_plan")
def generate_shopify_plan():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "shopify_plan"):
        return usage_limit_redirect("shopify_plan", "/business_workflow")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?shopify_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create a complete professional Shopify store setup plan using the user's saved workflow answers.
Provide practical copy and recommendations that the user can apply while building their Shopify store.

The Shopify setup plan must include:

1. Store Name Suggestion
2. Shopify Homepage Structure
3. Product Titles
4. Product Descriptions
5. Collections
6. Navigation Menu
7. About Page Copy
8. Contact Page Copy
9. Shipping Policy Draft
10. Refund Policy Draft
11. Store Launch Checklist

User workflow answers:
{workflow_text}
"""

    response = safe_openai_chat_completion(
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

    shopify_plan_content = response.choices[0].message.content

    plan_id = save_shopify_plan(
        user_id,
        "Generated Shopify Setup Plan",
        shopify_plan_content
    )

    log_usage(user_id, "shopify_plan")

    return redirect(f"/shopify_plan/{plan_id}")


@app.route("/generate_canva_branding")
def generate_canva_branding():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "canva_branding"):
        return usage_limit_redirect("canva_branding", "/business_workflow")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?canva_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create a complete professional Canva-ready branding package using the user's saved workflow answers.
Provide practical design direction and instructions the user can apply manually inside Canva.
Do not claim that Canva is connected and do not claim that designs were created automatically.

The Canva-ready branding package must include:

1. Logo Concept
2. Brand Color Palette
3. Font Direction
4. Brand Personality
5. Instagram Post Template Ideas
6. Instagram Story Template Ideas
7. TikTok/Reels Cover Ideas
8. Business Card Design Idea
9. Shopify Homepage Banner Concept
10. Product Mockup Ideas
11. Canva Search Keywords
12. Step-by-Step Canva Creation Checklist

User workflow answers:
{workflow_text}
"""

    response = safe_openai_chat_completion(
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

    package_content = response.choices[0].message.content

    package_id = save_canva_branding_package(
        user_id,
        "Generated Canva Branding Package",
        package_content
    )

    log_usage(user_id, "canva_branding")

    return redirect(f"/canva_branding/{package_id}")


@app.route("/generate_canva_design_brief")
def generate_canva_design_brief():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "canva_design_brief"):
        return usage_limit_redirect("canva_design_brief", "/business_workflow")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?canva_brief_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    branding_packages = get_canva_branding_packages(user_id)
    branding_package_text = (
        branding_packages[0][2]
        if branding_packages
        else "No saved Canva branding package is available yet."
    )

    prompt = f"""
Create a complete professional set of Canva design briefs using the user's saved workflow answers
and the latest saved Canva branding package when available.
Provide detailed, practical instructions the user can apply manually inside Canva.
Do not claim that Canva designs were created automatically and do not call any Canva API.

The Canva design brief document must include:

1. Logo Design Brief
2. Shopify Homepage Banner Design Brief
3. Instagram Post Template Brief
4. Instagram Story Template Brief
5. TikTok/Reels Cover Brief
6. Business Card Brief
7. Product Mockup Brief
8. Brand Color Usage Instructions
9. Font Usage Instructions
10. Canva Keywords to Search
11. Step-by-Step Canva Creation Checklist

Latest saved Canva branding package:
{branding_package_text}

User workflow answers:
{workflow_text}
"""

    response = safe_openai_chat_completion(
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

    brief_content = response.choices[0].message.content

    brief_id = save_canva_design_brief(
        user_id,
        "Generated Canva Design Brief",
        brief_content
    )

    log_usage(user_id, "canva_design_brief")

    return redirect(f"/canva_design_brief/{brief_id}")


@app.route("/create_canva_design")
def create_canva_design_from_brief():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "canva_design"):
        return usage_limit_redirect("canva_design", "/business_workflow")

    connection = get_canva_connection(user_id)

    if not connection or connection[2] != "connected":
        return redirect("/business_workflow?canva_design_error=connection")

    brief = get_latest_canva_design_brief(user_id)

    if not brief:
        return redirect("/business_workflow?canva_design_error=no_brief")

    title = f"{brief[1]} Draft"[:255]
    canva_design_id, edit_url, view_url, status, error = create_canva_design(
        user_id,
        title
    )

    if error:
        return redirect("/business_workflow?canva_design_error=create_failed")

    save_canva_design(
        user_id,
        title,
        canva_design_id,
        edit_url,
        view_url,
        status
    )
    log_usage(user_id, "canva_design")

    return redirect("/dashboard?canva_design=created")


@app.route("/create_canva_designs")
def create_canva_designs_from_brief():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "canva_design"):
        return usage_limit_redirect("canva_design", "/business_workflow")

    connection = get_canva_connection(user_id)

    if not connection or connection[2] != "connected":
        return redirect("/business_workflow?canva_design_error=connection")

    brief = get_latest_canva_design_brief(user_id)

    if not brief:
        return redirect("/business_workflow?canva_design_error=no_brief")

    base_title = brief[1][:180]
    draft_specs = [
        ("Logo Concept", 1080, 1080),
        ("Shopify Homepage Banner", 1800, 700),
        ("Instagram Post Template", 1080, 1080),
        ("Instagram Story Template", 1080, 1920),
        ("TikTok Reels Cover", 1080, 1920),
        ("Business Card", 1050, 600)
    ]
    created_count = 0
    failed_count = 0

    for label, width, height in draft_specs:
        if usage_limit_reached(user_id, "canva_design"):
            break

        title = f"{base_title} - {label}"[:255]
        canva_design_id, edit_url, view_url, status, error = (
            create_canva_design(
                user_id,
                title,
                width,
                height
            )
        )

        if error:
            failed_count += 1
            continue

        save_canva_design(
            user_id,
            title,
            canva_design_id,
            edit_url,
            view_url,
            status
        )
        log_usage(user_id, "canva_design")
        created_count += 1

    if not created_count:
        return redirect("/business_workflow?canva_design_error=batch_create_failed")

    return redirect(
        f"/dashboard?canva_designs=created"
        f"&created_count={created_count}"
        f"&failed_count={failed_count}"
    )


@app.route("/generate_build_quote")
def generate_build_quote():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?quote_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    shopify_plan = "Recommended Shopify plan based on the store requirements"
    canva_plan = "Recommended Canva plan based on the branding requirements"
    estimated_shopify_cost = "Depends on Shopify plan, domain, apps, and theme"
    estimated_canva_cost = "Depends on Canva plan and premium assets"
    service_fee = "R499"
    total_estimate = "R499 + Shopify/Canva external costs"

    prompt = f"""
Create a professional Store + Branding Build Quote using the user's saved workflow answers.
This is an informational estimate only. Do not claim that Shopify or Canva APIs are connected.
Do not claim that external Shopify or Canva costs will be charged.
Explain that external costs depend on the user's final choices.

The quote must include:

1. Store Build Summary
2. Shopify Setup Items
3. Canva Branding Items
4. Recommended Shopify Plan
5. Recommended Canva Plan
6. Estimated Shopify External Cost
7. Estimated Canva External Cost
8. BusinessBuilder AI Service Fee
9. Total Estimated Setup Cost
10. What Happens After Approval

Use these exact placeholder estimates:
- Estimated Shopify external cost: {estimated_shopify_cost}
- Estimated Canva external cost: {estimated_canva_cost}
- BusinessBuilder AI service fee: {service_fee}
- Total estimated setup cost: {total_estimate}

User workflow answers:
{workflow_text}
"""

    response = safe_openai_chat_completion(
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

    quote_content = response.choices[0].message.content

    quote_id = save_build_quote(
        user_id,
        "Store + Branding Build Quote",
        shopify_plan,
        canva_plan,
        estimated_shopify_cost,
        estimated_canva_cost,
        service_fee,
        total_estimate,
        quote_content
    )

    return redirect(f"/build_quote/{quote_id}")


@app.route("/create_shopify_product")
def create_shopify_product_from_workflow():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "shopify_product"):
        return usage_limit_redirect("shopify_product", "/business_workflow")

    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return redirect("/business_workflow?product_error=shopify_connection")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?product_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create one Shopify product from the user's saved workflow answers.
Return JSON only with exactly these keys:
- title
- description

The title must be concise and suitable for an online store.
The description must be a clear customer-facing product description in plain text.

User workflow answers:
{workflow_text}
"""

    try:
        response = safe_openai_chat_completion(
            model="gpt-4.1-mini",
            response_format={"type": "json_object"},
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
    except Exception:
        return redirect("/business_workflow?product_error=ai_response")

    try:
        product_details = json.loads(response.choices[0].message.content)
        title = product_details["title"].strip()
        description = product_details["description"].strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return redirect("/business_workflow?product_error=ai_response")

    if not title or not description:
        return redirect("/business_workflow?product_error=ai_response")

    shopify_product_id, status, error = create_shopify_product(
        user_id,
        title,
        description
    )

    if error:
        return redirect("/business_workflow?product_error=create_failed")

    save_shopify_product(
        user_id,
        title,
        description,
        shopify_product_id,
        status
    )
    log_usage(user_id, "shopify_product")

    return redirect("/dashboard?shopify_product=created")


@app.route("/build_shopify_store_draft")
def build_shopify_store_draft():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "shopify_product"):
        return usage_limit_redirect("shopify_product", "/business_workflow")

    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return redirect("/business_workflow?store_draft_error=shopify_connection")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?store_draft_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create a Shopify draft store build package from the user's saved workflow answers.
Return JSON only with exactly these top-level keys:
- products
- collections
- pages

The "products" value must be an array of 3 to 5 objects.
Each product object must contain exactly:
- title
- description
- suggested_price
- category

The "collections" value must be an array of 2 to 4 objects.
Each collection object must contain exactly:
- title
- description

The "pages" value must be an object with exactly:
- about
- contact
- shipping_policy
- refund_policy

Each page value must contain clear customer-facing plain-text copy.
Do not include HTML. Do not claim the store is published or live.

User workflow answers:
{workflow_text}
"""

    try:
        response = safe_openai_chat_completion(
            model="gpt-4.1-mini",
            response_format={"type": "json_object"},
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
    except Exception:
        return redirect("/business_workflow?store_draft_error=ai_response")

    try:
        draft_details = json.loads(response.choices[0].message.content)
        products = draft_details["products"]
        collections = draft_details["collections"]
        pages = draft_details["pages"]

        if not isinstance(products, list) or not 3 <= len(products) <= 5:
            raise ValueError

        if not isinstance(collections, list) or not 2 <= len(collections) <= 4:
            raise ValueError

        if not isinstance(pages, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return redirect("/business_workflow?store_draft_error=ai_response")

    created_product_count = 0
    created_collection_count = 0
    created_page_count = 0
    failed_count = 0

    for product in products:
        if usage_limit_reached(user_id, "shopify_product"):
            break

        try:
            title = product["title"].strip()
            description = product["description"].strip()
            suggested_price = product["suggested_price"].strip()
            category = product["category"].strip()
        except (AttributeError, KeyError, TypeError):
            failed_count += 1
            continue

        if not title or not description or not suggested_price or not category:
            failed_count += 1
            continue

        saved_description = (
            f"{description}\n\n"
            f"Suggested price: {suggested_price}\n"
            f"Collection / category: {category}"
        )
        shopify_product_id, status, error = create_shopify_product(
            user_id,
            title,
            description,
            suggested_price,
            category
        )

        if error:
            failed_count += 1
            continue

        save_shopify_product(
            user_id,
            title,
            saved_description,
            shopify_product_id,
            status
        )
        log_usage(user_id, "shopify_product")
        created_product_count += 1

    for collection in collections:
        try:
            title = collection["title"].strip()
            description = collection["description"].strip()
        except (AttributeError, KeyError, TypeError):
            failed_count += 1
            continue

        if not title or not description:
            failed_count += 1
            continue

        shopify_collection_id, status, error = create_shopify_collection(
            user_id,
            title,
            description
        )

        if error:
            failed_count += 1
            continue

        save_shopify_collection(
            user_id,
            title,
            description,
            shopify_collection_id,
            status
        )
        created_collection_count += 1

    page_titles = {
        "about": "About Us",
        "contact": "Contact Us",
        "shipping_policy": "Shipping Policy",
        "refund_policy": "Refund Policy"
    }

    for page_type, title in page_titles.items():
        try:
            content = pages[page_type].strip()
        except (AttributeError, KeyError, TypeError):
            failed_count += 1
            continue

        if not content:
            failed_count += 1
            continue

        shopify_page_id, status, error = create_shopify_page(
            user_id,
            title,
            content
        )

        if error:
            failed_count += 1
            continue

        save_shopify_page(
            user_id,
            title,
            page_type,
            content,
            shopify_page_id,
            status
        )
        created_page_count += 1

    if not (
        created_product_count
        or created_collection_count
        or created_page_count
    ):
        return redirect("/business_workflow?store_draft_error=create_failed")

    return redirect(
        f"/shopify_build_summary?draft_store=created"
        f"&product_count={created_product_count}"
        f"&collection_count={created_collection_count}"
        f"&page_count={created_page_count}"
        f"&failed_count={failed_count}"
    )


@app.route("/create_shopify_products")
def create_shopify_products_from_workflow():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    if usage_limit_reached(user_id, "shopify_product"):
        return usage_limit_redirect("shopify_product", "/business_workflow")

    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return redirect("/business_workflow?product_error=shopify_connection")

    answers = get_nonempty_workflow_answers(user_id)

    if not answers:
        return redirect("/business_workflow?product_error=no_answers")

    workflow_text = ""

    for step_number, step_name, answer in answers:
        workflow_text += f"""
Step {step_number}: {step_name}
Answer:
{answer}

"""

    prompt = f"""
Create 3 to 5 Shopify products from the user's saved workflow answers.
Return JSON only with one top-level key named "products".
The value of "products" must be an array of 3 to 5 objects.
Each object must contain exactly these keys:
- title
- description
- suggested_price
- category

Each title must be concise and suitable for an online store.
Each description must be clear customer-facing product copy in plain text.
Each suggested_price must be a plain-text price suggestion.
Each category must be a concise Shopify product type or collection suggestion.

User workflow answers:
{workflow_text}
"""

    response = safe_openai_chat_completion(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
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

    try:
        product_details = json.loads(response.choices[0].message.content)
        products = product_details["products"]

        if not isinstance(products, list) or not 3 <= len(products) <= 5:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return redirect("/business_workflow?product_error=batch_ai_response")

    created_count = 0
    failed_count = 0

    for product in products:
        if usage_limit_reached(user_id, "shopify_product"):
            break

        try:
            title = product["title"].strip()
            description = product["description"].strip()
            suggested_price = product["suggested_price"].strip()
            category = product["category"].strip()
        except (AttributeError, KeyError, TypeError):
            failed_count += 1
            continue

        if not title or not description or not suggested_price or not category:
            failed_count += 1
            continue

        saved_description = (
            f"{description}\n\n"
            f"Suggested price: {suggested_price}\n"
            f"Collection / category: {category}"
        )
        shopify_product_id, status, error = create_shopify_product(
            user_id,
            title,
            description,
            suggested_price,
            category
        )

        if error:
            failed_count += 1
            continue

        save_shopify_product(
            user_id,
            title,
            saved_description,
            shopify_product_id,
            status
        )
        log_usage(user_id, "shopify_product")
        created_count += 1

    if not created_count:
        return redirect("/business_workflow?product_error=batch_create_failed")

    return redirect(
        f"/dashboard?shopify_products=created"
        f"&created_count={created_count}"
        f"&failed_count={failed_count}"
    )


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

    data = request.get_json(silent=True) or {}
    user_message = str(data.get("message", "")).strip()

    if not user_message:
        return jsonify({
            "reply": "Enter a message before sending."
        }), 400

    if usage_limit_reached(user_id, "chat_message"):
        return jsonify({
            "reply": get_usage_limit_message("chat_message")
        }), 429

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
        title_response = safe_openai_chat_completion(
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

        response = safe_openai_chat_completion(
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
        response = safe_openai_chat_completion(
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
    log_usage(user_id, "chat_message")

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
