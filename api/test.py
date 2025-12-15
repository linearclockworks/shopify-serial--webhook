import json
import os
import urllib.request
import urllib.error

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

def get_google_sheet():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        print(f"Sheet ID: {GOOGLE_SHEET_ID}")
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        sheet = spreadsheet.worksheet('Clocks')
        print(f"Connected to sheet: {sheet.title}")
        return sheet
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return None

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date):
    try:
        print(f"Logging: {serial}")
        sheet = get_google_sheet()
        if not sheet:
            return False
        
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        
        row = [serial, name_part, description_part, '', order_number, '', '', '', '', 
               order_date, '', '', '', '', '', '', '', '', '', '', '', '', '']
        
        sheet.append_row(row)
        print(f"Logged: {serial}")
        return True
    except Exception as e:
        print(f"Error: {e}")
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

def handler(event, context):
    # Parse query parameters
    query = event.get('queryStringParameters', {}) or {}
    order_id = query.get('order_id')
    
    if not order_id:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Missing order_id'})
        }
    
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return {
                'statusCode': 500,
                'body': json.dumps({'error': 'Could not fetch order'})
            }
        
        order = result.get('order', {})
        order_number = order.get('name', '')
        customer = order.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        line_items = order.get('line_items', [])
        product_name = line_items[0].get('title', '') if line_items else 'Unknown'
        
        from datetime import datetime
        order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        serial, counter = get_next_serial()
        if serial:
            success = add_serial_to_order(order_id, serial)
            if success:
                sheet_result = log_to_google_sheet(product_name, serial, order_number, customer_name, order_date)
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({
                        'status': 'success',
                        'serial': serial,
                        'order_number': order_number,
                        'logged_to_sheet': sheet_result
                    })
                }
        
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Failed'})
        }
    except Exception as e:
        import traceback
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e), 'traceback': traceback.format_exc()})
        }