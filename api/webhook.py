# Summary: Unified Webhook & Manual Processing Hub with DYMO Print Queue Support
# 1. Handles both Hardwood (LCK-) and Cleartime (CT, FA, MP, KIT, LED) orders.
# 2. Generates unique serial numbers (LCK-#### for Hardwood, numbers for Cleartime).
# 3. Clones products, swaps order line items via GraphQL, updates order notes.
# 4. Logs to Google Sheets AND appends to 'PrintQueue' for automatic DYMO printing.

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

CLEARTIME_SKU_PREFIXES = ['CT', 'FA', 'MP', 'KIT', 'LED']

def get_google_sheet(is_cleartime=False, worksheet_name=None):
    """Connect to a specific Google Sheet worksheet"""
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
    """Add label job to PrintQueue tab for local DYMO listener"""
    try:
        # Try to open 'PrintQueue' worksheet; create if missing
        sheet = get_google_sheet(is_cleartime=is_cleartime, worksheet_name='PrintQueue')
        if not sheet:
            return False

        sn_text = f"S/N {serial}" if not str(serial).startswith('S/N') else str(serial)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Columns: [SKU, Serial/SN, Status, Timestamp]
        row = [sku, sn_text, 'PENDING', timestamp]
        sheet.insert_row(row, index=2)
        print(f"✓ Queued DYMO label print job: {sku} | {sn_text}")
        return True
    except Exception as e:
        print(f"⚠️ Could not queue label print job: {e}")
        return False

def log_to_google_sheet(product_name, serial, order_number, customer_name, order_date, product_id):
    """Add a new row to the main LCK Clocks Google Sheet with hyperlink"""
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
    """Add a new row to the CTClocks Google Sheet"""
    try:
        sheet = get_google_sheet(is_cleartime=True)
        if not sheet:
            return False
        
        row = [
            serial,          # A: Serial number
            sku,             # B: SKU
            '',              # C: Event tags
            order_number,    # D: Order number
            customer_name,   # E: Customer name
            '',              # F: Run length
            '',              # G: Steps
            '',              # H: Comments
        ]
        
        sheet.insert_row(row, index=2)
        print(f"✓ Logged to CTClocks sheet: {serial}")
        return True
    except Exception as e:
        print(f"✗ CTClocks sheet error: {e}")
        return False

def shopify_api_call(endpoint, method='GET', data=None):
    """Utility wrapper for executing REST API calls against Shopify Admin"""
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
    """Utility wrapper for executing Shopify Admin GraphQL API mutations"""
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
    """Fetch next serial from Shopify Metafields counter and increment"""
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
    """Remove 'sample' tag. Add 'featured' tag only if add_featured_tag=True."""
    tags = [t.strip() for t in tags_string.split(',') if t.strip()]
    tags = [t for t in tags if t.lower() != 'sample']
    
    if add_featured_tag:
        if 'featured' not in [t.lower() for t in tags]:
            tags.append('featured')
    else:
        tags = [t for t in tags if t.lower() != 'featured']
    
    return ', '.join(tags)

def get_location_id_by_name(location_name):
    """Get Shopify location ID by location name"""
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
    """Connect inventory item to target location and set available level"""
    if not inventory_item_id or not location_id:
        print(f"✗ Cannot configure inventory: item_id or location_id is missing.")
        return False
    try:
        connect_data = {
            'inventory_item_id': int(inventory_item_id),
            'location_id': int(location_id)
        }
        shopify_api_call('inventory_levels/connect.json', method='POST', data=connect_data)
        
        adjust_data = {
            'inventory_item_id': int(inventory_item_id),
            'location_id': int(location_id),
            'available': int(quantity)
        }
        result = shopify_api_call('inventory_levels/set.json', method='POST', data=adjust_data)
        if result:
            print(f"✓ Connected & set inventory at location {location_id}: qty={quantity}")
            return True
        else:
            print(f"⚠️ Failed to apply inventory level setting at location {location_id}")
            return False
    except Exception as e:
        print(f"⚠️ Inventory processing failed at location {location_id}: {e}")
        return False

