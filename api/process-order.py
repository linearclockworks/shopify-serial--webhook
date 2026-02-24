# Manual order processing tool - allows you to process Shopify orders that were created manually
# Visit: https://shopify-serial-webhook.vercel.app/api/process-order

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_SHEET_ID_CLEARTIME = os.environ.get('GOOGLE_SHEET_ID_CLEARTIME', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

CLEARTIME_SKU_PREFIXES = ['CT', 'FA', 'MP', 'KIT']

def get_google_sheet(is_cleartime=False):
    """Connect to the appropriate Google Sheet"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        
        client = gspread.authorize(creds)
        sheet_id = GOOGLE_SHEET_ID_CLEARTIME if is_cleartime else GOOGLE_SHEET_ID
        spreadsheet = client.open_by_key(sheet_id)
        sheet_name = 'CTClocks' if is_cleartime else 'Clocks'
        sheet = spreadsheet.worksheet(sheet_name)
        return sheet
    except Exception as e:
        print(f"Sheet error: {e}")
        return None

def log_to_lck_sheet(product_name, serial, order_number, customer_name, order_date):
    """Add a new row to the LCK Clocks Google Sheet"""
    try:
        sheet = get_google_sheet(is_cleartime=False)
        if not sheet:
            return False
        
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        serial_number_only = serial.replace('LCK-', '')
        
        row = [
            serial_number_only, name_part, description_part, '', order_number,
            '', '', '', '', order_date, '', '', '', '', '', '', '', '', '', '', '', '', ''
        ]
        
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to Clocks sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def log_to_cleartime_sheet(sku, serial, order_number, customer_name, order_date):
    """Add a new row to the CTClocks Google Sheet"""
    try:
        sheet = get_google_sheet(is_cleartime=True)
        if not sheet:
            return False
        
        row = [
            serial, sku, '', order_number, customer_name, '', '', ''
        ]
        
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to CTClocks sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    """Make API calls to Shopify"""
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"
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

def get_next_lck_serial():
    """Get the next LCK serial number"""
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

def get_next_cleartime_serial():
    """Get the next cleartime serial number"""
    result = shopify_api_call('metafields.json?namespace=custom&key=cleartime_serial_counter')
    if not result:
        return None
    
    metafields = result.get('metafields', [])
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        serial = str(current)
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

def add_serial_to_order_note(order_id, lck_serials, cleartime_serials):
    """Append serial numbers to order notes"""
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        note_parts = []
        if lck_serials:
            note_parts.append(f"Serial Numbers: {', '.join(lck_serials)}")
        if cleartime_serials:
            note_parts.append(f"Cleartime Serial Numbers: {', '.join(cleartime_serials)}")
        
        serial_text = '\n'.join(note_parts)
        new_note = f"{current_note}\n{serial_text}" if current_note else serial_text
        
        update_data = {'order': {'note': new_note}}
        result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
        
        if result:
            print(f"✓ Added to order notes")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding note: {e}")
        return False

def get_order(order_number):
    """Fetch order data from Shopify by order number"""
    # Try to get by order number (name field)
    result = shopify_api_call(f'orders.json?name=%23{order_number}&status=any')
    
    if result and result.get('orders'):
        return result['orders'][0]
    
    # Try as order ID
    result = shopify_api_call(f'orders/{order_number}.json')
    if result:
        return result.get('order')
    
    return None

def process_order(order_data):
    """Process an order and assign serials"""
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')
    
    customer = order_data.get('customer', {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    
    created_at = order_data.get('created_at', '')
    try:
        order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    except:
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    lck_serials = []
    cleartime_serials = []
    
    for item in order_data.get('line_items', []):
        product_title = item.get('title', '')
        sku = item.get('sku', '')
        quantity = item.get('quantity', 1)
        
        # Check if cleartime product
        is_cleartime = any(sku.upper().startswith(prefix) for prefix in CLEARTIME_SKU_PREFIXES)
        
        if sku and sku.upper().startswith('LCK-') and not product_title.startswith('--'):
            # Process LCK products
            for i in range(quantity):
                serial = get_next_lck_serial()
                if serial:
                    lck_serials.append(serial)
                    log_to_lck_sheet(product_title, serial, order_number, customer_name, order_date)
        
        elif sku and is_cleartime and not product_title.startswith('--'):
            # Process cleartime products
            for i in range(quantity):
                serial = get_next_cleartime_serial()
                if serial:
                    cleartime_serials.append(serial)
                    log_to_cleartime_sheet(sku, serial, order_number, customer_name, order_date)
    
    # Add serials to order notes
    if lck_serials or cleartime_serials:
        add_serial_to_order_note(order_id, lck_serials, cleartime_serials)
    
    return {
        'order': order_number,
        'lck_serials': lck_serials,
        'cleartime_serials': cleartime_serials
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Manual Order Processing</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
                h2 { color: #333; }
                form { background: #f5f5f5; padding: 20px; border-radius: 5px; }
                label { display: block; margin-bottom: 5px; font-weight: bold; }
                input { width: 100%; padding: 8px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 3px; box-sizing: border-box; }
                button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 3px; cursor: pointer; font-size: 16px; }
                button:hover { background: #45a049; }
                .note { color: #666; font-size: 14px; margin-top: 10px; }
            </style>
        </head>
        <body>
            <h2>📦 Manual Order Processing</h2>
            <p>Use this tool to process orders created manually in Shopify Admin.</p>
            <form method="POST">
                <label>Order Number:</label>
                <input type="text" name="order_number" placeholder="2803 or #2803" required autofocus>
                <button type="submit">Process Order</button>
                <div class="note">
                    💡 Enter the order number from Shopify (with or without #)<br>
                    The tool will assign serial numbers and update Google Sheets automatically.
                </div>
            </form>
        </body>
        </html>
        '''
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())
    
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            
            # Extract order number
            order_number = body.split('order_number=')[1].split('&')[0]
            order_number = order_number.replace('%23', '').replace('#', '').strip()
            
            # Fetch order from Shopify
            order_data = get_order(order_number)
            
            if not order_data:
                html = f'''
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Order Not Found</title>
                    <style>
                        body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                        .error {{ background: #ffebee; padding: 20px; border-radius: 5px; color: #c62828; }}
                        a {{ color: #1976d2; text-decoration: none; }}
                    </style>
                </head>
                <body>
                    <div class="error">
                        <h2>❌ Order Not Found</h2>
                        <p>Could not find order #{order_number} in Shopify.</p>
                        <p>Make sure the order number is correct and the order exists.</p>
                    </div>
                    <p><a href="/api/process-order">← Try again</a></p>
                </body>
                </html>
                '''
                self.send_response(404)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(html.encode())
                return
            
            # Process the order
            result = process_order(order_data)
            
            # Build results HTML
            serials_html = ''
            if result['lck_serials']:
                serials_html += f"<p><strong>LCK Serials:</strong> {', '.join(result['lck_serials'])}</p>"
            if result['cleartime_serials']:
                serials_html += f"<p><strong>Cleartime Serials:</strong> {', '.join(result['cleartime_serials'])}</p>"
            
            if not result['lck_serials'] and not result['cleartime_serials']:
                serials_html = '<p><em>No serials assigned (no LCK or cleartime products found)</em></p>'
            
            html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Order Processed</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .success {{ background: #e8f5e9; padding: 20px; border-radius: 5px; color: #2e7d32; }}
                    a {{ color: #1976d2; text-decoration: none; }}
                    strong {{ color: #1976d2; }}
                </style>
            </head>
            <body>
                <div class="success">
                    <h2>✅ Order Processed Successfully!</h2>
                    <p><strong>Order:</strong> {result['order']}</p>
                    {serials_html}
                    <p><em>Serial numbers have been added to order notes and Google Sheets.</em></p>
                </div>
                <p><a href="/api/process-order">← Process another order</a></p>
            </body>
            </html>
            '''
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            
            html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Error</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .error {{ background: #ffebee; padding: 20px; border-radius: 5px; color: #c62828; }}
                    a {{ color: #1976d2; text-decoration: none; }}
                    code {{ background: #f5f5f5; padding: 2px 5px; border-radius: 3px; }}
                </style>
            </head>
            <body>
                <div class="error">
                    <h2>❌ Error Processing Order</h2>
                    <p><code>{str(e)}</code></p>
                </div>
                <p><a href="/api/process-order">← Try again</a></p>
            </body>
            </html>
            '''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())