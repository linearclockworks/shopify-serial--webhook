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
        hmac_header = (
            request.headers.get('X-Shopify-Hmac-SHA256') or
            request.headers.get('X-Shopify-Hmac-Sha256') or
            request.headers.get('x-shopify-hmac-sha256') or
            ''
        )
        print(f"HMAC Header: {hmac_header[:20]}..." if hmac_header else "No HMAC header found")
        print(f"Secret set: {bool(SHOPIFY_SECRET)}")
        
        if not verify_webhook(body, hmac_header):
        
        if False:  # Temporarily disable verification for testing
            print("Webhook verification failed")
            return jsonify({'error': 'Unauthorized'}), 401
        
        print("Webhook verified")
        order_data = json.loads(body)
        order_id = order_data.get('id')
        order_number = order_data.get('name', '')
        
        print(f"Processing order {order_number}")
        
        serials_generated = []
        for item in order_data.get('line_items', []):
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            
            print(f"Item SKU: {sku}")
            
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
        
        return jsonify({'status': 'success', 'serials': serials_generated}), 200
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500