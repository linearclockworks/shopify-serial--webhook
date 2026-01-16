from flask import Flask, request, jsonify
import json
import os
import urllib.request
import urllib.error
from datetime import datetime

app = Flask(__name__)

# Shopify config
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

# Google Sheets config
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

def get_google_sheet():
    """Get Google Sheet client"""
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
    """Log serial number to Google Sheet"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            return False
            
        # Parse product name
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        # Remove LCK- prefix for sheet
        serial_number_only = serial.replace('LCK-', '')
        
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
        
        # Insert at row 2 (after header)
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

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
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"✗ API Error: {e}")
        return None

def get_next_serial():
    """Get and increment global serial counter"""
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
        
        # Increment counter
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
    """Add serial numbers to order note"""
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        serial_text = ', '.join(serials)
        new_note = f"{current_note}\nSerial Numbers: {serial_text}" if current_note else f"Serial Numbers: {serial_text}"
        
        update_data = {'order': {'note': new_note}}
        result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
        
        if result:
            print(f"✓ Added to order notes: {serial_text}")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding note: {e}")
        return False

def add_serial_to_line_item(order_id, line_item_id, serial):
    """Add serial number as line item property (note attribute)"""
    try:
        # Add metafield to the line item
        metafield_data = {
            'metafield': {
                'namespace': 'linear_clockworks',
                'key': 'serial_number',
                'type': 'single_line_text_field',
                'value': serial,
                'owner_resource': 'line_item',
                'owner_id': line_item_id
            }
        }
        
        result = shopify_api_call('metafields.json', method='POST', data=metafield_data)
        
        if result:
            print(f"✓ Added serial to line item: {serial}")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding line item serial: {e}")
        return False

@app.route('/api/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return 'Webhook handler is running - v5 (serial tracking)', 200
    
    try:
        body = request.get_data()
        order_data = json.loads(body)
        
        print("=" * 60)
        print("WEBHOOK RECEIVED")
        print("=" * 60)
        
        order_id = order_data.get('id')
        order_number = order_data.get('name', '')
        
        # Get customer info
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        
        # Get order date
        created_at = order_data.get('created_at', '')
        try:
            order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except:
            order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"Processing order {order_number} (ID: {order_id})")
        
        serials_assigned = []
        
        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            line_item_id = item.get('id')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            
            print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")
            
            # Check if this is a clock product (SKU starts with LCK-)
            # Skip products that start with -- (already processed)
            if sku and sku.upper().startswith('LCK-') and not product_title.startswith('--'):
                print(f"✓ Clock product detected: {sku}")
                
                # Only process single quantity
                if quantity != 1:
                    print(f"⚠ Quantity {quantity} != 1, skipping")
                    continue
                
                # Generate serial
                print("Generating serial...")
                serial = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue
                
                print(f"✓ Generated serial: {serial}")
                serials_assigned.append(serial)
                
                # Add serial to line item as metafield
                add_serial_to_line_item(order_id, line_item_id, serial)
                
                # Log to Google Sheet
                print("Logging to Google Sheet...")
                log_to_google_sheet(product_title, serial, order_number, customer_name, order_date)
        
        # Add all serials to order notes
        if serials_assigned:
            add_serial_to_order_note(order_id, serials_assigned)
        
        print("=" * 60)
        print(f"WEBHOOK COMPLETE - {len(serials_assigned)} serials assigned")
        print("=" * 60)
        
        return jsonify({
            'status': 'success',
            'serials': serials_assigned,
            'order': order_number
        }), 200
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)