import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import pdfplumber
import json
import os
import csv
from tkinter import filedialog
import glob

# Prompt user for PDF folder path
root = tk.Tk()
pdf_folder = filedialog.askdirectory(title="Select folder containing PDFs")
if not pdf_folder:
    print("No folder selected. Exiting.")
    root.destroy()
    exit(1)
pdf_files = sorted(glob.glob(os.path.join(pdf_folder, '*.pdf')))
if not pdf_files:
    print("No PDF files found in the selected folder.")
    root.destroy()
    exit(1)

# Use the first PDF for bounding box selection if needed
pdf_path = pdf_files[0]

# Set max display size
MAX_WIDTH = 2000
MAX_HEIGHT = 2000

# Open PDF and get first page as image
with pdfplumber.open(pdf_path) as pdf:
    page = pdf.pages[0]
    pil_image = page.to_image(resolution=150).original
    orig_width, orig_height = pil_image.size
    scale = min(MAX_WIDTH / orig_width, MAX_HEIGHT / orig_height, 1.0)
    display_width = int(orig_width * scale)
    display_height = int(orig_height * scale)
    # Use LANCZOS for high-quality downsampling (handle Pillow version differences)
    try:
        from PIL import Image
        resample_filter = Image.Resampling.LANCZOS
    except Exception:
        resample_filter = 1  # 1 is the value for LANCZOS in older Pillow versions
    display_image = pil_image.resize((display_width, display_height), resample_filter)
    # Print and use PDF mediabox for mapping
    print(f"DEBUG: page.bbox = {page.bbox}")
    print(f"DEBUG: page.mediabox = {page.mediabox}")
    pdf_x0, pdf_y0, pdf_x1, pdf_y1 = page.mediabox

