import os, json, random, string
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, g)
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash

import psycopg2
import psycopg2.extras

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

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'masala-kitchen-dev-key-change-in-prod')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
_openai_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
    return _openai_client

with open('data/restaurant.json') as f:
    RESTAURANT = json.load(f)

# Promo codes — server-side only, never sent to client
PROMOS = {
    'WELCOME10': {'type': 'pct',   'value': 10,  'label': '10% Welcome discount',       'multi_use': False},
    'SPICE20':   {'type': 'pct',   'value': 20,  'label': '20% Spice Lover discount',    'multi_use': False},
    'HALAL15':   {'type': 'pct',   'value': 15,  'label': '15% Halal loyalty discount',  'multi_use': False},
    'STUDENT10': {'type': 'pct',   'value': 10,  'label': '10% Student discount',        'multi_use': True, 'requires_id': True},
    'FLAT5':     {'type': 'fixed', 'value': 5.0, 'label': '£5 off your order',           'multi_use': False},
}

# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        db = g._db = psycopg2.connect(DATABASE_URL)
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, '_db', None)
    if db:
        db.close()

def db_execute(sql, params=(), fetch='none'):
    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if fetch == 'one':
            result = cur.fetchone()
        elif fetch == 'all':
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
    return result

def init_db():
    with app.app_context():
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS admin_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS promo_usage (
                    id SERIAL PRIMARY KEY,
                    customer_id INTEGER REFERENCES customers(id),
                    promo_code TEXT NOT NULL,
                    order_id TEXT DEFAULT '',
                    used_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(customer_id, promo_code)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    customer_id INTEGER DEFAULT NULL,
                    order_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    address TEXT DEFAULT '',
                    postcode TEXT DEFAULT '',
                    items TEXT NOT NULL,
                    subtotal REAL DEFAULT 0,
                    discount REAL DEFAULT 0,
                    total REAL NOT NULL,
                    promo_code TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    payment_method TEXT DEFAULT 'cash',
                    payment_status TEXT DEFAULT 'pending',
                    stripe_pi TEXT DEFAULT '',
                    status TEXT DEFAULT 'received',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id SERIAL PRIMARY KEY,
                    customer_id INTEGER DEFAULT NULL,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    guests INTEGER NOT NULL,
                    notes TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS menu_items (
                    id SERIAL PRIMARY KEY,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    price REAL NOT NULL,
     0              vegan INTEGER DEFAULT 0,
                    allergens TEXT DEFAULT '[]',
                    active INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0
                );
            ''')
            # Migrate: add customer_id to orders/bookings if not present
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_id INTEGER")
            cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_id INTEGER")
            # Default admin
            cur.execute('SELECT 1 FROM admin_users LIMIT 1')
            if not cur.fetchone():
                cur.execute('INSERT INTO admin_users (username,password_hash) VALUES (%s,%s)',
                           ('admin', generate_password_hash('admin123')))
            # Seed menu
            cur.execute('SELECT 1 FROM menu_items LIMIT 1')
            if not cur.fetchone():
                s = 0
                for cat, items in RESTAURANT['menu'].items():
                    for it in items:
                        cur.execute('''INSERT INTO menu_items
                            (category,name,description,price,vegan,allergens,sort_order)
                            VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                            (cat, it['name'], it.get('description',''), it['price'],
                             1 if it.get('vegan') else 0,
                             json.dumps(it.get('allergens',[])), s))
                        s += 1
        conn.commit()

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
    rows = db_execute('SELECT * FROM menu_items WHERE active=1 ORDER BY category,sort_order,id', fetch='all')
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

