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
    try:
        result = shopify_api_call(f'products/{sample_product_id}.json')
        if not result:
            print(f"✗ Could not fetch sample product {sample_product_id}")
            return None

        sample = result.get('product', {})
        base_title = sample.get('title', '')
        serial_only = serial.replace('LCK-', '')
        new_title = f"{base_title}-{serial_only}"

        original_tags = sample.get('tags', '')
        new_tags = swap_tags(original_tags)
        
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
            return {
                'product_id': new_product_id,
                'title': new_title
            }
        return None
    except Exception as e:
        print(f"✗ Error creating product: {e}")
        return None

def add_serial_to_order_note(order_id, serial):
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result: return False
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        note_addition = f"\nSerial Number: {serial}"
        new_note = f"{current_note}{note_addition}" if current_note else note_addition.strip()
        shopify_api_call(f'orders/{order_id}.json', method='PUT', data={'order': {'note': new_note}})
        return True
    except: return False

def try_acquire_processing_lock(order_id):
    try:
        result = shopify_api_call(f'orders/{order_id}/metafields.json?namespace=webhook&key=processing_lock')
        if result and result.get('metafields'):
            return False
        lock_data = {
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_lock',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        }
        shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=lock_data)
        return True
    except: return False

def mark_order_as_completed(order_id):
    completed_data = {
        'metafield': {
            'namespace': 'webhook',
            'key': 'processing_completed',
            'value': datetime.now().isoformat(),
            'type': 'single_line_text_field'
        }
    }
    shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=completed_data)

