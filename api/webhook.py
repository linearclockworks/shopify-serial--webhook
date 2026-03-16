# Summary: When a customer orders a "sample" tagged clock, this webhook automatically:
# 1. Generates a unique serial number (LCK-####)
# 2. Creates a new Shopify product with serial in name (e.g., "Elena-1028")
# 3. Copies photos, description, price from sample product
# 4. Logs to Clocksheet with hyperlink to new product
# 5. Adds serial number to order notes
# 6. Prevents duplicate processing with idempotency check

import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# Load configuration from environment variables
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

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date, product_id):
    """Add a new row to the Google Sheet with the clock's serial number and order details"""
    try:
        sheet = get_google_sheet()
        if not sheet:
            return False
        
        # Use the new product name (e.g., "Claret-1051") instead of original
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        # Remove "LCK-" prefix for cleaner serial numbers in the sheet
        serial_number_only = serial.replace('LCK-', '')
        
        # Create Shopify admin product URL
        product_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{product_id}"
        
        # Create a row with all the columns in the tracking sheet
        row = [
            serial_number_only,  # Serial
            name_part,           # Name (will be updated with hyperlink)
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
        sheet.insert_row(row, index=2)
        
        # Add hyperlink to the Name cell (column B, row 2)
        try:
            # Update cell B2 with hyperlink formula
            sheet.update_cell(2, 2, f'=HYPERLINK("{product_url}", "{name_part}")')
            print(f"✓ Logged to sheet with hyperlink: {serial}")
        except Exception as e:
            print(f"⚠️ Logged to sheet but hyperlink failed: {e}")
        
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    """Make API calls to Shopify to read or update order/product data"""
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2026-01/{endpoint}"
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
    """Get the next available serial number and increment the counter"""
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

def create_product_from_sample(sample_product_id, serial):
    """Create a new product based on the sample product with serial in name"""
    try:
        # Get the sample product details
        result = shopify_api_call(f'products/{sample_product_id}.json')
        if not result:
            print(f"✗ Could not fetch sample product {sample_product_id}")
            return None
        
        sample = result.get('product', {})
        
        # Sample products never have serials in name
        base_title = sample.get('title', '')
        
        # Create new product title with serial
        serial_only = serial.replace('LCK-', '')
        new_title = f"{base_title}-{serial_only}"
        
        # Get images - just reference the same URLs, don't duplicate
        images = []
        for img in sample.get('images', []):
            images.append({
                'src': img.get('src')
            })
        
        # Get first variant for price
        variants = sample.get('variants', [])
        price = variants[0].get('price') if variants else '0.00'
        
        # Create new product data
        new_product = {
            'product': {
                'title': new_title,
                'body_html': sample.get('body_html', ''),
                'vendor': sample.get('vendor', ''),
                'product_type': 'Wall Clocks',
                'status': 'active',
                'published': True,
                'images': images,
                'variants': [
                    {
                        'price': price,
                        'sku': serial,
                        'inventory_management': 'shopify',
                        'inventory_quantity': 1
                    }
                ]
            }
        }
        
        # Create the new product
        result = shopify_api_call('products.json', method='POST', data=new_product)
        
        if result and result.get('product'):
            new_product_id = result['product']['id']
            new_variant_id = result['product']['variants'][0]['id']
            print(f"✓ Created new product: {new_title} (ID: {new_product_id})")
            return {
                'product_id': new_product_id,
                'variant_id': new_variant_id,
                'title': new_title
            }
        else:
            print(f"✗ Failed to create product")
            return None
            
    except Exception as e:
        print(f"✗ Error creating product: {e}")
        import traceback
        traceback.print_exc()
        return None

def add_serial_to_order_note(order_id, serial):
    """Add serial number to order notes"""
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        note_addition = f"\nSerial Number: {serial}"
        new_note = f"{current_note}{note_addition}" if current_note else note_addition.strip()
        
        update_data = {'order': {'note': new_note}}
        result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
        
        if result:
            print(f"✓ Added serial to order notes: {serial}")
            return True
        return False
    except Exception as e:
        print(f"✗ Error adding serial to notes: {e}")
        return False

def mark_order_as_processed(order_id):
    """Mark order as processed to prevent duplicate webhook runs"""
    try:
        processed_data = {
            'metafield': {
                'namespace': 'webhook',
                'key': 'processed',
                'value': 'true',
                'type': 'boolean'
            }
        }
        result = shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=processed_data)
        if result:
            print(f"✓ Marked order as processed")
            return True
        return False
    except Exception as e:
        print(f"✗ Error marking order as processed: {e}")
        return False

