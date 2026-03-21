# Summary: When a customer orders a "sample" tagged clock, this webhook automatically:
# 1. Generates a unique serial number (LCK-####)
# 2. Creates a new Shopify product with serial in name (e.g., "Elena-1028")
# 3. Copies photos, description, price from sample product
# 4. Strips "sample" tag, adds "featured" to new product
# 5. Logs to Clocksheet with hyperlink to new product
# 6. Adds serial number to order notes
# 7. Prevents duplicate processing with atomic locking

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# Load configuration from environment variables
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

def get_google_sheet():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet = spreadsheet.worksheet('Clocks')
        return sheet
    except Exception as e:
        print(f"Sheet error: {e}")
        return None

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date, product_id):
    try:
        sheet = get_google_sheet()
        if not sheet:
            return False

        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''

        serial_number_only = serial.replace('LCK-', '')
        product_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{product_id}"

        row = [
            serial_number_only, name_part, description_part,
            '', order_number, '', '', '', '', order_date,
            '', '', '', '', '', '', '', '', '', '', '', '', '', ''
        ]

        sheet.insert_row(row, index=2)

        try:
            sheet.update_cell(2, 2, f'=HYPERLINK("{product_url}", "{name_part}")')
            print(f"✓ Logged to sheet with hyperlink: {serial}")
        except Exception as e:
            print(f"⚠️ Logged to sheet but hyperlink failed: {e}")

        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2026-01/{endpoint}"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json'
    }
    req_data = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"✗ API Error: {e}")
        return None

def get_next_serial():
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    if not result:
        return None
    metafields = result.get('metafields', [])
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        serial = f"LCK-{current}"
        next_val = current + 1
        update_data = {
            'metafield': {
                'id': metafield_id,
                'value': str(next_val),
                'type': 'number_integer'
            }
        }
        shopify_api_call(f'metafields/{metafield_id}.json', method='PUT', data=update_data)
        return serial
    return None

def swap_tags(tags_string):
    """Remove 'sample' tag, add 'featured' tag."""
    tags = [t.strip() for t in tags_string.split(',') if t.strip()]
    tags = [t for t in tags if t.lower() != 'sample']
    if 'featured' not in [t.lower() for t in tags]:
        tags.append('featured')
    return ', '.join(tags)

def create_product_from_sample(sample_product_id, serial, force=False, inventory_qty=1):
    """Create a new product based on the sample product with serial in name.
    If force=True, skip the sample tag check (for manual trigger UI).
    """
    try:
        result = shopify_api_call(f'products/{sample_product_id}.json')
        if not result:
            print(f"✗ Could not fetch sample product {sample_product_id}")
            return None

        sample = result.get('product', {})
        base_title = sample.get('title', '')
        serial_only = serial.replace('LCK-', '')
        new_title = f"{base_title}-{serial_only}"

        # Build new tags: strip 'sample', add 'featured'
        original_tags = sample.get('tags', '')
        new_tags = swap_tags(original_tags)
        print(f"✓ Tags: '{original_tags}' → '{new_tags}'")

        images = [{'src': img.get('src')} for img in sample.get('images', [])]

        variants = sample.get('variants', [])
        price = variants[0].get('price') if variants else '0.00'

        new_product = {
            'product': {
                'title': new_title,
                'body_html': sample.get('body_html', ''),
                'vendor': sample.get('vendor', ''),
                'product_type': 'Wall Clocks',
                'tags': new_tags,
                'status': 'active',
                'published': True,
                'images': images,
                'variants': [
                    {
                        'price': price,
                        'sku': serial,
                        'inventory_management': 'shopify',
                        'inventory_quantity': inventory_qty
                    }
                ]
            }
        }

        result = shopify_api_call('products.json', method='POST', data=new_product)

        if result and result.get('product'):
            new_product_id = result['product']['id']
            new_variant_id = result['product']['variants'][0]['id']
            print(f"✓ Created new product: {new_title} (ID: {new_product_id})")
            return {
                'product_id': new_product_id,
                'variant_id': new_variant_id,
                'title': new_title
            }
        else:
            print(f"✗ Failed to create product")
            return None

    except Exception as e:
        print(f"✗ Error creating product: {e}")
        import traceback
        traceback.print_exc()
        return None