def build_system_prompt():
    r = RESTAURANT
    menu_text = ''
    for cat, items in r['menu'].items():
        menu_text += f'\n{cat}:\n'
        for it in items:
            v = ' [VEGAN]' if it.get('vegan') else ''
            a = f" (allergens: {', '.join(it['allergens'])})" if it.get('allergens') else ''
            menu_text += f"  - {it['name']} - £{it['price']:.2f}: {it['description']}{v}{a}\n"
    hours = '\n'.join(f'  {d}: {t}' for d, t in r['hours'].items())
    deals = '\n'.join(f"  - {d['name']}: {d['description']} ({d['valid']})" for d in r['deals'])
    promo_text = '\n'.join(f"  {k}: {v['label']}" for k,v in PROMOS.items())
    bi = r.get('booking_info', {})
    return f'''You are the friendly AI assistant for {r["name"]} - "{r["tagline"]}".
Help customers with menu questions, allergen info, bookings, deals, hours. Be concise.

== RESTAURANT ==
{r["name"]} | {r["address"]} | {r["phone"]} | {r["email"]}

== HOURS ==
{hours}

== DEALS & PROMOS ==
{deals}
Promo codes (require account login): {promo_text}

== MENU ==
{menu_text}

== BOOKING ==
Advance: up to {bi.get("max_advance","30 days")}. Groups 8+: {bi.get("large_groups","call directly")}

== RULES ==
Only help with Masala Kitchen questions. Never invent prices/dishes.
Always mention allergens when asked. Direct complaints to {r["phone"]} or {r["email"]}.
'''

SYSTEM_PROMPT = build_system_prompt()

# ── Customer auth ──────────────────────────────────────────────────────────────
@app.route('/customer/register', methods=['POST'])
def customer_register():
    data  = request.get_json()
    email = (data.get('email','') or '').strip().lower()
    pw    = data.get('password','')
    name  = (data.get('name','') or '').strip()
    if not email or not pw or len(pw) < 6:
        return jsonify({'error': 'Email and password (min 6 chars) required'}), 400
    if db_execute('SELECT 1 FROM customers WHERE email=%s', (email,), fetch='one'):
        return jsonify({'error': 'Email already registered — please log in'}), 400
    row = db_execute('INSERT INTO customers (email,password_hash,name) VALUES(%s,%s,%s) RETURNING id',
                     (email, generate_password_hash(pw), name), fetch='one')
    session['cust_id']    = row['id']
    session['cust_email'] = email
    session['cust_name']  = name
    return jsonify({'success': True, 'name': name, 'email': email})

@app.route('/customer/login', methods=['POST'])
def customer_login():
    data  = request.get_json()
    email = (data.get('email','') or '').strip().lower()
    pw    = data.get('password','')
    user  = db_execute('SELECT * FROM customers WHERE email=%s', (email,), fetch='one')
    if not user or not check_password_hash(user['password_hash'], pw):
        return jsonify({'error': 'Invalid email or password'}), 401
    session['cust_id']    = user['id']
    session['cust_email'] = user['email']
    session['cust_name']  = user['name']
    return jsonify({'success': True, 'name': user['name'], 'email': user['email']})

@app.route('/customer/logout', methods=['POST'])
def customer_logout():
    for k in ('cust_id','cust_email','cust_name'):
        session.pop(k, None)
    return jsonify({'success': True})

@app.route('/customer/me')
def customer_me():
    if session.get('cust_id'):
        return jsonify({'logged_in': True, 'name': session.get('cust_name',''), 'email': session.get('cust_email','')})
    return jsonify({'logged_in': False})

@app.route('/customer/orders')
def customer_orders():
    if not session.get('cust_id'):
        return jsonify({'error': 'Not logged in'}), 401
    rows = db_execute('SELECT * FROM orders WHERE customer_id=%s ORDER BY created_at DESC',
                      (session['cust_id'],), fetch='all')
    result = []
    for r in rows:
        d = dict(r)
        d['items_list'] = json.loads(d['items'])
        d['created_at'] = str(d['created_at'])
        result.append(d)
    return jsonify(result)

@app.route('/customer/used-promos')
def customer_used_promos():
    if not session.get('cust_id'):
        return jsonify([])
    rows = db_execute('SELECT promo_code FROM promo_usage WHERE customer_id=%s',
                      (session['cust_id'],), fetch='all')
    return jsonify([r['promo_code'] for r in rows])

# ── Public routes ──────────────────────────────────────────────────────────────
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
    row = db_execute('SELECT * FROM orders WHERE id=%s', (oid.upper(),), fetch='one')
    if not row:
        return jsonify({'error': 'Order not found'}), 404
    return jsonify({
        'id': row['id'], 'name': row['name'], 'order_type': row['order_type'],
        'status': row['status'], 'total': row['total'],
        'items': json.loads(row['items']),
        'payment_method': row['payment_method'],
        'payment_status': row['payment_status'],
        'created_at': str(row['created_at']),
    })

