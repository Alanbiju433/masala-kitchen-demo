import os, json, sqlite3, random, string
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, g)
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash

# ── Stripe (optional – set env vars to enable) ─────────────────────────────────
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

# ── App ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'masala-kitchen-dev-key-change-in-prod')

# Get the app's directory
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APP_DIR, 'masala.db')

_openai_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
    return _openai_client

# Load restaurant data with proper path
with open(os.path.join(APP_DIR, 'data/restaurant.json')) as f:
    RESTAURANT = json.load(f)
# ── Database helpers ───────────────────────────────────────────────────────────
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

# ── Helpers ────────────────────────────────────────────────────────────────────
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

# ── AI system prompt ─────────────────────────────────────────────────
def build_system_prompt():
    r = RESTAURANT
    menu_text = ''
    for cat, items in r['menu'].items():
        menu_text += f'\{cat}:\n'
        for it in items:
            v = ' [VEGAN]' if it.get('vegan') else ''
            a = f" (allergens: {', '.join(it['allergens'])})" if it.get('allergens') else ''
            menu_text += f"  {it['name']} — £{it['price']:.2f}: {it['description']}{v}{a}\n"
    hours = '\n'.join(f'  {d}: {t}' for d, t in r['hours'].items())
    deals = '\n'.join(f"  - {d['name']}: {d['description']} ({d['valid']})" for d in r['deals'])
    bi    = r.get('booking_info', {})
    faqs  = r.get('faqs', [])
    faq_text = '\n'.join(f"  Q: {f['q']}\n  A: {f['a']}" for f in faqs) if faqs else '  (none)'
    return f'''You are the friendly AI assistant for {r['name']} — "{r['tagline']}".
Help customers with menu questions, allergen info, bookings, deals, hours. Be concise.

== RESTAURANT ==
{r['name']} | {r['address']} | {r['phone']} | {r['email']}
Services: {r.get('services', 'N/A')}

)== HOURS ==
{hours}

== DEALS ==
{deals}

== MENU ==
{menu_text}

== BOOKING ==
Advance: up to {bi.get('max_advance','30 days')}
Groups 8+: {bi.get('large_groups','call directly')}

== FAQs ==
{faq_text}

== RULES ==
Only help with Masala Kitchen questions. Never invent prices/dishes.
Always mention allergens when asked. Direct complaints to {r['phone']} or {r['email']}.
'''

SYSTEM_PROMPT = build_system_prompt()

# ── Public routes ───────────────────────────────────────────────────────
@app.route('/')
def index():
    restaurant = dict(RESTAURANT)
    restaurant['menu'] = menu_from_db()
    return render_template('index.html', restaurant=restaurant,
                           stripe_on=STRIPE_ON, stripe_pub=STRIPE_PUB)

@app.route('/order/status')
def order_status_page():
    return render_template('order_status.html', restaurant=RESTAURANT)

@app.route('/api/order/<oid>')
def api_get_order(oid):
    db  = get_db()
    row = db.execute('SELECT * FROM orders WHERE id=?', (oid.upper(),)).fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify({
        'id': row['id'], 'name': row['name'], 'order_type': row['order_type'],
        'status': row['status'], 'total': row['total'],
        'items': json.loads(row['items']),
        'payment_method': row['payment_method'],
        'payment_status': row['payment_status'],
        'created_at': row['created_at'],
    })