def add_serial_to_order_note(order_id, serial):
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        note_addition = f"\nSerial Number: {serial}"
        new_note = f"{current_note}{note_addition}" if current_note else note_addition.strip()
        update_data = {'order': {'note': new_note}}
        result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
        if result:
            print(f"✓ Added serial to order notes: {serial}")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding serial to notes: {e}")
        return False

def try_acquire_processing_lock(order_id):
    try:
        result = shopify_api_call(f'orders/{order_id}/metafields.json?namespace=webhook&key=processing_lock')
        if result and result.get('metafields'):
            existing_lock = result['metafields'][0]
            lock_time = existing_lock.get('value', '')
            print(f"⏭️ Processing lock exists (created at {lock_time}) - skipping")
            return False
        lock_data = {
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_lock',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        }
        result = shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=lock_data)
        if result:
            print(f"✓ Acquired processing lock")
            return True
        else:
            print(f"✗ Failed to acquire lock")
            return False
    except Exception as e:
        print(f"✗ Error checking/acquiring lock: {e}")
        return False

def mark_order_as_completed(order_id):
    try:
        completed_data = {
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_completed',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        }
        result = shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=completed_data)
        if result:
            print(f"✓ Marked order as completed")
            return True
        return False
    except Exception as e:
        print(f"✗ Error marking as completed: {e}")
        return False

def process_order(order_data, force=False, inventory_qty=1):
    """Process order data. If force=True, bypass sample tag check and processing lock."""
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')

    if not force:
        if not try_acquire_processing_lock(order_id):
            return {
                'status': 'already_processing',
                'order': order_number,
                'message': 'Order is already being processed by another webhook instance'
            }

    try:
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()

        created_at = order_data.get('created_at', '')
        try:
            order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except:
            order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(f"Processing order {order_number} (ID: {order_id}) force={force}")

        products_created = []
        serials_assigned = []

        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            product_id = item.get('product_id')

            print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")

            if not (sku and sku.upper().startswith('LCK-')):
                print(f"⏭️ Skipping non-clock item: {sku}")
                continue

            if not force:
                product_result = shopify_api_call(f'products/{product_id}.json')
                if product_result:
                    tags = product_result.get('product', {}).get('tags', '')
                    tags_list = [tag.strip().lower() for tag in tags.split(',')]

                    if 'featured' in tags_list and 'sample' in tags_list:
                        print(f"⚠️ WARNING: Product has BOTH 'featured' and 'sample' tags - skipping")
                        continue
                    if 'featured' in tags_list:
                        print(f"⏭️ Skipping - product tagged 'featured'")
                        continue
                    if 'sample' not in tags_list:
                        print(f"⏭️ Skipping - product not tagged 'sample'")
                        continue
                else:
                    print(f"⚠️ Could not fetch product tags - skipping for safety")
                    continue

            for i in range(quantity):
                print(f"Processing item {i+1} of {quantity}...")
                serial = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue

                print(f"✓ Generated serial: {serial}")
                serials_assigned.append(serial)

                new_product = create_product_from_sample(product_id, serial, force=force, inventory_qty=inventory_qty)
                if not new_product:
                    print("✗ Failed to create product")
                    continue

                products_created.append(new_product['title'])
                log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])

        if serials_assigned:
            for serial in serials_assigned:
                add_serial_to_order_note(order_id, serial)

        if not force:
            mark_order_as_completed(order_id)

        print(f"COMPLETE - {len(products_created)} products created")

        return {
            'status': 'success',
            'products': products_created,
            'serials': serials_assigned,
            'order': order_number
        }

    except Exception as e:
        print(f"ERROR during processing: {e}")
        import traceback
        traceback.print_exc()
        raise

# ── Manual trigger UI ────────────────────────────────────────────────────────