def process_webhook(order_data):
    """Process the webhook order data"""
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')
    
    # IDEMPOTENCY CHECK: Has this order already been processed?
    result = shopify_api_call(f'orders/{order_id}/metafields.json?namespace=webhook&key=processed')
    if result and result.get('metafields'):
        print(f"⏭️ Order {order_number} already processed - skipping to prevent duplicates")
        return {
            'status': 'already_processed',
            'order': order_number,
            'message': 'Order was already processed'
        }
    
    customer = order_data.get('customer', {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    
    created_at = order_data.get('created_at', '')
    try:
        order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
    except:
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"Processing order {order_number} (ID: {order_id})")
    
    products_created = []
    serials_assigned = []
    
    # Loop through each product in the order
    for item in order_data.get('line_items', []):
        product_title = item.get('title', '')
        line_item_id = item.get('id')
        sku = item.get('sku', '')
        quantity = item.get('quantity', 1)
        product_id = item.get('product_id')
        
        print(f"Line item: {product_title} (SKU: {sku}, Qty: {quantity})")
        
        # Only process clock products (SKU starts with LCK-)
        if sku and sku.upper().startswith('LCK-'):
            print(f"✓ Clock product detected: {sku}")
            
            # Check product tags - only process if tagged "sample"
            product_result = shopify_api_call(f'products/{product_id}.json')
            if product_result:
                tags = product_result.get('product', {}).get('tags', '')
                tags_list = [tag.strip().lower() for tag in tags.split(',')]
                
                if 'featured' in tags_list and 'sample' in tags_list:
                    print(f"⚠️ WARNING: Product has BOTH 'featured' and 'sample' tags - skipping")
                    continue
                
                if 'featured' in tags_list:
                    print(f"⏭️ Skipping - product tagged 'featured' (already completed)")
                    continue
                
                if 'sample' not in tags_list:
                    print(f"⏭️ Skipping - product not tagged 'sample' (doesn't need manufacturing)")
                    continue
                
                print(f"✓ Product tagged 'sample' - creating individual product")
            else:
                print(f"⚠️ Could not fetch product tags - skipping for safety")
                continue
            
            # Process each quantity as separate product
            for i in range(quantity):
                print(f"Processing item {i+1} of {quantity}...")
                
                # Generate serial number
                serial = get_next_serial()
                if not serial:
                    print("✗ Failed to generate serial")
                    continue
                
                print(f"✓ Generated serial: {serial}")
                serials_assigned.append(serial)
                
                # Create new product based on sample
                new_product = create_product_from_sample(product_id, serial)
                
                if not new_product:
                    print("✗ Failed to create product")
                    continue
                
                products_created.append(new_product['title'])
                
                # Log to Google Sheet with hyperlink to new product
                print("Logging to Google Sheet...")
                log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])
    
    # Add all serial numbers to order notes
    if serials_assigned:
        for serial in serials_assigned:
            add_serial_to_order_note(order_id, serial)
    
    # Mark order as processed to prevent duplicate runs
    mark_order_as_processed(order_id)
    
    print(f"WEBHOOK COMPLETE - {len(products_created)} products created")
    
    return {
        'status': 'success',
        'products': products_created,
        'serials': serials_assigned,
        'order': order_number
    }

class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler"""
    
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Webhook handler is running - v10 (hyperlinks + order notes)')
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