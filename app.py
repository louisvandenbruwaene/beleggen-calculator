#!/usr/bin/env python3
"""
Beleggingen Web Application
Belgian capital gains tax calculator with FIFO principle
Correct Belgian tax rules: €10,000 tax-free per year, 10% tax rate
"""

import os
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import json

# Application setup
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Database: use DATABASE_URL if available (Railway/Render), otherwise SQLite
database_url = os.environ.get('DATABASE_URL', 'sqlite:///beleggen.db')
# Railway uses postgres:// but SQLAlchemy needs postgresql://
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Encryption key for sensitive data (IMPORTANT: set this in production!)
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    # Generate a key for development (WARNING: data will be lost on restart without env var)
    ENCRYPTION_KEY = Fernet.generate_key()
elif isinstance(ENCRYPTION_KEY, str):
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()
cipher = Fernet(ENCRYPTION_KEY)

# =============================================================================
# BELGIAN TAX CONSTANTS (Correct values from meerwaardebelasting)
# =============================================================================
BASE_LIMIT = 10000      # €10,000 tax-free per year (constant)
BUFFER_ZONE = 1000      # €1,000 buffer above limit (if tax paid < €1,000)
TAX_RATE = 0.10         # 10% tax on gains above limit
# Note: No carryover system - limit is fixed at €10,000 each year


# =============================================================================
# DATABASE MODELS
# =============================================================================

class User(db.Model):
    """User account model."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    portfolios = db.relationship('Portfolio', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Portfolio(db.Model):
    """Portfolio containing multiple assets."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    assets = db.relationship('Asset', backref='portfolio', lazy=True, cascade='all, delete-orphan')


