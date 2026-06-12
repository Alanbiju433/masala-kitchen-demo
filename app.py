import os, json, sqlite3, random, string
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, g)
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash

# ââ Stripe (optional â set env vars to enable) âââââââââââââââââââââââââââââââââ
try:
    import stripe as _stripe
    _STRIPE_SECRET = os.environ.get('STRIPE_SECRET_KEY', '')
    STRIPE_PUB     = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
    STRIPE_ON      = bool(_STRIPE_SECRET and STRIPE_PUB)
    if STRIPE_ON:
        _stripe.api_key = _STRIPE_SECRET
except ImportError:
    _stripe = None
    STRIPE_ON  = False
    STRIPE_PUB = ''

# ââ App ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'masala-kitchen-dev-key-change-in-prod')

DB = os.path.join(os.path.dirname(__file__), 'masala.db')
client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

with open('data/restaurant.json') as f:
    RESTAURANT = json.load(f)

# ââ Database helpers âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        db = g._db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, '_db', None)
    if db:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                order_type TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT DEFAULT '',
                address TEXT DEFAULT '',
                postcode TEXT DEFAULT '',
                items TEXT NOT NULL,
                total REAL NOT NULL,
                notes TEXT DEFAULT '',
                payment_method TEXT DEFAULT 'cash',
                payment_status TEXT DEFAULT 'pending',
                stripe_pi TEXT DEFAULT '',
                status TEXT DEFAULT 'received',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL,
                vegan INTEGER DEFAULT 0,
                allergens TEXT DEFAULT '[]',
                active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0
            );
        ''')
        # Default admin
        if not db.execute('SELECT 1 FROM admin_users LIMIT 1').fetchone():
            db.execute('INSERT INTO admin_users (username,password_hash) VALUES (?,?)',
                       ('admin', generate_password_hash('admin123')))
        # Seed menu from JSON once
        if not db.execute('SELECT 1 FROM menu_items LIMIT 1').fetchone():
            s = 0
            for cat, items in RESTAURANT['menu'].items():
                for it in items:
                    db.execute('''INSERT INTO menu_items
                        (category,name,description,price,vegan,allergens,sort_order)
                        VALUES(?,?,?,?,?,?,?)''',
                        (cat, it['name'], it.get('description',''), it['price'],
                         1 if it.get('vegan') else 0,
                         json.dumps(it.get('allergens',[])), s))
                    s += 1
        db.commit()

init_db()

# ââ Helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def short_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def admin_required(f):
    @wraps(f)
    def deco(*a, **kw):
        if not session.get('admin_id'):
            return redirect(url_for('admin_login'))
        return f(*a, **kw)
    return deco

def menu_from_db():
    db = get_db()
    rows = db.execute(
        'SELECT * FROM menu_items WHERE active=1 ORDER BY category,sort_order,id'
    ).fetchall()
    cats = {}
    for r in rows:
        cat = r['category']
        cats.setdefault(cat, [])
        cats[cat].append({
            'name': r['name'], 'description': r['description'],
            'price': r['price'], 'vegan': bool(r['vegan']),
            'allergens': json.loads(r['allergens']),
        })
    return cats

# ââ AI system prompt ââââââââââââââââââââââââââââââ
