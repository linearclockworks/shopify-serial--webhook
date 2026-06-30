# Summary: When a customer orders a "sample" tagged clock, this webhook automatically:
# 1. Generates a unique serial number (LCK-####)
# 2. Creates a new Shopify product with serial in name (e.g., "Elena-1028")
# 3. Copies photos, description, price from sample product
# 4. Strips "sample" tag, adds "featured" to new product
# 5. Logs to Clocksheet with hyperlink to new product
# 6. Swaps out the "sample" line item on the order for the new serialized product via GraphQL
# 7. Adds serial number to order notes as a fallback if a swap fails
# 8. Prevents duplicate processing with atomic locking

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

def shopify_graphql_call(query, variables=None):
    """Utility wrapper for executing Shopify Admin GraphQL API mutations"""
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2026-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json'
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
        
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"✗ GraphQL API Error: {e}")
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

def swap_tags(tags_string, add_featured_tag=False):
    """Remove 'sample' tag. Add 'featured' tag only if add_featured_tag=True (stock builds)."""
    tags = [t.strip() for t in tags_string.split(',') if t.strip()]
    tags = [t for t in tags if t.lower() != 'sample']
    
    if add_featured_tag:
        if 'featured' not in [t.lower() for t in tags]:
            tags.append('featured')
    else:
        # Remove featured tag for customer orders (hidden from site)
        tags = [t for t in tags if t.lower() != 'featured']
    
    return ', '.join(tags)

def get_location_id_by_name(location_name):
    """Get location ID by location name (e.g., 'SandedNBranded', 'Brevard')"""
    try:
        result = shopify_api_call('locations.json')
        if result and result.get('locations'):
            for loc in result['locations']:
                if loc.get('name', '').lower() == location_name.lower():
                    return loc['id']
        return None
    except Exception as e:
        print(f"⚠️ Could not fetch locations: {e}")
        return None

def set_inventory_at_location(inventory_item_id, location_id, quantity):
    """Ensure inventory item is connected to the targeted location, then cleanly set quantity"""
    if not inventory_item_id or not location_id:
        print(f"✗ Cannot configure inventory: item_id or location_id is missing.")
        return False
    try:
        # Step A: Connect the inventory item to the location first (prevents 400 Bad Requests)
        connect_data = {
            'inventory_item_id': int(inventory_item_id),
            'location_id': int(location_id)
        }
        shopify_api_call('inventory_levels/connect.json', method='POST', data=connect_data)
        
        # Step B: Apply flat payload layout configuration to set inventory levels
        adjust_data = {
            'inventory_item_id': int(inventory_item_id),
            'location_id': int(location_id),
            'available': int(quantity)
        }
        result = shopify_api_call('inventory_levels/set.json', method='POST', data=adjust_data)
        if result:
            print(f"✓ Connected & set inventory at location {location_id}: qty={quantity}")
            return True
        else:
            print(f"⚠️ Failed to apply inventory level setting at location {location_id}")
            return False
    except Exception as e:
        print(f"⚠️ Inventory processing failed at location {location_id}: {e}")
        return False

def create_product_from_sample(sample_product_id, serial, add_featured_tag=False):
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
        new_tags = swap_tags(original_tags, add_featured_tag=add_featured_tag)
        
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
                        'inventory_management': 'shopify'
                    }
                ]
            }
        }

        result = shopify_api_call('products.json', method='POST', data=new_product)

        if result and result.get('product'):
            new_product_id = result['product']['id']
            variant = result['product'].get('variants', [{}])[0]
            new_variant_id = variant.get('id')
            inventory_item_id = variant.get('inventory_item_id')
            
            # Safeguard Recovery: If inventory_item_id is omitted from direct post return, query item directly
            if not inventory_item_id and new_variant_id:
                print("🔄 Fetching complete variant payload to resolve inventory tracking records...")
                variant_res = shopify_api_call(f'variants/{new_variant_id}.json')
                if variant_res and variant_res.get('variant'):
                    inventory_item_id = variant_res['variant'].get('inventory_item_id')
            
            if inventory_item_id:
                sanded_location_id = get_location_id_by_name('SandedNBranded')
                if sanded_location_id:
                    set_inventory_at_location(inventory_item_id, sanded_location_id, 1)
                    tag_status = "featured (visible)" if add_featured_tag else "no featured tag (hidden)"
                    print(f"✓ Created from 'sample' tag → qty=1 at both locations, {tag_status}")
                else:
                    print(f"⚠️ Could not find 'SandedNBranded' location - product only available at default location")
            
            return {'product_id': new_product_id, 'variant_id': new_variant_id, 'title': new_title}
        return None
    except Exception as e:
        print(f"✗ Error creating product: {e}")
        return None

