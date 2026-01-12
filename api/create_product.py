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
        with urllib.request.urlopen(req, timeout=60) as response:  # Changed from 30 to 60
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
    
    # Use since_id pagination (most reliable)
    since_id = 0
    found_products = 0
    max_products = 5000
    
    while found_products < max_products:
        if since_id == 0:
            url = f'products.json?limit=250'
        else:
            url = f'products.json?limit=250&since_id={since_id}'
        
        result = shopify_api_call(url)
        
        if not result or not result.get('products') or len(result['products']) == 0:
            break
        
        found_products += len(result['products'])
        print(f"Scanned {found_products} products...")
        
        for product in result['products']:
            title_lower = product['title'].lower()
            if title_lower.startswith(search_lower) and not product['title'].startswith('--'):
                print(f"✓ MATCH: {product['title']}")
                return product
        
        # Get last product ID for next page
        since_id = result['products'][-1]['id']
    
    print(f"✗ No match found after scanning {found_products} products")
    return None

def publish_to_sales_channels(product_id):
    """Publish product to Online Store and Shop sales channels"""
    try:
        # Get available publications (sales channels)
        publications = shopify_api_call('publications.json')
        
        if not publications or not publications.get('publications'):
            print("Could not fetch publications")
            return False
        
        # Find Online Store and Shop channels
        online_store_id = None
        shop_id = None
        
        for pub in publications['publications']:
            if pub['name'] == 'Online Store':
                online_store_id = pub['id']
            elif pub['name'] == 'Shop' or pub['name'] == 'Point of Sale':
                shop_id = pub['id']
        
        print(f"Found channels - Online Store: {online_store_id}, Shop: {shop_id}")
        
        # Publish to each channel
        success = True
        for pub_id in [online_store_id, shop_id]:
            if pub_id:
                result = shopify_api_call(
                    f'publications/{pub_id}/resource_feedbacks.json',
                    method='POST',
                    data={
                        'resource_feedback': {
                            'resource_id': product_id,
                            'resource_type': 'Product',
                            'feedback_generated_at': datetime.now().isoformat(),
                            'state': 'success'
                        }
                    }
                )
                if result:
                    print(f"Published to channel {pub_id}")
                else:
                    success = False
        
        return success
        
    except Exception as e:
        print(f"Error publishing to channels: {e}")
        return False
            
def create_available_product(master_product, serial):
    """Create a new available product (not tied to an order)"""
    
    # Create the new title - just add -- prefix, no "Available"
    new_title = f"-- {master_product['title']}"
    
    # Use master's handle + serial for unique URL
    serial_suffix = serial.replace('LCK-', '')  # Just the number
    base_handle = master_product.get('handle', '')
    if base_handle:
        new_handle = f"{base_handle}-{serial_suffix}"
    else:
        # Fallback: generate from title
        import re
        new_handle = master_product['title'].lower()
        new_handle = re.sub(r'[^a-z0-9]+', '-', new_handle)
        new_handle = re.sub(r'-+', '-', new_handle).strip('-')
        new_handle = f"{new_handle}-{serial_suffix}"
    
    product_data = {
        'product': {
            'title': new_title,
            'handle': new_handle,
            'body_html': master_product.get('body_html', ''),
            'vendor': master_product.get('vendor', ''),
            'product_type': 'Wall Clocks',
            'status': 'active',  # Active, not draft
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
            'type': 'number_integer',  # Store as simple number
            'value': str(master_product['id'])
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
        print(f"Adding metafield: {mf_data['key']}")
        result = shopify_api_call(
            f'products/{product_id}/metafields.json',
            method='POST',
            data={'metafield': mf_data}
        )
        if result:
            print(f"✓ Added metafield: {mf_data['key']}")
        else:
            print(f"✗ Failed to add metafield: {mf_data['key']}")
    
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