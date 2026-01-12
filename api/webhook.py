from flask import Flask, request, jsonify
import json
import hmac
import hashlib
import base64
import os
import urllib.request
import urllib.error

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
        import gspread
        from google.oauth2.service_account import Credentials
        
        print(f"Sheet ID: {GOOGLE_SHEET_ID}")
        print(f"Credentials available: {bool(GOOGLE_CREDS_JSON)}")
        
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        print("Authorized with Google")
        
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        print(f"Opened spreadsheet: {spreadsheet.title}")
        
        # Use the Clocks tab
        sheet = spreadsheet.worksheet('Clocks')
        print(f"Opened worksheet: {sheet.title}")
        
        return sheet
    except Exception as e:
        print(f"Error connecting to Google Sheets: {e}")
        import traceback
        traceback.print_exc()
        return None

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date):
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
        
        # Remove LCK- prefix for sheet (e.g., "LCK-1023" becomes "1023")
        serial_number_only = serial.replace('LCK-', '')
        
        # Remove first two empty columns - start directly with Serial
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
        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"Log error: {e}")
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

def get_master_product(product_id):
    """Get the master product details"""
    result = shopify_api_call(f'products/{product_id}.json')
    if result:
        return result.get('product')
    return None

def create_order_product(master_product, order_number, serial):
    """Create a new order-specific product based on master template"""
    
    product_data = {
        'product': {
            'title': f"-- {master_product['title']} - {order_number}",
            'body_html': master_product.get('body_html', ''),
            'vendor': master_product.get('vendor', ''),
            'product_type': master_product.get('product_type', ''),
            'status': 'active',
            'tags': '',  # No tags
            'images': [],
            'variants': [{
                'sku': serial,
                'price': master_product['variants'][0]['price'],
                'inventory_management': 'shopify',
                'inventory_quantity': 1,
                'inventory_policy': 'deny',
                'weight': master_product['variants'][0].get('weight'),
                'weight_unit': master_product['variants'][0].get('weight_unit', 'kg')
            }]
        }
    }
    
    # Copy images (reuse URLs)
    for img in master_product.get('images', []):
        product_data['product']['images'].append({
            'src': img['src'],
            'position': img.get('position', 1),
            'alt': img.get('alt')
        })
    
    # Create the product
    result = shopify_api_call('products.json', method='POST', data=product_data)
    
    if not result:
        print("Failed to create product")
        return None
    
    new_product = result.get('product')
    print(f"Created product: {new_product['id']} - {new_product['title']}")
    
    # Add metafields to the new product
    product_id = new_product['id']
    metafields = [
        {
            'namespace': 'linear_clockworks',
            'key': 'master_product_id',
            'type': 'product_reference',
            'value': f"gid://shopify/Product/{master_product['id']}"
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'order_number',
            'type': 'single_line_text_field',
            'value': order_number
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'serial_number',
            'type': 'single_line_text_field',
            'value': serial
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'is_order_product',
            'type': 'boolean',
            'value': 'true'
        }
    ]
    
    for mf_data in metafields:
        mf_result = shopify_api_call(
            f'products/{product_id}/metafields.json',
            method='POST',
            data={'metafield': mf_data}
        )
        if not mf_result:
            print(f"Warning: Failed to add metafield {mf_data['key']}")
    
    return new_product

def update_order_line_item(order_id, line_item_id, new_product_id, new_variant_id):
    """Replace the line item with the new product"""
    
    # First, get the current order to preserve other data
    result = shopify_api_call(f'orders/{order_id}.json')
    if not result:
        print("Failed to get order")
        return False
    
    order = result['order']
    
    # Find and update the line item
    updated_line_items = []
    for item in order['line_items']:
        if item['id'] == line_item_id:
            # Replace with new product
            item['product_id'] = new_product_id
            item['variant_id'] = new_variant_id
            print(f"Updated line item {line_item_id} to product {new_product_id}")
        updated_line_items.append(item)
    
    # Update the order
    update_data = {
        'order': {
            'id': order_id,
            'line_items': updated_line_items
        }
    }
    
    result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
    return result is not None

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
        
        # Skip verification for now
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
            product_id = item.get('product_id')
            line_item_id = item.get('id')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            
            print(f"Item: {product_title} (SKU: {sku})")
            
            # Check if this is a master product that needs duplication
            if sku.startswith('LCK-'):
                print(f"Processing {quantity} clock(s)")
                
                # Get the master product details
                master_product = get_master_product(product_id)
                if not master_product:
                    print(f"Warning: Could not fetch master product {product_id}")
                    continue
                
                for i in range(quantity):
                    # Generate serial
                    serial, counter = get_next_serial()
                    if not serial:
                        print("Failed to generate serial")
                        continue
                    
                    print(f"Generated: {serial}")
                    serials_generated.append(serial)
                    
                    # Create the order-specific product
                    new_product = create_order_product(master_product, order_number, serial)
                    
                    if new_product:
                        # Update the line item to point to the new product
                        # Note: This only works for single-quantity items
                        # For multi-quantity, you'd need to split the line item
                        if quantity == 1:
                            new_variant_id = new_product['variants'][0]['id']
                            update_order_line_item(order_id, line_item_id, new_product['id'], new_variant_id)
                    
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

@app.route('/api/test', methods=['GET'])
def test():
    """Test endpoint - add serial to a specific order"""
    order_id = request.args.get('order_id')
    
    if not order_id:
        return jsonify({'error': 'Missing order_id parameter'}), 400
    
    try:
        print(f"=== TEST ENDPOINT CALLED ===")
        print(f"Order ID: {order_id}")
        
        # Get order details
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return jsonify({'error': 'Could not fetch order'}), 500
        
        order = result.get('order', {})
        order_number = order.get('name', '')
        customer = order.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        
        line_items = order.get('line_items', [])
        if not line_items:
            return jsonify({'error': 'No line items'}), 400
            
        first_item = line_items[0]
        product_name = first_item.get('title', '')
        product_id = first_item.get('product_id')
        
        from datetime import datetime
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"Order: {order_number}, Product: {product_name}")
        
        # Get master product
        master_product = get_master_product(product_id)
        if not master_product:
            return jsonify({'error': 'Could not fetch master product'}), 500
        
        # Generate serial
        serial, counter = get_next_serial()
        print(f"Generated serial: {serial}")
        
        if serial:
            # Create order product
            new_product = create_order_product(master_product, order_number, serial)
            
            if new_product:
                # Add serial to order note
                success = add_serial_to_order(order_id, serial)
                print(f"Added to order: {success}")
                
                if success:
                    # Log to Google Sheet
                    sheet_result = log_to_google_sheet(product_name, serial, order_number, customer_name, order_date)
                    print(f"Logged to sheet: {sheet_result}")
                    
                    return jsonify({
                        'status': 'success',
                        'serial': serial,
                        'order_number': order_number,
                        'new_product_id': new_product['id'],
                        'logged_to_sheet': sheet_result
                    }), 200
        
        return jsonify({'error': 'Failed'}), 500
        
    except Exception as e:
        print(f"ERROR in test endpoint: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/debug', methods=['GET'])
def debug():
    """Debug endpoint to check code version"""
    return jsonify({
        'version': '2025-01-12-v1',
        'features': ['product_creation', 'line_item_update']
    }), 200

@app.route('/api/next-serial', methods=['GET'])
def next_serial():
    """Check what the next serial will be"""
    try:
        result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
        if result and result.get('metafields'):
            current = int(result['metafields'][0]['value'])
            return jsonify({
                'current_counter': current,
                'next_serial': f'LCK-{current}'
            }), 200
        return jsonify({'error': 'Counter not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500
