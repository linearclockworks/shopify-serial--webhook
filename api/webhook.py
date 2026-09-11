# Summary: Unified Webhook & Manual Processing Hub with DYMO Print Queue Support
import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler

SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP_NAME', '')
SHOPIFY_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_SHEET_ID_CLEARTIME = os.environ.get('GOOGLE_SHEET_ID_CLEARTIME', '')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDENTIALS', '')

CLEARTIME_SKU_PREFIXES = ['CT', 'FA', 'MP', 'KIT', 'LED', 'HZ']

def calculate_line_item_discount(item: dict) -> float:
    """Calculates true line-item discount including order-level allocations."""
    quantity = int(item.get('quantity', 1)) or 1
    discount_allocations = item.get('discount_allocations', [])
    if discount_allocations:
        total_allocated = sum(float(alloc.get('amount', 0.0)) for alloc in discount_allocations)
        return round(total_allocated / quantity, 2)
    total_discount = float(item.get('total_discount', 0.0) or 0.0)
    return round(total_discount / quantity, 2)

def get_google_sheet(is_cleartime=False, worksheet_name=None):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet_id = GOOGLE_SHEET_ID_CLEARTIME if is_cleartime else GOOGLE_SHEET_ID
        spreadsheet = client.open_by_key(sheet_id)
        if not worksheet_name:
            worksheet_name = 'CTClocks' if is_cleartime else 'Clocks'
        return spreadsheet.worksheet(worksheet_name)
    except Exception as e:
        print(f"Sheet error: {e}")
        return None

def queue_label_for_printing(sku, serial, is_cleartime=True):
    try:
        sheet = get_google_sheet(is_cleartime=is_cleartime, worksheet_name='PrintQueue')
        if not sheet:
            return False
        sn_text = f"S/N {serial}" if not str(serial).startswith('S/N') else str(serial)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        row = [sku, sn_text, 'PENDING', timestamp]
        sheet.insert_row(row, index=2)
        print(f"✓ Queued DYMO label print job: {sku} | {sn_text}")
        return True
    except Exception as e:
        print(f"⚠️ Could not queue label print job: {e}")
        return False

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date, product_id):
    try:
        sheet = get_google_sheet(is_cleartime=False)
        if not sheet:
            return False
        if ':' in product_name:
            name_part = product_name.split(':', 1)[0].strip()
            description_part = product_name.split(':', 1)[1].strip()
        else:
            name_part = product_name
            description_part = ''
        serial_number_only = serial.replace('LCK-', '')
        product_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{product_id}"
        row = [
            serial_number_only, name_part, description_part,
            '', order_number, '', '', '', '', order_date,
            '', '', '', '', '', '', '', '', '', '', '', '', '', ''
        ]
        sheet.insert_row(row, index=2)
        try:
            sheet.update_cell(2, 2, f'=HYPERLINK("{product_url}", "{name_part}")')
            print(f"✓ Logged to Clocks sheet with hyperlink: {serial}")
        except Exception as e:
            print(f"⚠️ Logged to Clocks sheet but hyperlink failed: {e}")
        return True
    except Exception as e:
        print(f"✗ Sheet error: {e}")
        return False

def log_to_cleartime_sheet(sku, serial, order_number, customer_name, order_date):
    try:
        sheet = get_google_sheet(is_cleartime=True)
        if not sheet:
            return False
        row = [serial, sku, '', order_number, customer_name, '', '', '']
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to CTClocks sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ CTClocks sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
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

def shopify_graphql_call(query, variables=None):
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2026-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_TOKEN,
        'Content-Type': 'application/json'
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"✗ GraphQL API Error: {e}")
        return None

def get_next_serial(key='global_serial_counter', prefix='LCK-'):
    result = shopify_api_call(f'metafields.json?namespace=custom&key={key}')
    if not result:
        return None
    metafields = result.get('metafields', [])
    if metafields:
        mf = metafields[0]
        current = int(mf['value'])
        metafield_id = mf['id']
        serial = f"{prefix}{current}" if prefix else str(current)
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

def swap_tags(tags_string, add_featured_tag=False):
    tags = [t.strip() for t in tags_string.split(',') if t.strip()]
    tags = [t for t in tags if t.lower() != 'sample']
    if add_featured_tag:
        if 'featured' not in [t.lower() for t in tags]:
            tags.append('featured')
    else:
        tags = [t for t in tags if t.lower() != 'featured']
    return ', '.join(tags)

def get_location_id_by_name(location_name):
    try:
        result = shopify_api_call('locations.json')
        if result and result.get('locations'):
            for loc in result['locations']:
                if loc.get('name', '').lower() == location_name.lower():
                    return loc['id']
        return None
    except Exception as e:
        print(f"⚠️ Could not fetch locations: {e}")
        return None

