# api/webhook.py
import json
import hmac
import hashlib
import base64
import os
import urllib.request
import urllib.error

# Shopify config
SHOPIFY_SECRET = os.environ.get('SHOPIFY_API_SECRET')
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME')
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
    else:
        print("No metafield found")
        return None, None

def add_serial_to_order(order_id, serial):
    """Add serial to order note"""
    result = shopify_api_call(f'orders/{order_id}.json')
    if not result:
        print(f"Failed to get order {order_id}")
        return False
    
    order = result.get('order', {})
    current_note = order.get('note', '') or ''
    
    if current_note:
        new_note = f"{current_note}\nSerial: {serial}"
    else:
        new_note = f"Serial: {serial}"
    
    update_data = {'order': {'id': order_id, 'note': new_note}}
    result = shopify_api_call(f'orders/{order_id}.json', method='PUT', data=update_data)
    return result is not None

def handler(event, context):
    """AWS Lambda style handler for Vercel"""
    
    # Parse the event
    http_method = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    headers = event.get('headers', {})
    body = event.get('body', '')
    
    # Handle GET request
    if http_method == 'GET':
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'body': 'Webhook handler is running'
        }
    
    # Handle POST request
    if http_method == 'POST':
        try:
            body_bytes = body.encode('utf-8') if isinstance(body, str) else body
            hmac_header = headers.get('x-shopify-hmac-sha256', '')
            
            if not verify_webhook(body_bytes, hmac_header):
                print("Webhook verification failed")
                return {'statusCode': 401, 'body': 'Unauthorized'}
            
            print("Webhook verified successfully")
            order_data = json.loads(body_bytes.decode('utf-8'))
            order_id = order_data.get('id')
            order_number = order_data.get('name', '')
            
            print(f"Processing order {order_number} (ID: {order_id})")
            
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
            
            if serials_generated:
                serial_text = ', '.join(serials_generated)
                print(f"Adding to order: {serial_text}")
                add_serial_to_order(order_id, serial_text)
            
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'status': 'success', 'serials': serials_generated})
            }
            
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            return {'statusCode': 500, 'body': str(e)}
    
    return {'statusCode': 405, 'body': 'Method not allowed'}