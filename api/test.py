from flask import Flask, request, jsonify
import json
import os
import urllib.request
import urllib.error

app = Flask(__name__)

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
        
        serial_number_only = serial.replace('LCK-', '')
        
        row = [
            serial_number_only, name_part, description_part, '', order_number,
            '', '', '', '', order_date, '', '', '', '', '', '', '', '', '', '', '', '', ''
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"Log error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"
    headers = {'X-Shopify-Access-Token': SHOPIFY_TOKEN, 'Content-Type': 'application/json'}
    req_data = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"API Error: {e}")
        return None

def get_next_serial():
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    if not result:
        return None, None
    metafields = result.get('metafields', [])
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        serial = f"LCK-{current}"
        next_val = current + 1
        update_data = {'metafield': {'id': metafield_id, 'value': str(next_val), 'type': 'number_integer'}}
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
            'tags': '',
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
    
    # Add metafields
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
        shopify_api_call(
            f'products/{product_id}/metafields.json',
            method='POST',
            data={'metafield': mf_data}
        )
    
    return new_product

def add_serial_to_order(order_id, serial):
    result = shopify_api_call(f'orders/{order_id}.json')
    if not result:
        return False
    order = result.get('order', {})
    current_note = order.get('note', '') or ''
    new_note = f"{current_note}\nSerial: {serial}" if current_note else f"Serial: {serial}"
    update_data = {'order': {'id': order_id, 'note': new_note}}
    result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
    return result is not None

@app.route('/api/test', methods=['GET'])
def test():
    order_id = request.args.get('order_id')
    if not order_id:
        return jsonify({'error': 'Missing order_id'}), 400
    
    try:
        print(f"=== TEST: Processing order {order_id} ===")
        
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return jsonify({'error': 'No order'}), 500
        
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
        
        print(f"Order: {order_number}, Product: {product_name}, ID: {product_id}")
        
        from datetime import datetime
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Get master product
        master_product = get_master_product(product_id)
        if not master_product:
            return jsonify({'error': 'Could not fetch master product'}), 500
        
        print(f"Master product fetched: {master_product['title']}")
        
        # Generate serial
        serial, counter = get_next_serial()
        if not serial:
            return jsonify({'error': 'Failed to generate serial'}), 500
        
        print(f"Generated serial: {serial}")
        
        # Create order product
        new_product = create_order_product(master_product, order_number, serial)
        
        if new_product:
            print(f"Created new product: {new_product['id']}")
            
            # Add serial to order
            success = add_serial_to_order(order_id, serial)
            print(f"Added to order notes: {success}")
            
            if success:
                # Log to sheet
                sheet_result = log_to_google_sheet(product_name, serial, order_number, customer_name, order_date)
                print(f"Logged to sheet: {sheet_result}")
                
                return jsonify({
                    'status': 'success',
                    'serial': serial,
                    'order_number': order_number,
                    'new_product_id': new_product['id'],
                    'new_product_title': new_product['title'],
                    'new_product_url': f"https://{SHOPIFY_SHOP}.myshopify.com/admin/products/{new_product['id']}",
                    'logged_to_sheet': sheet_result
                }), 200
        
        return jsonify({'error': 'Failed to create product'}), 500
        
    except Exception as e:
        import traceback
        print(f"ERROR: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500