def set_inventory_at_location(inventory_item_id, location_id, quantity):
    if not inventory_item_id or not location_id:
        return False
    try:
        shopify_api_call('inventory_levels/connect.json', method='POST', data={'inventory_item_id': int(inventory_item_id), 'location_id': int(location_id)})
        result = shopify_api_call('inventory_levels/set.json', method='POST', data={'inventory_item_id': int(inventory_item_id), 'location_id': int(location_id), 'available': int(quantity)})
        return bool(result)
    except Exception as e:
        return False

def create_product_from_sample(sample_product_id, serial, add_featured_tag=False, purchased_sku=None, variant_title=None):
    try:
        result = shopify_api_call(f'products/{sample_product_id}.json')
        if not result:
            return None
        sample = result.get('product', {})
        base_title = sample.get('title', '')
        serial_only = serial.replace('LCK-', '')
        original_tags = sample.get('tags', '')
        new_tags = swap_tags(original_tags, add_featured_tag=add_featured_tag)
        images = [{'src': img.get('src')} for img in sample.get('images', [])]
        variants = sample.get('variants', [])
        price = '0.00'
        matched_variant_title = ''
        if variants:
            price = variants[0].get('price', '0.00')
            if purchased_sku:
                for v in variants:
                    v_sku = v.get('sku', '')
                    if v_sku and v_sku.strip().lower() == purchased_sku.strip().lower():
                        price = v.get('price', price)
                        matched_variant_title = v.get('title', '')
                        break

        search_text = f"{variant_title or ''} {matched_variant_title} {purchased_sku or ''}".lower()
        size_label = ''
        if any(term in search_text for term in ['3-foot', '3 foot', '3ft', "3'"]) or '3' in (variant_title or ''):
            size_label = '3-foot'
        elif any(term in search_text for term in ['5-foot', '5 foot', '5ft', "5'"]) or '5' in (variant_title or ''):
            size_label = '5-foot'

        new_title = f"{base_title} {size_label}-{serial_only}" if size_label else f"{base_title}-{serial_only}"

        new_product = {
            'product': {
                'title': new_title,
                'body_html': sample.get('body_html', ''),
                'vendor': sample.get('vendor', ''),
                'product_type': 'Wall Clocks',
                'tags': new_tags,
                'status': 'active',
                'published': True,
                'images': images,
                'variants': [{'price': price, 'sku': serial, 'inventory_management': 'shopify'}]
            }
        }
        result = shopify_api_call('products.json', method='POST', data=new_product)
        if result and result.get('product'):
            new_product_id = result['product']['id']
            variant = result['product'].get('variants', [{}])[0]
            new_variant_id = variant.get('id')
            inventory_item_id = variant.get('inventory_item_id')
            if inventory_item_id:
                sanded_location_id = get_location_id_by_name('SandedNBranded')
                if sanded_location_id:
                    set_inventory_at_location(inventory_item_id, sanded_location_id, 1)
            return {'product_id': new_product_id, 'variant_id': new_variant_id, 'title': new_title}
        return None
    except Exception as e:
        return None

def create_sled_order_for_bryan(orig_order_number, sled_item_titles):
    try:
        line_items = [{'quantity': 1, 'price': '0.00', 'title': f"Sled for {t}", 'requires_shipping': True} for t in sled_item_titles]
        new_order = {
            'order': {
                'customer': {'first_name': 'Bryan', 'last_name': 'Crider'},
                'shipping_address': {'first_name': 'Bryan', 'last_name': 'Crider'},
                'line_items': line_items,
                'financial_status': 'paid',
                'note': f"Sled for Order #{orig_order_number.replace('#', '')}",
                'tags': 'Sled, Bryan Crider, Automated'
            }
        }
        result = shopify_api_call('orders.json', method='POST', data=new_order)
        return bool(result and result.get('order'))
    except Exception as e:
        return False

