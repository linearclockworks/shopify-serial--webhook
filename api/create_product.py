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
        import traceback
        traceback.print_exc()
        return None

def log_to_google_sheet(product_name, serial, order_number):
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
        
        from datetime import datetime
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
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
        with urllib.request.urlopen(req, timeout=30) as response:
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

def find_master_product(style_name):
    """Find master product by title using colon convention"""
    search_lower = style_name.lower().strip()
    if not search_lower.endswith(':'):
        search_lower += ':'
    
    print(f"Searching for: '{search_lower}'")
    
    # Search newest first - will hit master products faster (only a few -- clones to skip)
    page = 1
    found_products = 0
    
    while page <= 20:  # Max 5000 products
        url = f'products.json?limit=250&page={page}&order=created_at DESC'
        result = shopify_api_call(url)
        
        if not result or not result.get('products') or len(result['products']) == 0:
            break
        
        found_products += len(result['products'])
        print(f"Page {page}: scanned {found_products} total products")
        
        for product in result['products']:
            title_lower = product['title'].lower()
            # Skip -- products, find the master
            if title_lower.startswith(search_lower) and not product['title'].startswith('--'):
                print(f"✓ MATCH: {product['title']}")
                return product
        
        page += 1
    
    print(f"✗ No match found after scanning {found_products} products")
    return None
    
def create_available_product(master_product, serial):
    """Create a new available product (not tied to an order)"""
    
    product_data = {
        'product': {
            'title': f"-- {master_product['title']} - Available",
            'body_html': master_product.get('body_html', ''),
            'vendor': master_product.get('vendor', ''),
            'product_type': master_product.get('product_type', ''),
            'status': 'draft',  # Start as draft so you can customize
            'tags': 'available',
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
    
    # Copy images
    for img in master_product.get('images', []):
        product_data['product']['images'].append({
            'src': img['src'],
            'position': img.get('position', 1),
            'alt': img.get('alt')
        })
    
    result = shopify_api_call('products.json', method='POST', data=product_data)
    
    if not result:
        return None
    
    new_product = result.get('product')
    
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
            'key': 'serial_number',
            'type': 'single_line_text_field',
            'value': serial
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'is_order_product',
            'type': 'boolean',
            'value': 'false'
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'is_available_product',
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

@app.route('/api/create_product', methods=['GET'])
def create_product():
    """
    Create a new available product from a master
    Usage: /api/create-product?style=Claret
    """
    style_name = request.args.get('style')
    
    if not style_name:
        return jsonify({'error': 'Missing style parameter. Usage: /api/create-product?style=Claret'}), 400
    
    try:
        print(f"=== Creating available product for style: {style_name} ===")
        
        # Find master product
        print(f"Searching for master product: {style_name}")
        master_product = find_master_product(style_name)
        
        if not master_product:
            return jsonify({'error': f'Master product not found for style: {style_name}'}), 404
        
        print(f"✓ Found master: {master_product['title']} (ID: {master_product['id']})")
        
        # Generate serial
        serial, counter = get_next_serial()
        if not serial:
            return jsonify({'error': 'Failed to generate serial'}), 500
        
        print(f"✓ Generated serial: {serial}")
        
        # Create product
        new_product = create_available_product(master_product, serial)
        
        if not new_product:
            return jsonify({'error': 'Failed to create product'}), 500
        
        print(f"✓ Created product: {new_product['id']} - {new_product['title']}")
        
        # Log to sheet
        log_to_google_sheet(master_product['title'], serial, 'Available')
        
        return jsonify({
            'status': 'success',
            'serial': serial,
            'product_id': new_product['id'],
            'product_title': new_product['title'],
            'product_url': f"https://{SHOPIFY_SHOP}.myshopify.com/admin/products/{new_product['id']}",
            'message': 'Product created as DRAFT. Customize photos/description, then set to Active.'
        }), 200
        
    except Exception as e:
        import traceback
        print(f"ERROR: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500