def create_product_from_sample(sample_product_id, serial, add_featured_tag=False, purchased_sku=None, variant_title=None):
    """Create a new product based on the sample product with exact SKU matching and size in title"""
    try:
        result = shopify_api_call(f'products/{sample_product_id}.json')
        if not result:
            print(f"✗ Could not fetch sample product {sample_product_id}")
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
                        print(f"✓ Match found! Variant SKU '{v_sku}' price used: ${price}")
                        break

        # Determine size label (3-foot or 5-foot) based on selected variant
        search_text = f"{variant_title or ''} {matched_variant_title} {purchased_sku or ''}".lower()
        size_label = ''
        
        if any(term in search_text for term in ['3-foot', '3 foot', '3ft', "3'"]) or '3' in (variant_title or ''):
            size_label = '3-foot'
        elif any(term in search_text for term in ['5-foot', '5 foot', '5ft', "5'"]) or '5' in (variant_title or ''):
            size_label = '5-foot'

        if size_label:
            new_title = f"{base_title} {size_label}-{serial_only}"
        else:
            new_title = f"{base_title}-{serial_only}"

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
                'variants': [
                    {
                        'price': price,
                        'sku': serial,
                        'inventory_management': 'shopify'
                    }
                ]
            }
        }

        result = shopify_api_call('products.json', method='POST', data=new_product)

        if result and result.get('product'):
            new_product_id = result['product']['id']
            variant = result['product'].get('variants', [{}])[0]
            new_variant_id = variant.get('id')
            inventory_item_id = variant.get('inventory_item_id')
            
            if not inventory_item_id and new_variant_id:
                print("🔄 Fetching complete variant payload to resolve inventory tracking records...")
                variant_res = shopify_api_call(f'variants/{new_variant_id}.json')
                if variant_res and variant_res.get('variant'):
                    inventory_item_id = variant_res['variant'].get('inventory_item_id')
            
            if inventory_item_id:
                sanded_location_id = get_location_id_by_name('SandedNBranded')
                if sanded_location_id:
                    set_inventory_at_location(inventory_item_id, sanded_location_id, 1)
                    tag_status = "featured (visible)" if add_featured_tag else "no featured tag (hidden)"
                    print(f"✓ Created from 'sample' tag → qty=1 at location, {tag_status}")
                else:
                    print(f"⚠️ Could not find 'SandedNBranded' location - using default location")
            
            return {'product_id': new_product_id, 'variant_id': new_variant_id, 'title': new_title}
        return None
    except Exception as e:
        print(f"✗ Error creating product: {e}")
        return None