def process_order(order_data, force=False, inventory_qty=1):
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')

    if not force:
        if not try_acquire_processing_lock(order_id):
            return {'status': 'already_processing', 'order': order_number}

    try:
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        products_created = []
        serials_assigned = []

        for item in order_data.get('line_items', []):
            product_id = item.get('product_id')
            product_title = item.get('title', '')
            sku = item.get('sku', '')

            # REPAIR: Determine quantity based on current vs original
            current_qty = item.get('current_quantity', item.get('quantity', 1))

            if current_qty == 0:
                print(f"⏭️ Skipping removed item: {product_title}")
                continue

            if not (sku and sku.upper().startswith('LCK-')):
                continue

            # Process each unit of the clock
            for i in range(current_qty):
                serial = get_next_serial()
                if not serial: continue

                new_product = create_product_from_sample(product_id, serial, force=force, inventory_qty=inventory_qty)
                if new_product:
                    serials_assigned.append(serial)
                    products_created.append(new_product['title'])
                    log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])

        for serial in serials_assigned:
            add_serial_to_order_note(order_id, serial)

        if not force:
            mark_order_as_completed(order_id)

        return {'status': 'success', 'products': products_created, 'serials': serials_assigned, 'order': order_number}

    except Exception as e:
        print(f"ERROR: {e}")
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
  .card { background: #fff; border: 1px solid #e1e4e8; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
  label { font-weight: 600; font-size: .9rem; display: block; margin-bottom: 6px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccc; border-radius: 8px; font-size: 1rem; }
  button { margin-top: 12px; padding: 10px 20px; background: #0b61d8; color: #fff; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; margin-top: 15px; }
  th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; font-size: .9rem; }
  th { background: #f7f9fc; }
  .removed-row td { text-decoration: line-through; color: #999; background: #fffafb; }
  .removed-label { color: #d93025; font-weight: bold; text-decoration: none !important; display: inline-block; }
  .warn { background: #fff8e1; border: 1px solid #ffe082; border-radius: 8px; padding: 12px; margin-top: 15px; font-size: .9rem; color: #7a5800; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .75rem; background: #e8f0fe; color: #1a56db; margin-right: 4px; }
  .tag.sample { background: #fce8e6; color: #c0392b; }
  .log { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 14px; font-family: monospace; font-size: .85rem; white-space: pre-wrap; display: none; margin-top: 16px; }
</style>
</head>
<body>
<h1>⚡ Manual Webhook Trigger</h1>
<p style="color:#666">Items with 0 quantity (removed during edit) will be skipped automatically.</p>

<div class="card">
  <label>Order Number</label>
  <input type="text" id="orderInput" placeholder="2882">
  
  <div style="margin-top:15px;">
    <label>Build Type</label>
    <label style="font-weight:400;"><input type="radio" name="buildType" value="1"> Stock build (Qty 1)</label>
    <label style="font-weight:400;"><input type="radio" name="buildType" value="0" checked> Customer order (Qty 0)</label>
  </div>

  <button id="lookupBtn">Look Up Order</button>

  <div id="orderInfo" style="display:none">
    <table>
      <thead><tr><th>Product</th><th>SKU</th><th>Tags</th><th>Qty</th></tr></thead>
      <tbody id="itemsBody"></tbody>
    </table>
    <div class="warn" id="warnBox"></div>
    <button style="background:#c0392b" id="fireBtn">🔥 Force Process This Order</button>
    <div class="log" id="logBox"></div>
  </div>
</div>

<script>
let currentOrderId = null;

async function lookup() {
  const val = document.getElementById('orderInput').value.trim().replace('#','');
  if (!val) return;
  const btn = document.getElementById('lookupBtn');
  btn.disabled = true;
  btn.textContent = 'Searching...';
  
  try {
    const resp = await fetch('/api/lookup?order=' + val);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);

    currentOrderId = data.order_id;
    const tbody = document.getElementById('itemsBody');
    tbody.innerHTML = '';
    
    let validItems = 0;
    data.items.forEach(item => {
      const isRemoved = item.current_qty === 0;
      if(!isRemoved) validItems++;
      
      const tr = document.createElement('tr');
      if (isRemoved) tr.className = 'removed-row';
      
      tr.innerHTML = `
        <td>${item.title}</td>
        <td>${item.sku}</td>
        <td>${item.tags.map(t => `<span class="tag ${t==='sample'?'sample':''}">${t}</span>`).join('')}</td>
        <td>${isRemoved ? '<span class="removed-label">REMOVED (0)</span>' : item.current_qty}</td>
      `;
      tbody.appendChild(tr);
    });

    document.getElementById('fireBtn').disabled = validItems === 0;
    document.getElementById('orderInfo').style.display = 'block';
  } catch(e) { alert(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Look Up Order'; }
}

async function fire() {
  const btn = document.getElementById('fireBtn');
  const log = document.getElementById('logBox');
  btn.disabled = true;
  log.style.display = 'block';
  log.textContent = 'Processing...';

  try {
    const resp = await fetch('/api/manual', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        order_id: currentOrderId,
        inventory_qty: parseInt(document.querySelector('input[name=buildType]:checked').value)
      })
    });
    const res = await resp.json();
    log.textContent = JSON.stringify(res, null, 2);
    btn.textContent = '✓ Complete';
  } catch(e) { log.textContent = 'Error: ' + e.message; btn.disabled = false; }
}

document.getElementById('lookupBtn').addEventListener('click', lookup);
document.getElementById('fireBtn').addEventListener('click', fire);
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        p = urlparse(self.path)
        if p.path in ('/', '/api', '/api/'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(MANUAL_TRIGGER_HTML.encode())
        elif p.path == '/api/lookup':
            order_num = parse_qs(p.query).get('order', [''])[0]
            res = shopify_api_call(f'orders.json?name=%23{order_num}&status=any')
            if not res or not res.get('orders'): return self.send_json(404, {'error': 'Order not found'})
            
            order = res['orders'][0]
            items = []
            for item in order.get('line_items', []):
                pr = shopify_api_call(f"products/{item.get('product_id')}.json")
                tags = [t.strip().lower() for t in pr.get('product', {}).get('tags', '').split(',')] if pr else []
                items.append({
                    'title': item.get('title'),
                    'sku': item.get('sku'),
                    'current_qty': item.get('current_quantity', item.get('quantity', 1)),
                    'tags': tags
                })
            self.send_json(200, {'order_id': order['id'], 'items': items})

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        payload = json.loads(self.rfile.read(content_length))
        
        if self.path == '/api/manual':
            order_res = shopify_api_call(f"orders/{payload.get('order_id')}.json")
            result = process_order(order_res['order'], force=True, inventory_qty=int(payload.get('inventory_qty', 1)))
            self.send_json(200, result)
        else:
            result = process_order(payload, force=False, inventory_qty=0)
            self.send_json(200, result)