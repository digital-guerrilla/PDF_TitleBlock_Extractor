import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import pdfplumber
import json
import os
import csv
from tkinter import filedialog, simpledialog
import glob
import shutil
import multiprocessing
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
import re
import threading


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


def move_to_directory(src_path, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    base_name = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, base_name)
    if os.path.exists(dest_path):
        LOGGER.warning("Overwriting existing file: %s", dest_path)
        os.remove(dest_path)  # Remove the existing file
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


def prepare_page_image(pdf_path, max_width=2000, max_height=2000, resolution=150):
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
                display_width = int(orig_width * scale)
                display_height = int(orig_height * scale)
                LOGGER.debug("Scaled image size: width=%d, height=%d", display_width, display_height)
                try:
                    from PIL import Image
                    resample_filter = Image.Resampling.LANCZOS
                except Exception:
                    resample_filter = 1
                display_image = pil_image.resize((display_width, display_height), resample_filter)
                pdf_x0, pdf_y0, pdf_x1, pdf_y1 = page.mediabox
                LOGGER.debug("PDF media box: x0=%.2f, y0=%.2f, x1=%.2f, y1=%.2f", pdf_x0, pdf_y0, pdf_x1, pdf_y1)
                result = (display_image, scale, (pdf_x0, pdf_y0, pdf_x1, pdf_y1), display_height)
        except Exception as e:
            LOGGER.warning("Error while preparing page image for %s: %s", pdf_path, e)
            result = None

    result = None
    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=2)  # Set a 2-second timeout

    if thread.is_alive():
        LOGGER.warning("Timeout while preparing page image for: %s", pdf_path)
        thread.join()  # Ensure the thread is cleaned up
        return None, None, None, None

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

