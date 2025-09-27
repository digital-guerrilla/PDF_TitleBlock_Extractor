import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import pdfplumber
import json
import os
import csv
from tkinter import filedialog
import glob
import shutil
import multiprocessing
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import threading
from typing import Optional, Tuple


LOGGER = logging.getLogger(__name__)

POINTS_TO_MM = 25.4 / 72.0
A_SERIES_SIZES_MM = {
    'A0': (841, 1189),
    'A1': (594, 841),
    'A2': (420, 594),
    'A3': (297, 420),
    'A4': (210, 297),
    'A5': (148, 210),
}
SIZE_TOLERANCE_MM = 5


try:
    RESAMPLE_FILTER = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
except AttributeError:
    RESAMPLE_FILTER = getattr(Image, 'LANCZOS', 1)


def move_to_directory(src_path, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    base_name = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, base_name)
    if os.path.exists(dest_path):
        LOGGER.warning("Overwriting existing file: %s", dest_path)
        os.remove(dest_path)
    shutil.move(src_path, dest_path)
    LOGGER.debug("Moved %s to %s", src_path, dest_path)
    return dest_path


def classify_page_size(width_pt, height_pt, tolerance_mm=SIZE_TOLERANCE_MM):
    short_mm, long_mm = sorted((width_pt * POINTS_TO_MM, height_pt * POINTS_TO_MM))
    LOGGER.debug("Classifying page size: short_mm=%.2f, long_mm=%.2f", short_mm, long_mm)
    for name, (expected_short, expected_long) in A_SERIES_SIZES_MM.items():
        if abs(short_mm - expected_short) <= tolerance_mm and abs(long_mm - expected_long) <= tolerance_mm:
            LOGGER.debug("Matched size: %s", name)
            return name
    LOGGER.warning("No match found for page size: short_mm=%.2f, long_mm=%.2f", short_mm, long_mm)
    return None


def match_dimensions_to_label(short_mm, long_mm, dimensions_map, tolerance_mm=SIZE_TOLERANCE_MM):
    for label, dims in dimensions_map.items():
        ref_short, ref_long = dims
        if abs(short_mm - ref_short) <= tolerance_mm and abs(long_mm - ref_long) <= tolerance_mm:
            return label
    return None


