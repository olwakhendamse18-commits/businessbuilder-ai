from flask import Flask, request, jsonify, render_template, redirect, session, send_file
from openai import OpenAI
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
import secrets
import psycopg2
import re
import json
import urllib.parse
from io import BytesIO
from xml.sax.saxutils import escape


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

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS shopify_plans (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS canva_branding_packages (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS canva_design_briefs (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    cur.execute(f"""
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

    cur.execute(f"""
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

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS shopify_connections (
            id {id_type},
            user_id INTEGER NOT NULL,
            shop_domain TEXT NOT NULL,
            access_token TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS canva_connections (
            id {id_type},
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            connected_email TEXT,
            access_token TEXT,
            refresh_token TEXT
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS canva_oauth_sessions (
            id {id_type},
            user_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            code_verifier TEXT NOT NULL
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS shopify_products (
            id {id_type},
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            shopify_product_id TEXT NOT NULL,
            status TEXT NOT NULL
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


def save_shopify_plan(user_id, title, content):
    conn = db()
    cur = conn.cursor()

    if using_postgres():
        cur.execute(
            """
            INSERT INTO shopify_plans (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, title, content)
        )
        plan_id = cur.fetchone()[0]
    else:
        cur.execute(
            """
            INSERT INTO shopify_plans (user_id, title, content)
            VALUES (?, ?, ?)
            """,
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
            """
            INSERT INTO canva_branding_packages (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, title, content)
        )
        package_id = cur.fetchone()[0]
    else:
        cur.execute(
            """
            INSERT INTO canva_branding_packages (user_id, title, content)
            VALUES (?, ?, ?)
            """,
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
            """
            INSERT INTO canva_design_briefs (user_id, title, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, title, content)
        )
        brief_id = cur.fetchone()[0]
    else:
        cur.execute(
            """
            INSERT INTO canva_design_briefs (user_id, title, content)
            VALUES (?, ?, ?)
            """,
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
            """
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
            """,
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
            """
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
            """,
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


def create_canva_design(user_id, title):
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
                    "width": 1080,
                    "height": 1080
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

    design = result.get("design", {})
    canva_design_id = design.get("id")

    if response.status_code != 200 or not canva_design_id:
        return None, None, None, None, "Canva could not create the design draft. Reconnect Canva or try again."

    urls = design.get("urls", {})

    return (
        canva_design_id,
        urls.get("edit_url", ""),
        urls.get("view_url", ""),
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
    shopify_plans = get_shopify_plans(user_id)
    canva_branding_packages = get_canva_branding_packages(user_id)
    canva_design_briefs = get_canva_design_briefs(user_id)
    canva_designs = get_canva_designs(user_id)
    build_quotes = get_build_quotes(user_id)
    shopify_connection = get_shopify_connection(user_id)
    canva_connection = get_canva_connection(user_id)
    shopify_products = get_shopify_products(user_id)

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
        shopify_products=shopify_products
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

    filename = re.sub(r"[_-]+", "-", secure_filename(plan[1])).strip("-")
    filename = filename or "business-plan"

    return send_file(
        create_business_plan_pdf(plan[1], plan[2]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{filename}.pdf"
    )


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


@app.route("/generate_shopify_plan")
def generate_shopify_plan():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_all_workflow_answers(user_id)

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

    shopify_plan_content = response.choices[0].message.content

    plan_id = save_shopify_plan(
        user_id,
        "Generated Shopify Setup Plan",
        shopify_plan_content
    )

    return redirect(f"/shopify_plan/{plan_id}")


@app.route("/generate_canva_branding")
def generate_canva_branding():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_all_workflow_answers(user_id)

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

    package_content = response.choices[0].message.content

    package_id = save_canva_branding_package(
        user_id,
        "Generated Canva Branding Package",
        package_content
    )

    return redirect(f"/canva_branding/{package_id}")


@app.route("/generate_canva_design_brief")
def generate_canva_design_brief():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_all_workflow_answers(user_id)

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

    brief_content = response.choices[0].message.content

    brief_id = save_canva_design_brief(
        user_id,
        "Generated Canva Design Brief",
        brief_content
    )

    return redirect(f"/canva_design_brief/{brief_id}")


@app.route("/create_canva_design")
def create_canva_design_from_brief():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

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

    return redirect("/dashboard?canva_design=created")


@app.route("/generate_build_quote")
def generate_build_quote():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    answers = get_all_workflow_answers(user_id)

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

    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return redirect("/business_workflow?product_error=shopify_connection")

    answers = get_all_workflow_answers(user_id)

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

    response = client.chat.completions.create(
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

    return redirect("/dashboard?shopify_product=created")


@app.route("/create_shopify_products")
def create_shopify_products_from_workflow():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    if not user_has_paid(user_id):
        return redirect("/dashboard")

    connection = get_shopify_connection(user_id)

    if not connection or connection[3] != "connected":
        return redirect("/business_workflow?product_error=shopify_connection")

    answers = get_all_workflow_answers(user_id)

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

    response = client.chat.completions.create(
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