# Tkinter GUI to draw rectangle and get coordinates
class PDFCropper(tk.Toplevel):
    def __init__(self, master, image, scale, pdf_bbox, display_height, bbox_dict, fields):
        super().__init__(master)
        self.title("Draw a box to select area")
        self.geometry("1920x1000")  # Set initial window size
        self.minsize(400, 400)
        self.image = image
        self.scale = scale
        self.pdf_bbox = pdf_bbox
        self.bbox_dict = bbox_dict
        self.fields = fields or []
        self.current_field_index = 0
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
        self.selected_coords = None
        self.field_label = tk.Label(self, text=self._field_prompt())
        self.field_label.pack(side=tk.BOTTOM, fill=tk.X)
        self.result_label = tk.Label(self, text="Draw a rectangle to select area")
        self.result_label.pack(side=tk.BOTTOM, fill=tk.X)
        save_state = 'normal' if self.fields else 'disabled'
        self.save_button = tk.Button(self, text="Save Field", command=self.save_area, state=save_state)
        self.save_button.pack(side=tk.BOTTOM, fill=tk.X)
        finish_state = 'normal' if not self.fields else 'disabled'
        self.finish_button = tk.Button(self, text="Finish", command=self.finish, state=finish_state)
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
        self.protocol("WM_DELETE_WINDOW", self.finish)

    def _field_prompt(self):
        if self.current_field_index < len(self.fields):
            return f"Define bounding box for field: {self.fields[self.current_field_index]}"
        return "All fields collected. Click Finish."

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
        if self.current_field_index >= len(self.fields):
            self.result_label.config(text="All fields already captured. Click Finish.")
            return

        if not self.last_box:
            current_field = self.fields[self.current_field_index]
            self.result_label.config(text=f"Select an area for '{current_field}' before saving.")
            return

        field_name = self.fields[self.current_field_index]
        self.bbox_dict[field_name] = self.last_box
        self.result_label.config(text=f"Saved area for '{field_name}'")
        self.last_box = None
        if self.rect:
            self.canvas.delete(self.rect)
            self.rect = None
        self.start_x = self.start_y = self.end_x = self.end_y = 0

        self.current_field_index += 1
        if self.current_field_index >= len(self.fields):
            self.field_label.config(text="All fields collected. Click Finish.")
            self.save_button.config(state='disabled')
            self.finish_button.config(state='normal')
        else:
            self.field_label.config(text=self._field_prompt())
            self.result_label.config(text="Draw a rectangle to select area")

    def finish(self):
        if self.current_field_index < len(self.fields):
            remaining = len(self.fields) - self.current_field_index
            self.result_label.config(text=f"Please capture {remaining} more field(s) before finishing.")
            return
        self.finished = True
        self.destroy()

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

        fields = []
        while not fields:
            response = simpledialog.askstring(
                "Fields to extract",
                "Enter the field names to capture (comma or newline separated):",
                parent=root,
            )
            if response is None:
                LOGGER.warning("No fields were provided. Exiting.")
                return
            raw_fields = [item.strip() for item in re.split(r'[\n,]+', response) if item.strip()]
            unique_fields = []
            seen = set()
            for field in raw_fields:
                if field not in seen:
                    unique_fields.append(field)
                    seen.add(field)
            fields = unique_fields
            if not fields:
                LOGGER.warning("Field list was empty. Please enter at least one field.")
        LOGGER.info("Fields to capture: %s", ', '.join(fields))

        bbox_json_path = os.path.join(pdf_folder, 'bounding_boxes.json')
        csv_path = os.path.join(pdf_folder, 'extracted_text.csv')
        processed_dir = os.path.join(pdf_folder, 'Processed')
        error_dir = os.path.join(pdf_folder, 'error')

        size_examples = {}
        pdf_size_map = {}
        size_counts = {}
        size_dim_map = {}
        saved_dimensions = {}
        next_unknown_index = 1
        size_bboxes = {}
        if os.path.exists(bbox_json_path):
            try:
                with open(bbox_json_path, 'r', encoding='utf-8') as f:
                    saved_data = json.load(f)
                saved_fields = saved_data.get('fields')
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
                if saved_fields and saved_fields != fields:
                    LOGGER.warning("Existing bounding boxes use a different field list; ignoring saved data.")
                else:
                    for size, field_map in saved_data.get('sizes', {}).items():
                        size_bboxes[size] = {field: tuple(coords) for field, coords in field_map.items()}
                    if size_bboxes:
                        LOGGER.info("Loaded bounding boxes for sizes: %s", ', '.join(size_bboxes.keys()))
            except Exception as exc:
                LOGGER.exception("Failed to load existing bounding boxes: %s", exc)
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

        for size in ['A1', 'A2', 'A3', 'A0']:
            sample_pdf = size_examples.get(size)
            if not sample_pdf:
                continue
            size_boxes = size_bboxes.setdefault(size, {})
            missing_fields = [field for field in fields if field not in size_boxes]
            if not missing_fields:
                LOGGER.info("Bounding boxes for size %s already complete; reusing saved values.", size)
                continue

            LOGGER.info("Collecting bounding boxes for sheet size %s using %s", size, os.path.basename(sample_pdf))
            try:
                result = prepare_page_image(sample_pdf)
                if result is None:
                    LOGGER.warning("Skipping size %s due to timeout or error.", size)
                    continue

                display_image, scale, pdf_bbox, display_height = result
            except Exception as exc:
                LOGGER.exception("Failed to prepare image for %s: %s", sample_pdf, exc)
                continue

            collector = PDFCropper(root, display_image, scale, pdf_bbox, display_height, size_boxes, missing_fields)
            collector.grab_set()
            collector.wait_window()

            remaining = [field for field in missing_fields if field not in size_boxes]
            if remaining:
                LOGGER.warning("Bounding boxes for size %s remain incomplete (%s). Skipping further processing.", size, ', '.join(remaining))
                continue
            LOGGER.info("Captured bounding boxes for size %s.", size)

        additional_labels = [label for label in size_examples if label not in {'A1', 'A2', 'A3'}]
        additional_labels.sort(key=lambda lbl: file_order_map.get(size_examples[lbl], float('inf')))
        for label in additional_labels:
            sample_pdf = size_examples[label]
            size_boxes = size_bboxes.setdefault(label, {})
            missing_fields = [field for field in fields if field not in size_boxes]
            if not missing_fields:
                LOGGER.info("Bounding boxes for size %s already complete; reusing saved values.", label)
                continue

            LOGGER.info("Collecting bounding boxes for sheet size %s using %s", label, os.path.basename(sample_pdf))
            try:
                display_image, scale, pdf_bbox, display_height = prepare_page_image(sample_pdf)
            except Exception as exc:
                LOGGER.exception("Failed to prepare image for %s: %s", sample_pdf, exc)
                return

            collector = PDFCropper(root, display_image, scale, pdf_bbox, display_height, size_boxes, missing_fields)
            collector.grab_set()
            collector.wait_window()

            remaining = [field for field in missing_fields if field not in size_boxes]
            if remaining:
                LOGGER.warning("Bounding boxes for size %s remain incomplete (%s). Exiting.", label, ', '.join(remaining))
                return
            LOGGER.info("Captured bounding boxes for size %s.", label)

        complete_size_bboxes = {
            size: boxes for size, boxes in size_bboxes.items()
            if all(field in boxes for field in fields)
        }
        if not complete_size_bboxes:
            LOGGER.warning("No complete bounding box definitions available. Exiting.")
            return

        size_bboxes = complete_size_bboxes

        serializable_sizes = {
            size: {field: list(coords) for field, coords in boxes.items()}
            for size, boxes in size_bboxes.items()
        }
        serializable_dimensions = {
            label: list(size_dim_map[label])
            for label in size_bboxes
            if label in size_dim_map
        }
        payload = {'fields': fields, 'sizes': serializable_sizes}
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