def prompt_for_fields(
    parent,
    available_fields,
    default_selected=None,
) -> Tuple[Optional[list[str]], list[str]]:
    dialog = tk.Toplevel(parent)
    dialog.title("Select fields to capture")
    if parent is not None and parent.winfo_exists():
        try:
            parent.update_idletasks()
        except tk.TclError:
            pass
        if parent.winfo_viewable():
            dialog.transient(parent)
    dialog.grab_set()

    frame = tk.Frame(dialog, padx=16, pady=16)
    frame.pack(fill=tk.BOTH, expand=True)

    instruction = tk.Label(
        frame,
        text=(
            "Select the fields to capture for this run. Existing fields are listed below; "
            "use the box to add new ones if needed."
        ),
        justify=tk.LEFT,
        wraplength=420,
    )
    instruction.pack(fill=tk.X)

    list_container = tk.Frame(frame)
    list_container.pack(fill=tk.BOTH, expand=True, pady=(12, 12))

    list_scrollbar = tk.Scrollbar(list_container)
    list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    listbox = tk.Listbox(
        list_container,
        selectmode=tk.MULTIPLE,
        exportselection=False,
        width=40,
        height=12,
    )
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    listbox.config(yscrollcommand=list_scrollbar.set)
    list_scrollbar.config(command=listbox.yview)

    field_items = []
    seen = set()
    for field in available_fields or []:
        normalized = field.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        field_items.append(normalized)

    if default_selected:
        for field in default_selected:
            normalized = field.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                field_items.append(normalized)

    for field in field_items:
        listbox.insert(tk.END, field)

    if default_selected:
        default_set = {field.strip() for field in default_selected if field}
    else:
        default_set = set(field_items)

    for idx, field in enumerate(field_items):
        if field in default_set:
            listbox.select_set(idx)

    entry_frame = tk.Frame(frame)
    entry_frame.pack(fill=tk.X)

    entry_var = tk.StringVar()
    entry_label = tk.Label(entry_frame, text="New field:")
    entry_label.pack(side=tk.LEFT)
    entry = tk.Entry(entry_frame, textvariable=entry_var)
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))

    status_var = tk.StringVar(value="")

    def add_field():
        name = entry_var.get().strip()
        if not name:
            status_var.set("Enter a field name before adding.")
            entry.focus_set()
            return
        if name in field_items:
            status_var.set(f"'{name}' already exists; selecting it.")
            idx = field_items.index(name)
            listbox.select_set(idx)
            listbox.see(idx)
        else:
            field_items.append(name)
            listbox.insert(tk.END, name)
            idx = len(field_items) - 1
            listbox.select_clear(0, tk.END)
            listbox.select_set(idx)
            listbox.see(idx)
            status_var.set("")
        entry_var.set("")
        entry.focus_set()

    add_button = tk.Button(entry_frame, text="Add", command=add_field)
    add_button.pack(side=tk.RIGHT)

    status_label = tk.Label(frame, textvariable=status_var, anchor="w", fg="red")
    status_label.pack(fill=tk.X, pady=(8, 0))

    selected_fields: Optional[list[str]] = None

    def confirm():
        nonlocal selected_fields
        selection = listbox.curselection()
        if not selection:
            status_var.set("Select at least one field before continuing.")
            return
        selected_fields = [field_items[idx] for idx in selection]
        dialog.destroy()

    def cancel():
        dialog.destroy()

    button_frame = tk.Frame(frame)
    button_frame.pack(fill=tk.X, pady=(12, 0))

    cancel_button = tk.Button(button_frame, text="Cancel", command=cancel)
    cancel_button.pack(side=tk.RIGHT)

    confirm_button = tk.Button(button_frame, text="Continue", command=confirm)
    confirm_button.pack(side=tk.RIGHT, padx=(0, 8))

    entry.bind('<Return>', lambda _event: add_field())
    listbox.bind('<Return>', lambda _event: confirm())
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    dialog.update_idletasks()
    width = dialog.winfo_width()
    height = dialog.winfo_height()
    if width <= 1 or height <= 1:
        width, height = 520, 460
    screen_width = dialog.winfo_screenwidth()
    screen_height = dialog.winfo_screenheight()
    pos_x = max((screen_width - width) // 2, 0)
    pos_y = max((screen_height - height) // 2, 0)
    dialog.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
    dialog.deiconify()
    dialog.lift()
    dialog.attributes('-topmost', True)
    dialog.update()
    try:
        dialog.wait_visibility()
    except tk.TclError:
        pass
    LOGGER.debug(
        "Field selection dialog positioned at %sx%s +%s+%s (screen %sx%s)",
        width,
        height,
        pos_x,
        pos_y,
        screen_width,
        screen_height,
    )
    try:
        dialog.focus_force()
    except tk.TclError:
        dialog.after(100, lambda: dialog.focus_set())
    dialog.after_idle(lambda: dialog.attributes('-topmost', False))
    dialog.after(80, lambda: entry.focus_set())
    dialog.bell()
    try:
        if parent is not None and parent.winfo_exists():
            try:
                parent.wait_window(dialog)
            except tk.TclError:
                dialog.wait_window()
        else:
            dialog.wait_window()
    except tk.TclError:
        LOGGER.debug("Field selection dialog closed before wait_window completed")

    return selected_fields, field_items


def prepare_page_image(
    pdf_path,
    max_width=2000,
    max_height=2000,
    resolution=150,
) -> Tuple[Image.Image, float, Tuple[float, float, float, float], int]:
    LOGGER.debug("Preparing page image for: %s", pdf_path)

    def target():
        nonlocal result
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[0]
                LOGGER.debug("Page dimensions (points): width=%.2f, height=%.2f", page.width, page.height)
                pil_image = page.to_image(resolution=resolution).original
                orig_width, orig_height = pil_image.size
                LOGGER.debug("Original image size: width=%d, height=%d", orig_width, orig_height)
                scale = min(max_width / orig_width, max_height / orig_height, 1.0)
                display_width = max(1, int(orig_width * scale))
                display_height = max(1, int(orig_height * scale))
                LOGGER.debug("Scaled image size: width=%d, height=%d", display_width, display_height)
                display_image = pil_image.resize((display_width, display_height), RESAMPLE_FILTER)
                pdf_x0, pdf_y0, pdf_x1, pdf_y1 = page.mediabox
                LOGGER.debug("PDF media box: x0=%.2f, y0=%.2f, x1=%.2f, y1=%.2f", pdf_x0, pdf_y0, pdf_x1, pdf_y1)
                result = (display_image, scale, (pdf_x0, pdf_y0, pdf_x1, pdf_y1), display_height)
        except Exception as e:
            LOGGER.warning("Error while preparing page image for %s: %s", pdf_path, e)
            result = None

    result: Optional[Tuple[Image.Image, float, Tuple[float, float, float, float], int]] = None
    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=2)  # Set a 2-second timeout

    if thread.is_alive():
        LOGGER.warning("Timeout while preparing page image for: %s", pdf_path)
        thread.join()  # Ensure the thread is cleaned up
        raise RuntimeError(f"Timeout while preparing page image for {pdf_path}")

    if result is None:
        raise RuntimeError(f"Failed to render page image for {pdf_path}")

    return result


SHARED_CONFIG = None


def configure_logging(log_path, add_stream=True, level=logging.INFO):
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(processName)s %(message)s')
    root_logger = logging.getLogger()

    if add_stream and not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in root_logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    if log_path:
        abs_log_path = os.path.abspath(log_path)
        has_file_handler = any(
            isinstance(handler, logging.FileHandler) and os.path.abspath(getattr(handler, 'baseFilename', '')) == abs_log_path
            for handler in root_logger.handlers
        )
        if not has_file_handler:
            os.makedirs(os.path.dirname(abs_log_path), exist_ok=True)
            file_handler = logging.FileHandler(abs_log_path, mode='a', encoding='utf-8')
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

    root_logger.setLevel(level)


def init_worker(config, log_path, log_level):
    configure_logging(log_path, add_stream=False, level=log_level)
    global SHARED_CONFIG
    SHARED_CONFIG = config
    sizes = list(config.get('sizes', {}).keys()) if config else []
    LOGGER.debug("Worker process initialized for sizes: %s", sizes)


def extract_text_from_pdf(pdf_path, config=None):
    effective_config = config if config is not None else SHARED_CONFIG
    if not effective_config:
        raise ValueError("Extraction configuration not provided.")

    fields = effective_config.get('fields', [])
    size_boxes = effective_config.get('sizes', {})
    pdf_size_map = effective_config.get('pdf_size_map', {})
    size_dimensions = effective_config.get('dimensions', {})
    row = {'filename': os.path.basename(pdf_path)}
    for field in fields:
        row[field] = None

    LOGGER.debug("Extracting text from %s", pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        sheet_size = pdf_size_map.get(pdf_path)
        width_pt = page.width
        height_pt = page.height
        if not sheet_size:
            sheet_size = classify_page_size(width_pt, height_pt)
        if not sheet_size and size_dimensions:
            short_mm, long_mm = sorted((width_pt * POINTS_TO_MM, height_pt * POINTS_TO_MM))
            sheet_size = match_dimensions_to_label(short_mm, long_mm, size_dimensions)
        if not sheet_size:
            raise ValueError(f"Unrecognized sheet size ({width_pt:.2f}pt x {height_pt:.2f}pt)")

        boxes = size_boxes.get(sheet_size)
        if not boxes:
            raise ValueError(f"No bounding boxes defined for sheet size {sheet_size}")

        missing_fields = [field for field in fields if field not in boxes]
        if missing_fields:
            raise ValueError(f"Missing bounding boxes for fields {missing_fields} on sheet size {sheet_size}")

        for field in fields:
            area = boxes[field]
            cropped = page.within_bbox(tuple(area))
            text = cropped.extract_text()
            if text:
                text = text.replace('\n', ' ').replace('\r', ' ')
            row[field] = text

    return row


def inspect_pdf_dimensions(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            width_pt = page.width
            height_pt = page.height
            short_mm, long_mm = sorted((width_pt * POINTS_TO_MM, height_pt * POINTS_TO_MM))
            assigned_label = classify_page_size(width_pt, height_pt)
            return {
                'path': pdf_path,
                'width_pt': width_pt,
                'height_pt': height_pt,
                'short_mm': short_mm,
                'long_mm': long_mm,
                'assigned_label': assigned_label,
                'error': None,
            }
    except Exception as exc:
        LOGGER.exception("Failed to inspect %s: %s", os.path.basename(pdf_path), exc)
        return {
            'path': pdf_path,
            'width_pt': None,
            'height_pt': None,
            'short_mm': None,
            'long_mm': None,
            'assigned_label': None,
            'error': exc,
        }

# Tkinter GUI to draw rectangle and get coordinates
class PDFCropper(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.title("Bounding Box Editor")
        self.geometry("1920x1000")
        self.minsize(600, 500)

        self.image = None
        self.pdf_bbox = None
        self.display_height = None
        self.bbox_dict = {}
        self.fields = []
        self.selected_field = None
        self.zoom_level = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.view_width = 1200
        self.view_height = 900
        self.rect = None
        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.last_box = None
        self.tk_image = None
        self.pan_start = None
        self.session_done = tk.BooleanVar(value=False)
        self.session_result = False
        self.current_size = ""

        main_frame = tk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(main_frame, width=self.view_width, height=self.view_height, background="black")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(main_frame, width=320, padx=12, pady=12)
        sidebar.pack(side=tk.RIGHT, fill=tk.Y)

        self.size_label = tk.Label(sidebar, text="", anchor="w", font=("Segoe UI", 12, "bold"))
        self.size_label.pack(fill=tk.X, pady=(0, 10))

        self.field_list = tk.Listbox(sidebar, exportselection=False, height=15)
        self.field_list.pack(fill=tk.BOTH, expand=True)
        self.field_list.bind("<<ListboxSelect>>", self.on_field_select)
        self.listbox_default_bg = self.field_list.cget('bg')
        self.listbox_default_fg = self.field_list.cget('fg')
        self.listbox_saved_bg = '#E8F5E9'
        self.listbox_saved_fg = '#1B5E20'
        self.field_list.config(selectbackground='#BBDEFB', selectforeground='#0D47A1')

        self.save_button = tk.Button(sidebar, text="Save Selection", command=self.save_area, state='disabled')
        self.save_button.pack(fill=tk.X, pady=(10, 0))

        self.clear_button = tk.Button(sidebar, text="Clear Field", command=self.clear_field, state='disabled')
        self.clear_button.pack(fill=tk.X, pady=(6, 0))

        self.finish_button = tk.Button(sidebar, text="Finish Size", command=self.finish)
        self.finish_button.pack(fill=tk.X, pady=(10, 0))

        self.status_var = tk.StringVar(value="Select a field to start drawing.")
        self.status_label = tk.Label(sidebar, textvariable=self.status_var, wraplength=280, justify=tk.LEFT)
        self.status_label.pack(fill=tk.X, pady=(10, 0))

        self.result_label = tk.Label(sidebar, text="", anchor="w", wraplength=280, justify=tk.LEFT)
        self.result_label.pack(fill=tk.X, pady=(10, 0))

        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.canvas.bind('<ButtonPress-3>', self.on_pan_start)
        self.canvas.bind('<B3-Motion>', self.on_pan_move)
        self.canvas.bind('<MouseWheel>', self.on_mousewheel)
        self.canvas.bind('<Button-4>', self.on_mousewheel)
        self.canvas.bind('<Button-5>', self.on_mousewheel)

        self.bind('<Configure>', self.on_window_resize)
        self.after(100, self.resize_image_to_window)
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)

    def start_session(self, *, image, pdf_bbox, display_height, bbox_dict, fields, size_label):
        self.image = image
        self.pdf_bbox = pdf_bbox
        self.display_height = display_height
        self.bbox_dict = bbox_dict
        self.fields = list(fields)
        remaining = [field for field in self.fields if field not in self.bbox_dict]
        if remaining:
            self.selected_field = remaining[0]
            status_message = f"Draw a rectangle for '{self.selected_field}' and click Save."
        else:
            self.selected_field = self.fields[0] if self.fields else None
            status_message = "All fields captured. Adjust boxes or click Finish to continue."
        self.zoom_level = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.rect = None
        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.last_box = None
        self.session_result = False
        self.session_done = tk.BooleanVar(value=False)
        self.current_size = size_label
        self.size_label.config(text=f"Sheet size: {size_label}")
        self.status_var.set(status_message if self.selected_field else "No fields configured for this session.")
        self.result_label.config(text="")
        self.update_field_list()
        self.refresh_buttons()
        self.deiconify()
        self.lift()
        self.focus_set()
        self.update_view()
        self.grab_set()
        self.wait_variable(self.session_done)
        self.grab_release()
        self.withdraw()
        return self.session_result

    def resize_image_to_window(self):
        if self.image is None:
            return
        self.update_view()

    def on_window_resize(self, event):
        if event.widget is self and self.image is not None:
            self.update_view()

    def update_field_list(self):
        self.field_list.delete(0, tk.END)
        for field in self.fields:
            marker = "✓" if field in self.bbox_dict else " "
            self.field_list.insert(tk.END, f"[{marker}] {field}")
            idx = self.field_list.size() - 1
            if field in self.bbox_dict:
                self.field_list.itemconfig(idx, bg=self.listbox_saved_bg, fg=self.listbox_saved_fg)
            else:
                self.field_list.itemconfig(idx, bg=self.listbox_default_bg, fg=self.listbox_default_fg)
        if self.selected_field in self.fields:
            idx = self.fields.index(self.selected_field)
            self.field_list.select_clear(0, tk.END)
            self.field_list.select_set(idx)
            self.field_list.see(idx)
        else:
            self.field_list.select_clear(0, tk.END)

    def refresh_buttons(self):
        can_save = self.selected_field is not None and self.last_box is not None
        self.save_button.config(state='normal' if can_save else 'disabled')
        can_clear = self.selected_field is not None and self.selected_field in self.bbox_dict
        self.clear_button.config(state='normal' if can_clear else 'disabled')

    def on_field_select(self, _event):
        selection = self.field_list.curselection()
        if not selection:
            self.selected_field = None
            self.last_box = None
            self.status_var.set("Select a field to start drawing.")
            self.refresh_buttons()
            self.update_view()
            return
        idx = selection[0]
        self.selected_field = self.fields[idx]
        self.last_box = None
        if self.rect:
            self.canvas.delete(self.rect)
            self.rect = None
        self.status_var.set(f"Draw a rectangle for '{self.selected_field}' and click Save.")
        self.refresh_buttons()
        self.update_view()

    def update_view(self):
        if self.image is None:
            self.canvas.delete("all")
            return
        self.view_width = max(1, self.canvas.winfo_width())
        self.view_height = max(1, self.canvas.winfo_height())
        zoomed_width = max(1, int(self.image.width * self.zoom_level))
        zoomed_height = max(1, int(self.image.height * self.zoom_level))
        zoomed_image = self.image.resize((zoomed_width, zoomed_height), RESAMPLE_FILTER)
        max_x = max(0, zoomed_width - self.view_width)
        max_y = max(0, zoomed_height - self.view_height)
        self.offset_x = min(max(self.offset_x, 0), max_x)
        self.offset_y = min(max(self.offset_y, 0), max_y)
        cropped = zoomed_image.crop((self.offset_x, self.offset_y, self.offset_x + self.view_width, self.offset_y + self.view_height))
        self.tk_image = ImageTk.PhotoImage(cropped)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        self._draw_saved_boxes()
        self._draw_last_box_preview()

    def _draw_saved_boxes(self):
        if not self.pdf_bbox:
            return
        for field, box in self.bbox_dict.items():
            coords = self._pdf_to_canvas(box)
            if not coords:
                continue
            color = "#FFC107" if field == self.selected_field else "#4CAF50"
            width = 2 if field == self.selected_field else 1
            self.canvas.create_rectangle(*coords, outline=color, width=width, tags="saved_box")

    def _draw_last_box_preview(self):
        if not self.last_box or not self.selected_field:
            return
        coords = self._pdf_to_canvas(self.last_box)
        if coords:
            self.canvas.create_rectangle(*coords, outline="#2196F3", dash=(4, 2), width=2, tags="preview_box")

    def _pdf_to_canvas(self, box):
        if not self.image or not self.pdf_bbox:
            return None
        pdf_x0, pdf_y0, pdf_x1, pdf_y1 = self.pdf_bbox
        img_w, img_h = self.image.width, self.image.height
        if pdf_x1 == pdf_x0 or pdf_y1 == pdf_y0:
            return None
        x0_norm = (box[0] - pdf_x0) / (pdf_x1 - pdf_x0)
        x1_norm = (box[2] - pdf_x0) / (pdf_x1 - pdf_x0)
        y0_norm = (box[1] - pdf_y0) / (pdf_y1 - pdf_y0)
        y1_norm = (box[3] - pdf_y0) / (pdf_y1 - pdf_y0)
        x0_img = x0_norm * img_w
        x1_img = x1_norm * img_w
        y0_img = y0_norm * img_h
        y1_img = y1_norm * img_h
        x0_canvas = x0_img * self.zoom_level - self.offset_x
        x1_canvas = x1_img * self.zoom_level - self.offset_x
        y0_canvas = y0_img * self.zoom_level - self.offset_y
        y1_canvas = y1_img * self.zoom_level - self.offset_y
        if x0_canvas >= self.view_width or y0_canvas >= self.view_height or x1_canvas <= 0 or y1_canvas <= 0:
            return None
        return (
            max(0, x0_canvas),
            max(0, y0_canvas),
            min(self.view_width, x1_canvas),
            min(self.view_height, y1_canvas),
        )

    def zoom_in(self):
        self.set_zoom(self.zoom_level * 1.25)

    def zoom_out(self):
        self.set_zoom(self.zoom_level / 1.25)

    def set_zoom(self, new_zoom):
        if new_zoom < 0.2 or new_zoom > 10:
            return
        if not self.image:
            return
        center_x = self.offset_x + self.view_width // 2
        center_y = self.offset_y + self.view_height // 2
        rel_cx = center_x / max(1, self.image.width * self.zoom_level)
        rel_cy = center_y / max(1, self.image.height * self.zoom_level)
        self.zoom_level = new_zoom
        zoomed_width = max(1, int(self.image.width * self.zoom_level))
        zoomed_height = max(1, int(self.image.height * self.zoom_level))
        self.offset_x = int(rel_cx * zoomed_width - self.view_width // 2)
        self.offset_y = int(rel_cy * zoomed_height - self.view_height // 2)
        self.update_view()

    def on_pan_start(self, event):
        if self.image is None:
            return
        self.pan_start = (event.x, event.y)

    def on_pan_move(self, event):
        if self.pan_start and self.image is not None:
            dx = event.x - self.pan_start[0]
            dy = event.y - self.pan_start[1]
            self.offset_x -= dx
            self.offset_y -= dy
            self.pan_start = (event.x, event.y)
            self.update_view()

    def on_mousewheel(self, event):
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
        if self.image is None:
            return
        if not self.selected_field:
            self.status_var.set("Select a field before drawing a rectangle.")
            return
        self.start_x = int(event.x)
        self.start_y = int(event.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', dash=(4, 2))

    def on_drag(self, event):
        if self.rect is not None:
            self.end_x = int(event.x)
            self.end_y = int(event.y)
            self.canvas.coords(self.rect, self.start_x, self.start_y, self.end_x, self.end_y)

    def on_release(self, event):
        if self.image is None or not self.selected_field or self.rect is None or not self.pdf_bbox:
            return
        self.end_x = int(event.x)
        self.end_y = int(event.y)
        x0, y0 = min(self.start_x, self.end_x), min(self.start_y, self.end_y)
        x1, y1 = max(self.start_x, self.end_x), max(self.start_y, self.end_y)
        if x1 - x0 < 5 or y1 - y0 < 5:
            self.status_var.set("Draw a larger rectangle before saving.")
            return
        x0_img = (x0 + self.offset_x) / self.zoom_level
        x1_img = (x1 + self.offset_x) / self.zoom_level
        y0_img = (y0 + self.offset_y) / self.zoom_level
        y1_img = (y1 + self.offset_y) / self.zoom_level
        pdf_x0, pdf_y0, pdf_x1, pdf_y1 = self.pdf_bbox
        x0_pdf = pdf_x0 + (x0_img / self.image.width) * (pdf_x1 - pdf_x0)
        x1_pdf = pdf_x0 + (x1_img / self.image.width) * (pdf_x1 - pdf_x0)
        y0_pdf = pdf_y0 + (y0_img / self.image.height) * (pdf_y1 - pdf_y0)
        y1_pdf = pdf_y0 + (y1_img / self.image.height) * (pdf_y1 - pdf_y0)
        self.last_box = (
            min(x0_pdf, x1_pdf),
            min(y0_pdf, y1_pdf),
            max(x0_pdf, x1_pdf),
            max(y0_pdf, y1_pdf),
        )
        if self.rect:
            self.canvas.delete(self.rect)
            self.rect = None
        self.result_label.config(
            text=(
                f"Selected area for '{self.selected_field}': "
                f"x0={self.last_box[0]:.2f}, y0={self.last_box[1]:.2f}, "
                f"x1={self.last_box[2]:.2f}, y1={self.last_box[3]:.2f}"
            )
        )
        self.refresh_buttons()
        self.update_view()

    def save_area(self):
        if not self.selected_field:
            self.status_var.set("Select a field before saving.")
            return
        if not self.last_box:
            self.status_var.set(f"Draw a rectangle for '{self.selected_field}' before saving.")
            return
        self.bbox_dict[self.selected_field] = self.last_box
        self.result_label.config(text=f"Saved area for '{self.selected_field}'")
        self.last_box = None
        remaining = [field for field in self.fields if field not in self.bbox_dict]
        if remaining:
            self.selected_field = remaining[0]
            self.status_var.set(f"Captured box. Remaining fields: {', '.join(remaining)}")
        else:
            self.status_var.set("All fields captured. Click Finish to continue.")
        self.update_field_list()
        self.refresh_buttons()
        self.update_view()

    def clear_field(self):
        if not self.selected_field:
            return
        if self.selected_field in self.bbox_dict:
            del self.bbox_dict[self.selected_field]
            self.status_var.set(f"Cleared saved area for '{self.selected_field}'.")
            self.result_label.config(text="")
        self.last_box = None
        self.update_field_list()
        self.refresh_buttons()
        self.update_view()

    def finish(self):
        remaining = [field for field in self.fields if field not in self.bbox_dict]
        if remaining:
            self.status_var.set(f"Please capture remaining fields: {', '.join(remaining)}")
            target = remaining[0]
            if target in self.fields:
                idx = self.fields.index(target)
                self.field_list.select_clear(0, tk.END)
                self.field_list.select_set(idx)
                self.field_list.event_generate("<<ListboxSelect>>")
            return
        self.session_result = True
        if self.session_done is not None:
            self.session_done.set(True)

    def on_cancel(self):
        self.session_result = False
        if self.session_done is not None:
            self.session_done.set(True)
        self.withdraw()

def main():
    configure_logging(None)
    LOGGER.info("Starting PDF extraction run")
    root = tk.Tk()
    root.withdraw()
    try:
        pdf_folder = filedialog.askdirectory(title="Select folder containing PDFs")
        if not pdf_folder:
            LOGGER.warning("No folder selected. Exiting.")
            return
        log_path = os.path.join(pdf_folder, 'processing.log')
        configure_logging(log_path)
        LOGGER.info("Selected PDF folder: %s", pdf_folder)

        pdf_files = sorted(glob.glob(os.path.join(pdf_folder, '*.pdf')))
        if not pdf_files:
            LOGGER.warning("No PDF files found in the selected folder.")
            return

        bbox_json_path = os.path.join(pdf_folder, 'bounding_boxes.json')
        csv_path = os.path.join(pdf_folder, 'extracted_text.csv')
        processed_dir = os.path.join(pdf_folder, 'Processed')
        error_dir = os.path.join(pdf_folder, 'error')

        saved_data: dict = {}
        saved_fields_from_file: list[str] = []
        if os.path.exists(bbox_json_path):
            try:
                with open(bbox_json_path, 'r', encoding='utf-8') as f:
                    saved_data = json.load(f)
                saved_fields_from_file = [field for field in saved_data.get('fields', []) if field]
            except Exception as exc:
                LOGGER.exception("Failed to load existing bounding boxes: %s", exc)
                saved_data = {}
                saved_fields_from_file = []

        available_fields = list(saved_fields_from_file)
        default_active_fields = saved_data.get('active_fields', []) if saved_data else []
        if not default_active_fields:
            default_active_fields = list(available_fields)

        LOGGER.info("Prompting for fields to capture. Close the dialog to cancel.")
        root.update_idletasks()
        root.update()
        selected_fields, updated_field_pool = prompt_for_fields(root, available_fields, default_active_fields)
        if selected_fields is None:
            LOGGER.warning("No fields were selected. Exiting.")
            return

        fields = [field for field in selected_fields if field]
        if not fields:
            LOGGER.warning("No valid fields were selected. Exiting.")
            return

        LOGGER.info("Selected fields: %s", ', '.join(fields))

        all_fields = []
        seen_field_names = set()
        for field in (updated_field_pool or []):
            normalized = field.strip()
            if normalized and normalized not in seen_field_names:
                seen_field_names.add(normalized)
                all_fields.append(normalized)
        for field in fields:
            if field not in seen_field_names:
                seen_field_names.add(field)
                all_fields.append(field)

        available_fields = all_fields

        LOGGER.info("Fields to capture: %s", ', '.join(fields))

        size_examples = {}
        pdf_size_map = {}
        size_counts = {}
        size_dim_map = {}
        saved_dimensions = {}
        next_unknown_index = 1
        size_bboxes = {}
        existing_saved_bboxes = {}
        saved_fields = saved_fields_from_file
        if saved_data:
            saved_dimensions_raw = saved_data.get('dimensions', {})
            for label, dims in saved_dimensions_raw.items():
                if isinstance(dims, (list, tuple)) and len(dims) == 2:
                    try:
                        short_mm, long_mm = sorted((float(dims[0]), float(dims[1])))
                        size_dim_map[label] = (short_mm, long_mm)
                        saved_dimensions[label] = (short_mm, long_mm)
                        if label.startswith('U'):
                            try:
                                idx = int(label[1:]) + 1
                                next_unknown_index = max(next_unknown_index, idx)
                            except ValueError:
                                pass
                    except (TypeError, ValueError):
                        continue

            existing_saved_bboxes = {
                size: {
                    field: tuple(coords)
                    for field, coords in field_map.items()
                }
                for size, field_map in saved_data.get('sizes', {}).items()
            }

            current_field_set = {field for field in fields if field}
            saved_field_set = {field for field in saved_fields if field}
            preserved_fields = current_field_set & saved_field_set

            if saved_field_set:
                if not preserved_fields:
                    LOGGER.warning("Existing bounding boxes share no fields with current selection; ignoring saved data.")
                elif preserved_fields != current_field_set:
                    missing_now = sorted(current_field_set - saved_field_set)
                    removed_now = sorted(saved_field_set - current_field_set)
                    if missing_now and removed_now:
                        LOGGER.info(
                            "Reusing bounding boxes for overlapping fields (%s); new fields (%s) will need capturing;"
                            " ignoring obsolete fields (%s).",
                            ', '.join(sorted(preserved_fields)),
                            ', '.join(missing_now),
                            ', '.join(removed_now),
                        )
                    elif missing_now:
                        LOGGER.info(
                            "Reusing bounding boxes for existing fields (%s); new fields (%s) will need capturing.",
                            ', '.join(sorted(preserved_fields)),
                            ', '.join(missing_now),
                        )
                    elif removed_now:
                        LOGGER.info(
                            "Reusing bounding boxes for current fields (%s); ignoring saved fields no longer in use (%s).",
                            ', '.join(sorted(preserved_fields)),
                            ', '.join(removed_now),
                        )
                elif list(saved_field_set) != list(current_field_set):
                    LOGGER.info("Field order differs from saved data; reusing bounding boxes based on field names.")

            if preserved_fields:
                for size, field_map in existing_saved_bboxes.items():
                    filtered = {
                        field: coords
                        for field, coords in field_map.items()
                        if field in preserved_fields
                    }
                    if filtered:
                        size_bboxes[size] = filtered
                if size_bboxes:
                    LOGGER.info("Loaded bounding boxes for sizes: %s", ', '.join(size_bboxes.keys()))
        file_order_map = {path: idx for idx, path in enumerate(pdf_files)}
        LOGGER.info("Scanning PDFs to determine sheet sizes...")
        for pdf_path in pdf_files:
            width_pt = height_pt = None
            assigned_label = None
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    page = pdf.pages[0]
                    width_pt = page.width
                    height_pt = page.height
                    assigned_label = classify_page_size(width_pt, height_pt)
            except Exception as exc:
                LOGGER.exception("Failed to inspect %s: %s", os.path.basename(pdf_path), exc)

            short_mm = long_mm = None
            if width_pt is not None and height_pt is not None:
                short_mm, long_mm = sorted((width_pt * POINTS_TO_MM, height_pt * POINTS_TO_MM))
                if assigned_label:
                    canonical_dims = A_SERIES_SIZES_MM.get(assigned_label)
                    if canonical_dims:
                        size_dim_map.setdefault(assigned_label, tuple(canonical_dims))
                    else:
                        size_dim_map.setdefault(assigned_label, (short_mm, long_mm))
                else:
                    matched_label = match_dimensions_to_label(short_mm, long_mm, size_dim_map)
                    if not matched_label and saved_dimensions:
                        matched_label = match_dimensions_to_label(short_mm, long_mm, saved_dimensions)
                    if not matched_label:
                        label_candidate = f"U{next_unknown_index}"
                        while label_candidate in size_dim_map:
                            next_unknown_index += 1
                            label_candidate = f"U{next_unknown_index}"
                        size_dim_map[label_candidate] = (short_mm, long_mm)
                        assigned_label = label_candidate
                        next_unknown_index += 1
                        LOGGER.info(
                            "Assigned new sheet size label %s for %.1f mm x %.1f mm",
                            label_candidate,
                            short_mm,
                            long_mm,
                        )
                    else:
                        assigned_label = matched_label
                LOGGER.info(
                    "Sheet size for %s: %.1f mm x %.1f mm -> %s",
                    os.path.basename(pdf_path),
                    short_mm,
                    long_mm,
                    assigned_label or "Unknown",
                )
            else:
                LOGGER.info("Sheet size for %s could not be determined.", os.path.basename(pdf_path))

            pdf_size_map[pdf_path] = assigned_label
            if assigned_label:
                size_counts[assigned_label] = size_counts.get(assigned_label, 0) + 1
                size_examples.setdefault(assigned_label, pdf_path)

        for size in ['A1', 'A2', 'A3']:
            count = size_counts.get(size, 0)
            if count:
                LOGGER.info("Detected %d sheet(s) of size %s.", count, size)

        additional_count_labels = [label for label in size_counts if label not in {'A1', 'A2', 'A3'}]
        for label in sorted(additional_count_labels):
            LOGGER.info("Detected %d sheet(s) of size %s.", size_counts[label], label)

        if not size_examples:
            LOGGER.warning("No measurable sheet sizes were detected. Exiting.")
            return

        collector = None
        for size in ['A1', 'A2', 'A3', 'A0']:
            sample_pdf = size_examples.get(size)
            if not sample_pdf:
                continue
            size_boxes = size_bboxes.setdefault(size, {})
            missing_fields_before = [field for field in fields if field not in size_boxes]
            if missing_fields_before:
                LOGGER.info(
                    "Collecting bounding boxes for sheet size %s using %s (remaining fields: %s)",
                    size,
                    os.path.basename(sample_pdf),
                    ', '.join(missing_fields_before),
                )
            else:
                LOGGER.info(
                    "Reviewing existing bounding boxes for sheet size %s using %s",
                    size,
                    os.path.basename(sample_pdf),
                )
            try:
                result = prepare_page_image(sample_pdf)
            except Exception as exc:
                LOGGER.exception("Failed to prepare image for %s: %s", sample_pdf, exc)
                continue
            else:
                display_image, scale, pdf_bbox, display_height = result

            if collector is None:
                collector = PDFCropper(root)
            session_ok = collector.start_session(
                image=display_image,
                pdf_bbox=pdf_bbox,
                display_height=display_height,
                bbox_dict=size_boxes,
                fields=fields,
                size_label=size,
            )
            if not session_ok:
                LOGGER.warning("Bounding box collection cancelled by user. Exiting.")
                return

            remaining = [field for field in fields if field not in size_boxes]
            if remaining:
                LOGGER.warning(
                    "Bounding boxes for size %s remain incomplete (%s). Skipping further processing.",
                    size,
                    ', '.join(remaining),
                )
                continue
            if missing_fields_before:
                LOGGER.info("Captured bounding boxes for size %s.", size)
            else:
                LOGGER.info("Verified bounding boxes for size %s.", size)

        additional_labels = [label for label in size_examples if label not in {'A1', 'A2', 'A3'}]
        additional_labels.sort(key=lambda lbl: file_order_map.get(size_examples[lbl], float('inf')))
        for label in additional_labels:
            sample_pdf = size_examples[label]
            size_boxes = size_bboxes.setdefault(label, {})
            missing_fields_before = [field for field in fields if field not in size_boxes]
            if missing_fields_before:
                LOGGER.info(
                    "Collecting bounding boxes for sheet size %s using %s (remaining fields: %s)",
                    label,
                    os.path.basename(sample_pdf),
                    ', '.join(missing_fields_before),
                )
            else:
                LOGGER.info(
                    "Reviewing existing bounding boxes for sheet size %s using %s",
                    label,
                    os.path.basename(sample_pdf),
                )
            try:
                result = prepare_page_image(sample_pdf)
            except Exception as exc:
                LOGGER.exception("Failed to prepare image for %s: %s", sample_pdf, exc)
                return
            else:
                display_image, scale, pdf_bbox, display_height = result

            if collector is None:
                collector = PDFCropper(root)
            session_ok = collector.start_session(
                image=display_image,
                pdf_bbox=pdf_bbox,
                display_height=display_height,
                bbox_dict=size_boxes,
                fields=fields,
                size_label=label,
            )
            if not session_ok:
                LOGGER.warning("Bounding box collection cancelled by user. Exiting.")
                return

            remaining = [field for field in fields if field not in size_boxes]
            if remaining:
                LOGGER.warning("Bounding boxes for size %s remain incomplete (%s). Exiting.", label, ', '.join(remaining))
                return
            if missing_fields_before:
                LOGGER.info("Captured bounding boxes for size %s.", label)
            else:
                LOGGER.info("Verified bounding boxes for size %s.", label)

        complete_size_bboxes = {
            size: boxes for size, boxes in size_bboxes.items()
            if all(field in boxes for field in fields)
        }

        if not complete_size_bboxes:
            LOGGER.warning("No complete bounding box definitions available. Exiting.")
            return

        size_bboxes = complete_size_bboxes

        merged_sizes = {
            size: {
                field: list(coords)
                for field, coords in boxes.items()
            }
            for size, boxes in existing_saved_bboxes.items()
        }

        for size, boxes in size_bboxes.items():
            merged = merged_sizes.setdefault(size, {})
            for field, coords in boxes.items():
                merged[field] = list(coords)

        serializable_dimensions = {
            label: list(size_dim_map[label])
            for label in merged_sizes
            if label in size_dim_map
        }

        payload_fields = available_fields or list(fields)
        payload = {
            'fields': payload_fields,
            'active_fields': list(fields),
            'sizes': merged_sizes,
        }
        if serializable_dimensions:
            payload['dimensions'] = serializable_dimensions
        with open(bbox_json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        LOGGER.info("Saved bounding boxes to %s", bbox_json_path)

        processable_files = []
        skipped_files = []
        for path in pdf_files:
            if pdf_size_map.get(path) in size_bboxes:
                processable_files.append(path)
            else:
                skipped_files.append(path)

        for skipped in skipped_files:
            size_label = pdf_size_map.get(skipped) or "Unknown"
            LOGGER.warning("No bounding boxes for size %s; moving %s to error folder.", size_label, os.path.basename(skipped))
            move_to_directory(skipped, error_dir)

        if not processable_files:
            LOGGER.warning("No PDFs available for extraction after filtering. Exiting.")
            return

        extraction_pdf_size_map = {
            path: pdf_size_map[path]
            for path in processable_files
            if pdf_size_map.get(path)
        }
        extraction_dimensions = {
            label: size_dim_map[label]
            for label in size_bboxes
            if label in size_dim_map
        }
        extraction_config = {
            'fields': fields,
            'sizes': size_bboxes,
            'pdf_size_map': extraction_pdf_size_map,
            'dimensions': extraction_dimensions,
        }

        progress_win = tk.Toplevel(root)
        progress_win.title("Extracting Text from PDFs")
        progress_label = tk.Label(progress_win, text="Extracting text from PDFs...")
        progress_label.pack(padx=20, pady=(20, 5))
        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(progress_win, variable=progress_var, maximum=len(processable_files), length=400)
        progress_bar.pack(padx=20, pady=(0, 20))
        progress_win.update()
        LOGGER.info("Beginning extraction for %d PDF files", len(processable_files))

        csv_rows = []
        header = ['filename'] + list(fields)
        total_files = len(processable_files)
        max_workers = min(total_files, os.cpu_count() or 1)

        if max_workers > 1 and total_files > 1:
            LOGGER.info("Using %d worker processes", max_workers)
            with ProcessPoolExecutor(max_workers=max_workers, initializer=init_worker, initargs=(extraction_config, log_path, logging.getLogger().getEffectiveLevel())) as executor:
                future_to_meta = {
                    executor.submit(extract_text_from_pdf, pdf_path): (file_order_map[pdf_path], pdf_path)
                    for pdf_path in processable_files
                }
                completed = 0
                for future in as_completed(future_to_meta):
                    order_idx, pdf_path = future_to_meta[future]
                    try:
                        row = future.result()
                        csv_rows.append((order_idx, row))
                        move_to_directory(pdf_path, processed_dir)
                        LOGGER.info("Processed %s", os.path.basename(pdf_path))
                    except Exception as exc:
                        LOGGER.exception("Error processing %s: %s", os.path.basename(pdf_path), exc)
                        move_to_directory(pdf_path, error_dir)
                    finally:
                        completed += 1
                        progress_var.set(completed)
                        progress_win.update()
        else:
            LOGGER.info("Processing sequentially with %d worker(s)", max_workers)
            completed = 0
            for pdf_path in processable_files:
                order_idx = file_order_map[pdf_path]
                try:
                    row = extract_text_from_pdf(pdf_path, extraction_config)
                    csv_rows.append((order_idx, row))
                    move_to_directory(pdf_path, processed_dir)
                    LOGGER.info("Processed %s", os.path.basename(pdf_path))
                except Exception as exc:
                    LOGGER.exception("Error processing %s: %s", os.path.basename(pdf_path), exc)
                    move_to_directory(pdf_path, error_dir)
                finally:
                    completed += 1
                    progress_var.set(completed)
                    progress_win.update()

        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            for _, row in sorted(csv_rows, key=lambda item: item[0]):
                writer.writerow(row)
        LOGGER.info("Wrote extracted data to %s", csv_path)

        progress_label.config(text=f"Saved extracted text for all PDFs to {csv_path}")
        progress_win.update()
        progress_win.after(1500, progress_win.destroy)
        LOGGER.info("Extraction complete")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()