def execute_line_item_swap(order_id, old_line_item_id, new_variant_id):
    """Executes GraphQL sequence to swap out the sample for the serialized product on an order"""
    print(f"Starting line item swap sequence for Order ID: {order_id}")
    
    begin_mutation = """
    mutation orderEditBegin($id: ID!) {
      orderEditBegin(id: $id) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(begin_mutation, {"id": f"gid://shopify/Order/{order_id}"})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditBegin'):
        return False, f"orderEditBegin error: {res}"
    
    edit_data = res['data']['orderEditBegin']
    if edit_data.get('userErrors'):
        return False, f"orderEditBegin user error: {edit_data['userErrors'][0]['message']}"
        
    calc_order_id = edit_data['calculatedOrder']['id']
    print(f"✓ Order edit session started: {calc_order_id}")
    
    line_item_gids = [
        f"gid://shopify/LineItem/{old_line_item_id}",
        f"gid://shopify/CalculatedLineItem/{old_line_item_id}"
    ]
    
    remove_mutation = """
    mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
      orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    
    swap_success = False
    last_error_msg = ""
    
    for item_gid in line_item_gids:
        res = shopify_graphql_call(remove_mutation, {
            "id": calc_order_id, 
            "lineItemId": item_gid, 
            "quantity": 0
        })
        if res and 'data' in res and res['data'].get('orderEditSetQuantity') and not res['data']['orderEditSetQuantity'].get('userErrors'):
            print(f"✓ Successfully staged removal of sample line item using {item_gid.split('/')[-2]}")
            swap_success = True
            break
        else:
            last_error_msg = f"orderEditSetQuantity error: {res}"
            
    if not swap_success:
        return False, f"Line item removal failed. Details: {last_error_msg}"

    add_mutation = """
    mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
      orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
        calculatedOrder { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(add_mutation, {
        "id": calc_order_id, 
        "variantId": f"gid://shopify/ProductVariant/{new_variant_id}", 
        "quantity": 1
    })
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditAddVariant') or res['data']['orderEditAddVariant'].get('userErrors'):
        return False, f"orderEditAddVariant failure: {res}"
        
    print(f"✓ Successfully staged addition of serialized unique variant")

    commit_mutation = """
    mutation orderEditCommit($id: ID!) {
      orderEditCommit(id: $id) {
        order { id }
        userErrors { field message }
      }
    }
    """
    res = shopify_graphql_call(commit_mutation, {"id": calc_order_id})
    if not res or 'errors' in res or not res.get('data', {}).get('orderEditCommit') or res['data']['orderEditCommit'].get('userErrors'):
        return False, f"orderEditCommit failure: {res}"
        
    return True, "Success"

def add_serial_to_order_note(order_id, lck_serials, cleartime_serials, swap_status=None):
    """Append serial numbers to order notes"""
    try:
        result = shopify_api_call(f'orders/{order_id}.json')
        if not result:
            return False
        
        order = result.get('order', {})
        current_note = order.get('note', '') or ''
        
        note_parts = []
        status_text = f" ({swap_status})" if swap_status else ""
        
        if lck_serials:
            note_parts.append(f"Serial Number: {', '.join(lck_serials)}{status_text}")
        if cleartime_serials:
            note_parts.append(f"Cleartime Serial Numbers: {', '.join(cleartime_serials)}")
        
        serial_text = '\n'.join(note_parts)
        new_note = f"{current_note}\n{serial_text}" if current_note else serial_text
        
        shopify_api_call(f'orders/{order_id}.json', method='PUT', data={'order': {'note': new_note}})
        return True
    except Exception as e:
        print(f"✗ Error updating note: {e}")
        return False

def try_acquire_processing_lock(order_id):
    """Atomic locking mechanism to avoid duplicate webhooks"""
    try:
        result = shopify_api_call(f'orders/{order_id}/metafields.json?namespace=webhook&key=processing_lock')
        if result and result.get('metafields'):
            return False
        lock_data = {
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_lock',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        }
        shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data=lock_data)
        return True
    except:
        return False

def mark_order_as_completed(order_id):
    """Mark order completed with metafield tag"""
    try:
        shopify_api_call(f'orders/{order_id}/metafields.json', method='POST', data={
            'metafield': {
                'namespace': 'webhook',
                'key': 'processing_completed',
                'value': datetime.now().isoformat(),
                'type': 'single_line_text_field'
            }
        })
        return True
    except:
        return False

def process_order(order_data, add_featured_tag=False, force=False):
    """Process an order for both Hardwood and Cleartime clocks"""
    order_id = order_data.get('id')
    order_number = order_data.get('name', '')

    if not force and not try_acquire_processing_lock(order_id):
        return {'status': 'already_processing', 'order': order_number}

    try:
        customer = order_data.get('customer', {})
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        created_at = order_data.get('created_at', '')
        try:
            order_date = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        except:
            order_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(f"Processing order {order_number} (ID: {order_id}) force={force}")

        products_created = []
        lck_serials = []
        cleartime_serials = []

        for item in order_data.get('line_items', []):
            product_title = item.get('title', '')
            variant_title = item.get('variant_title', '')
            sku = item.get('sku', '')
            quantity = item.get('quantity', 1)
            current_qty = item.get('current_quantity', quantity)
            product_id = item.get('product_id')
            line_item_id = item.get('id')

            print(f"Line item: {product_title} - {variant_title} (SKU: {sku}, Qty: {quantity}, Current: {current_qty})")

            if not current_qty or current_qty == 0:
                print(f"⏭️ Skipping removed line item: {product_title}")
                continue

            is_cleartime = any(sku.upper().startswith(p) for p in CLEARTIME_SKU_PREFIXES) if sku else False

            # Hardwood Clocks (SKU starts with LCK-)
            if sku and sku.upper().startswith('LCK-'):
                product_result = shopify_api_call(f'products/{product_id}.json')
                if product_result:
                    tags = product_result.get('product', {}).get('tags', '')
                    tags_list = [tag.strip().lower() for tag in tags.split(',') if tag.strip()]

                    if 'featured' in tags_list:
                        print(f"⏭️ Skipping - product tagged 'featured'")
                        continue
                    if 'sample' not in tags_list:
                        print(f"⏭️ Skipping - product not tagged 'sample'")
                        continue
                else:
                    print(f"⚠️ Could not fetch product tags - skipping for safety")
                    continue

                for i in range(current_qty):
                    serial = get_next_serial(key='global_serial_counter', prefix='LCK-')
                    if not serial:
                        continue

                    lck_serials.append(serial)
                    new_product = create_product_from_sample(
                        product_id, 
                        serial, 
                        add_featured_tag=add_featured_tag, 
                        purchased_sku=sku,
                        variant_title=variant_title
                    )
                    
                    if new_product:
                        products_created.append(new_product['title'])
                        log_to_google_sheet(new_product['title'], serial, order_number, customer_name, order_date, new_product['product_id'])
                        queue_label_for_printing(sku=sku, serial=serial, is_cleartime=False)
                        
                        swap_success, swap_msg = execute_line_item_swap(order_id, line_item_id, new_product['variant_id'])
                        swap_status = "Swapped successfully" if swap_success else "Note fallback only"
                        print(f"Order swap status for {serial}: {swap_status} ({swap_msg if not swap_success else ''})")

            # Cleartime Clocks (SKU starts with CT, FA, MP, KIT, LED)
            elif sku and is_cleartime:
                for i in range(current_qty):
                    serial = get_next_serial(key='cleartime_serial_counter', prefix='')
                    if serial:
                        cleartime_serials.append(serial)
                        log_to_cleartime_sheet(sku, serial, order_number, customer_name, order_date)
                        queue_label_for_printing(sku=sku, serial=f"11{serial}" if len(serial)==2 else serial, is_cleartime=True)

        if lck_serials or cleartime_serials:
            add_serial_to_order_note(order_id, lck_serials, cleartime_serials)

        if not force:
            mark_order_as_completed(order_id)

        return {
            'status': 'success',
            'order': order_number,
            'products': products_created,
            'lck_serials': lck_serials,
            'cleartime_serials': cleartime_serials
        }
    except Exception as e:
        print(f"ERROR during processing: {e}")
        raise

# ── Manual trigger UI ────────────────────────────────────────────────────────

MANUAL_TRIGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Master Webhook Trigger</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f6f8fa; color: #111; }
  h1 { margin: 0 0 6px; font-size: 1.4rem; }
  .card { background: #fff; border: 1px solid #e1e4e8; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
  label { font-weight: 600; font-size: .9rem; display: block; margin-bottom: 6px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccc; border-radius: 8px; font-size: 1rem; }
  button { margin-top: 12px; padding: 10px 20px; background: #0b61d8; color: #fff; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; font-weight: 600; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; margin-top: 15px; }
  th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; font-size: .9rem; }
  th { background: #f7f9fc; font-weight: 600; }
  .removed-row td { text-decoration: line-through; color: #999; }
  .removed-label { color: #d93025; font-weight: bold; text-decoration: none !important; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: .75rem; background: #e8f0fe; color: #1a56db; margin-right: 4px; }
  .tag.sample { background: #fce8e6; color: #c0392b; }
  .tag.featured { background: #e6f4ea; color: #1e7e34; }
  .log { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 14px; font-family: monospace; font-size: .85rem; white-space: pre-wrap; display: none; margin-top: 16px; }
  .info-box { background: #eef3fc; border: 1px solid #c7d8f8; border-radius: 8px; padding: 12px 15px; margin-bottom: 20px; font-size: 0.9rem; color: #1a3e75; line-height: 1.5; }
  a { color: #0b61d8; text-decoration: none; font-weight: 500; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>⚡ Master Webhook Trigger</h1>
<div class="info-box">
  Processes both <strong>Hardwood (LCK-)</strong> and <strong>Cleartime (CT, FA, MP, KIT, LED)</strong> clocks.<br>
  • <strong>Hardwood:</strong> Clones sample product, swaps line item, logs to <a href="https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID" target="_blank">Clocks Spreadsheet ↗</a><br>
  • <strong>Cleartime:</strong> Generates serial number, updates notes, logs to <a href="https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID_CLEARTIME" target="_blank">CTClocks Spreadsheet ↗</a>
</div>

<div class="card">
  <label for="orderInput">Order Number</label>
  <input type="text" id="orderInput" placeholder="2882 or #2882">
  
  <div style="margin-top:16px;">
    <label style="font-weight:600; font-size:.9rem;">Build Type (for Hardwood LCK- clocks only)</label>
    <div style="display:flex; flex-direction:column; gap:8px; margin-top:6px;">
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="stock">
        <span><strong>Stock build</strong> — tagged "featured", visible on site</span>
      </label>
      <label style="display:flex; align-items:center; gap:6px; font-weight:400; cursor:pointer;">
        <input type="radio" name="buildType" value="customer" checked>
        <span><strong>Customer order</strong> — no "featured" tag, hidden from site</span>
      </label>
    </div>
  </div>

  <button id="lookupBtn">Look Up Order</button>

  <div id="orderInfo" style="display:none">
    <table>
      <thead>
        <tr>
          <th>Product</th>
          <th>SKU</th>
          <th>Tags</th>
          <th>Qty</th>
        </tr>
      </thead>
      <tbody id="itemsBody"></tbody>
    </table>
    <button style="background:#c0392b" id="fireBtn">🔥 Force Process This Order</button>
    <div class="log" id="logBox"></div>
  </div>
</div>

<script>
let currentOrderId = null;

async function lookup() {
  const raw = document.getElementById('orderInput').value.trim().replace('#','');
  if (!raw) return;
  const btn = document.getElementById('lookupBtn');
  btn.disabled = true;
  btn.textContent = 'Searching...';
  document.getElementById('orderInfo').style.display = 'none';
  
  try {
    const resp = await fetch('/api/lookup?order=' + encodeURIComponent(raw));
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);

    currentOrderId = data.order_id;
    const tbody = document.getElementById('itemsBody');
    tbody.innerHTML = '';
    
    let validItems = 0;
    data.items.forEach(item => {
      const isRemoved = item.current_qty === 0;
      if (!isRemoved) validItems++;
      
      const tr = document.createElement('tr');
      if (isRemoved) tr.className = 'removed-row';
      
      const tagsHtml = item.tags.map(t => {
        const cls = t === 'sample' ? 'tag sample' : t === 'featured' ? 'tag featured' : 'tag';
        return `<span class="${cls}">${t}</span>`;
      }).join('');

      tr.innerHTML = `
        <td>${item.title}</td>
        <td>${item.sku || '—'}</td>
        <td>${tagsHtml || '—'}</td>
        <td>${isRemoved ? '<span class="removed-label">REMOVED (0)</span>' : item.current_qty}</td>
      `;
      tbody.appendChild(tr);
    });

    document.getElementById('fireBtn').disabled = validItems === 0;
    document.getElementById('orderInfo').style.display = 'block';
    document.getElementById('logBox').style.display = 'none';
  } catch(e) { 
    alert('Lookup Error: ' + e.message); 
  } finally { 
    btn.disabled = false; 
    btn.textContent = 'Look Up Order'; 
  }
}

async function fire() {
  if (!currentOrderId) return;
  if (!confirm('Force-process this order? Serial numbers will be assigned and Google Sheets updated.')) return;

  const btn = document.getElementById('fireBtn');
  const log = document.getElementById('logBox');
  btn.disabled = true;
  btn.textContent = 'Processing...';
  log.style.display = 'block';
  log.textContent = 'Sending request...';

  try {
    const buildType = document.querySelector('input[name=buildType]:checked').value;
    const resp = await fetch('/api/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        order_id: currentOrderId,
        add_featured_tag: buildType === 'stock'
      })
    });
    const res = await resp.json();
    if (!resp.ok) throw new Error(res.error || resp.statusText);

    log.textContent = JSON.stringify(res, null, 2);
    btn.textContent = '✓ Complete';
  } catch(e) { 
    log.textContent = 'Error: ' + e.message; 
    btn.disabled = false; 
    btn.textContent = '🔥 Force Process This Order';
  }
}

document.getElementById('lookupBtn').addEventListener('click', lookup);
document.getElementById('orderInput').addEventListener('keydown', e => { if (e.key === 'Enter') lookup(); });
document.getElementById('fireBtn').addEventListener('click', fire);
</script>
</body>
</html>
"""

