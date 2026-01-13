from flask import Flask, request, jsonify
import json
import hmac
import hashlib
import base64
import os
import urllib.request
import urllib.error
from datetime import datetime

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
            print("Failed to get sheet")
            return False
            
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
        
        print(f"Inserting row at top of sheet: {row[:5]}")
        # Insert at row 2 (after header)
        sheet.insert_row(row, index=2)
        print("Successfully inserted to sheet")
        return True
    except Exception as e:
        print(f"Log error: {e}")
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
        with urllib.request.urlopen(req, timeout=60) as response:
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

def publish_to_sales_channels(product_id):
    """Publish product to Shop only (not Online Store) so it's hidden from search but available for orders"""
    try:
        # Get available publications (sales channels)
        publications = shopify_api_call('publications.json')
        
        if not publications or not publications.get('publications'):
            print("Could not fetch publications")
            return False
        
        # Find Shop channel only (skip Online Store)
        shop_id = None
        
        for pub in publications['publications']:
            print(f"Found publication: {pub['name']} (ID: {pub['id']})")
            if pub['name'] == 'Shop' or pub['name'] == 'Point of Sale':
                shop_id = pub['id']
        
        print(f"Publishing to Shop channel: {shop_id}")
        
        # Publish to Shop only
        if shop_id:
            result = shopify_api_call(
                f'publications/{shop_id}/resource_feedbacks.json',
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
                print(f"✓ Published to Shop channel")
                return True
            else:
                print(f"✗ Failed to publish to Shop")
                return False
        else:
            print("✗ Shop channel not found")
            return False
        
    except Exception as e:
        print(f"Error publishing to channels: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_order_product(master_product, order_number, serial):
    """Create a new order-specific product based on master template"""
    
    # Create the new title with order number
    new_title = f"-- {master_product['title']} - {order_number}"
    
    # Use master's handle + serial for unique URL
    serial_suffix = serial.replace('LCK-', '')
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
            'type': 'number_integer',
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
            'value': 'true'  # This is an order-specific product
        },
        {
            'namespace': 'linear_clockworks',
            'key': 'order_number',
            'type': 'single_line_text_field',
            'value': order_number
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
    
    # Publish to Shop only (not Online Store)
    print("Publishing to sales channels...")
    publish_to_sales_channels(product_id)
    
    return new_product

def replace_line_item_in_order(order_id, old_line_item_id, new_product_id, new_variant_id):
    """Replace a line item using Shopify's Order Editing API"""
    try:
        print(f"Starting order edit for order {order_id}")
        
        # Step 1: Begin order edit
        edit_data = {'order_edit': {}}
        result = shopify_api_call(f'orders/{order_id}/order_edits.json', method='POST', data=edit_data)
        
        if not result or not result.get('order_edit'):
            print("Failed to begin order edit")
            return False
        
        order_edit_id = result['order_edit']['id']
        print(f"✓ Order edit started: {order_edit_id}")
        
        # Step 2: Add new line item first (so order isn't empty)
        print(f"Adding new product variant {new_variant_id}")
        add_data = {
            'line_item': {
                'variant_id': new_variant_id,
                'quantity': 1
            }
        }
        result = shopify_api_call(
            f'order_edits/{order_edit_id}/line_items.json',
            method='POST',
            data=add_data
        )
        
        if not result:
            print("Failed to add new line item")
            return False
        
        print(f"✓ Added new line item")
        
        # Step 3: Remove old line item
        print(f"Removing old line item {old_line_item_id}")
        result = shopify_api_call(
            f'order_edits/{order_edit_id}/line_items/{old_line_item_id}.json',
            method='DELETE'
        )
        
        print(f"✓ Removed old line item")
        
        # Step 4: Commit the edit
        print("Committing order edit...")
        commit_data = {
            'order_edit': {
                'notify_customer': False
            }
        }
        result = shopify_api_call(
            f'order_edits/{order_edit_id}/commit.json',
            method='POST',
            data=commit_data
        )
        
        if result:
            print(f"✓ Order edit committed successfully")
            return True
        else:
            print("Failed to commit order edit")
            return False
        
    except Exception as e:
        print(f"Error in replace_line_item_in_order: {e}")
        import traceback
        traceback.print_exc()
        return False

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
        return 'Webhook handler is running - v4', 200
    
    try:
        body = request.get_data()
        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
        
        print("=" * 60)
        print("WEBHOOK RECEIVED")
        print("=" * 60)
        
        order_data = json.loads(body)
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
        
        serials_generated = []
        products_created = []
        
        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            product_id = item.get('product_id')
            line_item_id = item.get('id')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            
            print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")
            
            # Check if this is a clock product (SKU starts with LCK-) 
            # Skip products that start with -- (already processed)
            if sku and sku.startswith('LCK-') and not product_title.startswith('--'):
                print(f"✓ Clock product detected: {sku}")
                
                # Only process single quantity
                if quantity != 1:
                    print(f"⚠ Quantity {quantity} != 1, skipping product creation")
                    continue
                
                # Get the master product details
                print(f"Fetching master product {product_id}...")
                master_product = get_master_product(product_id)
                if not master_product:
                    print(f"✗ Could not fetch master product {product_id}")
                    continue
                
                print(f"✓ Master product: {master_product['title']}")
                
                # Generate serial
                print("Generating serial...")
                serial, counter = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue
                
                print(f"✓ Generated serial: {serial}")
                serials_generated.append(serial)
                
                # Create the order-specific product
                print(f"Creating order product...")
                new_product = create_order_product(master_product, order_number, serial)
                
                if new_product:
                    print(f"✓ Created product: {new_product['id']} - {new_product['title']}")
                    products_created.append({
                        'id': new_product['id'],
                        'title': new_product['title'],
                        'serial': serial
                    })
                    
                    # Replace the line item in the order
                    print(f"Replacing line item in order...")
                    new_variant_id = new_product['variants'][0]['id']
                    replaced = replace_line_item_in_order(order_id, line_item_id, new_product['id'], new_variant_id)
                    print(f"✓ Line item replaced: {replaced}")
                    
                    # Log to Google Sheet
                    print("Logging to Google Sheet...")
                    sheet_result = log_to_google_sheet(product_title, serial, order_number, customer_name, order_date)
                    print(f"✓ Sheet logged: {sheet_result}")
                else:
                    print("✗ Failed to create product")
        
        # Add serials to order notes
        if serials_generated:
            serial_text = ', '.join(serials_generated)
            print(f"Adding serials to order notes: {serial_text}")
            add_serial_to_order(order_id, serial_text)
        
        print("=" * 60)
        print(f"WEBHOOK COMPLETE - {len(products_created)} products created")
        print("=" * 60)
        
        return jsonify({
            'status': 'success',
            'serials': serials_generated,
            'products_created': products_created
        }), 200
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500