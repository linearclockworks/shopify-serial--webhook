# Summary: When a customer buys a cleartime product on Shopify, this webhook automatically:
# Assigns a unique serial number from a cleartime-specific counter
# Adds the serial to the order notes
# Creates a new row in the CTClocks Google Sheet for tracking

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# Load configuration from environment variables
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID_CLEARTIME', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

# SKU prefixes for cleartime products
CLEARTIME_SKU_PREFIXES = ['CT', 'FA', 'MP', 'KIT']

def get_google_sheet():
    """Connect to the CTClocks Google Sheet"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet = spreadsheet.worksheet('CTClocks')
        return sheet
    except Exception as e:
        print(f"Sheet error: {e}")
        return None

def log_to_google_sheet(sku, serial, order_number, customer_name, order_date):
    """Add a new row to the CTClocks Google Sheet"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            return False
        
        # CTClocks sheet columns: A=Serial numb, B=SKU, C=Event tags?, D=Order number, E=customer, F=run length, G=steps, H=comments
        row = [
            serial,          # A: Serial numb (just the number)
            sku,             # B: SKU (actual SKU like CT4024M)
            '',              # C: Event tags? (empty)
            order_number,    # D: Order number
            customer_name,   # E: customer
            '',              # F: run length (empty)
            '',              # G: steps (empty)
            '',              # H: comments (empty)
        ]
        
        # Insert at row 2 (after header)
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

def get_next_serial():
    """Get the next available cleartime serial number and increment the counter"""
    result = shopify_api_call('metafields.json?namespace=custom&key=cleartime_serial_counter')
    
    if not result:
        return None
    
    metafields = result.get('metafields', [])
    
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        
        # Just use the number directly, no prefix
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

def add_serial_to_order_note(order_id, serials):
    """Append serial numbers to order notes"""
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        serial_text = ', '.join(serials)
        new_note = f"{current_note}\nCleartime Serial Numbers: {serial_text}" if current_note else f"Cleartime Serial Numbers: {serial_text}"
        
        update_data = {'order': {'note': new_note}}
        result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
        
        if result:
            print(f"✓ Added to order notes: {serial_text}")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding note: {e}")
        return False

def process_webhook(order_data):
    """Process the webhook order data"""
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')
    
    customer = order_data.get('customer', {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    
    created_at = order_data.get('created_at', '')
    try:
        order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    except:
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"Processing cleartime order {order_number} (ID: {order_id})")
    
    serials_assigned = []
    
    for item in order_data.get('line_items', []):
        product_title = item.get('title', '')
        line_item_id = item.get('id')
        sku = item.get('sku', '')
        quantity = item.get('quantity', 1)
        
        print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")
        
        # Check if SKU starts with any cleartime prefix
        is_cleartime = any(sku.upper().startswith(prefix) for prefix in CLEARTIME_SKU_PREFIXES)
        
        if sku and is_cleartime and not product_title.startswith('--'):
            print(f"✓ Cleartime product detected: {sku}")
            
            if quantity != 1:
                print(f"⚠ Quantity {quantity} != 1, skipping")
                continue
            
            print("Generating cleartime serial...")
            serial = get_next_serial()
            if not serial:
                print("✗ Failed to generate serial")
                continue
            
            print(f"✓ Generated serial: {serial}")
            serials_assigned.append(serial)
            
            print("Logging to CTClocks Google Sheet...")
            log_to_google_sheet(sku, serial, order_number, customer_name, order_date)
    
    if serials_assigned:
        add_serial_to_order_note(order_id, serials_assigned)
    
    print(f"CLEARTIME WEBHOOK COMPLETE - {len(serials_assigned)} serials assigned")
    
    return {
        'status': 'success',
        'serials': serials_assigned,
        'order': order_number
    }

class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler"""
    
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Cleartime webhook handler is running - v1')
        return
    
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            order_data = json.loads(body)
            
            print("=" * 60)
            print("CLEARTIME WEBHOOK RECEIVED")
            print("=" * 60)
            
            result = process_webhook(order_data)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())