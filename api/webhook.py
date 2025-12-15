from flask import Flask, request, jsonify
import json
import hmac
import hashlib
import base64
import os
import urllib.request
import urllib.error
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Shopify config
SHOPIFY_SECRET = os.environ.get('SHOPIFY_API_SECRET', '')
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

# Google Sheets config
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

def get_google_sheet():
    """Get Google Sheet client"""
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        
        # Use the Clocks tab
        sheet = spreadsheet.worksheet('Clocks')
        return sheet
    except Exception as e:
        print(f"Error connecting to Google Sheets: {e}")
        return None

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date):
    """Log serial number to Google Sheet using existing structure"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            print("Could not connect to Google Sheet")
            return False
        
        # Parse product name: "Wade: 5-foot Cherry / resin" 
        # -> Name: "Wade", Description: "5-foot Cherry / resin"
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        # Your columns are: Serial, Name, Description, Avail, Order No, Bras tag description, 
        # Pointer, Font, Special order?, order date, color, PCB ver, Lettering width, 
        # Steps, Sled, Comments,, Quality, On website?, Location, Layout, Type, Speed steps/sec, Comments
        
        # We'll fill in: Serial, Name, Description, (skip Avail), Order No, (skip rest for now), order date
        row = [
            serial,           # Serial
            name_part,        # Name
            description_part, # Description
            '',               # Avail
            order_number,     # Order No
            '',               # Bras tag description
            '',               # Pointer
            '',               # Font
            '',               # Special order?
            order_date,       # order date
            '',               # color
            '',               # PCB ver
            '',               # Lettering width
            '',               # Steps
            '',               # Sled
            '',               # Comments
            '',               # (empty column)
            '',               # Quality
            '',               # On website?
            '',               # Location
            '',               # Layout
            '',               # Type
            '',               # Speed steps/sec
            ''                # Comments
        ]
        
        # Append new row (starting at row 2, after headers)
        sheet.append_row(row)
        print(f"Logged to Google Sheet: {serial} - {name_part}")
        return True
    except Exception as e:
        print(f"Error logging to Google Sheet: {e}")
        import traceback
        traceback.print_exc()
        return False
def verify_webhook(data, hmac_header):
    """Verify webhook is from Shopify"""
    if not SHOPIFY_SECRET or not hmac_header:
        return False
    digest = hmac.new(
        SHOPIFY_SECRET.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header)

def shopify_api_call(endpoint, method='GET', data=None):
    """Make Shopify API call"""
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json'
    }
    
    req_data = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"API Error: {e}")
        return None

def get_next_serial():
    """Get and increment global serial counter"""
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    
    if not result:
        print("Failed to get metafields")
        return None, None
    
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
        return serial, current
    
    return None, None

def add_serial_to_order(order_id, serial):
    """Add serial to order note"""
    result = shopify_api_call(f'orders/{order_id}.json')
    if not result:
        return False
    
    order = result.get('order', {})
    current_note = order.get('note', '') or ''
    new_note = f"{current_note}\nSerial: {serial}" if current_note else f"Serial: {serial}"
    
    update_data = {'order': {'id': order_id, 'note': new_note}}
    result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
    return result is not None

@app.route('/api/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return 'Webhook handler is running', 200
    
    try:
        body = request.get_data()
        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
        
        # Skip verification for now - we'll add it back later
        # if not verify_webhook(body, hmac_header):
        #     print("Webhook verification failed")
        #     return jsonify({'error': 'Unauthorized'}), 401
        
        print("Processing webhook")
        order_data = json.loads(body)
        order_id = order_data.get('id')
        order_number = order_data.get('name', '')
        
        # Get customer info
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        
        # Get order date
        from datetime import datetime
        created_at = order_data.get('created_at', '')
        try:
            order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except:
            order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"Processing order {order_number}")
        
        serials_generated = []
        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            
            print(f"Item: {product_title} (SKU: {sku})")
            
            if sku.startswith('LCK-'):
                print(f"Generating {quantity} serial(s)")
                for i in range(quantity):
                    serial, counter = get_next_serial()
                    if serial:
                        print(f"Generated: {serial}")
                        serials_generated.append(serial)
                        # Log to Google Sheet
                        log_to_google_sheet(product_title, serial, order_number, customer_name, order_date)
        
        if serials_generated:
            serial_text = ', '.join(serials_generated)
            print(f"Adding to order: {serial_text}")
            add_serial_to_order(order_id, serial_text)
        
        return jsonify({'status': 'success', 'serials': serials_generated}), 200
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/test-sheets', methods=['GET'])
def test_sheets():
    """Test Google Sheets connection"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            return jsonify({'error': 'Could not connect to sheet'}), 500
        
        # Try to read first cell
        first_cell = sheet.cell(1, 1).value
        row_count = sheet.row_count
        
        return jsonify({
            'status': 'success',
            'first_cell': first_cell,
            'row_count': row_count,
            'sheet_title': sheet.title
        }), 200
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500
@app.route('/api/test', methods=['GET'])
def test():
    """Test endpoint - add serial to a specific order"""
    order_id = request.args.get('order_id')
    
    if not order_id:
        return jsonify({'error': 'Missing order_id parameter. Use: /api/test?order_id=12345'}), 400
    
    try:
        # Get order details for logging
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return jsonify({'error': 'Could not fetch order'}), 500
        
        order = result.get('order', {})
        order_number = order.get('name', '')
        customer = order.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        
        # Get first item for product name
        line_items = order.get('line_items', [])
        product_name = line_items[0].get('title', '') if line_items else 'Unknown'
        
        from datetime import datetime
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        serial, counter = get_next_serial()
        if serial:
            success = add_serial_to_order(order_id, serial)
            if success:
                # Log to Google Sheet
                log_to_google_sheet(product_name, serial, order_number, customer_name, order_date)
                
                return jsonify({
                    'status': 'success',
                    'serial': serial,
                    'order_id': order_id,
                    'message': f'Added serial {serial} to order {order_number}'
                }), 200
            else:
                return jsonify({'error': 'Failed to add serial to order'}), 500
        else:
            return jsonify({'error': 'Failed to generate serial'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/next-serial', methods=['GET'])
def next_serial():
    """Check what the next serial will be (without incrementing)"""
    try:
        result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
        if result and result.get('metafields'):
            current = int(result['metafields'][0]['value'])
            return jsonify({
                'current_counter': current,
                'next_serial': f'LCK-{current}'
            }), 200
        else:
            return jsonify({'error': 'Counter not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500