@app.route('/apply-promo', methods=['POST'])
def apply_promo():
    data     = request.get_json()
    code     = (data.get('code','') or '').strip().upper()
    subtotal = float(data.get('subtotal', 0))
    promo    = PROMOS.get(code)

    if not promo:
        return jsonify({'valid': False, 'message': 'Invalid promo code'})

    cust_id = session.get('cust_id')
    if not cust_id:
        return jsonify({'valid': False, 'message': 'Please log in to use promo codes', 'require_login': True})

    # Check if already used (for single-use promos)
    if not promo.get('multi_use'):
        already = db_execute('SELECT 1 FROM promo_usage WHERE customer_id=%s AND promo_code=%s',
                             (cust_id, code), fetch='one')
        if already:
            return jsonify({'valid': False, 'message': f'You have already used {code}'})

    if promo['type'] == 'pct':
        discount = round(subtotal * promo['value'] / 100, 2)
    else:
        discount = min(promo['value'], subtotal)

    return jsonify({
        'valid':       True,
        'code':        code,
        'label':       promo['label'],
        'discount':    discount,
        'total':       round(subtotal - discount, 2),
        'requires_id': promo.get('requires_id', False),
    })

@app.route('/order', methods=['POST'])
def place_order():
    data  = request.get_json()
    name  = data.get('name','').strip()
    phone = data.get('phone','').strip()
    if not name or not phone:
        return jsonify({'error': 'Name and phone required'}), 400

    code     = (data.get('promo_code','') or '').strip().upper()
    subtotal = float(data.get('subtotal', data.get('total', 0)))
    discount = 0.0
    promo    = PROMOS.get(code)
    cust_id  = session.get('cust_id')

    if promo and cust_id:
        # Server-side re-validate usage
        if not promo.get('multi_use'):
            already = db_execute('SELECT 1 FROM promo_usage WHERE customer_id=%s AND promo_code=%s',
                                (cust_id, code), fetch='one')
            if already:
                code = ''  # strip the promo — already used
            else:
                if promo['type'] == 'pct':
                    discount = round(subtotal * promo['value'] / 100, 2)
                else:
                    discount = min(promo['value'], subtotal)
        else:
            if promo['type'] == 'pct':
                discount = round(subtotal * promo['value'] / 100, 2)
            else:
                discount = min(promo['value'], subtotal)
    elif promo and not cust_id:
        code = ''  # guests can't use promos

    total = round(subtotal - discount, 2)
    oid   = short_id()

    db_execute('''INSERT INTO orders
        (id,customer_id,order_type,name,phone,email,address,postcode,
         items,subtotal,discount,total,promo_code,notes,payment_method)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (
        oid, cust_id, data.get('order_type','Dine-in'), name, phone,
        data.get('email',''), data.get('address',''), data.get('postcode',''),
        json.dumps(data.get('items',[])), subtotal, discount, total,
        code, data.get('notes',''), data.get('payment_method','cash'),
    ))

    # Record promo usage for single-use codes
    if code and promo and cust_id and not promo.get('multi_use'):
        try:
            db_execute('INSERT INTO promo_usage (customer_id,promo_code,order_id) VALUES(%s,%s,%s)',
                      (cust_id, code, oid))
        except Exception:
            pass  # race condition — ignore duplicate

    print(f'=== NEW ORDER {oid} | {data.get("order_type")} | {name} | £{total} ===')
    return jsonify({'success': True, 'order_id': oid})

@app.route('/book', methods=['POST'])
def book():
    data  = request.get_json()
    name  = data.get('name','').strip()
    phone = data.get('phone','').strip()
    if not name or not phone:
        return jsonify({'error': 'Name and phone required'}), 400
    cust_id = session.get('cust_id')
    db_execute('''INSERT INTO bookings (customer_id,name,phone,email,date,time,guests,notes)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
               (cust_id, name, phone, data.get('email',''),
                data.get('date',''), data.get('time',''),
                int(data.get('guests',2)), data.get('notes','')))
    print(f"=== BOOKING | {name} | {data.get('date')} {data.get('time')} | {data.get('guests')} guests ===")
    return jsonify({'success': True, 'message': 'Booking received!'})

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
            messages=[{'role':'system','content':SYSTEM_PROMPT}] + history,
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

