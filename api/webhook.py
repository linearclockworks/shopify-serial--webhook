# Save this file on your workshop PC connected to the DYMO printer
# Run: python dymo_print_listener.py

import time
import json
import os
import sys
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# Try importing Windows printing API; fallback to POSIX lpr
try:
    import win32print
    import win32ui
    from PIL import ImageWin
    IS_WINDOWS = True
except ImportError:
    import subprocess
    IS_WINDOWS = False

# Configuration
GOOGLE_CREDS_FILE = 'credentials.json'  # Path to your Google Service Account JSON file
GOOGLE_SHEET_ID_CLEARTIME = '1Goxzf28QSIc5szy7O-IIt1UN725nKSmOzPgWVQR5-Sw'
LOGO_IMAGE_PATH = 'linear_logo.png'     # Place your tree logo image in the same directory
PRINTER_NAME_SEARCH = "DYMO"             # Substring to match installed DYMO printer

def get_google_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID_CLEARTIME)
    
    try:
        return spreadsheet.worksheet('PrintQueue')
    except:
        # Create tab if it doesn't exist yet
        sheet = spreadsheet.add_worksheet(title='PrintQueue', rows="100", cols="10")
        sheet.insert_row(['SKU', 'Serial', 'Status', 'Timestamp'], index=1)
        return sheet

def generate_dymo_label_image(sku: str, serial_text: str, output_path: str = "label_temp.png"):
    """
    Renders a 300 DPI label PNG matching your exact physical DYMO label design:
    Top: Linear Clockworks Tree Logo
    Middle: Large SKU (e.g., LED4018OUTER)
    Bottom: Large S/N (e.g., S/N 1175)
    """
    # Standard DYMO 30252 / 30334 Dimensions at 300 DPI (approx 2.1" x 1.1" -> 636 x 330 px)
    width, height = 636, 330
    
    img = Image.new('RGB', (width, height), color='white')
    draw = ImageDraw.Draw(img)
    
    # 1. Draw Logo
    if os.path.exists(LOGO_IMAGE_PATH):
        try:
            logo = Image.open(LOGO_IMAGE_PATH).convert("RGBA")
            logo.thumbnail((450, 100))  # Scale logo
            
            # Center logo horizontally
            logo_x = (width - logo.width) // 2
            img.paste(logo, (logo_x, 15), logo)
        except Exception as e:
            print(f"⚠️ Logo render error: {e}")
    else:
        # Text fallback if logo image file isn't present
        font_logo = ImageFont.truetype("arial.ttf", 32)
        draw.text((width//2, 30), "LINEAR CLOCKWORKS", fill="black", font=font_logo, anchor="mm")

    # 2. Fonts
    try:
        font_sku = ImageFont.truetype("arialbd.ttf", 46)   # Bold SKU
        font_sn = ImageFont.truetype("arial.ttf", 44)      # S/N
    except:
        font_sku = font_sn = ImageFont.load_default()

    # 3. Draw SKU Line
    draw.text((width // 2, 175), sku, fill="black", font=font_sku, anchor="mm")
    
    # 4. Draw Serial Number Line
    sn_display = f"S/N {serial_text}" if not str(serial_text).startswith("S/N") else str(serial_text)
    draw.text((width // 2, 245), sn_display, fill="black", font=font_sn, anchor="mm")

    img.save(output_path)
    return output_path

def find_dymo_printer():
    """Find local printer name matching 'DYMO'"""
    if IS_WINDOWS:
        printers = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
        for p in printers:
            if PRINTER_NAME_SEARCH.lower() in p.lower():
                return p
        return printers[0] if printers else None
    return "DYMO_LabelWriter"

def print_image_to_dymo(image_path):
    """Send rendered label PNG directly to physical printer"""
    printer_name = find_dymo_printer()
    if not printer_name:
        print("✗ Error: No DYMO printer detected on this PC.")
        return False

    print(f"🖨️ Printing to: {printer_name}...")

    if IS_WINDOWS:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            hdc = win32ui.CreateDC()
            hdc.CreatePrinterDC(printer_name)
            hdc.StartDoc("DYMO Clock Serial Label")
            hdc.StartPage()

            bmp = Image.open(image_path)
            dib = ImageWin.Dib(bmp)
            dib.draw(hdc.GetHandleOutput(), (0, 0, bmp.size[0], bmp.size[1]))

            hdc.EndPage()
            hdc.EndDoc()
            return True
        finally:
            win32print.ClosePrinter(hprinter)
    else:
        cmd = f'lpr -P "{printer_name}" "{image_path}"'
        subprocess.run(cmd, shell=True)
        return True

def main():
    print("==================================================")
    print("🚀 DYMO Automatic Label Print Listener Active")
    print("==================================================")
    
    while True:
        try:
            sheet = get_google_sheet()
            if sheet:
                records = sheet.get_all_records()
                
                # Scan for PENDING rows (bottom to top or top to bottom)
                for index, row in enumerate(records, start=2):
                    if row.get('Status') == 'PENDING':
                        sku = str(row.get('SKU', '')).strip()
                        serial = str(row.get('Serial', '')).strip()
                        
                        print(f"\n✨ New Print Job Found! SKU={sku}, Serial={serial}")
                        
                        img_path = generate_dymo_label_image(sku, serial)
                        success = print_image_to_dymo(img_path)
                        
                        if success:
                            sheet.update_cell(index, 3, 'PRINTED')
                            print("✓ Label printed & status updated to PRINTED")
                        else:
                            sheet.update_cell(index, 3, 'FAILED')
                            
        except Exception as e:
            print(f"Listener error: {e}")

        time.sleep(5)  # Poll sheet every 5 seconds

if __name__ == '__main__':
    main()