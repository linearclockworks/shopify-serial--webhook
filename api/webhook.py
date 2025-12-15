# api/webhook.py
import json
import hmac
import hashlib
import base64
import os
from datetime import datetime
import urllib.request
import urllib.error

# Shopify config
SHOPIFY_SECRET = os.environ.get('SHOPIFY_API_SECRET')
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME')  # e.g., 'yourstore'
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN')

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
    except Exception as e:
        print(f"Request Error: {e}")
        return None

def get_next_serial():
    """Get and increment global serial counter"""
    # Get shop metafields
    result = shopify_api_call('metafields.json?namespace=custom&key=global_serial_counter')
    
    if not result:
        print("Failed to get metafields")
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
        print("No metafield found, would create one")
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
        print(f"Failed to get order {order_id}")
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

# Vercel serverless function entry point
def handler(request):
    """Main handler for Vercel"""
    
    if request.method == 'GET':
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'body': 'Webhook handler is running'
        }
    
    if request.method == 'POST':
        try:
            # Get request body
            body = request.body
            
            # Verify webhook
            hmac_header = request.headers.get('x-shopify-hmac-sha256', '')
            if not verify_webhook(body, hmac_header):
                print("Webhook verification failed")
                return {
                    'statusCode': 401,
                    'body': 'Unauthorized'
                }
            
            print("Webhook verified successfully")
            
            # Parse order data
            order_data = json.loads(body.decode('utf-8'))
            order_id = order_data.get('id')
            order_number = order_data.get('name', '')
            
            print(f"Processing order {order_number} (ID: {order_id})")
            
            # Get customer name
            customer = order_data.get('customer', {})
            customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
            
            # Process line items
            serials_generated = []
            for item in order_data.get('line_items', []):
                product_title = item.get('title', '')
                sku = item.get('sku', '')
                quantity = item.get('quantity', 1)
                
                print(f"Checking item: {product_title} (SKU: {sku})")
                
                # Check if this product needs serials (SKU starts with LCK-)
                needs_serial = sku.startswith('LCK-')
                
                if needs_serial:
                    print(f"Item needs serial! Generating {quantity} serial(s)")
                    for i in range(quantity):
                        serial, counter = get_next_serial()
                        if serial:
                            print(f"Generated serial: {serial}")
                            serials_generated.append(serial)
                            log_to_google_sheet(product_title, serial, order_number, customer_name)
                        else:
                            print("Failed to generate serial")
                else:
                    print(f"Item does not need serial (SKU doesn't start with LCK-)")
            
            # Add all serials to order note
            if serials_generated:
                serial_text = ', '.join(serials_generated)
                print(f"Adding serials to order: {serial_text}")
                success = add_serial_to_order(order_id, serial_text)
                if success:
                    print("Successfully added serials to order")
                else:
                    print("Failed to add serials to order")
            else:
                print("No serials generated for this order")
            
            # Success response
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'status': 'success',
                    'serials_generated': serials_generated
                })
            }
            
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            return {
                'statusCode': 500,
                'body': str(e)
            }
    
    return {
        'statusCode': 405,
        'body': 'Method not allowed'
    }