# Tkinter GUI to draw rectangle and get coordinates
class PDFCropper(tk.Toplevel):
    def __init__(self, master, image, scale, pdf_bbox, display_height, bbox_dict):
        super().__init__(master)
        self.title("Draw a box to select area")
        self.geometry("1920x1000")  # Set initial window size
        self.minsize(400, 400)
        self.image = image
        self.scale = scale
        self.pdf_bbox = pdf_bbox
        self.bbox_dict = bbox_dict
        self.zoom_level = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.view_width = 1000
        self.view_height = 1000
        self.canvas = tk.Canvas(self, width=self.view_width, height=self.view_height)
        self.canvas.pack(fill=tk.BOTH, expand=True, side=tk.TOP)
        self.rect = None
        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.display_height = display_height
        self.result_label = tk.Label(self, text="Draw a rectangle to select area")
        self.result_label.pack(side=tk.BOTTOM, fill=tk.X)
        self.selected_coords = None
        self.name_entry = tk.Entry(self)
        self.name_entry.pack(side=tk.BOTTOM, fill=tk.X)
        self.name_entry.insert(0, "Enter name for this area")
        self.save_button = tk.Button(self, text="Save Area", command=self.save_area)
        self.save_button.pack(side=tk.BOTTOM, fill=tk.X)
        self.finish_button = tk.Button(self, text="Finish", command=self.finish)
        self.finish_button.pack(side=tk.BOTTOM, fill=tk.X)
        self.last_box = None
        self.finished = False
        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.canvas.bind('<ButtonPress-3>', self.on_pan_start)
        self.canvas.bind('<B3-Motion>', self.on_pan_move)
        self.canvas.bind('<MouseWheel>', self.on_mousewheel)  # Windows
        self.canvas.bind('<Button-4>', self.on_mousewheel)    # Linux scroll up
        self.canvas.bind('<Button-5>', self.on_mousewheel)    # Linux scroll down
        self.pan_start = None
        self.bind('<Configure>', self.on_window_resize)
        self.after(100, self.update_view)  # Schedule after window is ready
        self.bind('<Configure>', self.on_window_resize)
        self.after(100, self.resize_image_to_window)

    def resize_image_to_window(self):
        # Resize the image to fit the current window size on load
        win_width = self.winfo_width()
        win_height = self.winfo_height() - 100  # Account for controls
        if win_width > 1 and win_height > 1:
            self.view_width = win_width
            self.view_height = win_height
            self.canvas.config(width=self.view_width, height=self.view_height)
            self.update_view()

    def on_window_resize(self, event):
        if event.widget == self:
            # Subtract a bit for the menu/buttons at the bottom
            new_width = max(200, event.width)
            new_height = max(200, event.height - 100)
            self.view_width = new_width
            self.view_height = new_height
            self.canvas.config(width=self.view_width, height=self.view_height)
            self.update_view()

    def update_view(self):
        zoomed_width = int(self.image.width * self.zoom_level)
        zoomed_height = int(self.image.height * self.zoom_level)
        try:
            resample_filter = Image.Resampling.LANCZOS
        except Exception:
            resample_filter = 1
        zoomed_image = self.image.resize((zoomed_width, zoomed_height), resample_filter)
        max_x = max(0, zoomed_width - self.view_width)
        max_y = max(0, zoomed_height - self.view_height)
        self.offset_x = min(max(self.offset_x, 0), max_x)
        self.offset_y = min(max(self.offset_y, 0), max_y)
        cropped = zoomed_image.crop((self.offset_x, self.offset_y, self.offset_x + self.view_width, self.offset_y + self.view_height))
        # Prevent garbage collection of the image by keeping a reference
        self.tk_image = ImageTk.PhotoImage(cropped)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        self.rect = None
        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0

    def zoom_in(self):
        self.set_zoom(self.zoom_level * 1.25)

    def zoom_out(self):
        self.set_zoom(self.zoom_level / 1.25)

    def set_zoom(self, new_zoom):
        if new_zoom < 0.2 or new_zoom > 10:
            return
        # Adjust offset to keep center in view
        center_x = self.offset_x + self.view_width // 2
        center_y = self.offset_y + self.view_height // 2
        rel_cx = center_x / (self.image.width * self.zoom_level)
        rel_cy = center_y / (self.image.height * self.zoom_level)
        self.zoom_level = new_zoom
        zoomed_width = int(self.image.width * self.zoom_level)
        zoomed_height = int(self.image.height * self.zoom_level)
        self.offset_x = int(rel_cx * zoomed_width - self.view_width // 2)
        self.offset_y = int(rel_cy * zoomed_height - self.view_height // 2)
        self.update_view()

    def on_pan_start(self, event):
        self.pan_start = (event.x, event.y)

    def on_pan_move(self, event):
        if self.pan_start:
            dx = event.x - self.pan_start[0]
            dy = event.y - self.pan_start[1]
            self.offset_x -= dx
            self.offset_y -= dy
            self.pan_start = (event.x, event.y)
            self.update_view()

    def on_mousewheel(self, event):
        # Windows: event.delta, Linux: event.num
        if hasattr(event, 'delta') and event.delta:
            if event.delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
        elif hasattr(event, 'num'):
            if event.num == 4:
                self.zoom_in()
            elif event.num == 5:
                self.zoom_out()

    def on_press(self, event):
        self.start_x = int(event.x)
        self.start_y = int(event.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red')

    def on_drag(self, event):
        if self.rect is not None:
            self.end_x = int(event.x)
            self.end_y = int(event.y)
            self.canvas.coords(self.rect, self.start_x, self.start_y, self.end_x, self.end_y)

    def on_release(self, event):
        self.end_x = int(event.x)
        self.end_y = int(event.y)
        # Adjust for pan/zoom offsets
        x0, y0 = min(self.start_x, self.end_x), min(self.start_y, self.end_y)
        x1, y1 = max(self.start_x, self.end_x), max(self.start_y, self.end_y)
        x0_img = (x0 + self.offset_x) / self.zoom_level
        x1_img = (x1 + self.offset_x) / self.zoom_level
        y0_img = (y0 + self.offset_y) / self.zoom_level
        y1_img = (y1 + self.offset_y) / self.zoom_level
        pdf_x0, pdf_y0, pdf_x1, pdf_y1 = self.pdf_bbox
        x0_pdf = pdf_x0 + (x0_img / self.image.width) * (pdf_x1 - pdf_x0)
        x1_pdf = pdf_x0 + (x1_img / self.image.width) * (pdf_x1 - pdf_x0)
        y0_pdf = pdf_y0 + (y0_img / self.image.height) * (pdf_y1 - pdf_y0)
        y1_pdf = pdf_y0 + (y1_img / self.image.height) * (pdf_y1 - pdf_y0)
        self.last_box = (min(x0_pdf, x1_pdf), min(y0_pdf, y1_pdf), max(x0_pdf, x1_pdf), max(y0_pdf, y1_pdf))
        self.result_label.config(text=f"Selected area: x0={self.last_box[0]:.2f}, y0={self.last_box[1]:.2f}, x1={self.last_box[2]:.2f}, y1={self.last_box[3]:.2f}")

    def save_area(self):
        name = self.name_entry.get().strip()
        if name and self.last_box:
            self.bbox_dict[name] = self.last_box
            self.result_label.config(text=f"Saved area '{name}'")
            self.name_entry.delete(0, tk.END)
            self.name_entry.insert(0, "Enter name for this area")
            self.last_box = None
            if self.rect:
                self.canvas.delete(self.rect)
        else:
            self.result_label.config(text="Please select an area and enter a name.")

    def finish(self):
        self.finished = True
        self.destroy()

# Paths for saving bounding boxes and extracted text
bbox_json_path = os.path.join(pdf_folder, 'bounding_boxes.json')
csv_path = os.path.join(pdf_folder, 'extracted_text.csv')

# Main loop to collect bounding boxes
bbox_dict = {}
if os.path.exists(bbox_json_path):
    with open(bbox_json_path, 'r', encoding='utf-8') as f:
        bbox_dict = json.load(f)
    print(f"Loaded bounding boxes from {bbox_json_path}")
else:
    # Hide root window, but keep it alive for Tkinter image management
    root.withdraw()
    app = PDFCropper(root, display_image, scale, (pdf_x0, pdf_y0, pdf_x1, pdf_y1), display_height, bbox_dict)
    app.grab_set()
    app.wait_window()
    if bbox_dict:
        with open(bbox_json_path, 'w', encoding='utf-8') as f:
            json.dump(bbox_dict, f, indent=2)
        print(f"Saved bounding boxes to {bbox_json_path}")

# After closing the window or loading, extract text for each bounding box from all PDFs and save to CSV
if bbox_dict:
    # Progress bar popup
    progress_win = tk.Toplevel(root)
    progress_win.title("Extracting Text from PDFs")
    progress_label = tk.Label(progress_win, text="Extracting text from PDFs...")
    progress_label.pack(padx=20, pady=(20, 5))
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(progress_win, variable=progress_var, maximum=len(pdf_files), length=400)
    progress_bar.pack(padx=20, pady=(0, 20))
    progress_win.update()

    csv_rows = []
    header = ['filename'] + list(bbox_dict.keys())
    for idx, pdf_path in enumerate(pdf_files):
        row = {'filename': os.path.basename(pdf_path)}
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            for name, area in bbox_dict.items():
                cropped = page.within_bbox(area)
                text = cropped.extract_text()
                if text:
                    text = text.replace('\n', ' ').replace('\r', ' ')
                row[name] = text
        csv_rows.append(row)
        progress_var.set(idx + 1)
        progress_win.update()
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in csv_rows:
            writer.writerow(row)
    progress_label.config(text=f"Saved extracted text for all PDFs to {csv_path}")
    progress_win.update()
    progress_win.after(1500, progress_win.destroy)
else:
    
    print("No areas were selected.")