def execute_line_item_swap(order_id, old_line_item_id, new_variant_id, discount_amount=0.0, discount_description="Discount", currency_code="USD"):
    begin_mutation = """
    mutation orderEditBegin($id: ID!) {
      orderEditBegin(id: $id) { calculatedOrder { id } userErrors { field message } }
    }
    """
    res = shopify_graphql_call(begin_mutation, {"id": f"gid://shopify/Order/{order_id}"})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditBegin'):
        return False, "orderEditBegin error"
    calc_order_id = res['data']['orderEditBegin']['calculatedOrder']['id']

    remove_mutation = """
    mutation orderEditSetQuantity(\(id: ID!,\)lineItemId: ID!, $quantity: Int!) {
      orderEditSetQuantity(id: \(id, lineItemId:\)lineItemId, quantity: $quantity) { calculatedOrder { id } userErrors { field message } }
    }
    """
    shopify_graphql_call(remove_mutation, {"id": calc_order_id, "lineItemId": f"gid://shopify/LineItem/{old_line_item_id}", "quantity": 0})

    add_mutation = """
    mutation orderEditAddVariant(\(id: ID!,\)variantId: ID!, $quantity: Int!) {
      orderEditAddVariant(id: \(id, variantId:\)variantId, quantity: $quantity) {
        calculatedOrder { id addedLineItems(first: 5) { edges { node { id } } } }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(add_mutation, {"id": calc_order_id, "variantId": f"gid://shopify/ProductVariant/{new_variant_id}", "quantity": 1})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditAddVariant'):
        return False, "orderEditAddVariant error"

    if discount_amount > 0:
        added_edges = res['data']['orderEditAddVariant']['calculatedOrder'].get('addedLineItems', {}).get('edges', [])
        if added_edges:
            new_line_item_gid = added_edges[-1]['node']['id']
            discount_mutation = """
            mutation orderEditAddLineItemDiscount(\(id: ID!,\)lineItemId: ID!, $discount: OrderEditAppliedDiscountInput!) {
              orderEditAddLineItemDiscount(id: \(id, lineItemId:\)lineItemId, discount: $discount) { calculatedOrder { id } userErrors { field message } }
            }
            """
            shopify_graphql_call(discount_mutation, {"id": calc_order_id, "lineItemId": new_line_item_gid, "discount": {"description": discount_description, "fixedValue": {"amount": f"{discount_amount:.2f}", "currencyCode": currency_code}}})

    commit_mutation = """
    mutation orderEditCommit($id: ID!) {
      orderEditCommit(id: $id) { order { id } userErrors { field message } }
    }
    """
    shopify_graphql_call(commit_mutation, {"id": calc_order_id})
    return True, "Success"

def add_serial_to_order_note(order_id, lck_serials, cleartime_serials):
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        note_parts = []
        if lck_serials:
            note_parts.append(f"Serial Number: {', '.join(lck_serials)}")
        if cleartime_serials:
            note_parts.append(f"Cleartime Serial Numbers: {', '.join(cleartime_serials)}")
        serial_text = '\n'.join(note_parts)
        new_note = f"{current_note}\n{serial_text}" if current_note else serial_text
        shopify_api_call(f'orders/{order_id}.json', method='PUT', data={'order': {'note': new_note}})
        return True
    except Exception as e:
        return False

def process_order(order_data, add_featured_tag=False, force=False):
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')
    customer = order_data.get('customer', {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    currency_code = order_data.get('currency', 'USD')
    order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    products_created, lck_serials, cleartime_serials, bryan_sled_items = [], [], [], []

    for item in order_data.get('line_items', []):
        product_title = item.get('title', '')
        variant_title = item.get('variant_title', '')
        sku = item.get('sku', '')
        quantity = item.get('quantity', 1)
        current_qty = item.get('current_quantity', quantity)
        product_id = item.get('product_id')
        line_item_id = item.get('id')
        unit_discount = calculate_line_item_discount(item)

        if not current_qty or current_qty == 0:
            continue

        is_cleartime = any(sku.upper().startswith(p) for p in CLEARTIME_SKU_PREFIXES) if sku else False

        if sku and sku.upper().startswith('LCK-'):
            product_result = shopify_api_call(f'products/{product_id}.json')
            if product_result:
                tags = product_result.get('product', {}).get('tags', '')
                tags_list = [tag.strip().lower() for tag in tags.split(',') if tag.strip()]
                if 'sample' not in tags_list or 'featured' in tags_list:
                    continue
            else:
                continue

            for i in range(current_qty):
                serial = get_next_serial(key='global_serial_counter', prefix='LCK-')
                if not serial:
                    continue
                lck_serials.append(serial)
                new_product = create_product_from_sample(product_id, serial, add_featured_tag=add_featured_tag, purchased_sku=sku, variant_title=variant_title)
                if new_product:
                    products_created.append(new_product['title'])
                    bryan_sled_items.append(new_product['title'])
                    log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])
                    queue_label_for_printing(sku=sku, serial=serial, is_cleartime=False)
                    execute_line_item_swap(order_id, line_item_id, new_product['variant_id'], discount_amount=unit_discount, currency_code=currency_code)

        elif sku and is_cleartime:
            for i in range(current_qty):
                serial = get_next_serial(key='cleartime_serial_counter', prefix='')
                if serial:
                    cleartime_serials.append(serial)
                    log_to_cleartime_sheet(sku, serial, order_number, customer_name, order_date)
                    queue_label_for_printing(sku=sku, serial=f"11{serial}" if len(serial)==2 else serial, is_cleartime=True)

    if lck_serials or cleartime_serials:
        add_serial_to_order_note(order_id, lck_serials, cleartime_serials)

    if bryan_sled_items:
        create_sled_order_for_bryan(order_number, bryan_sled_items)

    return {
        'status': 'success',
        'order': order_number,
        'products': products_created,
        'lck_serials': lck_serials,
        'cleartime_serials': cleartime_serials
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Master webhook handler is active')

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        payload = json.loads(body.decode('utf-8'))
        result = process_order(payload, force=False)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode('utf-8'))