# ── Stripe ─────────────────────────────────────────────────────────────────────
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
        db_execute("UPDATE orders SET payment_status='paid' WHERE stripe_pi=%s", (pi,))
    return '', 200

# ── Admin auth ─────────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        user = db_execute('SELECT * FROM admin_users WHERE username=%s', (username,), fetch='one')
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

# ── Admin dashboard ────────────────────────────────────────────────────────────
@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    orders = db_execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 200', fetch='all')
    order_list = []
    for o in orders:
        od = dict(o)
        od['order_items'] = json.loads(od['items'])  # use order_items to avoid dict.items() collision
        od['created_at']  = str(od['created_at'])
        od['updated_at']  = str(od['updated_at'])
        order_list.append(od)

    bookings = db_execute('SELECT * FROM bookings ORDER BY date DESC, time DESC LIMIT 100', fetch='all')
    booking_list = [dict(b) for b in bookings]
    for b in booking_list:
        b['created_at'] = str(b['created_at'])

    menu_rows = db_execute('SELECT * FROM menu_items ORDER BY category,sort_order,id', fetch='all')
    menu_list = []
    for m in menu_rows:
        md = dict(m)
        md['allergens'] = json.loads(md['allergens'])
        menu_list.append(md)

    categories = list(dict.fromkeys(m['category'] for m in menu_list))

    stats = {
        'total_orders':      db_execute('SELECT COUNT(*) as c FROM orders', fetch='one')['c'],
        'today_orders':      db_execute("SELECT COUNT(*) as c FROM orders WHERE DATE(created_at)=CURRENT_DATE", fetch='one')['c'],
        'today_revenue':     db_execute("SELECT COALESCE(SUM(total),0) as s FROM orders WHERE DATE(created_at)=CURRENT_DATE", fetch='one')['s'],
        'pending':           db_execute("SELECT COUNT(*) as c FROM orders WHERE status='received'", fetch='one')['c'],
        'total_bookings':    db_execute("SELECT COUNT(*) as c FROM bookings", fetch='one')['c'],
        'upcoming_bookings': db_execute("SELECT COUNT(*) as c FROM bookings WHERE date::date >= CURRENT_DATE AND status != 'cancelled'", fetch='one')['c'],
        'total_customers':   db_execute("SELECT COUNT(*) as c FROM customers", fetch='one')['c'],
    }

    weekly = db_execute('''
        SELECT DATE(created_at) as day, COUNT(*) as orders, COALESCE(SUM(total),0) as revenue
        FROM orders WHERE created_at >= NOW() - INTERVAL '7 days'
        GROUP BY DATE(created_at) ORDER BY day
    ''', fetch='all')
    weekly_data = [{'day': str(r['day']), 'orders': r['orders'], 'revenue': float(r['revenue'])} for r in (weekly or [])]

    top_dishes_raw = db_execute('SELECT items FROM orders LIMIT 200', fetch='all')
    dish_counts = {}
    for row in (top_dishes_raw or []):
        for item in json.loads(row['items']):
            n = item.get('name','Unknown')
            dish_counts[n] = dish_counts.get(n, 0) + item.get('qty', 1)
    top_dishes = sorted(dish_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    return render_template('admin.html',
                           orders=order_list,
                           bookings=booking_list,
                           menu_items=menu_list,
                           categories=categories,
                           stats=stats,
                           weekly_data=json.dumps(weekly_data),
                           top_dishes=json.dumps(top_dishes),
                           username=session.get('admin_username','admin'))

# ── Admin API ──────────────────────────────────────────────────────────────────
@app.route('/admin/api/orders')
@admin_required
def admin_api_orders():
    rows = db_execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 200', fetch='all')
    result = []
    for r in rows:
        d = dict(r)
        d['order_items'] = json.loads(d['items'])
        d['created_at']  = str(d['created_at'])
        d['updated_at']  = str(d['updated_at'])
        result.append(d)
    return jsonify(result)

@app.route('/admin/api/orders/new-count')
@admin_required
def admin_new_order_count():
    since = request.args.get('since', '')
    if since:
        count = db_execute("SELECT COUNT(*) as c FROM orders WHERE created_at > %s AND status='received'",
                          (since,), fetch='one')['c']
    else:
        count = 0
    return jsonify({'count': count})

@app.route('/admin/api/order/<oid>/status', methods=['POST'])
@admin_required
def admin_update_status(oid):
    status = request.get_json().get('status','')
    valid  = ['received','preparing','ready','completed','cancelled']
    if status not in valid:
        return jsonify({'error': 'Invalid status'}), 400
    db_execute("UPDATE orders SET status=%s, updated_at=NOW() WHERE id=%s", (status, oid))
    return jsonify({'success': True})

@app.route('/admin/api/bookings')
@admin_required
def admin_api_bookings():
    rows = db_execute('SELECT * FROM bookings ORDER BY date DESC, time DESC', fetch='all')
    result = [dict(r) for r in rows]
    for r in result:
        r['created_at'] = str(r['created_at'])
    return jsonify(result)

@app.route('/admin/api/booking/<int:bid>/status', methods=['POST'])
@admin_required
def admin_update_booking_status(bid):
    status = request.get_json().get('status','')
    valid  = ['pending','confirmed','cancelled']
    if status not in valid:
        return jsonify({'error': 'Invalid status'}), 400
    db_execute("UPDATE bookings SET status=%s WHERE id=%s", (status, bid))
    return jsonify({'success': True})

@app.route('/admin/api/menu', methods=['GET'])
@admin_required
def admin_menu_list():
    rows = db_execute('SELECT * FROM menu_items ORDER BY category,sort_order,id', fetch='all')
    return jsonify([dict(r) for r in rows])

@app.route('/admin/api/menu', methods=['POST'])
@admin_required
def admin_menu_add():
    data = request.get_json()
    allergens = data.get('allergens','')
    if isinstance(allergens, list):
        allergens_json = json.dumps(allergens)
    else:
        allergens_json = json.dumps([a.strip() for a in allergens.split(',') if a.strip()])
    db_execute('''INSERT INTO menu_items (category,name,description,price,vegan,allergens)
                  VALUES(%s,%s,%s,%s,%s,%s)''',
               (data['category'], data['name'], data.get('description',''),
                float(data['price']), 1 if data.get('vegan') else 0, allergens_json))
    return jsonify({'success': True})

@app.route('/admin/api/menu/<int:mid>', methods=['PUT'])
@admin_required
def admin_menu_update(mid):
    data = request.get_json()
    allergens = data.get('allergens','')
    if isinstance(allergens, list):
        allergens_json = json.dumps(allergens)
    else:
        allergens_json = json.dumps([a.strip() for a in allergens.split(',') if a.strip()])
    db_execute('''UPDATE menu_items
                  SET category=%s,name=%s,description=%s,price=%s,vegan=%s,allergens=%s,active=%s
                  WHERE id=%s''',
               (data['category'], data['name'], data.get('description',''),
                float(data['price']), 1 if data.get('vegan') else 0,
                allergens_json, 1 if data.get('active', True) else 0, mid))
    return jsonify({'success': True})

@app.route('/admin/api/menu/<int:mid>', methods=['DELETE'])
@admin_required
def admin_menu_delete(mid):
    db_execute('UPDATE menu_items SET active=0 WHERE id=%s', (mid,))
    return jsonify({'success': True})

@app.route('/admin/api/change-password', methods=['POST'])
@admin_required
def change_password():
    data    = request.get_json()
    current = data.get('current','')
    new_pw  = data.get('new','')
    if len(new_pw) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    user = db_execute('SELECT * FROM admin_users WHERE id=%s', (session['admin_id'],), fetch='one')
    if not check_password_hash(user['password_hash'], current):
        return jsonify({'error': 'Current password is incorrect'}), 400
    db_execute('UPDATE admin_users SET password_hash=%s WHERE id=%s',
               (generate_password_hash(new_pw), session['admin_id']))
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
