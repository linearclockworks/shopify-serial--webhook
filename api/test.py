from flask import Flask, request, jsonify
import json
import os
import urllib.request
import urllib.error

app = Flask(__name__)

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

def shopify_api_call(endpoint, method='GET', data=None):
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
        return jsonify({'error': 'Missing order_id parameter'}), 400
    try:
        serial, counter = get_next_serial()
        if serial:
            success = add_serial_to_order(order_id, serial)
            if success:
                return jsonify({'status': 'success', 'serial': serial}), 200
        return jsonify({'error': 'Failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
