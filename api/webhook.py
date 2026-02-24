# Summary: When a customer buys a clock on Shopify, this webhook automatically:
# Assigns a unique serial number (LCK-####) from a global counter
# Attaches that serial to the specific product in the order
# Adds the serial to the order notes
# Creates a new row in your Google Sheet for manufacturing tracking

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# Load configuration from environment variables
# These are kept secret and not in the code itself
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

def get_google_sheet():
    """Connect to the Google Sheet where we track all clocks"""
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

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date):
    """Add a new row to the Google Sheet with the clock's serial number and order details"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            return False
        
        # Split product name into name and description parts
        # Example: "Tide Clock: Custom Blue" becomes name="Tide Clock", description="Custom Blue"
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        # Remove "LCK-" prefix for cleaner serial numbers in the sheet
        serial_number_only = serial.replace('LCK-', '')
        
        # Create a row with all the columns in the tracking sheet
        # Most columns start empty and get filled in during manufacturing
        row = [
            serial_number_only,  # Serial
            name_part,           # Name
            description_part,    # Description
            '',                  # Avail
            order_number,        # Order No
            '',                  # Bras tag description
            '',                  # Pointer
            '',                  # Font
            '',                  # Special order?
            order_date,          # order date
            '',                  # color
            '',                  # PCB ver
            '',                  # Lettering width
            '',                  # Steps
            '',                  # Sled
            '',                  # Comments
            '',                  # (empty column)
            '',                  # Quality
            '',                  # On website?
            '',                  # Location
            '',                  # Layout
            '',                  # Type
            '',                  # Speed steps/sec
            ''                   # Comments
        ]
        
        # Insert the new row at position 2 (right after the header row)
        # This keeps newest orders at the top
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    """Make API calls to Shopify to read or update order/product data"""
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json'
    }
    
    # Convert data to JSON if we're sending any
    req_data = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"✗ API Error: {e}")
        return None

def get_next_serial():
    """Get the next available serial number and increment the counter
    
    Uses a global counter stored in Shopify metafields. Each time this is called,
    it reads the current number, returns it as a serial (LCK-####), and increments
    the counter for next time.
    """
    # Read the current serial counter from Shopify
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    
    if not result:
        return None
    
    metafields = result.get('metafields', [])
    
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        
        # Create the serial number with LCK- prefix
        serial = f"LCK-{current}"
        next_val = current + 1
        
        # Increment the counter in Shopify for next time
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
    """Append the serial numbers to the order's notes field
    
    This makes the serial numbers visible in Shopify's order details page
    """
    try:
        # Get the current order data
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        # Format all serials as comma-separated list
        serial_text = ', '.join(serials)
        
        # Append to existing notes or create new note
        new_note = f"{current_note}\nSerial Numbers: {serial_text}" if current_note else f"Serial Numbers: {serial_text}"
        
        # Update the order with new notes
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
    
    print(f"Processing order {order_number} (ID: {order_id})")
    
    serials_assigned = []
    
    # Loop through each product in the order
    for item in order_data.get('line_items', []):
        product_title = item.get('title', '')
        line_item_id = item.get('id')
        sku = item.get('sku', '')
        quantity = item.get('quantity', 1)
        
        print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")
        
        # Only process clock products (SKU starts with LCK-)
        # Skip products that have been manually marked as processed (start with --)
        if sku and sku.upper().startswith('LCK-') and not product_title.startswith('--'):
            print(f"✓ Clock product detected: {sku}")
            
            # Generate one serial per quantity
            for i in range(quantity):
                print(f"Generating serial {i+1} of {quantity}...")
                serial = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue
                
                print(f"✓ Generated serial: {serial}")
                serials_assigned.append(serial)
                
                # Add a row to the Google Sheet for manufacturing tracking
                print("Logging to Google Sheet...")
                log_to_google_sheet(product_title, serial, order_number, customer_name, order_date)
    
    # Add all generated serials to the order notes
    if serials_assigned:
        add_serial_to_order_note(order_id, serials_assigned)
    
    print(f"WEBHOOK COMPLETE - {len(serials_assigned)} serials assigned")
    
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
        self.wfile.write(b'Webhook handler is running - v6 (Vercel native)')
        return
    
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            order_data = json.loads(body)
            
            print("=" * 60)
            print("WEBHOOK RECEIVED")
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