@app.route('/order', methods=['POST'])
def place_order():
    data  = request.get_json()
    name  = data.get('name','').strip()
    phone = data.get('phone','').strip()
    if not name or not phone:
        return jsonify({'error': 'Name and phone required'}), 400
    oid = short_id()
    db  = get_db()
    db.execute('''INSERT INTO orders
        (id,order_type,name,phone,email,address,postcode,
         items,total,notes,payment_method)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (
        oid, data.get('order_type','Dine-in'), name, phone,
        data.get('email',''), data.get('address',''), data.get('postcode',''),
        json.dumps(data.get('items',[])), float(data.get('total',0)),
        data.get('notes',''), data.get('payment_method','cash'),
    ))
    db.commit()
    print(f'=== NEW ORDER {oid} | {data.get("order_type")} | {name} | £{data.get("total")} ===')
    return jsonify({'success': True, 'order_id': oid})

@app.route('/book', methods=['POST'])
def book():
    data = request.get_json()
    print(f"=== BOOKING | {data.get('name')} | {data.get('phone')} | {data.get('date')} {data.get('time')} | {data.get('guests')} guests ===")
    return jsonify({'success': True, 'message': 'Booking received'})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    msg  = data.get('message','').strip()
    if not msg:
        return jsonify({'error': 'No message'}), 400
    if 'history' not in session:
        session['history'] = []
    session['history'].append({'role': 'user', 'content': msg})
    history = session['history'][-20:]
    try:
        resp  = get_openai_client().chat.completions.create(
            model='gpt-4o-mini',
            messages=[{"role":"system","content":SYSTEM_PROMPT}] + history,
            max_tokens=400, temperature=0.7,
        )
        reply = resp.choices[0].message.content
        session['history'].append({'role':'assistant','content':reply})
        session.modified = True
        return jsonify({'reply': reply})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/reset', methods=['POST'])
def reset():
    session.pop('history', None)
    return jsonify({'status': 'ok'})

# ── Stripe ────────────────────────────────────────────────────────────────────────
@app.route('/stripe/create-intent', methods=['POST'])
def create_intent():
    if not STRIPE_ON:
        return jsonify({'error': 'Stripe not configured'}), 503
    data   = request.get_json()
    pence  = int(float(data.get('total', 0)) * 100)
    intent = _stripe.PaymentIntent.create(
        amount=pence, currency='gbp',
        automatic_payment_methods={'enabled': True},
        metadata={'name': data.get('name',''), 'phone': data.get('phone','')},
    )
    return jsonify({'client_secret': intent.client_secret})

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    if not STRIPE_ON:
        return '', 400
    try:
        event = _stripe.Webhook.construct_event(
            request.data,
            request.headers.get('Stripe-Signature',''),
            os.environ.get('STRIPE_WEBHOOK_SECRET',''),
        )
    except Exception:
        return '', 400
    if event['type'] == 'payment_intent.succeeded':
        pi = event['data']['object']['id']
        db = get_db()
        db.execute("UPDATE orders SET payment_status='paid' WHERE stripe_pi=?", (pi,))
        db.commit()
    return '', 200

# ── Admin auth ──────────────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        db   = get_db()
        user = db.execute('SELECT * FROM admin_users WHERE username=?',(username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['admin_id']       = user['id']
            session['admin_username'] = user['username']
            return redirect(url_for('admin_dashboard'))
        error = 'Invalid username or password'
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_username', None)
    return redirect(url_for('admin_login'))

# ── Admin dashboard ────────────────────────────────────────────────────────
@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    db     = get_db()
    orders = db.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 200').fetchall()
    order_list = []
    for o in orders:
        od = dict(o)
        od['items'] = json.loads(od['items'])
        order_list.append(od)
    menu_rows = db.execute('SELECT * FROM menu_items ORDER BY category,sort_order,id').fetchall()
    menu_list = []
    for m in menu_rows:
        md = dict(m)
        md['allergens'] = json.loads(md['allergens'])
        menu_list.append(md)
    categories = list(dict.fromkeys(m['category'] for m in menu_list))
    stats = {
        'total_orders':  db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],
        'today_orders':  db.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now','localtime')").fetchone()[0],
        'today_revenue': db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE date(created_at)=date('now','localtime')").fetchone()[0],
        'pending':       db.execute("SELECT COUNT(*) FROM orders WHERE status='received'").fetchone()[0],
    }
    return render_template('admin.html',
                           orders=order_list, menu_items=menu_list,
                           categories=categories, stats=stats,
                           username=session.get('admin_username','admin'))

# ── Admin API ────────────────────────────────────────────────────────────────────────
@app.route('/admin/api/orders')
@admin_required
def admin_api_orders():
    db   = get_db()
    rows = db.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 200').fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['items'] = json.loads(d['items'])
        result.append(d)
    return jsonify(result)

@app.route('/admin/api/order/<oid>/status', methods=['POST'])
@admin_required
def admin_update_status(oid):
    status = request.get_json().get('status','')
    valid  = ['received','preparing','ready','completed','cancelled']
    if status not in valid:
        return jsonify({"error": "Invalid status"}), 400
    db = get_db()
    db.execute("UPDATE orders SET status=?,updated_at=datetime('now','localtime') WHERE id=?",
               (status, oid))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin/api/menu', methods=['GET'])
@admin_required
def admin_menu_list():
    db   = get_db()
    rows = db.execute('SELECT * FROM menu_items ORDER BY category,sort_order,id').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/admin/api/menu', methods=['POST'])
@admin_required
def admin_menu_add():
    data = request.get_json()
    db   = get_db()
    allergens = data.get('allergens','')
    if isinstance(allergens, list):
        allergens_json = json.dumps(allergens)
    else:
        allergens_json = json.dumps([a.strip() for a in allergens.split(',') if a.strip()])
    db.execute('''INSERT INTO menu_items (category,name,description,price,vegan,allergens)
                  VALUES(?,?,?,?,?,?)''',
               (data['category'], data['name'], data.get('description',''),
                float(data['price']), 1 if data.get('vegan') else 0, allergens_json))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin/api/menu/<int:mid>', methods=['PUT'])
@admin_required
def admin_menu_update(mid):
    data = request.get_json()
    db   = get_db()
    allergens = data.get('allergens','')
    if isinstance(allergens, list):
        allergens_json = json.dumps(allergens)
    else:
        allergens_json = json.dumps([a.strip() for a in allergens.split(',') if a.strip()])
    db.execute('''UPDATE menu_items
                  SET category=?,name=?,description=?,price=?,vegan=?,allergens=?,active=?
                  WHERE id=?''',
               (data['category'], data['name'], data.get('description',''),
                float(data['price']), 1 if data.get('vegan', True) else 0, allergens_json,
                1 if data.get('active', True) else 0, mid))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin/api/menu/<int:mid>', methods=['DELETE'])
@admin_required
def admin_menu_delete(mid):
    db = get_db()
    db.execute('UPDATE menu_items SET active=0 WHERE id=?', (mid,))
    db.commit()
    return jsonify({'success': True})

@app.route('/admin/api/change-password', methods=['POST'])
@admin_required
def change_password():
    data    = request.get_json()
    current = data.get('current','')
    new_pw  = data.get('new','')
    if len(new_pw) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    db   = get_db()
    user = db.execute('SELECT * FROM admin_users WHERE id=?',(session['admin_id'],)).fetchone()
    if not check_password_hash(user['password_hash'], current):
        return jsonify({'error': 'Current password is incorrect'}), 400
    db.execute('UPDATE admin_users SET password_hash=? WHERE id=?',
               (generate_password_hash(new_pw), session['admin_id']))
    db.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
