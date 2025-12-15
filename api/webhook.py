# api/webhook.py
import json
import hmac
import hashlib
import base64
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error

# Shopify config
SHOPIFY_SECRET = os.environ.get('SHOPIFY_API_SECRET')
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME')  # e.g., 'yourstore'
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN')

# Products that need serial numbers
SERIALIZED_PRODUCTS = ['Wade', 'Madison', 'Parker']

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
    request = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"API Error: {e.code} {e.read().decode()}")
        return None

def get_next_serial():
    """Get and increment global serial counter"""
    # Get shop metafields
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    
    if not result:
        return None, None
    
    metafields = result.get('metafields', [])
    
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        
        # Generate serial
        serial = f"LCK-{current}"
        
        # Increment counter
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
    else:
        # Create metafield starting at 1010
        create_data = {
            'metafield': {
                'namespace': 'custom',
                'key': 'global_serial_counter',
                'value': '1010',
                'type': 'number_integer'
            }
        }
        result = shopify_api_call('metafields.json', method='POST', data=create_data)
        return 'LCK-1010', 1010

def add_serial_to_order(order_id, serial):
    """Add serial to order note"""
    # Get current order
    result = shopify_api_call(f'orders/{order_id}.json')
    if not result:
        return False
    
    order = result.get('order', {})
    current_note = order.get('note', '') or ''
    
    # Add serial to note
    if current_note:
        new_note = f"{current_note}\nSerial: {serial}"
    else:
        new_note = f"Serial: {serial}"
    
    # Update order
    update_data = {
        'order': {
            'id': order_id,
            'note': new_note
        }
    }
    
    result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
    return result is not None

def log_to_google_sheet(product_name, serial, order_number, customer_name):
    """Log to Google Sheet - we'll add this later with service account"""
    # TODO: Add Google Sheets API integration
    print(f"Logging: {serial} | {product_name} | {order_number} | {customer_name}")
    return True

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Read request body
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            
            # Verify webhook
            hmac_header = self.headers.get('X-Shopify-Hmac-SHA256', '')
            if not verify_webhook(body, hmac_header):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'Unauthorized')
                return
            
            # Parse order data
            order_data = json.loads(body.decode('utf-8'))
            order_id = order_data.get('id')
            order_number = order_data.get('name', '')
            
            # Get customer name
            customer = order_data.get('customer', {})
            customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
            
            # Process line items
            serials_generated = []
            for item in order_data.get('line_items', []):
                product_title = item.get('title', '')
                quantity = item.get('quantity', 1)
                
                # Check if this product needs serials
                needs_serial = any(
                    prod.lower() in product_title.lower() 
                    for prod in SERIALIZED_PRODUCTS
                )
                
                if needs_serial:
                    for i in range(quantity):
                        serial, counter = get_next_serial()
                        if serial:
                            serials_generated.append(serial)
                            log_to_google_sheet(product_title, serial, order_number, customer_name)
            
            # Add all serials to order note
            if serials_generated:
                serial_text = ', '.join(serials_generated)
                add_serial_to_order(order_id, serial_text)
            
            # Success response
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                'status': 'success',
                'serials_generated': serials_generated
            }
            self.wfile.write(json.dumps(response).encode())
            
        except Exception as e:
            print(f"Error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
    
    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Webhook handler is running')