class Asset(db.Model):
    """Individual asset/stock in a portfolio."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    isin = db.Column(db.String(20))
    portfolio_id = db.Column(db.Integer, db.ForeignKey('portfolio.id'), nullable=False)
    lots_encrypted = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_lots(self):
        """Decrypt and return lots data."""
        if not self.lots_encrypted:
            return []
        try:
            decrypted = cipher.decrypt(self.lots_encrypted.encode()).decode()
            return json.loads(decrypted)
        except:
            return []

    def set_lots(self, lots):
        """Encrypt and store lots data."""
        json_data = json.dumps(lots)
        self.lots_encrypted = cipher.encrypt(json_data.encode()).decode()


# =============================================================================
# FIFO CALCULATION FUNCTIONS
# =============================================================================

def calculate_fifo_cost_basis(lots, quantity):
    """
    Calculate cost basis using FIFO for selling a given quantity.
    Returns: (total_cost, lots_used_details) or (None, []) if not enough shares
    """
    cost = 0.0
    remaining = quantity
    lots_used = []

    for lot in lots:
        if remaining <= 0:
            break
        available = lot.get('remaining', lot['quantity'])
        if available <= 0:
            continue

        use = min(remaining, available)
        cost += use * lot['price']
        lots_used.append({
            'date': lot['date'],
            'quantity': use,
            'price': lot['price']
        })
        remaining -= use

    if remaining > 0:
        return None, []

    return cost, lots_used


def get_total_available(lots):
    """Get total available shares from all lots."""
    return sum(lot.get('remaining', lot['quantity']) for lot in lots)


def get_total_cost(lots):
    """Get total cost basis for all available shares."""
    return sum(lot.get('remaining', lot['quantity']) * lot['price'] for lot in lots)


def calculate_gain(lots, quantity, sale_price):
    """Calculate gain/loss for selling quantity shares at sale_price."""
    cost, _ = calculate_fifo_cost_basis(lots, quantity)
    if cost is None:
        return None
    return quantity * sale_price - cost


def max_sellable_for_gain(lots, sale_price, target_gain):
    """
    Find maximum shares sellable to achieve exactly target_gain (FIFO).
    Uses binary search for precision.
    """
    total_available = get_total_available(lots)
    if total_available == 0:
        return 0, 0

    # Check if selling all achieves less than target
    gain_all = calculate_gain(lots, total_available, sale_price)
    if gain_all is not None and gain_all <= target_gain:
        return int(total_available), gain_all

    # Binary search
    low, high = 0, total_available
    best_n = 0
    best_gain = 0

    for _ in range(100):
        if high - low < 0.5:
            break

        mid = (low + high) / 2
        gain = calculate_gain(lots, mid, sale_price)

        if gain is None:
            high = mid
            continue

        if gain <= target_gain:
            best_n = mid
            best_gain = gain
            low = mid
        else:
            high = mid

    best_n = int(best_n)
    if best_n > 0:
        best_gain = calculate_gain(lots, best_n, sale_price)

    return best_n, best_gain


def calculate_tax(gain, yearly_limit=BASE_LIMIT):
    """
    Calculate tax on gain given the yearly limit.
    Tax is 10% on amount exceeding the limit.

    Buffer rule: If you go up to €1,000 over the limit and tax would be < €1,000,
    you can use the buffer zone.
    """
    if gain <= yearly_limit:
        return 0, 0  # No tax, no taxable amount

    taxable = gain - yearly_limit
    tax = taxable * TAX_RATE

    # Buffer zone: if tax < €1,000 and excess < €1,000 buffer, still allowed
    # But once tax >= €1,000, buffer resets
    return taxable, tax


def calculate_tax_with_buffer(gain, buffer_available=BUFFER_ZONE):
    """
    Calculate tax considering the €1,000 buffer zone.
    Returns: (taxable, tax, buffer_used, buffer_remaining)
    """
    if gain <= BASE_LIMIT:
        return 0, 0, 0, buffer_available

    excess = gain - BASE_LIMIT

    # Can we use the buffer?
    if excess <= buffer_available:
        # Within buffer - no tax, but buffer is used
        return 0, 0, excess, buffer_available - excess

    # Exceeded buffer - full tax on everything above limit
    taxable = excess
    tax = taxable * TAX_RATE

    # If tax >= €1,000, buffer resets to 0 for this calculation
    buffer_remaining = 0 if tax >= BUFFER_ZONE else BUFFER_ZONE - excess

    return taxable, tax, excess, max(0, buffer_remaining)


def shares_for_target_revenue(lots, sale_price, target_revenue):
    """
    Calculate how many shares to sell to get target_revenue (before tax).
    Returns (shares, actual_revenue, gain, cost_basis)
    """
    shares_needed = target_revenue / sale_price
    total_available = get_total_available(lots)

    if shares_needed > total_available:
        shares_needed = total_available

    shares_needed = int(shares_needed)
    if shares_needed <= 0:
        return 0, 0, 0, 0

    cost, _ = calculate_fifo_cost_basis(lots, shares_needed)
    if cost is None:
        return 0, 0, 0, 0

    actual_revenue = shares_needed * sale_price
    gain = actual_revenue - cost

    return shares_needed, actual_revenue, gain, cost


# =============================================================================
# MULTI-YEAR PLANNING
# =============================================================================

def calculate_yearly_limits(years):
    """
    Calculate tax limits for multiple years.
    With simplified rules: €10,000 limit each year (no carryover).
    """
    return [{'year': y + 1, 'limit': BASE_LIMIT} for y in range(years)]


def plan_multi_year_sales(lots, sale_price, years, price_increase=0.05):
    """
    Create multi-year sales plan.
    With simplified rules: €10,000 tax-free each year, no carryover.
    Sells up to €10,000 in gains each year.
    """
    plan = []
    remaining_lots = [lot.copy() for lot in lots]

    for lot in remaining_lots:
        if 'remaining' not in lot:
            lot['remaining'] = lot['quantity']

    total_sold = 0
    total_revenue = 0
    total_tax = 0

    for year in range(years):
        current_price = sale_price * ((1 + price_increase) ** year)
        available = get_total_available(remaining_lots)

        if available <= 0:
            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': 0,
                'revenue': 0,
                'gain': 0,
                'taxable': 0,
                'tax': 0,
                'net': 0,
                'remaining': 0,
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })
            continue

        # Find max units to sell within €10,000 limit
        units, gain = max_sellable_for_gain(remaining_lots, current_price, BASE_LIMIT)

        if units > 0:
            to_sell = units
            cost_basis = 0
            for lot in remaining_lots:
                if to_sell <= 0:
                    break
                avail = lot.get('remaining', 0)
                if avail <= 0:
                    continue
                use = min(to_sell, avail)
                cost_basis += use * lot['price']
                lot['remaining'] -= use
                to_sell -= use

            revenue = units * current_price
            taxable, tax = calculate_tax(gain, BASE_LIMIT)
            net = revenue - tax

            total_sold += units
            total_revenue += revenue
            total_tax += tax

            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': units,
                'revenue': revenue,
                'cost_basis': cost_basis,
                'gain': gain,
                'taxable': taxable,
                'tax': tax,
                'net': net,
                'remaining': get_total_available(remaining_lots),
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })
        else:
            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': 0,
                'revenue': 0,
                'gain': 0,
                'taxable': 0,
                'tax': 0,
                'net': 0,
                'remaining': available,
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })

    return plan


def plan_full_extraction(lots, sale_price, years, price_increase=0.05):
    """
    Plan to extract everything over the given years.
    Sells €10,000 in gains each year, then dumps rest in last year.
    """
    plan = []
    remaining_lots = [lot.copy() for lot in lots]

    for lot in remaining_lots:
        if 'remaining' not in lot:
            lot['remaining'] = lot['quantity']

    total_sold = 0
    total_revenue = 0
    total_tax = 0

    for year in range(years):
        current_price = sale_price * ((1 + price_increase) ** year)
        available = get_total_available(remaining_lots)

        if available <= 0:
            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': 0,
                'revenue': 0,
                'gain': 0,
                'taxable': 0,
                'tax': 0,
                'net': 0,
                'remaining': 0,
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })
            continue

        is_last_year = (year == years - 1)

        if is_last_year:
            # Last year: sell everything remaining
            units = int(available)
        else:
            # Sell up to €10,000 in gains
            units, _ = max_sellable_for_gain(remaining_lots, current_price, BASE_LIMIT)

        if units > 0:
            to_sell = units
            cost_basis = 0
            for lot in remaining_lots:
                if to_sell <= 0:
                    break
                avail = lot.get('remaining', 0)
                if avail <= 0:
                    continue
                use = min(to_sell, avail)
                cost_basis += use * lot['price']
                lot['remaining'] -= use
                to_sell -= use

            revenue = units * current_price
            gain = revenue - cost_basis
            taxable, tax = calculate_tax(gain, BASE_LIMIT)
            net = revenue - tax

            total_sold += units
            total_revenue += revenue
            total_tax += tax

            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': units,
                'revenue': revenue,
                'cost_basis': cost_basis,
                'gain': gain,
                'taxable': taxable,
                'tax': tax,
                'net': net,
                'remaining': get_total_available(remaining_lots),
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })
        else:
            plan.append({
                'year': year + 1,
                'price': current_price,
                'limit': BASE_LIMIT,
                'units': 0,
                'revenue': 0,
                'gain': 0,
                'taxable': 0,
                'tax': 0,
                'net': 0,
                'remaining': available,
                'cumulative_sold': total_sold,
                'cumulative_revenue': total_revenue,
                'cumulative_tax': total_tax
            })

    return plan


# =============================================================================
# AUTHENTICATION DECORATOR
# =============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Log in om deze pagina te bekijken.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# ROUTES - AUTHENTICATION
# =============================================================================

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')

        if not username or not email or not password:
            flash('Alle velden zijn verplicht.', 'error')
            return render_template('register.html')

        if len(password) < 8:
            flash('Wachtwoord moet minimaal 8 tekens bevatten.', 'error')
            return render_template('register.html')

        if password != confirm:
            flash('Wachtwoorden komen niet overeen.', 'error')
            return render_template('register.html')

        if User.query.filter_by(username=username).first():
            flash('Gebruikersnaam is al in gebruik.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('E-mailadres is al geregistreerd.', 'error')
            return render_template('register.html')

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)

        portfolio = Portfolio(name='Mijn Portfolio', owner=user)
        db.session.add(portfolio)
        db.session.commit()

        flash('Account aangemaakt! Je kunt nu inloggen.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            flash(f'Welkom terug, {user.username}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Ongeldige gebruikersnaam of wachtwoord.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Je bent uitgelogd.', 'info')
    return redirect(url_for('index'))


# =============================================================================
# ROUTES - USER SETTINGS
# =============================================================================

@app.route('/settings')
@login_required
def settings():
    user = User.query.get(session['user_id'])
    return render_template('settings.html', user=user)


@app.route('/settings/password', methods=['POST'])
@login_required
def change_password():
    user = User.query.get(session['user_id'])
    current = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    if not user.check_password(current):
        flash('Huidig wachtwoord is onjuist.', 'error')
        return redirect(url_for('settings'))

    if len(new_password) < 8:
        flash('Nieuw wachtwoord moet minimaal 8 tekens bevatten.', 'error')
        return redirect(url_for('settings'))

    if new_password != confirm:
        flash('Nieuwe wachtwoorden komen niet overeen.', 'error')
        return redirect(url_for('settings'))

    user.set_password(new_password)
    db.session.commit()
    flash('Wachtwoord succesvol gewijzigd.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/email', methods=['POST'])
@login_required
def change_email():
    user = User.query.get(session['user_id'])
    new_email = request.form.get('new_email', '').strip().lower()
    password = request.form.get('password', '')

    if not user.check_password(password):
        flash('Wachtwoord is onjuist.', 'error')
        return redirect(url_for('settings'))

    if User.query.filter_by(email=new_email).first():
        flash('Dit e-mailadres is al in gebruik.', 'error')
        return redirect(url_for('settings'))

    user.email = new_email
    db.session.commit()
    flash('E-mailadres succesvol gewijzigd.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/delete', methods=['POST'])
@login_required
def delete_account():
    user = User.query.get(session['user_id'])
    password = request.form.get('password', '')

    if not user.check_password(password):
        flash('Wachtwoord is onjuist.', 'error')
        return redirect(url_for('settings'))

    db.session.delete(user)
    db.session.commit()
    session.clear()
    flash('Account verwijderd.', 'info')
    return redirect(url_for('index'))


# =============================================================================
# ROUTES - DASHBOARD & PORTFOLIO
# =============================================================================

@app.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    return render_template('dashboard.html', user=user)


@app.route('/portfolio/<int:portfolio_id>')
@login_required
def view_portfolio(portfolio_id):
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    if portfolio.user_id != session['user_id']:
        flash('Geen toegang tot dit portfolio.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('portfolio.html', portfolio=portfolio)


@app.route('/portfolio/create', methods=['POST'])
@login_required
def create_portfolio():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Naam is verplicht.', 'error')
        return redirect(url_for('dashboard'))

    portfolio = Portfolio(name=name, user_id=session['user_id'])
    db.session.add(portfolio)
    db.session.commit()

    flash(f'Portfolio "{name}" aangemaakt.', 'success')
    return redirect(url_for('view_portfolio', portfolio_id=portfolio.id))


@app.route('/portfolio/<int:portfolio_id>/delete', methods=['POST'])
@login_required
def delete_portfolio(portfolio_id):
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    if portfolio.user_id != session['user_id']:
        flash('Geen toegang.', 'error')
        return redirect(url_for('dashboard'))

    db.session.delete(portfolio)
    db.session.commit()
    flash('Portfolio verwijderd.', 'success')
    return redirect(url_for('dashboard'))


# =============================================================================
# ROUTES - ASSET MANAGEMENT
# =============================================================================

@app.route('/portfolio/<int:portfolio_id>/asset/add', methods=['POST'])
@login_required
def add_asset(portfolio_id):
    portfolio = Portfolio.query.get_or_404(portfolio_id)
    if portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    name = request.form.get('name', '').strip()
    isin = request.form.get('isin', '').strip()

    if not name:
        flash('Naam is verplicht.', 'error')
        return redirect(url_for('view_portfolio', portfolio_id=portfolio_id))

    asset = Asset(name=name, isin=isin, portfolio_id=portfolio_id)
    asset.set_lots([])
    db.session.add(asset)
    db.session.commit()

    flash(f'Asset "{name}" toegevoegd.', 'success')
    return redirect(url_for('view_portfolio', portfolio_id=portfolio_id))


@app.route('/asset/<int:asset_id>/lot/add', methods=['POST'])
@login_required
def add_lot(asset_id):
    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    try:
        # Check if using "amount only" mode (1 unit at the given amount)
        amount_only = request.form.get('amount_only') == 'true'

        if amount_only:
            amount = float(request.form.get('amount', 0))
            if amount <= 0:
                flash('Bedrag moet positief zijn.', 'error')
                return redirect(url_for('view_portfolio', portfolio_id=asset.portfolio_id))
            quantity = 1
            price = amount
        else:
            quantity = float(request.form.get('quantity', 0))
            price = float(request.form.get('price', 0))
            if quantity <= 0 or price <= 0:
                flash('Aantal en prijs moeten positief zijn.', 'error')
                return redirect(url_for('view_portfolio', portfolio_id=asset.portfolio_id))

        date = request.form.get('date', datetime.now().strftime('%Y-%m-%d'))

        lots = asset.get_lots()
        lots.append({
            'date': date,
            'quantity': quantity,
            'price': price,
            'remaining': quantity
        })
        lots.sort(key=lambda x: x['date'])
        asset.set_lots(lots)
        db.session.commit()

        if amount_only:
            flash(f'Inleg van €{price:.2f} toegevoegd.', 'success')
        else:
            flash(f'Aankoop van {quantity:.0f} stuks @ €{price:.2f} toegevoegd.', 'success')

    except ValueError:
        flash('Ongeldige invoer.', 'error')

    return redirect(url_for('view_portfolio', portfolio_id=asset.portfolio_id))


@app.route('/asset/<int:asset_id>/delete', methods=['POST'])
@login_required
def delete_asset(asset_id):
    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    portfolio_id = asset.portfolio_id
    db.session.delete(asset)
    db.session.commit()

    flash('Asset verwijderd.', 'success')
    return redirect(url_for('view_portfolio', portfolio_id=portfolio_id))


# =============================================================================
# ROUTES - CALCULATOR
# =============================================================================

@app.route('/calculator/<int:asset_id>')
@login_required
def calculator(asset_id):
    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        flash('Geen toegang.', 'error')
        return redirect(url_for('dashboard'))

    lots = asset.get_lots()
    total_available = get_total_available(lots)
    total_cost = get_total_cost(lots)

    return render_template('calculator.html',
                         asset=asset,
                         lots=lots,
                         total_available=total_available,
                         total_cost=total_cost,
                         base_limit=BASE_LIMIT,
                         tax_rate=TAX_RATE * 100,
                         buffer_zone=BUFFER_ZONE)


@app.route('/api/calculate', methods=['POST'])
@login_required
def api_calculate():
    """API endpoint for single-year calculations."""
    data = request.get_json()
    asset_id = data.get('asset_id')
    sale_price = float(data.get('sale_price', 0))
    quantity = data.get('quantity')
    target_revenue = data.get('target_revenue')
    yearly_limit = float(data.get('yearly_limit', BASE_LIMIT))

    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    lots = asset.get_lots()
    total_available = get_total_available(lots)
    total_cost = get_total_cost(lots)

    if total_available == 0:
        return jsonify({'error': 'Geen beschikbare aandelen'}), 400

    results = {
        'total_available': int(total_available),
        'total_cost': total_cost,
        'sale_price': sale_price,
        'yearly_limit': yearly_limit,
        'tax_rate': TAX_RATE
    }

    # Scenario 1: Maximum within limit (stay under limit)
    max_units, max_gain = max_sellable_for_gain(lots, sale_price, yearly_limit)
    if max_units > 0:
        cost, _ = calculate_fifo_cost_basis(lots, max_units)
        revenue = max_units * sale_price
        results['within_limit'] = {
            'units': max_units,
            'cost_basis': cost,
            'revenue': revenue,
            'gain': max_gain,
            'taxable': 0,
            'tax': 0,
            'net': revenue
        }

    # Scenario 2: Sell all
    if total_available > 0:
        cost_all, _ = calculate_fifo_cost_basis(lots, total_available)
        revenue_all = total_available * sale_price
        gain_all = revenue_all - cost_all
        taxable_all, tax_all = calculate_tax(gain_all, yearly_limit)
        results['sell_all'] = {
            'units': int(total_available),
            'cost_basis': cost_all,
            'revenue': revenue_all,
            'gain': gain_all,
            'taxable': taxable_all,
            'tax': tax_all,
            'net': revenue_all - tax_all
        }

    # Scenario 3: Specific quantity
    if quantity and 0 < quantity <= total_available:
        quantity = int(quantity)
        cost_q, _ = calculate_fifo_cost_basis(lots, quantity)
        revenue_q = quantity * sale_price
        gain_q = revenue_q - cost_q
        taxable_q, tax_q = calculate_tax(gain_q, yearly_limit)
        results['custom_quantity'] = {
            'units': quantity,
            'cost_basis': cost_q,
            'revenue': revenue_q,
            'gain': gain_q,
            'taxable': taxable_q,
            'tax': tax_q,
            'net': revenue_q - tax_q
        }

    # Scenario 4: Target revenue (money to extract)
    if target_revenue and target_revenue > 0:
        units_rev, actual_rev, gain_rev, cost_rev = shares_for_target_revenue(lots, sale_price, target_revenue)
        if units_rev > 0:
            taxable_rev, tax_rev = calculate_tax(gain_rev, yearly_limit)
            results['target_revenue'] = {
                'units': units_rev,
                'cost_basis': cost_rev,
                'revenue': actual_rev,
                'gain': gain_rev,
                'taxable': taxable_rev,
                'tax': tax_rev,
                'net': actual_rev - tax_rev
            }

    return jsonify(results)


@app.route('/api/multi-year-plan', methods=['POST'])
@login_required
def api_multi_year_plan():
    """API endpoint for multi-year planning."""
    data = request.get_json()
    asset_id = data.get('asset_id')
    sale_price = float(data.get('sale_price', 0))
    years = int(data.get('years', 3))
    price_increase = float(data.get('price_increase', 5)) / 100
    full_extraction = data.get('full_extraction', False)

    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    lots = asset.get_lots()

    if full_extraction:
        plan = plan_full_extraction(lots, sale_price, years, price_increase)
    else:
        plan = plan_multi_year_sales(lots, sale_price, years, price_increase)

    return jsonify({
        'plan': plan,
        'full_extraction': full_extraction,
        'years': years,
        'price_increase': price_increase * 100,
        'base_limit': BASE_LIMIT,
        'tax_rate': TAX_RATE * 100
    })


@app.route('/api/chart-data', methods=['POST'])
@login_required
def api_chart_data():
    """API endpoint for chart data."""
    data = request.get_json()
    asset_id = data.get('asset_id')
    min_price = float(data.get('min_price', 10))
    max_price = float(data.get('max_price', 200))
    steps = int(data.get('steps', 50))
    yearly_limit = float(data.get('yearly_limit', BASE_LIMIT))

    asset = Asset.query.get_or_404(asset_id)
    if asset.portfolio.user_id != session['user_id']:
        return jsonify({'error': 'Geen toegang'}), 403

    lots = asset.get_lots()
    total_available = get_total_available(lots)
    total_cost = get_total_cost(lots)

    if total_available == 0:
        return jsonify({'error': 'Geen data'}), 400

    step_size = (max_price - min_price) / steps
    analysis = []

    for i in range(steps + 1):
        price = min_price + i * step_size

        # Max within limit
        max_units, max_gain = max_sellable_for_gain(lots, price, yearly_limit)

        # Full sale
        gain_all = total_available * price - total_cost
        taxable_all, tax_all = calculate_tax(gain_all, yearly_limit)

        analysis.append({
            'price': round(price, 2),
            'max_units_within_limit': max_units,
            'gain_at_max': round(max_gain, 2) if max_gain else 0,
            'gain_if_sell_all': round(gain_all, 2),
            'tax_if_sell_all': round(tax_all, 2),
            'net_if_sell_all': round(total_available * price - tax_all, 2)
        })

    # Break-even price
    break_even = total_cost / total_available if total_available > 0 else 0

    return jsonify({
        'analysis': analysis,
        'break_even': round(break_even, 2),
        'total_available': int(total_available),
        'total_cost': round(total_cost, 2),
        'yearly_limit': yearly_limit
    })


# =============================================================================
# DATABASE INITIALIZATION
# =============================================================================

def init_db():
    with app.app_context():
        db.create_all()


# Initialize database on import (for gunicorn)
init_db()


if __name__ == '__main__':
    # Development mode
    app.run(debug=True, host='0.0.0.0', port=5000)