# ── Serverless Route Handler ──────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    
    def send_json(self, code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        if parsed.path in ('/', '/api', '/api/'):
            lck_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}" if GOOGLE_SHEET_ID else "#"
            ct_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID_CLEARTIME}" if GOOGLE_SHEET_ID_CLEARTIME else "#"
            
            ui_html = MANUAL_TRIGGER_HTML.replace('GOOGLE_SHEET_ID_CLEARTIME', GOOGLE_SHEET_ID_CLEARTIME or '').replace('GOOGLE_SHEET_ID', GOOGLE_SHEET_ID or '')
            ui_html = ui_html.replace('https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID', lck_url)
            ui_html = ui_html.replace('https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID_CLEARTIME', ct_url)
            
            self.send_html(ui_html)
            return

        if parsed.path == '/api/lookup':
            qs = parse_qs(parsed.query)
            order_num = (qs.get('order') or [''])[0].strip()
            if not order_num:
                self.send_json(400, {'error': 'order parameter required'})
                return
            try:
                result = shopify_api_call(f'orders.json?name=%23{order_num}&status=any')
                if not result or not result.get('orders'):
                    self.send_json(404, {'error': f'Order #{order_num} not found in Shopify'})
                    return

                order = result['orders'][0]
                items = []
                for item in order.get('line_items', []):
                    product_id = item.get('product_id')
                    tags_list = []
                    if product_id:
                        pr = shopify_api_call(f'products/{product_id}.json')
                        if pr:
                            tags_str = pr.get('product', {}).get('tags', '')
                            tags_list = [t.strip().lower() for t in tags_str.split(',') if t.strip()]
                    
                    items.append({
                        'title': item.get('title', ''),
                        'sku': item.get('sku', ''),
                        'current_qty': item.get('current_quantity', item.get('quantity', 1)),
                        'tags': tags_list
                    })

                self.send_json(200, {
                    'order_id': order['id'],
                    'order_number': order.get('name'),
                    'items': items
                })
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Master webhook handler is active')

    def do_POST(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            self.send_json(400, {'error': f'Invalid JSON payload: {e}'})
            return

        if parsed.path == '/api/manual':
            order_id = payload.get('order_id')
            if not order_id:
                self.send_json(400, {'error': 'order_id is required'})
                return
            try:
                order_res = shopify_api_call(f'orders/{order_id}.json')
                if not order_res or not order_res.get('order'):
                    self.send_json(404, {'error': 'Order not found in Shopify'})
                    return
                
                result = process_order(
                    order_res['order'], 
                    add_featured_tag=payload.get('add_featured_tag', False), 
                    force=True
                )
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            return

        print("=" * 60)
        print("SHOPIFY WEBHOOK RECEIVED")
        print("=" * 60)
        try:
            result = process_order(payload, add_featured_tag=False, force=False)
            self.send_json(200, result)
        except Exception as e:
            print(f"ERROR processing webhook: {e}")
            import traceback
            traceback.print_exc()
            self.send_json(500, {'error': str(e)})