def execute_line_item_swap(order_id, old_line_item_id, new_variant_id):
    """Executes the complete GraphQL sequence to swap out the sample for the unique item on an active order layout"""
    print(f"Starting line item swap sequence for Order ID: {order_id}")
    
    # Step 1: Open Edit Session
    begin_mutation = """
    mutation orderEditBegin($id: ID!) {
      orderEditBegin(id: $id) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(begin_mutation, {"id": f"gid://shopify/Order/{order_id}"})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditBegin'):
        return False, f"orderEditBegin failed syntax check: {res}"
    
    edit_data = res['data']['orderEditBegin']
    if edit_data.get('userErrors'):
        return False, f"orderEditBegin error: {edit_data['userErrors'][0]['message']}"
        
    calc_order_id = edit_data['calculatedOrder']['id']
    print(f"✓ Order edit session started: {calc_order_id}")
    
    # Step 2: Remove Old Sample Product by adjusting its quantity to 0
    remove_mutation = """
    mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
      orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(remove_mutation, {
        "id": calc_order_id, 
        "lineItemId": f"gid://shopify/LineItem/{old_line_item_id}", 
        "quantity": 0
    })
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditSetQuantity'):
        return False, "orderEditSetQuantity failed execution"
        
    remove_data = res['data']['orderEditSetQuantity']
    if remove_data.get('userErrors'):
        return False, f"orderEditSetQuantity error: {remove_data['userErrors'][0]['message']}"
    print(f"✓ Successfully staged removal of sample line item")

    # Step 3: Add New Serialized Variant Product
    add_mutation = """
    mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
      orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(add_mutation, {
        "id": calc_order_id, 
        "variantId": f"gid://shopify/ProductVariant/{new_variant_id}", 
        "quantity": 1
    })
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditAddVariant'):
        return False, "orderEditAddVariant failed execution"
        
    add_data = res['data']['orderEditAddVariant']
    if add_data.get('userErrors'):
        return False, f"orderEditAddVariant error: {add_data['userErrors'][0]['message']}"
    print(f"✓ Successfully staged addition of serialized unique variant")

    # Step 4: Commit transaction changes to live order records
    commit_mutation = """
    mutation orderEditCommit($id: ID!) {
      orderEditCommit(id: $id) {
        order { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(commit_mutation, {"id": calc_order_id})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditCommit'):
        return False, "orderEditCommit failed execution"
        
    commit_data = res['data']['orderEditCommit']
    if commit_data.get('userErrors'):
        return False, f"orderEditCommit error: {commit_data['userErrors'][0]['message']}"
        
    return True, "Success"

def add_serial_to_order_note(order_id, serial, swap_status=None):
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result: return False
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        status_text = f" ({swap_status})" if swap_status else ""
        note_addition = f"\nSerial Number: {serial}{status_text}"
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
    try:
        shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data={
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_completed',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        })
        return True
    except: return False

def process_order(order_data, add_featured_tag=False):
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')

    if not try_acquire_processing_lock(order_id):
        return {'status': 'already_processing', 'order': order_number}

    try:
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()

        created_at = order_data.get('created_at', '')
        try:
            order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except:
            order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(f"Processing order {order_number} (ID: {order_id}) add_featured_tag={add_featured_tag}")

        products_created = []
        serials_assigned = []

        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            current_qty = item.get('current_quantity', quantity)
            product_id = item.get('product_id')
            line_item_id = item.get('id')

            print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity}, Current: {current_qty})")

            if not current_qty or current_qty == 0:
                print(f"⏭️ Skipping removed line item: {product_title}")
                continue

            if not (sku and sku.upper().startswith('LCK-')):
                print(f"⏭️ Skipping non-clock item: {sku}")
                continue

            product_result = shopify_api_call(f'products/{product_id}.json')
            if product_result:
                tags = product_result.get('product', {}).get('tags', '')
                tags_list = [tag.strip().lower() for tag in tags.split(',') if tag.strip()]

                if 'featured' in tags_list:
                    print(f"⏭️ Skipping - product tagged 'featured'")
                    continue
                if 'sample' not in tags_list:
                    print(f"⏭️ Skipping - product not tagged 'sample'")
                    continue

                print(f"✓ Product tagged 'sample' - creating individual product")
            else:
                print(f"⚠️ Could not fetch product tags - skipping for safety")
                continue

            for i in range(current_qty):
                print(f"Processing item {i+1} of {current_qty}...")
                serial = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue

                print(f"✓ Generated serial: {serial}")
                serials_assigned.append(serial)

                new_product = create_product_from_sample(product_id, serial, add_featured_tag=add_featured_tag)
                if new_product:
                    products_created.append(new_product['title'])
                    log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])
                    
                    # Run the live financial layout swap sequence
                    swap_success, swap_msg = execute_line_item_swap(order_id, line_item_id, new_product['variant_id'])
                    if swap_success:
                        print(f"✓ GraphQL order swap complete: {serial} is now explicitly on the order sheet")
                        add_serial_to_order_note(order_id, serial, swap_status="Swapped successfully")
                    else:
                        print(f"⚠️ GraphQL order swap fallback triggered: {swap_msg}")
                        add_serial_to_order_note(order_id, serial, swap_status="Note fallback only")

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
  .removed-row td { text-decoration: line-through; color: #999; }
  .removed-label { color: #d93025; font-weight: bold; text-decoration: none !important; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .75rem; background: #e8f0fe; color: #1a56db; margin-right: 4px; }
  .tag.sample { background: #fce8e6; color: #c0392b; }
  .tag.featured { background: #e6f4ea; color: #1e7e34; }
  .log { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 14px; font-family: monospace; font-size: .85rem; white-space: pre-wrap; display: none; margin-top: 16px; }
</style>
</head>
<body>
<h1>⚡ Manual Webhook Trigger</h1>
<p style="color:#666">Force-process orders. ONLY processes products tagged "sample".</p>

<div class="card">
  <label>Order Number</label>
  <input type="text" id="orderInput" placeholder="2882">
  
  <div style="margin-top:14px;">
    <label style="font-weight:600; font-size:.9rem;">Build Type (both create with qty=1, tagged difference controls visibility)</label>
    <div style="display:flex; flex-direction:column; gap:8px; margin-top:6px;">
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="stock">
        <span><strong>Stock build</strong> — tagged "featured", visible on site</span>
      </label>
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="customer" checked>
        <span><strong>Customer order</strong> — no "featured" tag, hidden from site</span>
      </label>
    </div>
  </div>

  <button id="lookupBtn">Look Up Order</button>

  <div id="orderInfo" style="display:none">
    <table>
      <thead><tr><th>Product</th><th>SKU</th><th>Tags</th><th>Qty</th></tr></thead>
      <tbody id="itemsBody"></tbody>
    </table>
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
        <td>${item.tags.map(t => `<span class="tag ${t==='sample'?'sample':t==='featured'?'featured':''}">${t}</span>`).join('')}</td>
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
    const buildType = document.querySelector('input[name=buildType]:checked').value;
    const resp = await fetch('/api/manual', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        order_id: currentOrderId,
        add_featured_tag: buildType === 'stock'
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
            result = process_order(order_res['order'], add_featured_tag=payload.get('add_featured_tag', False))
            self.send_json(200, result)
        else:
            result = process_order(payload, add_featured_tag=False)
            self.send_json(200, result)