MANUAL_TRIGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Manual Webhook Trigger</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; background: #f6f8fa; color: #111; }
  h1 { margin: 0 0 4px; font-size: 1.4rem; }
  .sub { color: #666; margin-bottom: 24px; font-size: .95rem; }
  .card { background: #fff; border: 1px solid #e1e4e8; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
  label { font-weight: 600; font-size: .9rem; display: block; margin-bottom: 6px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 9px 12px; border: 1px solid #ccc; border-radius: 8px; font-size: 1rem; }
  button { margin-top: 12px; padding: 10px 20px; background: #0b61d8; color: #fff; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: .6; cursor: not-allowed; }
  .order-info { display: none; margin-top: 16px; }
  .order-info table { width: 100%; border-collapse: collapse; }
  .order-info th, .order-info td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; font-size: .9rem; }
  .order-info th { background: #f7f9fc; font-weight: 600; color: #444; }
  .warn { background: #fff8e1; border: 1px solid #ffe082; border-radius: 8px; padding: 10px 14px; margin-top: 12px; font-size: .9rem; color: #7a5800; }
  .fire-btn { background: #c0392b; }
  .log { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 14px; font-family: monospace; font-size: .85rem; white-space: pre-wrap; display: none; margin-top: 16px; max-height: 300px; overflow-y: auto; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .8rem; background: #e8f0fe; color: #1a56db; margin: 2px; }
  .tag.sample { background: #fce8e6; color: #c0392b; }
  .tag.featured { background: #e6f4ea; color: #1e7e34; }
</style>
</head>
<body>
<h1>⚡ Manual Webhook Trigger</h1>
<p class="sub">Force-process an order that wasn't tagged sample — creates product + serial + sheet entry.</p>

<div class="card">
  <label for="orderInput">Order Number</label>
  <input type="text" id="orderInput" placeholder="#2531 or 2531">

  <div style="margin-top:14px;">
    <label style="font-weight:600; font-size:.9rem;">Build Type</label>
    <div style="display:flex; gap:12px; margin-top:6px;">
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="1">
        <span><strong>Stock build</strong> — I ordered it, qty 1, appears on site</span>
      </label>
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="0" checked>
        <span><strong>Customer order</strong> — already sold, qty 0, hidden from site</span>
      </label>
    </div>
  </div>

  <button id="lookupBtn">Look Up Order</button>

  <div class="order-info" id="orderInfo">
    <table id="itemsTable">
      <thead><tr><th>Product</th><th>SKU</th><th>Tags</th><th>Qty</th></tr></thead>
      <tbody id="itemsBody"></tbody>
    </table>
    <div class="warn" id="warnBox"></div>
    <button class="fire-btn" id="fireBtn">🔥 Force Process This Order</button>
    <div class="log" id="logBox"></div>
  </div>
</div>

<script>
let currentOrderId = null;

async function lookup() {
  const raw = document.getElementById('orderInput').value.trim().replace(/[^0-9]/g, '');
  if (!raw) return;
  document.getElementById('lookupBtn').disabled = true;
  document.getElementById('lookupBtn').textContent = 'Looking up…';
  document.getElementById('orderInfo').style.display = 'none';

  try {
    const resp = await fetch('/api/lookup?order=' + encodeURIComponent(raw));
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);

    currentOrderId = data.order_id;
    const tbody = document.getElementById('itemsBody');
    tbody.innerHTML = '';

    for (const item of data.items) {
      const tr = document.createElement('tr');
      const tagHtml = item.tags.map(t => {
        const cls = t === 'sample' ? 'tag sample' : t === 'featured' ? 'tag featured' : 'tag';
        return `<span class="${cls}">${t}</span>`;
      }).join('');
      tr.innerHTML = `<td>${item.title}</td><td>${item.sku}</td><td>${tagHtml || '—'}</td><td>${item.quantity}</td>`;
      tbody.appendChild(tr);
    }

    const warn = document.getElementById('warnBox');
    warn.textContent = data.warning || '';
    warn.style.display = data.warning ? 'block' : 'none';

    document.getElementById('orderInfo').style.display = 'block';
    document.getElementById('logBox').style.display = 'none';
    document.getElementById('fireBtn').disabled = false;
    document.getElementById('fireBtn').textContent = '🔥 Force Process This Order';
  } catch(e) {
    alert('Error: ' + e.message);
  } finally {
    document.getElementById('lookupBtn').disabled = false;
    document.getElementById('lookupBtn').textContent = 'Look Up Order';
  }
}

async function fire() {
  if (!currentOrderId) return;
  if (!confirm('Force-process order ' + document.getElementById('orderInput').value + '? This will create a new product and sheet entry.')) return;

  const btn = document.getElementById('fireBtn');
  btn.disabled = true;
  btn.textContent = 'Processing…';
  const log = document.getElementById('logBox');
  log.style.display = 'block';
  log.textContent = 'Sending…';

  try {
    const resp = await fetch('/api/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order_id: currentOrderId, inventory_qty: parseInt(document.querySelector('input[name=buildType]:checked').value) })
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);

    log.textContent = JSON.stringify(data, null, 2);
    btn.textContent = '✓ Done';
  } catch(e) {
    log.textContent = 'Error: ' + e.message;
    btn.disabled = false;
    btn.textContent = '🔥 Force Process This Order';
  }
}

document.getElementById('lookupBtn').addEventListener('click', lookup);
document.getElementById('orderInput').addEventListener('keydown', e => { if (e.key === 'Enter') lookup(); });
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('fireBtn').addEventListener('click', fire);
});
</script>
</body>
</html>
"""

# ── Handler ──────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        # Manual trigger UI
        if parsed.path in ('/', '/api', '/api/'):
            self.send_html(MANUAL_TRIGGER_HTML)
            return

        # Order lookup for the UI
        if parsed.path == '/api/lookup':
            qs = parse_qs(parsed.query)
            order_num = (qs.get('order') or [''])[0].strip()
            if not order_num:
                self.send_json(400, {'error': 'order param required'})
                return
            try:
                # Search by order number
                result = shopify_api_call(f'orders.json?name=%23{order_num}&status=any')
                if not result or not result.get('orders'):
                    self.send_json(404, {'error': f'Order #{order_num} not found'})
                    return

                order = result['orders'][0]
                order_id = order['id']
                items = []
                has_non_sample = False

                for item in order.get('line_items', []):
                    product_id = item.get('product_id')
                    tags_list = []
                    if product_id:
                        pr = shopify_api_call(f'products/{product_id}.json')
                        if pr:
                            tags_str = pr.get('product', {}).get('tags', '')
                            tags_list = [t.strip().lower() for t in tags_str.split(',') if t.strip()]
                    sku = item.get('sku', '')
                    if sku and sku.upper().startswith('LCK-') and 'sample' not in tags_list:
                        has_non_sample = True
                    items.append({
                        'title': item.get('title', ''),
                        'sku': sku,
                        'quantity': item.get('quantity', 1),
                        'tags': tags_list
                    })

                warning = ''
                if has_non_sample:
                    warning = "⚠️ One or more clock products are not tagged 'sample' — force processing will bypass this check."

                self.send_json(200, {
                    'order_id': order_id,
                    'order_number': order.get('name'),
                    'items': items,
                    'warning': warning
                })
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            return

        # Health check
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Webhook handler v12 - tag swapping + manual trigger')

    def do_POST(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
        except Exception as e:
            self.send_json(400, {'error': f'Invalid JSON: {e}'})
            return

        # Manual force-trigger from UI
        if parsed.path == '/api/manual':
            order_id = payload.get('order_id')
            inventory_qty = int(payload.get('inventory_qty', 1))
            if not order_id:
                self.send_json(400, {'error': 'order_id required'})
                return
            try:
                result = shopify_api_call(f'orders/{order_id}.json')
                if not result:
                    self.send_json(404, {'error': 'Order not found'})
                    return
                order_data = result.get('order', {})
                result = process_order(order_data, force=True, inventory_qty=inventory_qty)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            return

        # Normal Shopify webhook
        print("=" * 60)
        print("WEBHOOK RECEIVED")
        print("=" * 60)
        try:
            result = process_order(payload, force=False, inventory_qty=0)
            self.send_json(200, result)
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            self.send_json(500, {'error': str(e)})
